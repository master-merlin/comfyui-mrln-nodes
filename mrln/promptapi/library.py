"""Read/write endpoints over the two-tier prompt library itself: listing,
template/section detail, item pools, preview renders, user-tier saves and
deletes, and the shareable export/import bundles.
"""

import json

from .. import promptlib as pl
from .core import (
    ApiError,
    _factory_raw,
    _guarded,
    _kv_map,
    _pool,
    _raw_file,
    _require_str,
    _resolved_slot_json,
    _slot_detail,
    _write_json_atomic,
)
from .settings import _profiles_file


@_guarded
def handle_library(lib, payload):
    templates = []
    for slug in lib.template_slugs():
        entry = {"slug": slug, "tier": lib.tier_of("templates", slug)}
        try:
            tpl = lib.load_template(slug)
            entry.update(label=tpl.label, description=tpl.description)
        except pl.PromptLibError as exc:  # one broken file must not hide the library
            entry.update(label=slug, error=str(exc))
        templates.append(entry)
    sections = []
    for slug in lib.section_slugs():
        entry = {"slug": slug, "tier": lib.tier_of("sections", slug)}
        try:
            section = lib.load_section(slug)
            entry.update(
                label=section.label,
                description=section.description,
                item_count=len(section.items),
                suits=list(section.suits),
                merged=section.merged,
                has_lora=any(i.data and i.data.get("lora") for i in section.items),
                lora_bases=sorted(
                    {
                        base
                        for i in section.items
                        if i.data and i.data.get("lora")
                        for base in [pl.lora_base_family(i.data)]
                        if base
                    }
                ),
            )
        except pl.PromptLibError as exc:
            entry.update(label=slug, error=str(exc))
        sections.append(entry)
    factory_profiles = set(_profiles_file(lib.factory_root))
    user_profiles = set(_profiles_file(lib.user_root))
    profiles = [
        {
            "name": name,
            "tier": "factory+user"
            if name in factory_profiles and name in user_profiles
            else ("user" if name in user_profiles else "factory"),
        }
        for name in sorted(lib.pack_profiles())
    ]
    return 200, {
        "fingerprint": lib.fingerprint(),
        "templates": templates,
        "sections": sections,
        "folders": lib.section_folders(),
        "profiles": profiles,
    }


@_guarded
def handle_template(lib, payload):
    slug = _require_str(payload, "slug")
    tpl = lib.load_template(slug)
    refs = [slot.ref for slot in tpl.slots]
    refs.extend(slot.ref for variant in tpl.variants for slot in variant.slots)
    pools = {}
    missing_refs = []
    for ref in refs:
        if ref in pools or ref in missing_refs:  # twin slots on one section share a pool
            continue
        try:
            pools[ref] = _pool(lib, ref)
        except pl.SectionNotFoundError:
            missing_refs.append(ref)  # dead ref: detail still loads, slot flags missing
    detail = {
        "slug": slug,
        "label": tpl.label,
        "type": list(tpl.type),
        "description": tpl.description,
        "prefix": tpl.prefix,
        "suffix": tpl.suffix,
        "negative": tpl.negative,
        "order": list(tpl.order),
        "variant_default": tpl.variant_default,
        "variables": [
            {"name": v.name, "label": v.label, "default": v.default} for v in tpl.variables
        ],
        "profiles": pl.merged_profiles(lib, tpl),
        "slots": [_slot_detail(slot, missing_refs) for slot in tpl.slots],
        "variants": [
            {
                "name": variant.name,
                "label": variant.label,
                "slots": [_slot_detail(slot, missing_refs) for slot in variant.slots],
            }
            for variant in tpl.variants
        ],
        "render": {
            "format": tpl.render.format,
            "joiner": tpl.render.joiner,
            "labeled_line": tpl.render.labeled_line,
            "block_joiner": tpl.render.block_joiner,
            "profile": tpl.render.profile,
        },
    }
    return 200, {
        "slug": slug,
        "tier": lib.tier_of("templates", slug),
        "template": detail,
        "raw": _raw_file(lib, "templates", slug),
        "pools": pools,
        "missing_refs": missing_refs,
        "fingerprint": lib.fingerprint(),
    }


@_guarded
def handle_section(lib, payload):
    slug = _require_str(payload, "slug")
    section = lib.load_section(slug)
    tier = lib.tier_of("sections", slug)
    return 200, {
        "slug": slug,
        "tier": tier,
        "merged": section.merged,
        "replaces": section.replaces,
        "label": section.label,
        "description": section.description,
        "negative": section.negative,
        "tags": list(section.tags),
        "items": [
            {
                "name": item.name,
                "text": item.text,
                "negative": item.negative,
                "weight": item.weight,
                "data": item.data,
                "tags": list(item.tags),
                "excludes": list(item.excludes),
                "requires": list(item.requires),
                "hidden": item.hidden,
                "origin": item.origin or tier,
                # child slots MUST round-trip: the editor writes items back
                # whole and a user item replaces a factory one by name, so
                # omitting these silently destroys every nested item
                # (human/profile carries 17) the moment a row is edited
                "slots": [pl.dump_slot(slot) for slot in item.slots],
            }
            for item in section.items
        ],
        "raw": _raw_file(lib, "sections", slug),
        "factory_raw": _factory_raw(lib, "sections", slug),
        "fingerprint": lib.fingerprint(),
    }


@_guarded
def handle_items(lib, payload):
    ref = _require_str(payload, "ref")
    return 200, {"ref": ref, "items": _pool(lib, ref)}


@_guarded
def handle_preview(lib, payload):
    draft = payload.get("template_data")
    if draft is not None:
        # Unsaved composer draft: parse in memory so the panel can preview
        # structural edits (prefix/suffix/labels/order/slots) before saving.
        if not isinstance(draft, dict):
            raise ApiError("'template_data' must be a JSON object")
        slug = str(payload.get("template") or "(composer draft)")
        tpl = pl.parse_template(draft, slug, "composer draft")
    else:
        slug = _require_str(payload, "template")
        tpl = lib.load_template(slug)
    try:
        seed = int(payload.get("seed", 0))
    except (TypeError, ValueError):
        raise ApiError(f"'seed' must be an integer, got {payload.get('seed')!r}") from None
    mode = payload.get("mode") or "as configured"
    selection = _kv_map(payload, "selection")
    variables = _kv_map(payload, "variables")
    trigger = payload.get("trigger")
    if isinstance(trigger, str) and trigger:
        variables["trigger"] = trigger
    fmt = payload.get("format") or "template default"
    if fmt != "template default" and fmt not in pl.FORMATS:
        raise ApiError(f"unknown format '{fmt}' (formats: {', '.join(pl.FORMATS)})")
    policy = payload.get("conflict_policy") or "negative prevails"
    if policy not in pl.CONFLICT_POLICIES:
        raise ApiError(
            f"unknown conflict_policy '{policy}' (policies: {', '.join(pl.CONFLICT_POLICIES)})"
        )
    composed = pl.compose(
        lib,
        tpl,
        seed=seed,
        mode=mode,
        selection=selection,
        variables=variables,
        profile=payload.get("profile") or pl.STANDARD,
        format=fmt,
        text_length=payload.get("text_length") or "template default",
        conflict_policy=policy,
    )
    out = composed.rendered
    resolved = composed.resolved
    return 200, {
        "positive": out.positive,
        "negative": out.negative,
        "choices": out.choices,
        "format": composed.format,
        "profile": composed.profile,
        "llm": composed.llm,
        "variant": resolved.variant,
        "variant_random": resolved.variant_random,
        "slots": [_resolved_slot_json(s) for s in resolved.slots],
        "fingerprint": lib.fingerprint(),
    }


def _save(lib, payload, kind):
    slug = pl.validate_slug(_require_str(payload, "slug"))
    overrides = (lib.factory_root / kind / f"{slug}.json").is_file()
    lib.save_user(kind, slug, payload.get("data"))
    return 200, {"ok": True, "slug": slug, "tier": "user", "overrides_factory": overrides}


def _retarget_default(default, section_slug, ref, renames):
    """New default token if `default` names a renamed item of `section_slug`
    as seen through a slot ref (leaf or folder scope), else None."""
    if not isinstance(default, str) or not default:
        return None
    if ref == section_slug:
        rel = ""
    elif section_slug.startswith(ref + "/"):
        rel = section_slug[len(ref) + 1 :] + "/"
    else:
        return None
    for old, new in renames.items():
        for prefix in (rel, f"{ref}/{rel}"):  # scope-relative and fully-qualified
            if default == f"{prefix}{old}":
                return f"{prefix}{new}"
    return None


def _propagate_item_renames(lib, section_slug, renames):
    """Rewrite USER-tier template slot defaults referencing renamed items.
    Factory templates are read-only; workflow selections are healed at
    resolve time by the stale-pick fallback instead."""
    rewritten = 0
    for entry in lib._scan("templates").values():
        if entry.tier != "user":
            continue
        try:
            with open(entry.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        changed = False
        slot_lists = [data.get("slots") or []]
        slot_lists.extend((v.get("slots") or []) for v in data.get("variants") or [])
        for slots in slot_lists:
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                new_default = _retarget_default(
                    slot.get("default"), section_slug, str(slot.get("ref") or ""), renames
                )
                if new_default is not None:
                    slot["default"] = new_default
                    changed = True
        if changed:
            _write_json_atomic(entry.path, data, ensure_ascii=False)
            rewritten += 1
    if rewritten:
        lib.invalidate()
    return rewritten


@_guarded
def handle_save_section(lib, payload):
    status, body = _save(lib, payload, "sections")
    if status == 200:
        renames = payload.get("renames") or {}
        if isinstance(renames, dict) and renames:
            clean = {
                str(old): str(new)
                for old, new in renames.items()
                if old and new and str(old) != str(new)
            }
            body["templates_rewritten"] = (
                _propagate_item_renames(lib, body["slug"], clean) if clean else 0
            )
    return status, body


@_guarded
def handle_save_template(lib, payload):
    return _save(lib, payload, "templates")


_BUNDLE_KINDS = {"template": "templates", "section": "sections"}


@_guarded
def handle_export(lib, payload):
    """A shareable bundle for one template (embedding every user-tier
    section it draws from, transitively) or one section. The Composer
    offers it as a .json download."""
    kind = _require_str(payload, "kind")
    if kind not in _BUNDLE_KINDS:
        raise ApiError("'kind' must be 'template' or 'section'")
    slug = _require_str(payload, "slug")
    return 200, pl.export_bundle(lib, _BUNDLE_KINDS[kind], slug)


@_guarded
def handle_import(lib, payload):
    """Import a bundle into the user tier. dry_run=true returns the exact
    write/skip plan for the Composer's confirm card; overwrite=true
    replaces existing user-tier files the bundle collides with."""
    bundle = payload.get("bundle")
    if not isinstance(bundle, dict):
        raise ApiError("missing 'bundle' object (the content of an exported bundle file)")
    slug = payload.get("slug")
    report = pl.import_bundle(
        lib,
        bundle,
        slug=slug.strip() if isinstance(slug, str) and slug.strip() else None,
        overwrite=bool(payload.get("overwrite")),
        dry_run=bool(payload.get("dry_run")),
    )
    report["fingerprint"] = lib.fingerprint()
    return 200, report


@_guarded
def handle_delete(lib, payload):
    kind = _require_str(payload, "kind")
    slug = _require_str(payload, "slug")
    reverted = lib.delete_user(kind, slug)
    return 200, {"ok": True, "slug": slug, "reverted_to_factory": reverted}
