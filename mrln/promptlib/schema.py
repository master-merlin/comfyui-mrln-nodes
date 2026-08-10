"""File format v1: dataclasses + parsing/validation for sections and
templates. The formats are frozen API — every v1 feature parses from day one
even where rendering lands later. Unknown keys are ignored (forward compat).
"""

import re
from dataclasses import dataclass, field

from .errors import SchemaError

FORMATS = ("string", "string_labeled", "json", "json_flat")
RANDOM_TOKEN = "random"
SLUG_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION = 1


@dataclass(frozen=True)
class SectionItem:
    name: str
    text: str
    negative: str = ""
    weight: float = 1.0
    data: dict | None = None
    tags: tuple = ()
    excludes: tuple = ()
    requires: tuple = ()


@dataclass(frozen=True)
class Section:
    slug: str
    label: str
    items: tuple
    description: str = ""
    negative: str = ""
    tags: tuple = ()
    excludes: tuple = ()
    requires: tuple = ()


@dataclass(frozen=True)
class Variable:
    name: str
    label: str = ""
    default: str = ""


@dataclass(frozen=True)
class Slot:
    id: str
    ref: str
    label: str = ""  # empty -> section label at resolve time
    default: str = RANDOM_TOKEN  # item name | "random" | "random@<seed>"
    allow_empty: bool = False
    empty_weight: float = 1.0
    emphasis: float | None = None


@dataclass(frozen=True)
class Variant:
    name: str
    slots: tuple
    label: str = ""


@dataclass(frozen=True)
class RenderConfig:
    format: str = "string"
    joiner: str = ", "
    labeled_line: str = "{label}: {text}"
    block_joiner: str = "\n"
    profile: str | None = None


@dataclass(frozen=True)
class Template:
    slug: str
    label: str
    slots: tuple = ()
    variants: tuple = ()
    variant_default: str = ""  # "" -> first variant; may be "random"
    order: tuple = ()  # slot ids + "@variant"; empty -> file order + variant last
    prefix: str = ""
    suffix: str = ""
    negative: str = ""
    description: str = ""
    variables: tuple = ()
    render: RenderConfig = field(default_factory=RenderConfig)


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "item"


def _check_version(data, source):
    version = data.get("version", _VERSION)
    if not isinstance(version, int) or version > _VERSION:
        raise SchemaError(
            source,
            f"file version {version!r} requires a newer MRLN pack (this pack reads version {_VERSION})",
        )


def _str_tuple(value, source, key):
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise SchemaError(source, f"'{key}' must be a string or list of strings")


def _parse_item(raw, index, source):
    if isinstance(raw, str):
        if not raw.strip():
            raise SchemaError(source, f"items[{index}] is an empty string")
        return SectionItem(name=slugify(raw), text=raw)
    if not isinstance(raw, dict):
        raise SchemaError(source, f"items[{index}] must be an object or string")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SchemaError(source, f"items[{index}] is missing a non-empty 'text'")
    name = raw.get("name") or slugify(text)
    if not isinstance(name, str) or not name.strip():
        raise SchemaError(source, f"items[{index}] has an invalid 'name'")
    weight = raw.get("weight", 1.0)
    if not isinstance(weight, (int, float)) or weight < 0:
        raise SchemaError(source, f"items[{index}] ('{name}'): 'weight' must be a number >= 0")
    data = raw.get("data")
    if data is not None and not isinstance(data, dict):
        raise SchemaError(source, f"items[{index}] ('{name}'): 'data' must be an object")
    return SectionItem(
        name=name.strip(),
        text=text,
        negative=str(raw.get("negative", "") or ""),
        weight=float(weight),
        data=data,
        tags=_str_tuple(raw.get("tags"), source, "tags"),
        excludes=_str_tuple(raw.get("excludes"), source, "excludes"),
        requires=_str_tuple(raw.get("requires"), source, "requires"),
    )


def parse_section(data, slug, source):
    if not isinstance(data, dict):
        raise SchemaError(source, "root must be a JSON object")
    _check_version(data, source)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise SchemaError(source, "'items' must be a non-empty list")
    items = tuple(_parse_item(raw, i, source) for i, raw in enumerate(raw_items))
    seen = set()
    for item in items:
        if item.name in seen:
            raise SchemaError(source, f"duplicate item name '{item.name}'")
        seen.add(item.name)
    label = data.get("label") or slug.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
    return Section(
        slug=slug,
        label=str(label),
        items=items,
        description=str(data.get("description", "") or ""),
        negative=str(data.get("negative", "") or ""),
        tags=_str_tuple(data.get("tags"), source, "tags"),
        excludes=_str_tuple(data.get("excludes"), source, "excludes"),
        requires=_str_tuple(data.get("requires"), source, "requires"),
    )


def _parse_slot(raw, where, source):
    if not isinstance(raw, dict):
        raise SchemaError(source, f"{where}: slot must be an object")
    slot_id = raw.get("id")
    if not isinstance(slot_id, str) or not slot_id.strip():
        raise SchemaError(source, f"{where}: slot is missing an 'id'")
    slot_id = slot_id.strip()
    if slot_id == "variant" or slot_id.startswith("@") or "/" in slot_id:
        raise SchemaError(
            source, f"{where}: slot id '{slot_id}' is reserved (no 'variant', '@…', or '/')"
        )
    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise SchemaError(source, f"{where}: slot '{slot_id}' is missing a 'ref'")
    emphasis = raw.get("emphasis")
    if emphasis is not None:
        if not isinstance(emphasis, (int, float)) or emphasis <= 0:
            raise SchemaError(source, f"{where}: slot '{slot_id}': 'emphasis' must be > 0")
        emphasis = float(emphasis)
    empty_weight = raw.get("empty_weight", 1.0)
    if not isinstance(empty_weight, (int, float)) or empty_weight < 0:
        raise SchemaError(source, f"{where}: slot '{slot_id}': 'empty_weight' must be >= 0")
    return Slot(
        id=slot_id,
        ref=ref.strip().strip("/"),
        label=str(raw.get("label", "") or ""),
        default=str(raw.get("default", RANDOM_TOKEN) or RANDOM_TOKEN),
        allow_empty=bool(raw.get("allow_empty", False)),
        empty_weight=float(empty_weight),
        emphasis=emphasis,
    )


def parse_template(data, slug, source):
    if not isinstance(data, dict):
        raise SchemaError(source, "root must be a JSON object")
    _check_version(data, source)

    slots = tuple(_parse_slot(raw, "slots", source) for raw in data.get("slots", []) or [])
    shared_ids = [s.id for s in slots]
    if len(shared_ids) != len(set(shared_ids)):
        raise SchemaError(source, "duplicate slot ids in 'slots'")

    variants = []
    for raw_variant in data.get("variants", []) or []:
        if not isinstance(raw_variant, dict) or not raw_variant.get("name"):
            raise SchemaError(source, "each variant needs a 'name'")
        vname = str(raw_variant["name"]).strip()
        vslots = tuple(
            _parse_slot(raw, f"variant '{vname}'", source)
            for raw in raw_variant.get("slots", []) or []
        )
        vids = [s.id for s in vslots]
        if len(vids) != len(set(vids)):
            raise SchemaError(source, f"variant '{vname}': duplicate slot ids")
        clash = set(vids) & set(shared_ids)
        if clash:
            raise SchemaError(
                source, f"variant '{vname}': slot ids {sorted(clash)} collide with shared slots"
            )
        variants.append(
            Variant(name=vname, slots=vslots, label=str(raw_variant.get("label", "") or ""))
        )
    variants = tuple(variants)
    vnames = [v.name for v in variants]
    if len(vnames) != len(set(vnames)):
        raise SchemaError(source, "duplicate variant names")

    variant_default = str(data.get("variant_default", "") or "")
    if (
        variant_default
        and variants
        and variant_default != RANDOM_TOKEN
        and variant_default not in vnames
    ):
        raise SchemaError(
            source, f"variant_default '{variant_default}' is not a variant (have: {vnames})"
        )

    order = tuple(str(o) for o in data.get("order", []) or [])
    known = set(shared_ids) | {"@variant"}
    for entry in order:
        if entry not in known:
            raise SchemaError(source, f"'order' references unknown slot id '{entry}'")
    if not order:
        order = tuple(shared_ids) + (("@variant",) if variants else ())
    elif variants and "@variant" not in order:
        order = order + ("@variant",)

    raw_render = data.get("render", {}) or {}
    if not isinstance(raw_render, dict):
        raise SchemaError(source, "'render' must be an object")
    fmt = raw_render.get("format", "string")
    if fmt not in FORMATS:
        raise SchemaError(source, f"unknown render format '{fmt}' (formats: {', '.join(FORMATS)})")
    render = RenderConfig(
        format=fmt,
        joiner=str(raw_render.get("joiner", ", ")),
        labeled_line=str(raw_render.get("labeled_line", "{label}: {text}")),
        block_joiner=str(raw_render.get("block_joiner", "\n")),
        profile=raw_render.get("profile"),
    )

    variables = []
    for raw_var in data.get("variables", []) or []:
        if not isinstance(raw_var, dict) or not raw_var.get("name"):
            raise SchemaError(source, "each variable needs a 'name'")
        variables.append(
            Variable(
                name=str(raw_var["name"]).strip(),
                label=str(raw_var.get("label", "") or ""),
                default=str(raw_var.get("default", "") or ""),
            )
        )

    label = data.get("label") or slug.rsplit("/", 1)[-1].replace("-", " ").title()
    return Template(
        slug=slug,
        label=str(label),
        slots=slots,
        variants=variants,
        variant_default=variant_default,
        order=order,
        prefix=str(data.get("prefix", "") or ""),
        suffix=str(data.get("suffix", "") or ""),
        negative=str(data.get("negative", "") or ""),
        description=str(data.get("description", "") or ""),
        variables=tuple(variables),
        render=render,
    )
