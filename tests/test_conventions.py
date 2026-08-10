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
