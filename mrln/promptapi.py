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
import json
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
            )
        except pl.PromptLibError as exc:
            entry.update(label=slug, error=str(exc))
        sections.append(entry)
    return 200, {
        "fingerprint": lib.fingerprint(),
        "templates": templates,
        "sections": sections,
        "folders": lib.section_folders(),
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
    if fmt == "template default":
        fmt = tpl.render.format
    if fmt not in pl.FORMATS:
        raise ApiError(f"unknown format '{fmt}' (formats: {', '.join(pl.FORMATS)})")
    policy = payload.get("conflict_policy") or "negative prevails"
    if policy not in pl.CONFLICT_POLICIES:
        raise ApiError(
            f"unknown conflict_policy '{policy}' (policies: {', '.join(pl.CONFLICT_POLICIES)})"
        )
    resolved = pl.resolve_template(
        lib,
        tpl,
        seed=seed,
        mode=mode,
        selection=selection,
        variables=variables,
        text_length=payload.get("text_length") or "template default",
    )
    out = pl.render(resolved, fmt, tpl.render, conflict_policy=policy)
    return 200, {
        "positive": out.positive,
        "negative": out.negative,
        "choices": out.choices,
        "format": fmt,
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
        "children": [_resolved_slot_json(c) for c in s.children],
    }


def _save(lib, payload, kind):
    slug = pl.validate_slug(_require_str(payload, "slug"))
    overrides = (lib.factory_root / kind / f"{slug}.json").is_file()
    lib.save_user(kind, slug, payload.get("data"))
    return 200, {"ok": True, "slug": slug, "tier": "user", "overrides_factory": overrides}


@_guarded
def handle_save_section(lib, payload):
    return _save(lib, payload, "sections")


@_guarded
def handle_save_template(lib, payload):
    return _save(lib, payload, "templates")


@_guarded
def handle_delete(lib, payload):
    kind = _require_str(payload, "kind")
    slug = _require_str(payload, "slug")
    reverted = lib.delete_user(kind, slug)
    return 200, {"ok": True, "slug": slug, "reverted_to_factory": reverted}


@_guarded
def handle_decompose(lib, payload):
    prompt_text = _require_str(payload, "prompt")
    raw_type = payload.get("type") or []
    if isinstance(raw_type, str):
        raw_type = [t.strip() for t in raw_type.split(",") if t.strip()]
    if not isinstance(raw_type, list):
        raise ApiError("'type' must be a list or comma-separated string")
    report = pl.decompose(
        lib,
        prompt_text,
        template_type=tuple(str(t) for t in raw_type),
        engine=str(payload.get("engine") or "heuristic"),
    )
    report["fingerprint"] = lib.fingerprint()
    return 200, report


@_guarded
def handle_lora_meta(lib, payload):
    """Trigger word from an installed LoRA's own metadata. Names come from
    the /models/loras list; resolution goes through folder_paths only, so
    no request string touches the filesystem directly."""
    name = _require_str(payload, "name")
    try:
        import folder_paths
    except ImportError:
        return 400, {
            "error": "LoRA metadata is only readable inside a running ComfyUI",
            "remediation": "type the catchword manually",
        }
    available = folder_paths.get_filename_list("loras")
    norm = lambda n: n.replace("\\", "/").lower()  # noqa: E731 — two uses below
    real = next((c for c in available if c == name), None) or next(
        (c for c in available if norm(c) == norm(name)), None
    )
    if real is None:
        return 404, {
            "error": f"LoRA '{name}' not found in your loras folder",
            "remediation": "refresh the list or pick another file",
        }
    path = folder_paths.get_full_path("loras", real)
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
    ("post", "/mrln/prompt/preview", handle_preview, True),
    ("post", "/mrln/prompt/save-section", handle_save_section, True),
    ("post", "/mrln/prompt/save-template", handle_save_template, True),
    ("post", "/mrln/prompt/delete", handle_delete, True),
    ("post", "/mrln/prompt/decompose", handle_decompose, True),
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
