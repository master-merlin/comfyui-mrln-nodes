"""Rendering: ResolvedPrompt -> positive (4 formats) + negative (always a
plain string) + choices report (incl. conflicts and constraint warnings)."""

import json
import re
from dataclasses import dataclass

from .errors import RenderError
from .resolve import walk_slots
from .schema import FORMATS, emphasize  # one definition; a second copy would drift

CONFLICT_POLICIES = ("negative prevails", "positive prevails")


@dataclass(frozen=True)
class Rendered:
    positive: str
    negative: str
    choices: str


def _emphasized(slot):
    return emphasize(slot.text, slot.emphasis) if slot.text else ""


def _string(resolved, cfg):
    parts = [resolved.prefix]
    parts.extend(_emphasized(s) for s in resolved.slots if not s.inline)
    parts.append(resolved.suffix)
    if cfg.joiner.lstrip().startswith(","):
        # comma-joined fragments must not END in sentence periods (avoids
        # "encounter., masterpiece"); interior sentences keep theirs.
        return cfg.joiner.join(p.strip().rstrip(".") for p in parts if p)
    return cfg.joiner.join(p for p in parts if p)


def _lora_entries(resolved):
    """(entries, warnings). Schema rejects non-numeric strengths at parse
    time, but an old bad user file (or hand-built data) must degrade — skip
    the entry with a ⚠ choices warning — instead of killing every compose
    that draws the item (_choices calls this unconditionally)."""
    entries = []
    warnings = []
    seen = set()
    for slot in walk_slots(resolved.slots):
        data = slot.data or {}
        name = data.get("lora")
        if not name or slot.item_name is None:
            continue
        name = str(name)
        if name in seen:
            continue
        seen.add(name)
        try:
            sm = float(data.get("strength_model", 1.0))
            sc = float(data.get("strength_clip", sm))
        except (TypeError, ValueError):
            warnings.append(
                f"⚠ {slot.id}: LoRA '{name}' has a non-numeric strength — entry "
                "skipped; fix the item's data in the Composer"
            )
            continue
        entry = {"lora": name, "strength_model": sm, "strength_clip": sc}
        comment = str(data.get("comment") or "").strip()
        if comment.lower().startswith("urn:air:"):
            entry["air"] = comment
        base = lora_base_family(data)
        if base:
            entry["base"] = base
        entries.append(entry)
    return entries, warnings


def lora_base_family(data):
    """The base-model family a LoRA was trained for: the item's explicit
    data.base wins, else the ecosystem segment of its Civitai AIR urn
    (urn:air:<eco>:lora:…). '' when the item declares neither — a LoRA of
    unknown family is never reported as a mismatch."""
    explicit = str((data or {}).get("base") or "").strip().lower()
    if explicit:
        return explicit
    comment = str((data or {}).get("comment") or "").strip()
    if comment.lower().startswith("urn:air:"):
        parts = comment.split(":")
        if len(parts) > 2 and parts[2]:
            return parts[2].strip().lower()
    return ""


def lora_entries(resolved):
    """[{'lora': name-as-authored, 'strength_model': x, 'strength_clip': y}]
    for every drawn item carrying data.lora — the machine-readable stack
    the MRLN LoRA Apply node consumes (file names stay exactly as
    authored so the loader can resolve them). When the item's comment
    carries a Civitai AIR urn it rides along as 'air', making the wire
    self-describing: a machine missing the file knows where to get it."""
    return _lora_entries(resolved)[0]


def lora_tags(resolved):
    """'<lora:name:sm[:sc]>' for every drawn item carrying data.lora —
    the A1111-style syntax that tag-parsing loader nodes consume. The
    name keeps its subfolder, loses its extension, and uses forward
    slashes."""
    tags = []
    for entry in lora_entries(resolved):
        name = entry["lora"].replace("\\", "/")
        for ext in (".safetensors", ".ckpt", ".pt"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
                break
        sm = entry["strength_model"]
        sc = entry["strength_clip"]
        tag = f"<lora:{name}:{sm:g}>" if sc == sm else f"<lora:{name}:{sm:g}:{sc:g}>"
        if tag not in tags:
            tags.append(tag)
    return tags


def render(resolved, fmt, cfg, conflict_policy="negative prevails"):
    if conflict_policy not in CONFLICT_POLICIES:
        raise RenderError(
            f"unknown conflict policy '{conflict_policy}' "
            f"(policies: {', '.join(CONFLICT_POLICIES)})"
        )
    if fmt == "string":
        positive = _string(resolved, cfg)
    elif fmt == "string_labeled":
        lines = []
        if resolved.prefix:
            lines.append(resolved.prefix)
        for slot in resolved.slots:
            if slot.item_name is None or not slot.text or slot.inline:
                continue
            text = _emphasized(slot)
            lines.append(cfg.labeled_line.replace("{label}", slot.label).replace("{text}", text))
        if resolved.suffix:
            lines.append(resolved.suffix)
        positive = cfg.block_joiner.join(lines)
    elif fmt == "json":
        obj = {}
        if resolved.variant is not None:
            obj["variant"] = resolved.variant
        if resolved.prefix:
            obj["prefix"] = resolved.prefix
        for slot in resolved.slots:
            if slot.item_name is None or slot.inline:
                continue  # inline draws already live in the prefix/suffix text
            obj[slot.id] = slot.text  # no emphasis in JSON formats
        if resolved.suffix:
            obj["suffix"] = resolved.suffix
        if cfg.lora_tags:
            tags = lora_tags(resolved)
            if tags:
                obj["loras"] = tags
        positive = json.dumps(obj, ensure_ascii=False, indent=2)
    elif fmt == "json_flat":
        positive = json.dumps({"prompt": _string(resolved, cfg)}, ensure_ascii=False, indent=2)
    else:
        raise RenderError(f"unknown format '{fmt}' (formats: {', '.join(FORMATS)})")

    if cfg.lora_tags and fmt in ("string", "string_labeled", "json_flat"):
        tags = lora_tags(resolved)
        if tags:
            joined = " ".join(tags)
            if fmt == "string_labeled":
                positive = f"{positive}{cfg.block_joiner}LoRAs: {joined}"
            elif fmt == "json_flat":
                obj = json.loads(positive)
                obj["prompt"] = f"{obj['prompt']} {joined}"
                positive = json.dumps(obj, ensure_ascii=False, indent=2)
            else:
                positive = f"{positive} {joined}"

    negative = resolved.negative
    conflicts = []
    if negative:
        terms = [t for t in negative.split(", ") if t]
        # Whole-term match only: a plain substring search drops the negative
        # 'art' against a positive 'trending on artstation'. The lookarounds
        # (rather than \b) keep terms that begin or end in punctuation
        # working, so only the false positives change.
        conflicts = [
            t
            for t in terms
            if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", positive, re.IGNORECASE) is not None
        ]
        if conflict_policy == "positive prevails" and conflicts:
            negative = ", ".join(t for t in terms if t not in conflicts)

    return Rendered(
        positive=positive,
        negative=negative,
        choices=_choices(resolved, fmt, conflicts, conflict_policy),
    )


def _choices(resolved, fmt, conflicts=(), conflict_policy="negative prevails"):
    lines = [
        f"template: {resolved.template_slug}   seed: {resolved.seed}   "
        f"mode: {resolved.mode}   format: {fmt}"
    ]
    if resolved.variant is not None:
        lines.append(
            f"variant: {resolved.variant}  [{'random' if resolved.variant_random else 'fixed'}]"
        )
    elif getattr(resolved, "variant_off", False):
        lines.append("variant: (off)  [off]")
    for slot in walk_slots(resolved.slots):
        if slot.missing:
            indent = "  " * slot.id.count(".")
            lines.append(
                f"{indent}⚠ {slot.id}: section '{slot.ref}' is missing — remap the slot "
                "in the Composer or restore the section"
            )
            continue
        if slot.item_name is None:
            mark = "[random]" if slot.random else "[off]"
            value = "(omitted)" if slot.random else "(muted)"
        else:
            value = slot.item_name
            if slot.fixed_first:
                mark = "[fixed:first]"
            elif slot.random:
                mark = "[random]"
            else:
                mark = "[fixed]"
        indent = "  " * slot.id.count(".")  # nested children indent under parents
        line = f"{indent}{slot.id}: {value}  {mark}"
        if slot.random and slot.seed_used != resolved.seed:
            line += f"  @{slot.seed_used}"
        if slot.tier == "user":
            line += "  (user)"
        if slot.inline:
            line += "  (inline)"
        lines.append(line)
        if slot.stale_note:
            lines.append(f"{indent}⚠ {slot.id}: {slot.stale_note}")

    for tag in lora_tags(resolved):
        lines.append(f"lora: {tag}")
    lines.extend(_lora_entries(resolved)[1])  # skipped-entry warnings

    drawn = [s for s in walk_slots(resolved.slots) if s.item_name is not None]
    present = set()
    for slot in walk_slots(resolved.slots):
        present.update(slot.tags)
    for slot in drawn:
        for req in slot.requires:
            if req not in present:
                lines.append(f"⚠ {slot.id}: requires '{req}' — nothing drawn carries that tag")
        for exc in slot.excludes:
            # Per-slot provenance, not `present - set(slot.tags)`: subtracting
            # removes the tag VALUE globally, so a slot that carries AND
            # excludes 'glass' hid every other slot's 'glass'.
            if any(exc in other.tags for other in drawn if other is not slot):
                lines.append(f"⚠ {slot.id}: excludes '{exc}' — but another drawn item carries it")
    for term in conflicts:
        if conflict_policy == "positive prevails":
            lines.append(f"conflict: '{term}' is in the prompt — dropped from negative")
        else:
            lines.append(f"conflict: '{term}' is in the prompt — kept in negative (prevails)")
    return "\n".join(lines)
