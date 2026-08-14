"""Target-model profiles (Phase A): pack/user/template merging, render
overrides with widget precedence, the json_template scaffold, and the
node's llm output + profile widget."""

import json

import pytest
import support

from mrln import promptapi
from mrln.promptlib import (
    Library,
    SchemaError,
    SelectionError,
    compose,
    fill_json_template,
    merged_profiles,
)


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def lib(tmp_path):
    factory = tmp_path / "factory"
    user = tmp_path / "user"
    _write(
        factory,
        "profiles.json",
        {
            "profiles": {
                "krea2": {
                    "render": {"format": "string_labeled", "text_length": "long"},
                    "llm": {"system": "FACTORY-KREA", "params": {"max_words": 220}},
                },
                "sdxl": {
                    "render": {"format": "string", "text_length": "short"},
                    "llm": {"system": "FACTORY-SDXL"},
                },
            }
        },
    )
    _write(
        user,
        "profiles.json",
        {"profiles": {"krea2": {"llm": {"system": "USER-KREA"}}}},
    )
    _write(
        factory,
        "sections/color.json",
        {
            "label": "Color",
            "negative": "muddy tones",
            "items": [
                {"name": "red", "text": "bright red", "text_short": "red"},
                {"name": "blue", "text": "ocean blue", "text_short": "blue"},
            ],
        },
    )
    _write(
        factory,
        "templates/basic.json",
        {
            "slots": [
                {"id": "paint", "ref": "color", "default": "red"},
                {"id": "extra", "ref": "color", "default": "blue"},
            ],
            "render": {"format": "string", "joiner": ", "},
            "profiles": {
                "krea2": {"llm": {"params": {"max_words": 99}}},
                "mine": {"render": {"format": "json"}},
                "tuned": {
                    # per-profile template VARIANT: sparse diff vs standard
                    "overrides": {
                        "prefix": "TUNED:",
                        "slots": {"paint": {"default": "blue", "emphasis": 1.4}},
                    },
                },
                "ideo": {
                    "render": {"format": "string"},
                    "json_template": {
                        "prompt": "{positive}",
                        "negative_prompt": "{negative}",
                        "style": "{slot:missing|REALISTIC}",
                        "palette": {"members": "{slot:paint,extra}"},
                        "empty_drop": "{slot:missing}",
                        "embedded": "P={positive} S={slot:paint}",
                        "keep": 7,
                    },
                },
            },
        },
    )
    return Library(factory, user)


def run(lib, **kw):
    tpl = lib.load_template("basic")
    args = {"seed": 0, "mode": "as configured", "selection": {}, "variables": {}}
    args.update(kw)
    return compose(lib, tpl, **args)


# -- merging -------------------------------------------------------------


def test_pack_profiles_user_overlays_factory(lib):
    pack = lib.pack_profiles()
    assert pack["krea2"]["llm"]["system"] == "USER-KREA"  # user tier wins the key
    assert pack["krea2"]["llm"]["params"] == {"max_words": 220}  # factory keys survive
    assert pack["krea2"]["render"]["format"] == "string_labeled"
    assert pack["sdxl"]["llm"]["system"] == "FACTORY-SDXL"


def test_template_extends_pack(lib):
    merged = merged_profiles(lib, lib.load_template("basic"))
    assert merged["krea2"]["llm"] == {"system": "USER-KREA", "params": {"max_words": 99}}
    assert merged["mine"]["render"]["format"] == "json"
    assert "sdxl" in merged


# -- compose: precedence -------------------------------------------------


def test_standard_profile_is_plain_render(lib):
    composed = run(lib)
    assert composed.profile == "standard"
    assert composed.format == "string"
    assert composed.rendered.positive == "bright red, ocean blue"
    # the llm output always carries the prompt — ONE wire feeds Enhance
    assert json.loads(composed.llm) == {
        "target": "standard",
        "prompt": "bright red, ocean blue",
    }


def test_profile_render_overrides_apply(lib):
    composed = run(lib, profile="krea2")
    assert composed.format == "string_labeled"
    assert "Color: bright red" in composed.rendered.positive
    llm = json.loads(composed.llm)
    assert llm == {
        "target": "krea2",
        "prompt": composed.rendered.positive,
        "system": "USER-KREA",
        "params": {"max_words": 99},
    }


def test_profile_text_length_and_widget_precedence(lib):
    short = run(lib, profile="sdxl")
    assert short.rendered.positive == "red, blue"  # profile's short texts
    long_widget = run(lib, profile="sdxl", text_length="long")
    assert long_widget.rendered.positive == "bright red, ocean blue"  # widget wins
    fmt_widget = run(lib, profile="krea2", format="json")
    assert fmt_widget.format == "json"  # widget beats profile format


def test_unknown_profile_lists_names(lib):
    with pytest.raises(SelectionError, match="standard, ideo, krea2, mine, sdxl, tuned"):
        run(lib, profile="nope")


# -- per-profile template variants (overrides) -------------------------------


def test_overrides_apply_as_variant(lib):
    tuned = run(lib, profile="tuned")
    assert tuned.rendered.positive.startswith("TUNED:")
    assert "(ocean blue:1.4)" in tuned.rendered.positive  # paint: blue @ 1.4
    # the base render stays untouched — 'standard' is always the way back
    standard = run(lib)
    assert standard.rendered.positive == "bright red, ocean blue"


def test_overrides_validation(lib, tmp_path):
    def bad_template(name, overrides):
        _write(
            tmp_path / "factory",
            f"templates/{name}.json",
            {
                "slots": [{"id": "paint", "ref": "color"}],
                "profiles": {"x": {"overrides": overrides}},
            },
        )
        lib.invalidate()  # the scan memo must see the file written after first load

    bad_template("bad-slot", {"slots": {"ghost": {"default": "red"}}})
    with pytest.raises(SchemaError, match="unknown slot 'ghost'"):
        lib.load_template("bad-slot")
    bad_template("bad-key", {"order": ["paint"]})
    with pytest.raises(SchemaError, match="unknown overrides key"):
        lib.load_template("bad-key")
    bad_template("bad-emphasis", {"slots": {"paint": {"emphasis": -1}}})
    with pytest.raises(SchemaError, match="'emphasis' must be > 0"):
        lib.load_template("bad-emphasis")
    bad_template("bad-field", {"slots": {"paint": {"label": "X"}}})
    with pytest.raises(SchemaError, match="allow only default/emphasis"):
        lib.load_template("bad-field")


def test_overrides_via_preview_and_save_api(lib):
    status, body = promptapi.handle_preview(
        lib, {"template": "basic", "seed": 0, "profile": "tuned"}
    )
    assert status == 200, body
    assert body["positive"].startswith("TUNED:")
    # saving a template with a variant diff roundtrips; broken ones 400
    raw = {
        "slots": [{"id": "paint", "ref": "color", "default": "red"}],
        "profiles": {"sdxl": {"overrides": {"slots": {"paint": {"default": "blue"}}}}},
    }
    status, _ = promptapi.handle_save_template(lib, {"slug": "variant-rt", "data": raw})
    assert status == 200
    composed = compose(
        lib,
        lib.load_template("variant-rt"),
        seed=0,
        mode="as configured",
        selection={},
        variables={},
        profile="sdxl",
    )
    assert composed.rendered.positive == "blue"  # override pick, profile's short text
    raw["profiles"]["sdxl"]["overrides"]["slots"]["ghost"] = {"default": "x"}
    status, body = promptapi.handle_save_template(lib, {"slug": "variant-rt", "data": raw})
    assert status == 400 and "ghost" in body["error"]


# -- json_template ---------------------------------------------------------


def test_json_template_fills_and_drops(lib):
    composed = run(lib, profile="ideo")
    payload = json.loads(composed.rendered.positive)
    assert payload["prompt"] == "bright red, ocean blue"
    assert payload["negative_prompt"] == "muddy tones"
    assert payload["style"] == "REALISTIC"  # missing slot -> fallback
    assert payload["palette"]["members"] == ["bright red", "ocean blue"]
    assert "empty_drop" not in payload  # missing slot, no fallback -> dropped
    assert payload["embedded"] == "P=bright red, ocean blue S=bright red"
    assert payload["keep"] == 7


def test_fill_json_template_container_cleanup():
    filled = fill_json_template(
        {"a": {"b": "{slot:gone}"}, "c": ["{slot:gone}", "x"], "d": None},
        "P",
        "N",
        {},
    )
    assert filled == {"c": ["x"], "d": None}  # empty dict dropped, list filtered


# -- fingerprint -----------------------------------------------------------


def test_fingerprint_reacts_to_profiles_file(lib, tmp_path):
    before = lib.fingerprint()
    path = tmp_path / "user" / "profiles.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["profiles"]["new-one"] = {"render": {"format": "json"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert lib.fingerprint() != before


# -- profile editing API -----------------------------------------------------


def test_profile_detail_shows_tiers(lib):
    status, body = promptapi.handle_profile(lib, {"name": "krea2"})
    assert status == 200
    assert body["factory"]["llm"]["system"] == "FACTORY-KREA"
    assert body["user"]["llm"]["system"] == "USER-KREA"
    assert body["merged"]["llm"]["system"] == "USER-KREA"
    assert promptapi.handle_profile(lib, {"name": "nope"})[0] == 404


def test_save_profile_roundtrip_and_delete(lib):
    status, body = promptapi.handle_save_profile(
        lib, {"name": "my-model", "data": {"llm": {"system": "S"}}}
    )
    assert status == 200 and "my-model" in body["profiles"]
    assert lib.pack_profiles()["my-model"]["llm"]["system"] == "S"
    listing = promptapi.handle_library(lib, {})[1]["profiles"]
    tiers = {p["name"]: p["tier"] for p in listing}
    assert tiers["my-model"] == "user" and tiers["krea2"] == "factory+user"
    assert tiers["sdxl"] == "factory"
    # deleting the user entry reverts krea2 to pure factory
    status, _ = promptapi.handle_save_profile(lib, {"name": "krea2", "data": None})
    assert status == 200
    assert lib.pack_profiles()["krea2"]["llm"]["system"] == "FACTORY-KREA"


def test_save_profile_validation(lib):
    assert promptapi.handle_save_profile(lib, {"name": "standard", "data": {}})[0] == 400
    assert promptapi.handle_save_profile(lib, {"name": "Bad Name", "data": {}})[0] == 400
    status, body = promptapi.handle_save_profile(
        lib, {"name": "x", "data": {"render": {"format": "yaml"}}}
    )
    assert status == 400 and "format" in body["error"]
    assert promptapi.handle_save_profile(lib, {"name": "ghost", "data": None})[0] == 404


# -- node integration --------------------------------------------------------


@pytest.fixture()
def node_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    pack = support.load_pack()
    return pack.NODE_CLASS_MAPPINGS["MRLN_PromptTemplate"]


# one entry per Arcane Tuner model family (D:\MRLN Arcane Tuner\backend\
# app\engine\models\families) plus the classic public families
FACTORY_PROFILES = {
    # image
    "krea2",
    "flux",
    "flux2-klein",
    "ernie-image",
    "boogu-image",
    "qwen-image",
    "hidream",
    "sdxl",
    "sd15",
    "pony",
    "illustrious",
    "microsoft-lens",
    "zimage",
    "chroma",
    "lumina2",
    "longcat-image",
    "nucleus-image",
    "dreamlite",
    "omnigen2",
    "ovis-image",
    "prx",
    "ideogram4",
    # video
    "ltx2",
    "wan21",
    "wan22",
    "hunyuan-video15",
    "kandinsky5",
    "bernini-r",
    # audio
    "ace-step15",
}


def test_every_factory_profile_carries_fidelity_and_style_lock():
    # UAT 2026-08-12: enhancers dropped named content and converted stated
    # media (anime -> realistic). Both guarantees are universal — image,
    # video and audio families alike, since every one of them feeds the
    # Enhance node's system prompt.
    import json
    from pathlib import Path

    path = Path(support.ROOT) / "mrln" / "data" / "prompt" / "profiles.json"
    profiles = json.loads(path.read_text(encoding="utf-8"))["profiles"]
    assert len(profiles) >= 29
    for name, prof in profiles.items():
        system = (prof.get("llm") or {}).get("system") or ""
        assert system, f"profile '{name}' ships no LLM system prompt"
        assert "FIDELITY:" in system, name
        assert "STYLE LOCK:" in system, name


def test_profile_widget_position_and_lists_factory_profiles(node_env):
    inputs = node_env.INPUT_TYPES()
    all_names = [*inputs["required"], *inputs.get("optional", {})]
    # Ruling D2 (SPEC §1) RETIRED "profile stays last": the real rule it stood
    # in for is append-only. The order was re-cut once before shipping (see
    # test_protocol_nodes.FROZEN_ORDER, which owns the full list) — what this
    # test still guards is that profile sits with the OUTPUT-shaping widgets it
    # presets, not that it holds one particular index forever.
    assert all_names.index("profile") > all_names.index("format")
    options = inputs["optional"]["profile"][0]
    assert options[0] == "standard"
    # one EXPLICIT entry per model family — users must not need to know
    # which models share prompting conventions
    assert set(options) >= FACTORY_PROFILES


def test_node_llm_output_and_profile_render(node_env):
    node = node_env()
    # node outputs are lists since SPEC 4.2 (OUTPUT_IS_LIST); unbatched = 1 item
    out = [
        values[0]
        for values in node.execute(
            template="overdrive/full-shot",
            selection="",
            selection_mode="as configured",
            seed=3,
            format="template default",
            profile="sdxl",
        )
    ]
    llm = json.loads(out[1])
    assert llm["target"] == "sdxl" and "system" in llm
    assert llm["prompt"] == out[0]  # single wire: the llm output carries the prompt
    standard = [
        values[0]
        for values in node.execute(
            template="overdrive/full-shot",
            selection="",
            selection_mode="as configured",
            seed=3,
            format="template default",
        )
    ]
    standard_llm = json.loads(standard[1])
    assert standard_llm["target"] == "standard" and standard_llm["prompt"] == standard[0]
    assert (
        node_env.VALIDATE_INPUTS(template="overdrive/full-shot", selection="", profile="krea2")
        is True
    )
    verdict = node_env.VALIDATE_INPUTS(
        template="overdrive/full-shot", selection="", profile="bogus"
    )
    assert "unknown profile" in verdict


def test_preview_parity_with_profile(node_env):
    node = node_env()
    prompt, llm, _loras, negative, choices, *_rest = node.execute(
        template="overdrive/full-shot",
        selection="paint=guards-red",
        selection_mode="as configured",
        seed=11,
        format="template default",
        profile="sdxl",
    )
    # node outputs are lists since SPEC 4.2 (OUTPUT_IS_LIST); unbatched = 1 item
    prompt, negative, choices, llm = prompt[0], negative[0], choices[0], llm[0]
    from mrln.promptlib import open_library

    status, body = promptapi.handle_preview(
        open_library(),
        {
            "template": "overdrive/full-shot",
            "selection": "paint=guards-red",
            "seed": 11,
            "profile": "sdxl",
        },
    )
    assert status == 200, body
    assert (body["positive"], body["negative"], body["choices"]) == (prompt, negative, choices)
    assert body["profile"] == "sdxl" and body["llm"] == llm
