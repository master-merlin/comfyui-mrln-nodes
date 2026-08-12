"""Thumbnails: serving, replacing, resetting — and the Civitai LoRA preview.

Every fixture image is generated with Pillow inside the test (no binaries in
the repo, the same rule the image-intake suite follows), and every Civitai
response is canned: nothing here touches the network.

The load-bearing property under all of it is decision D3 — a repo update must
NEVER overwrite a user's thumbnails. It is asserted the way it is guaranteed:
no handler can name a factory path at all, so the factory tier is checked to be
byte-identical after every write path in this file.
"""

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library, factory_only_library

pytest.importorskip("PIL")

from PIL import Image

from mrln.promptapi import library as library_api
from mrln.promptapi import thumbs
from mrln.promptlib import store


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


# -- fixtures generated here, never committed ---------------------------------


def png_bytes(width=900, height=600, color=(200, 90, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width=640, height=480, color=(20, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def data_uri(raw, mime="image/png"):
    import base64

    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def webp_bytes(width=256, height=256, color=(10, 10, 10)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="WEBP")
    return buffer.getvalue()


def write_thumb(root, kind, slug, data=None):
    path = Path(root) / "thumbs" / kind / f"{slug}.webp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else webp_bytes())
    return path


def factory_snapshot(lib):
    return {
        str(p.relative_to(lib.factory_root)): p.read_bytes()
        for p in Path(lib.factory_root).rglob("*.webp")
    }


def ok(result):
    status, body = result
    assert status == 200, body
    return body


# -- serving ------------------------------------------------------------------


def test_no_thumbnail_in_either_tier_is_a_404_with_remediation(lib):
    status, body = thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"})
    assert status == 404
    assert "no thumbnail" in body["error"] and body["remediation"]


def test_a_factory_thumb_is_served_with_a_last_modified(lib):
    path = write_thumb(lib.factory_root, "templates", "basic")
    status, body = thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"})
    assert status == 200
    assert body.body == path.read_bytes()
    assert body.content_type == "image/webp"
    assert body.headers["Last-Modified"].endswith("GMT")
    assert body.headers["Cache-Control"] == "no-cache"
    assert body.headers["X-MRLN-Thumb-Tier"] == "factory"


def test_a_user_thumb_shadows_the_factory_one(lib):
    write_thumb(lib.factory_root, "templates", "basic", webp_bytes(color=(1, 1, 1)))
    user = write_thumb(lib.user_root, "templates", "basic", webp_bytes(color=(250, 250, 250)))
    status, body = thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"})
    assert status == 200 and body.body == user.read_bytes()
    assert body.headers["X-MRLN-Thumb-Tier"] == "user"


def test_a_nested_slug_serves_from_its_directory(lib):
    write_thumb(lib.factory_root, "sections", "location/urban")
    status, body = thumbs.handle_thumb(lib, {"kind": "sections", "slug": "location/urban"})
    assert status == 200 and body.body


def test_if_modified_since_answers_304_without_the_bytes(lib):
    write_thumb(lib.factory_root, "templates", "basic")
    served = ok(thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"}))
    status, body = thumbs.handle_thumb(
        lib,
        {
            "kind": "templates",
            "slug": "basic",
            "if_modified_since": served.headers["Last-Modified"],
        },
    )
    assert status == 304
    assert body.body == b""  # a 304 carries no body, only the validators
    assert body.headers["Last-Modified"] == served.headers["Last-Modified"]


def test_an_older_if_modified_since_gets_the_bytes(lib):
    write_thumb(lib.factory_root, "templates", "basic")
    status, body = thumbs.handle_thumb(
        lib,
        {
            "kind": "templates",
            "slug": "basic",
            "if_modified_since": "Wed, 21 Oct 2015 07:28:00 GMT",
        },
    )
    assert status == 200 and body.body


@pytest.mark.parametrize("stamp", ["", "not a date", "yesterday", "0"])
def test_a_malformed_if_modified_since_is_ignored(lib, stamp):
    """A broken header must serve the image, never a 304 the client cannot
    satisfy and never a 500."""
    write_thumb(lib.factory_root, "templates", "basic")
    status, body = thumbs.handle_thumb(
        lib, {"kind": "templates", "slug": "basic", "if_modified_since": stamp}
    )
    assert status == 200 and body.body


def test_binary_body_maps_onto_an_aiohttp_response():
    """The adapter change this feature needs, proven in isolation: routes.py
    answers `web.Response(body=…, content_type=…, headers=…)` for any handler
    body that is not a dict. Content-Type rides content_type, never headers —
    aiohttp refuses the duplicate."""
    web = pytest.importorskip("aiohttp.web")
    payload = thumbs.BinaryBody(b"RIFFwebp", "image/webp", {"Last-Modified": "x", "N": 1})
    response = web.Response(
        status=200,
        body=payload.body,
        content_type=payload.content_type,
        headers=payload.headers,
    )
    assert response.body == b"RIFFwebp"
    assert response.headers["Content-Type"] == "image/webp"
    assert response.headers["Last-Modified"] == "x" and response.headers["N"] == "1"
    assert "Content-Type" not in payload.headers


# -- writing ------------------------------------------------------------------


def test_post_writes_the_user_tier_and_never_the_factory_tier(lib):
    before = factory_snapshot(lib)
    body = ok(
        thumbs.handle_thumb_set(
            lib, {"kind": "templates", "slug": "basic", "image": data_uri(png_bytes())}
        )
    )
    assert body["tier"] == "user" and body["overrides_factory"] is False
    assert (lib.user_root / "thumbs" / "templates" / "basic.webp").is_file()
    assert factory_snapshot(lib) == before  # D3: the shipped tier is untouched
    assert not (Path(lib.factory_root) / "thumbs").exists()


def test_post_over_a_factory_thumb_shadows_it_byte_for_byte(lib):
    factory = write_thumb(lib.factory_root, "templates", "basic")
    original = factory.read_bytes()
    body = ok(
        thumbs.handle_thumb_set(
            lib, {"kind": "templates", "slug": "basic", "image": data_uri(png_bytes())}
        )
    )
    assert body["overrides_factory"] is True
    assert factory.read_bytes() == original
    served = ok(thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"}))
    assert served.headers["X-MRLN-Thumb-Tier"] == "user"


def test_post_downsizes_to_a_256_px_webp(lib):
    body = ok(
        thumbs.handle_thumb_set(
            lib, {"kind": "sections", "slug": "color", "image": data_uri(png_bytes(900, 600))}
        )
    )
    assert (body["width"], body["height"]) == (256, 171)  # longest side, aspect kept
    stored = lib.user_root / "thumbs" / "sections" / "color.webp"
    with Image.open(io.BytesIO(stored.read_bytes())) as img:
        assert img.format == "WEBP" and img.size == (256, 171)
    assert body["bytes"] == len(stored.read_bytes()) < len(png_bytes(900, 600))


def test_post_never_upscales_a_small_image(lib):
    body = ok(
        thumbs.handle_thumb_set(
            lib, {"kind": "sections", "slug": "color", "image": data_uri(png_bytes(120, 80))}
        )
    )
    assert (body["width"], body["height"]) == (120, 80)


def test_post_accepts_bare_base64_and_a_jpeg(lib):
    import base64

    body = ok(
        thumbs.handle_thumb_set(
            lib,
            {
                "kind": "sections",
                "slug": "lighting",
                "image": base64.b64encode(jpeg_bytes()).decode(),
            },
        )
    )
    assert body["ok"] is True and body["width"] == 256


def test_an_oversized_payload_is_refused_before_it_is_decoded(lib):
    from mrln.promptapi import intake

    oversized = "A" * (intake.MAX_IMAGE_BYTES * 4 // 3 + 64)
    status, body = thumbs.handle_thumb_set(
        lib, {"kind": "templates", "slug": "basic", "image": oversized}
    )
    assert status == 413
    assert "intake limit" in body["error"]
    assert "256 px" in body["remediation"]  # thumbnail advice, not the intake's
    assert not list(Path(lib.user_root).rglob("*.webp"))


def test_a_payload_that_is_not_an_image_is_a_clean_400(lib):
    status, body = thumbs.handle_thumb_set(
        lib, {"kind": "templates", "slug": "basic", "image": data_uri(b"not an image at all")}
    )
    assert status == 400 and body["error"] and body["remediation"]


def test_a_missing_image_payload_names_the_parameter(lib):
    status, body = thumbs.handle_thumb_set(lib, {"kind": "templates", "slug": "basic"})
    assert status == 400 and "'image'" in body["error"]


def test_writing_without_a_user_library_is_refused(tmp_path):
    lib = factory_only_library(tmp_path)
    write_thumb(lib.factory_root, "templates", "basic")
    status, body = thumbs.handle_thumb_set(
        lib, {"kind": "templates", "slug": "basic", "image": data_uri(png_bytes())}
    )
    assert status == 400 and "user library directory" in body["error"]
    assert ok(thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"})).body  # reads work


# -- resetting to factory -----------------------------------------------------


def test_delete_brings_the_factory_thumb_back(lib):
    factory = write_thumb(lib.factory_root, "templates", "basic", webp_bytes(color=(1, 1, 1)))
    ok(
        thumbs.handle_thumb_set(
            lib, {"kind": "templates", "slug": "basic", "image": data_uri(png_bytes())}
        )
    )
    body = ok(thumbs.handle_thumb_delete(lib, {"kind": "templates", "slug": "basic"}))
    assert body["removed"] is True and body["reverted_to_factory"] is True
    assert not (lib.user_root / "thumbs" / "templates" / "basic.webp").exists()
    served = ok(thumbs.handle_thumb(lib, {"kind": "templates", "slug": "basic"}))
    assert served.body == factory.read_bytes()
    assert served.headers["X-MRLN-Thumb-Tier"] == "factory"


def test_delete_can_never_remove_a_factory_thumb(lib):
    factory = write_thumb(lib.factory_root, "templates", "basic")
    before = factory_snapshot(lib)
    body = ok(thumbs.handle_thumb_delete(lib, {"kind": "templates", "slug": "basic"}))
    assert body["removed"] is False  # there was no USER thumb to remove
    assert body["has_thumb"] is True and body["reverted_to_factory"] is True
    assert factory.is_file() and factory_snapshot(lib) == before


def test_delete_of_a_thumbnail_nobody_set_is_a_clean_no_op(lib):
    body = ok(thumbs.handle_thumb_delete(lib, {"kind": "sections", "slug": "color"}))
    assert body == {
        "ok": True,
        "kind": "sections",
        "slug": "color",
        "removed": False,
        "reverted_to_factory": False,
        "has_thumb": False,
    }


# -- request validation -------------------------------------------------------

TRAVERSAL = ["../../evil", "..", "/etc/passwd", "..\\evil", "a/../../b", "con"]


@pytest.mark.parametrize("slug", TRAVERSAL)
def test_a_traversal_slug_is_refused_on_every_verb(lib, tmp_path, slug):
    for handler, payload in (
        (thumbs.handle_thumb, {}),
        (thumbs.handle_thumb_set, {"image": data_uri(png_bytes())}),
        (thumbs.handle_thumb_delete, {}),
    ):
        status, body = handler(lib, {"kind": "sections", "slug": slug, **payload})
        assert status == 400, (handler.__name__, status, body)
        assert body["error"] and body["remediation"]
    assert not list(tmp_path.rglob("*.webp"))


@pytest.mark.parametrize("slug", TRAVERSAL)
def test_a_traversal_lora_identity_is_neutralized_inside_the_tier(lib, tmp_path, slug):
    """A LoRA identity is a FILE NAME, not a slug, so it is reduced rather than
    refused — and the reduction can only ever land inside the loras tier."""
    status, _body = thumbs.handle_thumb_set(
        lib, {"kind": "loras", "slug": slug, "image": data_uri(png_bytes())}
    )
    assert status in (200, 400)
    if status == 200:
        written = list(Path(lib.user_root).rglob("*.webp"))
        assert written and all(
            Path(lib.user_root) / "thumbs" / "loras" in path.parents for path in written
        )
    assert not list(Path(lib.factory_root).rglob("*.webp"))


def test_an_unknown_kind_is_refused(lib):
    status, body = thumbs.handle_thumb(lib, {"kind": "profiles", "slug": "color"})
    assert status == 400 and "unknown thumbnail kind" in body["error"]


def test_every_handler_answers_the_error_contract_on_an_empty_payload(lib):
    for handler in (
        thumbs.handle_thumb,
        thumbs.handle_thumb_set,
        thumbs.handle_thumb_delete,
        thumbs.handle_lora_preview,
    ):
        status, body = handler(lib, {})
        assert status >= 400, handler.__name__
        assert body["error"] and body["remediation"], handler.__name__


def test_the_user_target_can_never_name_a_factory_path(lib):
    for kind, slug in (("sections", "color"), ("templates", "basic"), ("loras", "hycade")):
        target = thumbs.user_target(lib, kind, slug)
        assert Path(lib.user_root).resolve() in target.parents
        assert Path(lib.factory_root).resolve() not in target.parents


# -- has_thumb in the catalog payloads ----------------------------------------


def test_the_library_listing_carries_has_thumb_per_row(lib):
    write_thumb(lib.factory_root, "templates", "basic")
    write_thumb(lib.user_root, "sections", "mood")
    body = ok(library_api.handle_library(lib, {}))
    templates = {row["slug"]: row["has_thumb"] for row in body["templates"]}
    sections = {row["slug"]: row["has_thumb"] for row in body["sections"]}
    assert templates["basic"] is True and templates["varianted"] is False
    assert sections["mood"] is True and sections["color"] is False


def test_the_listing_reflects_a_write_after_the_invalidate(lib):
    assert ok(library_api.handle_library(lib, {}))["sections"][0]["has_thumb"] is False
    ok(
        thumbs.handle_thumb_set(
            lib, {"kind": "sections", "slug": "color", "image": data_uri(png_bytes())}
        )
    )
    rows = {
        row["slug"]: row["has_thumb"] for row in ok(library_api.handle_library(lib, {}))["sections"]
    }
    assert rows["color"] is True


def test_has_thumb_costs_one_directory_walk_per_tier_not_one_stat_per_row(lib, monkeypatch):
    """The whole reason thumbs.py keeps its own index: 268 shipped rows x
    store.thumb_path measured 362 ms on Windows (four Path.resolve() calls
    each). The listing walks the thumb dirs once and answers from a set."""
    write_thumb(lib.factory_root, "templates", "basic")
    write_thumb(lib.user_root, "sections", "mood")
    write_thumb(lib.user_root, "loras", "hycade")
    walks = []
    real_rglob = Path.rglob

    def counting(self, pattern, *args, **kwargs):
        if str(pattern).endswith(".webp"):
            walks.append(str(self))
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", counting)
    body = ok(library_api.handle_library(lib, {}))
    rows = len(body["templates"]) + len(body["sections"])
    assert rows >= 8  # more rows than walks: the cost does not scale with them
    assert len(walks) <= 2 * len(thumbs.KINDS)
    walks.clear()
    # and inside one request the index is answered from the memo — handlers
    # take a fingerprint first, which drops it once per request by design
    for kind, slug in [(k, s) for k in thumbs.KINDS for s in ("basic", "color", "mood", "x/y")]:
        thumbs.has_thumb(lib, kind, slug)
    assert walks == []
    ok(library_api.handle_library(lib, {}))  # a second request rebuilds it once
    assert len(walks) <= 2 * len(thumbs.KINDS)


def test_section_detail_and_item_pools_carry_has_thumb(lib):
    write_thumb(lib.factory_root, "sections", "color")
    write_thumb(lib.user_root, "loras", "hycade")
    body = ok(library_api.handle_section(lib, {"slug": "color"}))
    assert body["has_thumb"] is True
    assert all("has_thumb" not in item for item in body["items"])  # no LoRA, no tile
    kits = ok(library_api.handle_section(lib, {"slug": "lora/kits"}))
    assert kits["has_thumb"] is False
    assert [item["has_thumb"] for item in kits["items"]] == [True]
    pool = ok(library_api.handle_items(lib, {"ref": "lora/kits"}))["items"]
    assert [entry["has_thumb"] for entry in pool] == [True]


def test_template_detail_carries_has_thumb(lib):
    write_thumb(lib.user_root, "templates", "basic")
    body = ok(library_api.handle_template(lib, {"slug": "basic"}))
    assert body["has_thumb"] is True
    assert all("has_thumb" not in entry for entry in body["pools"]["color"])


# -- Civitai LoRA previews ----------------------------------------------------

PREVIEW_URL = "https://image.civitai.com/abc/1234-5678/width=450/preview.jpeg"

CIVITAI_VERSION = {
    "id": 789,
    "modelId": 123,
    "air": "urn:air:flux1:lora:civitai:123@789",
    "trainedWords": ["HycadeBodykit"],
    "name": "v2.0",
    "model": {"name": "Hycade Bodykit", "type": "LORA"},
    "images": [
        {
            "url": "https://image.civitai.com/abc/clip/width=450/clip.mp4",
            "type": "video",
            "nsfwLevel": 1,
        },
        {"url": PREVIEW_URL, "type": "image", "nsfwLevel": 1},
        {
            "url": "https://image.civitai.com/abc/other/width=450/other.jpeg",
            "type": "image",
            "nsfwLevel": 2,
        },
    ],
}


class _Response:
    def __init__(self, payload):
        self._data = payload

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._data = self._data, b""
        else:
            chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_net(monkeypatch, *, image=None, version=None, error=None):
    """Patch urllib.request.urlopen; returns the list of requested URLs."""
    seen = []

    def urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        seen.append(url)
        if error is not None:
            raise error
        if "/api/v1/model-versions/" in url:
            return _Response(json.dumps(version or CIVITAI_VERSION).encode())
        return _Response(image if image is not None else jpeg_bytes(1024, 768))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_a_canned_response_yields_a_downsized_user_tier_preview(lib, monkeypatch):
    seen = fake_net(monkeypatch)
    slug = thumbs.capture_lora_preview(CIVITAI_VERSION, file="kits\\hycade.safetensors", lib=lib)
    assert slug == "hycade"
    stored = lib.user_root / "thumbs" / "loras" / "hycade.webp"
    with Image.open(io.BytesIO(stored.read_bytes())) as img:
        assert img.format == "WEBP" and max(img.size) == 256
    # the CDN is asked for a thumbnail-sized original, and never with a key
    assert seen == ["https://image.civitai.com/abc/1234-5678/width=384/preview.jpeg"]
    assert not list(Path(lib.factory_root).rglob("*.webp"))  # never the repo tier


def test_the_lowest_rated_image_wins_and_videos_are_skipped(lib, monkeypatch):
    response = {
        "images": [
            {"url": "https://image.civitai.com/a/v.mp4", "type": "video", "nsfwLevel": 1},
            {"url": "https://image.civitai.com/a/r.jpeg", "type": "image", "nsfwLevel": 2},
            {"url": "https://image.civitai.com/a/pg.jpeg", "type": "image", "nsfwLevel": 1},
        ]
    }
    picked = thumbs.pick_preview_image(response)
    assert picked["url"].endswith("pg.jpeg") and picked["nsfw_level"] == 1
    seen = fake_net(monkeypatch)
    assert thumbs.capture_lora_preview(response, file="kit.safetensors", lib=lib) == "kit"
    assert seen == ["https://image.civitai.com/a/pg.jpeg"]


@pytest.mark.parametrize(
    "entry",
    [
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfwLevel": 4},
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfwLevel": 8},
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfwLevel": 32},
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfw": True},
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfwLevel": "Mature"},
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfwLevel": "wat"},
        {"url": "https://image.civitai.com/a/x.mp4", "type": "image", "nsfwLevel": 1},
        {"url": "https://image.civitai.com/a/x.jpeg", "type": "video", "nsfwLevel": 1},
    ],
)
def test_an_explicit_or_moving_preview_is_skipped_entirely(lib, monkeypatch, entry):
    """Above PG-13, or not a still: no tile at all, and NOT a request — the
    decision is made before anything is fetched or opened."""
    seen = fake_net(monkeypatch)
    assert thumbs.pick_preview_image({"images": [entry]}) is None
    assert thumbs.capture_lora_preview({"images": [entry]}, file="kit.safetensors", lib=lib) is None
    assert seen == []
    assert not list(Path(lib.user_root).rglob("*.webp"))


def test_an_unrated_entry_still_counts_as_pg(lib, monkeypatch):
    """Old uploads state nothing; treating those as explicit would mean no
    LoRA ever gets a face."""
    fake_net(monkeypatch)
    response = {"images": [{"url": "https://image.civitai.com/a/p.jpeg"}]}
    assert thumbs.capture_lora_preview(response, file="kit.safetensors", lib=lib) == "kit"


def test_one_preview_serves_every_item_that_shares_the_file(lib, monkeypatch):
    """Items reference the same weights with different spellings; the tile is
    keyed by the file, so one download answers for all of them."""
    lib.save_user(
        "sections",
        "lora/more",
        {
            "items": [
                {"name": "kit-a", "text": "A", "data": {"lora": "kits/Hycade.safetensors"}},
                {"name": "kit-b", "text": "B", "data": {"lora": "hycade.safetensors"}},
            ]
        },
    )
    fake_net(monkeypatch)
    assert thumbs.capture_lora_preview(CIVITAI_VERSION, file="hycade.safetensors", lib=lib)
    assert len(list((lib.user_root / "thumbs" / "loras").glob("*.webp"))) == 1
    rows = ok(library_api.handle_section(lib, {"slug": "lora/more"}))["items"]
    assert [row["has_thumb"] for row in rows] == [True, True]
    # and the pre-existing item in another section, spelled with a backslash
    kits = ok(library_api.handle_section(lib, {"slug": "lora/kits"}))["items"]
    assert [row["has_thumb"] for row in kits] == [True]


def test_a_preview_failure_leaves_the_download_successful_and_the_item_usable(lib, monkeypatch):
    """The capture runs after the weights are verified and may only ever log:
    a dead CDN must not turn a finished multi-GB download into an error."""
    fake_net(monkeypatch, error=urllib.error.URLError("connection refused"))
    assert thumbs.capture_lora_preview(CIVITAI_VERSION, file="hycade.safetensors", lib=lib) is None
    assert not list(Path(lib.user_root).rglob("*.webp"))
    rows = ok(library_api.handle_section(lib, {"slug": "lora/kits"}))["items"]
    assert rows[0]["data"]["lora"] and rows[0]["has_thumb"] is False


def test_a_preview_url_off_civitai_is_never_fetched(lib, monkeypatch):
    seen = fake_net(monkeypatch)
    response = {"images": [{"url": "http://169.254.169.254/latest/meta-data", "type": "image"}]}
    assert thumbs.capture_lora_preview(response, file="kit.safetensors", lib=lib) is None
    response = {"images": [{"url": "https://evil.example.com/x.jpeg", "type": "image"}]}
    assert thumbs.capture_lora_preview(response, file="kit.safetensors", lib=lib) is None
    assert seen == []


def test_a_user_thumbnail_survives_a_metadata_refresh(lib, monkeypatch):
    """The rule that protects a hand-set tile: automatic capture NEVER
    overwrites an existing file, so only the deliberate refresh replaces it."""
    ok(
        thumbs.handle_thumb_set(
            lib,
            {
                "kind": "loras",
                "slug": "kits/hycade.safetensors",
                "image": data_uri(png_bytes(300, 300, (7, 7, 7))),
            },
        )
    )
    mine = (lib.user_root / "thumbs" / "loras" / "hycade.webp").read_bytes()
    fake_net(monkeypatch)
    assert thumbs.capture_lora_preview(CIVITAI_VERSION, file="hycade.safetensors", lib=lib) is None
    assert (lib.user_root / "thumbs" / "loras" / "hycade.webp").read_bytes() == mine
    # the deliberate case replaces it
    assert thumbs.capture_lora_preview(
        CIVITAI_VERSION, file="hycade.safetensors", lib=lib, force=True
    )
    assert (lib.user_root / "thumbs" / "loras" / "hycade.webp").read_bytes() != mine


def test_capture_keys_on_the_air_when_no_file_is_known(lib, monkeypatch):
    fake_net(monkeypatch)
    assert thumbs.capture_lora_preview(CIVITAI_VERSION, lib=lib) == "civitai-123-789"
    assert (lib.user_root / "thumbs" / "loras" / "civitai-123-789.webp").is_file()
    served = ok(
        thumbs.handle_thumb(lib, {"kind": "loras", "slug": "urn:air:flux1:lora:civitai:123@789"})
    )
    assert served.body and served.headers["X-MRLN-Thumb-Tier"] == "user"


def test_capture_survives_a_response_that_is_not_a_civitai_shape(lib, monkeypatch):
    seen = fake_net(monkeypatch)
    for response in (None, {}, {"images": "nope"}, {"images": [None, 3, {}]}, "text"):
        assert thumbs.capture_lora_preview(response, file="kit.safetensors", lib=lib) is None
    assert seen == []


# -- the deliberate refresh route ---------------------------------------------


def test_the_refresh_route_stores_a_preview_for_an_item(lib, monkeypatch):
    lib.save_user(
        "sections",
        "lora/kits",
        {
            "items": [
                {
                    "name": "bodykit",
                    "text": "HycadeBodykit",
                    "data": {
                        "lora": "kits\\hycade.safetensors",
                        "comment": "urn:air:flux1:lora:civitai:123@789",
                    },
                }
            ]
        },
    )
    seen = fake_net(monkeypatch)
    body = ok(thumbs.handle_lora_preview(lib, {"section": "lora/kits", "item": "bodykit"}))
    assert body["ok"] is True and body["preview"] == "hycade" and body["nsfw_level"] == 1
    assert any("/api/v1/model-versions/789" in url for url in seen)
    assert (lib.user_root / "thumbs" / "loras" / "hycade.webp").is_file()


def test_the_refresh_route_says_why_it_showed_nothing(lib, monkeypatch):
    version = {
        "id": 789,
        "modelId": 123,
        "images": [{"url": "https://image.civitai.com/a/x.jpeg", "type": "image", "nsfwLevel": 16}],
    }
    fake_net(monkeypatch, version=version)
    body = ok(thumbs.handle_lora_preview(lib, {"air": "urn:air:flux1:lora:civitai:123@789"}))
    assert body["ok"] is False and body["preview"] is None
    assert "PG-13" in body["reason"]
    assert not list(Path(lib.user_root).rglob("*.webp"))


def test_the_refresh_route_needs_an_air(lib):
    status, body = thumbs.handle_lora_preview(lib, {"file": "kit.safetensors"})
    assert status == 400 and "AIR" in body["error"]


def test_the_refresh_route_reports_civitai_being_unreachable(lib, monkeypatch):
    fake_net(monkeypatch, error=urllib.error.URLError("boom"))
    status, body = thumbs.handle_lora_preview(lib, {"air": "urn:air:flux1:lora:civitai:123@789"})
    assert status == 502 and "unreachable" in body["error"] and body["remediation"]


def test_the_refresh_route_404s_on_an_unknown_item(lib):
    status, body = thumbs.handle_lora_preview(lib, {"section": "nope", "item": "x"})
    assert status == 404 and body["error"] and body["remediation"]


# -- the store contract this module builds on ---------------------------------


def test_the_lora_kind_lives_only_in_the_user_tier(lib):
    """A fetched preview is third-party content: the factory tier has no
    loras directory at all, so a repo update cannot ship (or clobber) one."""
    assert thumbs.factory_thumb(lib, "loras", "hycade") is None
    write_thumb(lib.factory_root, "loras", "hycade")  # even if one appeared
    assert thumbs.thumb_path(lib, "loras", "hycade") is None
    assert ("loras", "hycade") not in thumbs.thumb_index(lib)


def test_thumb_paths_agree_with_the_store_facade(lib):
    write_thumb(lib.user_root, "sections", "color")
    assert thumbs.thumb_path(lib, "sections", "color") == store.thumb_path(lib, "sections", "color")
    assert thumbs.user_target(lib, "sections", "color") == store.user_thumb_target(
        lib, "sections", "color"
    )
    assert thumbs.has_thumb(lib, "sections", "color") == store.has_thumb(lib, "sections", "color")
