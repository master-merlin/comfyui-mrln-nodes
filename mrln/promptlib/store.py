"""Every on-disk shape that is NOT the section/template/profile catalog:
thumbnails and the render history. `library.py` owns the catalog; this module
owns the rest, so path knowledge never scatters across handlers and nodes.

Decision D1: JSON files stay the single source of truth (268 files / 4.5 MB,
cold full parse 58 ms, warm 5.5 ms — a database buys nothing at this size).
This module is the seam where a rebuildable derived index (sqlite) could later
be slotted in without a single caller changing.

Layouts, both tiers:

    <tier>/thumbs/<kind>/<slug>.webp     kind in ("sections", "templates")
    <user>/history/render-YYYYMM.jsonl   append-only, one JSON object per line

The thumbnail split is load-bearing for decision D3. Factory thumbs live under
`mrln/data/prompt/thumbs/` and are written ONLY by the repo / the offline
`_harness` render tool; every API write goes to the user tier. A user thumb
SHADOWS the factory one, and deleting it makes the factory thumb reappear —
exactly how same-slug sections behave. Because the two tiers are different
directories, "a repo update never overwrites a user's thumbnails" is true BY
CONSTRUCTION, not by convention: no code path exists that writes the factory
tier at runtime. Keep it that way.

History is user-tier only: it is a record of what THIS install rendered, and
nothing in the repo may ship or overwrite it.
"""

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from .errors import SchemaError
from .library import validate_slug

THUMB_KINDS = ("sections", "templates")
THUMB_EXT = ".webp"

_THUMB_DIR = "thumbs"
_HISTORY_DIR = "history"
_HISTORY_NAME_RE = re.compile(r"^render-\d{6}\.jsonl$")
_ISO_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})")
# Key in Library._scan_cache, so lib.invalidate() drops the thumb memo with
# the rest of the disk snapshot. '@' prefix keeps it out of the kind keyspace
# (same trick Library.pack_profiles uses for '@profiles').
_THUMB_CACHE_KEY = "@thumbs"

# One append at a time per process. A JSONL line must land whole: two threads
# writing a render each (batch nodes, the API) must not interleave partial
# lines into the same file.
_APPEND_LOCK = threading.Lock()

_log = logging.getLogger(__name__)


# -- thumbnails --------------------------------------------------------------


def _validate_kind(kind):
    if kind not in THUMB_KINDS:
        raise SchemaError(str(kind), f"unknown thumbnail kind (kinds: {', '.join(THUMB_KINDS)})")
    return kind


def _tier_thumb(root, kind, slug):
    """`<root>/thumbs/<kind>/<slug>.webp`, or None when `root` is unset.

    Slugs come from request strings, so this is a filesystem gate: validate_slug
    rejects '..', backslashes, absolute paths and empty segments, and the
    containment check afterwards is the same defense in depth Library.save_user
    applies — a future caller that forgets to validate still cannot escape the
    tier."""
    _validate_kind(kind)
    validate_slug(slug)
    if not root:
        return None
    kind_dir = (Path(root) / _THUMB_DIR / kind).resolve()
    target = (kind_dir / f"{slug}{THUMB_EXT}").resolve()
    if kind_dir not in target.parents:
        raise SchemaError(slug, "slug escapes the thumbnail directory")
    return target


def thumb_path(lib, kind, slug):
    """The thumbnail to SERVE for `slug`, or None when neither tier has one.

    User tier shadows factory, exactly like sections do. Raises SchemaError for
    an unknown kind or an unsafe slug."""
    for root in (lib.user_root, lib.factory_root):  # user first == user wins
        path = _tier_thumb(root, kind, slug)
        if path is not None and path.is_file():
            return path
    return None


def user_thumb_target(lib, kind, slug):
    """Where a user thumbnail for `slug` is WRITTEN. Pure path computation —
    the caller creates `target.parent` (mkdir(parents=True, exist_ok=True)) and
    writes atomically. Never returns a factory path: the API only ever writes
    the user tier."""
    if not getattr(lib, "user_root", None):
        raise SchemaError(str(slug), "no user library directory is configured")
    return _tier_thumb(lib.user_root, kind, slug)


def has_thumb(lib, kind, slug):
    """Stat-based existence check for catalog payloads (one flag per entry, so
    it runs hundreds of times per listing). Memoized in the library's scan
    cache and therefore dropped by lib.invalidate() after any write. Returns
    False instead of raising — a bad slug must never break a whole listing."""
    try:
        cache = lib._scan_cache.setdefault(_THUMB_CACHE_KEY, {})
        key = (str(kind), str(slug))
        hit = cache.get(key)
        if hit is None:
            hit = cache[key] = thumb_path(lib, kind, slug) is not None
        return hit
    except Exception:
        return False


# -- render history ----------------------------------------------------------


def _history_dir(lib):
    root = getattr(lib, "user_root", None)
    return Path(root) / _HISTORY_DIR if root else None


def _month_of(ts):
    """'YYYYMM' from an ISO-8601 timestamp, else None."""
    match = _ISO_MONTH_RE.match(ts) if isinstance(ts, str) else None
    return f"{match.group(1)}{match.group(2)}" if match else None


def history_append(lib, record):
    """Append one record to `<user>/history/render-YYYYMM.jsonl`.

    NEVER raises: history is a convenience, and a failed history write must
    never break a render that already succeeded. Every failure (no user root,
    unwritable path, unserializable record) is logged and swallowed.

    A record without a usable 'ts' is stamped with the local time now; the
    month file follows the record's own timestamp, so a backfilled line lands
    in — and is pruned with — the month it belongs to. json.dumps escapes
    newlines, so one record is always exactly one line."""
    try:
        if not isinstance(record, dict):
            raise TypeError(f"history record must be a dict, got {type(record).__name__}")
        directory = _history_dir(lib)
        if directory is None:
            raise ValueError("no user library directory is configured")
        record = dict(record)
        month = _month_of(record.get("ts"))
        if month is None:
            record["ts"] = datetime.now().isoformat(timespec="seconds")
            month = _month_of(record["ts"])
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _APPEND_LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / f"render-{month}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        _log.warning("MRLN prompt: history append skipped (%s)", exc)


def history_files(lib):
    """Month files, newest first — 'render-YYYYMM' sorts chronologically."""
    directory = _history_dir(lib)
    if directory is None or not directory.is_dir():
        return []
    try:
        files = [p for p in directory.iterdir() if _HISTORY_NAME_RE.match(p.name) and p.is_file()]
    except OSError as exc:
        _log.warning("ignoring unreadable %s: %s", directory, exc)
        return []
    return sorted(files, key=lambda p: p.name, reverse=True)


def history_read(lib, limit=100, before=None):
    """Up to `limit` records, newest first.

    `before` is a cursor: an ISO-8601 'ts' string, of which only records
    strictly older are returned (ISO timestamps sort lexicographically, so the
    comparison is a plain string compare). Malformed lines are SKIPPED, not
    raised on — a half-written line from a killed process must never take the
    history tab down with it."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 100
    if limit == 0:
        return []
    cursor = before if isinstance(before, str) and before else None
    out = []
    for path in history_files(lib):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.warning("ignoring unreadable %s: %s", path, exc)
            continue
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # truncated/garbage line: skip, keep reading
            if not isinstance(record, dict):
                continue
            if cursor is not None and str(record.get("ts") or "") >= cursor:
                continue
            out.append(record)
            if len(out) >= limit:
                return out
    return out


def history_prune(lib, keep_months):
    """Keep the `keep_months` newest month files, delete the rest.

    Counted in FILES, not in calendar distance: someone who comes back after a
    year keeps their last `keep_months` months of ACTUAL history instead of
    finding all of it pruned. `keep_months <= 0` prunes nothing — a stray
    settings value must never wipe history; that is what the explicit clear
    action is for. Never raises."""
    try:
        keep = int(keep_months)
    except (TypeError, ValueError):
        return
    if keep <= 0:
        return
    for path in history_files(lib)[keep:]:
        try:
            path.unlink()
        except OSError as exc:
            _log.warning("could not prune %s: %s", path, exc)
