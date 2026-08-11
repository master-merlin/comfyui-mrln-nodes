"""Shareable bundles: export a template (or section) together with every
USER-tier section it draws from, import the bundle on another install.

Embed rule: user-tier section files travel VERBATIM (a thin extend diff
stays a thin diff), factory-pure sections are only listed by slug — the
recipient's pack ships those, and pinning factory snapshots into their
user tier would freeze them against factory updates. LoRA-carrying items
are summarized in a manifest (file name + Civitai AIR urn when known) so
the importer can offer the existing download-by-AIR healing.

Import validates EVERYTHING with the real parsers before writing anything,
then writes through Library.save_user only (slug validation + user tier
only). Existing user files are kept unless overwrite is requested;
identical content is skipped silently so re-imports stay idempotent.
"""

import json

from .errors import SchemaError, SectionNotFoundError
from .library import validate_slug
from .schema import parse_section, parse_template

BUNDLE_FORMAT = "mrln-bundle"
BUNDLE_VERSION = 1


def _canonical(lib, kind, slug):
    """The slug a lookup actually lands on (follows the alias chain)."""
    entries = lib._scan(kind)
    if slug in entries:
        return slug
    target = lib._alias_target(kind, slug, lambda s: s in entries)
    return target if target is not None else slug


def _tier_raw(lib, kind, slug):
    """The raw JSON of the file that currently answers for `slug`."""
    entry = lib._scan(kind).get(_canonical(lib, kind, slug))
    if entry is None:
        raise SchemaError(slug, f"no {kind} file for slug '{slug}'")
    return json.loads(entry.path.read_text(encoding="utf-8"))


def _user_raw(lib, kind, slug):
    """The raw user-tier file for `slug`, or None (no file / unreadable)."""
    path = (lib.user_root / kind / f"{slug}.json") if lib.user_root else None
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _factory_raw(lib, kind, slug):
    path = lib.factory_root / kind / f"{slug}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _expand_ref(lib, ref):
    """Concrete sections in scope of a leaf-or-folder ref, leaf winning on
    a name that is both (mirrors Library.scope_items precedence)."""
    ref = str(ref or "").strip("/")
    all_slugs = set(lib.section_slugs())
    if ref not in all_slugs:
        matching = sorted(s for s in all_slugs if s.startswith(ref + "/"))
        if matching:
            return [lib.load_section(s) for s in matching]
    return [lib.load_section(ref)]  # alias-tolerant; raises when truly missing


def section_closure(lib, refs):
    """(sections_by_slug, missing_refs): every concrete section the refs can
    reach on this install, transitively through item child slots."""
    sections, missing, seen = {}, [], set()
    queue = [str(r or "") for r in refs]
    while queue:
        ref = queue.pop().strip("/")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        try:
            expanded = _expand_ref(lib, ref)
        except SectionNotFoundError:
            missing.append(ref)
            continue
        for section in expanded:
            if section.slug in sections:
                continue
            sections[section.slug] = section
            for item in section.items:
                queue.extend(child.ref for child in item.slots)
    return sections, sorted(missing)


def template_refs(tpl):
    refs = [slot.ref for slot in tpl.slots]
    refs.extend(slot.ref for variant in tpl.variants for slot in variant.slots)
    return refs


def _lora_manifest(sections):
    """LoRA files the bundled content draws, with their AIR urns when the
    item comment carries one — the importer's download-by-AIR hook."""
    out, seen = [], set()
    for slug in sorted(sections):
        for item in sections[slug].items:
            data = item.data or {}
            lora = str(data.get("lora") or "")
            if not lora or lora in seen:
                continue
            seen.add(lora)
            entry = {"file": lora, "section": slug, "item": item.name}
            comment = str(data.get("comment") or "").strip()
            if comment.lower().startswith("urn:air:"):
                entry["air"] = comment
            out.append(entry)
    return out


def export_bundle(lib, kind, slug):
    """A self-contained shareable bundle for one template or section."""
    if kind == "templates":
        slug = _canonical(lib, "templates", validate_slug(slug))
        tpl = lib.load_template(slug)
        sections, missing = section_closure(lib, template_refs(tpl))
        primary = {"kind": "template", "template": _tier_raw(lib, "templates", slug)}
    elif kind == "sections":
        slug = _canonical(lib, "sections", validate_slug(slug))
        lib.load_section(slug)  # existence + parse check
        sections, missing = section_closure(lib, [slug])
        primary = {"kind": "section"}
    else:
        raise SchemaError(kind, "bundle kind must be 'templates' or 'sections'")
    embedded, factory_refs = {}, []
    for sec_slug in sorted(sections):
        if lib.tier_of("sections", sec_slug) == "user":
            embedded[sec_slug] = _tier_raw(lib, "sections", sec_slug)
        else:
            factory_refs.append(sec_slug)
    if kind == "sections" and not embedded:
        raise SchemaError(
            slug,
            f"'{slug}' is pure factory content — the pack already ships it, "
            "there is nothing user-tier to share",
        )
    return {
        "format": BUNDLE_FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "slug": slug,
        **primary,
        "sections": embedded,
        "factory_refs": factory_refs,
        "missing_refs": missing,
        "loras": _lora_manifest(sections),
    }


def import_bundle(lib, bundle, *, slug=None, overwrite=False, dry_run=False):
    """Validate a bundle fully, then write it into the user tier.

    Sections keep their slugs (item refs point at them); an existing user
    file is skipped unless `overwrite`. The template lands on `slug`
    (default: the bundle's own) — an occupied user slug needs `overwrite`,
    and the report's `needs_overwrite` tells the UI to ask. `dry_run`
    returns the exact plan without touching a file.
    """
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise SchemaError("bundle", "not an MRLN bundle (missing format marker)")
    version = bundle.get("bundle_version")
    if not isinstance(version, int) or not 1 <= version <= BUNDLE_VERSION:
        raise SchemaError(
            "bundle",
            f"unsupported bundle_version {version!r} — this pack reads 1..{BUNDLE_VERSION}; "
            "update ComfyUI-MRLN-Nodes to import newer bundles",
        )
    kind = bundle.get("kind")
    if kind not in ("template", "section"):
        raise SchemaError("bundle", f"unknown bundle kind {kind!r}")
    raw_sections = bundle.get("sections") or {}
    if not isinstance(raw_sections, dict):
        raise SchemaError("bundle", "'sections' must be an object of slug -> file content")
    for sec_slug, data in raw_sections.items():
        validate_slug(str(sec_slug))
        if not isinstance(data, dict):
            raise SchemaError(sec_slug, "section content must be a JSON object")
        parse_section(data, str(sec_slug), f"bundle:{sec_slug}")
    tpl_raw, target_slug = None, None
    if kind == "template":
        tpl_raw = bundle.get("template")
        if not isinstance(tpl_raw, dict):
            raise SchemaError("bundle", "template bundle carries no 'template' object")
        target_slug = validate_slug(str(slug or bundle.get("slug") or ""))
        parse_template(tpl_raw, target_slug, f"bundle:{target_slug}")
    elif kind == "section" and not raw_sections:
        raise SchemaError("bundle", "section bundle embeds no sections")

    report = {
        "written": [],
        "skipped": [],
        "missing_factory": [],
        "loras": [e for e in (bundle.get("loras") or []) if isinstance(e, dict)],
        "dry_run": bool(dry_run),
    }
    for ref in bundle.get("factory_refs") or []:
        if str(ref) in raw_sections:
            continue  # the bundle itself provides it
        try:
            _expand_ref(lib, str(ref))
        except SectionNotFoundError:
            report["missing_factory"].append(str(ref))
    for sec_slug in sorted(raw_sections):
        data = raw_sections[sec_slug]
        existing = _user_raw(lib, "sections", sec_slug)
        if existing == data:
            report["skipped"].append({"kind": "section", "slug": sec_slug, "reason": "identical"})
            continue
        if existing is not None and not overwrite:
            report["skipped"].append({"kind": "section", "slug": sec_slug, "reason": "exists"})
            report["needs_overwrite"] = True
            continue
        if not dry_run:
            lib.save_user("sections", sec_slug, data)
        extends = (lib.factory_root / "sections" / f"{sec_slug}.json").is_file()
        report["written"].append({"kind": "section", "slug": sec_slug, "extends_factory": extends})
    if kind == "template":
        existing = _user_raw(lib, "templates", target_slug)
        shadows = (lib.factory_root / "templates" / f"{target_slug}.json").is_file()
        identical = existing == tpl_raw or (
            existing is None and shadows and _factory_raw(lib, "templates", target_slug) == tpl_raw
        )
        if identical:
            report["skipped"].append(
                {"kind": "template", "slug": target_slug, "reason": "identical"}
            )
        elif existing is not None and not overwrite:
            report["skipped"].append({"kind": "template", "slug": target_slug, "reason": "exists"})
            report["needs_overwrite"] = True
        else:
            if not dry_run:
                lib.save_user("templates", target_slug, tpl_raw)
            report["written"].append(
                {"kind": "template", "slug": target_slug, "shadows_factory": shadows}
            )
        report["template_slug"] = target_slug
    return report
