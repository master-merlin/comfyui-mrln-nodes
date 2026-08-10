"""ComfyUI-MRLN-Nodes — entry point read by ComfyUI's custom node loader.

Keep this file a thin re-export shim: all real logic lives in the `mrln`
package so the pack (or a future per-domain split of it) can move between
repos without touching import machinery.
"""

from .mrln.registry import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
