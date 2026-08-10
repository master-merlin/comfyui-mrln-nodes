"""Selection parsing and slot resolution: (library, template, selection,
variables, seed, mode) -> ResolvedPrompt. Pure and deterministic."""

from dataclasses import dataclass, replace

from .errors import ItemNotFoundError, SelectionError
from .schema import RANDOM_TOKEN, Slot
from .seeding import derive_rng, weighted_index
from .textexpr import expand

MODES = ("as configured", "randomize all", "all fixed defaults")
RANDOM_TOKENS = (RANDOM_TOKEN, "🎲 random")


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
    """-> ("random", seed_override | None) or ("fixed", item_name)."""
    token = token.strip()
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


def _resolve_slot(lib, slot, key, *, master_seed, mode, selection):
    token_src = selection.get(slot.id, slot.default or RANDOM_TOKEN)
    kind, value = _parse_token(str(token_src), f"{slot.id}={token_src}")
    fixed_first = False

    if mode == "randomize all":
        kind, value = "random", value if kind == "random" else None
    elif mode == "all fixed defaults":
        d_kind, d_value = _parse_token(slot.default or RANDOM_TOKEN, f"{slot.id}={slot.default}")
        if d_kind == "fixed":
            kind, value = "fixed", d_value
        else:
            kind, value = "fixed", None  # pin to first pool item
            fixed_first = True

    pool = lib.scope_items(slot.ref)
    seed_used = value if (kind == "random" and value is not None) else master_seed
    rng = derive_rng(seed_used, key)

    if kind == "random":
        weights = [item.weight for _, _, item in pool]
        if slot.allow_empty:
            weights.append(slot.empty_weight)
        idx = weighted_index(rng, weights)
        if slot.allow_empty and idx == len(pool):
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
        qualified, section, item = pool[idx]
        is_random = True
    else:
        if fixed_first:
            qualified, section, item = pool[0]
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
        tier=lib.tier_of("sections", section.slug),
        seed_used=seed_used,
    )
    return resolved, rng, item


def resolve_template(lib, tpl, *, seed, mode, selection, variables):
    if mode not in MODES:
        raise SelectionError(mode, f"unknown selection mode (modes: {', '.join(MODES)})")

    merged_vars = {v.name: v.default for v in tpl.variables}
    merged_vars.update(variables or {})
    merged_vars.setdefault("trigger", "")

    # variant selection
    variant = None
    variant_random = False
    if tpl.variants:
        token_src = selection.get("variant") or tpl.variant_default or tpl.variants[0].name
        kind, value = _parse_token(str(token_src), f"variant={token_src}")
        if mode == "randomize all":
            kind = "random"
        elif mode == "all fixed defaults" and kind == "random":
            kind, value = "fixed", tpl.variants[0].name
        if kind == "random":
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
        if key in shared_by_id or key in active_variant_by_id:
            continue
        if key in inactive:
            raise SelectionError(
                f"{key}=…",
                f"slot '{key}' belongs to variant '{inactive[key]}'"
                + (f" (active: '{variant.name}')" if variant else ""),
            )
        raise SelectionError(
            f"{key}=…",
            f"unknown slot (active slots: {sorted(shared_by_id | active_variant_by_id)})",
        )

    # resolve in render order
    resolved_slots = []
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
                        )
                    )
            continue
        slot = shared_by_id[entry]
        resolved_slots.append(
            _resolve_and_expand(lib, slot, slot.id, seed, mode, selection, merged_vars)
        )

    prefix = expand(tpl.prefix, merged_vars, derive_rng(seed, "@prefix")) if tpl.prefix else ""
    suffix = expand(tpl.suffix, merged_vars, derive_rng(seed, "@suffix")) if tpl.suffix else ""

    negatives = []
    if tpl.negative:
        negatives.append(tpl.negative)
    for rs in resolved_slots:
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
    )


def _resolve_and_expand(lib, slot, key, seed, mode, selection, variables):
    resolved, rng, item = _resolve_slot(
        lib, slot, key, master_seed=seed, mode=mode, selection=selection
    )
    if item is not None and item.text:
        text = expand(item.text, variables, rng)
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
    if item is not None and item.text:
        text = expand(item.text, {"trigger": ""}, rng)
        if text != resolved.text:
            resolved = replace(resolved, text=text)
    return resolved
