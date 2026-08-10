"""Prompt domain: template-driven prompt composition from the two-tier
JSON library (see mrln/promptlib). Nodes are thin runtime anchors — all
logic lives in the engine; combos are rebuilt on every INPUT_TYPES call so
'Refresh node definitions' picks up new library files."""

from inspect import cleandoc

from .. import promptlib as pl
from ..pack import build_mappings, category, logger

EMPTY_SENTINEL = "(library empty — add JSON files and press R to refresh)"
RANDOM_ENTRY = "🎲 random"
FORMAT_OPTIONS = ["template default", *pl.FORMATS]


def _template_options():
    try:
        options = pl.open_library().template_slugs()
    except Exception as exc:  # combo builders must never raise (breaks /object_info)
        logger.warning("MRLN prompt: template listing failed: %s", exc)
        options = []
    return options or [EMPTY_SENTINEL]


def _section_options():
    try:
        lib = pl.open_library()
        options = sorted(set(lib.section_folders()) | set(lib.section_slugs()))
    except Exception as exc:
        logger.warning("MRLN prompt: section listing failed: %s", exc)
        options = []
    return options or [EMPTY_SENTINEL]


def _item_options():
    entries = [RANDOM_ENTRY]
    try:
        lib = pl.open_library()
        for slug in lib.section_slugs():
            try:
                section = lib.load_section(slug)
            except pl.PromptLibError as exc:
                logger.warning("MRLN prompt: skipping unreadable section '%s': %s", slug, exc)
                continue
            entries.extend(f"{slug}/{item.name}" for item in section.items)
    except Exception as exc:
        logger.warning("MRLN prompt: item listing failed: %s", exc)
    return entries


def _fingerprint_or_nan():
    try:
        return pl.open_library().fingerprint()
    except Exception:
        return float("nan")  # fail open: re-run rather than serve stale cache


class PromptTemplate:
    """Render positive + negative prompts from a library template.

    Loads a template from the two-tier prompt library (factory content
    shipped with the pack plus your persistent user library), resolves every
    slot as a fixed item or a seed-deterministic random draw, substitutes
    {variables} and inline {a|b} wildcards, and renders in the template's
    format or an override. The 'choices' output reports exactly what was
    selected or drawn.
    """

    CATEGORY = category("prompt")
    DESCRIPTION = cleandoc(__doc__)
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("prompt", "negative", "choices")
    OUTPUT_TOOLTIPS = (
        "The rendered positive prompt in the chosen format.",
        "The joined negative prompt (template + section + item negatives), always a plain string.",
        "Report of the variant/items chosen per slot with seed and tier — wire to a text "
        "preview to see what was drawn.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "template": (
                    _template_options(),
                    {
                        "tooltip": "Template from the prompt library (factory + user merged; a user "
                        "file with the same slug overrides factory). New files appear "
                        "after 'Refresh node definitions'.",
                    },
                ),
                "selection": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "# one per line, e.g.\n# paint=guards-red\n# location=random\n"
                        "# lighting=random@1392\n# variant=outdoor",
                        "tooltip": "Per-slot overrides, one 'slot=item' per line. 'slot=random' rolls "
                        "the slot with the master seed, 'slot=random@123' with its own "
                        "seed; 'variant=<name|random>' picks the variant branch. Blank "
                        "lines and # comments are ignored; unlisted slots use template "
                        "defaults.",
                    },
                ),
                "selection_mode": (
                    list(pl.MODES),
                    {
                        "tooltip": "Master switch. 'as configured' honors each slot's fixed/random "
                        "mode; 'randomize all' rolls every slot (and the variant); "
                        "'all fixed defaults' pins every slot to its template default.",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "control_after_generate": True,
                        "tooltip": "Master seed for all random slots. Same seed + same library files "
                        "= identical result; each slot draws independently, so fixed "
                        "slots stay constant while random ones vary with the seed. "
                        "Connect the same seed source as your sampler for lockstep.",
                    },
                ),
                "format": (
                    FORMAT_OPTIONS,
                    {
                        "tooltip": "Output format override. 'string' joins everything into one line; "
                        "'string_labeled' emits 'Label: text' lines; 'json' emits one key "
                        "per slot; 'json_flat' wraps the string render as "
                        '{"prompt": ...}. Negative output is always a plain string.',
                    },
                ),
            },
            "optional": {
                "trigger": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Value for the {trigger} variable (e.g. a LoRA trigger word). "
                        "Type it here or connect a STRING output from another node.",
                    },
                ),
                "variables": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "name=value",
                        "tooltip": "Extra template variables, one 'name=value' per line; fills "
                        "{name} placeholders in template and item text.",
                    },
                ),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Library fingerprint only: input values are already part of ComfyUI's
        # cache diff; this re-executes when library JSON files change on disk.
        return _fingerprint_or_nan()

    def execute(self, template, selection, selection_mode, seed, format, trigger="", variables=""):
        if template == EMPTY_SENTINEL:
            raise pl.TemplateNotFoundError(template, [])
        lib = pl.open_library()
        lib.ensure_user_dirs()
        tpl = lib.load_template(template)
        selection_map = pl.parse_kv_lines(selection, what="selection")
        variable_map = pl.parse_kv_lines(variables, what="variables")
        if trigger:
            variable_map["trigger"] = trigger
        resolved = pl.resolve_template(
            lib,
            tpl,
            seed=seed,
            mode=selection_mode,
            selection=selection_map,
            variables=variable_map,
        )
        fmt = tpl.render.format if format == "template default" else format
        out = pl.render(resolved, fmt, tpl.render)
        return (out.positive, out.negative, out.choices)


class PromptSection:
    """One library section as a standalone node: pick an item or roll the dice.

    Outputs the item text and negative for graph-native prompt composition —
    wire several Prompt Section nodes into any text-combine or third-party
    builder node. Folder scopes (e.g. 'location') draw across every section
    beneath them.
    """

    CATEGORY = category("prompt")
    DESCRIPTION = cleandoc(__doc__)
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("text", "negative", "choice")
    OUTPUT_TOOLTIPS = (
        "The selected/drawn item text (inline {a|b} wildcards resolved).",
        "The item's negative plus the section-level negative, as a plain string.",
        "The name of the item that was selected or drawn (empty when omitted).",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "section": (
                    _section_options(),
                    {
                        "tooltip": "Section or folder scope. Folder entries (e.g. 'location') mean "
                        "the union of every section beneath them.",
                    },
                ),
                "item": (
                    _item_options(),
                    {
                        "tooltip": "Item to render, listed as 'section-path/item-name' (searchable — "
                        "type the section name to filter). Must lie inside the chosen "
                        "section scope. '🎲 random' draws from the scope using the seed.",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "control_after_generate": True,
                        "tooltip": "Seed for the 🎲 random draw. Identical section + seed always "
                        "draws the same item; vary the seed for a different draw.",
                    },
                ),
                "allow_empty": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "When rolling 🎲 random, allow 'nothing' as one weighted outcome "
                        "(empty text output) — for optional add-on sections.",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, section=None, item=None):
        # Consuming these two params replaces the default combo check for
        # them: values can validly be stale (workflow older than the library).
        if section in (None, "", EMPTY_SENTINEL):
            return "prompt library is empty — add JSON files to your user library and Refresh"
        if item is None or item in (RANDOM_ENTRY, "random"):
            return True
        try:
            pool = pl.open_library().scope_items(section)
        except pl.PromptLibError as exc:
            return str(exc)
        names = {qualified for qualified, _, _ in pool}
        if item in names or any(item == f"{section}/{qualified}" for qualified in names):
            return True
        return (
            f"item '{item}' is not inside section '{section}' — pick an item under "
            f"{section}/ or {RANDOM_ENTRY}"
        )

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return _fingerprint_or_nan()

    def execute(self, section, item, seed, allow_empty):
        if section == EMPTY_SENTINEL:
            raise pl.SectionNotFoundError(section, [])
        lib = pl.open_library()
        lib.ensure_user_dirs()
        resolved = pl.resolve_section(lib, section, item, seed=seed, allow_empty=allow_empty)
        return (resolved.text, resolved.negative, resolved.item_name or "")


NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = build_mappings(
    {
        "PromptTemplate": PromptTemplate,
        "PromptSection": PromptSection,
    }
)
