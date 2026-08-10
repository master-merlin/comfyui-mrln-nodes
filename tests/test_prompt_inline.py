"""Inline slot weaving: prefix/suffix may reference top-level slot ids as
{placeholders} — the drawn text renders inside that sentence (with context)
and the slot leaves the joined body. This is how a LoRA catchword becomes an
in-context trigger word instead of a bare fragment."""

import json

import pytest
import support  # noqa: F401

from mrln import promptapi
from mrln.promptlib import Library, lora_entries, render, resolve_template


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def lib(tmp_path):
    factory = tmp_path / "factory"
    _write(
        factory,
        "sections/car.json",
        {
            "items": [
                {"name": "gt3", "text": "silver GT3 coupe"},
                {"name": "m4", "text": "frozen-blue M4"},
            ]
        },
    )
    _write(factory, "sections/scene.json", {"items": [{"name": "dusk", "text": "dusk pit lane"}]})
    _write(
        factory,
        "sections/lora/car.json",
        {
            "items": [
                {
                    "name": "m4-kit",
                    "text": "BMWM4CS_G82",
                    "data": {"lora": "mastermerlin\\bmw_m4_cs.safetensors", "strength_model": 0.35},
                }
            ]
        },
    )
    _write(
        factory,
        "templates/inline-basic.json",
        {
            "prefix": "cinematic photo of a {car} rolling through",
            "slots": [
                {"id": "car", "ref": "car", "default": "gt3"},
                {"id": "scene", "ref": "scene", "default": "dusk"},
            ],
        },
    )
    _write(
        factory,
        "templates/inline-emphasis.json",
        {
            "prefix": "hero shot of a {car}",
            "slots": [
                {"id": "car", "ref": "car", "default": "gt3", "emphasis": 1.2},
                {"id": "scene", "ref": "scene", "default": "dusk"},
            ],
        },
    )
    _write(
        factory,
        "templates/inline-suffix.json",
        {
            "suffix": "captured beside a {car}",
            "slots": [
                {"id": "car", "ref": "car", "default": "gt3"},
                {"id": "scene", "ref": "scene", "default": "dusk"},
            ],
        },
    )
    _write(
        factory,
        "templates/inline-lora.json",
        {
            "prefix": "a pristine {kit} on track",
            "slots": [
                {"id": "kit", "ref": "lora/car", "default": "m4-kit"},
                {"id": "scene", "ref": "scene", "default": "dusk"},
            ],
        },
    )
    _write(
        factory,
        "templates/inline-missing.json",
        {
            "prefix": "a {ghost} scene",
            "slots": [
                {"id": "ghost", "ref": "gone/away"},
                {"id": "scene", "ref": "scene", "default": "dusk"},
            ],
        },
    )
    _write(
        factory,
        "templates/trigger-slot.json",
        {
            "prefix": "X {trigger} Y",
            "slots": [{"id": "trigger", "ref": "car", "default": "gt3"}],
        },
    )
    _write(
        factory,
        "templates/var-collision.json",
        {
            "prefix": "build: {car}",
            "variables": [{"name": "car", "default": "VARCAR"}],
            "slots": [{"id": "car", "ref": "car", "default": "gt3"}],
        },
    )
    _write(
        factory,
        "templates/inline-alt.json",
        {
            "prefix": "{with a {car}|clean}",
            "slots": [
                {"id": "car", "ref": "car", "default": "gt3"},
                {"id": "scene", "ref": "scene", "default": "dusk"},
            ],
        },
    )
    return Library(factory, None)


def rt(lib, slug, selection=None, variables=None):
    tpl = lib.load_template(slug)
    resolved = resolve_template(
        lib,
        tpl,
        seed=0,
        mode="as configured",
        selection=selection or {},
        variables=variables or {},
    )
    return tpl, resolved


def test_prefix_weaves_and_slot_leaves_body(lib):
    tpl, resolved = rt(lib, "inline-basic")
    out = render(resolved, "string", tpl.render)
    assert out.positive == "cinematic photo of a silver GT3 coupe rolling through, dusk pit lane"
    assert out.positive.count("silver GT3 coupe") == 1
    car = next(s for s in resolved.slots if s.id == "car")
    assert car.inline is True
    scene = next(s for s in resolved.slots if s.id == "scene")
    assert scene.inline is False


def test_selection_still_drives_the_woven_draw(lib):
    tpl, resolved = rt(lib, "inline-basic", selection={"car": "m4"})
    out = render(resolved, "string", tpl.render)
    assert "photo of a frozen-blue M4 rolling" in out.positive


def test_emphasis_wrap_travels_into_the_weave(lib):
    tpl, resolved = rt(lib, "inline-emphasis")
    out = render(resolved, "string", tpl.render)
    assert "hero shot of a (silver GT3 coupe:1.2)" in out.positive
    assert out.positive.count("silver GT3 coupe") == 1


def test_suffix_weaves_too(lib):
    tpl, resolved = rt(lib, "inline-suffix")
    out = render(resolved, "string", tpl.render)
    assert out.positive == "dusk pit lane, captured beside a silver GT3 coupe"


def test_lora_catchword_inline_keeps_tag_and_entries(lib):
    tpl, resolved = rt(lib, "inline-lora")
    out = render(resolved, "string", tpl.render)
    assert out.positive.startswith("a pristine BMWM4CS_G82 on track")
    assert out.positive.endswith("<lora:mastermerlin/bmw_m4_cs:0.35>")
    assert out.positive.count("BMWM4CS_G82") == 1
    assert lora_entries(resolved) == [
        {
            "lora": "mastermerlin\\bmw_m4_cs.safetensors",
            "strength_model": 0.35,
            "strength_clip": 0.35,
        }
    ]


def test_labeled_format_drops_the_labeled_line(lib):
    tpl, resolved = rt(lib, "inline-basic")
    out = render(resolved, "string_labeled", tpl.render)
    assert "cinematic photo of a silver GT3 coupe rolling through" in out.positive
    assert "Car:" not in out.positive
    assert "Scene: dusk pit lane" in out.positive


def test_json_format_drops_the_key(lib):
    tpl, resolved = rt(lib, "inline-basic")
    obj = json.loads(render(resolved, "json", tpl.render).positive)
    assert "car" not in obj
    assert "silver GT3 coupe" in obj["prefix"]
    assert obj["scene"] == "dusk pit lane"


def test_choices_report_marks_inline(lib):
    tpl, resolved = rt(lib, "inline-basic")
    out = render(resolved, "string", tpl.render)
    assert "car: gt3  [fixed]  (inline)" in out.choices
    assert "scene: dusk  [fixed]\n" in out.choices + "\n"


def test_muted_weave_tidies_seams(lib):
    tpl, resolved = rt(lib, "inline-basic", selection={"car": "off"})
    out = render(resolved, "string", tpl.render)
    assert out.positive == "cinematic photo of a rolling through, dusk pit lane"
    assert "  " not in out.positive


def test_missing_ref_weaves_empty_and_warns(lib):
    tpl, resolved = rt(lib, "inline-missing")
    out = render(resolved, "string", tpl.render)
    assert out.positive == "a scene, dusk pit lane"
    assert "⚠ ghost" in out.choices


def test_trigger_stays_the_node_contract(lib):
    # a slot literally named 'trigger' is NOT woven — {trigger} keeps meaning
    # the node widget, and the slot renders in the body as usual
    tpl, resolved = rt(lib, "trigger-slot", variables={"trigger": "NODETRIG"})
    out = render(resolved, "string", tpl.render)
    assert out.positive.startswith("X NODETRIG Y")
    assert "silver GT3 coupe" in out.positive
    assert next(s for s in resolved.slots if s.id == "trigger").inline is False


def test_slot_beats_same_named_template_variable(lib):
    tpl, resolved = rt(lib, "var-collision")
    out = render(resolved, "string", tpl.render)
    assert "build: silver GT3 coupe" in out.positive
    assert "VARCAR" not in out.positive


def test_reference_inside_alternation_still_consumes(lib):
    tpl, resolved = rt(lib, "inline-alt")
    out = render(resolved, "string", tpl.render)
    assert out.positive in (
        "with a silver GT3 coupe, dusk pit lane",
        "clean, dusk pit lane",
    )
    assert next(s for s in resolved.slots if s.id == "car").inline is True


def test_preview_api_exposes_inline_flag(lib):
    status, body = promptapi.handle_preview(lib, {"template": "inline-basic"})
    assert status == 200, body
    flags = {s["id"]: s["inline"] for s in body["slots"]}
    assert flags == {"car": True, "scene": False}
