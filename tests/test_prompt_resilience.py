"""Library resilience: slug aliases, missing-section skip+warn, and
extend-merge tier semantics — a factory restructure must never kill a
user's saved templates."""

import json

import pytest
import support  # noqa: F401

from mrln import promptapi
from mrln.promptlib import (
    ItemNotFoundError,
    Library,
    SectionNotFoundError,
    TemplateNotFoundError,
    parse_section,
    render,
    resolve_section,
    resolve_template,
)
from mrln.promptlib.serialize import dump_section


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_roots(tmp_path):
    factory = tmp_path / "factory"
    user = tmp_path / "user"
    _write(
        factory,
        "sections/color.json",
        {
            "label": "Color",
            "description": "factory colors",
            "items": [
                {"name": "red", "text": "bright red"},
                {"name": "gold", "text": "shimmering gold", "weight": 3.0},
            ],
        },
    )
    _write(
        factory,
        "sections/location/urban.json",
        {"items": [{"name": "alley", "text": "narrow alley"}]},
    )
    _write(
        factory,
        "sections/location/nature.json",
        {"items": [{"name": "fjord", "text": "misty fjord"}]},
    )
    _write(
        factory,
        "aliases.json",
        {
            "sections": {
                "paint": "color",
                "hue": "paint",
                "scenery": "location",
                "cycle-a": "cycle-b",
                "cycle-b": "cycle-a",
                "dead-end": "nowhere",
            },
            # the TEMPLATE tier of the same table: the shipped aliases.json is
            # empty pre-release, so without a fixture this whole tier — and
            # the separate alias walk in promptapi._raw_file — never runs
            "templates": {"retired": "stale", "ancient": "retired"},
        },
    )
    _write(
        factory,
        "templates/stale.json",
        {
            "slots": [
                {"id": "paint", "ref": "hue", "default": "red"},
                {"id": "scene", "ref": "ghost/gone"},
                {"id": "place", "ref": "scenery"},
            ],
        },
    )
    return factory, user


@pytest.fixture()
def lib(tmp_path):
    return Library(*build_roots(tmp_path))


def rt(lib, slug="stale", seed=0, selection=None, mode="as configured"):
    tpl = lib.load_template(slug)
    return resolve_template(lib, tpl, seed=seed, mode=mode, selection=selection or {}, variables={})


# -- aliases -----------------------------------------------------------------


def test_alias_resolves_renamed_leaf(lib):
    assert lib.load_section("paint").slug == "color"
    assert [q for q, _, _ in lib.scope_items("paint")] == ["red", "gold"]


def test_alias_chain_and_folder_target(lib):
    assert lib.load_section("hue").slug == "color"  # hue -> paint -> color
    assert {q for q, _, _ in lib.scope_items("scenery")} == {"urban/alley", "nature/fjord"}


def test_alias_cycle_and_dead_end_stay_not_found(lib):
    with pytest.raises(SectionNotFoundError):
        lib.load_section("cycle-a")
    with pytest.raises(SectionNotFoundError):
        lib.scope_items("dead-end")


def test_user_alias_overrides_factory(lib, tmp_path):
    _write(tmp_path / "user", "aliases.json", {"sections": {"paint": "location/urban"}})
    assert lib.load_section("paint").slug == "location/urban"


def test_malformed_alias_file_is_ignored(tmp_path):
    factory, user = build_roots(tmp_path)
    (factory / "aliases.json").write_text("{not json", encoding="utf-8")
    lib = Library(factory, user)
    with pytest.raises(SectionNotFoundError):
        lib.load_section("paint")
    assert lib.load_section("color").slug == "color"  # direct loads unaffected


def test_aliased_slug_lands_on_merged_view(lib, tmp_path):
    _write(
        tmp_path / "user",
        "sections/color.json",
        {"items": [{"name": "petrol", "text": "dark petrol"}]},
    )
    merged = lib.load_section("paint")  # alias -> color -> factory+user merge
    assert merged.merged
    assert [item.name for item in merged.items] == ["red", "gold", "petrol"]


def test_template_alias_resolves_through_load_and_api(lib):
    """The pack's release promise ('a released slug never just dies') covers
    TEMPLATES too — the tier a saved workflow points at. Every reader must
    follow it: the engine's loader, the Composer's detail endpoint, and the
    raw-file reader behind the raw editor (which walks aliases itself)."""
    assert lib.load_template("retired").slug == "stale"
    assert lib.load_template("ancient").slug == "stale"  # chains, like sections do
    # the raw editor: promptapi._raw_file re-implements the alias walk
    assert promptapi._raw_file(lib, "templates", "retired") == promptapi._raw_file(
        lib, "templates", "stale"
    )
    assert promptapi._raw_file(lib, "templates", "ancient") == promptapi._raw_file(
        lib, "templates", "stale"
    )
    with pytest.raises(TemplateNotFoundError):
        promptapi._raw_file(lib, "templates", "never-existed")
    # a stored workflow opening the retired slug gets the LIVE template
    status, body = promptapi.handle_template(lib, {"slug": "retired"})
    assert status == 200
    live = promptapi.handle_template(lib, {"slug": "stale"})[1]
    assert [s["id"] for s in body["template"]["slots"]] == [
        s["id"] for s in live["template"]["slots"]
    ]
    assert body["missing_refs"] == live["missing_refs"]
    # and it renders — the full path a workflow takes after a factory rename
    status, preview = promptapi.handle_preview(lib, {"template": "retired"})
    assert status == 200 and preview["choices"]


def test_template_alias_cycle_stays_not_found(lib, tmp_path):
    _write(tmp_path / "user", "aliases.json", {"templates": {"ping": "pong", "pong": "ping"}})
    with pytest.raises(Exception, match="not found"):
        lib.load_template("ping")


# -- missing sections: skip + warn -------------------------------------------


def test_missing_ref_resolves_as_skipped_slot(lib):
    resolved = rt(lib)
    by_id = {s.id: s for s in resolved.slots}
    assert by_id["scene"].missing and by_id["scene"].item_name is None
    assert by_id["scene"].text == ""
    assert by_id["paint"].item_name == "red"  # healthy slots unaffected
    assert by_id["place"].item_name  # aliased folder ref drew normally


def test_missing_ref_render_skips_and_warns(lib):
    tpl = lib.load_template("stale")
    out = render(rt(lib), "string", tpl.render)
    assert "ghost" not in out.positive
    assert "⚠ scene: section 'ghost/gone' is missing" in out.choices
    assert "remap" in out.choices


def test_missing_ref_survives_all_modes_and_selection(lib):
    for mode in ("as configured", "randomize all", "all fixed defaults"):
        assert {s.id: s for s in rt(lib, mode=mode).slots}["scene"].missing
    # an explicit pick on a dead slot degrades to missing, not a crash
    resolved = rt(lib, selection={"scene": "anything"})
    assert {s.id: s for s in resolved.slots}["scene"].missing


def test_missing_child_slot_substitutes_empty(lib, tmp_path):
    _write(
        tmp_path / "factory",
        "sections/crew.json",
        {
            "items": [
                {
                    "name": "pair",
                    "text": "left {left} right",
                    "slots": [{"id": "left", "ref": "ghost/gone"}],
                }
            ]
        },
    )
    _write(
        tmp_path / "factory",
        "templates/nested-stale.json",
        {"slots": [{"id": "crew", "ref": "crew", "default": "pair"}]},
    )
    resolved = rt(lib, slug="nested-stale")
    crew = resolved.slots[0]
    assert crew.children[0].missing
    assert crew.text == "left  right"


def test_section_node_still_errors_hard(lib):
    with pytest.raises(SectionNotFoundError):
        resolve_section(lib, "ghost/gone", "random", seed=0)


def test_api_template_detail_survives_missing_ref(lib):
    status, body = promptapi.handle_template(lib, {"slug": "stale"})
    assert status == 200
    assert body["missing_refs"] == ["ghost/gone"]
    slots = {s["id"]: s for s in body["template"]["slots"]}
    assert slots["scene"]["missing"] and not slots["paint"]["missing"]
    assert "hue" in body["pools"] and "ghost/gone" not in body["pools"]


def test_api_preview_flags_missing_slot(lib):
    status, body = promptapi.handle_preview(lib, {"template": "stale"})
    assert status == 200
    slots = {s["id"]: s for s in body["slots"]}
    assert slots["scene"]["missing"] and not slots["paint"]["missing"]
    assert "⚠ scene" in body["choices"]


# -- extend-merge tiers ------------------------------------------------------


def user_color(tmp_path, items, **fields):
    return _write(tmp_path / "user", "sections/color.json", {**fields, "items": items})


def test_extend_merges_items_with_provenance(lib, tmp_path):
    user_color(
        tmp_path,
        [
            {"name": "gold", "text": "warm gold", "weight": 2.0},  # override by name
            {"name": "petrol", "text": "dark petrol"},  # new, appended
        ],
    )
    merged = lib.load_section("color")
    assert merged.merged and not merged.replaces
    assert [(i.name, i.origin) for i in merged.items] == [
        ("red", "factory"),
        ("gold", "user"),
        ("petrol", "user"),
    ]
    gold = merged.items[1]
    assert gold.text == "warm gold" and gold.weight == 2.0
    assert merged.description == "factory colors"  # empty user fields inherit


def test_extend_field_overrides_and_label_rules(lib, tmp_path):
    user_color(tmp_path, [], label="My Colors", suits=["car"])
    merged = lib.load_section("color")
    assert merged.label == "My Colors" and merged.suits == ("car",)
    # an auto-derived user label must not beat an explicit factory label
    user_color(tmp_path, [])
    assert lib.load_section("color").label == "Color"


def test_tombstone_hides_factory_item_from_pools(lib, tmp_path):
    user_color(tmp_path, [{"name": "gold", "hidden": True}])
    merged = lib.load_section("color")
    gold = next(i for i in merged.items if i.name == "gold")
    assert gold.hidden and gold.text == "shimmering gold"  # content stays visible to editors
    assert [q for q, _, _ in lib.scope_items("color")] == ["red"]
    with pytest.raises(ItemNotFoundError):
        resolve_section(lib, "color", "gold", seed=0)


def _pinned_lib(tmp_path, tombstone):
    """Factory template pinning 'gold', optionally tombstoned in the user
    tier. Built before the first read so no scan memo is in play."""
    factory, user = tmp_path / "factory", tmp_path / "user"
    _write(
        factory,
        "templates/pinned.json",
        {"slots": [{"id": "paint", "ref": "color", "default": "gold"}]},
    )
    if tombstone:
        _write(user, "sections/color.json", {"items": [{"name": "gold", "hidden": True}]})
    return Library(factory, user)


def test_pinned_item_is_honored_while_it_is_visible(tmp_path):
    build_roots(tmp_path)
    paint = {s.id: s for s in rt(_pinned_lib(tmp_path, False), slug="pinned").slots}["paint"]
    assert (paint.item_name, paint.random, paint.stale_note) == ("gold", False, "")


def test_tombstoned_item_pinned_by_a_template_degrades_to_random(tmp_path):
    """Hiding an item and PINNING it are two features that meet here: a
    template default (or a workflow selection) naming a tombstoned item can
    no longer find it, so the slot draws randomly and says so. Freezing the
    semantic matters because fixed picks deliberately bypass tag filters —
    a refactor letting them bypass the hidden filter too would silently
    resurrect every item a user tombstoned."""
    build_roots(tmp_path)
    lib = _pinned_lib(tmp_path, True)
    degraded = {s.id: s for s in rt(lib, slug="pinned").slots}["paint"]
    assert degraded.item_name == "red"  # the only item left in the pool
    assert degraded.random is True
    assert "gold" in degraded.stale_note
    # the choices report has to SHOW it — a silent swap is the failure mode
    tpl = lib.load_template("pinned")
    assert "⚠ paint:" in render(rt(lib, slug="pinned"), "string", tpl.render).choices
    # a durable workflow selection degrades identically ...
    chosen = {s.id: s for s in rt(lib, slug="pinned", selection={"paint": "gold"}).slots}["paint"]
    assert chosen.item_name == "red" and chosen.random is True and "gold" in chosen.stale_note
    # ... while the standalone Section node stays hard-strict about it
    with pytest.raises(ItemNotFoundError):
        resolve_section(lib, "color", "gold", seed=0)


def test_replaces_flag_shadows_factory(lib, tmp_path):
    user_color(tmp_path, [{"name": "petrol", "text": "dark petrol"}], replaces=True)
    replaced = lib.load_section("color")
    assert not replaced.merged and replaced.replaces
    assert [item.name for item in replaced.items] == ["petrol"]


def test_resolved_tier_is_per_item(lib, tmp_path):
    user_color(tmp_path, [{"name": "petrol", "text": "dark petrol"}])
    tpl = lib.load_template("stale")
    factory_pick = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={"paint": "red"}, variables={}
    )
    user_pick = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection={"paint": "petrol"}, variables={}
    )
    assert {s.id: s for s in factory_pick.slots}["paint"].tier == "factory"
    assert {s.id: s for s in user_pick.slots}["paint"].tier == "user"
    assert "petrol  [fixed]  (user)" in render(user_pick, "string", tpl.render).choices


def test_api_section_exposes_merge_provenance(lib, tmp_path):
    user_color(tmp_path, [{"name": "petrol", "text": "dark petrol"}])
    status, body = promptapi.handle_section(lib, {"slug": "color"})
    assert status == 200
    assert body["merged"] and not body["replaces"]
    origins = {i["name"]: i["origin"] for i in body["items"]}
    assert origins == {"red": "factory", "gold": "factory", "petrol": "user"}
    assert body["factory_raw"]["label"] == "Color"
    assert body["raw"]["items"][0]["name"] == "petrol"  # raw = the user file


def test_merged_section_roundtrips(lib, tmp_path):
    user_color(tmp_path, [{"name": "gold", "hidden": True}, {"name": "new", "text": "x"}])
    merged = lib.load_section("color")
    dumped = dump_section(merged)
    assert next(i for i in dumped["items"] if i["name"] == "gold")["hidden"] is True
    assert "origin" not in json.dumps(dumped)  # runtime provenance never serialized
    assert parse_section(dumped, "color", "mem") == merged  # compare=False runtime fields


# -- item renames: heal, never break ------------------------------------------


def _rename_lib(tmp_path):
    factory = tmp_path / "f"
    user = tmp_path / "u"
    _write(
        factory,
        "sections/color.json",
        {"items": [{"name": "red", "text": "bright red"}, {"name": "blue", "text": "ocean blue"}]},
    )
    _write(
        factory,
        "sections/location/urban.json",
        {"items": [{"name": "shibuya", "text": "Shibuya Crossing"}]},
    )
    _write(
        user,
        "templates/mine.json",
        {
            "slots": [
                {"id": "paint", "ref": "color", "default": "redd"},
                {"id": "place", "ref": "location", "default": "urban/shibuya"},
            ]
        },
    )
    return Library(factory, user)


def test_stale_item_pick_falls_back_to_seeded_random(tmp_path):
    lib = _rename_lib(tmp_path)
    tpl = lib.load_template("mine")
    resolved = resolve_template(lib, tpl, seed=7, mode="as configured", selection={}, variables={})
    paint = next(s for s in resolved.slots if s.id == "paint")
    assert paint.item_name in ("red", "blue")  # drew instead of dying
    assert paint.random is True
    assert "'redd' is not in 'color'" in paint.stale_note
    assert "did you mean 'red'" in paint.stale_note
    out = render(resolved, "string", tpl.render)
    assert "⚠ paint:" in out.choices
    again = resolve_template(lib, tpl, seed=7, mode="as configured", selection={}, variables={})
    assert next(s for s in again.slots if s.id == "paint").item_name == paint.item_name


def test_section_node_stays_strict_on_stale_item(tmp_path):
    lib = _rename_lib(tmp_path)
    with pytest.raises(ItemNotFoundError):
        resolve_section(lib, "color", "redd", seed=0)


def test_save_section_renames_repoint_user_templates(tmp_path):
    lib = _rename_lib(tmp_path)
    status, body = promptapi.handle_save_section(
        lib,
        {
            "slug": "color",
            "data": {"items": [{"name": "crimson", "text": "bright red"}]},
            "renames": {"redd": "crimson"},
        },
    )
    assert status == 200, body
    assert body["templates_rewritten"] == 1
    assert lib.load_template("mine").slots[0].default == "crimson"
    # folder-scoped defaults rewrite their qualified tail
    status, body = promptapi.handle_save_section(
        lib,
        {
            "slug": "location/urban",
            "data": {"items": [{"name": "shinjuku", "text": "Shinjuku"}]},
            "renames": {"shibuya": "shinjuku"},
        },
    )
    assert status == 200 and body["templates_rewritten"] == 1
    assert lib.load_template("mine").slots[1].default == "urban/shinjuku"
    # no-op renames report zero rewrites
    status, body = promptapi.handle_save_section(
        lib,
        {"slug": "color", "data": {"items": [{"name": "x", "text": "y"}]}, "renames": {"a": "a"}},
    )
    assert status == 200 and body["templates_rewritten"] == 0


def test_rename_rewrites_fully_qualified_defaults(tmp_path):
    """A folder-scoped slot may spell its default either way — scope-relative
    ('urban/shibuya') or fully qualified with the ref ('location/urban/
    shibuya'); the Composer writes the first, hand-authored templates the
    second. The rename heal has to cover BOTH prefixes or a hand-authored
    default is quietly left pointing at a name that no longer exists."""
    factory = tmp_path / "f"
    user = tmp_path / "u"
    _write(
        factory,
        "sections/location/urban.json",
        {"items": [{"name": "shibuya", "text": "Shibuya Crossing"}]},
    )
    _write(
        user,
        "templates/mine.json",
        {
            "slots": [
                {"id": "rel", "ref": "location", "default": "urban/shibuya"},
                {"id": "qualified", "ref": "location", "default": "location/urban/shibuya"},
                {"id": "leaf", "ref": "location/urban", "default": "shibuya"},
                # a look-alike that must NOT be touched: same tail, other scope
                {"id": "untouched", "ref": "location", "default": "urban/shibuya-station"},
            ]
        },
    )
    lib = Library(factory, user)
    status, body = promptapi.handle_save_section(
        lib,
        {
            "slug": "location/urban",
            "data": {"items": [{"name": "shinjuku", "text": "Shinjuku"}]},
            "renames": {"shibuya": "shinjuku"},
        },
    )
    assert status == 200 and body["templates_rewritten"] == 1
    defaults = {slot.id: slot.default for slot in lib.load_template("mine").slots}
    assert defaults == {
        "rel": "urban/shinjuku",
        "qualified": "location/urban/shinjuku",
        "leaf": "shinjuku",
        "untouched": "urban/shibuya-station",
    }


# -- a factory section that GROWS --------------------------------------------
# The question this answers: "will a future factory update break my templates?"
# Three separate contracts, and only the first one moves.


def _grow_lib(tmp_path):
    """A template with one random slot and one pinned slot on the same
    section, so both halves of the contract are observable at once."""
    factory = tmp_path / "factory"
    user = tmp_path / "user"
    _write(
        factory,
        "sections/color.json",
        {
            "label": "Color",
            "items": [{"name": "red", "text": "red"}, {"name": "blue", "text": "blue"}],
        },
    )
    _write(
        user,
        "templates/mine.json",
        {
            "label": "Mine",
            "text": "{roll} and {pinned}",
            "slots": [
                {"id": "roll", "ref": "color"},
                {"id": "pinned", "ref": "color", "default": "blue"},
            ],
        },
    )
    return Library(factory, user)


def _draw(lib, seed=11):
    tpl = lib.load_template("mine")
    resolved = resolve_template(
        lib, tpl, seed=seed, mode="as configured", selection={}, variables={}
    )
    return {s.id: s.item_name for s in resolved.slots}


def test_factory_growth_shifts_random_draws_but_never_pinned_ones(tmp_path):
    lib = _grow_lib(tmp_path)
    before = _draw(lib)
    assert before["pinned"] == "blue"

    # ship an update: the factory section gains items
    _write(
        tmp_path / "factory",
        "sections/color.json",
        {
            "label": "Color",
            "items": [
                {"name": "red", "text": "red"},
                {"name": "blue", "text": "blue"},
                {"name": "green", "text": "green"},
                {"name": "amber", "text": "amber"},
            ],
        },
    )
    lib = Library(tmp_path / "factory", tmp_path / "user")
    after = _draw(lib)

    # A random slot draws by weighted INDEX over the live pool, so a bigger
    # pool is a different draw for the same seed. That is the trade-off of
    # random-over-a-living-library and it is deliberate: the alternative is a
    # library that can never gain content. Pinning is the escape hatch.
    assert after["roll"] in {"red", "blue", "green", "amber"}
    # ...and the pinned slot does NOT move. This is the contract that matters:
    # anything a user chose on purpose survives every factory update.
    assert after["pinned"] == "blue"
    # stable in itself: re-resolving the updated library repeats exactly
    assert _draw(lib) == after


def test_factory_growth_leaves_no_warning_on_a_still_valid_pin(tmp_path):
    lib = _grow_lib(tmp_path)
    _write(
        tmp_path / "factory",
        "sections/color.json",
        {
            "label": "Color",
            "items": [{"name": "blue", "text": "blue"}, {"name": "teal", "text": "teal"}],
        },
    )
    lib = Library(tmp_path / "factory", tmp_path / "user")
    tpl = lib.load_template("mine")
    resolved = resolve_template(lib, tpl, seed=3, mode="as configured", selection={}, variables={})
    pinned = next(s for s in resolved.slots if s.id == "pinned")
    # 'red' disappeared but 'blue' survived: the pin still resolves, silently.
    assert pinned.item_name == "blue"
    assert pinned.stale_note == ""
    assert "⚠" not in render(resolved, "string", tpl.render).choices
