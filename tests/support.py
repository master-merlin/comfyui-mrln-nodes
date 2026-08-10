"""Shared test helpers: load the pack exactly the way ComfyUI's custom-node
loader does (no ComfyUI required) and lint the pack's registration
conventions. Used by the pytest suite and by the harness smoke test.
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_NAME = ROOT.name.replace("-", "_").replace(".", "_")

# Make `import mrln...` work when tests run outside ComfyUI.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_pack():
    """Import the repo root package ComfyUI-style and return the module."""
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def convention_failures(module):
    """Return a list of convention violations (empty = all good)."""
    failures = []
    classes = module.NODE_CLASS_MAPPINGS
    displays = module.NODE_DISPLAY_NAME_MAPPINGS

    if module.WEB_DIRECTORY and not (ROOT / module.WEB_DIRECTORY).is_dir():
        failures.append(f"WEB_DIRECTORY {module.WEB_DIRECTORY!r} does not exist")

    for key, cls in classes.items():
        where = f"{key} ({cls.__name__})"
        if not key.startswith("MRLN_"):
            failures.append(f"{where}: node ID missing MRLN_ prefix")
        if key not in displays:
            failures.append(f"{where}: no display name registered")
        if not str(getattr(cls, "CATEGORY", "")).startswith("MRLN"):
            failures.append(f"{where}: CATEGORY must start with 'MRLN'")
        if not isinstance(getattr(cls, "RETURN_TYPES", None), tuple):
            failures.append(f"{where}: RETURN_TYPES must be a tuple")
        fn = getattr(cls, "FUNCTION", None)
        if fn != "execute":
            failures.append(f"{where}: FUNCTION should be 'execute', got {fn!r}")
        elif not callable(getattr(cls, "execute", None)):
            failures.append(f"{where}: no callable execute() method")
        if not getattr(cls, "DESCRIPTION", "").strip():
            failures.append(f"{where}: missing DESCRIPTION (docstring)")
        if not callable(getattr(cls, "INPUT_TYPES", None)):
            failures.append(f"{where}: missing INPUT_TYPES classmethod")

    failures.extend(
        f"display mapping {key!r} has no class mapping" for key in displays if key not in classes
    )
    return failures
