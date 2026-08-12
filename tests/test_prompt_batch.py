"""Batch & combinatorial output on the Prompt Template node (SPEC 4.2).

The bug this feature fixes is the loudest one in the ComfyUI prompting
ecosystem: set the sampler's batch size to 4 and Comfy renders four copies of
one draw. MRLN's per-slot deterministic seeding lets that be solved exactly
rather than approximately — so these tests pin the exactness:

* a length-1 list must be byte-for-byte what the node emitted BEFORE the
  feature existed (the golden strings below were captured from the pre-change
  code path at ad91a71 and must never be edited to match new behavior),
* item i must render with master seed + i and nothing else,
* combinatorial mode must enumerate the random slots in authored order,
* the same seed and library must give the same batch, in the same order,
  every time.
"""

import json

import pytest
import support

# ---------------------------------------------------------------------------
# A tiny user-tier library: 3 colors x 2 lights, so a whole combinatorial
# space fits in an assertion and every golden string stays readable.
# ---------------------------------------------------------------------------

COLORS = [
    {"name": "red", "text": "bright red"},
    {"name": "green", "text": "deep green"},
    {"name": "blue", "text": "ocean blue"},
]
LIGHTS = [
    {"name": "day", "text": "bright daylight"},
    {"name": "night", "text": "moonlit night"},
]

# Captured from the PRE-change node (single-value outputs) with seed 7.
GOLDEN = (
    "a car, bright red, moonlit night",
    "",
    "template: batch/tiny   seed: 7   mode: as configured   format: string\n"
    "color: red  [random]  (user)\n"
    "light: night  [random]  (user)",
    "[]",
    '{"target": "standard", "prompt": "a car, bright red, moonlit night"}',
)

# Pre-change single renders at seeds 7, 8, 9, 10 — the reference an
# 'increment seed' batch of 4 starting at seed 7 must reproduce exactly.
GOLDEN_SEED_WALK = [
    "a car, bright red, moonlit night",
    "a car, deep green, bright daylight",
    "a car, bright red, bright daylight",
    "a car, ocean blue, moonlit night",
]

# color-major, light varying fastest = authored slot order
GOLDEN_PRODUCT = [
    "a car, bright red, bright daylight",
    "a car, bright red, moonlit night",
    "a car, deep green, bright daylight",
    "a car, deep green, moonlit night",
    "a car, ocean blue, bright daylight",
    "a car, ocean blue, moonlit night",
]


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def user_tier(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(user))
    _write(user, "sections/batch/color.json", {"label": "Color", "items": COLORS})
    _write(user, "sections/batch/light.json", {"label": "Light", "items": LIGHTS})
    _write(
        user,
        "templates/batch/tiny.json",
        {
            "label": "Tiny",
            "prefix": "a car",
            "slots": [
                {"id": "color", "ref": "batch/color", "default": "random"},
                {"id": "light", "ref": "batch/light", "default": "random"},
            ],
            "render": {"format": "string", "joiner": ", "},
        },
    )
    return user


@pytest.fixture(scope="module")
def classes():
    return support.load_pack().NODE_CLASS_MAPPINGS


@pytest.fixture()
def node(classes):
    return classes["MRLN_PromptTemplate"]()


def run(node, **kw):
    """Execute with the pre-batch argument set; kwargs add/override."""
    args = {
        "template": "batch/tiny",
        "selection": "",
        "selection_mode": "as configured",
        "seed": 7,
        "format": "template default",
    }
    args.update(kw)
    return node.execute(**args)


# ---------------------------------------------------------------------------
# Compatibility: a length-1 list must BE the old single value
# ---------------------------------------------------------------------------


def test_default_call_reproduces_the_pre_change_golden_as_length_one_lists(node):
    """The whole feature rides on this: OUTPUT_IS_LIST turns every output into
    a list, and ComfyUI treats a length-1 list exactly like a single value —
    so an existing workflow must keep rendering the identical five strings.
    The call deliberately passes NO batch arguments, which is exactly what a
    workflow saved before this feature does."""
    out = run(node)
    # the 5 outputs GOLDEN was captured from, plus gen_info APPENDED at the end
    # by SPEC 6.4 — GOLDEN itself stays untouched, which is the point of it
    assert len(out) == 6
    for index, (values, golden) in enumerate(zip(out[:5], GOLDEN, strict=True)):
        assert isinstance(values, list), f"output {index} is not a list"
        assert values == [golden], f"output {index} drifted from the pre-change render"
    # the appended output is a length-1 list too (its content: test_prompt_gen_info.py)
    assert out[5] == ["a car, bright red, moonlit night\nSeed: 7"]
    # and no batch bookkeeping leaks into a single render
    assert "batch " not in out[2][0]


def test_batch_count_one_is_identical_to_omitting_the_widgets(node):
    """Explicit defaults and absent defaults are the same render — the widget
    pair is inert until asked for."""
    assert run(node, batch_count=1, batch_mode="increment seed") == run(node)
    # combinatorial with nothing random left is a single render too, and takes
    # the same untouched path (no mode switch, no batch line)
    fixed = run(node, selection_mode="all fixed defaults")
    assert run(node, selection_mode="all fixed defaults", batch_mode="combinatorial") == fixed
    assert len(fixed[0]) == 1
    assert "mode: all fixed defaults" in fixed[2][0]


def test_output_is_list_covers_every_output(classes):
    cls = classes["MRLN_PromptTemplate"]
    assert cls.OUTPUT_IS_LIST == (True,) * 6  # 5 + gen_info (SPEC 6.4)
    assert len(cls.OUTPUT_IS_LIST) == len(cls.RETURN_TYPES) == len(cls.RETURN_NAMES)


# ---------------------------------------------------------------------------
# Append-only: widgets_values are POSITIONAL in saved workflows
# ---------------------------------------------------------------------------


def test_batch_widgets_append_after_profile(classes):
    """Ruling D2: the retired 'profile stays last' convention is replaced by
    the harder rule it was standing in for — append-only. Both new widgets go
    at the END of the optional group; anything else silently rewires every
    saved workflow's widgets_values."""
    inputs = classes["MRLN_PromptTemplate"].INPUT_TYPES()
    assert list(inputs["required"]) == [
        "template",
        "trigger",
        "selection",
        "selection_mode",
        "seed",
        "format",
        "conflict_policy",
        "text_length",
    ]
    assert list(inputs["optional"]) == ["variables", "profile", "batch_count", "batch_mode"]
    assert list(classes["MRLN_PromptTemplate"].RETURN_NAMES) == [
        "prompt",
        "negative",
        "choices",
        "loras",
        "llm",
        "gen_info",  # appended by SPEC 6.4; proof lives in test_prompt_gen_info.py
    ]


def test_batch_widget_specs_and_tooltips(classes):
    inputs = classes["MRLN_PromptTemplate"].INPUT_TYPES()
    kind, spec = inputs["optional"]["batch_count"]
    assert kind == "INT"
    assert (spec["default"], spec["min"], spec["max"]) == (1, 1, 64)
    tooltip = spec["tooltip"].lower()
    assert "list" in tooltip and "seed + i" in tooltip
    assert "ignored" in tooltip and "combinatorial" in tooltip  # the surprising bit, stated

    options, spec = inputs["optional"]["batch_mode"]
    assert options == ["increment seed", "combinatorial"]
    assert spec["default"] == "increment seed"
    assert "512" in spec["tooltip"]


# ---------------------------------------------------------------------------
# increment seed
# ---------------------------------------------------------------------------


def test_increment_seed_walks_the_master_seed(node):
    """Item i renders with master seed + i — asserted against renders the
    PRE-change node produced at those seeds, so the batch cannot quietly
    redefine what a seed draws."""
    out = run(node, batch_count=4)
    assert out[0] == GOLDEN_SEED_WALK
    assert all(len(values) == 4 for values in out)


def test_increment_seed_items_equal_the_single_renders(node):
    """Same claim, proven against the node's own single-render path rather
    than a literal: a batch of N is N single renders at seed..seed+N-1."""
    batched = run(node, batch_count=5, seed=1234)
    singles = [run(node, seed=1234 + i) for i in range(5)]
    # output 2 (choices) carries the extra batch line and is asserted below
    for output in (0, 1, 3, 4):
        assert batched[output] == [single[output][0] for single in singles], output


def test_batch_line_prefixes_every_item_and_only_when_batched(node):
    """N > 1 gains a first line naming the position and the seed used; the
    rest of the report is byte-identical to that item's single render, and at
    N == 1 the line is absent entirely."""
    batched = run(node, batch_count=3, seed=1234)
    for index in range(3):
        head, _, rest = batched[2][index].partition("\n")
        assert head == f"batch {index + 1}/3 (seed {1234 + index})"
        assert rest == run(node, seed=1234 + index)[2][0]
    assert not run(node, batch_count=1)[2][0].startswith("batch ")


def test_batch_count_is_clamped_not_trusted(node):
    """The widget bounds it, but an API-submitted workflow can send anything;
    a 0 or a negative must never yield an empty output list."""
    assert len(run(node, batch_count=0)[0]) == 1
    assert len(run(node, batch_count=-5)[0]) == 1
    assert len(run(node, batch_count=99)[0]) == 64


# ---------------------------------------------------------------------------
# combinatorial
# ---------------------------------------------------------------------------


def test_combinatorial_enumerates_the_random_slots_in_authored_order(node):
    out = run(node, batch_mode="combinatorial")
    assert out[0] == GOLDEN_PRODUCT
    assert all(len(values) == 6 for values in out)


def test_combinatorial_ignores_batch_count(node):
    """The product size sets the output length — the tooltip says so, and a
    user who leaves batch_count at 8 must not get 8 of a 6-item space."""
    for count in (1, 8, 64):
        assert run(node, batch_mode="combinatorial", batch_count=count)[0] == GOLDEN_PRODUCT


def test_combinatorial_pins_each_combination_and_names_the_axes(node):
    out = run(node, batch_mode="combinatorial")
    head, _, rest = out[2][0].partition("\n")
    assert head == "batch 1/6 (seed 7)   combinatorial: color, light"
    # every enumerated slot is PINNED for that item, which is what makes the
    # product exhaustive instead of a lucky sequence of draws
    assert "color: red  [fixed]" in rest
    assert "light: day  [fixed]" in rest
    assert out[2][5].startswith("batch 6/6 (seed 7)")


def test_combinatorial_takes_fixed_slots_out_of_the_product(node):
    """The documented remediation for the cap has to actually work: one
    selection line removes one axis."""
    out = run(node, batch_mode="combinatorial", selection="color=green")
    assert out[0] == ["a car, deep green, bright daylight", "a car, deep green, moonlit night"]
    out = run(node, batch_mode="combinatorial", selection="color=green\nlight=night")
    assert out[0] == ["a car, deep green, moonlit night"]
    assert not out[2][0].startswith("batch ")  # collapsed to a single render


def test_combinatorial_works_under_randomize_all(node):
    """'randomize all' re-rolls every slot by design, which would fight a pin.
    The enumerated renders switch to 'as configured' and pin explicitly, so
    the mode that most needs combinatorial output is the one that must work."""
    out = run(node, batch_mode="combinatorial", selection_mode="randomize all")
    assert out[0] == GOLDEN_PRODUCT


def test_combinatorial_skips_muted_slots(node):
    out = run(node, batch_mode="combinatorial", selection="light=off")
    assert out[0] == ["a car, bright red", "a car, deep green", "a car, ocean blue"]
    assert "combinatorial: color" in out[2][0]


def test_combinatorial_excludes_weight_zero_items(node, user_tier):
    """weight 0 means 'never draw' to the engine, so it is not part of the
    random space and must not appear in the enumeration of that space."""
    _write(
        user_tier,
        "sections/batch/color.json",
        {"label": "Color", "items": [*COLORS, {"name": "ghost", "text": "never", "weight": 0}]},
    )
    out = run(node, batch_mode="combinatorial")
    assert out[0] == GOLDEN_PRODUCT
    assert not any("never" in prompt for prompt in out[0])


def test_combinatorial_pins_the_drawn_variant(node, user_tier):
    """A random variant is 'remaining randomness': it stays on the master
    seed rather than becoming another axis, and every item then shares it."""
    _write(
        user_tier,
        "templates/batch/var.json",
        {
            "label": "Var",
            "slots": [{"id": "color", "ref": "batch/color", "default": "random"}],
            "variants": [
                {"name": "a", "slots": [{"id": "tone", "ref": "batch/light", "default": "day"}]},
                {"name": "b", "slots": [{"id": "tone", "ref": "batch/light", "default": "night"}]},
            ],
            "variant_default": "a",
            "order": ["@variant", "color"],
            "render": {"format": "string", "joiner": ", "},
        },
    )
    out = run(
        node, template="batch/var", batch_mode="combinatorial", selection_mode="randomize all"
    )
    variants = {line for report in out[2] for line in report.splitlines() if "variant:" in line}
    assert len(variants) == 1, "the variant must be constant across the batch"
    # tone and color are both random under 'randomize all' -> 2 x 3
    assert len(out[0]) == 6


def test_combinatorial_cap_names_the_size_and_the_way_out(node, user_tier):
    """Over the cap the node must refuse with something actionable — the
    computed size, the cap, the per-slot space, and what to do about it."""
    for index in range(3):
        _write(
            user_tier,
            f"sections/batch/big{index}.json",
            {"items": [{"name": f"i{n}", "text": f"item {n}"} for n in range(9)]},
        )
    _write(
        user_tier,
        "templates/batch/huge.json",
        {
            "slots": [
                {"id": f"s{index}", "ref": f"batch/big{index}", "default": "random"}
                for index in range(3)
            ],
            "render": {"format": "string", "joiner": ", "},
        },
    )
    with pytest.raises(ValueError) as excinfo:
        run(node, template="batch/huge", batch_mode="combinatorial")
    message = str(excinfo.value)
    assert "729" in message  # 9 x 9 x 9, the computed size
    assert "512" in message  # the cap
    assert "s0:9 x s1:9 x s2:9" in message  # where the size comes from
    assert "fix more slots" in message and "selection box" in message
    # exactly the remediation the message advertises brings it under the cap
    out = run(node, template="batch/huge", batch_mode="combinatorial", selection="s0=i0")
    assert len(out[0]) == 81
    # ... and 'increment seed' is never capped by the space
    assert len(run(node, template="batch/huge", batch_count=4)[0]) == 4


# ---------------------------------------------------------------------------
# determinism — the product promise
# ---------------------------------------------------------------------------


def test_same_seed_gives_the_same_batch_in_the_same_order(node):
    """Same seed + same library = the same batch, same order, every time —
    for both modes, across fresh library opens (each execute re-opens)."""
    for kwargs in (
        {"batch_count": 8},
        {"batch_count": 8, "selection_mode": "randomize all"},
        {"batch_mode": "combinatorial"},
        {"batch_mode": "combinatorial", "selection_mode": "randomize all"},
    ):
        runs = [run(node, seed=4242, **kwargs) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2], kwargs
        assert len(runs[0][0]) > 1


def test_a_different_seed_moves_the_increment_batch(node):
    a = run(node, batch_count=6, seed=100)[0]
    b = run(node, batch_count=6, seed=200)[0]
    assert a != b
    # a batch is not N copies of one draw — the bug this feature exists to fix
    assert len(set(a)) > 1


def test_every_output_stays_aligned_across_the_batch(node, user_tier):
    """The five outputs are parallel lists: index i of each belongs to the
    same render. A LoRA-bearing slot proves loras/llm travel per item too."""
    _write(
        user_tier,
        "sections/batch/kit.json",
        {
            "items": [
                {"name": "none", "text": "stock body"},
                {
                    "name": "wide",
                    "text": "WideBodyKit",
                    "data": {"lora": "kits/wide.safetensors", "strength_model": 0.8},
                },
            ]
        },
    )
    _write(
        user_tier,
        "templates/batch/kitted.json",
        {
            "slots": [{"id": "kit", "ref": "batch/kit", "default": "random"}],
            "render": {"format": "string", "joiner": ", "},
        },
    )
    prompts, negatives, choices, loras, llms, gen_info = run(
        node, template="batch/kitted", batch_mode="combinatorial"
    )
    assert (
        len(prompts) == len(negatives) == len(choices) == len(loras) == len(llms) == len(gen_info)
    ) and len(prompts) == 2
    assert prompts == ["stock body", "WideBodyKit"]
    assert json.loads(loras[0]) == []
    assert json.loads(loras[1])[0]["lora"] == "kits/wide.safetensors"
    assert json.loads(llms[1])["prompt"] == "WideBodyKit"
    assert json.loads(llms[1])["protect"] == ["WideBodyKit"]
