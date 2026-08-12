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


_HASH_CACHE = {}


def _sha256_of(path):
    import hashlib
    import os

    stat = os.stat(path)
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _HASH_CACHE.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    hexdigest = digest.hexdigest()
    _HASH_CACHE[str(path)] = (key, hexdigest)
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
    key = str(_read_settings(lib).get("civitai_api_key") or "")
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
            "error": f"Civitai unreachable: {exc}",
            "remediation": "check your network and retry",
        }
    out = _civitai_summary(data)
    out["name"] = real
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
        return 400, {"error": str(exc), "remediation": "type the catchword manually"}
    trigger, source = pl.trigger_from_metadata(meta)
    if not trigger:
        return 404, {
            "error": f"no trigger word in the metadata of '{real}'",
            "remediation": "type the catchword manually — trainers embed triggers as "
            "modelspec.trigger_phrase or kohya ss_tag_frequency",
        }
    return 200, {"trigger": trigger, "source": source, "name": real}


# -- LoRA download by AIR (heal missing files) -------------------------------
# A section item stores the LoRA file name + its Civitai AIR urn in the
# comment. On a machine that lacks the file, the Composer offers to fetch
# it: background thread (multi-GB files), SHA256-verified, .safetensors
# only, then the section item is re-pointed if the chosen path differs.

_AIR_RE = re.compile(r"^urn:air:[a-z0-9._-]+:[a-z0-9._-]+:civitai:(\d+)@(\d+)$", re.IGNORECASE)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
_LORA_DL_STATUS = {}  # air urn -> {"status", "detail", "name", "loaded", "total"}


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


def _heal_section_lora(lib, section_slug, item_name, new_lora):
    """Re-point a section item's data.lora at the downloaded file (user-tier
    write). Factory-origin items get a full self-contained snapshot — the
    tier merge replaces items by name wholesale, so a thin entry would wipe
    the item's texts."""
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
    lib.save_user("sections", section_slug, raw)


def _fetch_lora_file(meta_headers, token, version_id, dest_dir, filename, status):
    """Civitai version metadata -> stream the primary .safetensors file ->
    SHA256 verify -> move into place. Returns the final file name; RAISES on
    any failure (partial .part always removed). `status` is a plain progress
    sink so both the threaded and the synchronous caller can watch it."""
    import os
    import urllib.parse
    import urllib.request

    part_path = None
    try:
        request = urllib.request.Request(
            f"https://civitai.com/api/v1/model-versions/{version_id}", headers=meta_headers
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
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
        if token:
            # token rides the QUERY, not a header: the download redirects to
            # presigned storage where an Authorization header breaks the
            # signature
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={urllib.parse.quote(token)}"
        os.makedirs(dest_dir, exist_ok=True)
        final_path = os.path.join(dest_dir, filename)
        part_path = final_path + ".part"
        import hashlib

        digest = hashlib.sha256()
        loaded = 0
        request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-MRLN-Nodes"})
        with urllib.request.urlopen(request, timeout=120) as resp:
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
        os.replace(part_path, final_path)
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
            if stored.lower() != new_name.lower():
                _heal_section_lora(lib, section_slug, item_name, new_name)
                status["healed"] = new_name
        status["status"] = "done"
        status["detail"] = f"saved as {filename}"
    except Exception as exc:
        status["status"] = "error"
        status["detail"] = str(exc)


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
        return 200, {"air": air, **current}
    try:
        import folder_paths
    except ImportError:
        return 400, {
            "error": "LoRA downloads run inside a running ComfyUI only",
            "remediation": "download the file manually and place it in your loras folder",
        }
    roots = folder_paths.get_folder_paths("loras")
    if not roots:
        return 400, {"error": "no loras folder registered in this ComfyUI"}
    folder = _sanitize_subfolder(payload.get("folder"))
    filename = _sanitize_lora_filename(payload.get("filename"))
    import os

    dest_dir = os.path.join(roots[0], *folder.split("/")) if folder else roots[0]
    _, version_id = parsed
    token = str(_read_settings(lib).get("civitai_api_key") or "")
    meta_headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        meta_headers["Authorization"] = f"Bearer {token}"
    heal = None
    section = str(payload.get("section") or "").strip()
    item = str(payload.get("item") or "").strip()
    if section and item:
        heal = (lib, section, item, folder, str(payload.get("stored") or ""))
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
    token = str(_read_settings(lib).get("civitai_api_key") or "")
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    _LORA_DL_STATUS[air] = status  # the Composer can watch a node-side fetch
    name = _fetch_lora_file(
        headers, token, version_id, dest_dir, _sanitize_lora_filename(filename), status
    )
    status["status"] = "done"
    status["detail"] = f"saved as {name}"
    final = f"{folder}/{name}" if folder else name
    if section and item and str(stored or "").replace("\\", "/").lower() != final.lower():
        with contextlib.suppress(Exception):  # the file is there; healing is a bonus
            _heal_section_lora(lib, section, item, final)
            status["healed"] = final
    with contextlib.suppress(Exception):
        folder_paths.get_filename_list.cache_clear()  # make the new file visible now
    return final
