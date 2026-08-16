"""Ecosystem layering — the one structural rule every domain shares.

MRLN is one ecosystem of domains over shared pack-level libraries, and the
thing that keeps a future split-by-domain (ARCHITECTURE D6) and the shared
objects (families, entities, instruction grammar, store facades) honest is
that the layers only depend downwards:

    mrln/nodes/<domain>.py   ─▶  mrln/<name>lib/, mrln/promptapi, mrln/pack
    mrln/promptapi           ─▶  mrln/<name>lib/, mrln/pack
    mrln/<name>lib/, mrln/families, mrln/entities  ─▶  mrln/pack, each other

Concretely: a node module never imports another node module (domains talk
through shared libraries, never through each other's node classes), and a
library never reaches up into the node layer or the HTTP layer. Static scan
of import statements — no ComfyUI needed, so it runs in plain CI.
"""

import ast
import pathlib

import support

MRLN = support.ROOT / "mrln"
NODES = MRLN / "nodes"


def _py_files(folder: pathlib.Path):
    return sorted(p for p in folder.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: pathlib.Path):
    """Yield (module_string, level) for every import in the file, with the
    relative form normalised: `from ..promptapi import x` inside mrln/nodes/
    becomes ('mrln.promptapi', ...)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # the module's own package (for __init__.py that is its own directory)
    package_parts = path.relative_to(support.ROOT).with_suffix("").parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package_parts[: len(package_parts) - (node.level - 1)])
                target = ".".join(base + ([node.module] if node.module else []))
                # `from ..promptapi import llm` names a submodule too
                for alias in node.names:
                    yield f"{target}.{alias.name}" if target else alias.name
                yield target
            else:
                yield node.module or ""


def _layer_of(path: pathlib.Path) -> str:
    rel = path.relative_to(MRLN).parts
    if len(rel) == 1:
        return "root"  # pack.py, registry.py, __init__.py — the aggregation point
    if rel[0] == "nodes":
        return "nodes"
    if rel[0] == "promptapi":
        return "api"
    return "lib"


def _forbidden(layer: str, imported: str) -> str | None:
    """Return a reason when `imported` is not allowed from `layer`."""
    if layer == "root":
        return None
    if imported.startswith("mrln.nodes") and layer != "nodes":
        return "reaches up into the node layer"
    if imported.startswith("mrln.nodes") and layer == "nodes":
        return "node module imports another node module (domains share libraries, not nodes)"
    if imported.startswith("mrln.promptapi") and layer == "lib":
        return "library imports the HTTP layer"
    return None


def test_layers_only_depend_downwards():
    offences = []
    for path in _py_files(MRLN):
        layer = _layer_of(path)
        own = ".".join(path.relative_to(support.ROOT).with_suffix("").parts)
        for imported in _imports(path):
            # a node module may name itself or its own package; any other
            # `mrln.nodes.*` target is a cross-domain import
            if layer == "nodes" and imported in (own, "mrln.nodes"):
                continue
            reason = _forbidden(layer, imported)
            if reason:
                offences.append(f"{path.relative_to(support.ROOT)}: imports {imported} — {reason}")
    assert not offences, "\n".join(offences)


def test_every_node_module_is_a_registered_domain(pack):
    """A module under mrln/nodes/ that is not in registry.DOMAINS is dead code
    or an unregistered domain — either way it drifts silently. Register it
    (the registry soft-fails at runtime, and test_registry_domains_all_load
    then guarantees it actually loads)."""
    registry = __import__(f"{support.MODULE_NAME}.mrln.registry", fromlist=["DOMAINS"])
    modules = {p.stem for p in NODES.glob("*.py") if p.stem != "__init__"}
    unregistered = modules - set(registry.DOMAINS)
    assert not unregistered, f"node modules not in DOMAINS: {sorted(unregistered)}"
