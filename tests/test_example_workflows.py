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


# An example that needs downloaded models is allowed — but it has to be named
# here, and it has to SAY what it needs. The promise the rule protects is that
# a fresh install always has at least one graph that opens and runs.
MODEL_DEPENDENT = {
    "mrln-prompting-krea2-turbo.json": ("Krea-2 Turbo", "loras"),
}


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_self_contained(path):
    """Examples must not depend on third-party node packs, and at least the
    starter must not depend on downloaded models either."""
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
        # shipped by the frontend itself, like the ones above
        "ResolutionSelector",
        "ComfySwitchNode",
    }
    # a subgraph instance's `type` is the subgraph's uuid, and its definition
    # travels inside the same file — nothing third-party about it
    defined = {sub.get("id") for sub in (data.get("definitions") or {}).get("subgraphs", [])}
    for node in data["nodes"]:
        node_type = node.get("type", "")
        ok = node_type.startswith(core_prefixes) or node_type in core_types or node_type in defined
        assert ok, (
            f"{path.name}: node '{node_type}' is not MRLN, core ComfyUI or a "
            "subgraph defined in this file — examples must load without "
            "third-party packs"
        )


def test_the_starter_example_needs_no_downloaded_model():
    """One graph must always open and run on a fresh install. Every other
    example may need models, and must name them in a note."""
    starter = Path(support.ROOT) / "example_workflows" / "mrln-prompting.json"
    assert starter.is_file(), "the model-free starter example is gone"
    assert starter.name not in MODEL_DEPENDENT
    data = json.loads(starter.read_text(encoding="utf-8"))
    loaders = {"CheckpointLoaderSimple", "LoraLoaderModelOnly", "UNETLoader", "VAELoader"}
    used = {node.get("type") for node in data["nodes"]}
    assert not (used & loaders), (
        f"the starter loads a model ({used & loaders}) — it must run with nothing downloaded"
    )


@pytest.mark.parametrize("name,needles", sorted(MODEL_DEPENDENT.items()))
def test_a_model_dependent_example_says_what_it_needs(name, needles):
    path = Path(support.ROOT) / "example_workflows" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    notes = " ".join(
        str(value)
        for node in data["nodes"]
        if node.get("type") in ("Note", "MarkdownNote")
        for value in (node.get("widgets_values") or [])
    )
    for needle in needles:
        assert needle.lower() in notes.lower(), (
            f"{name}: the notes never mention '{needle}' — a user who opens this "
            "has to be told what to download"
        )
