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

import json
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
    entry = lib._scan(kind).get(slug)
    if entry is None:
        not_found = pl.SectionNotFoundError if kind == "sections" else pl.TemplateNotFoundError
        raise not_found(slug, list(lib._scan(kind)))
    with open(entry.path, encoding="utf-8") as fh:
        return json.load(fh)


def _slot_detail(slot):
    return {
        "id": slot.id,
        "ref": slot.ref,
        "label": slot.label,
        "default": slot.default,
        "allow_empty": slot.allow_empty,
        "empty_weight": slot.empty_weight,
        "emphasis": slot.emphasis,
    }


def _pool(lib, ref):
    return [
        {
            "name": qualified,
            "text": item.text,
            "negative": item.negative,
            "weight": item.weight,
            "section_slug": section.slug,
            "tier": lib.tier_of("sections", section.slug),
        }
        for qualified, section, item in lib.scope_items(ref)
    ]


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
    for ref in refs:
        if ref not in pools:  # twin slots on one section share a pool
            pools[ref] = _pool(lib, ref)
    detail = {
        "slug": slug,
        "label": tpl.label,
        "description": tpl.description,
        "prefix": tpl.prefix,
        "suffix": tpl.suffix,
        "negative": tpl.negative,
        "order": list(tpl.order),
        "variant_default": tpl.variant_default,
        "variables": [
            {"name": v.name, "label": v.label, "default": v.default} for v in tpl.variables
        ],
        "slots": [_slot_detail(slot) for slot in tpl.slots],
        "variants": [
            {
                "name": variant.name,
                "label": variant.label,
                "slots": [_slot_detail(slot) for slot in variant.slots],
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
        "fingerprint": lib.fingerprint(),
    }


@_guarded
def handle_section(lib, payload):
    slug = _require_str(payload, "slug")
    section = lib.load_section(slug)
    return 200, {
        "slug": slug,
        "tier": lib.tier_of("sections", slug),
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
            }
            for item in section.items
        ],
        "raw": _raw_file(lib, "sections", slug),
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
    resolved = pl.resolve_template(
        lib, tpl, seed=seed, mode=mode, selection=selection, variables=variables
    )
    out = pl.render(resolved, fmt, tpl.render)
    return 200, {
        "positive": out.positive,
        "negative": out.negative,
        "choices": out.choices,
        "format": fmt,
        "variant": resolved.variant,
        "variant_random": resolved.variant_random,
        "slots": [
            {
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
            }
            for s in resolved.slots
        ],
        "fingerprint": lib.fingerprint(),
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


ROUTES = (
    ("get", "/mrln/prompt/library", handle_library, False),
    ("get", "/mrln/prompt/template", handle_template, False),
    ("get", "/mrln/prompt/section", handle_section, False),
    ("get", "/mrln/prompt/items", handle_items, False),
    ("post", "/mrln/prompt/preview", handle_preview, True),
    ("post", "/mrln/prompt/save-section", handle_save_section, True),
    ("post", "/mrln/prompt/save-template", handle_save_template, True),
    ("post", "/mrln/prompt/delete", handle_delete, True),
)


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
            status, data = handler(pl.open_library(), payload)
            return web.json_response(data, status=status)

        return endpoint

    for method, path, handler, reads_body in ROUTES:
        getattr(instance.routes, method)(path)(adapt(handler, reads_body))
    return True
