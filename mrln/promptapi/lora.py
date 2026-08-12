"""Everything LoRA: Civitai lookups by file hash, the AIR-urn download
path (background for the Composer, synchronous for the node), the
install-status scan, and the section-item healing that re-points a library
item at a freshly fetched file.
"""

import contextlib
import json
import re
import threading

from .. import promptlib as pl
from .core import ApiError, _guarded, _require_str
from .settings import _read_settings

# -- secret scrubbing ---------------------------------------------------------
# Hard invariant: an API key is NEVER echoed by any API. The download-status
# poll route is unauthenticated and returns `detail` verbatim, so every string
# that can reach a client — error bodies, status details, raised messages —
# goes through _scrub_secrets first. Unconditional: it holds whichever auth
# route the download actually took (header or query fallback).

_TOKEN_QUERY_RE = re.compile(r"(token=)[^&\s\"'<>]+", re.IGNORECASE)
# below this length a value is not a credential, and redacting it would
# mangle ordinary words out of otherwise useful error text
_SECRET_MIN_LEN = 8
# ONE list, only ever mutated in place (worker threads scrub against it while
# handlers append to it). Never serialized, never logged, bounded.
_KNOWN_SECRETS = []


def _remember_secret(value):
    """Record a credential so later messages can be scrubbed even where the
    value itself is out of scope (worker threads, other modules). Returns the
    value unchanged so call sites can wrap the settings read directly."""
    value = str(value or "")
    if len(value) >= _SECRET_MIN_LEN and value not in _KNOWN_SECRETS:
        _KNOWN_SECRETS.append(value)
        del _KNOWN_SECRETS[:-8]  # keep the list bounded
    return value


def _scrub_secrets(text, *secrets):
    """Redact credentials out of anything a client may see: `token=…` query
    values plus the literal key (percent-encoded form included), both the ones
    passed in and every key seen this session. Pure, never raises."""
    import urllib.parse

    out = _TOKEN_QUERY_RE.sub(r"\1***", str(text))
    literals = set()
    for secret in (*secrets, *_KNOWN_SECRETS):
        secret = str(secret or "")
        if len(secret) < _SECRET_MIN_LEN:
            continue
        literals.add(secret)
        literals.add(urllib.parse.quote(secret, safe=""))
    for secret in sorted(literals, key=len, reverse=True):
        out = out.replace(secret, "***")
    return out


_ECO_MAP = (
    ("flux", "flux1"),
    ("sdxl", "sdxl"),
    ("sd 3", "sd3"),
    ("sd 2", "sd2"),
    ("sd 1", "sd1"),
    ("pony", "pony"),
    ("illustrious", "illustrious"),
    ("noobai", "noobai"),
)


def _civitai_summary(resp):
    """Pure: pick trigger + AIR out of a Civitai model-version response."""
    words = [str(w).strip() for w in resp.get("trainedWords") or [] if str(w).strip()]
    air = resp.get("air")
    if not air and resp.get("modelId") and resp.get("id"):
        base = str(resp.get("baseModel") or "").lower()
        eco = next((eco for frag, eco in _ECO_MAP if frag in base), None)
        eco = eco or (base.split() or ["model"])[0] or "model"
        mtype = str((resp.get("model") or {}).get("type") or "LORA").lower()
        air = f"urn:air:{eco}:{mtype}:civitai:{resp['modelId']}@{resp['id']}"
    return {
        "trigger": words[0] if words else None,
        "trained_words": words,
        "air": air,
        "model_name": (resp.get("model") or {}).get("name"),
        "version_name": resp.get("name"),
    }


# -- trigger words: provenance vs truth ---------------------------------------
# A LoRA item's TEXT is its catchword — the words that actually render. The
# full list Civitai (or the safetensors metadata) handed us is PROVENANCE,
# stored as data.lora_info.trained_words, and the editor's mute/solo NEVER
# edits it. That split is what makes the state survive a reload with no new
# schema field and nothing widget-only to lose:
#
#   MUTE = the word is in trained_words but absent from the catchword
#   SOLO = mute every other word — it collapses to the same persisted state
#
# so re-opening the editor re-derives the chips by set difference. The
# catchword is always joined in trained_words order, so a selection renders
# deterministically; all-muted is legal (a baked-in or unwanted trigger) and
# renders nothing; and words typed by hand that have no provenance entry are
# kept as user-added. Back-compat is the default selection: the FIRST trained
# word, which is exactly what the Civitai lookup has always written.

CATCHWORD_JOINER = ", "


def _clean_words(values):
    return [str(word).strip() for word in values or [] if str(word).strip()]


def split_catchword(catchword):
    """A catchword string -> the words it renders, in written order."""
    return [word.strip() for word in str(catchword or "").split(",") if word.strip()]


def default_trigger_selection(trained_words):
    """The back-compat selection: the first trained word and nothing else, so
    an item nobody touched keeps rendering byte-identically."""
    return _clean_words(trained_words)[:1]


def render_catchword(trained_words, selected):
    """The catchword TEXT for a selection: the provenance words in
    trained_words order first (determinism), then any free-text extras in the
    order given. Empty when everything is muted."""
    words = _clean_words(trained_words)
    picked = _clean_words(selected)
    wanted = {word.casefold() for word in picked}
    known = {word.casefold() for word in words}
    out = [word for word in words if word.casefold() in wanted]
    seen = {word.casefold() for word in out}
    for word in picked:
        folded = word.casefold()
        if folded not in known and folded not in seen:
            out.append(word)
            seen.add(folded)
    return CATCHWORD_JOINER.join(out)


def trigger_selection(trained_words, catchword):
    """The editor's chip state, derived from the FILE alone: which provenance
    words render, which are muted (present in provenance, absent from the
    catchword), and which rendered words are user-added free text."""
    words = _clean_words(trained_words)
    rendered = split_catchword(catchword)
    rendered_fold = {word.casefold() for word in rendered}
    known_fold = {word.casefold() for word in words}
    return {
        "words": words,
        "active": [word for word in words if word.casefold() in rendered_fold],
        "muted": [word for word in words if word.casefold() not in rendered_fold],
        "extra": [word for word in rendered if word.casefold() not in known_fold],
        "catchword": str(catchword or "").strip(),
    }


def lora_info(summary, *, filename=""):
    """The provenance blob an item stores as `data.lora_info`: what the source
    told us, never what the editor selected. Empty when the source told us
    nothing, so nothing pointless is ever written to a user file."""
    summary = summary or {}
    info = {}
    words = _clean_words(summary.get("trained_words"))
    if words:
        info["trained_words"] = words
    for key in ("air", "model_name", "version_name"):
        value = summary.get(key)
        if value:
            info[key] = str(value)
    if filename:
        info["file"] = str(filename)
    return info


# ONE dict for the whole package (path -> ((mtime_ns, size), sha256)), only
# ever mutated in place: the download worker seeds it, request threads read it.
_HASH_CACHE = {}
# Hashing a LoRA means reading the whole file — seconds for a multi-GB one, on
# an executor thread. Without single-flight, N clicks on the same row = N full
# reads on N of the pool's threads; with it, the extras wait for the first.
# Per PATH, so two different files still hash in parallel. Bounded in practice
# by the number of distinct lora files ever looked up (one small Lock each).
_HASH_LOCKS = {}
_HASH_LOCKS_GUARD = threading.Lock()


def _hash_key(path):
    """Cache key for a file path — normalized so the same file reached under a
    different spelling (case, separators) shares one entry."""
    import os

    return os.path.normcase(os.path.abspath(str(path)))


def _hash_lock(key):
    with _HASH_LOCKS_GUARD:
        lock = _HASH_LOCKS.get(key)
        if lock is None:
            lock = _HASH_LOCKS[key] = threading.Lock()
        return lock


def _sha256_of(path):
    import hashlib
    import os

    stat = os.stat(path)
    key = (stat.st_mtime_ns, stat.st_size)
    cache_key = _hash_key(path)
    cached = _HASH_CACHE.get(cache_key)
    if cached and cached[0] == key:
        return cached[1]
    with _hash_lock(cache_key):
        cached = _HASH_CACHE.get(cache_key)  # a concurrent caller may have won
        if cached and cached[0] == key:
            return cached[1]
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        hexdigest = digest.hexdigest()
        _HASH_CACHE[cache_key] = (key, hexdigest)
    return hexdigest


def _resolve_lora_file(name):
    """(real_name, path) via folder_paths, tolerant of slash/case — or None
    outside ComfyUI / for unknown names."""
    try:
        import folder_paths
    except ImportError:
        return None
    available = folder_paths.get_filename_list("loras")

    def norm(n):
        return n.replace("\\", "/").lower()

    real = next((c for c in available if c == name), None) or next(
        (c for c in available if norm(c) == norm(name)), None
    )
    if real is None:
        return ("", None)
    return (real, folder_paths.get_full_path("loras", real))


@_guarded
def handle_lora_civitai(lib, payload):
    """Trigger word + AIR from Civitai, keyed by the file's SHA256. Works
    keyless for public models; the stored API key (user-tier settings.json,
    never echoed) unlocks restricted ones."""
    name = _require_str(payload, "name")
    resolved = _resolve_lora_file(name)
    if resolved is None:
        return 400, {
            "error": "Civitai lookup runs inside a running ComfyUI only",
            "remediation": "type the catchword manually",
        }
    real, path = resolved
    if path is None:
        return 404, {
            "error": f"LoRA '{name}' not found in your loras folder",
            "remediation": "refresh the list or pick another file",
        }
    import urllib.error
    import urllib.request

    digest = _sha256_of(path)
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    key = _remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"https://civitai.com/api/v1/model-versions/by-hash/{digest}", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, {
                "error": f"'{real}' is not on Civitai (hash {digest[:12]}…)",
                "remediation": "type the catchword manually",
            }
        return 502, {
            "error": f"Civitai answered HTTP {exc.code}",
            "remediation": "check the API key in the Composer's Library tab, or retry later",
        }
    except Exception as exc:  # URLError / timeout / bad JSON
        return 502, {
            "error": _scrub_secrets(f"Civitai unreachable: {exc}", key),
            "remediation": "check your network and retry",
        }
    out = _civitai_summary(data)
    out["name"] = real
    # The editor stores `lora_info` as provenance and `catchword` as truth; the
    # default selection is the first word, so an item created from this answer
    # renders exactly what `trigger` alone used to produce.
    out["lora_info"] = lora_info(out, filename=real)
    out["catchword"] = render_catchword(
        out["trained_words"], default_trigger_selection(out["trained_words"])
    )
    return 200, out


@_guarded
def handle_lora_meta(lib, payload):
    """Trigger word from an installed LoRA's own metadata. Names come from
    the /models/loras list; resolution goes through folder_paths only, so
    no request string touches the filesystem directly."""
    name = _require_str(payload, "name")
    resolved = _resolve_lora_file(name)
    if resolved is None:
        return 400, {
            "error": "LoRA metadata is only readable inside a running ComfyUI",
            "remediation": "type the catchword manually",
        }
    real, path = resolved
    if path is None:
        return 404, {
            "error": f"LoRA '{name}' not found in your loras folder",
            "remediation": "refresh the list or pick another file",
        }
    try:
        meta = pl.read_safetensors_metadata(path)
    except ValueError as exc:
        return 400, {
            "error": _scrub_secrets(str(exc)),
            "remediation": "type the catchword manually",
        }
    trigger, source = pl.trigger_from_metadata(meta)
    if not trigger:
        return 404, {
            "error": f"no trigger word in the metadata of '{real}'",
            "remediation": "type the catchword manually — trainers embed triggers as "
            "modelspec.trigger_phrase or kohya ss_tag_frequency",
        }
    # Deliberately NOT extended with trained_words/lora_info: safetensors
    # metadata yields exactly ONE trigger, so the provenance list would be
    # [trigger] and the catchword would equal it — the client derives both
    # from `trigger` alone, and this body's exact shape is pinned by a
    # protocol test. The Civitai lookup, which really does return several
    # words, is where the richer shape belongs.
    return 200, {"trigger": trigger, "source": source, "name": real}


# -- LoRA download by AIR (heal missing files) -------------------------------
# A section item stores the LoRA file name + its Civitai AIR urn in the
# comment. On a machine that lacks the file, the Composer offers to fetch
# it: background thread (multi-GB files), SHA256-verified, .safetensors
# only, then the section item is re-pointed if the chosen path differs.

_AIR_RE = re.compile(r"^urn:air:[a-z0-9._-]+:[a-z0-9._-]+:civitai:(\d+)@(\d+)$", re.IGNORECASE)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
# ONE dict for the whole package, only ever mutated in place: worker threads
# write progress into it while the (unauthenticated) poll route reads it.
_LORA_DL_STATUS = {}  # air urn -> {"status", "detail", "name", "loaded", "total"}
# handlers run concurrently on the executor (routes.py), so the check-then-claim
# in handle_lora_download has to be atomic — see the comment there
_DL_LOCK = threading.Lock()

# Both per-key status maps are written from unauthenticated routes, one entry
# per distinct AIR (or model name) ever asked for, and nothing ever removed
# them — an unbounded map keyed by attacker-supplied strings in a long-running
# server. Eviction only ever drops entries that have REACHED A TERMINAL STATE:
# an in-flight one is what a poller is waiting on, so losing it would strand a
# multi-GB download with no way to observe it finishing.
_STATUS_KEEP = 64


def _evict_finished_status(statuses, keep=_STATUS_KEEP):
    """Trim a status map in place to `keep` entries, oldest terminal first.
    Python dicts preserve insertion order, so the head is the oldest."""
    if len(statuses) <= keep:
        return
    for key in [k for k, v in statuses.items() if (v or {}).get("status") in ("done", "error")]:
        if len(statuses) <= keep:
            break
        statuses.pop(key, None)


def parse_air(air):
    """(model_id, version_id) from a Civitai AIR urn, or None."""
    match = _AIR_RE.match(str(air or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _sanitize_subfolder(folder):
    """Relative subfolder under the loras root — every segment checked, no
    escapes, backslashes normalized. Empty means the root itself."""
    folder = str(folder or "").strip().replace("\\", "/").strip("/")
    if not folder:
        return ""
    parts = [p.strip() for p in folder.split("/") if p.strip()]
    for part in parts:
        if not _SAFE_SEGMENT.match(part) or part in (".", ".."):
            raise ApiError(f"invalid folder segment '{part}'")
    return "/".join(parts)


def _sanitize_lora_filename(name):
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        return ""
    if not base.lower().endswith(".safetensors"):
        base += ".safetensors"
    if not _SAFE_SEGMENT.match(base):
        raise ApiError(f"invalid file name '{base}'")
    return base


def _heal_section_lora(lib, section_slug, item_name, new_lora, info=None):
    """Re-point a section item's data.lora at the downloaded file (user-tier
    write). Factory-origin items get a full self-contained snapshot — the
    tier merge replaces items by name wholesale, so a thin entry would wipe
    the item's texts.

    `info` is the provenance blob from the same Civitai response (see
    lora_info): stored as data.lora_info so the editor can offer every trained
    word as a mute/solo chip. The item's TEXT — its catchword, i.e. which of
    those words actually render — is never touched here; overwriting a
    catchword the user curated is exactly what the provenance/truth split
    exists to prevent."""
    section = lib.load_section(section_slug)
    target = next((i for i in section.items if i.name == item_name), None)
    if target is None:
        raise ApiError(f"item '{item_name}' not found in section '{section_slug}'")
    raw = {"items": []}
    user_file = (lib.user_root / "sections" / f"{section_slug}.json") if lib.user_root else None
    if user_file and user_file.is_file():
        raw = json.loads(user_file.read_text(encoding="utf-8"))
        raw.setdefault("items", [])
    entry = next(
        (i for i in raw["items"] if isinstance(i, dict) and i.get("name") == item_name), None
    )
    if entry is None:
        entry = pl.dump_item(target)
        raw["items"].append(entry)
    entry["data"] = {**(entry.get("data") or {}), "lora": new_lora}
    if info:
        entry["data"]["lora_info"] = {**(entry["data"].get("lora_info") or {}), **info}
    lib.save_user("sections", section_slug, raw)


def _open_download(url, token, timeout=120):
    """Open the Civitai download stream, authenticated WITHOUT putting the key
    in the URL: it rides an `Authorization: Bearer` header, so no url-bearing
    exception, log line or status detail can carry it.

    Fallback, deliberately kept: Civitai answers a download with a redirect to
    presigned storage, and urllib's redirect handler forwards request headers
    to the new host. A presigned URL already carries its signature in the
    query, so the extra Authorization header makes such backends answer 401/403
    ("only one auth mechanism allowed"). That is the breakage an earlier
    session hit and worked around with the query param. On exactly those two
    codes — and only then — we retry once with `token=` in the query. Nothing
    is written to disk before this call succeeds, so the retry is clean, and
    _scrub_secrets covers the query form unconditionally either way.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if not token or exc.code not in (401, 403):
            raise
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{sep}token={urllib.parse.quote(token)}",
        headers={"User-Agent": "ComfyUI-MRLN-Nodes"},
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _fetch_lora_file(meta_headers, token, version_id, dest_dir, filename, status):
    """Civitai version metadata -> stream the primary .safetensors file ->
    SHA256 verify -> move into place. Returns the final file name; RAISES on
    any failure (partial .part always removed). `status` is a plain progress
    sink so both the threaded and the synchronous caller can watch it."""
    import os
    import urllib.request

    part_path = None
    try:
        request = urllib.request.Request(
            f"https://civitai.com/api/v1/model-versions/{version_id}", headers=meta_headers
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        # the same response that names the file also names every trained word:
        # record it as provenance so the caller can store it on the item
        # without a second Civitai round trip (empty when it carries none)
        provenance = lora_info(_civitai_summary(meta))
        if provenance:
            status["lora_info"] = provenance
        files = [
            f
            for f in (meta.get("files") or [])
            if str(f.get("name") or "").lower().endswith(".safetensors")
        ]
        if not files:
            raise RuntimeError("this Civitai version ships no .safetensors file")
        chosen = next((f for f in files if f.get("primary")), files[0])
        filename = filename or _sanitize_lora_filename(chosen.get("name"))
        want_sha = str((chosen.get("hashes") or {}).get("SHA256") or "").lower()
        url = str(chosen.get("downloadUrl") or "")
        if not url:
            url = f"https://civitai.com/api/download/models/{version_id}"
        os.makedirs(dest_dir, exist_ok=True)
        final_path = os.path.join(dest_dir, filename)
        # per-start unique: should two fetches of one AIR ever overlap (the
        # Composer's background download and the node's synchronous one), they
        # write separate torsos and the last os.replace wins — interleaved
        # writes into a single '.part' would silently corrupt the file
        part_path = f"{final_path}.part-{os.getpid()}-{threading.get_ident()}"
        import hashlib

        digest = hashlib.sha256()
        loaded = 0
        with _open_download(url, token) as resp:
            status["total"] = int(resp.headers.get("Content-Length") or 0)
            with open(part_path, "wb") as fh:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    fh.write(chunk)
                    digest.update(chunk)
                    loaded += len(chunk)
                    status["loaded"] = loaded
        if want_sha and digest.hexdigest().lower() != want_sha:
            os.remove(part_path)
            raise RuntimeError(
                f"SHA256 mismatch after download (got {digest.hexdigest()[:12]}…, "
                f"Civitai says {want_sha[:12]}…) — file discarded"
            )
        if not want_sha:
            # Civitai shipped no SHA256 for this file, so nothing was verified.
            # Say so instead of implying an integrity check happened: the caller
            # surfaces this, and a user weighing a multi-GB weight file from a
            # third party deserves to know it arrived unchecked.
            status["unverified"] = True
        os.replace(part_path, final_path)
        # the stream was hashed for verification anyway: seed the cache so the
        # Composer's follow-up Civitai lookup never re-reads the whole file
        with contextlib.suppress(OSError):
            stat = os.stat(final_path)
            _HASH_CACHE[_hash_key(final_path)] = (
                (stat.st_mtime_ns, stat.st_size),
                digest.hexdigest(),
            )
        status["name"] = filename
        return filename
    except Exception:
        # never leave a multi-GB torso in the loras folder: os.replace has
        # not run (or already consumed the file), so drop the partial
        if part_path and os.path.exists(part_path):
            with contextlib.suppress(OSError):
                os.remove(part_path)
        raise


def _lora_download_worker(status_key, meta_headers, token, version_id, dest_dir, filename, heal):
    """Background thread wrapper: fetch, then optionally heal the section item
    at the new path. Writes progress into _LORA_DL_STATUS; never raises."""
    status = _LORA_DL_STATUS[status_key]
    try:
        filename = _fetch_lora_file(meta_headers, token, version_id, dest_dir, filename, status)
        if heal:
            lib, section_slug, item_name, folder, stored = heal
            new_name = f"{folder}/{filename}" if folder else filename
            stored = str(stored or "").replace("\\", "/")
            moved = stored.lower() != new_name.lower()
            info = status.get("lora_info") or None
            # a user-tier snapshot is written when the path moved OR when there
            # is provenance worth keeping; without either there is nothing to say
            if moved or info:
                _heal_section_lora(lib, section_slug, item_name, new_name, info)
            if moved:
                status["healed"] = new_name
        status["status"] = "done"
        detail = f"saved as {filename}"
        if status.get("unverified"):
            detail += " (Civitai shipped no SHA256 — integrity unverified)"
        status["detail"] = _scrub_secrets(detail, token)
    except Exception as exc:
        # an UNAUTHENTICATED poll returns this string verbatim: scrub first
        status["status"] = "error"
        status["detail"] = _scrub_secrets(str(exc), token)
    finally:
        _evict_finished_status(_LORA_DL_STATUS)


@_guarded
def handle_lora_download(lib, payload):
    """POST {air, start:true, folder?, filename?, section?, item?, stored?}
    begins a background download of the AIR-referenced Civitai file into
    the loras folder; GET {air} polls progress. When section+item are given
    and the final path differs from `stored`, the section item is healed to
    point at the downloaded file. Only JSON `true` counts as start — GET
    query values are strings, so a polling (or cross-site) GET can never
    write into the loras folder."""
    air = _require_str(payload, "air")
    parsed = parse_air(air)
    if parsed is None:
        raise ApiError(
            f"'{air}' is not a Civitai AIR urn (urn:air:<eco>:<type>:civitai:<model>@<version>)"
        )
    if payload.get("start") is not True:
        status = _LORA_DL_STATUS.get(air) or {"status": "unknown", "detail": "no download started"}
        return 200, {"air": air, **status}
    current = _LORA_DL_STATUS.get(air)
    if current and current.get("status") == "downloading":
        return 200, {"air": air, **current}  # cheap early out; the claim below is the real one
    try:
        import folder_paths
    except ImportError:
        return 400, {
            "error": "LoRA downloads run inside a running ComfyUI only",
            "remediation": "download the file manually and place it in your loras folder",
        }
    roots = folder_paths.get_folder_paths("loras")
    if not roots:
        return 400, {
            "error": "no loras folder registered in this ComfyUI",
            "remediation": "add a loras path to extra_model_paths.yaml (or create "
            "models/loras) and restart ComfyUI",
        }
    folder = _sanitize_subfolder(payload.get("folder"))
    filename = _sanitize_lora_filename(payload.get("filename"))
    import os

    dest_dir = os.path.join(roots[0], *folder.split("/")) if folder else roots[0]
    _, version_id = parsed
    token = _remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))
    meta_headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        meta_headers["Authorization"] = f"Bearer {token}"
    heal = None
    section = str(payload.get("section") or "").strip()
    item = str(payload.get("item") or "").strip()
    if section and item:
        heal = (lib, section, item, folder, str(payload.get("stored") or ""))
    # Claim the AIR atomically: the check at the top of this handler and the
    # write below are seconds apart (folder_paths import, settings read), and
    # handlers run concurrently on the executor — a double-click would
    # otherwise start two workers streaming into one file. Re-check inside.
    with _DL_LOCK:
        current = _LORA_DL_STATUS.get(air)
        if current and current.get("status") == "downloading":
            return 200, {"air": air, **current}
        _LORA_DL_STATUS[air] = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    threading.Thread(
        target=_lora_download_worker,
        args=(air, meta_headers, token, version_id, dest_dir, filename, heal),
        daemon=True,
    ).start()
    return 200, {"air": air, "status": "downloading"}


def _lora_items(lib, template=None):
    """Every LoRA-carrying library item as (section_slug, item), scoped to one
    template's reachable pools when `template` is given."""
    if template is None:
        slugs = lib.section_slugs()
    else:
        tpl = lib.load_template(template)
        refs = [s.ref for s in tpl.slots]
        refs.extend(s.ref for v in tpl.variants for s in v.slots)
        sections, _missing = pl.section_closure(lib, refs)
        slugs = sorted(sections)
    out = []
    for slug in slugs:
        try:
            section = lib.load_section(slug)
        except pl.PromptLibError:
            continue
        for item in section.items:
            if item.hidden:
                continue
            if (item.data or {}).get("lora"):
                out.append((slug, item))
    return out


def lora_status(lib, template=None):
    """Which LoRA files the library (or one template) needs, and which of
    them are actually installed. Pure apart from the folder_paths lookup, so
    the startup scan, the endpoint and the node all share one answer."""
    installed = None
    try:
        import folder_paths

        installed = {
            n.replace("\\", "/").lower(): n for n in folder_paths.get_filename_list("loras")
        }
    except Exception:  # outside ComfyUI there is nothing to check against
        pass
    rows, missing = [], 0
    for slug, item in _lora_items(lib, template):
        data = item.data or {}
        name = str(data.get("lora") or "")
        comment = str(data.get("comment") or "").strip()
        air = comment if comment.lower().startswith("urn:air:") else ""
        present = True if installed is None else name.replace("\\", "/").lower() in installed
        if not present:
            missing += 1
        rows.append(
            {
                "file": name,
                "air": air,
                "section": slug,
                "item": item.name,
                "present": present,
            }
        )
    return {
        "loras": rows,
        "total": len(rows),
        "missing": missing,
        "can_download": installed is not None,
    }


@_guarded
def handle_lora_status(lib, payload):
    """GET: which LoRA files this library — or one template — needs and which
    are missing on this machine. Feeds the Composer's pre-render warning so a
    missing file surfaces before the graph dies in LoRA Apply."""
    template = payload.get("template")
    template = template.strip() if isinstance(template, str) and template.strip() else None
    body = lora_status(lib, template)
    if template:
        body["template"] = template
    return 200, body


def download_lora_by_air(lib, air, *, folder="", filename="", section="", item="", stored=""):
    """SYNCHRONOUS download-by-AIR for the node path — the Composer is not
    involved, so this blocks until the file is verified and in place.
    Returns the loras-root-relative name; raises RuntimeError on failure."""
    parsed = parse_air(air)
    if parsed is None:
        raise RuntimeError(f"'{air}' is not a Civitai AIR urn")
    try:
        import folder_paths
    except ImportError as exc:
        raise RuntimeError("LoRA downloads run inside a running ComfyUI only") from exc
    roots = folder_paths.get_folder_paths("loras")
    if not roots:
        raise RuntimeError("no loras folder registered in this ComfyUI")
    import os

    folder = _sanitize_subfolder(folder)
    dest_dir = os.path.join(roots[0], *folder.split("/")) if folder else roots[0]
    _, version_id = parsed
    token = _remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    _LORA_DL_STATUS[air] = status  # the Composer can watch a node-side fetch
    try:
        name = _fetch_lora_file(
            headers, token, version_id, dest_dir, _sanitize_lora_filename(filename), status
        )
    except Exception as exc:
        # the Composer polls this shared status: leave it resolved (not stuck
        # on "downloading") and scrubbed, and re-raise a message the node can
        # show without `from exc` dragging an unscrubbed cause into the log
        detail = _scrub_secrets(f"{type(exc).__name__}: {exc}", token)
        status["status"] = "error"
        status["detail"] = detail
        _evict_finished_status(_LORA_DL_STATUS)
        raise RuntimeError(detail) from None
    status["status"] = "done"
    detail = f"saved as {name}"
    if status.get("unverified"):
        detail += " (Civitai shipped no SHA256 — integrity unverified)"
    status["detail"] = _scrub_secrets(detail, token)
    _evict_finished_status(_LORA_DL_STATUS)
    final = f"{folder}/{name}" if folder else name
    moved = str(stored or "").replace("\\", "/").lower() != final.lower()
    if section and item and (moved or status.get("lora_info")):
        with contextlib.suppress(Exception):  # the file is there; healing is a bonus
            _heal_section_lora(lib, section, item, final, status.get("lora_info") or None)
            if moved:
                status["healed"] = final
    with contextlib.suppress(Exception):
        folder_paths.get_filename_list.cache_clear()  # make the new file visible now
    return final
