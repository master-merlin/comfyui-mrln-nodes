"""Pack-wide identity and naming helpers.

Every node module derives its node IDs, display names, and menu categories
from these helpers, so renaming the pack — or splitting a domain into its
own pack — is a change in exactly one place.
"""

import logging
import re

PACK_ID = "MRLN"  # node-ID prefix; node IDs are workflow-file API and must never change after release
PACK_MARKER = "(MRLN)"  # display-name suffix so users can tell our nodes apart in search
CATEGORY_ROOT = "MRLN"  # Add-Node menu root

logger = logging.getLogger("MRLN-Nodes")


def node_id(name: str) -> str:
    """Globally unique node ID, e.g. node_id("ImageResize") -> "MRLN_ImageResize"."""
    return f"{PACK_ID}_{name}"


def display(label: str) -> str:
    """Human display name carrying the pack marker, e.g. "Image Resize (MRLN)"."""
    return f"{label} {PACK_MARKER}"


def category(*parts: str) -> str:
    """Add-Node menu path, e.g. category("image") -> "MRLN/image"."""
    return "/".join((CATEGORY_ROOT, *parts))


def build_mappings(nodes: dict):
    """Build (NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS) for one domain module.

    `nodes` maps a bare node name to either a class, or a (class, "Display Label")
    tuple when the auto-spaced class name is not the label you want:

        NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = build_mappings({
            "ImageResize": ImageResize,
            "ImageRGBSplit": (ImageRGBSplit, "Split RGB Channels"),
        })
    """
    class_mappings = {}
    display_mappings = {}
    for name, entry in nodes.items():
        if isinstance(entry, tuple):
            cls, label = entry
        else:
            cls, label = entry, _spaced(name)
        class_mappings[node_id(name)] = cls
        display_mappings[node_id(name)] = display(label)
    return class_mappings, display_mappings


def _spaced(name: str) -> str:
    """Turn "ImageResize" into "Image Resize" (acronym runs stay intact)."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
