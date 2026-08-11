"""LoRA blocks: items carrying data.lora signal the loader via lora_entries
(the node's 'loras' output → LoRA Apply). <lora:...> PROMPT tags are opt-in
(render.lora_tags) for A1111-style tag-parsing loaders — in ComfyUI they are
inert tokens, so the default keeps the prompt clean."""

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
                        "comment": "urn:air:sdxl:lora:civitai:333@444",
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
        {
            "slots": [{"id": "scene", "ref": "host", "default": "carrier"}],
            "render": {"lora_tags": True},
        },
    )
    _write(
        factory,
        "templates/tagged.json",
        {
            "slots": [{"id": "kit", "ref": "lora/car", "default": "m4-kit"}],
            "render": {"lora_tags": True},
        },
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


def test_default_render_keeps_prompt_clean(lib):
    # ComfyUI-native default: the prompt carries only the catchword — loading
    # is signalled via lora_entries/the Apply node, and choices still report
    tpl, resolved = rt(lib, "with-lora")
    out = render(resolved, "string", tpl.render)
    assert "<lora:" not in out.positive
    assert out.positive.endswith("BMWM4CS_G82")
    assert "lora: <lora:mastermerlin/bmw_m4_cs:0.35:1>" in out.choices


def test_opt_in_appends_tag_and_choices_report(lib):
    tpl, resolved = rt(lib, "tagged")
    out = render(resolved, "string", tpl.render)
    assert out.positive.endswith("BMWM4CS_G82 <lora:mastermerlin/bmw_m4_cs:0.35:1>")
    assert "lora: <lora:mastermerlin/bmw_m4_cs:0.35:1>" in out.choices


def test_strength_clip_defaults_to_model(lib):
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "style-kit"})
    assert lora_tags(resolved) == ["<lora:styles/gloss:0.8>"]  # sc omitted when equal


def test_labeled_and_json_formats(lib):
    tpl, resolved = rt(lib, "tagged")
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


def test_explicit_false_also_keeps_prompt_clean(lib):
    tpl, resolved = rt(lib, "no-tags")
    out = render(resolved, "string", tpl.render)
    assert "<lora:" not in out.positive
    assert "lora: <lora:" in out.choices  # the report still tells the truth


def test_muted_and_plain_items_emit_nothing(lib):
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "off"})
    assert lora_tags(resolved) == []
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "plain"})
    assert lora_tags(resolved) == []


def test_lora_entries_keep_authored_names_and_carry_air(lib):
    from mrln.promptlib import lora_entries

    _tpl, resolved = rt(lib, "with-lora")
    assert lora_entries(resolved) == [
        {
            "lora": "mastermerlin\\bmw_m4_cs.safetensors",
            "strength_model": 0.35,
            "strength_clip": 1.0,
            # the comment's AIR urn rides along: the wire tells a machine
            # missing the file where to get it
            "air": "urn:air:sdxl:lora:civitai:333@444",
        }
    ]
    # a plain comment is NOT an air field
    _tpl, resolved = rt(lib, "with-lora", selection={"kit": "style-kit"})
    assert "air" not in lora_entries(resolved)[0]


def test_parse_loras_json_roundtrip_and_errors():
    from mrln.nodes.prompt import parse_loras_json

    assert parse_loras_json("") == []
    assert parse_loras_json("[]") == []
    entries = parse_loras_json(
        '[{"lora": "a.safetensors", "strength_model": 0.5}, {"lora": "b", "strength_clip": 0.7}]'
    )
    assert entries == [("a.safetensors", 0.5, 0.5, ""), ("b", 1.0, 0.7, "")]
    entries = parse_loras_json('[{"lora": "x", "air": "urn:air:sdxl:lora:civitai:1@2"}]')
    assert entries == [("x", 1.0, 1.0, "urn:air:sdxl:lora:civitai:1@2")]
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_loras_json("{broken")
    with pytest.raises(ValueError, match="missing the 'lora'"):
        parse_loras_json('[{"strength_model": 1}]')
    with pytest.raises(ValueError, match="JSON list"):
        parse_loras_json('{"lora": "x"}')


def test_llm_wire_carries_protected_triggers(lib):
    from mrln.promptlib import compose

    composed = compose(
        lib,
        lib.load_template("with-lora"),
        seed=0,
        mode="as configured",
        selection={},
        variables={"trigger": "EXTRA_TRIG"},
    )
    spec = json.loads(composed.llm)
    # drawn LoRA trigger text first, then the {trigger} variable — the
    # Enhance node enforces these verbatim
    assert spec["protect"] == ["BMWM4CS_G82", "EXTRA_TRIG"]
    muted = compose(
        lib,
        lib.load_template("with-lora"),
        seed=0,
        mode="as configured",
        selection={"kit": "off"},
        variables={},
    )
    assert "protect" not in json.loads(muted.llm)  # nothing drawn, nothing to guard


# -- download-by-AIR healing --------------------------------------------------


def test_parse_air():
    from mrln.promptapi import parse_air

    assert parse_air("urn:air:sdxl:lora:civitai:333@444") == (333, 444)
    assert parse_air("URN:AIR:flux1:lora:civitai:1@2") == (1, 2)
    assert parse_air("urn:air:sdxl:lora:civitai:333") is None  # no version
    assert parse_air("urn:air:sdxl:lora:tensorart:1@2") is None  # civitai only
    assert parse_air("") is None and parse_air(None) is None


def test_download_path_sanitizers():
    from mrln.promptapi import ApiError, _sanitize_lora_filename, _sanitize_subfolder

    assert _sanitize_subfolder("") == ""
    assert _sanitize_subfolder("testing\\deep") == "testing/deep"
    assert _sanitize_subfolder("/testing/") == "testing"
    with pytest.raises(ApiError):
        _sanitize_subfolder("../escape")
    with pytest.raises(ApiError):
        _sanitize_subfolder("ok/../escape")
    assert _sanitize_lora_filename("car") == "car.safetensors"
    assert _sanitize_lora_filename("sub\\car.safetensors") == "car.safetensors"
    assert _sanitize_lora_filename("") == ""
    with pytest.raises(ApiError):
        _sanitize_lora_filename("bad|name")


def test_lora_download_endpoint_guards(tmp_path):
    from mrln import promptapi

    lib = Library(tmp_path / "factory", tmp_path / "user")
    status, body = promptapi.handle_lora_download(lib, {"air": "not-an-air", "start": True})
    assert status == 400 and "AIR" in body["error"]
    status, body = promptapi.handle_lora_download(lib, {"air": "urn:air:sdxl:lora:civitai:1@2"})
    assert status == 200 and body["status"] == "unknown"  # poll before any start
    # outside a running ComfyUI there is no loras folder to write into
    status, body = promptapi.handle_lora_download(
        lib, {"air": "urn:air:sdxl:lora:civitai:1@2", "start": True}
    )
    assert status == 400 and "ComfyUI" in body["error"]


def test_heal_section_lora(lib, tmp_path):
    from mrln.promptapi import ApiError, _heal_section_lora

    user = tmp_path / "user"
    healing = Library(tmp_path / "factory", user)
    # factory-origin item: the user tier gets a FULL snapshot (thin entries
    # would wipe the texts — tier merge replaces items by name wholesale)
    _heal_section_lora(healing, "lora/car", "m4-kit", "moved/bmw_m4_cs.safetensors")
    merged = healing.load_section("lora/car")
    item = next(i for i in merged.items if i.name == "m4-kit")
    assert item.data["lora"] == "moved/bmw_m4_cs.safetensors"
    assert item.text == "BMWM4CS_G82"  # texts survived the heal
    assert item.data["strength_model"] == 0.35  # sibling data keys survived
    assert item.data["comment"] == "urn:air:sdxl:lora:civitai:333@444"
    # healing again edits the existing user entry in place
    _heal_section_lora(healing, "lora/car", "m4-kit", "again/bmw_m4_cs.safetensors")
    merged = healing.load_section("lora/car")
    item = next(i for i in merged.items if i.name == "m4-kit")
    assert item.data["lora"] == "again/bmw_m4_cs.safetensors"
    raw = json.loads((user / "sections" / "lora" / "car.json").read_text(encoding="utf-8"))
    assert len([i for i in raw["items"] if i.get("name") == "m4-kit"]) == 1
    with pytest.raises(ApiError):
        _heal_section_lora(healing, "lora/car", "ghost", "x.safetensors")
