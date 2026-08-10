"""Lint for shipped example workflows: valid UI-format JSON and every
MRLN node they reference actually exists in the pack."""

import json
from pathlib import Path

import pytest
import support

EXAMPLES = sorted((Path(support.ROOT) / "example_workflows").glob("*.json"))


def test_examples_exist():
    assert EXAMPLES, "example_workflows/ is empty"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_ui_format(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    # UI save format, not the API /prompt format (a dict keyed by node id)
    assert isinstance(data.get("nodes"), list) and data["nodes"], f"{path.name}: no nodes[]"
    assert "links" in data, f"{path.name}: no links[]"
    assert "version" in data, f"{path.name}: no version"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_mrln_nodes_exist(path):
    mappings = support.load_pack().NODE_CLASS_MAPPINGS
    data = json.loads(path.read_text(encoding="utf-8"))
    for node in data["nodes"]:
        node_type = node.get("type", "")
        if node_type.startswith("MRLN_"):
            assert node_type in mappings, f"{path.name}: unknown node '{node_type}'"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_self_contained(path):
    """Examples must not depend on third-party node packs or local models."""
    data = json.loads(path.read_text(encoding="utf-8"))
    core_prefixes = ("MRLN_",)
    core_types = {
        "CLIPTextEncode",
        "KSampler",
        "VAEDecode",
        "SaveImage",
        "PreviewImage",
        "EmptyLatentImage",
        "CheckpointLoaderSimple",
        "Note",
        "MarkdownNote",
        "PrimitiveString",
        "PrimitiveStringMultiline",
        "PreviewAny",
        "Reroute",
    }
    for node in data["nodes"]:
        node_type = node.get("type", "")
        assert node_type.startswith(core_prefixes) or node_type in core_types, (
            f"{path.name}: node '{node_type}' is not MRLN or core ComfyUI — "
            "examples must load without third-party packs"
        )
