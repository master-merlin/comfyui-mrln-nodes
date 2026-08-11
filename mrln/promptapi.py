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
            with open(entry.path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
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
    with open(lib.user_root / "profiles.json", "w", encoding="utf-8") as fh:
        json.dump({"profiles": user_profiles}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    lib.invalidate()
    return 200, {
        "ok": True,
        "name": name,
        "action": action,
        "profiles": sorted(lib.pack_profiles()),
    }


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
    return 200, {
        "civitai_key_set": bool(settings.get("civitai_api_key")),
        "llm": {
            "ollama_url": llm.get("ollama_url") or DEFAULT_OLLAMA_URL,
            "lmstudio_url": llm.get("lmstudio_url") or DEFAULT_LMSTUDIO_URL,
        },
    }


@_guarded
def handle_save_settings(lib, payload):
    if lib.user_root is None:
        return 400, {
            "error": "no user library root configured",
            "remediation": "set MRLN_PROMPT_DIR or run inside ComfyUI",
        }
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
    lib.user_root.mkdir(parents=True, exist_ok=True)
    with open(_settings_path(lib), "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    return 200, {"ok": True, "civitai_key_set": bool(settings.get("civitai_api_key"))}


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


@_guarded
def handle_llm_validate(lib, payload):
    """Ping a local LLM backend and list its models — powers the green
    checkmarks in the Composer settings and the Enhance node's dropdown."""
    provider = _require_str(payload, "provider")
    llm = _llm_settings(_read_settings(lib))
    import urllib.error
    import urllib.request

    if provider == "ollama":
        url = f"{llm.get('ollama_url') or DEFAULT_OLLAMA_URL}/api/tags"
    elif provider == "lmstudio":
        url = f"{llm.get('lmstudio_url') or DEFAULT_LMSTUDIO_URL}/v1/models"
    else:
        raise ApiError(f"unknown provider '{provider}' (have: ollama, lmstudio)")
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
    route) polls its status. The dropdown suggestion click lands here."""
    model = _require_str(payload, "model")
    if payload.get("start"):
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
    ("get", "/mrln/prompt/lora-civitai", handle_lora_civitai, False),
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
