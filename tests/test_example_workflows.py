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

# Third-party nodes an example is ALLOWED to use. Empty, and worth keeping that
# way: the Krea-2 example carried two (KJNodes' Krea2PromptWeight, Impact Pack's
# ImpactConcatConditionings) inside its subgraph until they were removed, and
# the concat node's output had never been wired to anything at all. Adding a key
# here is a decision that also obliges the workflow's own notes to name the pack
# — see test_a_declared_dependency_is_named_in_the_notes.
# workflow file -> {node type: the pack name a user has to install}
DECLARED_THIRD_PARTY = {}


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
        # core loaders and conditioning — only reachable now that this rule
        # walks inside subgraphs, which is where a real pipeline puts them
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "LoraLoaderModelOnly",
        "ConditioningZeroOut",
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
    subgraphs = (data.get("definitions") or {}).get("subgraphs", [])
    defined = {sub.get("id") for sub in subgraphs}
    # Walk INSIDE the subgraphs too. This rule used to check only the top
    # level, and the Krea-2 example quietly carried two custom-pack nodes in
    # its subgraph for exactly that reason: a graph whose pipeline lives in a
    # subgraph is where a dependency is easiest to miss, not hardest.
    every_node = list(data["nodes"])
    for sub in subgraphs:
        every_node.extend(sub.get("nodes") or [])
    for node in every_node:
        node_type = node.get("type", "")
        ok = (
            node_type.startswith(core_prefixes)
            or node_type in core_types
            or node_type in defined
            or node_type in DECLARED_THIRD_PARTY.get(path.name, {})
        )
        assert ok, (
            f"{path.name}: node '{node_type}' is not MRLN, core ComfyUI or a "
            "subgraph defined in this file — examples must load without "
            "third-party packs, and a deliberate exception has to be declared "
            "in DECLARED_THIRD_PARTY and named in the workflow's own notes"
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


@pytest.mark.parametrize("name", sorted(DECLARED_THIRD_PARTY))
def test_a_declared_dependency_is_named_in_the_notes(name):
    """Declaring a third-party node in the table above is not enough — the
    workflow has to tell the user which pack to install, in the workflow."""
    path = Path(support.ROOT) / "example_workflows" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    notes = " ".join(
        str(value)
        for node in data["nodes"]
        if node.get("type") in ("Note", "MarkdownNote")
        for value in (node.get("widgets_values") or [])
    ).lower()
    for node_type, pack in DECLARED_THIRD_PARTY[name].items():
        assert pack.lower() in notes, (
            f"{name}: uses {node_type} from {pack} but never says so — a user "
            "who opens this sees a red node and no idea which pack is missing"
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
