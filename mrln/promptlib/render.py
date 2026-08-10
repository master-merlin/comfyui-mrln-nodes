"""Rendering: ResolvedPrompt -> positive (4 formats) + negative (always a
plain string) + choices report."""

import json
from dataclasses import dataclass

FORMATS = ("string", "string_labeled", "json", "json_flat")


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
        return f"({text}:{slot.emphasis:g})"
    return text


def _string(resolved, cfg):
    parts = [resolved.prefix]
    parts.extend(_emphasized(s) for s in resolved.slots)
    parts.append(resolved.suffix)
    return cfg.joiner.join(p for p in parts if p)


def render(resolved, fmt, cfg):
    if fmt == "string":
        positive = _string(resolved, cfg)
    elif fmt == "string_labeled":
        lines = []
        if resolved.prefix:
            lines.append(resolved.prefix)
        for slot in resolved.slots:
            if slot.item_name is None or not slot.text:
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
            if slot.item_name is None:
                continue
            obj[slot.id] = slot.text  # no emphasis in JSON formats
        if resolved.suffix:
            obj["suffix"] = resolved.suffix
        positive = json.dumps(obj, ensure_ascii=False, indent=2)
    elif fmt == "json_flat":
        positive = json.dumps({"prompt": _string(resolved, cfg)}, ensure_ascii=False, indent=2)
    else:
        raise ValueError(f"unknown format '{fmt}' (formats: {', '.join(FORMATS)})")

    return Rendered(positive=positive, negative=resolved.negative, choices=_choices(resolved, fmt))


def _choices(resolved, fmt):
    lines = [
        f"template: {resolved.template_slug}   seed: {resolved.seed}   "
        f"mode: {resolved.mode}   format: {fmt}"
    ]
    if resolved.variant is not None:
        lines.append(
            f"variant: {resolved.variant}  [{'random' if resolved.variant_random else 'fixed'}]"
        )
    for slot in resolved.slots:
        if slot.item_name is None:
            mark = "[random]"
            value = "(omitted)"
        else:
            value = slot.item_name
            if slot.fixed_first:
                mark = "[fixed:first]"
            elif slot.random:
                mark = "[random]"
            else:
                mark = "[fixed]"
        line = f"{slot.id}: {value}  {mark}"
        if slot.random and slot.seed_used != resolved.seed:
            line += f"  @{slot.seed_used}"
        if slot.tier == "user":
            line += "  (user)"
        lines.append(line)
    return "\n".join(lines)
