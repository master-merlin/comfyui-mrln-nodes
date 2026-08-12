"""The pack must import ComfyUI-style and every registered node must follow
the pack conventions (MRLN_ IDs, MRLN/ categories, execute(), tooltips docs).
"""

import support


def test_pack_exports(pack):
    assert isinstance(pack.NODE_CLASS_MAPPINGS, dict)
    assert isinstance(pack.NODE_DISPLAY_NAME_MAPPINGS, dict)
    assert pack.WEB_DIRECTORY == "./web/js"


def test_conventions(pack):
    failures = support.convention_failures(pack)
    assert not failures, "\n".join(failures)


def test_registry_domains_all_load(pack):
    """Every activated domain must actually contribute; a domain that fails to
    import is silently skipped at runtime (by design), but in CI that's a bug.
    """
    registry = __import__(f"{support.MODULE_NAME}.mrln.registry", fromlist=["DOMAINS"])
    for domain in registry.DOMAINS:
        module_name = f"{support.MODULE_NAME}.mrln.nodes.{domain}"
        module = __import__(module_name, fromlist=["NODE_CLASS_MAPPINGS"])
        assert module.NODE_CLASS_MAPPINGS, f"domain '{domain}' registered no nodes"


def test_frontend_uses_only_public_comfyui_apis():
    """ComfyUI >= 0.32 warns in the console about two things this pack used to
    do, and both are now banned here rather than merely fixed:

      scripts/widgets.js  — an INTERNAL module ("not part of the public API.
        Future updates may break this import."). node.addDOMWidget is the
        documented replacement.
      nodeType.prototype.<hook> = …  — prototype hijacking, which the custom-
        node docs call deprecated in favour of official hooks, and which this
        pack's own CONVENTIONS already forbade while show_text.js did it.

    A user's console filling with deprecation warnings is a support burden the
    pack should never be the cause of.
    """
    import pathlib

    web = pathlib.Path(__file__).resolve().parents[1] / "web"
    banned = {
        "scripts/widgets.js": "internal module — use node.addDOMWidget()",
        "scripts/ui.js": "deprecated legacy API",
        "components/buttonGroup.js": "deprecated legacy API",
        "extensions/core/clipspace.js": "deprecated legacy API",
        "extensions/core/groupNode.js": "deprecated legacy API",
        "extensions/core/widgetInputs.js": "internal module",
    }
    offences = []
    for path in web.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            code = line.split("//")[0]
            if "import" in code:
                for needle, why in banned.items():
                    if needle in code:
                        offences.append(f"{path.name}: imports {needle} ({why})")
            if ".prototype." in code and "=" in code:
                offences.append(f"{path.name}: patches a prototype — {line.strip()[:60]}")
    assert not offences, "frontend uses a deprecated/internal ComfyUI API:\n  " + "\n  ".join(
        offences
    )
