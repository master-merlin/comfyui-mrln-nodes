"""Image -> template intake (SPEC 4.1).

No binaries live in this repo, so every fixture image is written HERE with
Pillow: an A1111 `parameters` PNG, a ComfyUI-graph PNG, an ambiguous
two-sampler graph, and the same A1111 text in a JPEG/WebP EXIF UserComment.
The Civitai URL path is mocked at urlopen.

The two output paths are the point of the feature, so they are asserted
separately: path A must reproduce the extracted prompt BYTE FOR BYTE with
every LLM backend unconfigured (proved by making any llm_chat call an
error), path B must hand the same text to the existing de-composer and come
back with slots bound to real library items.
"""

import base64
import io
import json
import sys
import types
import urllib.error
import urllib.request

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

pytest.importorskip("PIL")

from PIL import Image, PngImagePlugin

from mrln import promptapi
from mrln.promptapi import intake as intake_mod
from mrln.promptapi import llm as llm_mod

A1111 = (
    "a red {sports} car on a wet street, <lora:carkit:0.8>, neon reflections. cinematic.\n"
    "Negative prompt: blurry, lowres\n"
    "Steps: 28, Sampler: DPM++ 2M Karras, CFG scale: 7, Seed: 123456, Size: 1024x1024, "
    'Model: flux1-dev, Civitai resources: [{"type":"lora","weight":0.8,"modelVersionId":789,'
    '"modelId":123,"modelName":"CarKit"},{"type":"checkpoint","modelVersionId":1}], '
    "Version: f1.0"
)

GRAPH = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 42,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "flux1-dev.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024}},
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "a blue car in the rain, <lora:kit:0.6>", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry, watermark", "clip": ["4", 1]},
    },
}

AMBIGUOUS = {
    "3": {"class_type": "KSampler", "inputs": {"positive": ["6", 0], "negative": ["7", 0]}},
    "8": {"class_type": "KSamplerAdvanced", "inputs": {"positive": ["9", 0], "negative": ["7", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "first pass prompt"}},
    "9": {"class_type": "CLIPTextEncode", "inputs": {"text": "second pass prompt"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry"}},
}


# -- fixture image writers ----------------------------------------------------


def png_bytes(fields, size=(8, 8)):
    info = PngImagePlugin.PngInfo()
    for key, value in fields.items():
        info.add_text(key, value)
    buffer = io.BytesIO()
    Image.new("RGB", size, "black").save(buffer, "PNG", pnginfo=info)
    return buffer.getvalue()


def exif_bytes(text, fmt="JPEG"):
    """The same A1111 block A1111 writes into EXIF UserComment: the 8-byte
    'UNICODE\\0' character-code prefix plus a UTF-16-BE payload."""
    exif = Image.Exif()
    exif[0x8769] = {0x9286: b"UNICODE\x00" + text.encode("utf-16-be")}
    buffer = io.BytesIO()
    image = Image.new("RGB", (8, 8), "black")
    if fmt == "JPEG":
        image.save(buffer, "JPEG", exif=exif)
    else:
        image.save(buffer, fmt, exif=exif.tobytes())
    return buffer.getvalue()


def data_uri(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def fake_urlopen(monkeypatch, result):
    """`result` is a payload dict (answered), an exception (raised), or a
    callable taking the request."""
    calls = []

    class _Response:
        def __init__(self, obj):
            self._body = json.dumps(obj).encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=None):
        calls.append(request)
        answer = result(request) if callable(result) else result
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def extract(lib, **payload):
    status, body = promptapi.handle_extract_image(lib, payload)
    return status, body


# -- the A1111 / Forge / Civitai `parameters` dialect --------------------------


def test_a1111_png_yields_prompt_negative_params_and_loras(lib):
    status, body = extract(lib, image=data_uri(png_bytes({"parameters": A1111})))
    assert status == 200, body
    assert body["dialect"] == "a1111" and body["source"] == "parameters"
    assert body["container"] == "PNG"
    # the <lora:…> tag left the prompt and closed its seam
    assert body["positive"] == "a red {sports} car on a wet street, neon reflections. cinematic."
    assert body["negative"] == "blurry, lowres"
    assert body["params"]["Steps"] == "28"
    assert body["params"]["Sampler"] == "DPM++ 2M Karras"
    assert body["params"]["Model"] == "flux1-dev"
    assert body["loras"][0]["name"] == "carkit"
    assert body["loras"][0]["strength_model"] == 0.8
    assert body["loras"][0]["strength_clip"] == 0.8


def test_the_civitai_resource_json_survives_the_comma_split(lib):
    """The settings tail is comma separated AND carries a JSON array full of
    commas — splitting on ', ' the naive way shreds it."""
    status, body = extract(lib, image=data_uri(png_bytes({"parameters": A1111})))
    assert status == 200
    resources = body["resources"]
    assert [r["type"] for r in resources] == ["lora", "checkpoint"]
    assert resources[0]["model_version_id"] == 789 and resources[0]["model_id"] == 123
    # the tag entry and the resource entry are the same LoRA, merged by name
    assert len(body["loras"]) == 1
    assert body["loras"][0]["model_version_id"] == 789
    assert body["loras"][0]["model_name"] == "CarKit"


def test_a_civitai_resource_never_invents_an_air():
    """An AIR's third segment IS the base-model family (lora_base_family reads
    it), so a made-up ecosystem would make LoRA Apply warn about a mismatch
    that does not exist. No AIR unless Civitai stated one."""
    without = promptapi.parse_civitai_resources(
        '[{"type":"lora","modelId":1,"modelVersionId":2,"modelName":"X"}]'
    )
    assert "air" not in without[0]
    assert without[0]["model_version_id"] == 2
    stated = promptapi.parse_civitai_resources(
        [{"type": "lora", "air": "urn:air:sdxl:lora:civitai:1@2"}]
    )
    assert stated[0]["air"] == "urn:air:sdxl:lora:civitai:1@2"


def test_broken_resource_json_is_ignored_not_fatal():
    assert promptapi.parse_civitai_resources("[{not json") == []
    assert promptapi.parse_civitai_resources('{"a": 1}') == []
    assert promptapi.parse_civitai_resources(None) == []


def test_a_bare_prompt_has_no_negative_and_no_tail(lib):
    status, body = extract(lib, image=data_uri(png_bytes({"parameters": "just a car"})))
    assert status == 200
    assert (body["positive"], body["negative"], body["params"]) == ("just a car", "", {})


def test_a_prompt_line_that_merely_holds_a_colon_is_not_a_settings_tail():
    """Disambiguation rule: a tail needs several k/v pairs AND at least one key
    a real generator emits. 'Style: neon' is prompt, not settings."""
    out = promptapi.parse_a1111_parameters("a car\nStyle: neon, Mood: calm")
    assert out["positive"] == "a car\nStyle: neon, Mood: calm"
    assert out["params"] == {}
    assert promptapi.is_param_tail("Steps: 20, Sampler: Euler") is True
    assert promptapi.is_param_tail("Style: neon, Mood: calm") is False
    assert promptapi.is_param_tail("Steps: 20") is False  # one pair is not a tail


def test_a_quoted_tail_value_keeps_its_commas():
    params = promptapi.parse_param_tail(
        'Steps: 20, Lora hashes: "kit: abc123, other: def456", Version: v1.9'
    )
    assert params["Lora hashes"] == '"kit: abc123, other: def456"'
    assert params["Version"] == "v1.9"


def test_a_multiline_negative_block_stays_whole():
    out = promptapi.parse_a1111_parameters(
        "line one\nline two\nNegative prompt: bad hands\nextra negative\nSteps: 4, Seed: 1"
    )
    assert out["positive"] == "line one\nline two"
    assert out["negative"] == "bad hands\nextra negative"
    assert out["params"] == {"Steps": "4", "Seed": "1"}


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP"])
def test_exif_usercomment_reads_the_same_dialect(lib, fmt):
    status, body = extract(lib, image=data_uri(exif_bytes(A1111, fmt), "image/jpeg"))
    assert status == 200, body
    assert body["source"] == "exif-usercomment" and body["container"] == fmt
    assert body["positive"] == "a red {sports} car on a wet street, neon reflections. cinematic."
    assert body["negative"] == "blurry, lowres"
    assert body["params"]["Seed"] == "123456"


def test_user_comment_decoding_covers_the_prefixes_writers_use():
    assert promptapi.decode_user_comment(b"UNICODE\x00" + "hi there".encode("utf-16-be")) == (
        "hi there"
    )
    assert promptapi.decode_user_comment(b"ASCII\x00\x00\x00plain text") == "plain text"
    assert promptapi.decode_user_comment(b"no prefix at all") == "no prefix at all"
    assert promptapi.decode_user_comment("already text") == "already text"
    assert promptapi.decode_user_comment(None) == ""


# -- inline <lora:…> tags -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a car, <lora:kit:0.8>, at dusk", "a car, at dusk"),
        ("<lora:kit:0.8> a car", "a car"),
        ("a car <lora:kit:0.8>", "a car"),
        ("a car, <lora:kit:0.8>", "a car"),
        ("a car,\n<lora:kit:0.8>\nat dusk", "a car\nat dusk"),
    ],
)
def test_a_removed_tag_leaves_no_seam(text, expected):
    cleaned, found = promptapi.strip_lora_tags(text)
    assert cleaned == expected
    assert found == [{"name": "kit", "strength_model": 0.8, "strength_clip": 0.8}]


def test_tag_strengths_default_and_split():
    _text, found = promptapi.strip_lora_tags("<lora:a> <lora:b:0.5> <lycoris:c:0.5:0.25>")
    assert [(e["name"], e["strength_model"], e["strength_clip"]) for e in found] == [
        ("a", 1.0, 1.0),
        ("b", 0.5, 0.5),
        ("c", 0.5, 0.25),
    ]


# -- ComfyUI graphs -----------------------------------------------------------


def test_comfy_graph_png_follows_the_sampler_links(lib):
    status, body = extract(lib, image=data_uri(png_bytes({"prompt": json.dumps(GRAPH)})))
    assert status == 200, body
    assert body["dialect"] == "comfyui" and body["source"] == "comfy-prompt"
    assert body["positive"] == "a blue car in the rain"  # tag stripped
    assert body["negative"] == "blurry, watermark"
    assert body.get("ambiguous") is not True
    assert body["loras"][0]["name"] == "kit"


def test_the_walk_passes_through_intermediate_conditioning_nodes():
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"positive": ["2", 0], "negative": ["4", 0]}},
        "2": {
            "class_type": "ControlNetApplyAdvanced",
            "inputs": {"positive": ["3", 0], "strength": 1.0},
        },
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "deep in the graph"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "nope"}},
    }
    out = promptapi.extraction_from_candidates(promptapi.graph_candidates(graph))
    assert out["positive"] == "deep in the graph" and out["negative"] == "nope"
    assert out.get("ambiguous") is not True


def test_an_ambiguous_two_sampler_graph_returns_every_candidate(lib):
    """Never guess silently: two different positive prompts means the UI asks."""
    status, body = extract(lib, image=data_uri(png_bytes({"prompt": json.dumps(AMBIGUOUS)})))
    assert status == 200, body
    assert body["ambiguous"] is True
    assert body["positive"] == ""  # nothing was picked for the user
    assert body["negative"] == "blurry"  # the unambiguous side still resolves
    texts = sorted(c["text"] for c in body["candidates"] if c["role"] == "positive")
    assert texts == ["first pass prompt", "second pass prompt"]
    assert {c["sampler"] for c in body["candidates"]} == {"3", "8"}


def test_two_samplers_sharing_one_prompt_are_not_ambiguous():
    graph = {
        "1": {"class_type": "KSampler", "inputs": {"positive": ["3", 0], "negative": ["4", 0]}},
        "2": {
            "class_type": "KSamplerAdvanced",
            "inputs": {"positive": ["3", 0], "negative": ["4", 0]},
        },
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "one prompt"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "one negative"}},
    }
    out = promptapi.extraction_from_candidates(promptapi.graph_candidates(graph))
    assert out["positive"] == "one prompt" and out["negative"] == "one negative"
    assert "ambiguous" not in out


def test_a_graph_without_a_recognizable_sampler_offers_unknown_roles():
    graph = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "orphan prompt"}}}
    out = promptapi.extraction_from_candidates(promptapi.graph_candidates(graph))
    assert out["ambiguous"] is True
    assert out["candidates"] == [
        {
            "role": "unknown",
            "text": "orphan prompt",
            "node": "1",
            "class_type": "CLIPTextEncode",
            "input": "text",
        }
    ]


def test_the_workflow_chunk_is_a_candidate_only_fallback(lib):
    workflow = {
        "nodes": [
            {"id": 6, "type": "CLIPTextEncode", "widgets_values": ["ui format prompt"]},
            {"id": 7, "type": "CLIPTextEncode", "widgets_values": ["ui format negative"]},
            {"id": 4, "type": "CheckpointLoaderSimple", "widgets_values": ["x.safetensors"]},
        ]
    }
    status, body = extract(lib, image=data_uri(png_bytes({"workflow": json.dumps(workflow)})))
    assert status == 200, body
    assert body["source"] == "comfy-workflow" and body["ambiguous"] is True
    assert sorted(c["text"] for c in body["candidates"]) == [
        "ui format negative",
        "ui format prompt",
    ]
    assert any("no reliable" in note for note in body["notes"])


def test_a_file_with_both_dialects_prefers_the_text_block(lib):
    raw = png_bytes({"parameters": "text block wins", "prompt": json.dumps(GRAPH)})
    status, body = extract(lib, image=data_uri(raw))
    assert status == 200
    assert body["source"] == "parameters" and body["positive"] == "text block wins"
    assert any("both" in note for note in body["notes"])


# -- refusals -----------------------------------------------------------------


def test_an_image_without_metadata_says_what_happened(lib):
    status, body = extract(lib, image=data_uri(png_bytes({})))
    assert status == 400
    assert "no generation metadata" in body["error"]
    assert "civitai.com URL" in body["remediation"]


def test_a_non_image_payload_is_a_clean_400(lib):
    status, body = extract(lib, image=data_uri(b"not an image at all"))
    assert status == 400
    assert "could not be read as an image" in body["error"]
    assert body["remediation"]


def test_a_missing_payload_names_both_ways_in(lib):
    status, body = extract(lib)
    assert status == 400 and "missing required parameter 'image'" in body["error"]
    assert "url" in body["remediation"]


def test_bad_base64_is_a_400_not_a_500(lib):
    status, body = extract(lib, image="data:image/png;base64,!!!!not base64!!!!")
    assert status == 400 and "not valid base64" in body["error"]


def test_the_image_cap_lives_under_the_route_body_cap():
    """A payload path must not be able to bypass or trivially exhaust the
    1 MiB body guard, so the decoded cap is chosen so its base64 form still
    leaves room for the JSON envelope."""
    encoded = -(-promptapi.MAX_IMAGE_BYTES // 3) * 4
    assert encoded < promptapi.MAX_BODY_BYTES
    assert promptapi.MAX_BODY_BYTES - encoded > 64 * 1024


def test_an_oversized_payload_is_refused_before_it_is_decoded(lib, monkeypatch):
    def must_not_decode(*args, **kwargs):
        raise AssertionError("base64 was decoded despite an oversized payload")

    monkeypatch.setattr(base64, "b64decode", must_not_decode)
    oversized = "A" * (-(-promptapi.MAX_IMAGE_BYTES // 3) * 4 + 4)
    status, body = extract(lib, image=f"data:image/png;base64,{oversized}")
    assert status == 413
    assert "intake limit" in body["error"] and "civitai.com URL" in body["remediation"]


def test_pillow_missing_is_an_actionable_400(lib, monkeypatch):
    """Class-B soft import: the module still imports, the handler still
    answers, and the message says how to fix it."""
    monkeypatch.setitem(sys.modules, "PIL", None)
    status, body = extract(lib, image=data_uri(b"\x89PNG\r\n\x1a\n"))
    assert status == 400
    assert "Pillow" in body["error"] and "ComfyUI" in body["remediation"]


# -- Civitai URL path (mocked) ------------------------------------------------

CIVITAI_IMAGE = {
    "items": [
        {
            "id": 12345678,
            "width": 1024,
            "height": 1024,
            "meta": {
                "prompt": "a silver coupe, <lora:carkit:0.7>, studio light",
                "negativePrompt": "blurry",
                "steps": 30,
                "cfgScale": 3.5,
                "sampler": "Euler",
                "seed": 999,
                "Model": "flux1-dev",
                "civitaiResources": [
                    {"type": "lora", "weight": 0.7, "modelVersionId": 789, "modelName": "CarKit"}
                ],
            },
        }
    ]
}


def test_a_civitai_image_url_maps_its_meta(lib, monkeypatch):
    calls = fake_urlopen(monkeypatch, CIVITAI_IMAGE)
    status, body = extract(lib, url="https://civitai.com/images/12345678?postId=1")
    assert status == 200, body
    assert body["source"] == "civitai-api" and body["dialect"] == "civitai"
    assert body["image_id"] == 12345678
    assert body["positive"] == "a silver coupe, studio light"
    assert body["negative"] == "blurry"
    assert body["params"]["Steps"] == "30" and body["params"]["CFG scale"] == "3.5"
    assert body["params"]["Size"] == "1024x1024"
    assert body["loras"][0]["model_version_id"] == 789
    # only the numeric id is reused, into our own constant endpoint
    assert calls[0].full_url.startswith(promptapi.CIVITAI_IMAGES_ENDPOINT + "?imageId=12345678")


def test_an_empty_first_answer_retries_asking_for_every_rating(lib, monkeypatch):
    calls = fake_urlopen(
        monkeypatch, lambda req: CIVITAI_IMAGE if "nsfw=X" in req.full_url else {"items": []}
    )
    status, body = extract(lib, url="https://civitai.com/images/12345678")
    assert status == 200 and body["positive"].startswith("a silver coupe")
    assert len(calls) == 2 and "nsfw=X" in calls[1].full_url


def test_no_record_at_all_is_a_404_that_points_at_the_file(lib, monkeypatch):
    fake_urlopen(monkeypatch, {"items": []})
    status, body = extract(lib, url="https://civitai.com/images/1")
    assert status == 404 and "no record for image 1" in body["error"]
    assert "drop the file" in body["remediation"]


def test_a_civitai_image_without_meta_says_it_was_stripped(lib, monkeypatch):
    fake_urlopen(monkeypatch, {"items": [{"id": 5, "meta": None}]})
    status, body = extract(lib, url="https://civitai.com/images/5")
    assert status == 400 and "no generation metadata" in body["error"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/images/1",
        "http://127.0.0.1:8188/images/1",
        "file:///etc/passwd",
        "https://civitai.com.evil.test/images/1",
    ],
)
def test_only_civitai_hosts_are_ever_fetched(lib, monkeypatch, url):
    """SSRF by construction: the host is checked and then discarded — every
    request is our own endpoint plus an integer."""

    def must_not_fetch(*args, **kwargs):
        raise AssertionError(f"the server fetched {url}")

    monkeypatch.setattr(urllib.request, "urlopen", must_not_fetch)
    status, body = extract(lib, url=url)
    assert status == 400
    assert "civitai.com" in body["error"] or "http(s)" in body["error"]


def test_a_cdn_image_link_says_to_use_the_page_url(lib):
    status, body = extract(lib, url="https://civitai.com/api/download/x/00001-12345.jpeg")
    assert status == 400 and "no image id" in body["error"]
    assert "images/12345678" in body["remediation"]


def test_a_civitai_http_error_never_echoes_the_api_key(lib, monkeypatch):
    promptapi.handle_save_settings(lib, {"civitai_api_key": "civ-SECRET-abcdef"})
    error = urllib.error.HTTPError(
        "https://civitai.com/api/v1/images?token=civ-SECRET-abcdef", 401, "no", {}, None
    )
    fake_urlopen(monkeypatch, error)
    status, body = extract(lib, url="https://civitai.com/images/9")
    assert status == 502 and "HTTP 401" in body["error"]
    assert "civ-SECRET-abcdef" not in json.dumps(body)
    assert "API key" in body["remediation"]


def test_a_network_failure_is_a_502_that_offers_the_file_path(lib, monkeypatch):
    fake_urlopen(monkeypatch, urllib.error.URLError("connection refused"))
    status, body = extract(lib, url="https://civitai.com/images/9")
    assert status == 502 and "Civitai unreachable" in body["error"]
    assert "drop the file" in body["remediation"]


def test_resolve_true_turns_a_version_id_into_a_real_air(lib, monkeypatch):
    """A bare modelVersionId cannot become an AIR offline (its ecosystem
    segment is the base-model family), so opting in asks Civitai — through the
    very summary the LoRA lookup already uses."""
    version = {
        "id": 789,
        "modelId": 123,
        "baseModel": "Flux.1 D",
        "trainedWords": ["CarKit", "wide body"],
        "model": {"name": "CarKit", "type": "LORA"},
    }
    fake_urlopen(
        monkeypatch,
        lambda req: version if "/model-versions/789" in req.full_url else CIVITAI_IMAGE,
    )
    status, body = extract(lib, url="https://civitai.com/images/12345678", resolve=True)
    assert status == 200, body
    entry = body["loras"][0]
    assert entry["air"] == "urn:air:flux1:lora:civitai:123@789"
    assert entry["trained_words"] == ["CarKit", "wide body"]
    assert entry["catchword"] == "CarKit"  # default selection = the first word


def test_without_resolve_the_missing_air_is_reported_not_invented(lib):
    status, body = promptapi.handle_extract_image(
        lib, {"image": data_uri(png_bytes({"parameters": A1111}))}
    )
    assert status == 200
    assert "air" not in body["loras"][0]
    assert any("resolve=true" in note for note in body["notes"])


def test_a_failed_resolve_leaves_the_extraction_usable(lib, monkeypatch):
    fake_urlopen(
        monkeypatch,
        lambda req: RuntimeError("boom") if "/model-versions/" in req.full_url else CIVITAI_IMAGE,
    )
    status, body = extract(lib, url="https://civitai.com/images/12345678", resolve=True)
    assert status == 200 and body["positive"].startswith("a silver coupe")
    assert "air" not in body["loras"][0]


# -- local LoRA resolution ----------------------------------------------------


def test_a_tag_name_resolves_to_the_installed_file_and_its_trigger(monkeypatch, tmp_path):
    """A tag carries no folder and no extension; the installed file does."""
    import struct

    header = json.dumps({"__metadata__": {"modelspec.trigger_phrase": "CarKitTrigger"}}).encode()
    path = tmp_path / "carkit.safetensors"
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    installed = {"kits/CarKit.safetensors": path}
    monkeypatch.setitem(
        sys.modules,
        "folder_paths",
        types.SimpleNamespace(
            get_filename_list=lambda kind: list(installed),
            get_folder_paths=lambda kind: [str(tmp_path)],
            get_full_path=lambda kind, name: str(installed[name]),
        ),
    )
    entries = promptapi.attach_local_files([{"name": "carkit"}])
    assert entries[0]["file"] == "kits/CarKit.safetensors"
    assert entries[0]["catchword"] == "CarKitTrigger"
    # asked and absent is a different answer from never asked
    absent = promptapi.attach_local_files([{"name": "nothing-here"}])
    assert absent[0]["file"] == ""


def test_outside_comfyui_nothing_is_resolved_and_nothing_breaks():
    entries = promptapi.attach_local_files([{"name": "carkit"}])
    assert entries[0]["file"] is None


# -- path A: verbatim ---------------------------------------------------------


@pytest.fixture()
def no_llm(monkeypatch):
    """Every LLM backend unset AND unreachable: a single llm_chat call fails
    the test. Path A must never touch one."""

    def must_not_call(*args, **kwargs):
        raise AssertionError("path A called an LLM backend")

    monkeypatch.setattr(llm_mod, "llm_chat", must_not_call)
    monkeypatch.setattr(intake_mod.decompose_api.llm, "llm_chat", must_not_call)


def apply_verbatim(lib, extraction, **extra):
    return promptapi.handle_extract_apply(
        lib, {"path": "verbatim", "extraction": extraction, **extra}
    )


def test_path_a_reproduces_the_extracted_prompt_byte_for_byte(lib, no_llm):
    status, body = promptapi.handle_extract_image(
        lib, {"image": data_uri(png_bytes({"parameters": A1111}))}
    )
    assert status == 200
    status, applied = apply_verbatim(lib, body)
    assert status == 200, applied
    assert applied["verbatim"] is True
    assert applied["positive"] == body["positive"]
    assert applied["negative"] == body["negative"]


@pytest.mark.parametrize(
    "positive",
    [
        "a red {sports} car",  # braces are the engine's variable syntax
        "plain prompt ending in a period.",  # the comma joiner strips those
        "a {a|b} wildcard-looking prompt",
        "trailing comma prompt,",
        "unbalanced } brace",
        "(emphasis:1.3) and 100% quality",
        "multi\nline\nprompt.",
    ],
)
def test_path_a_survives_every_shape_the_engine_would_otherwise_rewrite(lib, no_llm, positive):
    extraction = {"positive": positive, "negative": "bad, worse"}
    status, body = apply_verbatim(lib, extraction)
    assert status == 200, body
    assert body["positive"] == positive
    assert body["negative"] == "bad, worse"
    assert body["verbatim"] is True


def test_path_a_does_no_slotting_and_matches_nothing(lib, no_llm):
    """'bright red' IS a library item; path A must not bind it to one."""
    status, body = apply_verbatim(lib, {"positive": "bright red", "negative": ""})
    assert status == 200
    assert body["template"]["slots"] == []
    assert body["template"]["prefix"] == "bright red"
    assert "negative" not in body["template"]


def test_path_a_records_the_params_in_the_template_description(lib, no_llm):
    extraction = {
        "positive": "a car",
        "params": {"Steps": "28", "Sampler": "Euler", "Model": "flux1-dev", "Seed": "7"},
    }
    status, body = apply_verbatim(lib, extraction)
    assert status == 200
    description = body["template"]["description"]
    assert "Model: flux1-dev" in description and "Steps: 28" in description
    assert description.index("Model: flux1-dev") < description.index("Steps: 28")  # stable order


def test_path_a_attaches_the_loras_as_items_without_drawing_them(lib, no_llm):
    extraction = {
        "positive": "a car",
        "loras": [
            {
                "name": "carkit",
                "file": "kits/CarKit.safetensors",
                "strength_model": 0.8,
                "strength_clip": 0.4,
                "air": "urn:air:flux1:lora:civitai:123@789",
                "trained_words": ["CarKit", "wide body"],
            }
        ],
    }
    status, body = apply_verbatim(lib, extraction)
    assert status == 200
    item = body["section"]["items"][0]
    assert item["data"]["lora"] == "kits/CarKit.safetensors"
    assert item["data"]["strength_model"] == 0.8 and item["data"]["strength_clip"] == 0.4
    assert item["data"]["comment"] == "urn:air:flux1:lora:civitai:123@789"
    assert item["data"]["lora_info"]["trained_words"] == ["CarKit", "wide body"]
    assert item["text"] == "CarKit"  # default selection, back-compat
    # no slotting: the prompt is untouched and a note explains the consequence
    assert body["template"]["slots"] == []
    assert body["positive"] == "a car"
    assert any("no slotting" in note for note in body["notes"])


def test_path_a_saves_both_files_to_the_user_tier(lib, no_llm):
    extraction = {
        "positive": "a saved car",
        "negative": "blurry",
        "loras": [{"name": "carkit", "strength_model": 0.8}],
    }
    status, body = apply_verbatim(lib, extraction, slug="intake/my-shot", save=True)
    assert status == 200, body
    assert body["saved"] is True and body["section_slug"] == "intake/my-shot-loras"
    assert (lib.user_root / "templates" / "intake" / "my-shot.json").is_file()
    assert (lib.user_root / "sections" / "intake" / "my-shot-loras.json").is_file()
    # and the saved template really renders the prompt back
    status, preview = promptapi.handle_preview(lib, {"template": "intake/my-shot"})
    assert status == 200
    assert preview["positive"] == "a saved car" and preview["negative"] == "blurry"


def test_path_a_without_save_writes_nothing(lib, no_llm):
    status, body = apply_verbatim(lib, {"positive": "a car"}, slug="intake/dry")
    assert status == 200 and body["saved"] is False
    assert not (lib.user_root / "templates" / "intake").exists()


def test_path_a_needs_a_slug_to_save(lib, no_llm):
    status, body = apply_verbatim(lib, {"positive": "a car"}, save=True)
    assert status == 400 and "'slug' is required" in body["error"]


def test_path_a_rejects_a_path_escaping_slug(lib, no_llm):
    status, body = apply_verbatim(lib, {"positive": "a car"}, slug="../../etc/passwd", save=True)
    assert status == 400 and "slug" in body["error"]


def test_an_ambiguous_extraction_cannot_be_applied_blind(lib, no_llm):
    status, body = promptapi.handle_extract_image(
        lib, {"image": data_uri(png_bytes({"prompt": json.dumps(AMBIGUOUS)}))}
    )
    assert status == 200 and body["ambiguous"] is True
    status, applied = apply_verbatim(lib, body)
    assert status == 400 and "pick a candidate first" in applied["error"]
    # picking one makes it work
    picked = {**body, "positive": body["candidates"][0]["text"]}
    status, applied = apply_verbatim(lib, picked)
    assert status == 200 and applied["verbatim"] is True


def test_path_a_accepts_flat_fields_too(lib, no_llm):
    status, body = promptapi.handle_extract_apply(
        lib, {"path": "as-is", "positive": "a flat car", "negative": "bad"}
    )
    assert status == 200 and body["positive"] == "a flat car"


# -- path B: decompose --------------------------------------------------------


def test_path_b_hands_the_text_to_the_decomposer_and_binds_slots(lib):
    extraction = {"positive": "bright red\nmoonlit night", "negative": "lowres"}
    status, body = promptapi.handle_extract_apply(
        lib, {"path": "decompose", "extraction": extraction}
    )
    assert status == 200, body
    assert body["engine"] == "programmatic"
    assert body["matched"] == 2 and body["unmatched"] == 0
    bound = {(f["match"]["section"], f["match"]["item"]) for f in body["fragments"] if f["match"]}
    assert bound == {("color", "red"), ("lighting", "night")}


def test_path_b_forwards_the_engine_and_type_filters(lib, monkeypatch):
    seen = {}

    def spy(library, payload):
        seen.update(payload)
        return 200, {"engine": payload.get("engine")}

    monkeypatch.setattr(intake_mod.decompose_api, "handle_decompose", spy)
    status, body = promptapi.handle_extract_apply(
        lib,
        {
            "path": "b",
            "extraction": {"positive": "bright red"},
            "engine": "hybrid",
            "type": "car",
            "backend": "ollama",
        },
    )
    assert status == 200 and body["engine"] == "hybrid"
    assert seen["prompt"] == "bright red"  # the extracted text, not the raw payload
    assert seen["type"] == "car" and seen["backend"] == "ollama"
    assert "extraction" not in seen and "path" not in seen


def test_an_unknown_path_is_refused_with_both_names(lib):
    status, body = promptapi.handle_extract_apply(
        lib, {"path": "magic", "extraction": {"positive": "x"}}
    )
    assert status == 400
    assert "verbatim" in body["error"] and "decompose" in body["error"]


def test_an_extraction_without_a_positive_is_refused(lib):
    status, body = promptapi.handle_extract_apply(lib, {"path": "verbatim", "positive": "   "})
    assert status == 400 and "no positive prompt" in body["error"]


# -- registration -------------------------------------------------------------


def test_both_routes_are_declared_as_body_reading_posts():
    table = {(method, path): (handler, reads) for method, path, handler, reads in promptapi.ROUTES}
    assert table[("post", "/mrln/prompt/extract-image")] == (
        promptapi.handle_extract_image,
        True,
    )
    assert table[("post", "/mrln/prompt/extract-apply")] == (
        promptapi.handle_extract_apply,
        True,
    )
    # no GET twin: both write-ish paths stay off the CSRF-reachable verb
    assert not any(m == "get" and "extract" in p for m, p, _h, _b in promptapi.ROUTES)
