"""Pre-ship protocol regressions for the node layer (mrln/nodes/prompt.py).

Each test here pins a defect the pre-shipment audit found, so the fix cannot
silently regress: the Section node's pre-queue validator drifting away from
the engine it fronts, the Enhance memo cache growing without bound, and the
append-only widget/output contract that saved workflows depend on.
"""

import importlib
import json
import sys

import pytest
import support

from mrln import promptlib as pl


@pytest.fixture(autouse=True)
def user_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    return tmp_path / "user"


@pytest.fixture(scope="module")
def classes():
    return support.load_pack().NODE_CLASS_MAPPINGS


# ---------------------------------------------------------------------------
# PromptSection.VALIDATE_INPUTS must agree with resolve_section, not be
# stricter than it: an API-submitted workflow may legitimately carry any
# control token the engine's parser accepts.
# ---------------------------------------------------------------------------

SECTION = "vehicle/car/color/paint"

# tokens the ENGINE resolves; every one of them must queue
ENGINE_TOKENS = ("random", "🎲 random", "random@123", "🎲 random@123", "off", "🔇 off")


@pytest.mark.parametrize("token", ENGINE_TOKENS)
def test_section_validate_accepts_every_token_the_engine_resolves(classes, token):
    node_cls = classes["MRLN_PromptSection"]
    # the engine is the reference: prove it handles the token first ...
    resolved = pl.resolve_section(pl.open_library(), SECTION, token, seed=5)
    assert resolved is not None
    # ... then that the pre-queue gate lets it through
    assert node_cls.VALIDATE_INPUTS(section=SECTION, item=token) is True


@pytest.mark.parametrize("token", ("off", "🔇 off"))
def test_section_execute_mutes_on_an_off_token(classes, token):
    """The muted outcome is three empty strings, never None — the widget
    validator now admits these tokens, so the node has to survive them."""
    out = classes["MRLN_PromptSection"]().execute(
        section=SECTION, item=token, seed=5, allow_empty=False
    )
    assert out == ("", "", "")


def test_section_validate_seeded_random_matches_the_engines_draw(classes):
    """'random@123' is a per-item seed override, not an item name: the
    validator must not read it as a (missing) item, and the draw it names
    must be the seeded one, independent of the node's own seed widget."""
    node = classes["MRLN_PromptSection"]()
    assert classes["MRLN_PromptSection"].VALIDATE_INPUTS(section=SECTION, item="random@123") is True
    a = node.execute(section=SECTION, item="🎲 random@123", seed=1, allow_empty=False)
    b = node.execute(section=SECTION, item="random@123", seed=999, allow_empty=False)
    assert a == b and a[2]  # same item despite different master seeds


def test_section_validate_rejects_a_malformed_seed_like_the_engine(classes):
    """Stricter-than-engine was the bug; looser-than-engine would be the next
    one. 'random@abc' is refused by the engine, so it is refused here — with
    the engine's own message, not 'is not inside section'."""
    verdict = classes["MRLN_PromptSection"].VALIDATE_INPUTS(section=SECTION, item="random@abc")
    assert isinstance(verdict, str)
    assert "not a valid seed integer" in verdict
    with pytest.raises(pl.SelectionError):
        pl.resolve_section(pl.open_library(), SECTION, "random@abc", seed=0)


def test_section_validate_still_rejects_an_out_of_scope_item(classes):
    """The loosening is bounded to control tokens: a real item name outside
    the scope is still caught before the graph runs."""
    node_cls = classes["MRLN_PromptSection"]
    verdict = node_cls.VALIDATE_INPUTS(section="lighting/day", item=f"{SECTION}/guards-red")
    assert "not inside" in verdict
    assert node_cls.VALIDATE_INPUTS(section=SECTION, item="no-such-item").startswith("item ")
    # surrounding whitespace is stripped by the engine's parser, so it must
    # not turn a valid pick into a rejection
    assert node_cls.VALIDATE_INPUTS(section=SECTION, item=f"  {SECTION}/guards-red  ") is True


# ---------------------------------------------------------------------------
# PromptEnhance memo cache: bounded LRU, not an append-only log
# ---------------------------------------------------------------------------


def _node_module(cls):
    """The node module object the loaded pack actually uses (the pack loads
    under its own ComfyUI-style package name, so this is NOT
    `mrln.nodes.prompt`)."""
    return sys.modules[cls.__module__]


def _node_api(cls):
    package = sys.modules[cls.__module__].__package__  # <pack>.mrln.nodes
    return importlib.import_module(package.rsplit(".", 1)[0] + ".promptapi")


def _fake_chat(monkeypatch, cls):
    """Canned rewrite; returns the log of prompts the backend actually saw."""
    seen = []

    def fake(lib, **kwargs):
        seen.append(kwargs["prompt"])
        return f"rewritten: {kwargs['prompt']}"

    monkeypatch.setattr(_node_api(cls), "llm_chat", fake)
    return seen


def _enhance(node, prompt):
    return node.execute(
        backend="ollama",
        model="gemma3",
        temperature=0.2,
        seed=0,
        max_tokens=64,
        timeout=5,
        free_vram="after call",
        on_error="pass through",
        llm="",
        prompt=prompt,
        system="rewrite",
    )


@pytest.fixture()
def enhance(classes, monkeypatch):
    """A PromptEnhance node with a canned backend and an EMPTY cache — the
    cache is module state shared across the session."""
    cls = classes["MRLN_PromptEnhance"]
    module = _node_module(cls)
    monkeypatch.setattr(module, "_ENHANCE_CACHE", type(module._ENHANCE_CACHE)())
    seen = _fake_chat(monkeypatch, cls)
    return cls(), module, seen


def test_enhance_cache_is_bounded(enhance):
    """Unbounded, this dict kept every rewrite (full system text + full prompt
    + full completion) for the server's lifetime, and control_after_generate
    makes repeats rare — a leak by construction."""
    node, module, seen = enhance
    calls = 400
    for index in range(calls):
        _enhance(node, f"distinct prompt number {index}")
    assert len(seen) == calls  # every prompt really was a miss
    # the symptom: 400 unique rewrites must NOT leave 400 entries behind
    assert len(module._ENHANCE_CACHE) < calls
    assert len(module._ENHANCE_CACHE) <= module._ENHANCE_CACHE_MAX


def test_enhance_cache_evicts_least_recently_used(enhance):
    node, module, seen = enhance
    cap = module._ENHANCE_CACHE_MAX
    for index in range(cap):
        _enhance(node, f"p{index}")
    assert len(seen) == cap
    # touch the OLDEST entry: a hit must re-date it, not just serve it
    _, report = _enhance(node, "p0")
    assert "(cached)" in report and len(seen) == cap
    _enhance(node, "one too many")  # forces exactly one eviction
    assert len(module._ENHANCE_CACHE) == cap
    # the re-touched entry survived ...
    _, report = _enhance(node, "p0")
    assert "(cached)" in report
    # ... and the one that was oldest after that touch is gone
    _, report = _enhance(node, "p1")
    assert "(cached)" not in report


def test_enhance_cache_still_serves_a_repeat(enhance):
    """The eviction must not cost the feature it guards: a re-queue of the
    same inputs never re-calls the backend."""
    node, _module, seen = enhance
    first, _ = _enhance(node, "a bright red car")
    second, report = _enhance(node, "a bright red car")
    assert second == first and len(seen) == 1 and "(cached)" in report


# ---------------------------------------------------------------------------
# Append-only contract: widgets_values and output links are POSITIONAL in
# saved workflows, so any reorder or insertion silently corrupts them.
# ---------------------------------------------------------------------------

FROZEN_ORDER = {
    "MRLN_LoraApply": {
        "required": ["model", "clip", "loras"],
        "optional": ["on_missing", "on_mismatch"],
        "outputs": ["model", "clip", "report"],
    },
    "MRLN_PromptEnhance": {
        "required": [
            "backend",
            "model",
            "temperature",
            "seed",
            "max_tokens",
            "timeout",
            "free_vram",
            "on_error",
        ],
        "optional": ["llm", "prompt", "system"],
        "outputs": ["prompt", "report"],
    },
    "MRLN_PromptSection": {
        "required": ["section", "item", "seed", "allow_empty"],
        "optional": [],
        "outputs": ["text", "negative", "choice"],
    },
    # RE-CUT ONCE, before shipping, on the author's explicit call: the widgets
    # now read in the order the work happens — what to render, what goes in,
    # how it draws, what comes out — instead of the order they were added in.
    # From here it is append-only again.
    "MRLN_PromptTemplate": {
        "required": [
            "template",  # what
            "template_names",  # …and how the widget above names it
            "trigger",  # what goes in
            "selection",
            "selection_mode",
            "seed",  # how it draws
            "format",  # what comes out
            "text_length",
            "conflict_policy",
        ],
        "optional": ["variables", "profile", "batch_count", "batch_mode"],
        "outputs": ["prompt", "negative", "choices", "loras", "llm"],
    },
    "MRLN_ShowText": {
        "required": ["value"],
        "optional": [],
        "outputs": ["text"],
    },
}


def test_widget_and_output_order_is_append_only(classes):
    """A saved workflow stores widgets_values POSITIONALLY and links outputs
    by index. Adding a widget/output at the END is safe; inserting or
    reordering ANYWHERE else silently rewires every existing user workflow.
    Extend the lists below when you append — never edit an existing entry."""
    assert set(classes) == set(FROZEN_ORDER), "node id set changed (IDs are frozen)"
    for node_id, frozen in FROZEN_ORDER.items():
        cls = classes[node_id]
        inputs = cls.INPUT_TYPES()
        actual_required = list(inputs.get("required", {}))
        actual_optional = list(inputs.get("optional", {}))
        assert actual_required[: len(frozen["required"])] == frozen["required"], node_id
        assert actual_optional[: len(frozen["optional"])] == frozen["optional"], node_id
        outputs = list(cls.RETURN_NAMES)
        assert outputs[: len(frozen["outputs"])] == frozen["outputs"], node_id
        assert len(cls.RETURN_TYPES) == len(outputs), node_id


def test_every_input_entry_carries_a_tooltip(classes):
    """Every widget/input of every node — the pack's documentation promise."""
    for node_id, cls in classes.items():
        for group in cls.INPUT_TYPES().values():
            for name, spec in group.items():
                assert len(spec) == 2 and spec[1].get("tooltip"), f"{node_id}.{name}"


def test_lora_report_is_json_serializable_and_stringy(classes):
    """The three STRING outputs of the Template node must always be strings —
    downstream text nodes concatenate them without guarding for None."""
    out = classes["MRLN_PromptTemplate"]().execute(
        template="overdrive/full-shot",
        selection="",
        selection_mode="as configured",
        seed=0,
        format="template default",
    )
    # SPEC 4.2 made every output a LIST of strings (OUTPUT_IS_LIST); the
    # promise is unchanged — no None ever reaches a downstream text node.
    assert all(isinstance(values, list) and values for values in out)
    assert all(isinstance(value, str) for values in out for value in values)
    assert isinstance(json.loads(out[3][0]), list)
