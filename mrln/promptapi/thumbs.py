"""Thumbnails: serving them, replacing one, resetting to the shipped one —
plus the Civitai preview capture that gives a LoRA-bearing item a face.

Decision D3 in three sentences. Factory thumbs SHIP (rendered offline by
`_harness/tools/render_thumbs.py` on the user's own GPU and curated by them),
a user thumb SHADOWS the factory one exactly like a same-slug section does,
and a repo update can never overwrite a user thumb because the two tiers are
different directories:

    <factory>/thumbs/sections|templates/<slug>.webp   shipped with the pack
    <user>/thumbs/sections|templates/<slug>.webp       the user's replacement
    <user>/thumbs/loras/<file-stem>.webp              a fetched Civitai preview

Two invariants hold that up, both enforced per call rather than by convention:
`user_target()` resolves inside `lib.user_root` or raises, so no handler here
can name a factory path at all; and the `loras` kind exists ONLY in the user
tier, because a Civitai preview is fetched third-party content and has no
business in the repo.

`store.py` owns the sections/templates layout and is reused as-is. Its frozen
`THUMB_KINDS` does not know the `loras` kind the LoRA-preview feature added,
so the three lines of path building for that one kind are mirrored here (from
store's own constants, with the same validate-then-contain gate). Folding
`loras` into `store.THUMB_KINDS` later would let this file drop them again.

Pillow is Class B (ComfyUI guarantees it): soft-imported INSIDE the encode
function, with an actionable error when it is missing, and no requirements.txt
entry. Nothing here raises at import time.
"""

import contextlib
import json
import logging
import os
import re
import threading
import urllib.parse
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from .. import promptlib as pl
from ..promptlib import store

# module objects, not bound names: intake owns the image-payload cap and lora
# owns the secret scrubber + the AIR parser, and both stay monkeypatchable
from . import intake as intake_api
from . import lora as lora_api
from .core import ApiError, _guarded, _require_str
from .settings import _read_settings

LORA_KIND = "loras"
KINDS = (*store.THUMB_KINDS, LORA_KIND)
CONTENT_TYPE = "image/webp"

# 256 px longest side at webp q80: measured 1-7 KiB for rendered art, 30 KiB
# for pure noise. The repo budget for the factory set is ~2 MB (§6.1).
THUMB_MAX_SIDE = 256
THUMB_QUALITY = 80

# A Civitai preview is fetched at CDN thumbnail width and capped: we store a
# 256 px webp either way, so pulling a 20 MB original would be pure waste.
PREVIEW_FETCH_WIDTH = 384
PREVIEW_TIMEOUT = 15
MAX_PREVIEW_BYTES = 4 << 20

# Civitai's browsing levels: PG 1, PG-13 2, R 4, X 8, XXX 16, Blocked 32.
# Anything above PG-13 is never shown as a thumbnail — see pick_preview_image.
SAFE_NSFW_LEVEL = 2
_NSFW_WORDS = {"none": 1, "soft": 2, "mature": 4, "x": 8, "xxx": 16, "blocked": 32}

# Key in Library._scan_cache, so lib.invalidate() drops the thumb index with
# the rest of the disk snapshot ('@' prefix keeps it out of the kind keyspace,
# the same trick Library.pack_profiles and store's memo use).
_INDEX_CACHE_KEY = "@thumb-index"

PILLOW_REMEDIATION = (
    "Pillow ships with ComfyUI — install it into this Python environment "
    "('pip install Pillow'); until then the Composer falls back to glyph tiles"
)
OVERSIZE_REMEDIATION = (
    "the server stores a 256 px thumbnail anyway — scale the image down (or export it "
    "as a JPEG/WebP) before dropping it"
)
UNREADABLE_REMEDIATION = (
    "drop a PNG, JPEG or WebP — a truncated file, an SVG or a RAW camera file cannot "
    "be resized here"
)

_log = logging.getLogger(__name__)


class ThumbError(intake_api.IntakeError):
    """A refused thumbnail write. Inherits IntakeError's whole contract — its
    own status, an actionable remediation, and a body() that scrubs both
    strings — because the payload cap and its 413 come from that module."""


class BinaryBody:
    """A response body that is NOT JSON.

    Handlers return `(status, body)` and the adapter in routes.py answers with
    `web.json_response(body)`; a webp cannot travel that way. So a handler that
    must answer with bytes returns one of these and the adapter turns it into a
    `web.Response`. The adapter needs no import of this module — the branch is
    `isinstance(data, dict)`, and anything else is a binary body:

        if isinstance(data, dict):
            return web.json_response(data, status=status)
        return web.Response(status=status, body=data.body,
                            content_type=data.content_type, headers=data.headers)

    Content-Type never appears in `headers` (aiohttp refuses the duplicate);
    it rides `content_type`.
    """

    __slots__ = ("body", "content_type", "headers")

    def __init__(self, body=b"", content_type=CONTENT_TYPE, headers=None):
        self.body = bytes(body or b"")
        self.content_type = str(content_type)
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}

    def __repr__(self):
        return f"BinaryBody({len(self.body)} bytes, {self.content_type}, {self.headers})"


# -- paths -------------------------------------------------------------------


def _existing(path):
    return path if path is not None and path.is_file() else None


def _lora_tier_path(root, slug):
    """`<root>/thumbs/loras/<slug>.webp`, or None when `root` is unset.

    Mirrors store._tier_thumb for the ONE kind the store facade does not know,
    with the same two gates in the same order: validate_slug rejects '..',
    backslashes, absolute paths and empty segments, and the containment check
    afterwards means even a caller that skipped the validator cannot escape the
    tier. The layout constants come from store, so a layout change there moves
    this with it."""
    pl.validate_slug(slug)
    if not root:
        return None
    kind_dir = (Path(root) / store._THUMB_DIR / LORA_KIND).resolve()
    target = (kind_dir / f"{slug}{store.THUMB_EXT}").resolve()
    if kind_dir not in target.parents:
        raise pl.SchemaError(slug, "slug escapes the thumbnail directory")
    return target


def thumb_path(lib, kind, slug):
    """The file to SERVE for (kind, slug), or None when there is none.

    sections/templates: user tier shadows factory (store owns that). loras:
    user tier only — a fetched preview is never repo content."""
    if kind == LORA_KIND:
        return _existing(_lora_tier_path(getattr(lib, "user_root", None), slug))
    return store.thumb_path(lib, kind, slug)


def factory_thumb(lib, kind, slug):
    """The SHIPPED thumbnail for (kind, slug), or None. Read-only by design:
    nothing in this module writes the path it returns."""
    if kind == LORA_KIND:
        return None  # fetched content has no factory tier, by construction
    return _existing(store._tier_thumb(lib.factory_root, kind, slug))


def user_target(lib, kind, slug):
    """Where a write goes. THE D3 invariant, restated on every single write:
    the result is inside `lib.user_root` or this raises. No path exists through
    this module that can name a factory file, which is what makes "a repo
    update never overwrites a user thumbnail" true by construction."""
    root = getattr(lib, "user_root", None)
    if not root:
        raise pl.SchemaError(str(slug), "no user library directory is configured")
    if kind == LORA_KIND:
        target = _lora_tier_path(root, slug)
    else:
        target = store.user_thumb_target(lib, kind, slug)
    if Path(root).resolve() not in target.parents:
        raise pl.SchemaError(str(slug), "a thumbnail write may only target the user tier")
    return target


def tier_of(lib, path):
    """Which tier a served file came from, for the response header."""
    root = getattr(lib, "user_root", None)
    return "user" if root and Path(root).resolve() in Path(path).parents else "factory"


def _write_bytes_atomic(path, data):
    """Sibling-tmp + os.replace, the Library.save_user pattern: a crash or a
    concurrent write can never leave a half-written thumbnail in place. The tmp
    name carries pid+thread so two writers cannot share one torso."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):  # os.replace consumed it on the happy path
            with contextlib.suppress(OSError):
                os.remove(tmp)


# -- the has_thumb index -----------------------------------------------------


def thumb_index(lib):
    """{(kind, slug)} for every thumbnail on disk, both tiers.

    ONE directory walk per tier and kind, memoized in the library's scan cache
    and therefore dropped by lib.invalidate() with the rest of the snapshot.

    Why an index and not a stat per entry: `has_thumb` runs once per catalog
    row, and the shipped catalog is 268 rows. Measured on Windows (the platform
    that pays most for path resolution): 268 x store.thumb_path = 362 ms,
    because Path.resolve() costs ~0.33 ms here and each call does four of them;
    268 x store.has_thumb with every file present = 108 ms. This index costs
    0.22 ms while `thumbs/` does not exist yet and 18.5 ms with all 268 files
    present — i.e. handle_library keeps its ~45 ms instead of quadrupling.
    store.has_thumb stays the right call for ONE entry; a listing uses this."""
    cached = lib._scan_cache.get(_INDEX_CACHE_KEY)
    if cached is not None:
        return cached
    found = set()
    for tier, root in (("factory", lib.factory_root), ("user", getattr(lib, "user_root", None))):
        if not root:
            continue
        for kind in KINDS:
            if kind == LORA_KIND and tier == "factory":
                continue  # fetched previews never live in the repo tier
            kind_dir = Path(root) / store._THUMB_DIR / kind
            try:
                if not kind_dir.is_dir():
                    continue
                for path in kind_dir.rglob(f"*{store.THUMB_EXT}"):
                    slug = path.relative_to(kind_dir).with_suffix("").as_posix()
                    found.add((kind, slug))
            except OSError as exc:  # unreadable tier must not break a listing
                _log.warning("ignoring unreadable %s: %s", kind_dir, exc)
    lib._scan_cache[_INDEX_CACHE_KEY] = found
    return found


def has_thumb(lib, kind, slug):
    """Index-backed existence flag for catalog payloads. Returns False instead
    of raising — a bad slug must never break a whole listing."""
    try:
        return (str(kind), str(slug)) in thumb_index(lib)
    except Exception:
        return False


def has_lora_thumb(lib, identity):
    """Does the LoRA named by `identity` (a file name or an AIR) have a tile?"""
    slug = lora_slug(identity)
    return bool(slug) and has_thumb(lib, LORA_KIND, slug)


def annotate_entries(lib, entries, kind):
    """Add `has_thumb` to catalog listing rows (each carries its own 'slug')."""
    for entry in entries:
        entry["has_thumb"] = has_thumb(lib, kind, str(entry.get("slug") or ""))
    return entries


def annotate_items(lib, entries):
    """Add `has_thumb` to ITEM rows: pool entries and section-detail items.

    An item has no thumbnail of its own — its face is its LoRA's preview. That
    is why the flag appears only on LoRA-bearing rows: everything else falls
    back to the section thumb and then the UI's domain glyph, and a `false` on
    each of a few hundred item rows would be payload carrying no information.
    The URL the client builds is `?kind=loras&slug=<the row's own lora value>`;
    the endpoint reduces that identity to the storage key server-side, so the
    slug rule lives in exactly one place."""
    for entry in entries:
        identity = lora_identity(entry)
        if identity:
            entry["has_thumb"] = has_lora_thumb(lib, identity)
    return entries


# -- LoRA identity -> slug ---------------------------------------------------

_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")
_SLUG_HEAD = re.compile(r"^[^a-z0-9]+")
_SLUG_TAIL = re.compile(r"[^a-z0-9_-]+$")
_SAFETENSORS = ".safetensors"


def lora_air(data):
    """The Civitai AIR an item's data carries — the `comment` field the library
    stores it in, else the provenance blob — or ''."""
    if not isinstance(data, dict):
        return ""
    comment = str(data.get("comment") or "").strip()
    if comment.lower().startswith("urn:air:"):
        return comment
    info = data.get("lora_info")
    return str(info.get("air") or "").strip() if isinstance(info, dict) else ""


def lora_identity(source):
    """The string a LoRA preview is keyed by, out of an item's data, a
    section-detail item or a pool entry.

    The FILE wins over the AIR, which is the one place this deviates from the
    spec's wording ("AIR when present, else the stem"): what several items
    SHARE is the file — the same group-by-file logic the missing-LoRA banner
    uses — and the download worker knows only the bare file name it wrote,
    while the catalog rows the Composer draws carry the file and not always the
    AIR. Keying on the AIR would store previews under a name the display side
    cannot reconstruct. The AIR remains the identity when no file is known."""
    if not isinstance(source, dict):
        return ""
    data = source.get("data") if isinstance(source.get("data"), dict) else source
    file = str(data.get("lora") or source.get("lora") or "")
    return file or lora_air(data)


def lora_slug(identity):
    """A LoRA identity -> the single path-safe slug its preview is stored under.

    An AIR collapses to 'civitai-<model>-<version>' (short, stable, unique).
    Anything else is treated as a file name and reduced to its BASE name
    without the extension: the download worker only ever knows the bare name it
    wrote, while library items reference it with whatever folder they were
    authored with ('kits\\hycade.safetensors'), and the two must land on the
    same key or a fetched preview could never be found again. Two same-named
    files in different folders therefore share a tile — cosmetic, and the
    alternative loses the preview outright. Traversal is neutralized rather
    than refused here (a lora name is not a slug), and the result still goes
    through validate_slug + the containment check before any file is touched."""
    raw = str(identity or "").strip()
    if raw.lower().startswith("urn:air:"):
        parsed = lora_api.parse_air(raw)
        if parsed is not None:
            return f"civitai-{parsed[0]}-{parsed[1]}"
    stem = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if stem.endswith(_SAFETENSORS):
        stem = stem[: -len(_SAFETENSORS)]
    cleaned = _SLUG_HEAD.sub("", _SLUG_BAD.sub("-", stem))[:80]
    return _SLUG_TAIL.sub("", cleaned)


# -- encoding ----------------------------------------------------------------


def _image_module():
    try:
        from PIL import Image
    except Exception as exc:  # ImportError, but a broken install raises others
        raise ThumbError(
            f"resizing a thumbnail needs Pillow (PIL), which is not importable here: {exc}",
            PILLOW_REMEDIATION,
        ) from None
    return Image


def _resample(image_module):
    """LANCZOS across Pillow generations (Image.LANCZOS, then Resampling)."""
    resampling = getattr(image_module, "Resampling", None)
    return getattr(resampling, "LANCZOS", None) or image_module.LANCZOS


def _has_alpha(img):
    return img.mode in ("RGBA", "LA", "PA", "P") and (
        img.mode != "P" or "transparency" in (img.info or {})
    )


def encode_thumb(data, *, max_side=THUMB_MAX_SIDE, quality=THUMB_QUALITY):
    """Any image -> (webp bytes, (width, height)) at `max_side` longest side.

    thumbnail() only ever shrinks, so a small source keeps its size instead of
    being blown up into a blurry 256 px tile. Alpha survives (webp carries it);
    an EXIF orientation is applied, because a phone photo dropped as a
    thumbnail is otherwise silently sideways."""
    import io

    image_module = _image_module()
    try:
        with image_module.open(io.BytesIO(data)) as opened:
            img = opened
            # no ImageOps / no EXIF orientation: the raw orientation is correct
            with contextlib.suppress(Exception):
                from PIL import ImageOps

                img = ImageOps.exif_transpose(img) or img
            wanted = "RGBA" if _has_alpha(img) else "RGB"
            if img.mode != wanted:
                img = img.convert(wanted)
            img.thumbnail((max_side, max_side), _resample(image_module))
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=quality, method=6)
            return out.getvalue(), (img.width, img.height)
    except ThumbError:
        raise
    except Exception as exc:
        raise ThumbError(
            f"this file could not be read as an image ({type(exc).__name__}: {exc})",
            UNREADABLE_REMEDIATION,
        ) from None


def _decode_source(payload):
    """The dropped image, capped the way the image intake already caps its own
    payload: MAX_IMAGE_BYTES sits under the 1 MiB body cap and the ENCODED
    length is refused before any base64 is decoded. One cap, one
    implementation, no second way in — only the 413's remediation differs,
    because for a thumbnail the answer is "scale it down", not "paste a
    Civitai URL"."""
    try:
        return intake_api.decode_image_payload(payload.get("image"), key="image")
    except intake_api.IntakeError as exc:
        if exc.status == 413:
            raise ThumbError(str(exc), OVERSIZE_REMEDIATION, 413) from None
        raise


# -- HTTP conditional requests ----------------------------------------------


def _http_date(mtime):
    return formatdate(mtime, usegmt=True)


def _not_modified(stamp, mtime):
    """True when the client's If-Modified-Since already covers this file.

    HTTP dates carry whole seconds, so the mtime is truncated before comparing:
    a file written at x.4 s would otherwise always look newer than the
    Last-Modified we just sent, and every request would re-download."""
    if not stamp:
        return False
    try:
        since = parsedate_to_datetime(str(stamp))
    except (TypeError, ValueError):
        return False  # a malformed header is no header
    if since is None:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return int(mtime) <= int(since.timestamp())


def _cache_headers(stamp, tier):
    # no-cache = "you may keep it, but revalidate" -> the browser sends
    # If-Modified-Since and a replaced thumbnail is visible immediately, while
    # an unchanged one still costs a 304 instead of the bytes.
    return {"Last-Modified": stamp, "Cache-Control": "no-cache", "X-MRLN-Thumb-Tier": tier}


def _thumb_request(payload):
    """(kind, slug) out of a request, validated. For loras the `slug` is a raw
    LoRA identity (file name or AIR) and is reduced to the storage key here."""
    kind = _require_str(payload, "kind")
    if kind not in KINDS:
        raise ApiError(f"unknown thumbnail kind '{kind}' (kinds: {', '.join(KINDS)})")
    raw = _require_str(payload, "slug")
    if kind != LORA_KIND:
        return kind, pl.validate_slug(raw)
    slug = lora_slug(raw)
    if not slug:
        raise ApiError(f"'{raw}' carries no usable LoRA file name or Civitai AIR")
    return kind, slug


# -- handlers ----------------------------------------------------------------


@_guarded
def handle_thumb(lib, payload):
    """GET /mrln/prompt/thumb?kind=sections|templates|loras&slug=<slug>

    200 -> the webp BYTES (BinaryBody) with Last-Modified + Cache-Control
    304 -> when the request's If-Modified-Since covers the file's mtime
    404 -> {error, remediation} when neither tier has one (the UI draws its
           glyph tile then — a missing thumbnail is normal, not an error)

    `if_modified_since` arrives in the payload: a GET payload is otherwise the
    pure query string, so the adapter copies that ONE request header in when it
    is present. A client that puts the value in the query instead gets the same
    conditional answer, which is harmless — it can only ever ask for its own
    304."""
    kind, slug = _thumb_request(payload)
    path = thumb_path(lib, kind, slug)
    if path is None:
        return 404, {
            "error": f"no thumbnail for {kind}/{slug}",
            "remediation": "POST /mrln/prompt/thumb with an image to set one; "
            "factory thumbnails ship for templates and curated sections",
        }
    try:
        stat = path.stat()
        stamp = _http_date(stat.st_mtime)
        tier = tier_of(lib, path)
        if _not_modified(payload.get("if_modified_since"), stat.st_mtime):
            return 304, BinaryBody(headers=_cache_headers(stamp, tier))
        data = path.read_bytes()
    except OSError as exc:  # vanished/locked between the index and the read
        return 404, {
            "error": f"the thumbnail for {kind}/{slug} could not be read ({exc.strerror or exc})",
            "remediation": "reload the panel; the file may have just been replaced",
        }
    return 200, BinaryBody(data, CONTENT_TYPE, _cache_headers(stamp, tier))


@_guarded
def handle_thumb_set(lib, payload):
    """POST /mrln/prompt/thumb {kind, slug, image: "<data: URI | base64>"}
    -> 200 {ok, kind, slug, tier: "user", width, height, bytes,
            overrides_factory, fingerprint}

    Downsizes to 256 px longest side, webp q80, and writes the USER TIER ONLY:
    `user_target` cannot return a factory path, so this endpoint can never
    touch a shipped thumbnail — resetting to it is what handle_thumb_delete is
    for."""
    kind, slug = _thumb_request(payload)
    try:
        source = _decode_source(payload)
        data, (width, height) = encode_thumb(source)
    except intake_api.IntakeError as exc:
        return exc.status, exc.body()
    target = user_target(lib, kind, slug)
    factory = factory_thumb(lib, kind, slug)
    try:
        _write_bytes_atomic(target, data)
    except OSError as exc:
        return 500, {
            "error": lora_api._scrub_secrets(f"could not write the thumbnail: {exc}"),
            "remediation": "check that the user library directory is writable "
            "(antivirus and cloud-sync tools lock files on Windows)",
        }
    lib.invalidate()
    return 200, {
        "ok": True,
        "kind": kind,
        "slug": slug,
        "tier": "user",
        "width": width,
        "height": height,
        "bytes": len(data),
        "overrides_factory": factory is not None,
    }


@_guarded
def handle_thumb_delete(lib, payload):
    """POST /mrln/prompt/thumb-delete {kind, slug}
    -> 200 {ok, kind, slug, removed, reverted_to_factory, has_thumb}

    Removes the USER thumbnail so the factory one reappears — the shadow model
    working, exactly as deleting a user-tier section reveals the factory
    section again. A factory thumbnail is not deletable through the API at all:
    `user_target` only ever names a user path, so there is nothing to guard
    against here beyond that one call.

    Registered as a POST, not a DELETE: the route table is linted to
    method in {get, post} (tests/test_prompt_api.py::test_route_table_sane) and
    the pack already spells the same idea POST /mrln/prompt/delete."""
    kind, slug = _thumb_request(payload)
    target = user_target(lib, kind, slug)
    removed = False
    try:
        if target.is_file():
            target.unlink()
            removed = True
    except OSError as exc:
        return 500, {
            "error": lora_api._scrub_secrets(f"could not remove the thumbnail: {exc}"),
            "remediation": "close whatever holds the file open and retry",
        }
    lib.invalidate()
    remaining = thumb_path(lib, kind, slug)
    return 200, {
        "ok": True,
        "kind": kind,
        "slug": slug,
        "removed": removed,
        "reverted_to_factory": remaining is not None and tier_of(lib, remaining) == "factory",
        "has_thumb": remaining is not None,
    }


# -- Civitai LoRA previews ---------------------------------------------------

_VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".gif")
_WIDTH_IN_PATH_RE = re.compile(r"/width=\d+", re.IGNORECASE)


def _nsfw_level(entry):
    """Civitai's browsing level for one image entry, as an int on their scale.

    `nsfwLevel` is what the model-version endpoint sends today; older and
    neighbouring shapes send a word ('None'/'Soft'/'Mature'/'X') or a bare
    boolean, so all three are read. An UNKNOWN word counts as Blocked (skip);
    absent AND unflagged counts as PG, because a response that states nothing
    is the common case for the oldest uploads and treating those as explicit
    would mean no LoRA ever gets a face."""
    raw = entry.get("nsfwLevel")
    if isinstance(raw, bool):
        pass
    elif isinstance(raw, (int, float)):
        return max(1, int(raw))
    elif isinstance(raw, str) and raw.strip():
        word = raw.strip().lower()
        return max(1, int(word)) if word.isdigit() else _NSFW_WORDS.get(word, 32)
    flag = entry.get("nsfw")
    if isinstance(flag, str) and flag.strip():
        return _NSFW_WORDS.get(flag.strip().lower(), 32)
    return 8 if flag is True else 1


def _is_image_entry(entry):
    """A still image, not a video: a thumbnail cannot be an mp4, and Civitai
    ships both in the same `images[]` array."""
    if str(entry.get("type") or "image").strip().lower() != "image":
        return False
    path = urllib.parse.urlsplit(str(entry.get("url") or "")).path.lower()
    return not path.endswith(_VIDEO_SUFFIXES)


def pick_preview_image(response):
    """The one entry out of a Civitai `images[]` array we are willing to show,
    or None — {url, nsfw_level, index}.

    Two rules, both from the user's ruling. Skip VIDEO entries. And respect the
    rating: only entries at or below PG-13 are eligible, and of those the
    LOWEST rated wins (ties keep the order Civitai chose, which is its own
    preferred-image order). When nothing qualifies the LoRA simply gets no
    preview — surfacing something explicit that nobody asked for is worse than
    an empty tile. Pure: no library, no network, no filesystem, so a canned
    response is fully testable and a call with nothing usable costs nothing."""
    images = response.get("images") if isinstance(response, dict) else None
    if not isinstance(images, list):
        return None
    best = None
    for index, entry in enumerate(images):
        if not isinstance(entry, dict) or not str(entry.get("url") or "").strip():
            continue
        if not _is_image_entry(entry):
            continue
        level = _nsfw_level(entry)
        if level > SAFE_NSFW_LEVEL:
            continue
        if best is None or level < best[0]:
            best = (level, index, entry)
    if best is None:
        return None
    return {"url": str(best[2]["url"]).strip(), "nsfw_level": best[0], "index": best[1]}


def _preview_url(url):
    """The preview URL, host-pinned and asked for at thumbnail width.

    The URL comes out of a Civitai response rather than a user request, but it
    still decides where this server connects, so the host is pinned to
    civitai.com the same way intake pins its image endpoint — and embedded
    credentials are refused. Civitai's CDN encodes the wanted width in the
    path, so rewriting it keeps the fetch small (we store 256 px regardless)."""
    parts = urllib.parse.urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise ValueError(f"preview URL is not https: {parts.scheme or url!r}")
    if "@" in parts.netloc:
        raise ValueError("preview URL carries embedded credentials")
    if host != "civitai.com" and not host.endswith(".civitai.com"):
        raise ValueError(f"preview URL host {host or url!r} is not civitai.com")
    path = _WIDTH_IN_PATH_RE.sub(f"/width={PREVIEW_FETCH_WIDTH}", parts.path)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _fetch_preview(url):
    """The preview bytes. NO API key travels here: a CDN media host has no
    business seeing a credential, and previews are public."""
    import urllib.request

    request = urllib.request.Request(
        _preview_url(url), headers={"User-Agent": "ComfyUI-MRLN-Nodes"}
    )
    with urllib.request.urlopen(request, timeout=PREVIEW_TIMEOUT) as resp:
        data = resp.read(MAX_PREVIEW_BYTES + 1)
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError(f"preview image exceeds {MAX_PREVIEW_BYTES // 1024} KiB")
    if not data:
        raise ValueError("preview image was empty")
    return data


def capture_lora_preview(response, *, file="", air="", lib=None, force=False):
    """Store a Civitai preview as this LoRA's thumbnail. Returns the slug it
    wrote, or None.

    NEVER raises and never blocks a caller that matters: both call sites run
    after the weights are verified, so a failed preview leaves a working LoRA
    and a log line. A missing preview is the normal case, not an error.

    `force=False` (the automatic paths) NEVER overwrites an existing tile —
    that single rule is what protects a thumbnail the user set by hand from a
    later metadata refresh. The explicit "refresh preview" action passes
    force=True, because replacing one is then exactly what was asked for."""
    try:
        return _capture_lora_preview(response, file=file, air=air, lib=lib, force=force)
    except Exception as exc:
        _log.info(
            "MRLN prompt: LoRA preview skipped (%s: %s)",
            type(exc).__name__,
            lora_api._scrub_secrets(str(exc)),
        )
        return None


def _capture_lora_preview(response, *, file, air, lib, force):
    picked = pick_preview_image(response)
    if picked is None:
        return None  # nothing safe/usable: decided BEFORE any library is opened
    identity = file or air
    if not identity:
        identity = str((lora_api._civitai_summary(response) or {}).get("air") or "")
    slug = lora_slug(identity)
    if not slug:
        return None
    if lib is None:
        # the download worker has no library object (it is a background thread
        # started from a request that has long returned); the user root is a
        # property of the install, so opening one here is the same thing the
        # route layer does per request
        lib = pl.open_library()
    target = user_target(lib, LORA_KIND, slug)
    if target.is_file() and not force:
        return None
    data, _size = encode_thumb(_fetch_preview(picked["url"]))
    _write_bytes_atomic(target, data)
    lib.invalidate()
    _log.info("MRLN prompt: stored a Civitai preview for '%s' as %s", identity, target.name)
    return slug


def _item_data(lib, section_slug, item_name):
    section = lib.load_section(section_slug)  # SectionNotFoundError -> 404
    target = next((i for i in section.items if i.name == item_name), None)
    if target is None:
        raise ApiError(f"item '{item_name}' not found in section '{section_slug}'")
    return target.data or {}


def _civitai_version(lib, version_id):
    import urllib.request

    key = lora_api._remember_secret(str(_read_settings(lib).get("civitai_api_key") or ""))
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"https://civitai.com/api/v1/model-versions/{int(version_id)}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=PREVIEW_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


@_guarded
def handle_lora_preview(lib, payload):
    """POST /mrln/prompt/lora-preview {air?, section?, item?, file?, force?}
    -> 200 {ok, slug, preview, nsfw_level, reason?} | 400 | 404 | 502

    The DELIBERATE refresh. Automatic capture never overwrites an existing
    tile, so replacing one has to be asked for — which is why `force` defaults
    to true HERE and to false there. Unlike the automatic paths this reports
    failures honestly: the user pressed a button and deserves to know."""
    air = str(payload.get("air") or "").strip()
    file = str(payload.get("file") or "").strip()
    section = str(payload.get("section") or "").strip()
    item = str(payload.get("item") or "").strip()
    if section and item:
        data = _item_data(lib, section, item)
        file = file or str(data.get("lora") or "")
        air = air or lora_air(data)
    parsed = lora_api.parse_air(air)
    if parsed is None:
        raise ApiError(
            f"'{air}' is not a Civitai AIR urn — a preview can only be fetched for a LoRA "
            "whose item carries one (urn:air:<eco>:<type>:civitai:<model>@<version>)"
        )
    try:
        response = _civitai_version(lib, parsed[1])
    except Exception as exc:
        return 502, {
            "error": lora_api._scrub_secrets(f"Civitai unreachable: {type(exc).__name__}: {exc}"),
            "remediation": "check your network and the API key in the Composer's Settings tab",
        }
    picked = pick_preview_image(response)
    if picked is None:
        return 200, {
            "ok": False,
            "slug": lora_slug(file or air),
            "preview": None,
            "reason": "this version's Civitai previews are videos or rated above PG-13, "
            "so none of them is shown as a thumbnail — set one by hand instead",
        }
    slug = capture_lora_preview(
        response, file=file, air=air, lib=lib, force=payload.get("force") is not False
    )
    if slug is None:
        return 502, {
            "error": "the preview image could not be fetched or stored (see the server log)",
            "remediation": "retry, or set the thumbnail by hand from the item editor",
        }
    return 200, {
        "ok": True,
        "slug": slug,
        "preview": slug,
        "nsfw_level": picked["nsfw_level"],
    }
