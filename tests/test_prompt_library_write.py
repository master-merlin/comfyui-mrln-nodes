"""User-tier write support: validate_slug, Library.save_user, delete_user."""

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library, factory_only_library

from mrln.promptlib import (
    SchemaError,
    SectionNotFoundError,
    TemplateNotFoundError,
    validate_slug,
)

GOOD_SECTION = {"items": [{"name": "petrol", "text": "dark petrol"}]}
GOOD_TEMPLATE = {"slots": [{"id": "paint", "ref": "color"}]}


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


@pytest.mark.parametrize("slug", ["a", "a/b", "car/color/paint", "a-b.c_d", "x2/y9"])
def test_validate_slug_accepts(slug):
    assert validate_slug(slug) == slug


@pytest.mark.parametrize(
    "slug",
    ["", None, "../x", "a/../b", "a\\b", "/abs", "A", "a//b", "a/", ".hidden", "a b", "a/.tmp"],
)
def test_validate_slug_rejects(slug):
    with pytest.raises(SchemaError):
        validate_slug(slug)


def test_save_user_new_section(lib):
    path = lib.save_user("sections", "car/trim", GOOD_SECTION)
    assert path.is_file()
    assert lib.tier_of("sections", "car/trim") == "user"
    assert [item.name for item in lib.load_section("car/trim").items] == ["petrol"]


def test_save_user_new_template(lib):
    lib.save_user("templates", "mine/tpl", GOOD_TEMPLATE)
    assert lib.load_template("mine/tpl").slots[0].ref == "color"


def test_save_user_overrides_factory_and_changes_fingerprint(lib):
    before = lib.fingerprint()
    assert lib.tier_of("sections", "lighting") == "factory"
    lib.save_user("sections", "lighting", GOOD_SECTION)
    assert lib.tier_of("sections", "lighting") == "user"
    assert [item.name for item in lib.load_section("lighting").items] == ["petrol"]
    assert lib.fingerprint() != before


def test_save_rejects_invalid_content(lib):
    with pytest.raises(SchemaError):
        lib.save_user("sections", "bad", {"items": []})
    assert "bad" not in lib.section_slugs()  # nothing was written
    with pytest.raises(SchemaError):
        lib.save_user("sections", "alsobad", "not a dict")
    with pytest.raises(SchemaError):
        lib.save_user("profiles", "x", GOOD_SECTION)  # unwritable kind


def test_save_rejects_traversal_slugs(lib):
    for slug in ("../escape", "a/../../b", "..", "a\\b"):
        with pytest.raises(SchemaError):
            lib.save_user("sections", slug, GOOD_SECTION)


def test_save_without_user_root_raises(tmp_path):
    lib = factory_only_library(tmp_path)
    with pytest.raises(SchemaError, match="no user library"):
        lib.save_user("sections", "x", GOOD_SECTION)


def test_save_is_atomic_and_leaves_no_tmp(lib):
    lib.save_user("sections", "car/trim", GOOD_SECTION)
    updated = {"items": [{"name": "bronze", "text": "satin bronze"}]}
    lib.save_user("sections", "car/trim", updated)
    assert [item.name for item in lib.load_section("car/trim").items] == ["bronze"]
    assert list(lib.user_root.rglob("*.tmp")) == []


def test_delete_user_plain(lib):
    lib.save_user("sections", "car/trim", GOOD_SECTION)
    reverted = lib.delete_user("sections", "car/trim")
    assert reverted is False
    assert "car/trim" not in lib.section_slugs()


def test_delete_user_reverts_to_factory(lib):
    # fixtures: user color.json overrides factory color.json
    assert lib.tier_of("sections", "color") == "user"
    reverted = lib.delete_user("sections", "color")
    assert reverted is True
    assert lib.tier_of("sections", "color") == "factory"
    assert [item.name for item in lib.load_section("color").items][-1] == "gold"


def test_delete_factory_only_raises(lib):
    with pytest.raises(SectionNotFoundError):
        lib.delete_user("sections", "lighting")
    with pytest.raises(TemplateNotFoundError):
        lib.delete_user("templates", "basic")
