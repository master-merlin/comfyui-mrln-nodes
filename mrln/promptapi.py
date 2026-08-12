"""HTTP API backing the Prompt Composer sidebar panel.

Two layers, deliberately separated:
- pure handlers `handle_*(lib, payload) -> (status, body_dict)` — no ComfyUI
  and no aiohttp imports, so pytest exercises them directly;
- `register_routes()` — the only code touching `server`/`aiohttp`, imported
  lazily inside the function so this module always imports cleanly
  (headless, pytest, non-ComfyUI). It soft-fails by returning False.

Security: saves go through Library.save_user only (slug validation +
parse-before-write, user tier only); no request string reaches the
filesystem any other way. JSON in, JSON out; errors are
{"error": ..., "remediation": ...} with a matching HTTP status.
"""

import asyncio
import contextlib
import json
import re
import threading
import traceback

from . import promptlib as pl
from .pack import logger

MAX_BODY_BYTES = 1 << 20  # library files are small; refuse bigger bodies


class ApiError(pl.PromptLibError):
    """Bad request parameter; message is the full user-facing text."""


def _require_str(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f"missing required parameter '{key}'")
    return value.strip()


def _guarded(handler):
    def run(lib, payload):
        try:
            return handler(lib, payload)
        except (pl.SectionNotFoundError, pl.TemplateNotFoundError) as exc:
            return 404, {
                "error": str(exc),
                "remediation": "list valid slugs via GET /mrln/prompt/library",
            }
        except pl.PromptLibError as exc:
            return 400, {
                "error": str(exc),
                "remediation": "fix the request and retry (the message names the field)",
            }
        except Exception:
            logger.warning("MRLN prompt API failure:\n%s", traceback.format_exc())
            return 500, {
                "error": "internal error in the MRLN prompt API",
                "remediation": "see the ComfyUI server log for the traceback",
            }

    run.__name__ = handler.__name__
    return run


def _raw_file(lib, kind, slug):
    entries = lib._scan(kind)
    entry = entries.get(slug)
    if entry is None:
        target = lib._alias_target(kind, slug, lambda s: s in entries)
        if target is not None:
            return _raw_file(lib, kind, target)
        not_found = pl.SectionNotFoundError if kind == "sections" else pl.TemplateNotFoundError
        raise not_found(slug, list(entries))
    with open(entry.path, encoding="utf-8") as fh:
        return json.load(fh)


def _factory_raw(lib, kind, slug):
    """The factory file raw when a user file shadows/extends it, else None —
    the composer diffs edits against this baseline for extend-mode saves."""
    if lib._scan(kind).get(slug) is None or lib.tier_of(kind, slug) != "user":
        return None
    path = lib.factory_root / kind / f"{slug}.json"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _slot_detail(slot, missing_refs=()):
    return {
        "id": slot.id,
        "ref": slot.ref,
        "label": slot.label,
        "default": slot.default,
        "allow_empty": slot.allow_empty,
        "empty_weight": slot.empty_weight,
        "emphasis": slot.emphasis,
        "tags_any": list(slot.tags_any),
        "tags_none": list(slot.tags_none),
        "missing": slot.ref in missing_refs,
    }


def _pool(lib, ref):
    pool = []
    for qualified, section, item in lib.scope_items(ref):
        entry = {
            "name": qualified,
            "text": item.text,
            "negative": item.negative,
            "weight": item.weight,
            "section_slug": section.slug,
            "tier": item.origin or lib.tier_of("sections", section.slug),
        }
        if item.data and item.data.get("lora"):
            entry["lora"] = str(item.data["lora"])
            base = pl.lora_base_family(item.data)
            if base:  # the pill names the target model, so a mismatch is visible
                entry["base"] = base
        pool.append(entry)
    return pool


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


def _kv_map(payload, key):
    value = payload.get(key) or ""
    if isinstance(value, str):
        return pl.parse_kv_lines(value, what=key)
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    raise ApiError(f"'{key}' must be a string of name=value lines or an object")


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


def _resolved_slot_json(s):
    return {
        "id": s.id,
        "key": s.key,
        "label": s.label,
        "ref": s.ref,
        "section_slug": s.section_slug,
        "item": s.item_name,
        "text": s.text,
        "negative": s.negative,
        "random": s.random,
        "fixed_first": s.fixed_first,
        "emphasis": s.emphasis,
        "seed_used": s.seed_used,
        "tier": s.tier,
        "omitted": s.item_name is None,
        "missing": s.missing,
        "inline": s.inline,
        "stale_note": s.stale_note,
        "children": [_resolved_slot_json(c) for c in s.children],
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


def _write_json_atomic(path, data, *, ensure_ascii=True):
    """Sibling-.tmp + os.replace — the Library.save_user pattern: a crash
    mid-write can never truncate the live file (settings.json holds API
    keys; library files are the persistence truth)."""
    import os

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=ensure_ascii, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


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


_DECOMPOSE_SYSTEM = (
    "You decompose an image-generation prompt into ordered fragments and map "
    "each fragment onto a catalog of prompt-library items.\n"
    "Answer with STRICT JSON only — no prose, no markdown fences:\n"
    '{"fragments": [{"text": "<verbatim fragment>", "section": "<slug or null>", '
    '"item": "<item name or null>", "name": "<kebab-case name or null>", '
    '"rewrite": "<polished text or null>", "short": "<compact tags or null>"}]}\n'
    "Rules:\n"
    "- The fragments joined in order must cover the whole input; 'text' keeps "
    "the original wording verbatim.\n"
    "- Assign section+item ONLY when the fragment expresses the same content "
    "as that item; otherwise use null for both.\n"
    "- Only use section slugs and item names from the catalog below. Never "
    "invent names for 'section'/'item'.\n"
    "- For UNMATCHED fragments (section null) also deliver library-grade "
    "enrichment: 'name' = a short kebab-case name for the fragment as a "
    "library item; 'rewrite' = the fragment rewritten as one polished, "
    "self-contained, renderable description — expand shorthand, fix grammar, "
    "keep every stated fact, add nothing new; 'short' = the same content as "
    "a compact comma-separated tag phrase. Matched fragments: null for all "
    "three.\n"
)


def _decompose_catalog(lib, template_type, budget=9000):
    """'slug: item, item, …' lines over the drawable library (suits-filtered
    like random pools). Shrinks to stay within budget so small local models
    keep headroom for the actual prompt."""

    def build(max_names):
        lines = []
        for slug in lib.section_slugs():
            try:
                section = lib.load_section(slug)
            except Exception:
                continue
            if template_type and section.suits and not (set(section.suits) & set(template_type)):
                continue
            names = [i.name for i in section.items if not i.hidden]
            if not names:
                continue
            shown = names if max_names is None else names[:max_names]
            if shown:
                tail = "" if len(shown) == len(names) else ", …"
                lines.append(f"{slug}: {', '.join(shown)}{tail}")
            else:
                lines.append(f"{slug}: …")
        return "\n".join(lines)

    for max_names in (None, 8, 3, 0):
        catalog = build(max_names)
        if len(catalog) <= budget:
            return catalog
    return catalog  # slug-only lines — as small as it gets


def _extract_json(text):
    """The JSON object out of an LLM answer — tolerant of <think> blocks,
    fences and surrounding prose."""
    text = re.sub(r"<think>.*?</think>", "", str(text), flags=re.DOTALL)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise RuntimeError("no JSON object in the LLM response")
    return json.loads(text[start : end + 1])


def _validate_llm_fragments(lib, raw_fragments):
    """LLM-proposed fragments -> the report contract, every assignment
    checked against the real library (score_match doubles as validator);
    invalid items demote to a suggestion when the section at least exists."""
    fragments = []
    for raw in raw_fragments:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        entry = {"text": text, "match": None}
        section = raw.get("section")
        item = raw.get("item")
        if section and item:
            score = pl.score_match(lib, text, str(section), str(item))
            if score is not None:
                entry["match"] = {"section": str(section), "item": str(item), "score": score}
        if entry["match"] is None and section:
            try:
                lib.load_section(str(section))
                entry["suggestion"] = {"section": str(section), "score": 0.0}
            except Exception:
                pass
        if entry["match"] is None:
            # library-grade enrichment for the residue: the raw fragment makes
            # a poor item text — the LLM's rewrite becomes the new item
            name = re.sub(r"[^a-z0-9._-]+", "-", str(raw.get("name") or "").strip().lower())
            name = name.strip("-.")[:60]
            rewrite = str(raw.get("rewrite") or "").strip()
            short = str(raw.get("short") or "").strip()
            if name:
                entry["suggested_name"] = name
            if rewrite:
                entry["rewrite"] = rewrite
            if short:
                entry["short"] = short
        fragments.append(entry)
    return fragments


def _llm_decompose(lib, prompt_text, template_type, engine, backend, model, timeout, base):
    import hashlib

    system = _DECOMPOSE_SYSTEM + "\nCatalog (section: items):\n"
    system += _decompose_catalog(lib, template_type)
    if engine == "hybrid":
        hints = []
        for fragment in base["fragments"]:
            match = fragment.get("match")
            if match:
                hints.append(
                    f'- "{fragment["text"]}" -> {match["section"]} / {match["item"]}'
                    f" (score {match['score']})"
                )
            elif fragment.get("suggestion"):
                hints.append(
                    f'- "{fragment["text"]}" -> nearest section'
                    f" {fragment['suggestion']['section']}, no item matched"
                )
        if hints:
            system += (
                "\n\nA programmatic token-matching pass already suggested the "
                "following (verify, correct or reject each):\n" + "\n".join(hints)
            )
    digest = hashlib.sha256(prompt_text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big") & 0x7FFFFFFF
    text = llm_chat(
        lib,
        backend=backend,
        model=model,
        system=system,
        prompt=prompt_text,
        temperature=0.1,
        seed=seed,
        max_tokens=6000,  # rewrites ride along — the old 4000 truncated them
        timeout=timeout,
    )
    data = _extract_json(text)
    raw_fragments = data.get("fragments")
    if not isinstance(raw_fragments, list) or not raw_fragments:
        raise RuntimeError("the LLM returned no fragments")
    fragments = _validate_llm_fragments(lib, raw_fragments)
    if not fragments:
        raise RuntimeError("the LLM output held no usable fragments")
    matched = sum(1 for f in fragments if f["match"])
    return {
        "engine": engine,
        "fragments": fragments,
        "matched": matched,
        "unmatched": len(fragments) - matched,
    }


@_guarded
def handle_decompose(lib, payload):
    prompt_text = _require_str(payload, "prompt")
    raw_type = payload.get("type") or []
    if isinstance(raw_type, str):
        raw_type = [t.strip() for t in raw_type.split(",") if t.strip()]
    if not isinstance(raw_type, list):
        raise ApiError("'type' must be a list or comma-separated string")
    template_type = tuple(str(t) for t in raw_type)
    engine = str(payload.get("engine") or "programmatic")
    engine = {"heuristic": "programmatic", "ollama": "llm"}.get(engine, engine)
    if engine not in pl.ENGINES:
        raise ApiError(f"unknown engine '{engine}' (engines: {', '.join(pl.ENGINES)})")
    # the programmatic pass always runs: it IS the result, the hybrid
    # context, and the fallback when a backend dies mid-decompose
    report = pl.decompose(lib, prompt_text, template_type=template_type, engine="programmatic")
    if engine != "programmatic":
        backend = str(payload.get("backend") or "ollama")
        model = str(payload.get("model") or "")
        timeout = payload.get("timeout", 90)
        if not isinstance(timeout, int) or not 5 <= timeout <= 600:
            raise ApiError("'timeout' must be an integer between 5 and 600 seconds")
        try:
            report = _llm_decompose(
                lib, prompt_text, template_type, engine, backend, model, timeout, report
            )
        except Exception as exc:
            report["llm_error"] = (
                f"{engine} engine failed ({exc}) — showing the programmatic result"
            )
    report["fingerprint"] = lib.fingerprint()
    return 200, report


def _profiles_file(root):
    """Raw 'profiles' map of one tier's profiles.json ({} if absent/broken)."""
    if not root:
        return {}
    path = root / "profiles.json"
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        profiles = data.get("profiles")
        return profiles if isinstance(profiles, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@_guarded
def handle_profile(lib, payload):
    name = _require_str(payload, "name")
    factory = _profiles_file(lib.factory_root).get(name)
    user = _profiles_file(lib.user_root).get(name)
    merged = lib.pack_profiles().get(name)
    if merged is None and factory is None and user is None:
        return 404, {
            "error": f"profile '{name}' not found",
            "remediation": "list names via GET /mrln/prompt/library",
        }
    return 200, {"name": name, "merged": merged or {}, "factory": factory, "user": user}


@_guarded
def handle_save_profile(lib, payload):
    """Write (or with data=null delete) a USER-tier profile entry — the
    overlay above factory profiles.json. The Composer's Profiles editor
    calls this; users need their own system prompts per target model."""
    if lib.user_root is None:
        return 400, {
            "error": "no user library root configured",
            "remediation": "set MRLN_PROMPT_DIR or run inside ComfyUI",
        }
    name = _require_str(payload, "name")
    if name == pl.STANDARD or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ApiError(
            f"profile name '{name}' must be lowercase-kebab and not 'standard' (reserved)"
        )
    data = payload.get("data")
    with _SETTINGS_LOCK:
        user_profiles = dict(_profiles_file(lib.user_root))
        if data is None:
            if name not in user_profiles:
                return 404, {
                    "error": f"no user-tier entry for profile '{name}'",
                    "remediation": "only user-tier entries can be deleted; factory "
                    "profiles are read-only",
                }
            user_profiles.pop(name)
            action = "deleted"
        else:
            if not isinstance(data, dict):
                raise ApiError("'data' must be an object (or null to delete the user entry)")
            render_over = data.get("render") or {}
            if not isinstance(render_over, dict):
                raise ApiError("'render' must be an object")
            if "format" in render_over and render_over["format"] not in pl.FORMATS:
                raise ApiError(f"unknown render format '{render_over['format']}'")
            if "text_length" in render_over and render_over["text_length"] not in pl.TEXT_LENGTHS:
                raise ApiError("unknown text_length (lengths: long, short)")
            user_profiles[name] = data
            action = "saved"
        lib.user_root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            lib.user_root / "profiles.json", {"profiles": user_profiles}, ensure_ascii=False
        )
    lib.invalidate()
    return 200, {
        "ok": True,
        "name": name,
        "action": action,
        "profiles": sorted(lib.pack_profiles()),
    }


# settings.json and profiles.json are read-modify-write and the handlers
# run on the executor thread pool — one lock so two overlapping saves
# (e.g. the Settings tab's per-provider Save buttons) never drop a write
_SETTINGS_LOCK = threading.Lock()


def _settings_path(lib):
    return lib.user_root / "settings.json"


def _read_settings(lib):
    if lib.user_root is None:
        return {}
    path = _settings_path(lib)
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_LMSTUDIO_URL = "http://127.0.0.1:1234"


def read_settings(lib):
    """Public settings reader (keys stay server-side; callers must never
    echo secrets)."""
    return _read_settings(lib)


def _llm_settings(settings):
    llm = settings.get("llm")
    return llm if isinstance(llm, dict) else {}


@_guarded
def handle_settings(lib, payload):
    # secrets are NEVER echoed back — only whether one is stored; local
    # backend URLs are not secrets and round-trip for the settings UI
    settings = _read_settings(lib)
    llm = _llm_settings(settings)
    keys = settings.get("llm_api_keys") or {}
    return 200, {
        "civitai_key_set": bool(settings.get("civitai_api_key")),
        "llm": {
            "ollama_url": llm.get("ollama_url") or DEFAULT_OLLAMA_URL,
            "lmstudio_url": llm.get("lmstudio_url") or DEFAULT_LMSTUDIO_URL,
        },
        "llm_keys_set": {p: bool(keys.get(p)) for p in CLOUD_PROVIDERS},
    }


@_guarded
def handle_save_settings(lib, payload):
    if lib.user_root is None:
        return 400, {
            "error": "no user library root configured",
            "remediation": "set MRLN_PROMPT_DIR or run inside ComfyUI",
        }
    with _SETTINGS_LOCK:
        settings = _read_settings(lib)
        if "civitai_api_key" in payload:
            raw = payload.get("civitai_api_key")
            if not isinstance(raw, str):
                raise ApiError("'civitai_api_key' must be a string")
            if raw.strip():
                settings["civitai_api_key"] = raw.strip()
            else:
                settings.pop("civitai_api_key", None)  # empty clears
        if "llm" in payload:
            raw_llm = payload.get("llm")
            if not isinstance(raw_llm, dict):
                raise ApiError("'llm' must be an object")
            llm = _llm_settings(settings)
            for key in ("ollama_url", "lmstudio_url"):
                if key in raw_llm:
                    value = raw_llm[key]
                    if not isinstance(value, str):
                        raise ApiError(f"'llm.{key}' must be a string")
                    value = value.strip().rstrip("/")
                    if value:
                        llm[key] = value
                    else:
                        llm.pop(key, None)  # empty reverts to the default
            settings["llm"] = llm
        if "llm_api_keys" in payload:
            raw_keys = payload.get("llm_api_keys")
            if not isinstance(raw_keys, dict):
                raise ApiError("'llm_api_keys' must be an object of provider -> key")
            keys = settings.get("llm_api_keys")
            keys = keys if isinstance(keys, dict) else {}
            for provider, value in raw_keys.items():
                if provider not in CLOUD_PROVIDERS:
                    raise ApiError(
                        f"unknown provider '{provider}' (have: {', '.join(CLOUD_PROVIDERS)})"
                    )
                if not isinstance(value, str):
                    raise ApiError(f"'llm_api_keys.{provider}' must be a string")
                if value.strip():
                    keys[provider] = value.strip()
                else:
                    keys.pop(provider, None)  # empty clears, like the Civitai key
            settings["llm_api_keys"] = keys
        lib.user_root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(_settings_path(lib), settings)
    keys = settings.get("llm_api_keys") or {}
    return 200, {
        "ok": True,
        "civitai_key_set": bool(settings.get("civitai_api_key")),
        "llm_keys_set": {p: bool(keys.get(p)) for p in CLOUD_PROVIDERS},
    }


# Curated pull suggestions for the Enhance node's model dropdown — small
# instruct models that rewrite prompts well. Ollama downloads a pick via
# /mrln/prompt/llm-pull. Edit freely; installed models are filtered out.
SUGGESTED_OLLAMA_MODELS = (
    "gemma3:12b",
    "gemma3:4b",
    "qwen3:14b",
    "qwen3:8b",
    "llama3.2:3b",
    "phi4:14b",
    "mistral-small:24b",
)


# Curated cloud model suggestions for the model dropdowns; the FIRST entry
# per provider is the DEFAULT_CLOUD_MODELS fallback. Edit freely.
CLOUD_MODEL_SUGGESTIONS = {
    "anthropic": ("claude-haiku-4-5-20251001", "claude-sonnet-5"),
    "openai": ("gpt-4o-mini", "gpt-4o"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro"),
    "openrouter": (),
}


@_guarded
def handle_llm_validate(lib, payload):
    """Local providers: ping the backend and list installed models. Cloud
    providers: no network — answer with the stored-key state and curated
    model suggestions. Powers the green checkmarks in Settings and every
    model dropdown (Enhance node, De-compose tab)."""
    provider = _require_str(payload, "provider")
    settings = _read_settings(lib)
    llm = _llm_settings(settings)
    if provider in CLOUD_PROVIDERS:
        key_set = bool((settings.get("llm_api_keys") or {}).get(provider))
        return 200, {
            "state": "ok",
            "provider": provider,
            "models": [],
            "suggested": list(CLOUD_MODEL_SUGGESTIONS.get(provider, ())),
            "key_set": key_set,
        }
    import urllib.error
    import urllib.request

    if provider == "ollama":
        url = f"{llm.get('ollama_url') or DEFAULT_OLLAMA_URL}/api/tags"
    elif provider == "lmstudio":
        url = f"{llm.get('lmstudio_url') or DEFAULT_LMSTUDIO_URL}/v1/models"
    else:
        raise ApiError(
            f"unknown provider '{provider}' (have: ollama, lmstudio, {', '.join(CLOUD_PROVIDERS)})"
        )
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-MRLN-Nodes"})
        with urllib.request.urlopen(request, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # URLError / timeout / bad JSON — all mean "not reachable"
        return 502, {
            "error": f"{provider} unreachable at {url}: {exc}",
            "remediation": "start the server or fix the URL, then Validate again",
        }
    if provider == "ollama":
        models = sorted(m.get("name", "") for m in data.get("models") or [] if m.get("name"))
        stems = {m.split(":")[0] for m in models}
        suggested = [
            s for s in SUGGESTED_OLLAMA_MODELS if s not in models and s.split(":")[0] not in stems
        ]
    else:
        models = sorted(m.get("id", "") for m in data.get("data") or [] if m.get("id"))
        suggested = []  # LM Studio has no pull API — install via its own UI
    return 200, {"state": "ok", "provider": provider, "models": models, "suggested": suggested}


# model name -> {"status": "pulling"|"done"|"error", "detail": str}; module
# scope like _ENHANCE_CACHE — worker threads write, the poll endpoint reads.
_PULL_STATUS = {}


def _pull_worker(url, model):
    import urllib.request

    try:
        request = urllib.request.Request(
            f"{url}/api/pull",
            data=json.dumps({"model": model, "stream": False}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "ComfyUI-MRLN-Nodes"},
        )
        # a multi-GB pull is legitimately slow — generous cap, not the 5s ping
        with urllib.request.urlopen(request, timeout=3600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _PULL_STATUS[model] = {"status": "done", "detail": str(data.get("status") or "success")}
    except Exception as exc:
        _PULL_STATUS[model] = {"status": "error", "detail": str(exc)}


@_guarded
def handle_llm_pull(lib, payload):
    """POST starts an Ollama model download in the background; GET (same
    route) polls its status. The dropdown suggestion click lands here.
    Only JSON `true` counts as start — GET query values are strings, so a
    polling (or cross-site) GET can never kick off a pull."""
    model = _require_str(payload, "model")
    if payload.get("start") is True:
        current = _PULL_STATUS.get(model)
        if current and current.get("status") == "pulling":
            return 200, {"model": model, "status": "pulling", "detail": "already running"}
        llm = _llm_settings(_read_settings(lib))
        url = llm.get("ollama_url") or DEFAULT_OLLAMA_URL
        _PULL_STATUS[model] = {"status": "pulling", "detail": ""}
        threading.Thread(target=_pull_worker, args=(url, model), daemon=True).start()
        return 200, {"model": model, "status": "pulling", "detail": "started"}
    status = _PULL_STATUS.get(model) or {"status": "unknown", "detail": "no pull started"}
    return 200, {"model": model, **status}


_ECO_MAP = (
    ("flux", "flux1"),
    ("sdxl", "sdxl"),
    ("sd 3", "sd3"),
    ("sd 2", "sd2"),
    ("sd 1", "sd1"),
    ("pony", "pony"),
    ("illustrious", "illustrious"),
    ("noobai", "noobai"),
)


def _civitai_summary(resp):
    """Pure: pick trigger + AIR out of a Civitai model-version response."""
    words = [str(w).strip() for w in resp.get("trainedWords") or [] if str(w).strip()]
    air = resp.get("air")
    if not air and resp.get("modelId") and resp.get("id"):
        base = str(resp.get("baseModel") or "").lower()
        eco = next((eco for frag, eco in _ECO_MAP if frag in base), None)
        eco = eco or (base.split() or ["model"])[0] or "model"
        mtype = str((resp.get("model") or {}).get("type") or "LORA").lower()
        air = f"urn:air:{eco}:{mtype}:civitai:{resp['modelId']}@{resp['id']}"
    return {
        "trigger": words[0] if words else None,
        "trained_words": words,
        "air": air,
        "model_name": (resp.get("model") or {}).get("name"),
        "version_name": resp.get("name"),
    }


_HASH_CACHE = {}


def _sha256_of(path):
    import hashlib
    import os

    stat = os.stat(path)
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _HASH_CACHE.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    hexdigest = digest.hexdigest()
    _HASH_CACHE[str(path)] = (key, hexdigest)
    return hexdigest


def _resolve_lora_file(name):
    """(real_name, path) via folder_paths, tolerant of slash/case — or None
    outside ComfyUI / for unknown names."""
    try:
        import folder_paths
    except ImportError:
        return None
    available = folder_paths.get_filename_list("loras")

    def norm(n):
        return n.replace("\\", "/").lower()

    real = next((c for c in available if c == name), None) or next(
        (c for c in available if norm(c) == norm(name)), None
    )
    if real is None:
        return ("", None)
    return (real, folder_paths.get_full_path("loras", real))


@_guarded
def handle_lora_civitai(lib, payload):
    """Trigger word + AIR from Civitai, keyed by the file's SHA256. Works
    keyless for public models; the stored API key (user-tier settings.json,
    never echoed) unlocks restricted ones."""
    name = _require_str(payload, "name")
    resolved = _resolve_lora_file(name)
    if resolved is None:
        return 400, {
            "error": "Civitai lookup runs inside a running ComfyUI only",
            "remediation": "type the catchword manually",
        }
    real, path = resolved
    if path is None:
        return 404, {
            "error": f"LoRA '{name}' not found in your loras folder",
            "remediation": "refresh the list or pick another file",
        }
    import urllib.error
    import urllib.request

    digest = _sha256_of(path)
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    key = str(_read_settings(lib).get("civitai_api_key") or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"https://civitai.com/api/v1/model-versions/by-hash/{digest}", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, {
                "error": f"'{real}' is not on Civitai (hash {digest[:12]}…)",
                "remediation": "type the catchword manually",
            }
        return 502, {
            "error": f"Civitai answered HTTP {exc.code}",
            "remediation": "check the API key in the Composer's Library tab, or retry later",
        }
    except Exception as exc:  # URLError / timeout / bad JSON
        return 502, {
            "error": f"Civitai unreachable: {exc}",
            "remediation": "check your network and retry",
        }
    out = _civitai_summary(data)
    out["name"] = real
    return 200, out


# -- LLM backends (shared by the Enhance node and the LLM de-composer) -------

CLOUD_PROVIDERS = ("anthropic", "openai", "gemini", "openrouter")

# editable defaults — used when the model widget is empty on a cloud backend
DEFAULT_CLOUD_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "openrouter": "",  # a router needs an explicit model choice
}


def _post_json(url, payload, timeout, headers=None):
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ComfyUI-MRLN-Nodes",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cloud_request(backend, key, model, system, prompt, temperature, seed, max_tokens):
    """(url, headers, payload, extract) for a cloud chat call — pure, so
    tests cover the request shapes without any network."""
    if backend == "anthropic":
        return (
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": min(float(temperature), 1.0),  # anthropic range is 0..1
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            lambda d: "".join(
                b.get("text", "") for b in d.get("content") or [] if b.get("type") == "text"
            ),
        )
    if backend == "gemini":
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": key},
            {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": float(temperature),
                    "maxOutputTokens": max_tokens,
                    "seed": seed,
                },
            },
            lambda d: "".join(
                p.get("text", "")
                for c in (d.get("candidates") or [])[:1]
                for p in ((c.get("content") or {}).get("parts") or [])
            ),
        )
    base = (
        "https://openrouter.ai/api/v1" if backend == "openrouter" else "https://api.openai.com/v1"
    )
    return (
        f"{base}/chat/completions",
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(temperature),
            "seed": seed,
            "max_tokens": max_tokens,
            "stream": False,
        },
        lambda d: str(((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""),
    )


def llm_chat(
    lib,
    *,
    backend,
    model,
    system,
    prompt,
    temperature,
    seed,
    max_tokens,
    timeout,
    free_vram="after call",
):
    """One entry point for every LLM backend, local and cloud. Returns the
    raw completion text; raises RuntimeError with an actionable message on
    any failure (callers decide pass-through vs raise). Keys live in the
    user tier's settings.json and are never echoed anywhere."""
    settings = _read_settings(lib)
    llm = _llm_settings(settings)
    model = str(model or "").strip()
    if backend == "ollama":
        if not model:
            raise RuntimeError(
                "Ollama needs a model name — the node's dropdown lists installed models"
            )
        url = llm.get("ollama_url") or DEFAULT_OLLAMA_URL
        data = _post_json(
            f"{url}/api/chat",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                # 0 unloads right after the call, -1 pins the model loaded
                "keep_alive": {"after call": 0, "always keep": -1}.get(free_vram, "5m"),
                "options": {"temperature": temperature, "seed": seed, "num_predict": max_tokens},
            },
            timeout,
        )
        return str((data.get("message") or {}).get("content") or "")
    if backend == "lm studio":
        url = llm.get("lmstudio_url") or DEFAULT_LMSTUDIO_URL
        _, _, payload, extract = _cloud_request(
            "openai", "", model or "local-model", system, prompt, temperature, seed, max_tokens
        )
        return extract(_post_json(f"{url}/v1/chat/completions", payload, timeout))
    if backend not in CLOUD_PROVIDERS:
        raise RuntimeError(f"unknown backend '{backend}'")
    key = str((settings.get("llm_api_keys") or {}).get(backend) or "")
    if not key:
        raise RuntimeError(f"no {backend} API key stored — add it in the Composer's Settings tab")
    model = model or DEFAULT_CLOUD_MODELS.get(backend, "")
    if not model:
        raise RuntimeError(f"{backend} needs a model name — set the model widget")
    url, headers, payload, extract = _cloud_request(
        backend, key, model, system, prompt, temperature, seed, max_tokens
    )
    return extract(_post_json(url, payload, timeout, headers))


# -- LoRA download by AIR (heal missing files) -------------------------------
# A section item stores the LoRA file name + its Civitai AIR urn in the
# comment. On a machine that lacks the file, the Composer offers to fetch
# it: background thread (multi-GB files), SHA256-verified, .safetensors
# only, then the section item is re-pointed if the chosen path differs.

_AIR_RE = re.compile(r"^urn:air:[a-z0-9._-]+:[a-z0-9._-]+:civitai:(\d+)@(\d+)$", re.IGNORECASE)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
_LORA_DL_STATUS = {}  # air urn -> {"status", "detail", "name", "loaded", "total"}


def parse_air(air):
    """(model_id, version_id) from a Civitai AIR urn, or None."""
    match = _AIR_RE.match(str(air or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _sanitize_subfolder(folder):
    """Relative subfolder under the loras root — every segment checked, no
    escapes, backslashes normalized. Empty means the root itself."""
    folder = str(folder or "").strip().replace("\\", "/").strip("/")
    if not folder:
        return ""
    parts = [p.strip() for p in folder.split("/") if p.strip()]
    for part in parts:
        if not _SAFE_SEGMENT.match(part) or part in (".", ".."):
            raise ApiError(f"invalid folder segment '{part}'")
    return "/".join(parts)


def _sanitize_lora_filename(name):
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        return ""
    if not base.lower().endswith(".safetensors"):
        base += ".safetensors"
    if not _SAFE_SEGMENT.match(base):
        raise ApiError(f"invalid file name '{base}'")
    return base


def _heal_section_lora(lib, section_slug, item_name, new_lora):
    """Re-point a section item's data.lora at the downloaded file (user-tier
    write). Factory-origin items get a full self-contained snapshot — the
    tier merge replaces items by name wholesale, so a thin entry would wipe
    the item's texts."""
    section = lib.load_section(section_slug)
    target = next((i for i in section.items if i.name == item_name), None)
    if target is None:
        raise ApiError(f"item '{item_name}' not found in section '{section_slug}'")
    raw = {"items": []}
    user_file = (lib.user_root / "sections" / f"{section_slug}.json") if lib.user_root else None
    if user_file and user_file.is_file():
        raw = json.loads(user_file.read_text(encoding="utf-8"))
        raw.setdefault("items", [])
    entry = next(
        (i for i in raw["items"] if isinstance(i, dict) and i.get("name") == item_name), None
    )
    if entry is None:
        entry = pl.dump_item(target)
        raw["items"].append(entry)
    entry["data"] = {**(entry.get("data") or {}), "lora": new_lora}
    lib.save_user("sections", section_slug, raw)


def _fetch_lora_file(meta_headers, token, version_id, dest_dir, filename, status):
    """Civitai version metadata -> stream the primary .safetensors file ->
    SHA256 verify -> move into place. Returns the final file name; RAISES on
    any failure (partial .part always removed). `status` is a plain progress
    sink so both the threaded and the synchronous caller can watch it."""
    import os
    import urllib.parse
    import urllib.request

    part_path = None
    try:
        request = urllib.request.Request(
            f"https://civitai.com/api/v1/model-versions/{version_id}", headers=meta_headers
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        files = [
            f
            for f in (meta.get("files") or [])
            if str(f.get("name") or "").lower().endswith(".safetensors")
        ]
        if not files:
            raise RuntimeError("this Civitai version ships no .safetensors file")
        chosen = next((f for f in files if f.get("primary")), files[0])
        filename = filename or _sanitize_lora_filename(chosen.get("name"))
        want_sha = str((chosen.get("hashes") or {}).get("SHA256") or "").lower()
        url = str(chosen.get("downloadUrl") or "")
        if not url:
            url = f"https://civitai.com/api/download/models/{version_id}"
        if token:
            # token rides the QUERY, not a header: the download redirects to
            # presigned storage where an Authorization header breaks the
            # signature
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={urllib.parse.quote(token)}"
        os.makedirs(dest_dir, exist_ok=True)
        final_path = os.path.join(dest_dir, filename)
        part_path = final_path + ".part"
        import hashlib

        digest = hashlib.sha256()
        loaded = 0
        request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-MRLN-Nodes"})
        with urllib.request.urlopen(request, timeout=120) as resp:
            status["total"] = int(resp.headers.get("Content-Length") or 0)
            with open(part_path, "wb") as fh:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    fh.write(chunk)
                    digest.update(chunk)
                    loaded += len(chunk)
                    status["loaded"] = loaded
        if want_sha and digest.hexdigest().lower() != want_sha:
            os.remove(part_path)
            raise RuntimeError(
                f"SHA256 mismatch after download (got {digest.hexdigest()[:12]}…, "
                f"Civitai says {want_sha[:12]}…) — file discarded"
            )
        os.replace(part_path, final_path)
        status["name"] = filename
        return filename
    except Exception:
        # never leave a multi-GB torso in the loras folder: os.replace has
        # not run (or already consumed the file), so drop the partial
        if part_path and os.path.exists(part_path):
            with contextlib.suppress(OSError):
                os.remove(part_path)
        raise


def _lora_download_worker(status_key, meta_headers, token, version_id, dest_dir, filename, heal):
    """Background thread wrapper: fetch, then optionally heal the section item
    at the new path. Writes progress into _LORA_DL_STATUS; never raises."""
    status = _LORA_DL_STATUS[status_key]
    try:
        filename = _fetch_lora_file(meta_headers, token, version_id, dest_dir, filename, status)
        if heal:
            lib, section_slug, item_name, folder, stored = heal
            new_name = f"{folder}/{filename}" if folder else filename
            stored = str(stored or "").replace("\\", "/")
            if stored.lower() != new_name.lower():
                _heal_section_lora(lib, section_slug, item_name, new_name)
                status["healed"] = new_name
        status["status"] = "done"
        status["detail"] = f"saved as {filename}"
    except Exception as exc:
        status["status"] = "error"
        status["detail"] = str(exc)


@_guarded
def handle_lora_download(lib, payload):
    """POST {air, start:true, folder?, filename?, section?, item?, stored?}
    begins a background download of the AIR-referenced Civitai file into
    the loras folder; GET {air} polls progress. When section+item are given
    and the final path differs from `stored`, the section item is healed to
    point at the downloaded file. Only JSON `true` counts as start — GET
    query values are strings, so a polling (or cross-site) GET can never
    write into the loras folder."""
    air = _require_str(payload, "air")
    parsed = parse_air(air)
    if parsed is None:
        raise ApiError(
            f"'{air}' is not a Civitai AIR urn (urn:air:<eco>:<type>:civitai:<model>@<version>)"
        )
    if payload.get("start") is not True:
        status = _LORA_DL_STATUS.get(air) or {"status": "unknown", "detail": "no download started"}
        return 200, {"air": air, **status}
    current = _LORA_DL_STATUS.get(air)
    if current and current.get("status") == "downloading":
        return 200, {"air": air, **current}
    try:
        import folder_paths
    except ImportError:
        return 400, {
            "error": "LoRA downloads run inside a running ComfyUI only",
            "remediation": "download the file manually and place it in your loras folder",
        }
    roots = folder_paths.get_folder_paths("loras")
    if not roots:
        return 400, {"error": "no loras folder registered in this ComfyUI"}
    folder = _sanitize_subfolder(payload.get("folder"))
    filename = _sanitize_lora_filename(payload.get("filename"))
    import os

    dest_dir = os.path.join(roots[0], *folder.split("/")) if folder else roots[0]
    _, version_id = parsed
    token = str(_read_settings(lib).get("civitai_api_key") or "")
    meta_headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        meta_headers["Authorization"] = f"Bearer {token}"
    heal = None
    section = str(payload.get("section") or "").strip()
    item = str(payload.get("item") or "").strip()
    if section and item:
        heal = (lib, section, item, folder, str(payload.get("stored") or ""))
    _LORA_DL_STATUS[air] = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    threading.Thread(
        target=_lora_download_worker,
        args=(air, meta_headers, token, version_id, dest_dir, filename, heal),
        daemon=True,
    ).start()
    return 200, {"air": air, "status": "downloading"}


def _lora_items(lib, template=None):
    """Every LoRA-carrying library item as (section_slug, item), scoped to one
    template's reachable pools when `template` is given."""
    if template is None:
        slugs = lib.section_slugs()
    else:
        tpl = lib.load_template(template)
        refs = [s.ref for s in tpl.slots]
        refs.extend(s.ref for v in tpl.variants for s in v.slots)
        sections, _missing = pl.section_closure(lib, refs)
        slugs = sorted(sections)
    out = []
    for slug in slugs:
        try:
            section = lib.load_section(slug)
        except pl.PromptLibError:
            continue
        for item in section.items:
            if item.hidden:
                continue
            if (item.data or {}).get("lora"):
                out.append((slug, item))
    return out


def lora_status(lib, template=None):
    """Which LoRA files the library (or one template) needs, and which of
    them are actually installed. Pure apart from the folder_paths lookup, so
    the startup scan, the endpoint and the node all share one answer."""
    installed = None
    try:
        import folder_paths

        installed = {
            n.replace("\\", "/").lower(): n for n in folder_paths.get_filename_list("loras")
        }
    except Exception:  # outside ComfyUI there is nothing to check against
        pass
    rows, missing = [], 0
    for slug, item in _lora_items(lib, template):
        data = item.data or {}
        name = str(data.get("lora") or "")
        comment = str(data.get("comment") or "").strip()
        air = comment if comment.lower().startswith("urn:air:") else ""
        present = True if installed is None else name.replace("\\", "/").lower() in installed
        if not present:
            missing += 1
        rows.append(
            {
                "file": name,
                "air": air,
                "section": slug,
                "item": item.name,
                "present": present,
            }
        )
    return {
        "loras": rows,
        "total": len(rows),
        "missing": missing,
        "can_download": installed is not None,
    }


@_guarded
def handle_lora_status(lib, payload):
    """GET: which LoRA files this library — or one template — needs and which
    are missing on this machine. Feeds the Composer's pre-render warning so a
    missing file surfaces before the graph dies in LoRA Apply."""
    template = payload.get("template")
    template = template.strip() if isinstance(template, str) and template.strip() else None
    body = lora_status(lib, template)
    if template:
        body["template"] = template
    return 200, body


def download_lora_by_air(lib, air, *, folder="", filename="", section="", item="", stored=""):
    """SYNCHRONOUS download-by-AIR for the node path — the Composer is not
    involved, so this blocks until the file is verified and in place.
    Returns the loras-root-relative name; raises RuntimeError on failure."""
    parsed = parse_air(air)
    if parsed is None:
        raise RuntimeError(f"'{air}' is not a Civitai AIR urn")
    try:
        import folder_paths
    except ImportError as exc:
        raise RuntimeError("LoRA downloads run inside a running ComfyUI only") from exc
    roots = folder_paths.get_folder_paths("loras")
    if not roots:
        raise RuntimeError("no loras folder registered in this ComfyUI")
    import os

    folder = _sanitize_subfolder(folder)
    dest_dir = os.path.join(roots[0], *folder.split("/")) if folder else roots[0]
    _, version_id = parsed
    token = str(_read_settings(lib).get("civitai_api_key") or "")
    headers = {"User-Agent": "ComfyUI-MRLN-Nodes"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status = {"status": "downloading", "detail": "", "loaded": 0, "total": 0}
    _LORA_DL_STATUS[air] = status  # the Composer can watch a node-side fetch
    name = _fetch_lora_file(
        headers, token, version_id, dest_dir, _sanitize_lora_filename(filename), status
    )
    status["status"] = "done"
    status["detail"] = f"saved as {name}"
    final = f"{folder}/{name}" if folder else name
    if section and item and str(stored or "").replace("\\", "/").lower() != final.lower():
        with contextlib.suppress(Exception):  # the file is there; healing is a bonus
            _heal_section_lora(lib, section, item, final)
            status["healed"] = final
    with contextlib.suppress(Exception):
        folder_paths.get_filename_list.cache_clear()  # make the new file visible now
    return final


@_guarded
def handle_lora_meta(lib, payload):
    """Trigger word from an installed LoRA's own metadata. Names come from
    the /models/loras list; resolution goes through folder_paths only, so
    no request string touches the filesystem directly."""
    name = _require_str(payload, "name")
    resolved = _resolve_lora_file(name)
    if resolved is None:
        return 400, {
            "error": "LoRA metadata is only readable inside a running ComfyUI",
            "remediation": "type the catchword manually",
        }
    real, path = resolved
    if path is None:
        return 404, {
            "error": f"LoRA '{name}' not found in your loras folder",
            "remediation": "refresh the list or pick another file",
        }
    try:
        meta = pl.read_safetensors_metadata(path)
    except ValueError as exc:
        return 400, {"error": str(exc), "remediation": "type the catchword manually"}
    trigger, source = pl.trigger_from_metadata(meta)
    if not trigger:
        return 404, {
            "error": f"no trigger word in the metadata of '{real}'",
            "remediation": "type the catchword manually — trainers embed triggers as "
            "modelspec.trigger_phrase or kohya ss_tag_frequency",
        }
    return 200, {"trigger": trigger, "source": source, "name": real}


ROUTES = (
    ("get", "/mrln/prompt/library", handle_library, False),
    ("get", "/mrln/prompt/template", handle_template, False),
    ("get", "/mrln/prompt/section", handle_section, False),
    ("get", "/mrln/prompt/items", handle_items, False),
    ("get", "/mrln/prompt/lora-meta", handle_lora_meta, False),
    ("get", "/mrln/prompt/lora-status", handle_lora_status, False),
    ("get", "/mrln/prompt/lora-civitai", handle_lora_civitai, False),
    ("get", "/mrln/prompt/lora-download", handle_lora_download, False),
    ("post", "/mrln/prompt/lora-download", handle_lora_download, True),
    ("get", "/mrln/prompt/settings", handle_settings, False),
    ("post", "/mrln/prompt/save-settings", handle_save_settings, True),
    ("get", "/mrln/prompt/profile", handle_profile, False),
    ("post", "/mrln/prompt/save-profile", handle_save_profile, True),
    ("get", "/mrln/prompt/llm-validate", handle_llm_validate, False),
    ("get", "/mrln/prompt/llm-pull", handle_llm_pull, False),
    ("post", "/mrln/prompt/llm-pull", handle_llm_pull, True),
    ("post", "/mrln/prompt/preview", handle_preview, True),
    ("post", "/mrln/prompt/save-section", handle_save_section, True),
    ("post", "/mrln/prompt/save-template", handle_save_template, True),
    ("post", "/mrln/prompt/delete", handle_delete, True),
    ("post", "/mrln/prompt/decompose", handle_decompose, True),
    ("get", "/mrln/prompt/export", handle_export, False),
    ("post", "/mrln/prompt/import", handle_import, True),
)


def _warm_library_caches():
    """Populate the module-level parse cache once at server boot so the
    composer's first open never pays the cold-file cost (first-touch AV
    scanning of ~170 JSON files can cost seconds on Windows)."""
    try:
        lib = pl.open_library()
        count = 0
        for slug in lib.section_slugs():
            try:
                lib.load_section(slug)
                count += 1
            except pl.PromptLibError:
                pass
        for slug in lib.template_slugs():
            try:
                lib.load_template(slug)
                count += 1
            except pl.PromptLibError:
                pass
        logger.info("MRLN prompt library warmed (%d files)", count)
        # Startup LoRA audit: a missing file otherwise only surfaces when the
        # graph already died in LoRA Apply. Report it while the user is still
        # reading the boot log, and name the AIRs that can heal themselves.
        status = lora_status(lib)
        if status["can_download"] and status["missing"]:
            gone = [row for row in status["loras"] if not row["present"]]
            healable = sum(1 for row in gone if row["air"])
            logger.warning(
                "MRLN prompt: %d of %d referenced LoRA file(s) are missing "
                "(%d carry a Civitai AIR and can be fetched from the Composer, "
                "or by the LoRA Apply node with on_missing = 'download')",
                status["missing"],
                status["total"],
                healable,
            )
            for row in gone[:10]:
                logger.warning(
                    "MRLN prompt:   missing '%s' (%s/%s)%s",
                    row["file"],
                    row["section"],
                    row["item"],
                    "" if row["air"] else " — no AIR, needs a manual file pick",
                )
            if len(gone) > 10:
                logger.warning("MRLN prompt:   … and %d more", len(gone) - 10)
    except Exception:
        logger.debug("MRLN prompt library warm-up skipped", exc_info=True)


def register_routes():
    """Attach ROUTES to the running ComfyUI server. Returns False (never
    raises) when there is no server to attach to."""
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return False  # headless / pytest / outside ComfyUI: endpoints are optional
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return False

    def adapt(handler, reads_body):
        async def endpoint(request):
            if reads_body:
                if (request.content_length or 0) > MAX_BODY_BYTES:
                    body = {"error": "request body too large", "remediation": "send less data"}
                    return web.json_response(body, status=413)
                try:
                    payload = await request.json()
                except Exception:
                    body = {"error": "request body is not valid JSON", "remediation": "send JSON"}
                    return web.json_response(body, status=400)
                if not isinstance(payload, dict):
                    body = {"error": "request body must be a JSON object", "remediation": ""}
                    return web.json_response(body, status=400)
            else:
                payload = dict(request.rel_url.query)
                # GET only ever polls/reads: the state-changing 'start' flag
                # rides POST JSON bodies exclusively, so a bare cross-site
                # GET (no CORS preflight) can never trigger a download
                payload.pop("start", None)
            # handlers are pure/synchronous — run them in the executor so
            # they never block (or wait behind) the busy boot-time loop
            loop = asyncio.get_running_loop()
            status, data = await loop.run_in_executor(None, handler, pl.open_library(), payload)
            return web.json_response(data, status=status)

        return endpoint

    for method, path, handler, reads_body in ROUTES:
        getattr(instance.routes, method)(path)(adapt(handler, reads_body))
    threading.Thread(target=_warm_library_caches, name="mrln-prompt-warmup", daemon=True).start()
    return True
