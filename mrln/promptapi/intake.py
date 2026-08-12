"""Image -> template intake: the generation metadata a rendered image
carries, and the two things the USER can then do with it.

Read side (`POST /mrln/prompt/extract-image`) — every dialect a generated
image actually ships:

  PNG text chunks  `parameters`          A1111 / Forge / Civitai dialect
                   `prompt` / `workflow` ComfyUI API-format graph JSON
  JPEG / WebP      EXIF `UserComment`    the same A1111 text
  civitai.com URL  that image's `meta`   over the public Civitai API

Pillow does the container reading (Class B: ComfyUI guarantees it), soft
imported INSIDE the functions — importing this module never needs it and can
never raise. No hand-rolled PNG chunk parser lives here on purpose.

Write side (`POST /mrln/prompt/extract-apply`) — the extraction feeds TWO
paths and the user picks; this module never decides for them:

  path='verbatim'   (A) the found prompt becomes a template that renders it
                    back byte for byte. No slotting, no library matching,
                    nothing rewritten — and NO LLM anywhere on the path, so
                    it works with every backend unset.
  path='decompose'  (B) the found text goes to handle_decompose unchanged
                    (programmatic / llm / hybrid).

Security notes: the request body cap in routes.py (MAX_BODY_BYTES) is the
outer gate; MAX_IMAGE_BYTES sits UNDER it so an image payload can neither
bypass nor exhaust it, and the encoded length is checked before any base64 is
decoded. A pasted Civitai URL is never fetched as given — only its numeric
image id is reused, into our own constant endpoint, so the server can never be
pointed at another host. Every client-visible string this module produces goes
through lora._scrub_secrets.
"""

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, urlsplit

from .. import promptlib as pl
from . import decompose as decompose_api
from . import lora as lora_api
from .core import ApiError, _guarded
from .settings import _read_settings

# The route body cap is 1 MiB (core.MAX_BODY_BYTES). base64 inflates by 4/3,
# so this is the largest image that still leaves room for the JSON envelope.
# Deliberately derived from that cap rather than chosen freely: raising this
# without raising the cap would only produce 413s.
MAX_IMAGE_BYTES = 700 * 1024
_MAX_B64_CHARS = -(-MAX_IMAGE_BYTES // 3) * 4  # exact base64 length of the cap

PILLOW_REMEDIATION = (
    "Pillow ships with ComfyUI — install it into this Python environment "
    "('pip install Pillow') or paste the prompt text into the De-compose box instead"
)
OVERSIZE_REMEDIATION = (
    "paste the image's civitai.com URL instead, or send only the head of the file — "
    "a PNG keeps its metadata chunks before the pixel data, so the first few hundred "
    "KB are enough to read them"
)
CIVITAI_REMEDIATION = (
    "check the Civitai API key in the Composer's Settings tab, make sure the image is "
    "public, or download the image and drop the file instead"
)

# VERIFY LIVE: Civitai's REST API documents /api/v1/images with an `imageId`
# filter, and that is what this builds. It is the one thing here that cannot be
# proven offline. If the shape ever changes, this path fails as a 502 carrying
# CIVITAI_REMEDIATION ("download the image and drop the file instead") — the
# file paths above are unaffected, so image intake never depends on it.
CIVITAI_IMAGES_ENDPOINT = "https://civitai.com/api/v1/images"
_CIVITAI_HOSTS = frozenset({"civitai.com", "www.civitai.com"})
_CIVITAI_IMAGE_PATH_RE = re.compile(r"/images/(\d+)")


class IntakeError(ApiError):
    """A refused extraction. Carries its own status and remediation so each
    dialect says the actionable thing instead of the guard's generic 400 text,
    and scrubs both strings on the way out (an intake error can quote a
    Civitai response, and the API key travels in that request's headers)."""

    def __init__(self, message, remediation, status=400):
        super().__init__(message)
        self.remediation = remediation
        self.status = status

    def body(self):
        return {
            "error": lora_api._scrub_secrets(str(self)),
            "remediation": lora_api._scrub_secrets(self.remediation),
        }


# -- payload decoding ---------------------------------------------------------

_DATA_URI_RE = re.compile(r"^data:[\w.+/-]*;base64,", re.IGNORECASE)


def decode_image_payload(value, *, key="image"):
    """A `data:` URI (or bare base64) -> bytes, capped at MAX_IMAGE_BYTES.

    The ENCODED length is checked first: refusing before b64decode means an
    oversized payload is never expanded into a second buffer."""
    if not isinstance(value, str) or not value.strip():
        raise IntakeError(
            f"missing required parameter '{key}' (a data: URI or base64 image payload)",
            'drop a PNG, JPEG or WebP onto the intake box, or send {"url": …} instead',
        )
    raw = value.strip()
    match = _DATA_URI_RE.match(raw)
    if match is not None:
        raw = raw[match.end() :]
    raw = "".join(raw.split())  # some clients line-wrap a data: URI
    if len(raw) > _MAX_B64_CHARS:
        raise IntakeError(
            f"the image payload is about {len(raw) * 3 // 4 // 1024} KiB, over the "
            f"{MAX_IMAGE_BYTES // 1024} KiB intake limit",
            OVERSIZE_REMEDIATION,
            413,
        )
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise IntakeError(
            f"'{key}' is not valid base64",
            "send the file as a data: URI (data:image/png;base64,…) or bare base64",
        ) from None
    if not data:
        raise IntakeError(
            f"'{key}' decoded to zero bytes",
            "re-read the file and send it again",
        )
    if len(data) > MAX_IMAGE_BYTES:  # defense in depth: whitespace-padded input
        raise IntakeError(
            f"the image is {len(data) // 1024} KiB, over the "
            f"{MAX_IMAGE_BYTES // 1024} KiB intake limit",
            OVERSIZE_REMEDIATION,
            413,
        )
    return data


# -- container reading (Pillow, soft imported) --------------------------------


def _image_module():
    try:
        from PIL import Image
    except Exception as exc:  # ImportError, but a broken install raises others
        raise IntakeError(
            f"reading image metadata needs Pillow (PIL), which is not importable here: {exc}",
            PILLOW_REMEDIATION,
        ) from None
    return Image


_USER_COMMENT_TAG = 0x9286
_EXIF_IFD_TAG = 0x8769
_COMMENT_PREFIXES = (b"UNICODE\x00", b"ASCII\x00\x00\x00", b"JIS\x00\x00\x00\x00\x00", b"\x00" * 8)


def decode_user_comment(raw):
    """EXIF UserComment -> text. A1111 writes the `parameters` block here with
    the 8-byte 'UNICODE\\0' character-code prefix and UTF-16 payload; other
    writers use ASCII or no prefix at all, so all of them are tried and the
    first decode without embedded NULs (the wrong-endianness tell) wins."""
    if isinstance(raw, str):
        return raw.strip("\x00").strip()
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    blob = bytes(raw)
    bodies = [blob]
    if blob[:8] in _COMMENT_PREFIXES:
        bodies.insert(0, blob[8:])
    for body in bodies:
        for encoding in ("utf-8", "utf-16-be", "utf-16-le"):
            try:
                text = body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            if "\x00" in text.strip("\x00"):
                continue  # decoded with the wrong width/endianness
            text = text.strip("\x00").strip()
            if text:
                return text
    return ""


def _exif_user_comment(img):
    try:
        exif = img.getexif()
    except Exception:
        return ""
    raw = None
    try:
        raw = exif.get_ifd(_EXIF_IFD_TAG).get(_USER_COMMENT_TAG)
    except Exception:
        raw = None
    if raw is None:
        try:
            raw = exif.get(_USER_COMMENT_TAG)
        except Exception:
            raw = None
    return decode_user_comment(raw)


def read_image_metadata(data):
    """(container format, {field: text}) — every text-ish metadata field
    Pillow exposes: PNG tEXt/zTXt/iTXt chunks (`.text`), the rest of `.info`,
    and the EXIF UserComment JPEG/WebP carry the same A1111 block in."""
    import io

    image_module = _image_module()
    try:
        with image_module.open(io.BytesIO(data)) as img:
            fmt = str(img.format or "")
            fields = {}
            for source in (getattr(img, "text", None) or {}, getattr(img, "info", None) or {}):
                for key, value in source.items():
                    if isinstance(value, str) and value.strip() and str(key) not in fields:
                        fields[str(key)] = value
            comment = _exif_user_comment(img)
            if comment:
                fields.setdefault("UserComment", comment)
    except IntakeError:
        raise
    except Exception as exc:
        raise IntakeError(
            f"this file could not be read as an image ({type(exc).__name__}: {exc})",
            "drop a PNG, JPEG or WebP written by a generator — a truncated or "
            "re-encoded file usually lost its metadata on the way",
        ) from None
    return fmt, fields


# -- A1111 / Forge / Civitai `parameters` dialect ------------------------------

# The tail is the LAST line and only counts as one when it parses into several
# key/value pairs AND names at least one setting real generators emit. That is
# the whole disambiguation: a prompt whose last line merely contains a colon
# ("Style: neon") stays part of the prompt.
_KNOWN_TAIL_KEYS = frozenset(
    {
        "steps",
        "sampler",
        "schedule type",
        "cfg scale",
        "distilled cfg scale",
        "guidance",
        "seed",
        "size",
        "model",
        "model hash",
        "vae",
        "vae hash",
        "denoising strength",
        "clip skip",
        "version",
        "hires upscale",
        "hires upscaler",
        "civitai resources",
        "civitai metadata",
    }
)
_NEGATIVE_LABEL_RE = re.compile(r"^\s*Negative prompt:[ \t]?(.*)$", re.IGNORECASE)
_TAIL_KEY_RE = re.compile(r"[ \t]*([A-Za-z][A-Za-z0-9 _\-/().]*?)[ \t]*:[ \t]*")
_TAIL_NEXT_RE = re.compile(r",[ \t]*(?=[A-Za-z][A-Za-z0-9 _\-/().]*?[ \t]*:)")
_TAIL_SEP_RE = re.compile(r"[ \t]*,[ \t]*")
_BRACKETS = {"[": "]", "{": "}"}


def _balanced_end(text, start):
    """Index of the bracket closing `text[start]`, or None. String aware, so a
    ']' inside a JSON string value never ends it early."""
    depth = 0
    in_string = False
    escaped = False
    closer = _BRACKETS[text[start]]
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _BRACKETS:
            depth += 1
        elif char in ("]", "}"):
            depth -= 1
            if depth == 0:
                return pos if char == closer else None
    return None


def parse_param_tail(line):
    """'Steps: 20, Sampler: DPM++ 2M, Civitai resources: [{…}]' -> dict.

    Values carry commas (Civitai's resource array is JSON), so this scans for
    the next 'Key:' boundary honoring brackets and quotes instead of splitting
    on ', ' the way a naive reader would."""
    params = {}
    pos, end = 0, len(line)
    while pos < end:
        key_match = _TAIL_KEY_RE.match(line, pos)
        if key_match is None:
            break
        key = key_match.group(1).strip()
        pos = key_match.end()
        if pos < end and line[pos] in _BRACKETS:
            stop = _balanced_end(line, pos)
            stop = end if stop is None else stop + 1
        elif pos < end and line[pos] == '"':
            quoted = line.find('"', pos + 1)
            stop = end if quoted == -1 else quoted + 1
        else:
            following = _TAIL_NEXT_RE.search(line, pos)
            stop = following.start() if following is not None else end
        params[key] = line[pos:stop].strip()
        pos = stop
        separator = _TAIL_SEP_RE.match(line, pos)
        if separator is None:
            break  # no ', ' follows: whatever is left is not a k/v tail
        pos = separator.end()
    return params


def is_param_tail(line):
    """True when `line` is a generator's settings tail rather than prompt."""
    params = parse_param_tail(line)
    return len(params) >= 2 and any(key.casefold() in _KNOWN_TAIL_KEYS for key in params)


def parse_a1111_parameters(text):
    """{positive, negative, params} out of the A1111/Forge/Civitai dialect:
    positive block, optional `Negative prompt:` block, trailing settings
    tail. All three parts are optional — a bare prompt parses as positive."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    tail_index = None
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            continue
        if is_param_tail(lines[index].strip()):
            tail_index = index
        break  # only the last non-empty line can be the tail
    params = parse_param_tail(lines[tail_index].strip()) if tail_index is not None else {}
    body = lines[:tail_index] if tail_index is not None else lines
    positive, negative, in_negative = [], [], False
    for line in body:
        label = _NEGATIVE_LABEL_RE.match(line)
        if label is not None and not in_negative:
            in_negative = True
            negative.append(label.group(1))
            continue
        (negative if in_negative else positive).append(line)
    return {
        "positive": "\n".join(positive).strip(),
        "negative": "\n".join(negative).strip(),
        "params": params,
    }


def _param(params, name):
    """Case-insensitive parameter lookup ('CFG scale' vs 'cfg scale')."""
    wanted = str(name).casefold()
    for key, value in (params or {}).items():
        if str(key).casefold() == wanted:
            return value
    return None


# -- Civitai resource lists ---------------------------------------------------


def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_civitai_resources(raw):
    """The `Civitai resources` tail (a JSON array, or the already-parsed list
    a Civitai API `meta` carries) -> normalized resource entries.

    An AIR is only reported when the source states one: an AIR's third segment
    IS the base-model family (lora_base_family reads it), so inventing an
    ecosystem here would make LoRA Apply warn about a mismatch that does not
    exist. Entries without one keep their modelVersionId, which resolve_air
    turns into a real AIR when the user asks."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").strip().lower() or "unknown"
        weight = entry.get("weight") if "weight" in entry else entry.get("strength")
        item = {
            "type": kind,
            "model_name": str(entry.get("modelName") or entry.get("name") or "") or None,
            "version_name": str(entry.get("modelVersionName") or "") or None,
            "model_id": _as_int(entry.get("modelId")),
            "model_version_id": _as_int(entry.get("modelVersionId") or entry.get("versionId")),
            "weight": _as_float(weight),
        }
        air = str(entry.get("air") or "").strip()
        if air.lower().startswith("urn:air:"):
            item["air"] = air
        out.append(item)
    return out


# -- inline <lora:…> tags -----------------------------------------------------

_LORA_TAG_RE = re.compile(
    r"<(?:lora|lyco|lycoris)\s*:\s*([^:<>]+?)\s*"
    r"(?::\s*(-?\d+(?:\.\d+)?)\s*)?(?::\s*(-?\d+(?:\.\d+)?)\s*)?>",
    re.IGNORECASE,
)


def _tidy_seams(text):
    """Close the hole a removed tag leaves: no doubled commas, no doubled
    spaces at the seam, no comma dangling at the end of a line, no line left
    blank by a tag that had one to itself. Only ever applied when a tag was
    actually removed — a prompt nobody edited must come out untouched."""
    out = re.sub(r"[ \t]{2,}", " ", str(text or ""))
    out = re.sub(r"[ \t]*,(?:[ \t]*,)+", ",", out)
    out = re.sub(r"[ \t]*,[ \t]*(?=\n|$)", "", out)
    out = re.sub(r"(?m)^[ \t]*,[ \t]*", "", out)
    out = re.sub(r"\n[ \t]*\n", "\n", out)
    return out.strip().strip(",").strip()


def strip_lora_tags(text):
    """(prompt without the tags, [{name, strength_model, strength_clip}]).

    A1111 writes the LoRA stack INTO the prompt text. Those tags are inert
    tokens for every ComfyUI loader, so they leave the prompt and become real
    entries the AIR/download machinery can act on."""
    found = []

    def take(match):
        name = (match.group(1) or "").strip()
        if not name:
            return match.group(0)
        strength_model = _as_float(match.group(2))
        strength_model = 1.0 if strength_model is None else strength_model
        strength_clip = _as_float(match.group(3))
        found.append(
            {
                "name": name,
                "strength_model": strength_model,
                "strength_clip": strength_model if strength_clip is None else strength_clip,
            }
        )
        return ""

    raw = str(text or "")
    cleaned = _LORA_TAG_RE.sub(take, raw)
    # nothing was removed -> nothing to tidy; handing back the input verbatim
    # is what keeps a tag-free prompt's own formatting intact
    return (_tidy_seams(cleaned) if found else raw), found


def _norm_name(name):
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def merge_lora_resources(loras, resources):
    """Fold Civitai lora/lycoris resources into the tag-derived entries.

    Matched by normalized name (Civitai's modelName and the `<lora:…>` tag name
    are the same string modulo punctuation); an unmatched resource becomes its
    own entry, because Civitai lists LoRAs that a re-uploaded prompt no longer
    names inline. Tag strength always wins: it is what the prompt said."""
    merged = [dict(entry) for entry in loras]
    keys = [_norm_name(entry.get("name")) for entry in merged]
    for resource in resources:
        if resource.get("type") not in ("lora", "lycoris", "locon", "dora"):
            continue
        candidates = [
            _norm_name(resource.get("model_name")),
            _norm_name(resource.get("version_name")),
        ]
        target = None
        for candidate in candidates:
            if not candidate:
                continue
            for index, key in enumerate(keys):
                if key and (key == candidate or key in candidate or candidate in key):
                    target = merged[index]
                    break
            if target is not None:
                break
        if target is None:
            weight = resource.get("weight")
            weight = 1.0 if weight is None else weight
            target = {
                "name": resource.get("model_name") or f"civitai-{resource.get('model_version_id')}",
                "strength_model": weight,
                "strength_clip": weight,
            }
            merged.append(target)
            keys.append(_norm_name(target["name"]))
        for key in ("air", "model_id", "model_version_id", "model_name", "version_name"):
            if resource.get(key) and not target.get(key):
                target[key] = resource[key]
    return merged


def attach_local_files(loras):
    """Point each entry at the installed .safetensors it means, and lift the
    trigger word out of that file's own metadata when it has one.

    An A1111 tag carries no folder and no extension, so this tries the exact
    name, then '<name>.safetensors', then a stem match across the installed
    list — the same tolerance _resolve_lora_file already applies to case and
    separators. Three distinct answers, and the difference matters to the UI:
    `file: None` = there was no ComfyUI to ask, `file: ""` = asked and the file
    is not installed here, a name = installed."""
    for entry in loras:
        name = str(entry.get("name") or "")
        entry.setdefault("file", None)
        probe = lora_api._resolve_lora_file(name)
        if probe is None:
            continue  # outside ComfyUI: nothing to resolve against
        real = probe[0] if probe[1] is not None else ""
        if not real:
            alt = lora_api._resolve_lora_file(f"{name}.safetensors")
            real = alt[0] if alt and alt[1] is not None else ""
        real = real or _stem_match(name) or ""
        entry["file"] = real
        if not real:
            continue
        resolved = lora_api._resolve_lora_file(real)
        path = resolved[1] if resolved else None
        if path is None:
            continue
        try:
            trigger, _source = pl.trigger_from_metadata(pl.read_safetensors_metadata(path))
        except Exception:
            continue
        if trigger and not entry.get("catchword"):
            entry["catchword"] = trigger
    return loras


def _stem_match(name):
    """The installed LoRA whose bare file stem matches a tag name, or ''/None
    (None = no ComfyUI to ask)."""
    try:
        import folder_paths
    except ImportError:
        return None
    stem = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if not stem:
        return ""
    for installed in folder_paths.get_filename_list("loras"):
        base = installed.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base.rsplit(".", 1)[0] == stem:
            return installed
    return ""


def resolve_air(lib, loras):
    """Turn a bare modelVersionId into a real AIR (plus trained words) through
    the Civitai model-version endpoint — the same call and the same summary
    the LoRA lookup already uses, so the AIR's ecosystem segment is the one
    Civitai states rather than one we made up. Opt-in (`resolve: true`): it is
    one request per LoRA and the file dialects work fully without it. Never
    raises: an unresolvable entry simply keeps no AIR."""
    import urllib.request

    key = lora_api._remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for entry in loras:
        version_id = _as_int(entry.get("model_version_id"))
        if entry.get("air") or version_id is None:
            continue
        try:
            request = urllib.request.Request(
                f"https://civitai.com/api/v1/model-versions/{version_id}", headers=headers
            )
            with urllib.request.urlopen(request, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
        summary = lora_api._civitai_summary(data)
        if summary.get("air"):
            entry["air"] = summary["air"]
        if summary.get("trained_words"):
            entry["trained_words"] = summary["trained_words"]
            entry.setdefault("catchword", summary["trained_words"][0])
        for key_name, value in (
            ("model_name", summary.get("model_name")),
            ("version_name", summary.get("version_name")),
        ):
            if value and not entry.get(key_name):
                entry[key_name] = value
    return loras


# -- ComfyUI graphs -----------------------------------------------------------

_SAMPLER_RE = re.compile(r"sampler", re.IGNORECASE)
_TEXT_INPUT_KEYS = ("text", "text_g", "text_l", "populated_text", "wildcard_text", "prompt")
_ENCODE_RE = re.compile(r"text.?encode|textencode|prompt", re.IGNORECASE)
_FOLLOW_DEPTH = 8


def _follow_text(nodes, link, depth=0, seen=frozenset()):
    """Walk back from a conditioning link to the string widget(s) feeding it,
    through whatever pass-through nodes sit in between (combine, concat,
    ControlNet apply, …). Returns [(text, node_id, class_type)]."""
    if depth > _FOLLOW_DEPTH or not isinstance(link, list) or not link:
        return []
    node_id = str(link[0])
    if node_id in seen:
        return []
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return []
    class_type = str(node.get("class_type") or "")
    inputs = node.get("inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    direct = [
        (inputs[key], node_id, class_type)
        for key in _TEXT_INPUT_KEYS
        if isinstance(inputs.get(key), str) and inputs[key].strip()
    ]
    if direct:
        return direct
    out = []
    for value in inputs.values():
        if isinstance(value, list):
            out.extend(_follow_text(nodes, value, depth + 1, seen | {node_id}))
    return out


def _bare_text_nodes(nodes):
    """Fallback for a graph with no sampler we recognize: every text-encode-ish
    node's string, role unknown so the UI still asks rather than guessing."""
    out = []
    for node_id, node in nodes.items():
        class_type = str(node.get("class_type") or "")
        if not _ENCODE_RE.search(class_type):
            continue
        inputs = node.get("inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        for key in _TEXT_INPUT_KEYS:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                out.append(
                    {
                        "role": "unknown",
                        "text": value,
                        "node": node_id,
                        "class_type": class_type,
                        "input": key,
                    }
                )
    return out


def graph_candidates(graph):
    """Every prompt string an API-format ComfyUI graph offers, tagged with the
    role the sampler gave it. Sampler-class nodes name their conditioning
    inputs `positive`/`negative`; following those links is the only reliable
    read of a graph we did not write."""
    nodes = {str(key): value for key, value in graph.items() if isinstance(value, dict)}
    candidates = []
    for node_id, node in nodes.items():
        if not _SAMPLER_RE.search(str(node.get("class_type") or "")):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for role in ("positive", "negative"):
            for text, source_id, source_class in _follow_text(nodes, inputs.get(role)):
                candidates.append(
                    {
                        "role": role,
                        "text": text,
                        "node": source_id,
                        "class_type": source_class,
                        "sampler": node_id,
                        "sampler_class": str(node.get("class_type") or ""),
                    }
                )
    if not candidates:
        candidates = _bare_text_nodes(nodes)
    seen = set()
    unique = []
    for candidate in candidates:
        key = (candidate["role"], candidate["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def workflow_candidates(graph):
    """The UI-format `workflow` chunk: no links to follow reliably, so every
    text-encode node's widget string becomes an unknown-role candidate."""
    out = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("type") or "")
        if not _ENCODE_RE.search(class_type):
            continue
        for value in node.get("widgets_values") or []:
            if isinstance(value, str) and value.strip():
                out.append(
                    {
                        "role": "unknown",
                        "text": value,
                        "node": str(node.get("id", "")),
                        "class_type": class_type,
                    }
                )
    return out


def extraction_from_candidates(candidates):
    """Candidates -> {positive, negative} only where they are UNAMBIGUOUS.

    One positive string and at most one negative resolves; anything else
    returns the full candidate list with `ambiguous` set so the UI can offer a
    picker. Never guess silently — a wrong pick is worse than a question."""
    positives = [c for c in candidates if c["role"] == "positive"]
    negatives = [c for c in candidates if c["role"] == "negative"]
    out = {"positive": "", "negative": "", "params": {}, "candidates": list(candidates)}
    if len(positives) == 1:
        out["positive"] = positives[0]["text"]
    if len(negatives) == 1:
        out["negative"] = negatives[0]["text"]
    if len(positives) != 1 or len(negatives) > 1:
        out["ambiguous"] = True
    return out


# -- dialect dispatch ---------------------------------------------------------


def _field(fields, name):
    wanted = str(name).casefold()
    for key, value in (fields or {}).items():
        if str(key).casefold() == wanted and isinstance(value, str) and value.strip():
            return value
    return None


def _finish(result, *, lib=None, resolve=False):
    """Common tail for every dialect: pull the inline LoRA tags out of both
    prompts, fold in the Civitai resources, and resolve what we can."""
    notes = result.setdefault("notes", [])
    positive, loras = strip_lora_tags(result.get("positive") or "")
    negative, negative_loras = strip_lora_tags(result.get("negative") or "")
    result["positive"] = positive
    result["negative"] = negative
    loras.extend(negative_loras)
    resources = result.get("resources") or []
    raw_resources = _param(result.get("params") or {}, "Civitai resources")
    if raw_resources and not resources:
        resources = parse_civitai_resources(raw_resources)
    if resources:
        result["resources"] = resources
        loras = merge_lora_resources(loras, resources)
    attach_local_files(loras)
    if resolve and lib is not None:
        resolve_air(lib, loras)
    result["loras"] = loras
    unresolved = [e for e in loras if not e.get("air") and e.get("model_version_id")]
    if unresolved and not resolve:
        notes.append(
            f"{len(unresolved)} LoRA(s) carry a Civitai modelVersionId but no AIR — "
            "re-run the extraction with resolve=true to look their AIR up, which is "
            "what lets a missing file heal itself"
        )
    missing = [e for e in loras if e.get("file") == ""]
    if missing:
        notes.append(
            f"{len(missing)} LoRA(s) named in the prompt are not installed here: "
            + ", ".join(sorted(str(e.get("name")) for e in missing)[:6])
        )
    return result


def extraction_from_fields(fields, *, fmt="", lib=None, resolve=False):
    """The metadata fields of one image file -> the extraction result.

    Dialect precedence: `parameters` first (unambiguous text), then the
    ComfyUI API graph in `prompt`, then the UI graph in `workflow`. A file
    carrying both gets the text one, and says so."""
    result = None
    parameters = _field(fields, "parameters")
    comment = _field(fields, "UserComment")
    text = parameters or comment
    if text:
        result = parse_a1111_parameters(text)
        result["dialect"] = "a1111"
        result["source"] = "parameters" if parameters else "exif-usercomment"
    graph_field = _field(fields, "prompt")
    if result is None and graph_field:
        try:
            graph = json.loads(graph_field)
        except (TypeError, ValueError):
            graph = None
        if isinstance(graph, dict):
            result = extraction_from_candidates(graph_candidates(graph))
            result["dialect"] = "comfyui"
            result["source"] = "comfy-prompt"
    workflow_field = _field(fields, "workflow")
    if result is None and workflow_field:
        try:
            graph = json.loads(workflow_field)
        except (TypeError, ValueError):
            graph = None
        if isinstance(graph, dict):
            result = extraction_from_candidates(workflow_candidates(graph))
            result["dialect"] = "comfyui"
            result["source"] = "comfy-workflow"
            result.setdefault("notes", []).append(
                "read from the UI-format 'workflow' chunk: it carries no reliable "
                "positive/negative roles, so every prompt string is offered as a candidate"
            )
    if result is None:
        raise IntakeError(
            "no generation metadata in this image"
            + (f" (fields present: {', '.join(sorted(fields)[:8])})" if fields else ""),
            "the image was re-encoded or stripped (most upload pipelines do that) — "
            "use the original file, paste its civitai.com URL, or paste the prompt text",
        )
    result["container"] = fmt
    if text and graph_field:
        result.setdefault("notes", []).append(
            "this file carries both an A1111 'parameters' block and a ComfyUI graph; "
            "the text block was used because it names positive and negative outright"
        )
    return _finish(result, lib=lib, resolve=resolve)


# -- Civitai image URLs -------------------------------------------------------


def civitai_image_id(url):
    """The numeric image id out of a civitai.com image URL.

    The host is checked and then discarded: every request built from this is
    our own constant endpoint plus an integer, so a pasted URL can never make
    the server fetch somewhere else."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise IntakeError(
            f"'{url}' is not an http(s) URL",
            "paste a https://civitai.com/images/<id> link, or drop the image file",
        )
    host = (parts.hostname or "").lower()
    if host not in _CIVITAI_HOSTS:
        raise IntakeError(
            f"'{host or url}' is not a civitai.com URL — only Civitai image pages are fetched",
            "paste a https://civitai.com/images/<id> link, or drop the image file itself",
        )
    match = _CIVITAI_IMAGE_PATH_RE.search(parts.path)
    if match is not None:
        return int(match.group(1))
    query = parse_qs(parts.query)
    for key in ("imageId", "imageid"):
        raw = (query.get(key) or [""])[0]
        if raw.isdigit():
            return int(raw)
    raise IntakeError(
        f"no image id in '{url}'",
        "use the URL of the image PAGE (https://civitai.com/images/12345678) — a CDN "
        "image link carries no id, so download that file and drop it instead",
    )


def _civitai_get(url, headers):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise IntakeError(f"Civitai answered HTTP {exc.code}", CIVITAI_REMEDIATION, 502) from None
    except Exception as exc:
        raise IntakeError(
            f"Civitai unreachable: {type(exc).__name__}: {exc}",
            "check your network and retry, or download the image and drop the file",
            502,
        ) from None


def fetch_civitai_image(lib, image_id):
    """That image's API record. The key rides an Authorization header and is
    remembered for scrubbing, so it can never surface in an error string."""
    key = lora_api._remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    base = f"{CIVITAI_IMAGES_ENDPOINT}?imageId={int(image_id)}&limit=1"
    # Two attempts: the plain query first, then again asking for every rating.
    # The endpoint's default nsfw filter hides mature images from an anonymous
    # read, and "my own car render came back empty" is not an answer.
    for url in (base, f"{base}&nsfw=X"):
        data = _civitai_get(url, headers)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    raise IntakeError(
        f"Civitai returned no record for image {int(image_id)}",
        CIVITAI_REMEDIATION,
        404,
    )


_CIVITAI_META_LABELS = {
    "cfgscale": "CFG scale",
    "sampler": "Sampler",
    "steps": "Steps",
    "seed": "Seed",
    "model": "Model",
    "clipskip": "Clip skip",
    "denoise": "Denoising strength",
    "scheduler": "Schedule type",
}
_CIVITAI_META_SKIP = frozenset(
    {"prompt", "negativeprompt", "parameters", "resources", "civitairesources", "hashes"}
)


def extraction_from_civitai_item(item):
    """A Civitai image record -> the extraction result. `meta` is the A1111
    tail already parsed into a dict, so the `parameters` string is preferred
    when present and the known keys are mapped otherwise."""
    meta = item.get("meta") if isinstance(item, dict) else None
    if not isinstance(meta, dict) or not meta:
        raise IntakeError(
            "this Civitai image carries no generation metadata "
            "(the uploader stripped it, or it was not made on Civitai)",
            "drop the image file itself, or paste the prompt text into the De-compose box",
        )
    raw = meta.get("parameters")
    if isinstance(raw, str) and raw.strip():
        result = parse_a1111_parameters(raw)
    else:
        params = {}
        for key, value in meta.items():
            if str(key).replace(" ", "").casefold() in _CIVITAI_META_SKIP:
                continue
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                label = _CIVITAI_META_LABELS.get(str(key).replace(" ", "").casefold(), str(key))
                params[label] = str(value)
        width, height = item.get("width"), item.get("height")
        if "Size" not in params and _as_int(width) and _as_int(height):
            params["Size"] = f"{_as_int(width)}x{_as_int(height)}"
        result = {
            "positive": str(meta.get("prompt") or "").strip(),
            "negative": str(meta.get("negativePrompt") or "").strip(),
            "params": params,
        }
    resources = parse_civitai_resources(meta.get("civitaiResources"))
    resources.extend(parse_civitai_resources(meta.get("resources")))
    if resources:
        result["resources"] = resources
    result["dialect"] = "civitai"
    result["source"] = "civitai-api"
    result["container"] = ""
    result["image_id"] = _as_int(item.get("id"))
    return result


# -- path A: the verbatim template --------------------------------------------

_BRACE_TABLE = str.maketrans({"{": "{{", "}": "}}"})
_PARAM_ORDER = (
    "Model",
    "Model hash",
    "Steps",
    "Sampler",
    "Schedule type",
    "CFG scale",
    "Distilled CFG Scale",
    "Guidance",
    "Seed",
    "Size",
    "Denoising strength",
    "Clip skip",
    "VAE",
    "Version",
)
_PARAM_SKIP = frozenset({"civitai resources", "civitai metadata"})
# The joiner must NOT start with ',': the comma-joined assembly strips a
# trailing '.' off every part (render._string), which would silently edit a
# verbatim prompt. With one part the joiner is otherwise unused.
VERBATIM_RENDER = {"format": "string", "joiner": "\n"}


def escape_braces(text):
    """'{'/'}' are the engine's variable and wildcard syntax, and a verbatim
    prompt has to survive expand() untouched — so both braces are doubled into
    the documented literal form, which expands back to single ones."""
    return str(text or "").translate(_BRACE_TABLE)


def params_summary(params):
    """The generation settings as a stable text block. RECORDED, never
    rendered: a template emits prompt text, not sampler settings, and losing
    'which model and CFG made this' would gut the point of the intake."""
    if not params:
        return ""
    lines = []
    used = set()
    for key in _PARAM_ORDER:
        value = _param(params, key)
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
            used.add(str(key).casefold())
    for key, value in params.items():
        folded = str(key).casefold()
        if folded in used or folded in _PARAM_SKIP or value in (None, ""):
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _item_name(name):
    """A library item name from a LoRA tag name. Not a slug (only section
    slugs are path-validated), but schema rejects the selection control tokens
    as item names — an item called 'random' could never be picked — so a name
    that lands on one is prefixed instead of written and refused."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-.")[:40].rstrip("-.")
    if not slug:
        return "lora"
    return f"lora-{slug}" if slug in ("random", "off") else slug


def build_lora_section(extraction, *, label="Intake LoRAs"):
    """The companion user section for path A: one item per extracted LoRA,
    carrying the file, the strengths, the AIR (so a missing file can heal
    itself) and the trained words as provenance.

    Path A does NO slotting — that is its contract — so these items are the
    record, not part of the render: an item's text is its catchword, and
    slotting one would append that catchword to a prompt that already reads
    exactly as it did in the image. The Composer wires them into a slot when
    the user wants them drawn."""
    items = []
    used = set()
    for entry in extraction.get("loras") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        base = _item_name(name)
        item_name = base
        suffix = 2
        while item_name in used:
            item_name = f"{base}-{suffix}"
            suffix += 1
        used.add(item_name)
        data = {"lora": str(entry.get("file") or f"{name}.safetensors")}
        strength_model = _as_float(entry.get("strength_model"))
        strength_clip = _as_float(entry.get("strength_clip"))
        if strength_model is not None:
            data["strength_model"] = strength_model
        if strength_clip is not None:
            data["strength_clip"] = strength_clip
        if entry.get("air"):
            data["comment"] = str(entry["air"])
        words = [str(w) for w in entry.get("trained_words") or [] if str(w).strip()]
        if words:
            data["lora_info"] = lora_api.lora_info({"trained_words": words})
        catchword = str(entry.get("catchword") or "").strip()
        if not catchword and words:
            catchword = lora_api.render_catchword(words, lora_api.default_trigger_selection(words))
        items.append({"name": item_name, "text": catchword or name, "data": data})
    if not items:
        return None
    return {"version": 1, "label": label, "items": items}


def build_verbatim_template(extraction, *, label="", source=""):
    """The path-A template: the found prompt, reproduced.

    Byte-for-byte holds by construction, not by luck:
      * the positive goes into `prefix` with its braces escaped, so expand()
        hands the identical string back and no {variable} is ever looked up;
      * there are NO slots, so nothing else joins the assembly and the inline
        weaving path (which collapses whitespace) never runs;
      * the joiner does not start with ',', so the comma-assembly's trailing
        '.' strip never touches it;
      * the negative is the template negative alone, and with no slots
        resolve() joins exactly that one string.
    handle_extract_apply proves it per request by composing the result."""
    positive = str(extraction.get("positive") or "")
    negative = str(extraction.get("negative") or "")
    lines = [
        "Verbatim image intake" + (f" ({source})" if source else "") + ".",
        "The prompt renders exactly as it was found — no slotting, no library "
        "matching, nothing rewritten.",
    ]
    settings = params_summary(extraction.get("params") or {})
    if settings:
        lines.append("")
        lines.append("Generation settings as found:")
        lines.append(settings)
    loras = extraction.get("loras") or []
    if loras:
        lines.append("")
        lines.append("LoRAs as found:")
        for entry in loras:
            air = f"  {entry['air']}" if entry.get("air") else ""
            lines.append(
                f"  <lora:{entry.get('name')}:{entry.get('strength_model')}>{air}".rstrip()
            )
    data = {
        "version": 1,
        "label": label or "Image intake (verbatim)",
        "description": "\n".join(lines),
        "prefix": escape_braces(positive),
        "slots": [],
        "render": dict(VERBATIM_RENDER),
    }
    if negative:
        data["negative"] = negative
    return data


def _extraction_arg(payload):
    """The extraction the client is acting on: the whole object it got back,
    or the flat fields, so a caller can drive path A from hand-written JSON."""
    raw = payload.get("extraction")
    if raw is None:
        raw = payload
    if not isinstance(raw, dict):
        raise ApiError("'extraction' must be the object returned by /extract-image")
    out = {
        "positive": str(raw.get("positive") or ""),
        "negative": str(raw.get("negative") or ""),
        "params": raw.get("params") if isinstance(raw.get("params"), dict) else {},
        "loras": [e for e in (raw.get("loras") or []) if isinstance(e, dict)],
        "source": str(raw.get("source") or ""),
    }
    if not out["positive"].strip():
        raise ApiError(
            "the extraction carries no positive prompt — pick a candidate first "
            "(an ambiguous ComfyUI graph offers several) or paste the text by hand"
        )
    return out


# -- handlers ------------------------------------------------------------------


@_guarded
def handle_extract_image(lib, payload):
    """POST {image: "<data: URI | base64>"} or {url: "https://civitai.com/images/<id>"}
    (+ optional resolve: true) -> {source, dialect, container, positive,
    negative, params, loras, resources?, candidates?, ambiguous?, notes}.

    Extraction ONLY. Which of the two paths runs next is a second call, so
    this endpoint never decides for the user."""
    try:
        resolve = payload.get("resolve") is True
        url = payload.get("url")
        if isinstance(url, str) and url.strip():
            item = fetch_civitai_image(lib, civitai_image_id(url))
            result = _finish(extraction_from_civitai_item(item), lib=lib, resolve=resolve)
        else:
            fmt, fields = read_image_metadata(decode_image_payload(payload.get("image")))
            result = extraction_from_fields(fields, fmt=fmt, lib=lib, resolve=resolve)
    except IntakeError as exc:
        return exc.status, exc.body()
    result.setdefault("notes", [])
    result.setdefault("params", {})
    result.setdefault("loras", [])
    return 200, result


@_guarded
def handle_extract_apply(lib, payload):
    """POST {path: "verbatim"|"decompose", extraction: {...}, ...} — the
    SECOND call, where the user's choice lands. Extraction is identical for
    both paths; only what happens to it differs.

    path='verbatim' (A): {template, section?, positive, negative, verbatim,
    notes} — and with save=true + slug=<slug> both files are written to the
    user tier. No LLM is consulted anywhere on this path.
    path='decompose' (B): handle_decompose's own report, unchanged."""
    raw_path = str(payload.get("path") or "verbatim").strip().lower()
    path = {"a": "verbatim", "as-is": "verbatim", "use-as-is": "verbatim", "b": "decompose"}.get(
        raw_path, raw_path
    )
    if path not in ("verbatim", "decompose"):
        raise ApiError(
            f"unknown path '{raw_path}' — 'verbatim' reproduces the found prompt exactly, "
            "'decompose' runs the de-composer on it"
        )
    extraction = _extraction_arg(payload)
    if path == "decompose":
        # allowlist, not a blocklist: the caller may hand back the whole
        # extraction payload (base64 image included), and only the keys
        # handle_decompose actually reads have any business travelling on
        forward = {
            key: payload[key]
            for key in ("type", "engine", "backend", "model", "timeout")
            if key in payload
        }
        forward["prompt"] = extraction["positive"]
        # module object, not a bound name: the LLM seam stays patchable
        return decompose_api.handle_decompose(lib, forward)

    slug = payload.get("slug")
    slug = pl.validate_slug(slug.strip()) if isinstance(slug, str) and slug.strip() else None
    label = str(payload.get("label") or "").strip()
    template = build_verbatim_template(
        extraction, label=label, source=extraction.get("source") or ""
    )
    section = build_lora_section(extraction, label=f"{template['label']} — LoRAs")
    section_slug = f"{slug}-loras" if (slug and section) else None
    notes = []
    # Prove the promise instead of asserting it: compose the template we just
    # built and compare. Pure library work — no backend, no network, no LLM.
    parsed = pl.parse_template(template, slug or "(image intake)", "image intake")
    composed = pl.compose(
        lib,
        parsed,
        seed=0,
        mode="as configured",
        selection={},
        variables={},
    )
    rendered = composed.rendered
    verbatim = (
        rendered.positive == extraction["positive"] and rendered.negative == extraction["negative"]
    )
    if not verbatim:
        notes.append(
            "the rendered prompt does not match the extracted one byte for byte — "
            "report this: path A must never rewrite a prompt"
        )
    if section:
        notes.append(
            f"{len(section['items'])} LoRA(s) were recorded as items in "
            f"'{section_slug or '(unsaved)'}'. Path A does no slotting, so they are not "
            "drawn yet: add a slot for them in the Composer when you want the loras "
            "output to carry them."
        )
    body = {
        "path": "verbatim",
        "slug": slug,
        "section_slug": section_slug,
        "template": template,
        "section": section,
        "positive": rendered.positive,
        "negative": rendered.negative,
        "verbatim": verbatim,
        "saved": False,
        "notes": notes,
    }
    if payload.get("save") is True:
        if slug is None:
            raise ApiError("'slug' is required to save the template (e.g. 'intake/my-car-shot')")
        if section is not None:
            lib.save_user("sections", section_slug, section)
        lib.save_user("templates", slug, template)
        body["saved"] = True
        body["fingerprint"] = lib.fingerprint()
    return 200, body
