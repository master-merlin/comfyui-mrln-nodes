"""Target-model profiles: named bundles of render overrides, an optional
LLM spec, and an optional JSON scaffold (json_template). Pack-level
defaults live in <root>/profiles.json (factory overlaid by the user
tier); a template's own 'profiles' extend them by name.

compose() is the single template pipeline (profile -> resolve -> render
-> optional json_template fill) used by BOTH the node and the preview
endpoint, so parity is structural, not tested-for."""

import json
import re
from dataclasses import dataclass, replace

from .errors import SelectionError
from .render import render
from .resolve import resolve_template, walk_slots

STANDARD = "standard"
_RENDER_OVERRIDES = (
    "format",
    "joiner",
    "labeled_line",
    "block_joiner",
    "text_length",
    "lora_tags",
    "profile",
)
_WIDGET_DEFAULT = (None, "", "template default")
_DROP = object()
_SLOT_RE = re.compile(r"\{slot:([A-Za-z0-9_,-]+)(?:\|([^{}]*))?\}")


def overlay_profile(base, over):
    """Merge two profile dicts: 'render' and 'llm' merge shallowly, the
    json_template scaffold and scalar keys replace wholesale."""
    out = dict(base)
    for key, value in over.items():
        if key in ("render", "llm") and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def merged_profiles(lib, tpl):
    """Pack-level profiles extended by the template's own, by name."""
    merged = dict(lib.pack_profiles())
    for name, profile in (tpl.profiles or {}).items():
        merged[name] = overlay_profile(merged.get(name, {}), profile)
    return merged


def _effective_render(cfg, profile):
    overrides = profile.get("render") or {}
    known = {k: overrides[k] for k in _RENDER_OVERRIDES if k in overrides}
    if "lora_tags" in known:
        known["lora_tags"] = bool(known["lora_tags"])
    return replace(cfg, **known) if known else cfg


def apply_template_overrides(tpl, overrides):
    """A template profile's 'overrides' block: a per-profile VARIANT of the
    template stored as a sparse diff against the standard render (prefix/
    suffix/negative/variant_default plus per-slot default/emphasis by id).
    The base template stays untouched on disk, so 'standard' is always the
    way back."""
    if not overrides:
        return tpl
    slot_ov = overrides.get("slots") or {}

    def _fix(slot):
        raw = slot_ov.get(slot.id)
        if not isinstance(raw, dict):
            return slot
        known = {}
        if "default" in raw:
            known["default"] = str(raw["default"])
        if "emphasis" in raw:
            known["emphasis"] = None if raw["emphasis"] is None else float(raw["emphasis"])
        return replace(slot, **known) if known else slot

    known = {
        key: str(overrides[key])
        for key in ("prefix", "suffix", "negative", "variant_default")
        if key in overrides
    }
    known["slots"] = tuple(_fix(s) for s in tpl.slots)
    known["variants"] = tuple(
        replace(v, slots=tuple(_fix(s) for s in v.slots)) for v in tpl.variants
    )
    return replace(tpl, **known)


def _fill_string(text, positive, negative, slot_texts):
    stripped = text.strip()
    if stripped == "{positive}":
        return positive if positive else _DROP
    if stripped == "{negative}":
        return negative if negative else _DROP
    whole = _SLOT_RE.fullmatch(stripped)
    if whole:
        ids = [i.strip() for i in whole.group(1).split(",") if i.strip()]
        texts = [t for t in ((slot_texts.get(i) or "").strip() for i in ids) if t]
        if not texts:
            fallback = whole.group(2)
            return fallback if fallback else _DROP
        return texts if len(ids) > 1 else texts[0]

    def _sub(match):
        ids = [i.strip() for i in match.group(1).split(",") if i.strip()]
        texts = [t for t in ((slot_texts.get(i) or "").strip() for i in ids) if t]
        return ", ".join(texts) if texts else (match.group(2) or "")

    filled = text.replace("{positive}", positive).replace("{negative}", negative)
    return _SLOT_RE.sub(_sub, filled)


def fill_json_template(node, positive, negative, slot_texts):
    """Fill a profile's JSON scaffold: '{positive}'/'{negative}' and
    '{slot:id}' placeholders resolve from the render; '{slot:a,b}' becomes
    a list; '{slot:id|fallback}' falls back; unfilled optional values (and
    containers left empty by drops) disappear from the payload."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            filled = fill_json_template(value, positive, negative, slot_texts)
            if filled is _DROP:
                continue
            out[key] = filled
        return out if out else _DROP
    if isinstance(node, list):
        out = [
            filled
            for filled in (fill_json_template(v, positive, negative, slot_texts) for v in node)
            if filled is not _DROP
        ]
        return out if out else _DROP
    if isinstance(node, str):
        return _fill_string(node, positive, negative, slot_texts)
    return node


@dataclass(frozen=True)
class Composed:
    resolved: object  # ResolvedPrompt
    rendered: object  # Rendered (positive may be a filled json_template)
    llm: str  # JSON: {"target", "prompt", "protect"?, "system"?, "params"?} — one Enhance wire
    format: str  # effective format after profile + widget precedence
    text_length: str
    profile: str


def compose(
    lib,
    tpl,
    *,
    seed,
    mode,
    selection,
    variables,
    profile=STANDARD,
    format=None,
    text_length=None,
    conflict_policy="negative prevails",
):
    """The single template pipeline. Precedence per knob: explicit widget
    value > profile override > template render config."""
    name = (profile or STANDARD).strip() or STANDARD
    prof = {}
    if name != STANDARD:
        available = merged_profiles(lib, tpl)
        if name not in available:
            raise SelectionError(
                f"profile={name}",
                f"unknown profile (have: {', '.join([STANDARD, *sorted(available)])})",
            )
        prof = available[name]
    tpl = apply_template_overrides(tpl, prof.get("overrides"))
    cfg = _effective_render(tpl.render, prof)
    effective_length = text_length if text_length not in _WIDGET_DEFAULT else cfg.text_length
    resolved = resolve_template(
        lib,
        tpl,
        seed=seed,
        mode=mode,
        selection=selection,
        variables=variables,
        text_length=effective_length,
    )
    fmt = format if format not in _WIDGET_DEFAULT else cfg.format
    out = render(resolved, fmt, cfg, conflict_policy=conflict_policy)

    scaffold = prof.get("json_template")
    if scaffold:
        slot_texts = {s.id: (s.text or "") for s in resolved.slots}
        filled = fill_json_template(scaffold, out.positive, out.negative, slot_texts)
        payload = {} if filled is _DROP else filled
        out = replace(out, positive=json.dumps(payload, ensure_ascii=False, indent=2))

    # the llm output is self-sufficient: it carries the rendered prompt, so
    # a single wire feeds the Enhance node (its prompt input stays optional
    # for enhancing arbitrary strings)
    llm = {"target": name, "prompt": out.positive}
    # protected spans: drawn LoRA trigger texts + the {trigger} value. The
    # Enhance node hard-enforces these — LLMs love to "improve" trigger
    # words, which silently kills the LoRA activation.
    protect = []
    for slot in walk_slots(resolved.slots):
        data = slot.data or {}
        if data.get("lora") and slot.item_name is not None:
            text = (slot.text or "").strip()
            if text and text not in protect:
                protect.append(text)
    trigger_value = str((variables or {}).get("trigger") or "").strip()
    if trigger_value and trigger_value not in protect:
        protect.append(trigger_value)
    if protect:
        llm["protect"] = protect
    if name != STANDARD:
        for key in ("system", "params"):
            value = (prof.get("llm") or {}).get(key)
            if value:
                llm[key] = value
    return Composed(
        resolved=resolved,
        rendered=out,
        llm=json.dumps(llm, ensure_ascii=False),
        format=fmt,
        text_length=effective_length,
        profile=name,
    )
