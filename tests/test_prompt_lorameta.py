"""Trigger words from LoRA metadata: pure safetensors-header reading (no
torch) and the lora-meta API handler's no-ComfyUI behavior."""

import json
import struct
import sys
import types
import urllib.error
import urllib.request

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi
from mrln.promptlib import read_safetensors_metadata, trigger_from_metadata


def write_st(path, meta):
    header = json.dumps({"__metadata__": meta, "__dummy__": None}).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    return path


def fake_folder_paths(monkeypatch, installed):
    """Inject a folder_paths stub. `installed` maps the name ComfyUI reports
    (exact casing/separators) to the real file on disk."""
    module = types.SimpleNamespace(
        get_filename_list=lambda kind: list(installed),
        get_folder_paths=lambda kind: ["/loras"],
        get_full_path=lambda kind, name: str(installed[name]),
    )
    monkeypatch.setitem(sys.modules, "folder_paths", module)
    return module


class _FakeResponse:
    def __init__(self, obj):
        self._body = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, result):
    """`result` is either a payload dict (answered) or an exception to raise."""

    def fake(request, timeout=None):
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def http_error(code):
    return urllib.error.HTTPError("https://civitai.com/x", code, "boom", {}, None)


def test_reads_metadata_dict(tmp_path):
    path = write_st(tmp_path / "kit.safetensors", {"modelspec.trigger_phrase": "BMWM4CS_G82"})
    assert read_safetensors_metadata(path) == {"modelspec.trigger_phrase": "BMWM4CS_G82"}


def test_phrase_key_wins_over_tag_frequency():
    meta = {
        "modelspec.trigger_phrase": "  BMWM4CS_G82  ",
        "ss_tag_frequency": json.dumps({"10_car": {"other": 999}}),
    }
    assert trigger_from_metadata(meta) == ("BMWM4CS_G82", "modelspec.trigger_phrase")


def test_tag_frequency_merges_datasets_and_picks_top():
    meta = {
        "ss_tag_frequency": json.dumps(
            {
                "10_car": {"bmwm4cs": 40, "car": 30},
                "5_studio": {"bmwm4cs": 25, "studio": 10},
            }
        )
    }
    assert trigger_from_metadata(meta) == ("bmwm4cs", "ss_tag_frequency")


def test_training_comment_as_trigger():
    # the Arcane Tuner / hands-on kohya convention: bare token in the comment
    assert trigger_from_metadata({"ss_training_comment": "FerrariF40"}) == (
        "FerrariF40",
        "ss_training_comment",
    )
    # 'trigger words:' prefix styles are stripped
    assert trigger_from_metadata({"ss_training_comment": "trigger words: 911T4rga"}) == (
        "911T4rga",
        "ss_training_comment",
    )
    # explicit comment beats the derived tag frequency
    both = {
        "ss_training_comment": "RBOctavia",
        "ss_tag_frequency": json.dumps({"10_car": {"other": 99}}),
    }
    assert trigger_from_metadata(both) == ("RBOctavia", "ss_training_comment")


def test_training_comment_junk_rejected():
    freq = {"ss_tag_frequency": json.dumps({"10_x": {"fallback": 5}})}
    for junk in (
        "None",
        "none",
        "Dynamic resize with sv_fro: 0.9 from 384;",
        "see https://example.com for usage",
        "a very long sentence that keeps going and clearly is not a trigger word",
    ):
        assert trigger_from_metadata({"ss_training_comment": junk, **freq}) == (
            "fallback",
            "ss_tag_frequency",
        )


def test_no_trigger_sources_yields_none():
    assert trigger_from_metadata({}) == (None, None)
    assert trigger_from_metadata({"ss_tag_frequency": "{broken"}) == (None, None)
    assert trigger_from_metadata({"trigger_phrase": "   "}) == (None, None)
    assert trigger_from_metadata({"ss_training_comment": "None"}) == (None, None)


def test_non_safetensors_and_corrupt_files_raise(tmp_path):
    ckpt = tmp_path / "old.ckpt"
    ckpt.write_bytes(b"pickle")
    with pytest.raises(ValueError, match=r"only \.safetensors"):
        read_safetensors_metadata(ckpt)
    truncated = tmp_path / "cut.safetensors"
    truncated.write_bytes(b"\x05\x00")
    with pytest.raises(ValueError, match="truncated"):
        read_safetensors_metadata(truncated)
    bogus = tmp_path / "big.safetensors"
    bogus.write_bytes(struct.pack("<Q", 1 << 60) + b"x")
    with pytest.raises(ValueError, match="implausible"):
        read_safetensors_metadata(bogus)
    notjson = tmp_path / "nj.safetensors"
    notjson.write_bytes(struct.pack("<Q", 4) + b"{{{{")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_safetensors_metadata(notjson)


def test_missing_metadata_block_is_empty_dict(tmp_path):
    header = json.dumps({"weight": {"dtype": "F16"}}).encode("utf-8")
    path = tmp_path / "bare.safetensors"
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    assert read_safetensors_metadata(path) == {}


def test_handler_requires_name_and_comfyui():
    status, body = promptapi.handle_lora_meta(None, {})
    assert status == 400 and "name" in body["error"]
    # under pytest there is no folder_paths module: graceful refusal
    status, body = promptapi.handle_lora_meta(None, {"name": "x.safetensors"})
    assert status == 400 and "ComfyUI" in body["error"]
    status, body = promptapi.handle_lora_civitai(None, {"name": "x.safetensors"})
    assert status == 400 and "ComfyUI" in body["error"]


# -- Civitai summary + settings ----------------------------------------------


def test_civitai_summary_uses_response_air_and_words():
    resp = {
        "id": 789,
        "modelId": 123,
        "name": "v2.0",
        "air": "urn:air:flux1:lora:civitai:123@789",
        "trainedWords": [" BMWM4CS_G82 ", "m4 coupe"],
        "model": {"name": "BMW M4 CS", "type": "LORA"},
    }
    out = promptapi._civitai_summary(resp)
    assert out["trigger"] == "BMWM4CS_G82"
    assert out["trained_words"] == ["BMWM4CS_G82", "m4 coupe"]
    assert out["air"] == "urn:air:flux1:lora:civitai:123@789"
    assert out["model_name"] == "BMW M4 CS" and out["version_name"] == "v2.0"


def test_civitai_summary_constructs_air_when_absent():
    resp = {
        "id": 789,
        "modelId": 123,
        "baseModel": "Flux.1 D",
        "trainedWords": [],
        "model": {"type": "LORA"},
    }
    out = promptapi._civitai_summary(resp)
    assert out["air"] == "urn:air:flux1:lora:civitai:123@789"
    assert out["trigger"] is None


def test_settings_roundtrip_never_echoes_key(tmp_path):
    lib = build_library(tmp_path)
    status, body = promptapi.handle_settings(lib, {})
    assert status == 200 and body["civitai_key_set"] is False
    assert body["llm"]["ollama_url"]  # backend URLs round-trip (not secrets)
    status, body = promptapi.handle_save_settings(lib, {"civitai_api_key": "secret-123"})
    assert status == 200 and body["civitai_key_set"] is True
    status, body = promptapi.handle_settings(lib, {})
    assert body["civitai_key_set"] is True  # the key itself never leaves the server
    assert "secret-123" not in json.dumps(body)
    # stored server-side in the user tier
    stored = json.loads((lib.user_root / "settings.json").read_text(encoding="utf-8"))
    assert stored["civitai_api_key"] == "secret-123"
    # empty clears
    status, body = promptapi.handle_save_settings(lib, {"civitai_api_key": ""})
    assert body["civitai_key_set"] is False
    status, _ = promptapi.handle_save_settings(lib, {"civitai_api_key": 42})
    assert status == 400


# -- the glue the section editor actually hits --------------------------------
# _resolve_lora_file + the two handlers wrapped around the (well-tested) pure
# readers. Everything below needs a folder_paths stub; without one the
# handlers stop at their outside-ComfyUI guard, which is all the suite used
# to reach.

INSTALLED = "kits/Hycade.safetensors"


@pytest.fixture()
def installed(tmp_path, monkeypatch):
    """One LoRA installed as 'kits/Hycade.safetensors', carrying a trigger."""
    path = write_st(tmp_path / "hycade.safetensors", {"modelspec.trigger_phrase": "HycadeBodykit"})
    fake_folder_paths(monkeypatch, {INSTALLED: path})
    return path


@pytest.mark.parametrize(
    "asked",
    [
        INSTALLED,  # exact
        "kits\\Hycade.safetensors",  # Windows separators
        "kits/hycade.safetensors",  # lowercased (Linux-authored)
        "KITS\\HYCADE.SAFETENSORS",  # both at once
    ],
)
def test_lora_meta_resolves_slashes_and_case(installed, asked):
    r"""LoRA blocks are authored on one OS and run on another. The tolerant
    lookup is what keeps 'kits\Hycade.safetensors' working on Linux — and it
    must answer with the name ComfyUI reports, not the one that was asked."""
    status, body = promptapi.handle_lora_meta(None, {"name": asked})
    assert status == 200, body
    assert body["name"] == INSTALLED  # the installed casing, always
    assert body["trigger"] == "HycadeBodykit"
    assert body["source"] == "modelspec.trigger_phrase"


def test_lora_meta_unknown_file_is_a_clean_404(installed):
    status, body = promptapi.handle_lora_meta(None, {"name": "kits/other.safetensors"})
    assert status == 404
    assert "not found in your loras folder" in body["error"]
    # tolerance stops at separators/case: a different NAME is still missing
    assert promptapi.handle_lora_meta(None, {"name": "Hycade.safetensors"})[0] == 404


def test_lora_meta_corrupt_header_is_a_400_with_remediation(tmp_path, monkeypatch):
    broken = tmp_path / "broken.safetensors"
    broken.write_bytes(b"\x05\x00")  # truncated length prefix
    fake_folder_paths(monkeypatch, {"broken.safetensors": broken})
    status, body = promptapi.handle_lora_meta(None, {"name": "broken.safetensors"})
    assert status == 400 and "truncated" in body["error"]
    assert "catchword" in body["remediation"]


def test_lora_meta_without_a_trigger_is_a_404_that_says_where_to_look(tmp_path, monkeypatch):
    bare = write_st(tmp_path / "bare.safetensors", {"ss_network_dim": "32"})
    fake_folder_paths(monkeypatch, {"kits/Bare.safetensors": bare})
    status, body = promptapi.handle_lora_meta(None, {"name": "kits/bare.safetensors"})
    assert status == 404
    assert "no trigger word" in body["error"] and "kits/Bare.safetensors" in body["error"]
    assert "ss_tag_frequency" in body["remediation"]


# -- Civitai lookup by hash ---------------------------------------------------

CIVITAI_VERSION = {
    "id": 789,
    "modelId": 123,
    "air": "urn:air:flux1:lora:civitai:123@789",
    "trainedWords": ["HycadeBodykit"],
    "name": "v2.0",
    "model": {"name": "Hycade Bodykit", "type": "LORA"},
}


def test_civitai_lookup_success_reports_the_installed_name(tmp_path, installed, monkeypatch):
    lib = build_library(tmp_path)
    fake_urlopen(monkeypatch, CIVITAI_VERSION)
    status, body = promptapi.handle_lora_civitai(lib, {"name": "kits\\hycade.safetensors"})
    assert status == 200
    assert body["trigger"] == "HycadeBodykit"
    assert body["air"] == "urn:air:flux1:lora:civitai:123@789"
    assert body["model_name"] == "Hycade Bodykit" and body["version_name"] == "v2.0"
    assert body["name"] == INSTALLED  # the caller can heal the item with this


def test_civitai_404_is_not_on_civitai_not_a_network_error(tmp_path, installed, monkeypatch):
    """The one mapping worth freezing: collapsing 404 into 502 sends users
    chasing network problems for a LoRA that simply is not on Civitai."""
    lib = build_library(tmp_path)
    fake_urlopen(monkeypatch, http_error(404))
    status, body = promptapi.handle_lora_civitai(lib, {"name": INSTALLED})
    assert status == 404
    assert "is not on Civitai" in body["error"] and INSTALLED in body["error"]
    assert "hash" in body["error"]  # the digest is shown so it can be searched
    assert "catchword" in body["remediation"]


@pytest.mark.parametrize("code", [401, 403, 500, 503])
def test_civitai_other_http_codes_point_at_the_api_key(tmp_path, installed, monkeypatch, code):
    lib = build_library(tmp_path)
    fake_urlopen(monkeypatch, http_error(code))
    status, body = promptapi.handle_lora_civitai(lib, {"name": INSTALLED})
    assert status == 502
    assert f"HTTP {code}" in body["error"]
    assert "API key" in body["remediation"]


def test_civitai_network_failure_is_a_502(tmp_path, installed, monkeypatch):
    lib = build_library(tmp_path)
    fake_urlopen(monkeypatch, urllib.error.URLError("connection refused"))
    status, body = promptapi.handle_lora_civitai(lib, {"name": INSTALLED})
    assert status == 502
    assert "unreachable" in body["error"] and "network" in body["remediation"]


def test_civitai_unknown_file_never_reaches_the_network(tmp_path, installed, monkeypatch):
    lib = build_library(tmp_path)
    fake_urlopen(monkeypatch, AssertionError("must not call out for an unknown file"))
    status, body = promptapi.handle_lora_civitai(lib, {"name": "kits/ghost.safetensors"})
    assert status == 404 and "not found in your loras folder" in body["error"]


def test_hash_cache_notices_a_changed_file(tmp_path):
    """The digest is what Civitai is keyed by, and it is memoized on
    (mtime, size) — a re-downloaded or re-trained file at the same path must
    not answer with the old model's trigger words."""
    path = tmp_path / "swap.safetensors"
    path.write_bytes(b"first content")
    first = promptapi._sha256_of(path)
    assert promptapi._sha256_of(path) == first  # memoized, same file
    path.write_bytes(b"second, different content")
    assert promptapi._sha256_of(path) != first
