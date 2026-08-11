"""Canonical JSON dicts from engine objects — the inverse of schema.parse_*.

Guarantee: parse_section(dump_section(s), s.slug, source) == s, and the same
for templates (frozen-dataclass equality). Only non-default fields are
emitted so files stay hand-friendly. Raw user-edited files are saved
verbatim elsewhere (dumping them would strip unknown keys the schema
tolerates); these dumpers serve panel-built objects, tests, and future
embedded bundles.
"""

import copy

from .schema import RenderConfig

_DEFAULT_RENDER = RenderConfig()


def dump_item(item):
    out = {"name": item.name, "text": item.text}
    if item.text_short:
        out["text_short"] = item.text_short
    if item.negative:
        out["negative"] = item.negative
    if item.weight != 1.0:
        out["weight"] = item.weight
    if item.data is not None:
        out["data"] = item.data
    for key in ("tags", "excludes", "requires"):
        value = getattr(item, key)
        if value:
            out[key] = list(value)
    if item.slots:
        out["slots"] = [dump_slot(slot) for slot in item.slots]
    if item.hidden:
        out["hidden"] = True
    return out  # origin is runtime provenance, never serialized


def dump_section(section):
    out = {"version": 1, "label": section.label}
    if section.description:
        out["description"] = section.description
    if section.negative:
        out["negative"] = section.negative
    if section.suits:
        out["suits"] = list(section.suits)
    for key in ("tags", "excludes", "requires"):
        value = getattr(section, key)
        if value:
            out[key] = list(value)
    if section.replaces:
        out["replaces"] = True
    out["items"] = [dump_item(item) for item in section.items]
    return out  # merged is runtime state, never serialized


def dump_slot(slot):
    out = {"id": slot.id, "ref": slot.ref}
    if slot.label:
        out["label"] = slot.label
    if slot.default != "random":
        out["default"] = slot.default
    if slot.allow_empty:
        out["allow_empty"] = True
    if slot.empty_weight != 1.0:
        out["empty_weight"] = slot.empty_weight
    if slot.emphasis is not None:
        out["emphasis"] = slot.emphasis
    if slot.tags_any:
        out["tags_any"] = list(slot.tags_any)
    if slot.tags_none:
        out["tags_none"] = list(slot.tags_none)
    return out


def dump_variable(var):
    out = {"name": var.name}
    if var.label:
        out["label"] = var.label
    if var.default:
        out["default"] = var.default
    return out


def dump_variant(variant):
    out = {"name": variant.name}
    if variant.label:
        out["label"] = variant.label
    out["slots"] = [dump_slot(slot) for slot in variant.slots]
    return out


def dump_render(cfg):
    out = {}
    if cfg.format != _DEFAULT_RENDER.format:
        out["format"] = cfg.format
    if cfg.joiner != _DEFAULT_RENDER.joiner:
        out["joiner"] = cfg.joiner
    if cfg.labeled_line != _DEFAULT_RENDER.labeled_line:
        out["labeled_line"] = cfg.labeled_line
    if cfg.block_joiner != _DEFAULT_RENDER.block_joiner:
        out["block_joiner"] = cfg.block_joiner
    if cfg.profile is not None:
        out["profile"] = cfg.profile
    if cfg.text_length != _DEFAULT_RENDER.text_length:
        out["text_length"] = cfg.text_length
    if cfg.lora_tags != _DEFAULT_RENDER.lora_tags:
        out["lora_tags"] = cfg.lora_tags
    return out


def _default_order(tpl):
    ids = tuple(slot.id for slot in tpl.slots)
    return ids + (("@variant",) if tpl.variants else ())


def dump_template(tpl):
    out = {"version": 1, "label": tpl.label}
    if tpl.type:
        out["type"] = list(tpl.type)
    if tpl.description:
        out["description"] = tpl.description
    if tpl.prefix:
        out["prefix"] = tpl.prefix
    if tpl.suffix:
        out["suffix"] = tpl.suffix
    if tpl.negative:
        out["negative"] = tpl.negative
    if tpl.variables:
        out["variables"] = [dump_variable(var) for var in tpl.variables]
    if tpl.slots:
        out["slots"] = [dump_slot(slot) for slot in tpl.slots]
    if tpl.variants:
        out["variants"] = [dump_variant(var) for var in tpl.variants]
    if tpl.variant_default:
        out["variant_default"] = tpl.variant_default
    if tpl.order != _default_order(tpl):
        out["order"] = list(tpl.order)
    render = dump_render(tpl.render)
    if render:
        out["render"] = render
    if tpl.profiles:
        out["profiles"] = copy.deepcopy(tpl.profiles)
    return out
