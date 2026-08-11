"""Rendering: ResolvedPrompt -> positive (4 formats) + negative (always a
plain string) + choices report (incl. conflicts and constraint warnings)."""

import json
import re
from dataclasses import dataclass

from .resolve import walk_slots

FORMATS = ("string", "string_labeled", "json", "json_flat")
CONFLICT_POLICIES = ("negative prevails", "positive prevails")


@dataclass(frozen=True)
class Rendered:
    positive: str
    negative: str
    choices: str


def _emphasized(slot):
    text = slot.text
    if not text:
        return ""
    if slot.emphasis and slot.emphasis != 1.0:
        # a sentence period inside a weight wrapper reads as "…canopy.:1.15"
        return f"({text.rstrip('.')}:{slot.emphasis:g})"
    return text


def _string(resolved, cfg):
    parts = [resolved.prefix]
    parts.extend(_emphasized(s) for s in resolved.slots if not s.inline)
    parts.append(resolved.suffix)
    if cfg.joiner.lstrip().startswith(","):
        # comma-joined fragments must not END in sentence periods (avoids
        # "encounter., masterpiece"); interior sentences keep theirs.
        return cfg.joiner.join(p.strip().rstrip(".") for p in parts if p)
    return cfg.joiner.join(p for p in parts if p)


def lora_entries(resolved):
    """[{'lora': name-as-authored, 'strength_model': x, 'strength_clip': y}]
    for every drawn item carrying data.lora — the machine-readable stack
    the MRLN LoRA Apply node consumes (file names stay exactly as
    authored so the loader can resolve them)."""
    entries = []
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
        sm = float(data.get("strength_model", 1.0))
        entries.append(
            {
                "lora": name,
                "strength_model": sm,
                "strength_clip": float(data.get("strength_clip", sm)),
            }
        )
    return entries


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
        raise ValueError(
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
        raise ValueError(f"unknown format '{fmt}' (formats: {', '.join(FORMATS)})")

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
        conflicts = [
            t for t in terms if re.search(re.escape(t), positive, re.IGNORECASE) is not None
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

    present = set()
    for slot in walk_slots(resolved.slots):
        present.update(slot.tags)
    for slot in walk_slots(resolved.slots):
        if slot.item_name is None:
            continue
        others = present - set(slot.tags)
        for req in slot.requires:
            if req not in present:
                lines.append(f"⚠ {slot.id}: requires '{req}' — nothing drawn carries that tag")
        for exc in slot.excludes:
            if exc in others:
                lines.append(f"⚠ {slot.id}: excludes '{exc}' — but another drawn item carries it")
    for term in conflicts:
        if conflict_policy == "positive prevails":
            lines.append(f"conflict: '{term}' is in the prompt — dropped from negative")
        else:
            lines.append(f"conflict: '{term}' is in the prompt — kept in negative (prevails)")
    return "\n".join(lines)
