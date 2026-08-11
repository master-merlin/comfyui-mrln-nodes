"""Shareable bundles: export embeds the user tier a template needs,
import writes it back — validated, conflict-aware, idempotent."""

import json

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_roots

from mrln import promptapi
from mrln.promptlib import (
    Library,
    SchemaError,
    export_bundle,
    import_bundle,
    section_closure,
)

AIR = "urn:air:sdxl:lora:civitai:101@202"


@pytest.fixture()
def lib(tmp_path):
    library = Library(*build_roots(tmp_path))
    # a LoRA item with an AIR comment + a template drawing user AND factory
    # sections, folder scope and nested child slots included
    library.save_user(
        "sections",
        "lora/kits",
        {
            "items": [
                {
                    "name": "bodykit",
                    "text": "HycadeBodykit",
                    "data": {
                        "lora": "kits/hycade.safetensors",
                        "strength_model": 0.87,
                        "comment": AIR,
                    },
                }
            ]
        },
    )
    library.save_user(
        "templates",
        "share-me",
        {
            "label": "Share Me",
            "slots": [
                {"id": "paint", "ref": "color", "default": "petrol"},
                {"id": "vibe", "ref": "mood", "default": "random"},
                {"id": "kit", "ref": "lora/kits", "default": "bodykit"},
                {"id": "place", "ref": "location", "default": "random"},
                {"id": "crew", "ref": "crew", "default": "pair"},
            ],
        },
    )
    return library


def fresh_install(tmp_path):
    """Same factory content, EMPTY user tier — the receiving machine."""
    factory, _user = build_roots(tmp_path / "dest")
    return Library(factory, tmp_path / "dest-user")


# -- closure -----------------------------------------------------------------


def test_closure_transitive_and_folder(lib):
    sections, missing = section_closure(lib, ["crew", "location"])
    # crew's 'pair' item declares child slots on color -> color joins
    assert {"crew", "color", "location/urban", "location/nature"} <= set(sections)
    assert missing == []


def test_closure_survives_self_nesting(lib):
    sections, _ = section_closure(lib, ["loop"])  # item nests its own section
    assert "loop" in sections


def test_closure_reports_missing(lib):
    _, missing = section_closure(lib, ["ghost/section"])
    assert missing == ["ghost/section"]


# -- export ------------------------------------------------------------------


def test_export_template_bundle(lib):
    bundle = export_bundle(lib, "templates", "share-me")
    assert (bundle["format"], bundle["kind"], bundle["slug"]) == (
        "mrln-bundle",
        "template",
        "share-me",
    )
    # user-tier sections travel verbatim (color's file is the extend diff)
    assert set(bundle["sections"]) == {"color", "mood", "lora/kits"}
    assert any(i["name"] == "petrol" for i in bundle["sections"]["color"]["items"])
    # factory-pure closure members are refs, not copies
    assert set(bundle["factory_refs"]) == {
        "lighting",
        "location/urban",
        "location/nature",
        "crew",
    } - {"lighting"}  # lighting is not referenced by share-me
    assert bundle["template"]["label"] == "Share Me"
    assert bundle["loras"] == [
        {
            "file": "kits/hycade.safetensors",
            "section": "lora/kits",
            "item": "bodykit",
            "air": AIR,
        }
    ]


def test_export_section_bundle(lib):
    bundle = export_bundle(lib, "sections", "lora/kits")
    assert bundle["kind"] == "section"
    assert set(bundle["sections"]) == {"lora/kits"}
    assert bundle["loras"][0]["air"] == AIR


def test_export_factory_pure_section_refuses(lib):
    with pytest.raises(SchemaError, match="factory content"):
        export_bundle(lib, "sections", "lighting")


def test_export_unknown_kind(lib):
    with pytest.raises(SchemaError, match="bundle kind"):
        export_bundle(lib, "profiles", "krea2")


# -- import ------------------------------------------------------------------


def test_import_into_fresh_install(lib, tmp_path):
    bundle = export_bundle(lib, "templates", "share-me")
    dest = fresh_install(tmp_path)
    report = import_bundle(dest, bundle)
    written = {(w["kind"], w["slug"]) for w in report["written"]}
    assert ("template", "share-me") in written
    assert ("section", "lora/kits") in written
    assert report["missing_factory"] == []
    assert report["template_slug"] == "share-me"
    # the imported template actually composes on the receiving install
    tpl = dest.load_template("share-me")
    assert tpl.label == "Share Me"
    merged = dest.load_section("color")
    assert any(i.name == "petrol" for i in merged.items)  # extend diff landed
    assert report["loras"][0]["air"] == AIR


def test_import_is_idempotent(lib):
    bundle = export_bundle(lib, "templates", "share-me")
    report = import_bundle(lib, bundle)  # back into the very same library
    assert report["written"] == []
    assert {s["reason"] for s in report["skipped"]} == {"identical"}


def test_import_keeps_existing_without_overwrite(lib, tmp_path):
    bundle = export_bundle(lib, "templates", "share-me")
    dest = fresh_install(tmp_path)
    dest.save_user("sections", "mood", {"items": [{"name": "calm", "text": "calm air"}]})
    report = import_bundle(dest, bundle)
    skipped = {s["slug"]: s["reason"] for s in report["skipped"]}
    assert skipped == {"mood": "exists"}
    assert report["needs_overwrite"] is True
    # my file untouched
    assert any(i.name == "calm" for i in dest.load_section("mood").items)
    # ... until overwrite is requested
    report = import_bundle(dest, bundle, overwrite=True)
    assert any(w["slug"] == "mood" for w in report["written"])
    assert any(i.name == "epic" for i in dest.load_section("mood").items)


def test_import_dry_run_writes_nothing(lib, tmp_path):
    bundle = export_bundle(lib, "templates", "share-me")
    dest = fresh_install(tmp_path)
    report = import_bundle(dest, bundle, dry_run=True)
    assert report["dry_run"] is True
    assert any(w["kind"] == "template" for w in report["written"])
    assert dest.tier_of("templates", "share-me") == ""  # nothing on disk


def test_import_template_slug_override(lib, tmp_path):
    bundle = export_bundle(lib, "templates", "share-me")
    dest = fresh_install(tmp_path)
    report = import_bundle(dest, bundle, slug="mine/fork")
    assert report["template_slug"] == "mine/fork"
    assert dest.load_template("mine/fork").label == "Share Me"


def test_import_shadow_flag_and_factory_identical_skip(lib, tmp_path):
    dest = fresh_install(tmp_path)
    factory_raw = json.loads(
        (dest.factory_root / "templates" / "basic.json").read_text(encoding="utf-8")
    )
    bundle = {
        "format": "mrln-bundle",
        "bundle_version": 1,
        "kind": "template",
        "slug": "basic",
        "template": factory_raw,
        "sections": {},
    }
    report = import_bundle(dest, bundle)  # identical to factory -> no shadow file
    assert report["skipped"] == [{"kind": "template", "slug": "basic", "reason": "identical"}]
    bundle["template"] = {**factory_raw, "label": "Basic (edited)"}
    report = import_bundle(dest, bundle)
    assert report["written"] == [{"kind": "template", "slug": "basic", "shadows_factory": True}]


def test_import_all_or_nothing_validation(lib, tmp_path):
    dest = fresh_install(tmp_path)
    bundle = {
        "format": "mrln-bundle",
        "bundle_version": 1,
        "kind": "section",
        "slug": "good",
        "sections": {
            "good": {"items": [{"name": "a", "text": "fine"}]},
            "zbad": {"items": "not-a-list"},
        },
    }
    with pytest.raises(SchemaError):
        import_bundle(dest, bundle)
    assert dest.tier_of("sections", "good") == ""  # the valid file was NOT written


def test_import_rejects_foreign_files(lib):
    with pytest.raises(SchemaError, match="format marker"):
        import_bundle(lib, {"kind": "template"})
    with pytest.raises(SchemaError, match="bundle_version"):
        import_bundle(lib, {"format": "mrln-bundle", "bundle_version": 99, "kind": "template"})
    with pytest.raises(SchemaError, match="bundle kind"):
        import_bundle(lib, {"format": "mrln-bundle", "bundle_version": 1, "kind": "profile"})


def test_import_reports_missing_factory(lib, tmp_path):
    dest = fresh_install(tmp_path)
    bundle = {
        "format": "mrln-bundle",
        "bundle_version": 1,
        "kind": "section",
        "slug": "mood",
        "sections": {"mood": {"items": [{"name": "epic", "text": "epic composition"}]}},
        "factory_refs": ["lighting", "ghost/section"],
    }
    report = import_bundle(dest, bundle)
    assert report["missing_factory"] == ["ghost/section"]


# -- endpoints ---------------------------------------------------------------


def ok(result):
    status, body = result
    assert status == 200, body
    return body


def test_handle_export_and_import_roundtrip(lib, tmp_path):
    bundle = ok(promptapi.handle_export(lib, {"kind": "template", "slug": "share-me"}))
    assert bundle["format"] == "mrln-bundle"
    status, _ = promptapi.handle_export(lib, {"kind": "profiles", "slug": "x"})
    assert status == 400
    dest = fresh_install(tmp_path)
    report = ok(promptapi.handle_import(dest, {"bundle": bundle, "dry_run": True}))
    assert report["dry_run"] is True and "fingerprint" in report
    status, _ = promptapi.handle_import(dest, {"bundle": "nope"})
    assert status == 400
