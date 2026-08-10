import os

import pytest
import support  # noqa: F401
from mrln.promptlib import Library, SchemaError, SectionNotFoundError, default_roots
from promptlib_fixtures import build_library, build_roots, factory_only_library


def test_merged_slugs(tmp_path):
    lib = build_library(tmp_path)
    slugs = lib.section_slugs()
    assert "color" in slugs
    assert "location/urban" in slugs
    assert "mood" in slugs  # user-only


def test_section_folders(tmp_path):
    lib = build_library(tmp_path)
    assert lib.section_folders() == ["location"]


def test_user_overrides_factory(tmp_path):
    lib = build_library(tmp_path)
    section = lib.load_section("color")
    assert lib.tier_of("sections", "color") == "user"
    assert [i.name for i in section.items][-1] == "petrol"  # user version has 5 items
    assert lib.tier_of("sections", "lighting") == "factory"


def test_factory_only(tmp_path):
    lib = factory_only_library(tmp_path)
    assert "mood" not in lib.section_slugs()
    assert [i.name for i in lib.load_section("color").items] == ["red", "green", "blue", "gold"]


def test_scope_items_leaf_and_folder(tmp_path):
    lib = build_library(tmp_path)
    leaf = lib.scope_items("lighting")
    assert [q for q, _, _ in leaf] == ["daylight", "night"]
    folder = lib.scope_items("location")
    assert [q for q, _, _ in folder] == [
        "nature/alpine-pass",
        "nature/desert-road",
        "urban/shibuya",
        "urban/neon-alley",
    ]


def test_scope_missing(tmp_path):
    lib = build_library(tmp_path)
    with pytest.raises(SectionNotFoundError, match="nonexistent"):
        lib.scope_items("nonexistent")


def test_fingerprint_reacts(tmp_path):
    lib = build_library(tmp_path)
    before = lib.fingerprint()
    assert before == lib.fingerprint()  # stable
    target = tmp_path / "user" / "sections" / "color.json"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert lib.fingerprint() != before
    after_touch = lib.fingerprint()
    (tmp_path / "user" / "sections" / "new.json").write_text('{"items":["x"]}', encoding="utf-8")
    assert lib.fingerprint() != after_touch


def test_broken_json_raises_named(tmp_path):
    factory, user = build_roots(tmp_path)
    (user / "sections" / "broken.json").write_text("{not json", encoding="utf-8")
    lib = Library(factory, user)
    assert "broken" in lib.section_slugs()  # listed so the error is reachable
    with pytest.raises(SchemaError, match="invalid JSON"):
        lib.load_section("broken")


def test_env_override_user_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(tmp_path / "custom"))
    _, user = default_roots()
    assert user == tmp_path / "custom"


def test_ensure_user_dirs(tmp_path):
    lib = build_library(tmp_path)
    lib.ensure_user_dirs()
    for kind in ("sections", "templates", "profiles", "system_prompts"):
        assert (tmp_path / "user" / kind).is_dir()
