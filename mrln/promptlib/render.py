"""Rendering: ResolvedPrompt -> positive (4 formats) + negative (always a
plain string) + choices report (incl. conflicts and constraint warnings).

Also home of the per-profile RENDER POLICY (block order + negative policy):
reading order changes what a target model produces, and no template can store
one slot order per model — so the order is a render-time function of the
profile, applied to the positive assembly after resolution."""

import json
import math
import re
from dataclasses import dataclass, replace

from .errors import RenderError
from .resolve import walk_slots
from .schema import FORMATS, emphasize  # one definition; a second copy would drift

CONFLICT_POLICIES = ("negative prevails", "positive prevails")
NEGATIVE_POLICIES = ("keep", "drop", "preset")
# Unlisted domains sort in the MIDDLE, so a partial policy moves only the
# domains it names and leaves everything else in authored order.
NEUTRAL_RANK = 50


@dataclass(frozen=True)
class Rendered:
    positive: str
    negative: str
    choices: str


def block_domain(slot):
    """The domain of a rendered block: the first segment of the slug of the
    section it drew from ('lighting/night' -> 'lighting'). Empty for muted,
    omitted and missing slots (they carry no section) — those sort at
    NEUTRAL_RANK. The single definition of the rule block_order ranks."""
    return (getattr(slot, "section_slug", "") or "").split("/", 1)[0]


def _block_order(raw, where):
    """domain -> rank map from raw profile JSON, or None when unset."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RenderError(
            f"{where}render 'block_order' must be an object mapping a block domain to a rank "
            f'number (e.g. {{"subject": 10, "setting": 20, "style": 30}}), '
            f"got {type(raw).__name__} — fix the profile"
        )
    ranks = {}
    for key, value in raw.items():
        domain = str(key).strip()
        if not domain:
            raise RenderError(
                f"{where}render 'block_order' has an empty domain key — a key is a section "
                "domain, the first path segment of a section slug ('lighting/night' -> "
                "'lighting')"
            )
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise RenderError(
                f"{where}render 'block_order': the rank for '{domain}' must be a finite "
                f"number, got {value!r} — rank the domains you care about (10, 20, 30 …); "
                f"anything unlisted renders at {NEUTRAL_RANK}"
            )
        ranks[domain] = float(value)
    return ranks or None


def _negative_policy(overrides, where):
    """('keep'|'drop'|'preset', preset text) from raw profile JSON."""
    raw = overrides.get("negative_policy")
    policy = "keep" if raw is None else raw
    if not isinstance(policy, str) or policy.strip() not in NEGATIVE_POLICIES:
        raise RenderError(
            f"{where}unknown negative_policy {raw!r} (policies: {', '.join(NEGATIVE_POLICIES)}) "
            "— 'keep' renders the template's negative, 'drop' suppresses it for this profile "
            "only, 'preset' replaces it with 'negative_preset'"
        )
    policy = policy.strip()
    raw_preset = overrides.get("negative_preset")
    if raw_preset is not None and not isinstance(raw_preset, str):
        raise RenderError(
            f"{where}render 'negative_preset' must be a string, "
            f"got {type(raw_preset).__name__} — fix the profile"
        )
    preset = raw_preset or ""
    if policy == "preset" and not preset.strip():
        raise RenderError(
            f"{where}negative_policy 'preset' needs a non-empty 'negative_preset' — add the "
            "replacement negative, or use 'drop' to suppress the negative for this profile"
        )
    return policy, preset


@dataclass(frozen=True)
class RenderPolicy:
    """A profile's render-time shaping of one template.

    `block_order` maps a block domain (see block_domain) to a rank; the
    positive assembly is stable-sorted by (rank, authored index), so the same
    blocks come out in a different order — deterministic and content
    preserving. `negative_policy` shapes the negative output at RENDER time
    only: the template file keeps its negative even when the target model
    ignores it, because a profile is a default the node's `profile` widget can
    switch away from, not a lock.
    """

    block_order: dict | None = None
    negative_policy: str = "keep"
    negative_preset: str = ""
    profile: str = ""  # names the profile in the choices report

    @classmethod
    def from_render(cls, overrides, *, profile=""):
        """Build from a profile's raw `render` block (pack-level profiles.json
        is unvalidated user JSON). Returns None when the block carries neither
        knob — no policy, no change to how anything renders. Malformed policy
        data raises RenderError (a 400, not a 500)."""
        if not isinstance(overrides, dict):
            return None  # a non-object render block is already dropped upstream
        where = f"profile '{profile}': " if profile else ""
        ranks = _block_order(overrides.get("block_order"), where)
        negative, preset = _negative_policy(overrides, where)
        if ranks is None and negative == "keep":
            return None
        return cls(
            block_order=ranks,
            negative_policy=negative,
            negative_preset=preset,
            profile=str(profile or ""),
        )


def _body_ids(slots):
    """Ids of the slots that can reach the positive body: inline draws live in
    the prefix/suffix, and a slot that drew nothing renders nothing."""
    return [s.id for s in slots if not s.inline and (s.item_name is not None or s.text)]


def _ordered(resolved, policy):
    """(prompt-for-the-positive-assembly, reordered?). Stable sort of the
    TOP-LEVEL slots by (rank, authored index) — nested children ride inside
    their parent's text and never move. The ResolvedPrompt is never mutated:
    the choices report and every other consumer keep authored order, and a
    policy that changes no position hands back the very same object."""
    if policy is None or not policy.block_order:
        return resolved, False
    ranks = policy.block_order
    ordered = tuple(
        slot
        for _, slot in sorted(
            enumerate(resolved.slots),
            key=lambda pair: (ranks.get(block_domain(pair[1]), NEUTRAL_RANK), pair[0]),
        )
    )
    if _body_ids(ordered) == _body_ids(resolved.slots):
        return resolved, False
    return replace(resolved, slots=ordered), True


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


def render(resolved, fmt, cfg, conflict_policy="negative prevails", policy=None):
    """`policy` is an optional RenderPolicy (the profile's block_order +
    negative_policy). It shapes the POSITIVE assembly and the negative output
    only; resolution — and therefore every drawn item — already happened."""
    if conflict_policy not in CONFLICT_POLICIES:
        raise RenderError(
            f"unknown conflict policy '{conflict_policy}' "
            f"(policies: {', '.join(CONFLICT_POLICIES)})"
        )
    body, reordered = _ordered(resolved, policy)
    if fmt == "string":
        positive = _string(body, cfg)
    elif fmt == "string_labeled":
        lines = []
        if body.prefix:
            lines.append(body.prefix)
        for slot in body.slots:
            if slot.item_name is None or not slot.text or slot.inline:
                continue
            text = _emphasized(slot)
            lines.append(cfg.labeled_line.replace("{label}", slot.label).replace("{text}", text))
        if body.suffix:
            lines.append(body.suffix)
        positive = cfg.block_joiner.join(lines)
    elif fmt == "json":
        obj = {}
        if body.variant is not None:
            obj["variant"] = body.variant
        if body.prefix:
            obj["prefix"] = body.prefix
        for slot in body.slots:
            if slot.item_name is None or slot.inline:
                continue  # inline draws already live in the prefix/suffix text
            obj[slot.id] = slot.text  # no emphasis in JSON formats
        if body.suffix:
            obj["suffix"] = body.suffix
        if cfg.lora_tags:
            tags = lora_tags(body)
            if tags:
                obj["loras"] = tags
        positive = json.dumps(obj, ensure_ascii=False, indent=2)
    elif fmt == "json_flat":
        positive = json.dumps({"prompt": _string(body, cfg)}, ensure_ascii=False, indent=2)
    else:
        raise RenderError(f"unknown format '{fmt}' (formats: {', '.join(FORMATS)})")

    if cfg.lora_tags and fmt in ("string", "string_labeled", "json_flat"):
        tags = lora_tags(body)
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

    # The negative policy runs BEFORE the conflict policy: conflicts are a
    # property of the negative that actually ships.
    negative = resolved.negative
    negative_note = ""
    if policy is not None and policy.negative_policy != "keep":
        shaped = "" if policy.negative_policy == "drop" else policy.negative_preset
        if shaped != negative:
            negative_note = (
                f"negative: {'dropped' if not shaped else 'replaced by a preset'} for "
                f"{policy.profile or 'the target profile'} — the template keeps its negative; "
                "switch the profile to use it"
            )
        negative = shaped
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

    # The report is about the TEMPLATE (authored order, template negative);
    # notes tell what this profile's render did differently, and only when it
    # actually did something.
    notes = []
    if reordered:
        notes.append(f"order: optimized for {policy.profile or 'the target profile'}")
    if negative_note:
        notes.append(negative_note)
    return Rendered(
        positive=positive,
        negative=negative,
        choices=_choices(resolved, fmt, conflicts, conflict_policy, notes=notes),
    )


def _choices(resolved, fmt, conflicts=(), conflict_policy="negative prevails", notes=()):
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
    lines.extend(notes)
    return "\n".join(lines)
