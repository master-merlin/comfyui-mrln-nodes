"""De-compose a pasted prompt against the library: map each fragment to an
existing section item where one matches, leave the rest as honest residue.

Pure and deterministic — the composer's De-compose tab renders the report
and lets the user resolve residue (new items via extend-saves, prefix or
suffix prose) before storing the result as a template. This module IS the
"programmatic" engine ("heuristic" is accepted as its old alias). The
"llm" and "hybrid" engines live in the API layer (promptapi) because they
call a backend: llm asks the model directly, hybrid feeds this module's
report into the LLM system prompt as suggestions to verify or correct —
both validate every assignment against the real library via score_match().

Matching model (programmatic): the prompt splits into lines, each line
first tries to match a whole item (optionally behind a "Label:" lead-in),
then falls back to comma-piece matching. Scores are token-set F1; emphasis
wrappers "(text:1.3)" and unexpanded {a|b} wildcards are stripped before
comparison.
"""

import re

from .errors import SelectionError

ENGINES = ("programmatic", "llm", "hybrid")
_ENGINE_ALIASES = {"heuristic": "programmatic"}
_LINE_SCORE = 0.85  # full line vs one item
_PIECE_SCORE = 0.6  # comma piece vs one item
_LABEL_RE = re.compile(r"^[^:\n]{1,60}:\s+")
_EMPHASIS_RE = re.compile(r"\(([^()]*):[0-9.]+\)")
_BRACES_RE = re.compile(r"\{[^{}]*\}")
_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text):
    text = text.lower()
    while _EMPHASIS_RE.search(text):
        text = _EMPHASIS_RE.sub(r"\1", text)
    text = _BRACES_RE.sub(" ", text)
    return frozenset(_WORD_RE.findall(text))


def _f1(a, b):
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    if not overlap:
        return 0.0
    return 2.0 * overlap / (len(a) + len(b))


def _candidates(lib, template_type):
    """(section_slug, item_name, token_set) over every drawable item,
    honoring suits/type the way random pools do."""
    result = []
    for slug in lib.section_slugs():
        try:
            section = lib.load_section(slug)
        except Exception:
            continue  # one broken file must not kill decomposition
        if template_type and section.suits and not (set(section.suits) & set(template_type)):
            continue
        for item in section.items:
            if item.hidden:
                continue
            tokens = _tokens(item.text)
            if tokens:
                result.append((slug, item.name, tokens))
    return result


def _best_match(tokens, candidates):
    best, best_score = None, 0.0
    for slug, name, item_tokens in candidates:
        score = _f1(tokens, item_tokens)
        if score > best_score:
            best, best_score = (slug, name), score
    return best, best_score


def _match_fragment(text, candidates, threshold):
    tokens = _tokens(text)
    best, score = _best_match(tokens, candidates)
    if best and score >= threshold:
        return {
            "text": text,
            "match": {"section": best[0], "item": best[1], "score": round(score, 3)},
        }
    return {"text": text, "match": None}


def score_match(lib, text, section_slug, item_name):
    """Token-F1 of a fragment against one specific item, or None when the
    section/item does not exist — the LLM engines use this both to validate
    an assignment and to attach an honest score to it."""
    try:
        section = lib.load_section(section_slug)
    except Exception:
        return None
    target = next((i for i in section.items if i.name == item_name and not i.hidden), None)
    if target is None:
        return None
    return round(_f1(_tokens(text), _tokens(target.text)), 3)


def decompose(lib, prompt_text, *, template_type=(), engine="programmatic"):
    engine = _ENGINE_ALIASES.get(engine, engine)
    if engine not in ENGINES:
        raise SelectionError(engine, f"unknown engine (engines: {', '.join(ENGINES)})")
    if engine != "programmatic":
        raise SelectionError(
            engine,
            "the llm/hybrid engines run through the Composer API "
            "(POST /mrln/prompt/decompose) — this module is the programmatic engine",
        )
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise SelectionError("prompt", "nothing to decompose — paste a prompt first")

    candidates = _candidates(lib, template_type)
    fragments = []
    for raw_line in prompt_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        stripped = _LABEL_RE.sub("", line)
        whole = _match_fragment(stripped, candidates, _LINE_SCORE)
        if whole["match"]:
            fragments.append(whole)
            continue
        pieces = [p.strip() for p in stripped.split(", ") if p.strip()]
        if len(pieces) <= 1:
            fragments.append(whole)
            continue
        # comma pieces: matched ones become slots, adjacent residue re-joins
        residue = []
        line_fragments = []
        for piece in pieces:
            hit = _match_fragment(piece, candidates, _PIECE_SCORE)
            if hit["match"]:
                if residue:
                    line_fragments.append({"text": ", ".join(residue), "match": None})
                    residue = []
                line_fragments.append(hit)
            else:
                residue.append(piece)
        if residue:
            line_fragments.append({"text": ", ".join(residue), "match": None})
        fragments.extend(line_fragments)

    # residue still gets a nearest-section hint so the UI can preselect a home
    for fragment in fragments:
        if fragment["match"] is None:
            best, score = _best_match(_tokens(fragment["text"]), candidates)
            if best:
                fragment["suggestion"] = {"section": best[0], "score": round(score, 3)}

    matched = sum(1 for f in fragments if f["match"])
    return {
        "engine": engine,
        "fragments": fragments,
        "matched": matched,
        "unmatched": len(fragments) - matched,
    }
