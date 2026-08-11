"""Trigger words from LoRA metadata: pure safetensors-header reading (no
torch) and the lora-meta API handler's no-ComfyUI behavior."""

import json
import struct

import pytest
import support  # noqa: F401

from mrln import promptapi
from mrln.promptlib import read_safetensors_metadata, trigger_from_metadata


def write_st(path, meta):
    header = json.dumps({"__metadata__": meta, "__dummy__": None}).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header)) + header)
    return path


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
