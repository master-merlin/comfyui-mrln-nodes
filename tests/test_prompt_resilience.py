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
            }
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
