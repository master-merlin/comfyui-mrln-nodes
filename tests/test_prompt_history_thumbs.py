"""The history row's mini thumbnail, and the matching that needs no wiring.

The whole feature rests on one claim: a PNG ComfyUI saved carries the executed
graph, that graph carries the MRLN Prompt Template node, and its `template` and
`seed` are the same pair the history line recorded. If that holds, a row can
find its own picture with nothing wired by the user.

So these tests build PNGs the way ComfyUI does — a real `prompt` text chunk
holding a real graph — and assert the match end to end, plus the four ways it
is allowed to find nothing (no image, no chunk, a linked seed, a different
render). Finding NOTHING is a first-class outcome here: a wrong thumbnail is
worse than no thumbnail, because it lies about which prompt made which picture.
"""

import io
import json

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

pytest.importorskip("PIL")

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from mrln.promptapi import histthumbs


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def comfy_png(
    template="animal/documentary", seed=730198984095416, *, linked_seed=False, chunk=True
):
    """A PNG shaped like one ComfyUI saved: pixels plus a 'prompt' chunk whose
    JSON is the executed graph. `linked_seed` models a seed fed by another node
    (a primitive), which arrives as ['node_id', slot] and names no seed."""
    graph = {
        "12": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}},
        "85": {
            "class_type": "MRLN_PromptTemplate",
            "inputs": {
                "template": template,
                "seed": ["99", 0] if linked_seed else seed,
                "selection_mode": "as configured",
                "profile": "standard",
            },
        },
    }
    info = PngInfo()
    if chunk:
        info.add_text("prompt", json.dumps(graph))
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), (120, 60, 30)).save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def write_output(monkeypatch, tmp_path, files):
    """Stand an output folder up and point the module at it, the way
    folder_paths.get_output_directory() would inside ComfyUI."""
    root = tmp_path / "output"
    (root / "MRLN").mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    monkeypatch.setattr(histthumbs, "_output_root", lambda: root)
    return root


# -- the key both sides agree on ---------------------------------------------


def test_the_key_normalises_a_seed_however_it_arrives():
    """A seed is an int in a graph and a string in a query payload; both name
    the same render, so both have to produce the same key."""
    assert histthumbs.record_key("a/b", 42) == histthumbs.record_key("a/b", "42") != ""


@pytest.mark.parametrize("template,seed", [("", 1), ("   ", 1), ("a/b", None), ("a/b", "nope")])
def test_an_unusable_pair_has_no_key(template, seed):
    assert histthumbs.record_key(template, seed) == ""


def test_a_linked_seed_names_no_render():
    """['99', 0] is a wire, not a number. Matching on it would pair every
    render of that graph with one arbitrary image."""
    graph = json.loads(
        json.dumps(
            {
                "85": {
                    "class_type": "MRLN_PromptTemplate",
                    "inputs": {"template": "a/b", "seed": ["99", 0]},
                }
            }
        )
    )
    assert histthumbs._key_from_graph(graph) == ""


def test_a_boolean_is_not_a_seed():
    """bool is an int in Python; True must not become seed 1."""
    graph = {
        "85": {"class_type": "MRLN_PromptTemplate", "inputs": {"template": "a/b", "seed": True}}
    }
    assert histthumbs._key_from_graph(graph) == ""


def test_a_graph_without_an_mrln_node_names_no_render():
    graph = {"1": {"class_type": "KSampler", "inputs": {"seed": 5}}}
    assert histthumbs._key_from_graph(graph) == ""


# -- the promise: a row finds its own picture, with nothing wired -------------


def test_a_row_finds_its_render_with_no_wiring(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {"MRLN/ComfyUI_00004_.png": comfy_png()})
    data = histthumbs.thumb_bytes(lib, "animal/documentary", 730198984095416)
    assert data, "the row could not find the image ComfyUI saved for it"
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", "not a webp"
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) <= histthumbs.THUMB_MAX_SIDE


def test_a_different_seed_is_a_different_render(lib, tmp_path, monkeypatch):
    """The nearest miss that matters: same template, one seed apart. A thumb
    here would put yesterday's picture on today's prompt."""
    write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(seed=1000)})
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 1001) is None


def test_a_different_template_is_a_different_render(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(template="animal/documentary")})
    assert histthumbs.thumb_bytes(lib, "animal/small-world", 730198984095416) is None


@pytest.mark.parametrize(
    "name,data",
    [
        ("MRLN/no-chunk.png", comfy_png(chunk=False)),
        ("MRLN/linked.png", comfy_png(linked_seed=True)),
    ],
)
def test_an_image_that_names_no_render_is_skipped(lib, tmp_path, monkeypatch, name, data):
    write_output(monkeypatch, tmp_path, {name: data})
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 730198984095416) is None


def test_a_real_sized_render_still_names_itself(lib, tmp_path, monkeypatch):
    """THE regression that only real data caught.

    Every PNG in this file is a few hundred bytes, so a header-sized read
    always held the whole image and nothing was ever truncated. A real render
    is megabytes: reading a slice of it and handing that to Pillow raises
    'image file is truncated' and the key comes back empty — even though the
    'prompt' chunk sits complete at byte 41. Every actual render silently had
    no thumbnail while this suite stayed green.

    So this one is deliberately bigger than the header slice."""
    import os

    info = PngInfo()
    info.add_text(
        "prompt",
        json.dumps(
            {
                "85": {
                    "class_type": "MRLN_PromptTemplate",
                    "inputs": {"template": "animal/documentary", "seed": 555},
                }
            }
        ),
    )
    buffer = io.BytesIO()
    # noise, so the pixel data cannot be compressed down under the slice
    noise = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3))
    noise.save(buffer, format="PNG", pnginfo=info)
    data = buffer.getvalue()
    assert len(data) > histthumbs._HEAD_BYTES, "fixture is not bigger than the header read"

    write_output(monkeypatch, tmp_path, {"MRLN/big.png": data})
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 555), (
        "a normally-sized render could not find its own image"
    )


def test_text_chunks_are_read_without_decoding_the_image(lib):
    """The chunk walk is the thing that survives a truncated read; assert it
    directly, on a slice, the way the indexer uses it."""
    info = PngInfo()
    info.add_text("prompt", '{"ok": 1}')
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), (9, 9, 9)).save(buffer, format="PNG", pnginfo=info)
    whole = buffer.getvalue()
    assert histthumbs._png_text_chunks(whole[:2048])["prompt"] == '{"ok": 1}'
    assert histthumbs._png_text_chunks(b"not a png at all") is None


def test_no_output_folder_means_no_thumbnails_and_no_crash(lib, monkeypatch):
    """Headless, or a ComfyUI that moved its output dir. The feature turns
    itself off; it does not take the History tab with it."""
    monkeypatch.setattr(histthumbs, "_output_root", lambda: None)
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 1) is None


def test_a_deleted_render_stops_producing_a_thumbnail(lib, tmp_path, monkeypatch):
    root = write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(seed=7)})
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 7)
    # the index still points at it, and the cache still holds the webp — but a
    # cache hit is the correct answer here: the tile the user saw is still true
    (root / "MRLN" / "a.png").unlink()
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 7), "the cached tile should survive"
    # with the cache gone too, the row goes quiet instead of erroring
    histthumbs.clear_thumb_cache(lib)
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 7) is None


# -- cost: the reason this is usable with thousands of records ----------------


def test_the_index_is_written_once_and_reused(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(seed=11)})
    histthumbs.refresh_index(lib)
    index_file = histthumbs._index_path(lib)
    assert index_file.is_file(), "the index was not persisted, so every row would rescan"
    stored = json.loads(index_file.read_text(encoding="utf-8"))
    assert stored["entries"], "nothing was indexed"
    # a second call must not have to read the folder again to answer
    monkeypatch.setattr(
        histthumbs, "_candidates", lambda *a, **k: pytest.fail("rescanned a known folder")
    )
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 11)


def test_the_encoded_tile_is_cached_on_disk(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(seed=21)})
    first = histthumbs.thumb_bytes(lib, "animal/documentary", 21)
    cached = histthumbs._cache_path(lib, histthumbs.record_key("animal/documentary", 21))
    assert cached.is_file(), "the webp was re-encoded per row instead of cached"
    assert histthumbs.thumb_bytes(lib, "animal/documentary", 21) == first


def test_a_scan_is_bounded_per_call(lib, tmp_path, monkeypatch):
    """A first run against a full output folder must not walk everything
    inside one request."""
    files = {f"MRLN/img{i:03}.png": comfy_png(seed=1000 + i) for i in range(12)}
    write_output(monkeypatch, tmp_path, files)
    monkeypatch.setattr(histthumbs, "SCAN_BUDGET", 4)
    index = histthumbs.refresh_index(lib, budget=4)
    assert len(index["entries"]) <= 4, "the budget was ignored"


# -- the route ----------------------------------------------------------------


def test_the_route_answers_bytes_and_a_cache_header(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(seed=31)})
    status, body = histthumbs.handle_history_thumb(
        lib, {"template": "animal/documentary", "seed": "31"}
    )
    assert status == 200
    assert body.content_type == "image/webp"
    assert "max-age" in body.headers.get("Cache-Control", ""), (
        "without a cache header a scroll back through a thousand rows refetches every tile"
    )


def test_the_route_404s_for_a_render_with_no_image(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {})
    status, body = histthumbs.handle_history_thumb(
        lib, {"template": "animal/documentary", "seed": "1"}
    )
    assert status == 404
    assert isinstance(body, dict), "a 404 here is JSON the row ignores, not bytes"


def test_the_route_refuses_a_request_that_names_no_render(lib):
    status, body = histthumbs.handle_history_thumb(lib, {"template": "animal/documentary"})
    assert status == 400 and "seed" in body["error"]


def test_turning_the_setting_off_stops_the_route(lib, tmp_path, monkeypatch):
    write_output(monkeypatch, tmp_path, {"MRLN/a.png": comfy_png(seed=41)})
    from mrln.promptapi import settings as settings_api

    monkeypatch.setattr(settings_api, "_read_settings", lambda _lib: {"history_thumbs": False})
    status, _body = histthumbs.handle_history_thumb(
        lib, {"template": "animal/documentary", "seed": "41"}
    )
    assert status == 404, "the opt-out has to be honoured server-side, not just in the panel"
