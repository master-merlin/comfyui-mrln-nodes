"""Selection parsing and slot resolution: (library, template, selection,
variables, seed, mode) -> ResolvedPrompt. Pure and deterministic.

Nested randomness: a drawn item may carry child slots (defined on the item
in its SECTION, so templates stay free of choice). Children resolve under
dotted ids ('scene.subject-a') with seed keys '{parent-key}.{child-id}',
and their texts substitute {child-id} placeholders in the parent text."""

import re
from dataclasses import dataclass, replace

from .errors import ItemNotFoundError, RecursionLimitError, SectionNotFoundError, SelectionError
from .schema import RANDOM_TOKEN, TEXT_LENGTHS, Slot
from .seeding import derive_rng, weighted_index
from .textexpr import expand, variable_names

MODES = ("as configured", "randomize all", "all fixed defaults")
RANDOM_TOKENS = (RANDOM_TOKEN, "🎲 random")
OFF_TOKENS = ("off", "🔇 off")  # mute a slot (or the variant block) from the selection
_MAX_NEST_DEPTH = 3


@dataclass(frozen=True)
class ResolvedSlot:
    id: str
    key: str  # seeding key (variant-qualified)
    label: str
    ref: str
    section_slug: str
    item_name: str | None  # None = omitted (allow_empty draw)
    text: str
    negative: str
    random: bool
    fixed_first: bool  # "all fixed defaults" pinned a default-random slot
    emphasis: float | None
    data: dict | None
    tier: str
    seed_used: int
    tags: tuple = ()  # effective tags of the drawn item (item + section)
    requires: tuple = ()  # effective requires (item + section)
    excludes: tuple = ()  # effective excludes (item + section)
    children: tuple = ()  # nested ResolvedSlots (dotted ids)
    missing: bool = False  # ref points at no section: skipped, ⚠ in choices
    inline: bool = False  # woven into prefix/suffix via {slot-id}; leaves the body


def walk_slots(slots):
    """Yield slots depth-first including nested children."""
    for slot in slots:
        yield slot
        yield from walk_slots(slot.children)


@dataclass(frozen=True)
class ResolvedPrompt:
    template_slug: str
    seed: int
    mode: str
    variant: str | None
    variant_random: bool
    prefix: str
    suffix: str
    slots: tuple  # ResolvedSlot, already in render order
    negative: str
    variant_off: bool = False  # variant block muted via 'variant=off'


def parse_kv_lines(text, *, what="line"):
    """'key=value' per line; blank lines and '#'-comments skipped."""
    result = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SelectionError(line, f"expected 'name=value' in {what}")
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            raise SelectionError(line, "empty name")
        if key in result:
            raise SelectionError(line, f"duplicate entry for '{key}'")
        result[key] = value
    return result


def _parse_token(token, line_hint):
    """-> ("random", seed_override | None), ("off", None) or ("fixed", item_name)."""
    token = token.strip()
    if token in OFF_TOKENS:
        return "off", None
    for rand in RANDOM_TOKENS:
        if token == rand:
            return "random", None
        if token.startswith(rand + "@"):
            raw_seed = token[len(rand) + 1 :]
            try:
                return "random", int(raw_seed)
            except ValueError:
                raise SelectionError(
                    line_hint, f"'{raw_seed}' is not a valid seed integer"
                ) from None
    return "fixed", token


def _find_item(pool, ref, token):
    for qualified, section, item in pool:
        if token == qualified or token == f"{ref}/{qualified}":
            return qualified, section, item
    raise ItemNotFoundError(ref, token, [q for q, _, _ in pool])


def _filtered_pool(pool, slot, template_type):
    """Random-draw pool after suits/type and slot tag filters. Fixed picks
    always search the FULL pool — tagging never restricts an explicit
    choice."""
    result = []
    for qualified, section, item in pool:
        if template_type and section.suits and not (set(section.suits) & set(template_type)):
            continue
        effective = set(item.tags) | set(section.tags)
        if slot.tags_any and not (effective & set(slot.tags_any)):
            continue
        if slot.tags_none and (effective & set(slot.tags_none)):
            continue
        result.append((qualified, section, item))
    return result


def _resolve_slot(lib, slot, key, *, master_seed, mode, selection, template_type=()):
    token_src = selection.get(slot.id, slot.default or RANDOM_TOKEN)
    kind, value = _parse_token(str(token_src), f"{slot.id}={token_src}")
    fixed_first = False

    if mode == "randomize all":
        if kind != "off":  # a mute survives 'randomize all'
            kind, value = "random", value if kind == "random" else None
    elif mode == "all fixed defaults":
        d_kind, d_value = _parse_token(slot.default or RANDOM_TOKEN, f"{slot.id}={slot.default}")
        if d_kind == "fixed":
            kind, value = "fixed", d_value
        else:
            kind, value = "fixed", None  # pin to first pool item
            fixed_first = True

    seed_used = value if (kind == "random" and value is not None) else master_seed
    rng = derive_rng(seed_used, key)

    try:
        pool = lib.scope_items(slot.ref)
    except SectionNotFoundError:
        # A dead ref (renamed/deleted section without alias, stale user
        # template) must not kill the whole prompt: the slot resolves as
        # `missing`, contributes nothing, and the choices report carries a
        # loud ⚠ pointing at the Composer's remap.
        return (
            ResolvedSlot(
                id=slot.id,
                key=key,
                label=slot.label,
                ref=slot.ref,
                section_slug="",
                item_name=None,
                text="",
                negative="",
                random=False,
                fixed_first=False,
                emphasis=slot.emphasis,
                data=None,
                tier="",
                seed_used=seed_used,
                missing=True,
            ),
            rng,
            None,
        )
    draw_pool = _filtered_pool(pool, slot, template_type)

    if kind == "off":
        # Muted from the selection: render nothing, but keep the slot in the
        # report. Other slots' draws are unaffected (per-slot seed keys).
        return (
            ResolvedSlot(
                id=slot.id,
                key=key,
                label=slot.label,
                ref=slot.ref,
                section_slug="",
                item_name=None,
                text="",
                negative="",
                random=False,
                fixed_first=False,
                emphasis=slot.emphasis,
                data=None,
                tier="",
                seed_used=seed_used,
            ),
            rng,
            None,
        )

    if kind == "random":
        if not draw_pool:
            raise SelectionError(
                slot.id,
                f"no items left in '{slot.ref}' after suits/tag filters — loosen the "
                "slot's tags_any/tags_none or the template type, or pick an item "
                "explicitly",
            )
        weights = [item.weight for _, _, item in draw_pool]
        if slot.allow_empty:
            weights.append(slot.empty_weight)
        idx = weighted_index(rng, weights)
        if slot.allow_empty and idx == len(draw_pool):
            return (
                ResolvedSlot(
                    id=slot.id,
                    key=key,
                    label=slot.label,
                    ref=slot.ref,
                    section_slug="",
                    item_name=None,
                    text="",
                    negative="",
                    random=True,
                    fixed_first=False,
                    emphasis=slot.emphasis,
                    data=None,
                    tier="",
                    seed_used=seed_used,
                ),
                rng,
                None,
            )
        qualified, section, item = draw_pool[idx]
        is_random = True
    else:
        if fixed_first:
            first_pool = draw_pool or pool
            qualified, section, item = first_pool[0]
        else:
            qualified, section, item = _find_item(pool, slot.ref, value)
        is_random = False

    negative = ", ".join(p for p in (section.negative, item.negative) if p)
    resolved = ResolvedSlot(
        id=slot.id,
        key=key,
        label=slot.label or section.label,
        ref=slot.ref,
        section_slug=section.slug,
        item_name=qualified,
        text=item.text,
        negative=negative,
        random=is_random,
        fixed_first=fixed_first,
        emphasis=slot.emphasis,
        data=item.data,
        tier=item.origin or lib.tier_of("sections", section.slug),
        seed_used=seed_used,
        tags=tuple(sorted(set(item.tags) | set(section.tags))),
        requires=tuple(sorted(set(item.requires) | set(section.requires))),
        excludes=tuple(sorted(set(item.excludes) | set(section.excludes))),
    )
    return resolved, rng, item


def resolve_template(lib, tpl, *, seed, mode, selection, variables, text_length=None):
    if mode not in MODES:
        raise SelectionError(mode, f"unknown selection mode (modes: {', '.join(MODES)})")
    if text_length in (None, "template default"):
        text_length = tpl.render.text_length
    if text_length not in TEXT_LENGTHS:
        raise SelectionError(
            text_length, f"unknown text length (lengths: {', '.join(TEXT_LENGTHS)})"
        )

    merged_vars = {v.name: v.default for v in tpl.variables}
    merged_vars.update(variables or {})
    merged_vars.setdefault("trigger", "")

    # variant selection
    variant = None
    variant_random = False
    variant_off = False
    if tpl.variants:
        token_src = selection.get("variant") or tpl.variant_default or tpl.variants[0].name
        kind, value = _parse_token(str(token_src), f"variant={token_src}")
        if mode == "randomize all":
            if kind != "off":  # a muted variant block survives 'randomize all'
                kind = "random"
        elif mode == "all fixed defaults" and kind in ("random", "off"):
            kind, value = "fixed", tpl.variants[0].name
        if kind == "off":
            variant_off = True
        elif kind == "random":
            rng = derive_rng(value if value is not None else seed, "@variant")
            variant = tpl.variants[weighted_index(rng, [1.0] * len(tpl.variants))]
            variant_random = True
        else:
            by_name = {v.name: v for v in tpl.variants}
            if value not in by_name:
                raise SelectionError(
                    f"variant={value}", f"unknown variant (have: {sorted(by_name)})"
                )
            variant = by_name[value]

    # validate selection keys against active slots
    shared_by_id = {s.id: s for s in tpl.slots}
    active_variant_by_id = {s.id: s for s in variant.slots} if variant else {}
    inactive = {
        s.id: v.name for v in tpl.variants for s in v.slots if not variant or v.name != variant.name
    }
    for key in selection:
        if key == "variant":
            continue
        head = key.split(".", 1)[0]  # nested keys validate their head here,
        if head in shared_by_id or head in active_variant_by_id:  # rest after resolution
            continue
        if head in inactive:
            raise SelectionError(
                f"{key}=…",
                f"slot '{head}' belongs to variant '{inactive[head]}'"
                + (f" (active: '{variant.name}')" if variant else ""),
            )
        raise SelectionError(
            f"{key}=…",
            f"unknown slot (active slots: {sorted(shared_by_id | active_variant_by_id)})",
        )

    # resolve in render order
    resolved_slots = []
    consumed = set()  # dotted child ids that actually materialized
    for entry in tpl.order:
        if entry == "@variant":
            if variant:
                for slot in variant.slots:
                    resolved_slots.append(
                        _resolve_and_expand(
                            lib,
                            slot,
                            f"{variant.name}/{slot.id}",
                            seed,
                            mode,
                            selection,
                            merged_vars,
                            tpl.type,
                            text_length,
                            consumed=consumed,
                        )
                    )
            continue
        slot = shared_by_id[entry]
        resolved_slots.append(
            _resolve_and_expand(
                lib,
                slot,
                slot.id,
                seed,
                mode,
                selection,
                merged_vars,
                tpl.type,
                text_length,
                consumed=consumed,
            )
        )

    for sel_key in selection:
        if "." in sel_key and sel_key not in consumed:
            raise SelectionError(
                f"{sel_key}=…",
                "no such nested slot in the drawn items "
                f"(nested slots present: {sorted(consumed) if consumed else 'none'})",
            )

    # Inline weaving: prefix/suffix may reference top-level slot ids as
    # {placeholders} — the drawn text (with its emphasis wrap) renders right
    # there, inside the author's sentence, and the slot leaves the joined
    # body. This is how a LoRA catchword gets its trigger IN context:
    # prefix "a photo of a {car-lora} at dusk". {trigger} stays the node's.
    slot_vars = {}
    for rs in resolved_slots:
        if rs.id == "trigger":  # the node widget keeps its contract
            continue
        text = rs.text
        if text and rs.emphasis and rs.emphasis != 1.0:
            text = f"({text.rstrip('.')}:{rs.emphasis:g})"
        slot_vars[rs.id] = text
    inline_ids = set()
    for wrapper in (tpl.prefix, tpl.suffix):
        if wrapper:
            inline_ids |= variable_names(wrapper) & set(slot_vars)
    if inline_ids:
        resolved_slots = [
            replace(rs, inline=True) if rs.id in inline_ids else rs for rs in resolved_slots
        ]
    wrap_vars = {**merged_vars, **slot_vars}  # a slot beats a same-named variable
    prefix = expand(tpl.prefix, wrap_vars, derive_rng(seed, "@prefix")) if tpl.prefix else ""
    suffix = expand(tpl.suffix, wrap_vars, derive_rng(seed, "@suffix")) if tpl.suffix else ""
    if inline_ids:
        # an empty draw (allow_empty / muted / missing) weaves "" — tidy seams
        prefix = re.sub(r" {2,}", " ", prefix).strip()
        suffix = re.sub(r" {2,}", " ", suffix).strip()

    negatives = []
    if tpl.negative:
        negatives.append(tpl.negative)
    for rs in walk_slots(resolved_slots):
        for part in rs.negative.split(", "):
            if part and part not in negatives:
                negatives.append(part)

    return ResolvedPrompt(
        template_slug=tpl.slug,
        seed=seed,
        mode=mode,
        variant=variant.name if variant else None,
        variant_random=variant_random,
        prefix=prefix,
        suffix=suffix,
        slots=tuple(resolved_slots),
        negative=", ".join(negatives),
        variant_off=variant_off,
    )


def _resolve_and_expand(
    lib,
    slot,
    key,
    seed,
    mode,
    selection,
    variables,
    template_type=(),
    text_length="long",
    depth=0,
    consumed=None,
):
    resolved, rng, item = _resolve_slot(
        lib,
        slot,
        key,
        master_seed=seed,
        mode=mode,
        selection=selection,
        template_type=template_type,
    )
    if item is not None:
        child_vars = {}
        if item.slots:
            if depth >= _MAX_NEST_DEPTH:
                raise RecursionLimitError()
            children = []
            for child_slot in item.slots:
                dotted = replace(child_slot, id=f"{slot.id}.{child_slot.id}")
                if consumed is not None:
                    consumed.add(dotted.id)
                child = _resolve_and_expand(
                    lib,
                    dotted,
                    f"{key}.{child_slot.id}",
                    seed,
                    mode,
                    selection,
                    variables,
                    template_type=template_type,
                    text_length=text_length,
                    depth=depth + 1,
                    consumed=consumed,
                )
                children.append(child)
                child_text = child.text
                if child_text and child.emphasis and child.emphasis != 1.0:
                    child_text = f"({child_text.rstrip('.')}:{child.emphasis:g})"
                child_vars[child_slot.id] = child_text
            resolved = replace(resolved, children=tuple(children))
        base_text = item.text_short if (text_length == "short" and item.text_short) else item.text
        text = expand(base_text, {**variables, **child_vars}, rng)
        if text != resolved.text:
            resolved = replace(resolved, text=text)
    if "{" in resolved.label:
        # Labels carry template prose, so they expand too — under their own
        # "@label:" seed namespace so no existing draw stream shifts.
        label = expand(resolved.label, variables, derive_rng(seed, f"@label:{key}"))
        if label != resolved.label:
            resolved = replace(resolved, label=label)
    return resolved


def resolve_section(lib, section_ref, item_token, *, seed, allow_empty=False):
    """Standalone Section-node path. `item_token` is 'random'/'🎲 random'
    (optionally '@<seed>') or a path-qualified item ('<section>/<item>' or
    scope-relative)."""
    slot = Slot(id="section", ref=section_ref, allow_empty=allow_empty)
    key = f"@section:{section_ref}"
    selection = {"section": str(item_token)}
    resolved, rng, item = _resolve_slot(
        lib, slot, key, master_seed=seed, mode="as configured", selection=selection
    )
    if resolved.missing:
        # The standalone Section node IS its section — there is nothing to
        # skip to, so a dead ref stays a hard, actionable error here.
        raise SectionNotFoundError(section_ref, lib.section_slugs() + lib.section_folders())
    if item is not None and item.text:
        text = expand(item.text, {"trigger": ""}, rng)
        if text != resolved.text:
            resolved = replace(resolved, text=text)
    return resolved
