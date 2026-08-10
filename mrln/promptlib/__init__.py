"""MRLN prompt engine: two-tier JSON library, deterministic seeded
selection, and format-aware rendering. Pure Python, zero dependencies,
fully importable without ComfyUI.
"""

from .errors import (
    ItemNotFoundError,
    PromptLibError,
    RecursionLimitError,
    RenderError,
    SchemaError,
    SectionNotFoundError,
    SelectionError,
    TemplateNotFoundError,
    UnknownVariableError,
    WildcardSyntaxError,
)
from .library import Library, default_roots, open_library, validate_slug
from .render import FORMATS, Rendered, render
from .resolve import (
    MODES,
    RANDOM_TOKENS,
    ResolvedPrompt,
    ResolvedSlot,
    parse_kv_lines,
    resolve_section,
    resolve_template,
)
from .schema import (
    RenderConfig,
    Section,
    SectionItem,
    Slot,
    Template,
    Variable,
    Variant,
    parse_section,
    parse_template,
)
from .serialize import dump_section, dump_template

__all__ = [
    "FORMATS",
    "MODES",
    "RANDOM_TOKENS",
    "ItemNotFoundError",
    "Library",
    "PromptLibError",
    "RecursionLimitError",
    "RenderConfig",
    "RenderError",
    "Rendered",
    "ResolvedPrompt",
    "ResolvedSlot",
    "SchemaError",
    "Section",
    "SectionItem",
    "SectionNotFoundError",
    "SelectionError",
    "Slot",
    "Template",
    "TemplateNotFoundError",
    "UnknownVariableError",
    "Variable",
    "Variant",
    "WildcardSyntaxError",
    "default_roots",
    "dump_section",
    "dump_template",
    "open_library",
    "parse_kv_lines",
    "parse_section",
    "parse_template",
    "render",
    "resolve_section",
    "resolve_template",
    "validate_slug",
]
