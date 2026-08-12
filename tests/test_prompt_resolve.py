import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln.promptlib import (
    ItemNotFoundError,
    SelectionError,
    parse_kv_lines,
    parse_template,
    resolve_section,
    resolve_template,
)


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def rt(lib, tpl_slug="basic", seed=0, mode="as configured", selection=None, variables=None):
    tpl = lib.load_template(tpl_slug)
    return resolve_template(
        lib, tpl, seed=seed, mode=mode, selection=selection or {}, variables=variables or {}
    )


def slot(resolved, slot_id):
    return next(s for s in resolved.slots if s.id == slot_id)


# -- kv lines ---------------------------------------------------------------


def test_parse_kv_lines():
    parsed = parse_kv_lines("# comment\n\npaint=red\nlocation = urban/shibuya \n")
    assert parsed == {"paint": "red", "location": "urban/shibuya"}


@pytest.mark.parametrize(
    "text,match",
    [
        ("noequals", "name=value"),
        ("=x", "empty name"),
        ("a=1\na=2", "duplicate"),
    ],
)
def test_parse_kv_lines_errors(text, match):
    with pytest.raises(SelectionError, match=match):
        parse_kv_lines(text)


# -- mirror contract with the Composer's JS helpers --------------------------
# web/js/composer/util.js re-implements parse_kv_lines and _parse_token for
# the panel. The split is DELIBERATE — the panel must never refuse to render
# a half-typed line, the engine must never guess at one — but it has to stay
# deliberate. These vectors are the same ones tests/js/util.test.mjs asserts
# on the JS side; if either mirror moves, exactly one of the two suites goes
# red. Change both together or not at all.


@pytest.mark.parametrize(
    "text,expected",
    [
        ("  subject = woman \nsetting=street", {"subject": "woman", "setting": "street"}),
        ("expr=a=b=c", {"expr": "a=b=c"}),  # only the FIRST '=' splits
        ("subject=woman # not a comment", {"subject": "woman # not a comment"}),
        ("subject=woman\r\nsetting=street\r\n", {"subject": "woman", "setting": "street"}),
        ("# a comment\n\n   \nsubject=woman", {"subject": "woman"}),
        ("", {}),
    ],
)
def test_parse_kv_lines_agrees_with_the_js_mirror(text, expected):
    """Vectors where BOTH sides must produce the same map."""
    assert parse_kv_lines(text) == expected


@pytest.mark.parametrize(
    "text,match",
    [
        ("not a pair", "name=value"),  # JS silently SKIPS this line
        ("a=1\na=2", "duplicate"),  # JS keeps the last one
    ],
)
def test_parse_kv_lines_is_strict_where_the_js_mirror_is_lenient(text, match):
    """The panel tolerates half-typed text so it can keep rendering; the
    engine refuses it so a typo never silently drops a slot override."""
    with pytest.raises(SelectionError, match=match):
        parse_kv_lines(text)


@pytest.mark.parametrize(
    "token,expected",
    [
        ("sunset", ("fixed", "sunset")),
        ("  sunset  ", ("fixed", "sunset")),  # both mirrors trim
        ("random", ("random", None)),
        ("random@123", ("random", 123)),
        ("🎲 random", ("random", None)),
        ("🎲 random@7", ("random", 7)),
        ("Random", ("fixed", "Random")),  # case sensitive on both sides
        ("randomly", ("fixed", "randomly")),
    ],
)
def test_parse_token_agrees_with_the_js_mirror(token, expected):
    from mrln.promptlib.resolve import _parse_token

    assert _parse_token(token, token) == expected


@pytest.mark.parametrize("token", ["random@abc", "random@"])
def test_parse_token_rejects_a_bad_seed_where_the_js_mirror_shrugs(token):
    """JS parseToken's `random(?:@(\\d+))?$` fails to match, so it classifies
    these as a plain item name; the engine names the problem instead of
    hunting for an item literally called 'random@abc'."""
    from mrln.promptlib.resolve import _parse_token

    with pytest.raises(SelectionError, match="not a valid seed integer"):
        _parse_token(token, token)


def test_parse_token_accepts_a_negative_seed_the_js_mirror_will_not():
    """Divergence in the OTHER direction, pinned so it cannot drift
    unnoticed: python's int() takes '-1', the JS `(\\d+)` group does not — so
    'random@-1' rolls seed -1 in the engine but shows as an item name in the
    panel. Harmless today (a negative seed still derives an rng); if the
    panel ever has to round-trip it, this is the line that says so."""
    from mrln.promptlib.resolve import _parse_token

    assert _parse_token("random@-1", "random@-1") == ("random", -1)


@pytest.mark.parametrize("token", ["off", "🔇 off"])
def test_parse_token_reads_off_as_a_mute_where_the_js_mirror_defers(token):
    """JS parseToken returns 'off' as a fixed item — its callers recognize
    mutes separately. The engine resolves the mute right here."""
    from mrln.promptlib.resolve import _parse_token

    assert _parse_token(token, token) == ("off", None)


# -- fixed / random ---------------------------------------------------------


def test_fixed_default(lib):
    resolved = rt(lib)
    paint = slot(resolved, "paint")
    assert (paint.item_name, paint.text, paint.random) == ("red", "bright red", False)


def test_folder_ref_qualified_default(lib):
    location = slot(rt(lib), "location")
    assert location.item_name == "urban/shibuya"
    assert location.section_slug == "location/urban"
    assert location.label == "Place"


def test_selection_override(lib):
    resolved = rt(lib, selection={"paint": "petrol"})
    assert slot(resolved, "paint").item_name == "petrol"  # user-tier item
    assert slot(resolved, "paint").tier == "user"


def test_ref_qualified_selection_accepted(lib):
    resolved = rt(lib, selection={"location": "location/nature/alpine-pass"})
    assert slot(resolved, "location").item_name == "nature/alpine-pass"


def test_unknown_item_falls_back_to_random(lib):
    # item-level resilience: a stale pick (renamed/removed item) draws
    # randomly with a loud note instead of killing the prompt
    resolved = rt(lib, selection={"paint": "crimson"})
    paint = next(s for s in resolved.slots if s.id == "paint")
    assert paint.item_name is not None and paint.random is True
    assert "'crimson' is not in" in paint.stale_note


def test_unknown_slot_error(lib):
    with pytest.raises(SelectionError, match="unknown slot"):
        rt(lib, selection={"nope": "x"})


def test_random_deterministic(lib):
    one = rt(lib, seed=7)
    two = rt(lib, seed=7)
    assert [s.item_name for s in one.slots] == [s.item_name for s in two.slots]


def test_random_varies_with_seed(lib):
    draws = {slot(rt(lib, seed=s), "lighting").item_name for s in range(20)}
    assert draws == {"daylight", "night"}


def test_per_slot_seed_override(lib):
    pinned = {
        slot(rt(lib, seed=s, selection={"lighting": "random@5"}), "lighting").item_name
        for s in range(10)
    }
    assert len(pinned) == 1  # decoupled from master seed
    echoed = slot(rt(lib, seed=99, selection={"lighting": "random@5"}), "lighting")
    assert echoed.seed_used == 5


def test_twin_slots_draw_independently(lib):
    # paint and extra both ref 'color'; over several seeds their random draws
    # must not always coincide
    differing = 0
    for seed in range(30):
        resolved = rt(lib, seed=seed, mode="randomize all")
        if slot(resolved, "paint").item_name != slot(resolved, "extra").item_name:
            differing += 1
    assert differing > 0


def test_allow_empty_omits(lib):
    # extra slot: empty_weight=100 vs item weights sum ~6 -> mostly omitted
    omitted = sum(1 for seed in range(30) if slot(rt(lib, seed=seed), "extra").item_name is None)
    assert omitted > 15


# -- master modes -----------------------------------------------------------


def test_randomize_all_overrides_fixed(lib):
    draws = {slot(rt(lib, seed=s, mode="randomize all"), "paint").item_name for s in range(30)}
    assert len(draws) > 1  # fixed default 'red' was overridden


def test_all_fixed_defaults_pins_random(lib):
    for seed in range(5):
        resolved = rt(lib, seed=seed, mode="all fixed defaults")
        lighting = slot(resolved, "lighting")
        assert lighting.item_name == "daylight"  # first pool item
        assert lighting.fixed_first
        assert slot(resolved, "paint").item_name == "red"


# -- variables --------------------------------------------------------------


def test_trigger_variable(lib):
    resolved = rt(lib, variables={"trigger": "SkylineGTR"})
    assert resolved.prefix == "photo of a SkylineGTR"


def test_trigger_default_from_template(lib):
    assert rt(lib).prefix == "photo of a sports car"


# -- variants ---------------------------------------------------------------


def test_variant_default(lib):
    resolved = rt(lib, "varianted")
    assert resolved.variant == "studio"
    assert slot(resolved, "backdrop").item_name == "shibuya"
    # order: @variant first, then paint
    assert [s.id for s in resolved.slots] == ["backdrop", "paint"]


def test_variant_selected(lib):
    resolved = rt(lib, "varianted", selection={"variant": "outdoor"})
    assert resolved.variant == "outdoor"
    assert slot(resolved, "backdrop").section_slug.startswith("location/nature")


def test_variant_random_deterministic(lib):
    names = {
        rt(lib, "varianted", seed=s, selection={"variant": "random"}).variant for s in range(20)
    }
    assert names == {"studio", "outdoor"}
    assert (
        rt(lib, "varianted", seed=3, selection={"variant": "random"}).variant
        == rt(lib, "varianted", seed=3, selection={"variant": "random"}).variant
    )


def test_inactive_variant_slot_key_error(lib):
    # 'backdrop' exists in both variants; use a bogus key instead:
    with pytest.raises(SelectionError, match="unknown slot"):
        rt(lib, "varianted", selection={"bogus": "x"})


# -- negatives --------------------------------------------------------------


def test_negative_aggregation_and_dedupe(lib):
    resolved = rt(lib, selection={"paint": "blue", "lighting": "daylight"})
    negative = resolved.negative
    assert negative.startswith("lowres")
    assert "flat lighting" in negative  # section-level negative
    assert negative.count("flat lighting") == 1


# -- section node path ------------------------------------------------------


def test_resolve_section_fixed(lib):
    resolved = resolve_section(lib, "color", "petrol", seed=0)
    assert resolved.item_name == "petrol"
    assert resolved.text == "dark petrol"


def test_resolve_section_random_and_emoji(lib):
    a = resolve_section(lib, "location", "🎲 random", seed=4)
    b = resolve_section(lib, "location", "random", seed=4)
    assert a.item_name == b.item_name


def test_resolve_section_scope_error(lib):
    with pytest.raises(ItemNotFoundError):
        resolve_section(lib, "lighting", "urban/shibuya", seed=0)


def test_resolve_section_allow_empty_draws_nothing_sometimes(lib):
    """The Section node's allow_empty widget ('for optional add-on sections')
    reaches the engine through resolve_section's own Slot, which uses the
    SCHEMA-default empty_weight — a wiring no template test covers. The empty
    outcome must be possible, deterministic per seed, and impossible when the
    widget is off."""
    seeds = range(60)
    empty = [
        s
        for s in seeds
        if resolve_section(lib, "color", "random", seed=s, allow_empty=True).item_name is None
    ]
    assert empty, "allow_empty=True never omitted in 60 seeds"
    # ... and with the widget off the very same seeds always draw an item
    assert all(
        resolve_section(lib, "color", "random", seed=s, allow_empty=False).item_name is not None
        for s in seeds
    )
    # an omitted draw is still a fully-formed, string-typed result
    omitted = resolve_section(lib, "color", "random", seed=empty[0], allow_empty=True)
    assert (omitted.text, omitted.negative) == ("", "")
    assert omitted.random is True
    # deterministic: the same seed omits again
    again = resolve_section(lib, "color", "random", seed=empty[0], allow_empty=True)
    assert again.item_name is None


def test_resolve_section_allow_empty_leaves_fixed_picks_alone(lib):
    """allow_empty weights the RANDOM draw; an explicit pick still wins."""
    resolved = resolve_section(lib, "color", "petrol", seed=0, allow_empty=True)
    assert (resolved.item_name, resolved.text) == ("petrol", "dark petrol")


# -- label expansion ---------------------------------------------------------


def _label_template(paint_label):
    return parse_template(
        {
            "variables": [{"name": "trigger", "default": "HycadeBodykit"}],
            "slots": [
                {"id": "paint", "ref": "color", "default": "red", "label": paint_label},
                {"id": "lighting", "ref": "lighting", "default": "random"},
            ],
        },
        "labeled",
        "mem",
    )


def test_labels_expand_variables(lib):
    tpl = _label_template("Mandatory: ({trigger}:1.1). Body color:")
    resolved = resolve_template(lib, tpl, seed=0, mode="as configured", selection={}, variables={})
    assert slot(resolved, "paint").label == "Mandatory: (HycadeBodykit:1.1). Body color:"
    resolved = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={}, variables={"trigger": "MySubject"}
    )
    assert slot(resolved, "paint").label == "Mandatory: (MySubject:1.1). Body color:"


def test_label_expansion_does_not_disturb_draws(lib):
    plain = _label_template("plain label")
    braced = _label_template("wildcard {a|b} and {trigger}")
    for seed in range(6):
        a = resolve_template(
            lib, plain, seed=seed, mode="as configured", selection={}, variables={}
        )
        b = resolve_template(
            lib, braced, seed=seed, mode="as configured", selection={}, variables={}
        )
        assert [s.item_name for s in a.slots] == [s.item_name for s in b.slots]
    labels = {
        resolve_template(lib, braced, seed=seed, mode="as configured", selection={}, variables={})
        .slots[0]
        .label
        for seed in range(12)
    }
    assert labels <= {"wildcard a and HycadeBodykit", "wildcard b and HycadeBodykit"}
    assert len(labels) == 2  # the @label rng actually varies with the seed


# -- off / mute --------------------------------------------------------------


def test_off_mutes_slot_without_shifting_other_draws(lib):
    for seed in range(6):
        normal = rt(lib, seed=seed)
        muted = rt(lib, seed=seed, selection={"lighting": "off"})
        lighting = slot(muted, "lighting")
        assert lighting.item_name is None and lighting.random is False
        assert lighting.text == "" and lighting.negative == ""
        for sid in ("paint", "location", "extra"):
            assert slot(muted, sid).item_name == slot(normal, sid).item_name


def test_off_survives_randomize_all(lib):
    muted = rt(lib, seed=3, mode="randomize all", selection={"paint": "off"})
    assert slot(muted, "paint").item_name is None


def test_all_fixed_defaults_unmutes(lib):
    pinned = rt(lib, seed=3, mode="all fixed defaults", selection={"paint": "off"})
    assert slot(pinned, "paint").item_name == "red"


def test_variant_off(lib):
    resolved = rt(lib, "varianted", selection={"variant": "off"})
    assert resolved.variant is None and resolved.variant_off is True
    assert [s.id for s in resolved.slots] == ["paint"]  # backdrop slot gone


# -- "off" as a DURABLE default (Apply bakes mutes into the file) ------------


def test_off_default_mutes_slot_across_modes(lib):
    tpl = parse_template(
        {
            "slots": [
                {"id": "paint", "ref": "color", "default": "off"},
                {"id": "mood", "ref": "lighting"},
            ]
        },
        "off-default",
        "test",
    )
    for mode in ("as configured", "randomize all", "all fixed defaults"):
        resolved = resolve_template(lib, tpl, seed=0, mode=mode, selection={}, variables={})
        muted = slot(resolved, "paint")
        assert (muted.item_name, muted.text) == (None, ""), mode  # baked mute holds
        assert slot(resolved, "mood").item_name is not None  # siblings draw
    # an explicit selection un-mutes the baked default
    resolved = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={"paint": "red"}, variables={}
    )
    assert slot(resolved, "paint").item_name == "red"
    resolved = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={"paint": "random"}, variables={}
    )
    assert slot(resolved, "paint").item_name is not None


def test_off_variant_default(lib):
    raw = {
        "slots": [{"id": "paint", "ref": "color", "default": "red"}],
        "variants": [
            {"name": "studio", "slots": [{"id": "backdrop", "ref": "lighting"}]},
            {"name": "outdoor", "slots": [{"id": "sky", "ref": "lighting"}]},
        ],
        "variant_default": "off",
    }
    tpl = parse_template(raw, "off-variant", "test")
    for mode in ("as configured", "randomize all", "all fixed defaults"):
        resolved = resolve_template(lib, tpl, seed=0, mode=mode, selection={}, variables={})
        assert resolved.variant is None and resolved.variant_off is True, mode
    # an explicit selection un-mutes the block
    resolved = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={"variant": "studio"}, variables={}
    )
    assert resolved.variant == "studio"
    # a bogus variant_default still fails parsing — only names/random/off pass
    raw["variant_default"] = "ghost"
    with pytest.raises(Exception, match="not a variant"):
        parse_template(raw, "off-variant", "test")
