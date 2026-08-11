"""Audit hardening round: empty-pool pinning, malformed user data (LoRA
strengths, profiles.json shapes), alias-aware section merging, dump_template
profiles, Section-node child slots, and the untested combinations inline
weaving x variants and profile overrides x 'off' defaults."""

import json
import logging

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi
from mrln.promptlib import (
    ItemNotFoundError,
    Library,
    RenderConfig,
    ResolvedPrompt,
    ResolvedSlot,
    SchemaError,
    SelectionError,
    compose,
    dump_template,
    lora_entries,
    parse_section,
    parse_template,
    render,
    resolve_section,
    resolve_template,
)

MODES = ("as configured", "randomize all", "all fixed defaults")


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _slot(resolved, slot_id):
    return next(s for s in resolved.slots if s.id == slot_id)


# -- 1: 'all fixed defaults' on an empty draw pool ---------------------------


def test_all_fixed_defaults_on_empty_pool_raises_selection_error(tmp_path):
    lib = build_library(tmp_path)
    _write(tmp_path / "factory", "sections/void.json", {"items": []})  # empty is legal
    tpl = parse_template(
        {"slots": [{"id": "gap", "ref": "void", "default": "random"}]}, "empty-pin", "test"
    )
    with pytest.raises(SelectionError, match="no items in 'void'"):
        resolve_template(lib, tpl, seed=0, mode="all fixed defaults", selection={}, variables={})


# -- 2: malformed data.lora strengths ----------------------------------------


@pytest.mark.parametrize("key", ["strength_model", "strength_clip"])
def test_bad_lora_strength_fails_at_parse(key):
    raw = {"items": [{"name": "kit", "text": "K", "data": {"lora": "x.safetensors", key: "high"}}]}
    with pytest.raises(SchemaError, match=f"'{key}' must be a number"):
        parse_section(raw, "lora/bad", "test")


def test_numeric_lora_strengths_still_parse():
    raw = {
        "items": [
            {"name": "kit", "text": "K", "data": {"lora": "x", "strength_model": 0.5}},
            {"name": "plain", "text": "P", "data": {"note": "no lora — free-form data"}},
        ]
    }
    section = parse_section(raw, "lora/ok", "test")
    assert section.items[0].data["strength_model"] == 0.5


def test_stale_bad_lora_strength_degrades_with_choices_warning():
    # a bad file predating parse-time validation must not kill compose:
    # entry skipped, ⚠ in choices, prompt text untouched
    bad = ResolvedSlot(
        id="kit",
        key="kit",
        label="Kit",
        ref="lora/kits",
        section_slug="lora/kits",
        item_name="kit",
        text="TRIG",
        negative="",
        random=False,
        fixed_first=False,
        emphasis=None,
        data={"lora": "x.safetensors", "strength_model": "high"},
        tier="user",
        seed_used=0,
    )
    resolved = ResolvedPrompt(
        template_slug="t",
        seed=0,
        mode="as configured",
        variant=None,
        variant_random=False,
        prefix="",
        suffix="",
        slots=(bad,),
        negative="",
    )
    assert lora_entries(resolved) == []
    out = render(resolved, "string", RenderConfig(lora_tags=True))
    assert out.positive == "TRIG"
    assert "non-numeric strength" in out.choices


# -- 3: pack_profiles malformed shapes ---------------------------------------


def test_pack_profiles_skips_malformed_shapes(tmp_path, caplog):
    factory = tmp_path / "factory"
    user = tmp_path / "user"
    _write(factory, "profiles.json", {"profiles": {"good": {"llm": {"system": "S"}}}})
    lib = Library(factory, user)
    for bad in ([1, 2], {"profiles": [1]}, {"profiles": {"broken": "x", "standard": {}}}):
        _write(user, "profiles.json", bad)
        lib.invalidate()
        with caplog.at_level(logging.WARNING):
            pack = lib.pack_profiles()
        assert list(pack) == ["good"], bad  # the factory tier survives every shape
        assert "ignoring" in caplog.text


# -- 4: compose survives malformed pack-profile blocks -----------------------


def test_compose_ignores_non_dict_profile_blocks(tmp_path, caplog):
    lib = build_library(tmp_path)
    _write(
        tmp_path / "user",
        "profiles.json",
        {
            "profiles": {
                "weird": {
                    "render": "my profile x",
                    "llm": "gpt",
                    "overrides": "bogus",
                    "json_template": 5,
                },
                "halfway": {"overrides": {"slots": "nope", "prefix": "P:"}},
            }
        },
    )
    tpl = lib.load_template("basic")
    with caplog.at_level(logging.WARNING):
        composed = compose(
            lib, tpl, seed=0, mode="as configured", selection={}, variables={}, profile="weird"
        )
    standard = compose(lib, tpl, seed=0, mode="as configured", selection={}, variables={})
    assert composed.rendered.positive == standard.rendered.positive  # blocks all ignored
    assert json.loads(composed.llm)["target"] == "weird"
    assert "ignoring non-object" in caplog.text
    halfway = compose(
        lib, tpl, seed=0, mode="as configured", selection={}, variables={}, profile="halfway"
    )
    assert halfway.rendered.positive.startswith("P:")  # good keys apply, bad 'slots' skipped


def test_pack_profile_bad_emphasis_names_the_profile(tmp_path):
    lib = build_library(tmp_path)
    _write(
        tmp_path / "user",
        "profiles.json",
        {"profiles": {"bademph": {"overrides": {"slots": {"paint": {"emphasis": "big"}}}}}},
    )
    tpl = lib.load_template("basic")
    with pytest.raises(SelectionError, match="profile=bademph"):
        compose(
            lib, tpl, seed=0, mode="as configured", selection={}, variables={}, profile="bademph"
        )


# -- 5: Section node resolves item child slots -------------------------------


def test_resolve_section_resolves_item_child_slots(tmp_path):
    lib = build_library(tmp_path)
    resolved = resolve_section(lib, "crew", "pair", seed=0)
    assert "{left}" not in resolved.text and "{right}" not in resolved.text
    assert "and bright red paint" in resolved.text  # fixed child default 'red'
    assert {c.id for c in resolved.children} == {"section.left", "section.right"}
    assert resolve_section(lib, "crew", "pair", seed=0).text == resolved.text  # deterministic
    texts = {resolve_section(lib, "crew", "pair", seed=s).text for s in range(12)}
    assert len(texts) > 1  # the random 'left' child actually draws


def test_resolve_section_stays_strict_for_the_own_pick(tmp_path):
    lib = build_library(tmp_path)
    with pytest.raises(ItemNotFoundError):
        resolve_section(lib, "crew", "ghost", seed=0)


def test_resolve_section_aggregates_child_negatives(tmp_path):
    factory = tmp_path / "f"
    _write(
        factory,
        "sections/tone.json",
        {"items": [{"name": "moody", "text": "moody teal", "negative": "washed out"}]},
    )
    _write(
        factory,
        "sections/duo.json",
        {
            "items": [
                {
                    "name": "rig",
                    "text": "{a} rig",
                    "negative": "blurry",
                    "slots": [{"id": "a", "ref": "tone", "default": "moody"}],
                }
            ]
        },
    )
    resolved = resolve_section(Library(factory, None), "duo", "rig", seed=0)
    assert resolved.text == "moody teal rig"
    assert resolved.negative == "blurry, washed out"


# -- 6: user extend-section survives a factory rename ------------------------


def test_user_extend_survives_factory_rename(tmp_path):
    factory = tmp_path / "factory"
    user = tmp_path / "user"
    _write(factory, "sections/environment.json", {"items": [{"name": "dock", "text": "dry dock"}]})
    _write(factory, "aliases.json", {"sections": {"scene": "environment"}})
    _write(user, "sections/scene.json", {"items": [{"name": "pier", "text": "wet pier"}]})
    merged = Library(factory, user).load_section("scene")
    assert merged.merged is True
    assert [i.name for i in merged.items] == ["dock", "pier"]  # factory baseline kept
    assert {i.name: i.origin for i in merged.items} == {"dock": "factory", "pier": "user"}


# -- 7: dump_template keeps profiles -----------------------------------------


def test_dump_template_keeps_profiles():
    raw = {
        "slots": [{"id": "paint", "ref": "color", "default": "red"}],
        "profiles": {"sdxl": {"render": {"format": "string"}, "overrides": {"prefix": "S:"}}},
    }
    tpl = parse_template(raw, "profiled", "test")
    dumped = dump_template(tpl)
    assert dumped["profiles"] == raw["profiles"]
    assert parse_template(dumped, "profiled", "test") == tpl  # round-trip restored
    dumped["profiles"]["sdxl"]["render"]["format"] = "json"  # deepcopy: no shared state
    assert tpl.profiles["sdxl"]["render"]["format"] == "string"


# -- 8: inline weaving x variants --------------------------------------------


@pytest.fixture()
def weave_lib(tmp_path):
    factory = tmp_path / "wfactory"
    _write(factory, "sections/color.json", {"items": [{"name": "red", "text": "bright red"}]})
    _write(factory, "sections/city.json", {"items": [{"name": "tokyo", "text": "Tokyo streets"}]})
    _write(factory, "sections/wild.json", {"items": [{"name": "dunes", "text": "rolling dunes"}]})
    _write(
        factory,
        "templates/weave.json",
        {
            "prefix": "shot of {backdrop} with {sky}",
            "slots": [{"id": "paint", "ref": "color", "default": "red"}],
            "variants": [
                {
                    "name": "studio",
                    "slots": [{"id": "backdrop", "ref": "city", "default": "tokyo"}],
                },
                {
                    "name": "outdoor",
                    "slots": [
                        {"id": "backdrop", "ref": "wild", "default": "dunes"},
                        {"id": "sky", "ref": "wild", "default": "dunes"},
                    ],
                },
            ],
            "variant_default": "studio",
        },
    )
    return Library(factory, None)


def wv(lib, selection=None):
    tpl = lib.load_template("weave")
    return resolve_template(
        lib, tpl, seed=0, mode="as configured", selection=selection or {}, variables={}
    )


def test_active_variant_slot_weaves_inline(weave_lib):
    resolved = wv(weave_lib)
    assert resolved.prefix == "shot of Tokyo streets with"  # 'sky' is outdoor-only -> ""
    assert _slot(resolved, "backdrop").inline is True


def test_variant_off_weaves_empty_instead_of_raising(weave_lib):
    resolved = wv(weave_lib, {"variant": "off"})
    assert resolved.variant is None and resolved.variant_off is True
    assert resolved.prefix == "shot of with"  # both woven ids mute to "" + tidy seams


def test_other_variant_weaves_its_own_slots(weave_lib):
    resolved = wv(weave_lib, {"variant": "outdoor"})
    assert resolved.prefix == "shot of rolling dunes with rolling dunes"


# -- 9: profile overrides x 'off' defaults -----------------------------------


def _profile_mute_template():
    return {
        "slots": [
            {"id": "paint", "ref": "color", "default": "red"},
            {"id": "mood", "ref": "lighting", "default": "random"},
        ],
        "profiles": {"quiet": {"overrides": {"slots": {"paint": {"default": "off"}}}}},
    }


def test_profile_override_mutes_slot_in_all_modes(tmp_path):
    lib = build_library(tmp_path)
    tpl = parse_template(_profile_mute_template(), "prof-mute", "test")
    for mode in MODES:
        composed = compose(lib, tpl, seed=0, mode=mode, selection={}, variables={}, profile="quiet")
        paint = _slot(composed.resolved, "paint")
        assert (paint.item_name, paint.text) == (None, ""), mode  # profile mute holds
        assert _slot(composed.resolved, "mood").item_name is not None
    standard = compose(lib, tpl, seed=0, mode="as configured", selection={}, variables={})
    assert _slot(standard.resolved, "paint").item_name == "red"  # 'standard' is the way back


def test_profile_override_unmutes_baked_off_default(tmp_path):
    lib = build_library(tmp_path)
    tpl = parse_template(
        {
            "slots": [{"id": "paint", "ref": "color", "default": "off"}],
            "profiles": {"loud": {"overrides": {"slots": {"paint": {"default": "green"}}}}},
        },
        "prof-unmute",
        "test",
    )
    for mode in MODES:
        composed = compose(lib, tpl, seed=0, mode=mode, selection={}, variables={}, profile="loud")
        paint = _slot(composed.resolved, "paint")
        assert paint.item_name is not None, mode  # override un-mutes per target model
        if mode != "randomize all":  # that mode randomizes the un-muted pick
            assert paint.item_name == "green"
        standard = compose(lib, tpl, seed=0, mode=mode, selection={}, variables={})
        assert _slot(standard.resolved, "paint").item_name is None, mode  # base mute holds


def test_profile_mute_round_trips_through_save_and_preview(tmp_path):
    lib = build_library(tmp_path)
    status, _ = promptapi.handle_save_template(
        lib, {"slug": "prof-mute", "data": _profile_mute_template()}
    )
    assert status == 200
    status, body = promptapi.handle_preview(lib, {"template": "prof-mute", "profile": "quiet"})
    assert status == 200, body
    flags = {s["id"]: s["omitted"] for s in body["slots"]}
    assert flags == {"paint": True, "mood": False}
    status, body = promptapi.handle_preview(lib, {"template": "prof-mute"})
    assert status == 200
    assert next(s for s in body["slots"] if s["id"] == "paint")["omitted"] is False
