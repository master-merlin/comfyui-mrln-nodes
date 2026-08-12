"""The LLM and hybrid de-compose engines: prompt text -> ordered fragments
mapped onto real library items. The programmatic engine lives in promptlib;
only the model-driven passes need the API layer's LLM access.
"""

import json
import re

from .. import promptlib as pl
from . import llm  # module object, not the name: keeps llm_chat patchable
from .core import ApiError, _guarded, _require_str

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
    text = llm.llm_chat(
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
        # tolerant like handle_preview's seed: plenty of JSON serializers emit
        # 90.0 for an integer, and a GET-shaped client sends "90". bool is an
        # int subclass, so `true` is rejected on purpose rather than by luck.
        raw_timeout = payload.get("timeout", 90)
        try:
            timeout = None if isinstance(raw_timeout, bool) else int(raw_timeout)
        except (TypeError, ValueError):
            timeout = None
        if timeout is None or not 5 <= timeout <= 600:
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
