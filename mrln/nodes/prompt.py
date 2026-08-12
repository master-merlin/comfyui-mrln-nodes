"""Prompt domain: template-driven prompt composition from the two-tier
JSON library (see mrln/promptlib). Nodes are thin runtime anchors — all
logic lives in the engine; combos are rebuilt on every INPUT_TYPES call so
'Refresh node definitions' picks up new library files."""

import hashlib
import json
from collections import OrderedDict
from inspect import cleandoc

from .. import promptlib as pl
from ..pack import build_mappings, category, logger

# The engine's own selection-token parser. VALIDATE_INPUTS runs the very
# function resolve_section runs so the pre-queue gate can never be stricter
# (or looser) than the engine it fronts — one parser, no mirrored regex.
from ..promptlib.resolve import _parse_token as _parse_selection_token

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


def _profile_options():
    try:
        lib = pl.open_library()
        names = set(lib.pack_profiles())
        for slug in lib.template_slugs():
            try:
                names.update(lib.load_template(slug).profiles)
            except pl.PromptLibError:
                continue  # one broken template must not hide the combo
        return [pl.STANDARD, *sorted(names)]
    except Exception as exc:
        logger.warning("MRLN prompt: profile listing failed: %s", exc)
        return [pl.STANDARD]


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
            entries.extend(f"{slug}/{item.name}" for item in section.items if not item.hidden)
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
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("prompt", "negative", "choices", "loras", "llm")
    OUTPUT_TOOLTIPS = (
        "The rendered positive prompt in the chosen format.",
        "The joined negative prompt (template + section + item negatives), always a plain string.",
        "Report of the variant/items chosen per slot with seed and tier — wire to a text "
        "preview to see what was drawn.",
        "JSON list of the drawn LoRA blocks (file + strengths) — wire into the "
        "'LoRA Apply (MRLN)' node between your model/clip loaders and the sampler.",
        "The 'Prompt Enhance (MRLN)' single wire: {target, prompt, protect, system, params} "
        "— the rendered prompt, the profile's LLM system prompt, and the LoRA trigger "
        "words the enhancer must keep verbatim.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "template": (
                    _template_options(),
                    {
                        "tooltip": "Template from the prompt library (factory + user merged; "
                        "a user file with the same slug overrides factory). New files "
                        "appear after 'Refresh node definitions'.",
                    },
                ),
                "trigger": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Value for the {trigger} variable — usually your LoRA "
                        "trigger word or the subject line (e.g. 'BMWM4CS_G82'). Type it "
                        "here or connect a STRING output from another node.",
                    },
                ),
                "selection": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "# one per line, e.g.\n# paint=guards-red\n"
                        "# location=random\n# lighting=random@1392\n# variant=outdoor",
                        "tooltip": "Per-slot overrides, one 'slot=item' per line. 'slot=random' "
                        "rolls the slot with the master seed, 'slot=random@123' with its "
                        "own seed; 'slot=off' mutes the slot entirely; "
                        "'variant=<name|random|off>' picks or mutes the variant branch. "
                        "Blank lines and # comments are ignored; unlisted slots use "
                        "template defaults.",
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
                        "tooltip": "Master seed for all random slots. Same seed + same library "
                        "files = identical result; each slot draws independently, so "
                        "fixed slots stay constant while random ones vary with the seed. "
                        "Connect the same seed source as your sampler for lockstep.",
                    },
                ),
                "format": (
                    FORMAT_OPTIONS,
                    {
                        "tooltip": "Output format override. 'string' joins everything into one "
                        "line; 'string_labeled' emits 'Label: text' lines; 'json' emits "
                        "one key per slot; 'json_flat' wraps the string render as "
                        '{"prompt": ...}. Negative output is always a plain string.',
                    },
                ),
                "conflict_policy": (
                    list(pl.CONFLICT_POLICIES),
                    {
                        "tooltip": "When a negative term also appears in the rendered prompt: "
                        "'negative prevails' keeps it in the negative output, 'positive "
                        "prevails' drops it (a drawn section explicitly wants the term). "
                        "Conflicts are always listed in the choices report.",
                    },
                ),
                "text_length": (
                    ["template default", *pl.TEXT_LENGTHS],
                    {
                        "tooltip": "Which item texts render: 'long' full descriptions, 'short' "
                        "compact variants for tight tokenizers (e.g. SDXL). Items without "
                        "a short text fall back to their long text. Draws are identical "
                        "either way.",
                    },
                ),
            },
            "optional": {
                "variables": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "placeholder": "# extra template variables, one per line, e.g.\n"
                        "# plate=MRLN 500   → fills {plate} in overdrive/full-shot\n"
                        "# caption=RIVIERA  → fills {caption} in poster/travel",
                        "tooltip": "Extra template variables as 'name=value' lines. Each line "
                        "fills the matching {name} placeholder in the template's prefix/"
                        "suffix and item texts — e.g. 'plate=MRLN 500' sets the license "
                        "plate caption {plate} in overdrive/full-shot. The template's "
                        "Composer view lists which variables it declares.",
                    },
                ),
                "profile": (
                    _profile_options(),
                    {
                        "default": pl.STANDARD,
                        "tooltip": "Target-model profile: applies that profile's render "
                        "overrides (format/text length) and emits its LLM system prompt "
                        "on the llm output. 'standard' = the template's plain render. "
                        "Profiles come from profiles.json (factory + user tier) extended "
                        "by the template's own; explicit format/text_length widget "
                        "choices still win.",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, template=None, selection=None, profile=None):
        # Pre-queue check with execute()-quality messages: catches selection
        # lines left over from a different template (switching the combo on
        # the node) before the graph runs. Consuming 'template' replaces the
        # default combo check; load_template gives the better message anyway.
        if template in (None, "", EMPTY_SENTINEL) or selection is None:
            return True
        try:
            lib = pl.open_library()
            tpl = lib.load_template(template)
            selection_map = pl.parse_kv_lines(selection, what="selection")
        except pl.PromptLibError as exc:
            return str(exc)
        known = {slot.id for slot in tpl.slots}
        known.update(slot.id for variant in tpl.variants for slot in variant.slots)
        known.add("variant")
        # nested keys ('scene.subject-a') validate their head here; the rest
        # depends on drawn items and stays with resolve
        unknown = sorted(key for key in selection_map if key.split(".", 1)[0] not in known)
        if unknown:
            return (
                f"selection references unknown slot(s) {', '.join(unknown)} for template "
                f"'{template}' — remove those lines or re-apply from the Composer panel"
            )
        if profile not in (None, "", pl.STANDARD):
            names = pl.merged_profiles(lib, tpl)
            if profile not in names:
                return (
                    f"unknown profile '{profile}' — have: "
                    f"{', '.join([pl.STANDARD, *sorted(names)])}"
                )
        return True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Library fingerprint only: input values are already part of ComfyUI's
        # cache diff; this re-executes when library JSON files change on disk.
        return _fingerprint_or_nan()

    def execute(
        self,
        template,
        selection,
        selection_mode,
        seed,
        format,
        conflict_policy="negative prevails",
        text_length="template default",
        trigger="",
        variables="",
        profile=pl.STANDARD,
    ):
        if template == EMPTY_SENTINEL:
            raise pl.TemplateNotFoundError(template, [])
        lib = pl.open_library()
        lib.ensure_user_dirs()
        tpl = lib.load_template(template)
        selection_map = pl.parse_kv_lines(selection, what="selection")
        variable_map = pl.parse_kv_lines(variables, what="variables")
        if trigger:
            variable_map["trigger"] = trigger
        composed = pl.compose(
            lib,
            tpl,
            seed=seed,
            mode=selection_mode,
            selection=selection_map,
            variables=variable_map,
            profile=profile,
            format=format,
            text_length=text_length,
            conflict_policy=conflict_policy,
        )
        out = composed.rendered
        loras = json.dumps(pl.lora_entries(composed.resolved), ensure_ascii=False)
        return (out.positive, out.negative, out.choices, loras, composed.llm)


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
                        "tooltip": "Item to render, listed as 'section-path/item-name' "
                        "(searchable — type the section name to filter). Must lie inside "
                        "the chosen scope. '🎲 random' draws from it using the seed.",
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
                        "tooltip": "When rolling 🎲 random, allow 'nothing' as one weighted "
                        "outcome (empty text output) — for optional add-on sections.",
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
        if item is None:
            return True
        # Control tokens are the ENGINE's grammar, not the dropdown's: an
        # API-submitted workflow may carry 'off'/'🔇 off' (mute) or
        # 'random@<seed>'/'🎲 random@<seed>' (own-seed roll), all of which
        # resolve_section handles. Ask the engine's parser instead of
        # guessing, so a malformed seed is refused here with the engine's
        # own message rather than blowing up mid-queue.
        try:
            kind, value = _parse_selection_token(item, item)
        except pl.PromptLibError as exc:
            return str(exc)
        if kind != "fixed":
            return True
        try:
            pool = pl.open_library().scope_items(section)
        except pl.PromptLibError as exc:
            return str(exc)
        names = {qualified for qualified, _, _ in pool}
        if value in names or any(value == f"{section}/{qualified}" for qualified in names):
            return True
        return (
            f"item '{value}' is not inside section '{section}' — pick an item under "
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


def parse_loras_json(loras):
    """'loras' JSON -> validated [(name, strength_model, strength_clip, air,
    base)]. Pure so pytest covers it; raises ValueError with remediation
    text. `air` and `base` are "" when the block carries none."""
    if not isinstance(loras, str) or not loras.strip():
        return []
    try:
        entries = json.loads(loras)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"'loras' is not valid JSON ({exc}) — wire it from the Prompt Template "
            "node's loras output"
        ) from None
    if not isinstance(entries, list):
        raise ValueError("'loras' must be a JSON list of {lora, strength_model, strength_clip}")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("lora"):
            raise ValueError(f"lora entry {entry!r} is missing the 'lora' file name")
        sm = float(entry.get("strength_model", 1.0))
        sc = float(entry.get("strength_clip", sm))
        result.append(
            (
                str(entry["lora"]),
                sm,
                sc,
                str(entry.get("air") or ""),
                str(entry.get("base") or "").lower(),
            )
        )
    return result


# A loaded model reports its architecture through comfy.model_base; map the
# class names onto the AIR ecosystem slugs LoRA items declare. Only families
# whose LoRAs are genuinely incompatible need an entry — anything unmapped
# simply skips the check rather than crying wolf.
_MODEL_FAMILIES = (
    ("flux", "flux1"),
    ("sd3", "sd3"),
    ("sdxl", "sdxl"),
    ("stablediffusionxl", "sdxl"),
    ("qwenimage", "qwen"),
    ("wan", "wan"),
    ("hunyuanvideo", "hunyuan"),
    ("hunyuandit", "hunyuan"),
    ("ltxv", "ltxv"),
    ("auraflow", "auraflow"),
    ("hidream", "hidream"),
    ("cascade", "cascade"),
    ("sd15", "sd1"),
    ("sd1", "sd1"),
)

# families that share a LoRA format closely enough that a mismatch is noise
_FAMILY_ALIASES = {"pony": "sdxl", "illustrious": "sdxl", "noobai": "sdxl", "sd2": "sd1"}


def _canonical_family(name):
    name = str(name or "").strip().lower()
    return _FAMILY_ALIASES.get(name, name)


def model_family(model):
    """Best-effort base-model family slug for a loaded MODEL, or "" when it
    cannot be determined. Never raises: a wrong guess must not break a run,
    so an unknown architecture simply disables the compatibility check."""
    names = []
    try:
        inner = getattr(model, "model", None)
        if inner is not None:
            names.append(type(inner).__name__)
            config = getattr(inner, "model_config", None)
            if config is not None:
                names.append(type(config).__name__)
        names.append(type(model).__name__)
    except Exception:
        return ""
    for raw in names:
        flat = "".join(ch for ch in str(raw).lower() if ch.isalnum())
        for needle, family in _MODEL_FAMILIES:
            if needle in flat:
                return family
    return ""


class LoraApply:
    """Apply the LoRA blocks a Prompt Template drew onto MODEL and CLIP.

    Wire the Prompt Template's 'loras' output into this node between your
    model/clip loaders and the sampler: every drawn LoRA block (a section
    item carrying lora + strength metadata) is loaded with its authored
    strengths — the deterministic draw decides the LoRA stack, no
    tag-parsing loader needed. With no LoRA blocks drawn, model and clip
    pass through unchanged.
    """

    CATEGORY = category("prompt")
    DESCRIPTION = cleandoc(__doc__)
    FUNCTION = "execute"
    RETURN_TYPES = ("MODEL", "CLIP", "STRING")
    RETURN_NAMES = ("model", "clip", "report")
    OUTPUT_TOOLTIPS = (
        "The model with every drawn LoRA applied at its authored strength.",
        "The CLIP with every drawn LoRA applied at its authored strength.",
        "What actually happened, line by line: each LoRA applied with its "
        "strengths, plus any base-model mismatch, missing file, skip or "
        "download. Wire it into Show Text (MRLN) to see it in the graph — "
        "silent degradation is the failure mode this catches.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "Model to apply the drawn LoRA stack to."}),
                "clip": ("CLIP", {"tooltip": "CLIP to apply the drawn LoRA stack to."}),
                "loras": (
                    "STRING",
                    {
                        "forceInput": True,
                        "tooltip": "The 'loras' output of a Prompt Template (MRLN) node — "
                        "a JSON list of the drawn LoRA blocks.",
                    },
                ),
            },
            "optional": {
                "on_missing": (
                    ["error", "skip", "download"],
                    {
                        "default": "error",
                        "tooltip": "A drawn LoRA file this machine does not have: "
                        "'error' stops the run and names the file (safe default); "
                        "'skip' renders without it and logs a warning — the trigger "
                        "words stay in the prompt, so the image just loses that "
                        "LoRA's influence; 'download' fetches it from Civitai by the "
                        "AIR urn stored on the item (SHA256-verified, blocks the run "
                        "for as long as the download takes) and re-points the item at "
                        "the file. Use 'download' for shared workflows that should "
                        "heal themselves without opening the Composer.",
                    },
                ),
                "on_mismatch": (
                    ["warn", "skip", "error", "ignore"],
                    {
                        "default": "warn",
                        "tooltip": "A LoRA trained for a different base model than the "
                        "connected one (a FLUX LoRA on an SDXL or KREA checkpoint) "
                        "loads without erroring but quietly degrades the image. "
                        "'warn' logs the mismatch and applies it anyway; 'skip' leaves "
                        "that LoRA out; 'error' stops the run; 'ignore' disables the "
                        "check. Only LoRA items that declare a family (data.base, or "
                        "the ecosystem segment of their Civitai AIR) can be checked.",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, loras=None):
        if loras is None:
            return True
        try:
            parse_loras_json(loras)
        except ValueError as exc:
            return str(exc)
        return True

    def execute(self, model, clip, loras, on_missing="error", on_mismatch="warn"):
        entries = parse_loras_json(loras)
        report = []
        if not entries:
            return (model, clip, "no LoRA blocks drawn — model and clip pass through")
        import comfy.sd  # ComfyUI runtime only — keeps the pack importable anywhere
        import comfy.utils
        import folder_paths

        def lookup(name):
            available = folder_paths.get_filename_list("loras")
            normalized = {n.replace("\\", "/").lower(): n for n in available}
            return (
                name if name in available else normalized.get(name.replace("\\", "/").lower())
            ), available

        target = "" if on_mismatch == "ignore" else _canonical_family(model_family(model))
        for name, strength_model, strength_clip, air, base in entries:
            # a LoRA for another architecture loads without complaint and just
            # degrades the image — the one failure mode nothing else reports
            declared = _canonical_family(base)
            if target and declared and declared != target:
                note = (
                    f"LoRA '{name}' was trained for {declared}, but the connected "
                    f"model is {target} — results will be poor"
                )
                if on_mismatch == "error":
                    raise ValueError(
                        f"{note}. Pick a {target} LoRA, or set on_mismatch to "
                        "'warn'/'skip' on the LoRA Apply (MRLN) node."
                    )
                if on_mismatch == "skip":
                    logger.warning("MRLN LoRA Apply: %s — skipped", note)
                    report.append(f"⚠ SKIPPED {name} — trained for {declared}, model is {target}")
                    continue
                logger.warning("MRLN LoRA Apply: %s (applied anyway)", note)
                report.append(
                    f"⚠ MISMATCH {name} — trained for {declared}, model is {target}; applied anyway"
                )
            real, available = lookup(name)
            if real is None and on_missing == "download" and air:
                # the workflow can heal itself without the Composer: fetch by
                # the AIR the item carries, verified, then look the file up again
                from .. import promptapi

                logger.info("MRLN LoRA Apply: fetching missing '%s' from %s", name, air)
                fetched = promptapi.download_lora_by_air(
                    pl.open_library(), air, filename=name.replace("\\", "/").rsplit("/", 1)[-1]
                )
                report.append(f"⬇ DOWNLOADED {fetched} from {air}")
                real, available = lookup(fetched)
            if real is None:
                if on_missing == "skip":
                    logger.warning(
                        "MRLN LoRA Apply: '%s' not installed — skipped (trigger words "
                        "remain in the prompt, the LoRA's influence does not)",
                        name,
                    )
                    report.append(
                        f"⚠ MISSING {name} — skipped; its trigger words are still in "
                        "the prompt, its influence is not"
                    )
                    continue
                hint = (
                    f" Its Civitai AIR is {air} — set this node's on_missing to "
                    "'download' to fetch it automatically, or use the Composer's "
                    "one-click download."
                    if air
                    else " Fix the LoRA block in the Composer (Library tab) or install the file."
                )
                raise FileNotFoundError(
                    f"LoRA '{name}' not found in your loras folder "
                    f"({len(available)} files available).{hint}"
                )
            path = folder_paths.get_full_path("loras", real)
            lora_sd = comfy.utils.load_torch_file(path, safe_load=True)
            model, clip = comfy.sd.load_lora_for_models(
                model, clip, lora_sd, strength_model, strength_clip
            )
            logger.info(
                "MRLN LoRA Apply: %s (model %.2f / clip %.2f)", real, strength_model, strength_clip
            )
            report.append(
                f"✓ {real} (model {strength_model:g} / clip {strength_clip:g})"
                + (f" · {declared}" if declared else "")
            )
        applied = sum(1 for line in report if line.startswith("✓"))
        issues = sum(1 for line in report if line.startswith("⚠"))
        head = f"{applied} of {len(entries)} LoRA(s) applied"
        head += f" onto {target}" if target else ""
        head += f" · {issues} issue(s)" if issues else ""
        return (model, clip, "\n".join([head, *report]))


def parse_llm_spec(llm):
    """'llm' JSON from the Template node -> dict ({} for empty/standard)."""
    if not isinstance(llm, str) or not llm.strip():
        return {}
    try:
        data = json.loads(llm)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"'llm' is not valid JSON ({exc}) — wire it from the Prompt Template node's llm output"
        ) from None
    if not isinstance(data, dict):
        raise ValueError("'llm' must be a JSON object")
    return data


def _effective_max_tokens(prompt, max_tokens):
    """A keep-everything rewrite cannot be shorter than its input — raise a
    too-small generation cap so the backend never truncates silently (that
    reads as 'the enhancer ate my prompt'). Words -> tokens with headroom."""
    floor = int(len(prompt.split()) * 1.8) + 64
    return max(max_tokens, min(floor, 8192))


def _enforce_protected(text, protect):
    """(repaired_text, missing) — every protected span must survive the
    rewrite EXACTLY; spans the LLM dropped or mutated are re-appended so a
    LoRA's activation can never be enhanced away."""
    missing = [p for p in protect if p and p not in text]
    if not missing:
        return text, []
    base = text.rstrip()
    sep = " " if base.endswith((".", "!", "?")) else ", "
    if not base:
        sep = ""
    return base + sep + ", ".join(missing), missing


def _strip_thinking(text):
    """Remove <think>…</think> blocks that reasoning models prepend."""
    import re as _re

    return _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)


# (backend, model, system, prompt, seed, temp, max_tokens) -> text. Bounded
# LRU: the key embeds the FULL system text and prompt and every entry holds a
# full rewrite, while control_after_generate rolls the seed per queue — an
# unbounded dict would grow into an append-only log of multi-KB triples for
# the server's lifetime. The stated purpose (re-queues never re-call) only
# ever needs the recent tail.
_ENHANCE_CACHE = OrderedDict()
_ENHANCE_CACHE_MAX = 128

# When no profile system prompt rides the wire ('standard' profile, or a bare
# prompt input), the enhancer still works under this generic contract —
# model-agnostic, so it only carries the rules every target shares.
_GENERIC_SYSTEM = (
    "You refine image-generation prompts. Rewrite the input for clarity and "
    "flow while keeping its nature: a tag list stays a tag list, prose stays "
    "prose. FIDELITY: every subject, element, color, material and light the "
    "input names must appear in your rewrite, and you may add NONE it does "
    "not name — no new objects, no palette shifts, no generic embellishments. "
    "STYLE LOCK: the medium and art style the input states are facts — keep "
    "their exact words and never shift the prompt toward a different medium "
    "or realism level; if the input names no medium, do not introduce one. "
    "Keep trigger words and (weighted:1.2) spans verbatim. NEVER summarize: "
    "your rewrite carries at least the input's level of detail. Answer with "
    "the rewritten prompt only — no preamble, no quotes, no explanations."
)


class PromptEnhance:
    """Rewrite a prompt with an LLM under a target-model system prompt.

    ONE wire does it: the Prompt Template node's llm output carries the
    rendered prompt plus the selected profile's system prompt, which tells
    the LLM how the TARGET image model wants its prompts (prose for
    KREA/FLUX, tags for SDXL/Pony, ...). The optional prompt input enhances
    any other STRING instead and wins when both are wired. Backends: local
    Ollama / LM Studio (URLs in the Composer's Settings tab) or cloud
    Anthropic / OpenAI / Gemini / OpenRouter (API keys stored there,
    server-side only — never in widgets). Deterministic per seed where the
    backend supports it, cached per input so re-queues never re-call, and a
    failing backend passes the original prompt through instead of killing
    the render (switchable). Ollama frees its VRAM right after the call by
    default so the sampler gets the GPU back. LoRA trigger words riding the
    llm wire are PROTECTED: they are demanded verbatim in the system prompt
    and verified after the rewrite — any span the LLM dropped or mutated is
    re-injected, so an enhancement can never disarm a LoRA.
    """

    CATEGORY = category("prompt")
    DESCRIPTION = cleandoc(__doc__)
    FUNCTION = "execute"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("prompt", "report")
    OUTPUT_TOOLTIPS = (
        "The enhanced prompt (or the original on pass-through).",
        "What happened: backend, model, seed, cache/VRAM state, or the pass-through reason.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "backend": (
                    ["ollama", "lm studio", "anthropic", "openai", "gemini", "openrouter"],
                    {
                        "tooltip": "LLM backend. Local: Ollama / LM Studio (URLs in the "
                        "Composer's Settings tab). Cloud backends need an API key "
                        "stored there — in the browser only keyed ones are listed.",
                    },
                ),
                "model": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Model name, e.g. 'gemma3:12b' (Ollama) or an LM Studio "
                        "model id. Required for Ollama; LM Studio falls back to its "
                        "loaded model; cloud backends fall back to a sensible default. "
                        "In the browser this becomes a dropdown of installed models "
                        "plus pull suggestions Ollama downloads on pick.",
                    },
                ),
                "temperature": (
                    "FLOAT",
                    {
                        "default": 0.2,
                        "min": 0.0,
                        "max": 2.0,
                        "step": 0.05,
                        "tooltip": "Sampling temperature — keep low for faithful rewrites.",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "tooltip": "LLM seed for reproducible rewrites (where the backend "
                        "supports it). 0 derives a stable seed from the prompt + system "
                        "text, so identical inputs enhance identically.",
                    },
                ),
                "max_tokens": (
                    "INT",
                    {
                        "default": 512,
                        "min": 16,
                        "max": 8192,
                        "tooltip": "Generation cap for the rewrite. Auto-raised when the "
                        "input is longer than the cap allows — a keep-everything "
                        "rewrite can never be shorter than its input (the report "
                        "notes when this happens).",
                    },
                ),
                "timeout": (
                    "INT",
                    {
                        "default": 60,
                        "min": 5,
                        "max": 600,
                        "tooltip": "Seconds to wait for the backend before giving up.",
                    },
                ),
                "free_vram": (
                    ["after call", "keep 5m", "always keep"],
                    {
                        "tooltip": "Ollama keep_alive: 'after call' unloads the LLM "
                        "immediately so the diffusion model gets the VRAM back "
                        "(recommended on one GPU); 'keep 5m' keeps it warm for rapid "
                        "iteration; 'always keep' pins it loaded until Ollama stops "
                        "(second GPU / big VRAM). LM Studio manages its own lifetime.",
                    },
                ),
                "on_error": (
                    ["pass through", "raise"],
                    {
                        "tooltip": "When the backend is unreachable or errors: pass the "
                        "ORIGINAL prompt through (render never dies, report says why) "
                        "or raise and stop the queue.",
                    },
                ),
            },
            "optional": {
                "llm": (
                    "STRING",
                    {
                        "forceInput": True,
                        "tooltip": "The Prompt Template node's llm output — the single "
                        "wire: {target, prompt, protect, system, params}. It carries "
                        "the rendered prompt, the profile's system prompt, and the "
                        "LoRA trigger words that are enforced verbatim (dropped ones "
                        "are re-injected).",
                    },
                ),
                "prompt": (
                    "STRING",
                    {
                        "forceInput": True,
                        "tooltip": "Optional override: enhance this STRING instead of "
                        "the prompt carried inside the llm input (wins when both are "
                        "wired). Use it to enhance text from any other node.",
                    },
                ),
                "system": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": "System prompt override. Empty = use the llm input's "
                        "system prompt; set both and this one wins (template guides, "
                        "user decides).",
                    },
                ),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, llm=None):
        if llm in (None, ""):
            return True
        try:
            parse_llm_spec(llm)
        except ValueError as exc:
            return str(exc)
        return True

    def execute(
        self,
        backend,
        model,
        temperature,
        seed,
        max_tokens,
        timeout,
        free_vram,
        on_error,
        llm="",
        prompt="",
        system="",
    ):
        spec = parse_llm_spec(llm)
        # explicit prompt input wins; otherwise the llm wire carries it
        prompt = str(prompt).strip() or str(spec.get("prompt") or "")
        if not prompt.strip():
            return (
                "",
                "pass-through: nothing to enhance — wire the Prompt Template node's "
                "llm output (it carries the prompt), or the prompt input",
            )
        system_text = system.strip() or str(spec.get("system") or "").strip()
        # 'standard' (and any profile without an llm block) used to silently
        # pass through here — the enhancer must always work, so a generic
        # fidelity-contract system prompt fills the gap.
        generic = not system_text
        if generic:
            system_text = _GENERIC_SYSTEM
        # LoRA trigger words must survive the rewrite EXACTLY — most LLMs
        # happily "improve" them, which silently kills the LoRA. Spans come
        # from the llm wire; only those actually present in the source count
        # (an overridden prompt may not contain them).
        raw_protect = spec.get("protect")
        protect = []
        if isinstance(raw_protect, list):
            protect = [str(p).strip() for p in raw_protect if str(p).strip()]
            protect = [p for p in protect if p in prompt]
        if protect:
            system_text += (
                "\n\nPROTECTED SPANS — reproduce each of these EXACTLY as written, "
                "character for character, unchanged, somewhere in your rewrite: "
                + "; ".join(f'"{p}"' for p in protect)
            )
        if seed == 0:
            digest = hashlib.sha256(f"{system_text}\n{prompt}".encode()).digest()
            seed = int.from_bytes(digest[:8], "big") & 0x7FFFFFFF
        cache_key = (backend, model, system_text, prompt, seed, round(temperature, 4), max_tokens)
        cached = _ENHANCE_CACHE.pop(cache_key, None)
        if cached is not None:
            _ENHANCE_CACHE[cache_key] = cached  # re-insert at the end: LRU touch
            return (cached, f"enhanced via {backend}:{model or 'default'} (cached) seed {seed}")

        # RELATIVE import: inside ComfyUI the pack is not a top-level module
        # (custom nodes load under the loader's package path) — absolute
        # 'from mrln import …' only resolves in pytest
        from .. import promptapi

        effective_max = _effective_max_tokens(prompt, max_tokens)
        try:
            text = promptapi.llm_chat(
                pl.open_library(),
                backend=backend,
                model=model,
                system=system_text,
                prompt=prompt,
                temperature=temperature,
                seed=seed,
                max_tokens=effective_max,
                timeout=timeout,
                free_vram=free_vram,
            )
        except Exception as exc:
            if on_error == "raise":
                raise RuntimeError(f"LLM enhance failed via {backend}: {exc}") from exc
            return (prompt, f"pass-through: {backend} failed ({exc}) — original prompt kept")
        text = _strip_thinking(text).strip()
        if not text:
            return (prompt, f"pass-through: {backend} returned empty text — original kept")
        text, missing = _enforce_protected(text, protect)
        _ENHANCE_CACHE[cache_key] = text  # a miss, so this inserts at the end
        while len(_ENHANCE_CACHE) > _ENHANCE_CACHE_MAX:
            _ENHANCE_CACHE.popitem(last=False)  # drop the least recently used
        vram = "vram freed" if (backend == "ollama" and free_vram == "after call") else "vram kept"
        guarded = ""
        if protect:
            guarded = (
                f" · re-injected {len(missing)} protected span(s): {', '.join(missing)}"
                if missing
                else f" · {len(protect)} protected span(s) verified"
            )
        raised = (
            f" · token cap auto-raised {max_tokens}→{effective_max} to fit the input"
            if effective_max > max_tokens
            else ""
        )
        fallback = (
            " · generic system prompt (no profile system on the wire — pick a "
            "profile on the Template node for model-tuned rewriting)"
            if generic
            else ""
        )
        return (
            text,
            f"enhanced via {backend}:{model or 'default'} seed {seed} "
            f"temp {temperature:g} ({vram}){guarded}{raised}{fallback}",
        )


NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = build_mappings(
    {
        "PromptTemplate": PromptTemplate,
        "PromptSection": PromptSection,
        "LoraApply": LoraApply,
        "PromptEnhance": PromptEnhance,
    }
)
