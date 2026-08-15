"""A thumbnail for a history row, matched to its image WITHOUT any wiring.

The problem: a history line is written while the prompt is being composed —
before the sampler has run, so nothing about the eventual image is knowable at
that moment. Making the user wire a UUID into SaveImage would solve it and was
rejected for the right reason: this should be automatic.

It can be, because ComfyUI already writes the whole executed graph into every
PNG it saves. That graph contains the MRLN Prompt Template node with its
widget values, and two of them — `template` and `seed` — are exactly what the
history line records. So the image identifies ITSELF:

    output/…/ComfyUI_00004_.png   PNG chunk 'prompt' ->
        {"85": {"class_type": "MRLN_PromptTemplate",
                "inputs": {"template": "animal/documentary",
                           "seed": 730198984095416, …}}}

    history line -> {"template": "animal/documentary", "seed": 730198984095416}

Same pair, no wiring, and it works on images ALREADY on disk — a UUID minted
today could never have matched yesterday's renders.

Two honest limits:

  * A batch that varies its seed per item records one line per item, while the
    saved graph carries only the node's base seed. The first item matches; the
    rest do not, and get no thumbnail rather than a wrong one.
  * A graph that feeds `seed` from another node (a primitive, a link) has no
    literal to match on and is skipped.

Cost control, because the user's case is thousands of records: the index is
built INCREMENTALLY and persisted, new renders are picked up first and older
files are backfilled a bounded number per call, metadata reads never decode
pixels, and the encoded webp is cached on disk. Nothing here ever raises — a
missing thumbnail is a cosmetic absence, never a broken History tab.
"""

import contextlib
import hashlib
import json
import logging
import time
import zlib

from .core import ApiError, _guarded
from .thumbs import BinaryBody, _write_bytes_atomic, encode_thumb

_log = logging.getLogger(__name__)

# Small on purpose. The row shows it at ~28 px; 64 keeps it crisp on a hidpi
# screen and still lands around 1-2 KB per file.
THUMB_MAX_SIDE = 64
THUMB_QUALITY = 70

IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")

# Per request. A first run against a full output folder would otherwise walk
# thousands of files inside one HTTP call; instead each call advances the
# backfill and the tab fills in as the user scrolls.
SCAN_BUDGET = 240

_INDEX_NAME = "thumb-index.json"
_INDEX_VERSION = 1
_CACHE_DIR = "history"

# Two concurrent row requests can both walk the folder and both write the
# index. That race is left alone deliberately: entries only ever accumulate,
# the write is atomic, and the loser costs one duplicated scan — a lock here
# would serialise every row behind the slowest one for no correctness gain.

# The index, in the process, keyed by user root. A History page asks for ~25
# tiles at once; without this each one re-read the index file and walked the
# whole output folder, and the tiles trickled in over many seconds instead of
# appearing together.
_INDEX_MEMO: dict = {}
# When each root was last walked (monotonic seconds). A MISS is not a reason
# to walk again immediately: with 25 unmatched rows that is 25 full walks. One
# walk covers them all, and anything still missing is missing because it is
# not there yet — a render whose image the sampler has not written.
_LAST_SCAN: dict = {}
_SCAN_COOLDOWN = 8.0


def _output_root():
    """ComfyUI's output directory, or None outside ComfyUI (pytest, headless).

    None disables the whole feature quietly, which is the correct behaviour
    for a cosmetic extra."""
    try:
        import folder_paths

        root = folder_paths.get_output_directory()
    except Exception:
        return None
    try:
        from pathlib import Path

        path = Path(root)
        return path if path.is_dir() else None
    except Exception:
        return None


def _index_path(lib):
    root = getattr(lib, "user_root", None)
    if not root:
        return None
    from pathlib import Path

    return Path(root) / "history" / _INDEX_NAME


def _blank_index():
    return {"version": _INDEX_VERSION, "entries": {}, "newest_ns": 0, "oldest_ns": 0}


def _read_index_file(lib):
    path = _index_path(lib)
    if path is None or not path.is_file():
        return _blank_index()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _blank_index()
    if not isinstance(data, dict) or data.get("version") != _INDEX_VERSION:
        return _blank_index()
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return _blank_index()
    return {
        "version": _INDEX_VERSION,
        "entries": {str(k): str(v) for k, v in entries.items()},
        "newest_ns": int(data.get("newest_ns") or 0),
        "oldest_ns": int(data.get("oldest_ns") or 0),
    }


def _load_index(lib):
    """The index, from memory when we already have it.

    Held in the process, not re-read per call: a History page asks for ~25
    tiles at once and each one used to re-read this file AND walk the whole
    output folder. That is the difference between the tiles appearing at once
    and trickling in one by one, which is exactly how it behaved."""
    key = str(getattr(lib, "user_root", "") or "")
    hit = _INDEX_MEMO.get(key)
    if hit is not None:
        return hit
    index = _read_index_file(lib)
    _INDEX_MEMO[key] = index
    return index


def _save_index(lib, index):
    _INDEX_MEMO[str(getattr(lib, "user_root", "") or "")] = index
    path = _index_path(lib)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomic(path, json.dumps(index).encode("utf-8"))
    except Exception as exc:  # an unwritable index costs a rescan, nothing more
        _log.debug("MRLN prompt: history thumb index not saved (%s)", exc)


def forget_index_memo():
    """Drop the in-process copy. For tests, and for anything that edits the
    index behind this module's back."""
    _INDEX_MEMO.clear()
    _LAST_SCAN.clear()


def record_key(template, seed):
    """The match key both sides agree on. Seed is normalised through int so a
    string '42' from a JSON payload and 42 from a graph are the same render."""
    try:
        seed_text = str(int(seed))
    except (TypeError, ValueError):
        return ""
    template_text = str(template or "").strip()
    if not template_text:
        return ""
    return f"{template_text}|{seed_text}"


def _key_from_graph(graph):
    """(template, seed) out of an embedded ComfyUI graph, or ''.

    Only a LITERAL seed counts: an input fed by a link arrives as
    ['node_id', slot] and names no seed at all."""
    if not isinstance(graph, dict):
        return ""
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "MRLN_PromptTemplate":
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        template, seed = inputs.get("template"), inputs.get("seed")
        if not isinstance(template, str) or not isinstance(seed, (int, float)):
            continue
        if isinstance(seed, bool):  # bool is an int; a seed is not a flag
            continue
        key = record_key(template, seed)
        if key:
            return key
    return ""


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The header slice a PNG's text chunks live in. ComfyUI writes them before the
# pixel data, so this is generous: the real file that proved this code out
# carries 'prompt' at byte 41 and reaches IDAT by 33 KB.
_HEAD_BYTES = 512 * 1024


def _png_text_chunks(head):
    """{keyword: text} from a PNG's tEXt/zTXt/iTXt chunks, or None if this is
    not a PNG.

    Read straight out of the byte stream rather than through Pillow, because
    Pillow refuses a TRUNCATED file outright ('image file is truncated') even
    when the chunk being asked for sits complete in the first few hundred
    bytes. That is not hypothetical: it made every real 1.4 MB render come
    back with no key while the unit tests — whose PNGs are a few hundred bytes
    and therefore never truncated — all passed.

    Walking the chunks costs no decode at all, so this is also the cheaper
    path for the thousands-of-files case."""
    if not head.startswith(_PNG_MAGIC):
        return None
    out, pos, size = {}, len(_PNG_MAGIC), len(head)
    while pos + 8 <= size:
        length = int.from_bytes(head[pos : pos + 4], "big")
        ctype = head[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if ctype in (b"IDAT", b"IEND"):
            break  # text rides ahead of the pixels; nothing to find past here
        if length < 0 or end > size:
            break  # the chunk runs past what we read, or the length is junk
        body = head[start:end]
        try:
            if ctype == b"tEXt":
                key, _, value = body.partition(b"\x00")
                out.setdefault(key.decode("latin-1"), value.decode("latin-1"))
            elif ctype == b"zTXt":
                key, _, rest = body.partition(b"\x00")
                if rest[:1] == b"\x00":
                    text = zlib.decompress(rest[1:]).decode("utf-8", "replace")
                    out.setdefault(key.decode("latin-1"), text)
            elif ctype == b"iTXt":
                key, _, rest = body.partition(b"\x00")
                compressed = rest[:1] == b"\x01"
                rest = rest[2:]  # compression flag + method
                _lang, _, rest = rest.partition(b"\x00")
                _translated, _, text = rest.partition(b"\x00")
                if compressed:
                    text = zlib.decompress(text)
                out.setdefault(key.decode("latin-1"), text.decode("utf-8", "replace"))
        except Exception:
            pass  # one unreadable chunk must not lose the others
        pos = end + 4  # + CRC
    return out


def _key_of_file(path):
    """The match key an image carries, or '' when it carries none."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return ""
    fields = _png_text_chunks(head)
    if fields is None:
        # not a PNG (webp/jpeg): these carry the block in EXIF and are the
        # rarer output format, so pay Pillow's full read for them
        try:
            from .intake import read_image_metadata

            with open(path, "rb") as fh:
                _fmt, fields = read_image_metadata(fh.read())
        except Exception:
            return ""
    raw = fields.get("prompt")
    if not isinstance(raw, str):
        return ""
    try:
        return _key_from_graph(json.loads(raw))
    except Exception:
        return ""


def _candidates(root, index, budget):
    """Files worth reading this call: everything NEWER than what we have (a
    render that just finished is what the user is looking at), then a bounded
    backfill of older ones so a big output folder fills in over several calls
    instead of blocking one."""
    newest, oldest = index["newest_ns"], index["oldest_ns"]
    fresh, older = [], []
    try:
        for path in root.rglob("*"):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if mtime > newest:
                fresh.append((mtime, path))
            elif oldest and mtime < oldest:
                older.append((mtime, path))
    except Exception as exc:
        _log.debug("MRLN prompt: output scan stopped (%s)", exc)
    fresh.sort(reverse=True)
    older.sort(reverse=True)
    picked = fresh[:budget]
    if len(picked) < budget:
        picked += older[: budget - len(picked)]
    return picked


def refresh_index(lib, *, budget=SCAN_BUDGET, force=False):
    """Index a bounded slice of the output folder. Returns the index.

    Rate-limited per root: a page of misses must cost ONE walk, not one per
    row. `force` is for the boot warm-up, which wants the first walk to happen
    whether or not anything has asked yet."""
    index = _load_index(lib)
    root = _output_root()
    if root is None:
        return index
    key = str(getattr(lib, "user_root", "") or "")
    now = time.monotonic()
    last = _LAST_SCAN.get(key)
    if not force and last is not None and now - last < _SCAN_COOLDOWN:
        return index
    _LAST_SCAN[key] = now
    picked = _candidates(root, index, budget)
    if not picked:
        return index
    entries = index["entries"]
    newest, oldest = index["newest_ns"], index["oldest_ns"]
    for mtime, path in picked:
        key = _key_of_file(path)
        if key:
            # newest wins: re-rendering the same template+seed should show the
            # image the user just made, not the first one they ever made
            entries.setdefault(key, str(path))
        newest = max(newest, mtime)
        oldest = mtime if not oldest else min(oldest, mtime)
    index["entries"], index["newest_ns"], index["oldest_ns"] = entries, newest, oldest
    _save_index(lib, index)
    return index


def _cache_path(lib, key):
    root = getattr(lib, "user_root", None)
    if not root:
        return None
    from pathlib import Path

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return Path(root) / "thumbs" / _CACHE_DIR / f"{digest}.webp"


def thumb_bytes(lib, template, seed):
    """The mini thumbnail for one history row, or None. Never raises."""
    key = record_key(template, seed)
    if not key:
        return None
    cached = _cache_path(lib, key)
    if cached is not None and cached.is_file():
        try:
            return cached.read_bytes()
        except OSError:
            pass
    index = _load_index(lib)
    source = index["entries"].get(key)
    if source is None:
        # not indexed yet: this is the call that goes looking
        index = refresh_index(lib)
        source = index["entries"].get(key)
    if source is None:
        return None
    try:
        from pathlib import Path

        path = Path(source)
        if not path.is_file():  # the user moved or deleted the render
            return None
        data, _size = encode_thumb(
            path.read_bytes(), max_side=THUMB_MAX_SIDE, quality=THUMB_QUALITY
        )
    except Exception as exc:
        _log.debug("MRLN prompt: history thumb not encoded (%s)", exc)
        return None
    if cached is not None:
        with contextlib.suppress(Exception):
            cached.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes_atomic(cached, data)
    return data


@_guarded
def handle_history_thumb(lib, payload):
    """GET /mrln/prompt/history-thumb?template=…&seed=…

    Answers webp bytes, or 404 when this render has no image on disk. 404 is a
    normal answer here, not an error: the row simply shows no thumbnail."""
    from .history import history_thumbs
    from .settings import _read_settings

    if not history_thumbs(_read_settings(lib)):
        return 404, {"error": "history thumbnails are turned off", "remediation": ""}
    template = str(payload.get("template") or "").strip()
    seed = payload.get("seed")
    if not template or seed is None:
        raise ApiError("both 'template' and 'seed' are required")
    data = thumb_bytes(lib, template, seed)
    if not data:
        return 404, {"error": "no image found for this render", "remediation": ""}
    return 200, BinaryBody(
        data,
        content_type="image/webp",
        # the render behind a history line never changes, so let the browser
        # keep it: a scroll back through a thousand rows must not refetch
        headers={"Cache-Control": "private, max-age=86400"},
    )


def forget_thumb(lib, template, seed):
    """Drop ONE cached tile — the row it belonged to has been deleted.

    The tile is keyed by template+seed, which a re-render of the same pair
    shares. Deleting it is still right: it costs one re-encode to the row that
    still wants it, and leaving it would keep a picture cached for a record
    the user asked to be rid of."""
    key = record_key(template, seed)
    if not key:
        return False
    path = _cache_path(lib, key)
    if path is None or not path.is_file():
        return False
    with contextlib.suppress(OSError):
        path.unlink()
        return True
    return False


def clear_thumb_cache(lib):
    """Drop every cached webp, the index, and the in-process copy of it.

    Called when the history is cleared: a tile must never outlive the record
    it belonged to. The index goes too — it is a memo of the output folder, so
    rebuilding it costs one walk and there is nothing left to look at until
    the next render anyway."""
    removed = 0
    index_file = _index_path(lib)
    if index_file is not None and index_file.is_file():
        with contextlib.suppress(OSError):
            index_file.unlink()
            removed += 1
    root = getattr(lib, "user_root", None)
    if root:
        from pathlib import Path

        folder = Path(root) / "thumbs" / _CACHE_DIR
        if folder.is_dir():
            for path in folder.glob("*.webp"):
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
    # the in-process copy would otherwise hand the deleted index straight back
    forget_index_memo()
    return removed


def index_stats(lib):
    """For the Settings tab and the tests: how much is known right now."""
    index = _load_index(lib)
    return {
        "indexed": len(index["entries"]),
        "output_dir": _output_root() is not None,
        "newest_ns": index["newest_ns"],
        "scanned_at": int(time.time()),
    }
