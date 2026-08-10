"""ComfyUI-MRLN-Nodes — entry point read by ComfyUI's custom node loader.

Keep this file a thin re-export shim: all real logic lives in the `mrln`
package so the pack (or a future per-domain split of it) can move between
repos without touching import machinery.
"""

try:
    from .mrln.registry import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
except ImportError:
    # Outside ComfyUI's loader there is no parent package for the relative
    # import (pytest importing the repo-root __init__.py, REPL experiments).
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from mrln.registry import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
