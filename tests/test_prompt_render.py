import json

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln.promptlib import render, resolve_template


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def resolved_basic(lib, **kw):
    tpl = lib.load_template("basic")
    defaults = {
        "seed": 0,
        "mode": "as configured",
        "selection": {"lighting": "daylight", "extra": "gold"},
        "variables": {},
    }
    defaults.update(kw)
    return tpl, resolve_template(lib, tpl, **defaults)


def test_string_format(lib):
    tpl, resolved = resolved_basic(lib)
    out = render(resolved, "string", tpl.render)
    assert out.positive == (
        "photo of a sports car, bright red, Shibuya Crossing, bright daylight, "
        "(shimmering gold:1.3), high quality"
    )
    assert out.negative.startswith("lowres")


def test_string_labeled(lib):
    tpl, resolved = resolved_basic(lib)
    out = render(resolved, "string_labeled", tpl.render)
    lines = out.positive.split("\n")
    assert lines[0] == "photo of a sports car"
    assert "Place: Shibuya Crossing" in lines
    assert lines[-1] == "high quality"


def test_json_format(lib):
    tpl, resolved = resolved_basic(lib)
    obj = json.loads(render(resolved, "json", tpl.render).positive)
    assert list(obj) == ["prefix", "paint", "location", "lighting", "extra", "suffix"]
    assert obj["extra"] == "shimmering gold"  # no emphasis in json
    assert "variant" not in obj


def test_json_variant_key(lib):
    tpl = lib.load_template("varianted")
    resolved = resolve_template(lib, tpl, seed=0, mode="as configured", selection={}, variables={})
    obj = json.loads(render(resolved, "json", tpl.render).positive)
    assert obj["variant"] == "studio"


def test_json_flat(lib):
    tpl, resolved = resolved_basic(lib)
    obj = json.loads(render(resolved, "json_flat", tpl.render).positive)
    assert obj["prompt"].startswith("photo of a sports car, bright red")


def test_omitted_slot_dropped_everywhere(lib):
    tpl = lib.load_template("basic")
    resolved = None
    for seed in range(40):
        candidate = resolve_template(
            lib,
            tpl,
            seed=seed,
            mode="as configured",
            selection={"lighting": "daylight"},
            variables={},
        )
        if next(s for s in candidate.slots if s.id == "extra").item_name is None:
            resolved = candidate
            break
    assert resolved is not None, "no omitted draw in 40 seeds"
    string_out = render(resolved, "string", tpl.render).positive
    assert ", ," not in string_out and not string_out.endswith(", ")
    obj = json.loads(render(resolved, "json", tpl.render).positive)
    assert "extra" not in obj
    assert "(omitted)" in render(resolved, "string", tpl.render).choices


def test_choices_report(lib):
    tpl, resolved = resolved_basic(lib, selection={"lighting": "random@5", "extra": "petrol"})
    choices = render(resolved, "string", tpl.render).choices
    assert "template: basic" in choices
    assert "paint: red  [fixed]" in choices
    assert "@5" in choices  # per-slot seed echoed
    assert "(user)" in choices  # petrol comes from the user tier


def test_choices_fixed_first_marker(lib):
    tpl = lib.load_template("basic")
    resolved = resolve_template(
        lib, tpl, seed=0, mode="all fixed defaults", selection={}, variables={}
    )
    assert "[fixed:first]" in render(resolved, "string", tpl.render).choices


def test_a_trigger_less_lora_leaves_no_gap_in_the_prompt(tmp_path):
    """A LoRA item that renders no text (SPEC 4.3 'all-muted is legal') must
    still load its weights — and must not leave the dangling separator that
    would betray an empty block."""
    from mrln.promptlib import Library, compose, lora_entries

    root = tmp_path / "factory"
    (root / "sections" / "loralab").mkdir(parents=True)
    (root / "templates").mkdir(parents=True)
    (root / "sections" / "subject.json").write_text(
        json.dumps({"version": 1, "items": [{"name": "car", "text": "a red sports car"}]}),
        encoding="utf-8",
    )
    (root / "sections" / "loralab" / "quiet.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "name": "detail-boost",
                        "text": "",
                        "data": {"lora": "detail.safetensors", "strength_model": 0.7},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "templates" / "t.json").write_text(
        json.dumps(
            {
                "version": 1,
                "slots": [
                    {"id": "subject", "ref": "subject", "default": "car"},
                    {"id": "lora", "ref": "loralab/quiet", "default": "detail-boost"},
                ],
                "suffix": "cinematic lighting",
            }
        ),
        encoding="utf-8",
    )
    lib = Library(factory_root=root, user_root=None)
    composed = compose(
        lib, lib.load_template("t"), seed=1, mode="as configured", selection={}, variables={}
    )
    # the empty block is dropped whole: no ", ," and no trailing comma
    assert composed.rendered.positive == "a red sports car, cinematic lighting"
    # ...and the weights still leave through the loras wire at their strength
    assert lora_entries(composed.resolved) == [
        {"lora": "detail.safetensors", "strength_model": 0.7, "strength_clip": 0.7}
    ]
