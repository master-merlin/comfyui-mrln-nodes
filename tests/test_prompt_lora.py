"""LoRA blocks: items carrying data.lora emit <lora:...> tags."""

import json

import pytest
import support  # noqa: F401

from mrln.promptlib import Library, lora_tags, render, resolve_template


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def lib(tmp_path):
    factory = tmp_path / "factory"
    _write(
        factory,
        "sections/lora/car.json",
        {
            "items": [
                {
                    "name": "m4-kit",
                    "text": "BMWM4CS_G82",
                    "data": {
                        "lora": "mastermerlin\\bmw_m4_cs.safetensors",
                        "strength_model": 0.35,
                        "strength_clip": 1.0,
                    },
                },
                {
                    "name": "style-kit",
                    "text": "glossy studio style",
                    "data": {"lora": "styles/gloss.ckpt", "strength_model": 0.8},
                },
                {"name": "plain", "text": "no lora here"},
            ]
        },
    )
    _write(
        factory,
        "sections/host.json",
        {
            "items": [
                {
                    "name": "carrier",
                    "text": "a scene with {kit}",
                    "slots": [{"id": "kit", "ref": "lora/car", "default": "style-kit"}],
                }
            ]
        },
    )
    _write(
        factory,
        "templates/with-lora.json",
        {"slots": [{"id": "kit", "ref": "lora/car", "default": "m4-kit"}]},
    )
    _write(
        factory,
        "templates/nested-lora.json",
        {"slots": [{"id": "scene", "ref": "host", "default": "carrier"}]},
    )
    _write(
        factory,
        "templates/no-tags.json",
        {
            "slots": [{"id": "kit", "ref": "lora/car", "default": "m4-kit"}],
            "render": {"lora_tags": False},
        },
    )
    return Library(factory, None)


def rt(lib, slug, selection=None):
    tpl = lib.load_template(slug)
    resolved = resolve_template(
        lib, tpl, seed=0, mode="as configured", selection=selection or {}, variables={}
    )
    return tpl, resolved


def test_tag_built_from_data(lib):
    _, resolved = rt(lib, "with-lora")
    assert lora_tags(resolved) == ["<lora:mastermerlin/bmw_m4_cs:0.35:1>"]


def test_string_render_appends_tag_and_choices_report(lib):
    tpl, resolved = rt(lib, "with-lora")
    out = render(resolved, "string", tpl.render)
    assert out.positive.endswith("BMWM4CS_G82 <lora:mastermerlin/bmw_m4_cs:0.35:1>")
    assert "lora: <lora:mastermerlin/bmw_m4_cs:0.35:1>" in out.choices


def test_strength_clip_defaults_to_model(lib):
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "style-kit"})
    assert lora_tags(resolved) == ["<lora:styles/gloss:0.8>"]  # sc omitted when equal


def test_labeled_and_json_formats(lib):
    tpl, resolved = rt(lib, "with-lora")
    labeled = render(resolved, "string_labeled", tpl.render)
    assert labeled.positive.splitlines()[-1] == "LoRAs: <lora:mastermerlin/bmw_m4_cs:0.35:1>"
    obj = json.loads(render(resolved, "json", tpl.render).positive)
    assert obj["loras"] == ["<lora:mastermerlin/bmw_m4_cs:0.35:1>"]
    flat = json.loads(render(resolved, "json_flat", tpl.render).positive)
    assert flat["prompt"].endswith("<lora:mastermerlin/bmw_m4_cs:0.35:1>")


def test_nested_child_lora_emits(lib):
    tpl, resolved = rt(lib, "nested-lora")
    assert lora_tags(resolved) == ["<lora:styles/gloss:0.8>"]
    out = render(resolved, "string", tpl.render)
    assert out.positive.endswith("<lora:styles/gloss:0.8>")


def test_lora_tags_flag_disables_emission(lib):
    tpl, resolved = rt(lib, "no-tags")
    out = render(resolved, "string", tpl.render)
    assert "<lora:" not in out.positive
    assert "lora: <lora:" in out.choices  # the report still tells the truth


def test_muted_and_plain_items_emit_nothing(lib):
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "off"})
    assert lora_tags(resolved) == []
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "plain"})
    assert lora_tags(resolved) == []


def test_lora_entries_keep_authored_names(lib):
    from mrln.promptlib import lora_entries

    _tpl, resolved = rt(lib, "with-lora")
    assert lora_entries(resolved) == [
        {
            "lora": "mastermerlin\\bmw_m4_cs.safetensors",
            "strength_model": 0.35,
            "strength_clip": 1.0,
        }
    ]


def test_parse_loras_json_roundtrip_and_errors():
    from mrln.nodes.prompt import parse_loras_json

    assert parse_loras_json("") == []
    assert parse_loras_json("[]") == []
    entries = parse_loras_json(
        '[{"lora": "a.safetensors", "strength_model": 0.5}, {"lora": "b", "strength_clip": 0.7}]'
    )
    assert entries == [("a.safetensors", 0.5, 0.5), ("b", 1.0, 0.7)]
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_loras_json("{broken")
    with pytest.raises(ValueError, match="missing the 'lora'"):
        parse_loras_json('[{"strength_model": 1}]')
    with pytest.raises(ValueError, match="JSON list"):
        parse_loras_json('{"lora": "x"}')
