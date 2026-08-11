"""Prompt domain: template-driven prompt composition from the two-tier
JSON library (see mrln/promptlib). Nodes are thin runtime anchors — all
logic lives in the engine; combos are rebuilt on every INPUT_TYPES call so
'Refresh node definitions' picks up new library files."""

import hashlib
import json
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
        "The 'Prompt Enhance (MRLN)' single wire: {target, prompt, system, params} — it "
        "carries the rendered prompt plus the selected profile's LLM system prompt.",
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


def parse_loras_json(loras):
    """'loras' JSON -> validated [(name, strength_model, strength_clip, air)].
    Pure so pytest covers it; raises ValueError with remediation text. The
    air urn is "" when the block carries none."""
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
        result.append((str(entry["lora"]), sm, sc, str(entry.get("air") or "")))
    return result


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
    RETURN_TYPES = ("MODEL", "CLIP")
    RETURN_NAMES = ("model", "clip")
    OUTPUT_TOOLTIPS = (
        "The model with every drawn LoRA applied at its authored strength.",
        "The CLIP with every drawn LoRA applied at its authored strength.",
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

    def execute(self, model, clip, loras):
        entries = parse_loras_json(loras)
        if not entries:
            return (model, clip)
        import comfy.sd  # ComfyUI runtime only — keeps the pack importable anywhere
        import comfy.utils
        import folder_paths

        available = folder_paths.get_filename_list("loras")
        normalized = {name.replace("\\", "/").lower(): name for name in available}
        for name, strength_model, strength_clip, air in entries:
            real = name if name in available else normalized.get(name.replace("\\", "/").lower())
            if real is None:
                hint = (
                    f" Its Civitai AIR is {air} — the Composer's section editor "
                    "offers a one-click download that heals the LoRA block."
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
        return (model, clip)


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


def _strip_thinking(text):
    """Remove <think>…</think> blocks that reasoning models prepend."""
    import re as _re

    return _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)


_ENHANCE_CACHE = {}  # (backend, model, system, prompt, seed, temp, max_tokens) -> text


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
    default so the sampler gets the GPU back.
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
                        "tooltip": "Generation cap for the rewrite.",
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
                    ["after call", "keep 5m"],
                    {
                        "tooltip": "Ollama keep_alive: 'after call' unloads the LLM "
                        "immediately so the diffusion model gets the VRAM back "
                        "(recommended on one GPU); 'keep 5m' keeps it warm for rapid "
                        "iteration. LM Studio manages its own lifetime.",
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
                        "wire: {target, prompt, system, params}. It carries the "
                        "rendered prompt AND the profile's system prompt.",
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
        if not system_text:
            return (
                prompt,
                "pass-through: no system prompt — select a profile on the Template node "
                "and wire its llm output, or type a system override",
            )
        if seed == 0:
            digest = hashlib.sha256(f"{system_text}\n{prompt}".encode()).digest()
            seed = int.from_bytes(digest[:8], "big") & 0x7FFFFFFF
        cache_key = (backend, model, system_text, prompt, seed, round(temperature, 4), max_tokens)
        cached = _ENHANCE_CACHE.get(cache_key)
        if cached is not None:
            return (cached, f"enhanced via {backend}:{model or 'default'} (cached) seed {seed}")

        from mrln import promptapi

        try:
            text = promptapi.llm_chat(
                pl.open_library(),
                backend=backend,
                model=model,
                system=system_text,
                prompt=prompt,
                temperature=temperature,
                seed=seed,
                max_tokens=max_tokens,
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
        _ENHANCE_CACHE[cache_key] = text
        vram = "vram freed" if (backend == "ollama" and free_vram == "after call") else "vram kept"
        return (
            text,
            f"enhanced via {backend}:{model or 'default'} seed {seed} "
            f"temp {temperature:g} ({vram})",
        )


NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = build_mappings(
    {
        "PromptTemplate": PromptTemplate,
        "PromptSection": PromptSection,
        "LoraApply": LoraApply,
        "PromptEnhance": PromptEnhance,
    }
)
