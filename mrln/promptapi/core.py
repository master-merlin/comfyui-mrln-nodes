"""Shared primitives every prompt-API submodule builds on: the request
error type, the handler guard that maps exceptions onto HTTP statuses, the
raw-file readers, the JSON shaping helpers and the atomic writer.
"""

import json
import traceback

from .. import promptlib as pl
from ..pack import logger

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


def _tier_raw(lib, kind, slug, tier=""):
    """The raw file of ONE tier, or the winning one when tier is empty. The
    editor edits what it is SHOWN, so a factory view has to hand over the
    factory file — the winner's raw would put a user file under a factory
    label."""
    if tier:
        root = lib.factory_root if tier == "factory" else lib.user_root
        path = (root / kind / f"{slug}.json") if root else None
        if path and path.is_file():
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    return _raw_file(lib, kind, slug)


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
        if item.tags:
            # A slot's tags_any/tags_none filter can only offer the tags its
            # pool actually carries; without them the composer's slot editor
            # has nothing to draw, and the filter stays invisible and
            # hand-edited. Omitted when empty — most pools have none, and a
            # [] on every row of a few hundred is payload saying nothing.
            entry["tags"] = list(item.tags)
        if item.data and item.data.get("lora"):
            entry["lora"] = str(item.data["lora"])
            base = pl.lora_base_family(item.data)
            if base:  # the pill names the target model, so a mismatch is visible
                entry["base"] = base
        pool.append(entry)
    return pool


def _kv_map(payload, key):
    value = payload.get(key) or ""
    if isinstance(value, str):
        return pl.parse_kv_lines(value, what=key)
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    raise ApiError(f"'{key}' must be a string of name=value lines or an object")


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
