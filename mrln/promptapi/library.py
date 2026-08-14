"""Read/write endpoints over the two-tier prompt library itself: listing,
template/section detail, item pools, preview renders, user-tier saves and
deletes, and the shareable export/import bundles.
"""

import json
import re

from .. import promptlib as pl

# Direct submodule import: promptlib's __init__ rebinds the name 'render' to
# render() itself, so `pl.render` is the function, not this module.
from ..promptlib.render import ordered_slot_ids

# Module objects, not names — the same seam style as decompose.py, so a test
# can patch either module and every caller here sees it. lora.py owns the
# secret registry (every NEW client-visible string goes through
# _scrub_secrets); thumbs.py owns the two-tier thumbnail layout, and the
# listings below only tag each row with whether a thumbnail exists.
from . import lora, thumbs
from .core import (
    ApiError,
    _factory_raw,
    _guarded,
    _kv_map,
    _pool,
    _require_str,
    _resolved_slot_json,
    _slot_detail,
    _tier_raw,
    _write_json_atomic,
)
from .settings import _profiles_file


@_guarded
def handle_library(lib, payload):
    # Fingerprint FIRST, for two reasons. (1) 'fp=<fingerprint>' short-circuits
    # the whole listing: the panel caches (fingerprint, payload) and re-sends
    # the fingerprint on refresh, so an unchanged library costs one scan
    # instead of parsing every file. (2) Taken first, the returned fingerprint
    # can only ever be OLDER than the payload it labels — the other order could
    # label a stale payload with a fresh fingerprint, which a caching client
    # would then keep forever. A request without 'fp' behaves exactly as before.
    fingerprint = lib.fingerprint()
    fp = payload.get("fp")
    if isinstance(fp, str) and fp and fp == fingerprint:
        return 200, {"unchanged": True, "fingerprint": fingerprint}
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
                # A "combine" is an ordinary section whose every item only
                # delegates to another section, so nothing structural marks one
                # — in the library it looked exactly like any other section and
                # the only way to find out was to open it. The panel draws a
                # chip from this, and the shape it tests is the one the combine
                # builder generates (util.js isCombineItem).
                combine=bool(section.items)
                and all(
                    i.text.strip() == "{pick}" and len(i.slots) == 1 and i.slots[0].id == "pick"
                    for i in section.items
                ),
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
    # One flag per row so the card grid knows whether to request a tile or draw
    # its domain glyph. Index-backed (thumbs.thumb_index): one directory walk
    # per tier for the whole listing, not a stat per row — 268 rows of
    # per-entry path resolution measured 362 ms on Windows, the index 0.2 ms
    # with no thumbnails on disk and 18 ms with all 268 present.
    thumbs.annotate_entries(lib, templates, "templates")
    thumbs.annotate_entries(lib, sections, "sections")
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
        "fingerprint": fingerprint,
        "templates": templates,
        "sections": sections,
        "folders": lib.section_folders(),
        "profiles": profiles,
    }


@_guarded
def handle_template(lib, payload):
    slug = _require_str(payload, "slug")
    # Fingerprint FIRST, for the same two reasons as handle_library: taken
    # last it can label an already-stale payload with a fresh fingerprint,
    # and since fingerprint() invalidates the scan memo before re-walking,
    # taking it first shares those walks with the load below instead of
    # doubling them (measured: 4 tree walks -> 2 on this path).
    fingerprint = lib.fingerprint()
    # `tier` shows ONE tier's file for a slug that exists in both — the factory
    # version under a user file that shadows it. A comparison, not a mode:
    # nothing here changes which file a render uses.
    tier_view = str(payload.get("tier") or "").strip()
    if tier_view and tier_view not in ("factory", "user"):
        raise ApiError("'tier' must be 'factory' or 'user'")
    tpl = lib.load_template(slug, tier=tier_view or None)
    # aliases.json redirects a retired slug: everything below must speak the
    # LIVE slug, or tier_of() misses (reporting tier "") and a save-back
    # writes a user file under the dead name, shadowing the alias forever.
    resolved = tpl.slug
    refs = [slot.ref for slot in tpl.slots]
    refs.extend(slot.ref for variant in tpl.variants for slot in variant.slots)
    pools = {}
    missing_refs = []
    for ref in refs:
        if ref in pools or ref in missing_refs:  # twin slots on one section share a pool
            continue
        try:
            # LoRA-bearing pool rows get has_thumb: an item's face is its
            # LoRA's Civitai preview (thumbs.annotate_items says why nothing
            # else is tagged)
            pools[ref] = thumbs.annotate_items(lib, _pool(lib, ref))
        except pl.SectionNotFoundError:
            missing_refs.append(ref)  # dead ref: detail still loads, slot flags missing
    detail = {
        "slug": resolved,
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
    tiers = lib.tiers_of("templates", resolved)
    return 200, {
        "slug": resolved,
        "requested": slug,  # so a client that asked under a retired name can correlate
        "tier": lib.tier_of("templates", resolved),
        # which tiers HAVE a file (the winner alone cannot say a factory
        # version exists), and which one this payload is showing
        "tiers": list(tiers),
        "viewing": tier_view or lib.tier_of("templates", resolved),
        "has_thumb": thumbs.has_thumb(lib, "templates", resolved),
        "template": detail,
        "raw": _tier_raw(lib, "templates", resolved, tier_view),
        "pools": pools,
        "missing_refs": missing_refs,
        "fingerprint": fingerprint,
    }


@_guarded
def handle_section(lib, payload):
    slug = _require_str(payload, "slug")
    fingerprint = lib.fingerprint()  # first: see handle_template
    # see handle_template: one tier's own file, for comparison. For a SECTION
    # that also means unmerged — the only way to see what the factory shipped
    # under a slug your file extends.
    tier_view = str(payload.get("tier") or "").strip()
    if tier_view and tier_view not in ("factory", "user"):
        raise ApiError("'tier' must be 'factory' or 'user'")
    section = lib.load_section(slug, tier=tier_view or None)
    resolved = section.slug  # alias-resolved; see handle_template
    tier = lib.tier_of("sections", resolved)
    return 200, {
        "slug": resolved,
        "requested": slug,
        "tier": tier,
        "tiers": list(lib.tiers_of("sections", resolved)),
        "viewing": tier_view or tier,
        "has_thumb": thumbs.has_thumb(lib, "sections", resolved),
        "merged": section.merged,
        "replaces": section.replaces,
        "label": section.label,
        "description": section.description,
        "negative": section.negative,
        "tags": list(section.tags),
        "items": thumbs.annotate_items(
            lib,
            [
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
        ),
        "raw": _tier_raw(lib, "sections", resolved, tier_view),
        "factory_raw": _factory_raw(lib, "sections", resolved),
        "fingerprint": fingerprint,
    }


@_guarded
def handle_items(lib, payload):
    ref = _require_str(payload, "ref")
    return 200, {"ref": ref, "items": thumbs.annotate_items(lib, _pool(lib, ref))}


def _render_order(lib, tpl, composed):
    """Top-level slot ids in the READING ORDER this render used: the target
    profile's block_order as render.py sorts it, authored order when the
    profile ranks nothing.

    'slots' below stays in AUTHORED order (it mirrors the template, and the
    Composer's rows are keyed off it) and `choices` reports only THAT a
    profile moved something — so the Composer's "Optimize for …" comparison,
    which shows the optimized order and can write it back into a template,
    had no way to learn the order itself without reimplementing the sort in
    JS. This is that one additive key. The policy is rebuilt exactly the way
    compose() builds it (from_render tolerates a wrong-shaped render block
    there and here alike); 'standard' is not a profile, so it never has one."""
    policy = None
    if composed.profile != pl.STANDARD:
        prof = pl.merged_profiles(lib, tpl).get(composed.profile) or {}
        policy = pl.RenderPolicy.from_render(prof.get("render") or {}, profile=composed.profile)
    return ordered_slot_ids(composed.resolved, policy)


@_guarded
def handle_preview(lib, payload):
    # first, like handle_template — /preview fires on every 300 ms-debounced
    # keystroke in the composer, so halving its tree walks is the hot path
    fingerprint = lib.fingerprint()
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
        "render_order": _render_order(lib, tpl, composed),
        "fingerprint": fingerprint,
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
    resolve time by the stale-pick fallback instead.

    Returns (rewritten, failures). This runs AFTER the section file is
    durably saved, so an unwritable template file (AV/sync lock on Windows)
    must never turn that save into a 500 — reads were always skipped on
    OSError, and now writes are too, reported instead of raised."""
    rewritten, failures = 0, []
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
            try:
                _write_json_atomic(entry.path, data, ensure_ascii=False)
            except OSError as exc:
                failures.append(lora._scrub_secrets(f"{entry.slug}: {exc}"))
                continue
            rewritten += 1
    if rewritten:
        lib.invalidate()
    return rewritten, failures


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
            rewritten, failures = (
                _propagate_item_renames(lib, body["slug"], clean) if clean else (0, [])
            )
            body["templates_rewritten"] = rewritten
            if failures:
                # the section file is already on disk: report the partial
                # follow-up inside the 200 instead of claiming the save failed
                body["rename_warning"] = (
                    f"{len(failures)} user template file(s) could not be re-pointed "
                    f"at the renamed items: {'; '.join(failures[:3])}"
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


# -- search -------------------------------------------------------------------

SEARCH_SCOPES = ("name", "text", "both")
SEARCH_LIMIT = 60
SEARCH_SAMPLES = 3


def _search_terms(query):
    """The query as compiled term matchers. Every term must hit (AND), because
    narrowing is the entire point — 'neon street' should not return every
    section that mentions either.

    A term matches at the START OF A WORD, not anywhere in it. Plain substring
    matching looked reasonable and was not: 'rain' hit terrain, grain and
    training; 'disco' hit discovered. Word-prefix keeps the useful half
    (rain -> rainy, rainfall) and drops the noise, which is the difference
    between a filter you trust and one you stop using."""
    return [
        re.compile(r"(?<![a-z0-9])" + re.escape(term))
        for term in str(query or "").lower().split()
        if term
    ]


def search_sections(lib, query, *, scope="both", limit=SEARCH_LIMIT):
    """Sections matching `query`, with WHERE each one matched.

    Runs server-side because the answer needs item text, and the alternative is
    a client fetching 210 pools to filter them: the library is already parsed
    and warm here (routes.py warms it at boot), so this is a walk over memory.

    `where` is the useful half of the result — 'name' means the slug or label
    says it, 'item' means something inside does. A user hunting a disco finds
    it in wardrobe/historical's items, and being told THAT is what turns a
    dead end into a pick.
    """
    terms = _search_terms(query)
    if not terms:
        return []
    scope = scope if scope in SEARCH_SCOPES else "both"
    hits = []
    for slug in lib.section_slugs():
        try:
            section = lib.load_section(slug)
        except pl.PromptLibError:
            continue
        name_hay = f"{slug} {section.label or ''} {section.description or ''}".lower()
        name_hit = all(term.search(name_hay) for term in terms)
        item_names = []
        if scope != "name":
            for item in section.items:
                if item.hidden:
                    continue
                hay = f"{item.name} {item.text} {item.text_short or ''}".lower()
                if all(term.search(hay) for term in terms):
                    item_names.append(item.name)
        if scope == "name" and not name_hit:
            continue
        if scope == "text" and not item_names:
            continue
        if not name_hit and not item_names:
            continue
        where = []
        if name_hit:
            where.append("name")
        if item_names:
            where.append("item")
        hits.append(
            {
                "slug": slug,
                "label": section.label or "",
                "item_count": len(section.items),
                "where": where,
                "matches": len(item_names),
                "samples": item_names[:SEARCH_SAMPLES],
                # a name hit is what the user was looking for; an item hit is
                # where it turned out to live, so name hits sort first
                "_rank": (0 if name_hit else 1, -len(item_names), slug),
            }
        )
    hits.sort(key=lambda hit: hit.pop("_rank"))
    return hits[:limit]


@_guarded
def handle_search(lib, payload):
    """GET /mrln/prompt/search?q=&scope=name|text|both&limit=

    Answers {query, scope, results, truncated} — the picker's filter row. An
    empty query answers an empty list rather than the whole library: 210
    sections is the problem, not the answer."""
    query = str(payload.get("q") or payload.get("query") or "")
    scope = str(payload.get("scope") or "both").strip().lower()
    try:
        limit = max(1, min(int(payload.get("limit") or SEARCH_LIMIT), SEARCH_LIMIT))
    except (TypeError, ValueError):
        limit = SEARCH_LIMIT
    results = search_sections(lib, query, scope=scope, limit=limit + 1)
    truncated = len(results) > limit
    return 200, {
        "query": query,
        "scope": scope if scope in SEARCH_SCOPES else "both",
        "results": results[:limit],
        "truncated": truncated,
    }
