"""Pure-handler tests for the prompt HTTP API — no aiohttp, no running
ComfyUI. The aiohttp adapter itself is ~30 straight-line lines verified
during UAT; everything computational is covered here."""

import json

import pytest
import support
from promptlib_fixtures import build_library

from mrln import promptapi


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def ok(result):
    status, body = result
    assert status == 200, body
    return body


# -- library -----------------------------------------------------------------


def test_library_listing(lib):
    body = ok(promptapi.handle_library(lib, {}))
    templates = {t["slug"]: t for t in body["templates"]}
    assert templates["basic"]["tier"] == "factory"
    assert templates["varianted"]["label"] == "Varianted"
    sections = {s["slug"]: s for s in body["sections"]}
    assert sections["color"]["tier"] == "user"  # user override wins
    assert sections["lighting"]["item_count"] == 2
    assert sections["lora/kits"]["has_lora"] is True  # LoRA pill in the tree
    assert sections["lighting"]["has_lora"] is False
    assert "location" in body["folders"]
    assert body["fingerprint"] == lib.fingerprint()


def test_library_survives_broken_user_file(lib):
    bad = lib.user_root / "sections" / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    body = ok(promptapi.handle_library(lib, {}))
    entry = next(s for s in body["sections"] if s["slug"] == "broken")
    assert "error" in entry


# -- template / section / items ----------------------------------------------


def test_template_detail_and_pools(lib):
    body = ok(promptapi.handle_template(lib, {"slug": "basic"}))
    detail = body["template"]
    assert detail["order"] == ["paint", "location", "lighting", "extra"]  # synthesized
    assert detail["render"]["joiner"] == ", "
    assert set(body["pools"]) == {"color", "location", "lighting"}  # twin refs deduped
    pool_names = [item["name"] for item in body["pools"]["location"]]
    assert "urban/shibuya" in pool_names  # folder scope, qualified names
    assert body["raw"]["slots"][0]["id"] == "paint"


def test_template_detail_variants(lib):
    body = ok(promptapi.handle_template(lib, {"slug": "varianted"}))
    detail = body["template"]
    assert [v["name"] for v in detail["variants"]] == ["studio", "outdoor"]
    assert detail["order"] == ["@variant", "paint"]
    assert "location/nature" in body["pools"]


def test_template_missing_slug_and_unknown(lib):
    status, body = promptapi.handle_template(lib, {})
    assert status == 400 and "slug" in body["error"]
    status, body = promptapi.handle_template(lib, {"slug": "nope"})
    assert status == 404 and "remediation" in body


def test_section_detail(lib):
    body = ok(promptapi.handle_section(lib, {"slug": "lighting"}))
    assert body["negative"] == "flat lighting"
    assert body["items"][0]["name"] == "daylight"
    assert body["raw"]["label"] == "Lighting"


def test_items_folder_scope(lib):
    body = ok(promptapi.handle_items(lib, {"ref": "location"}))
    names = [item["name"] for item in body["items"]]
    assert "nature/alpine-pass" in names and "urban/shibuya" in names


def test_items_carry_lora_flag(lib):
    body = ok(promptapi.handle_items(lib, {"ref": "lora/kits"}))
    assert body["items"][0]["lora"] == "kits\\hycade.safetensors"
    plain = ok(promptapi.handle_items(lib, {"ref": "lighting"}))
    assert all("lora" not in item for item in plain["items"])


# -- preview -----------------------------------------------------------------


def test_preview_deterministic(lib):
    payload = {"template": "basic", "seed": 7, "selection": "paint=random"}
    assert ok(promptapi.handle_preview(lib, payload)) == ok(promptapi.handle_preview(lib, payload))


def test_preview_selection_string_and_dict_equivalent(lib):
    a = ok(promptapi.handle_preview(lib, {"template": "basic", "selection": "paint=petrol"}))
    b = ok(promptapi.handle_preview(lib, {"template": "basic", "selection": {"paint": "petrol"}}))
    assert a == b
    paint = next(s for s in a["slots"] if s["id"] == "paint")
    assert paint["item"] == "petrol" and paint["tier"] == "user"


def test_preview_slot_detail_and_trigger(lib):
    body = ok(
        promptapi.handle_preview(lib, {"template": "basic", "seed": 3, "trigger": "SkylineGTR"})
    )
    assert body["positive"].startswith("photo of a SkylineGTR")
    for slot in body["slots"]:
        assert {"id", "item", "random", "seed_used", "tier", "omitted"} <= set(slot)
    assert "choices" in body and body["format"] == "string"


def test_preview_format_override(lib):
    body = ok(promptapi.handle_preview(lib, {"template": "basic", "format": "json"}))
    assert json.loads(body["positive"])["paint"] == "bright red"


def test_preview_errors(lib):
    assert promptapi.handle_preview(lib, {"template": "nope"})[0] == 404
    status, body = promptapi.handle_preview(lib, {"template": "basic", "seed": "abc"})
    assert status == 400 and "seed" in body["error"]
    status, body = promptapi.handle_preview(lib, {"template": "basic", "format": "yaml"})
    assert status == 400 and "format" in body["error"]
    status, body = promptapi.handle_preview(lib, {"template": "basic", "selection": "bogus=1"})
    assert status == 400 and "unknown slot" in body["error"]


def test_preview_matches_node_execute(tmp_path, monkeypatch):
    """The panel preview and the node must produce identical output."""
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "user"))
    pack = support.load_pack()
    node = pack.NODE_CLASS_MAPPINGS["MRLN_PromptTemplate"]()
    prompt, negative, choices, _loras, _llm = node.execute(
        template="overdrive/full-shot",
        selection="paint=guards-red",
        selection_mode="as configured",
        seed=11,
        format="template default",
        trigger="SkylineGTR34Vspec",
    )
    from mrln.promptlib import open_library

    body = ok(
        promptapi.handle_preview(
            open_library(),
            {
                "template": "overdrive/full-shot",
                "selection": "paint=guards-red",
                "seed": 11,
                "trigger": "SkylineGTR34Vspec",
            },
        )
    )
    assert (body["positive"], body["negative"], body["choices"]) == (prompt, negative, choices)


# -- save / delete -----------------------------------------------------------


def test_save_section_and_override_flag(lib):
    body = ok(
        promptapi.handle_save_section(
            lib, {"slug": "car/trim", "data": {"items": [{"name": "x", "text": "y"}]}}
        )
    )
    assert body == {"ok": True, "slug": "car/trim", "tier": "user", "overrides_factory": False}
    body = ok(
        promptapi.handle_save_section(
            lib, {"slug": "lighting", "data": {"items": [{"name": "x", "text": "y"}]}}
        )
    )
    assert body["overrides_factory"] is True
    assert lib.tier_of("sections", "lighting") == "user"


def test_save_template(lib):
    data = {"slots": [{"id": "paint", "ref": "color"}]}
    body = ok(promptapi.handle_save_template(lib, {"slug": "mine", "data": data}))
    assert body["ok"] and lib.load_template("mine").slots[0].ref == "color"


def test_save_rejects_invalid(lib):
    # items must be a LIST ([] is legal: extend files may only retag)
    status, body = promptapi.handle_save_section(lib, {"slug": "bad", "data": {"items": "x"}})
    assert status == 400 and "items" in body["error"]
    assert "bad" not in lib.section_slugs()
    status, _ = promptapi.handle_save_section(
        lib, {"slug": "../escape", "data": {"items": [{"name": "x", "text": "y"}]}}
    )
    assert status == 400
    status, _ = promptapi.handle_save_section(lib, {"slug": "no-data"})
    assert status == 400


def test_delete_and_revert(lib):
    ok(promptapi.handle_save_section(lib, {"slug": "lighting", "data": {"items": ["x"]}}))
    body = ok(promptapi.handle_delete(lib, {"kind": "sections", "slug": "lighting"}))
    assert body["reverted_to_factory"] is True
    assert promptapi.handle_delete(lib, {"kind": "sections", "slug": "lighting"})[0] == 404
    status, _ = promptapi.handle_delete(lib, {"kind": "profiles", "slug": "x"})
    assert status == 400


# -- registration ------------------------------------------------------------


def test_register_routes_soft_fails_outside_comfyui():
    assert promptapi.register_routes() is False  # no `server` module under pytest


def test_route_table_sane():
    for method, path, handler, reads_body in promptapi.ROUTES:
        assert method in ("get", "post")
        assert path.startswith("/mrln/prompt/")
        assert callable(handler)
        assert isinstance(reads_body, bool)


def test_preview_inline_template_data_matches_slug(lib):
    raw = promptapi._raw_file(lib, "templates", "basic")
    by_slug = ok(promptapi.handle_preview(lib, {"template": "basic", "seed": 5}))
    by_data = ok(
        promptapi.handle_preview(lib, {"template": "basic", "template_data": raw, "seed": 5})
    )
    for key in ("positive", "negative", "choices"):
        assert by_slug[key] == by_data[key]


def test_preview_inline_template_data_reflects_edits(lib):
    raw = promptapi._raw_file(lib, "templates", "basic")
    raw["prefix"] = "EDITED {trigger} skeleton"
    body = ok(promptapi.handle_preview(lib, {"template": "basic", "template_data": raw, "seed": 5}))
    assert body["positive"].startswith("EDITED sports car skeleton")


def test_preview_inline_template_data_invalid(lib):
    status, body = promptapi.handle_preview(
        lib, {"template": "basic", "template_data": {"slots": [{"id": "x"}]}}
    )
    assert status == 400 and "ref" in body["error"]
    status, _ = promptapi.handle_preview(lib, {"template_data": "not a dict"})
    assert status == 400
