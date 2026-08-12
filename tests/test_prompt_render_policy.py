"""Phase 3 — the optimize-for-model RENDER POLICY.

Reading order changes what a model produces and no template can store one
order per model, so the order is a render-time function of the PROFILE:
one template on disk, every profile renders it in the shape its target
model rewards. These tests pin the mechanism (block_order + negative_policy);
authoring the 29 profiles' policies is data, not engine.
"""

import json

import pytest
import support  # noqa: F401

from mrln.promptlib import (
    NEUTRAL_RANK,
    Library,
    RenderError,
    RenderPolicy,
    block_domain,
    compose,
    render,
    resolve_template,
)

# authored order of templates/shot.json — the order on disk, never rewritten
AUTHORED = ("subject", "setting", "style", "lighting", "camera", "mood")
AUTHORED_TEXT = (
    "a lone driver",
    "rain-slicked downtown",
    "oil painting",
    "moonlit night",
    "35mm lens",
    "epic composition",
)


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def lib(tmp_path):
    """A library whose sections span five domains + one domain no policy
    names, so 'moves only what it names' is observable."""
    factory = tmp_path / "factory"
    for slug, items in (
        ("subject/people", [("driver", "a lone driver"), ("racer", "a helmeted racer")]),
        ("setting/city", [("downtown", "rain-slicked downtown"), ("bridge", "a steel bridge")]),
        ("style/paint", [("oil", "oil painting"), ("ink", "ink wash")]),
        ("lighting/night", [("moon", "moonlit night"), ("neon", "neon glow")]),
        ("camera/lens", [("35mm", "35mm lens"), ("85mm", "85mm portrait lens")]),
        ("mood", [("epic", "epic composition"), ("calm", "calm composition")]),
    ):
        _write(
            factory,
            f"sections/{slug}.json",
            {"items": [{"name": n, "text": t} for n, t in items]},
        )
    _write(
        factory,
        "templates/shot.json",
        {
            "label": "Shot",
            "negative": "lowres, blurry, oil painting",
            "slots": [
                {"id": "subject", "ref": "subject/people", "default": "driver"},
                {"id": "setting", "ref": "setting/city", "default": "downtown"},
                {"id": "style", "ref": "style/paint", "default": "oil"},
                {"id": "lighting", "ref": "lighting/night", "default": "moon"},
                {"id": "camera", "ref": "camera/lens", "default": "35mm"},
                {"id": "mood", "ref": "mood", "default": "epic"},
            ],
            "render": {"format": "string", "joiner": ", "},
            "profiles": {
                # a prose family: subject/setting lead, technical blocks last,
                # and the model ignores negatives at CFG 1
                "prose": {
                    "render": {
                        "block_order": {
                            "subject": 10,
                            "setting": 20,
                            "style": 30,
                            "lighting": 40,
                            "camera": 60,
                        },
                        "negative_policy": "drop",
                    }
                },
                # a tag family: quality tags lead, negatives are a house preset
                "tagged": {
                    "render": {
                        "block_order": {"style": 10, "camera": 20},
                        "negative_policy": "preset",
                        "negative_preset": "worst quality, oil painting",
                    }
                },
                # only names one domain: everything else keeps authored order
                "partial": {"render": {"block_order": {"camera": 10}}},
                # explicit no-ops: must render byte-identically to standard
                "keeper": {"render": {"negative_policy": "keep"}},
                "plain": {"llm": {"system": "NO RENDER BLOCK"}},
                # ranks that agree with the authored order: nothing moves
                "noop-order": {"render": {"block_order": {"subject": 10, "setting": 20}}},
                # ties: equal ranks fall back to authored order (stable sort)
                "tied": {"render": {"block_order": {"camera": 10, "style": 10}}},
            },
        },
    )
    return Library(factory, None)


def run(lib, **kw):
    args = {
        "seed": 0,
        "mode": "as configured",
        "selection": {},
        "variables": {},
        "template": "shot",
    }
    args.update(kw)
    tpl = lib.load_template(args.pop("template"))
    return compose(lib, tpl, **args)


def fragments(positive):
    return positive.split(", ")


def drawn(composed):
    """{slot id: (item, seed it drew with)} — the seeding result, which the
    policy must never touch."""
    return {s.id: (s.item_name, s.seed_used) for s in composed.resolved.slots}


# -- block_order: the sort ---------------------------------------------------


def test_block_domain_is_the_first_slug_segment(lib):
    composed = run(lib)
    domains = [block_domain(s) for s in composed.resolved.slots]
    assert domains == ["subject", "setting", "style", "lighting", "camera", "mood"]


def test_block_order_reorders_the_positive_and_is_deterministic(lib):
    reverse = RenderPolicy.from_render(
        {"block_order": {"camera": 10, "lighting": 20, "style": 30, "setting": 40, "subject": 50}},
        profile="reverse",
    )
    tpl = lib.load_template("shot")
    resolved = resolve_template(lib, tpl, seed=7, mode="as configured", selection={}, variables={})
    first = render(resolved, "string", tpl.render, policy=reverse)
    again = render(resolved, "string", tpl.render, policy=reverse)
    assert first == again  # same inputs, same bytes — nothing here is order-of-dict luck
    assert fragments(first.positive) == [
        "35mm lens",
        "moonlit night",
        "oil painting",
        "rain-slicked downtown",
        "a lone driver",
        "epic composition",  # unlisted 'mood' sits at the neutral rank, after 'setting'(40)
    ]


def test_reorder_is_content_preserving(lib):
    standard = fragments(run(lib).rendered.positive)
    optimized = fragments(run(lib, profile="prose").rendered.positive)
    assert optimized != standard  # different order …
    assert sorted(optimized) == sorted(standard)  # … same multiset of blocks
    assert sorted(standard) == sorted(AUTHORED_TEXT)


def test_equal_ranks_keep_authored_order(lib):
    # 'tied' ranks style and camera both at 10: a stable sort keeps style
    # (authored index 2) ahead of camera (index 4).
    out = fragments(run(lib, profile="tied").rendered.positive)
    assert out[:2] == ["oil painting", "35mm lens"]


def test_partial_policy_moves_only_what_it_names(lib):
    out = fragments(run(lib, profile="partial").rendered.positive)
    assert out == [
        "35mm lens",  # the only named domain, ranked 10
        "a lone driver",
        "rain-slicked downtown",
        "oil painting",
        "moonlit night",
        "epic composition",  # everything unlisted keeps its authored order at rank 50
    ]


def test_unlisted_domain_sits_at_the_neutral_rank(lib):
    # 'mood' is unlisted; ranks below NEUTRAL_RANK pull ahead of it, above push behind.
    early = RenderPolicy.from_render({"block_order": {"camera": NEUTRAL_RANK - 1}}, profile="p")
    late = RenderPolicy.from_render({"block_order": {"camera": NEUTRAL_RANK + 1}}, profile="p")
    tpl = lib.load_template("shot")
    resolved = resolve_template(lib, tpl, seed=0, mode="as configured", selection={}, variables={})
    assert (
        fragments(render(resolved, "string", tpl.render, policy=early).positive)[0] == "35mm lens"
    )
    assert fragments(render(resolved, "string", tpl.render, policy=late).positive)[-1] == (
        "35mm lens"
    )


def test_empty_domain_slots_never_shuffle_the_output(lib):
    # A muted slot carries no section, so block_domain is '' and it renders
    # nothing: a policy naming only such a domain changes no byte.
    composed = run(lib, selection={"camera": "off"})
    muted = next(s for s in composed.resolved.slots if s.id == "camera")
    assert block_domain(muted) == ""
    policy = RenderPolicy.from_render({"block_order": {"camera": 10}}, profile="ghost")
    tpl = lib.load_template("shot")
    resolved = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={"camera": "off"}, variables={}
    )
    assert render(resolved, "string", tpl.render, policy=policy) == render(
        resolved, "string", tpl.render
    )


# -- the seeding algorithm is frozen ----------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 42, 9999])
def test_same_seed_draws_the_same_items_with_and_without_the_policy(lib, seed):
    plain = run(lib, seed=seed, mode="randomize all")
    optimized = run(lib, seed=seed, mode="randomize all", profile="prose")
    assert drawn(optimized) == drawn(plain)  # reorder happens AFTER resolution
    assert sorted(fragments(optimized.rendered.positive)) == sorted(
        fragments(plain.rendered.positive)
    )
    assert fragments(optimized.rendered.positive) != fragments(plain.rendered.positive)


# -- formats -----------------------------------------------------------------


def test_string_labeled_follows_the_policy(lib):
    labels = [
        line.split(":")[0]
        for line in run(lib, profile="tagged", format="string_labeled").rendered.positive.split(
            "\n"
        )
    ]
    assert labels == ["Paint", "Lens", "People", "City", "Night", "Mood"]


def test_json_key_order_follows_the_policy(lib):
    payload = json.loads(run(lib, profile="prose", format="json").rendered.positive)
    assert list(payload) == ["subject", "setting", "style", "lighting", "mood", "camera"]
    plain = json.loads(run(lib, format="json").rendered.positive)
    assert list(plain) == list(AUTHORED)  # the template's own order is untouched
    assert payload == plain  # same keys, same values — only the order differs


def test_json_flat_follows_the_policy(lib):
    flat = json.loads(run(lib, profile="partial", format="json_flat").rendered.positive)
    assert flat["prompt"].startswith("35mm lens, a lone driver")


# -- negative_policy ---------------------------------------------------------


def test_drop_empties_the_negative_but_the_template_keeps_it(lib):
    dropped = run(lib, profile="prose")
    assert dropped.rendered.negative == ""
    # the file on disk is untouched — a profile is a DEFAULT, not a lock, so
    # the negative must still be there when the widget switches profile
    assert lib.load_template("shot").negative == "lowres, blurry, oil painting"
    assert run(lib).rendered.negative == "lowres, blurry, oil painting"


def test_preset_replaces_the_negative(lib):
    # the template's own "lowres, blurry, oil painting" never reaches the output
    assert run(lib, profile="tagged").rendered.negative == "worst quality, oil painting"
    assert lib.load_template("shot").negative == "lowres, blurry, oil painting"


def test_keep_and_no_policy_render_byte_identically(lib):
    standard = run(lib).rendered
    for profile in ("keeper", "plain", "noop-order"):
        assert run(lib, profile=profile).rendered == standard, profile


def test_conflict_policy_applies_after_the_negative_policy(lib):
    # 'oil painting' is in both the preset negative and the positive
    kept = run(lib, profile="tagged")
    assert "oil painting" in kept.rendered.negative
    assert "conflict: 'oil painting'" in kept.rendered.choices
    prevails = run(lib, profile="tagged", conflict_policy="positive prevails")
    assert prevails.rendered.negative == "worst quality"
    # a dropped negative has no terms left to conflict with
    assert "conflict:" not in run(lib, profile="prose").rendered.choices


def test_drop_removes_the_negative_key_from_a_json_scaffold(lib, tmp_path):
    _write(
        tmp_path / "factory",
        "templates/api.json",
        {
            "negative": "lowres",
            "slots": [{"id": "subject", "ref": "subject/people", "default": "driver"}],
            "profiles": {
                "noneg": {
                    "render": {"negative_policy": "drop"},
                    "json_template": {"prompt": "{positive}", "negative_prompt": "{negative}"},
                }
            },
        },
    )
    lib.invalidate()
    payload = json.loads(run(lib, template="api", profile="noneg").rendered.positive)
    assert payload == {"prompt": "a lone driver"}  # the unfilled optional drops out


# -- the choices report ------------------------------------------------------


def test_choices_keeps_authored_order_and_notes_the_reorder(lib):
    choices = run(lib, profile="prose").rendered.choices
    reported = [line.split(":")[0] for line in choices.splitlines() if ": " in line]
    assert [r for r in reported if r in AUTHORED] == list(AUTHORED)
    assert "order: optimized for prose" in choices.splitlines()
    assert (
        "negative: dropped for prose — the template keeps its negative; "
        "switch the profile to use it" in choices.splitlines()
    )


def test_choices_mentions_the_reorder_only_when_something_moved(lib):
    assert "order: optimized" not in run(lib, profile="noop-order").rendered.choices
    assert "order: optimized" not in run(lib).rendered.choices
    assert "negative:" not in run(lib, profile="keeper").rendered.choices
    assert "order: optimized for partial" in run(lib, profile="partial").rendered.choices


# -- malformed policy data ---------------------------------------------------


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ({"block_order": ["subject", "setting"]}, "must be an object mapping a block domain"),
        ({"block_order": "subject"}, "got str"),
        ({"block_order": {"subject": "first"}}, "rank for 'subject' must be a finite number"),
        ({"block_order": {"subject": True}}, "rank for 'subject' must be a finite number"),
        ({"block_order": {"subject": float("nan")}}, "must be a finite number"),
        ({"block_order": {"subject": None}}, "must be a finite number"),
        ({"block_order": {"  ": 10}}, "empty domain key"),
        ({"negative_policy": "nuke"}, "unknown negative_policy 'nuke'"),
        ({"negative_policy": 3}, "unknown negative_policy 3"),
        ({"negative_policy": "preset"}, "needs a non-empty 'negative_preset'"),
        ({"negative_policy": "preset", "negative_preset": "  "}, "non-empty 'negative_preset'"),
        ({"negative_preset": ["a"]}, "'negative_preset' must be a string"),
    ],
)
def test_malformed_policy_raises_a_render_error(block, match):
    with pytest.raises(RenderError, match=match) as excinfo:
        RenderPolicy.from_render(block, profile="krea2")
    assert "krea2" in str(excinfo.value)  # the message names the profile to fix


def test_malformed_policy_fails_through_compose(lib, tmp_path):
    _write(
        tmp_path / "factory",
        "profiles.json",
        {"profiles": {"broken": {"render": {"block_order": {"subject": "first"}}}}},
    )
    lib.invalidate()
    with pytest.raises(RenderError, match="profile 'broken': render 'block_order'"):
        run(lib, profile="broken")


# -- the policy object itself ------------------------------------------------


def test_no_knobs_means_no_policy_object():
    # the compatibility contract: a profile that names neither knob leaves
    # render() on exactly the path it took before Phase 3
    assert RenderPolicy.from_render({}) is None
    assert RenderPolicy.from_render({"format": "json", "text_length": "short"}) is None
    assert RenderPolicy.from_render({"negative_policy": "keep"}) is None
    assert RenderPolicy.from_render({"block_order": {}}) is None
    assert RenderPolicy.from_render(None) is None
    assert RenderPolicy.from_render({"block_order": {"subject": 10}}).block_order == {
        "subject": 10.0
    }
