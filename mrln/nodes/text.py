"""Text domain: small text utilities. First node: Show Text — display any
value inside the node (and pass it through), so no third-party pack is
needed to inspect prompt/choices outputs. Server-side first: the text is
delivered via the standard OUTPUT_NODE "ui" channel and works headless;
the widget display in the graph is a progressive enhancement in web/js."""

import json
from inspect import cleandoc

from ..pack import build_mappings, category


def _as_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


class ShowText:
    """Display any input as text inside the node.

    Connect any output — strings show as-is, everything else is
    stringified (dicts/lists as pretty JSON). The value also passes
    through as a STRING output, so the node can sit inline in a chain.
    """

    CATEGORY = category("text")
    DESCRIPTION = cleandoc(__doc__)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    OUTPUT_TOOLTIPS = ("The displayed value as a plain string (passthrough).",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "value": (
                    "*",
                    {
                        "forceInput": True,
                        "tooltip": "Any value to display — strings show as-is, other "
                        "types are stringified (dicts/lists as pretty JSON).",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, input_types):
        # Consuming input_types opts out of backend type matching for the
        # wildcard input — any upstream type is accepted.
        return True

    def execute(self, value):
        text = _as_text(value)
        return {"ui": {"text": [text]}, "result": (text,)}


NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = build_mappings(
    {
        "ShowText": ShowText,
    }
)
