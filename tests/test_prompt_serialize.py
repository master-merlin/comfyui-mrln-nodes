"""serialize.dump_* is the exact inverse of schema.parse_* — round-trip,
minimality, and idempotence guarantees."""

from pathlib import Path

import pytest
import support
from promptlib_fixtures import build_library

from mrln.promptlib import Library, parse_section, parse_template
from mrln.promptlib.serialize import dump_section, dump_template

FACTORY_ROOT = Path(support.ROOT) / "mrln" / "data" / "prompt"


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def test_section_roundtrip_fixtures(lib):
    for slug in lib.section_slugs():
        section = lib.load_section(slug)
        assert parse_section(dump_section(section), slug, "mem") == section


def test_template_roundtrip_fixtures(lib):
    for slug in lib.template_slugs():
        template = lib.load_template(slug)
        assert parse_template(dump_template(template), slug, "mem") == template


def test_factory_content_roundtrips():
    factory = Library(FACTORY_ROOT, None)
    for slug in factory.section_slugs():
        section = factory.load_section(slug)
        assert parse_section(dump_section(section), slug, "mem") == section
    for slug in factory.template_slugs():
        template = factory.load_template(slug)
        assert parse_template(dump_template(template), slug, "mem") == template


def test_minimal_section_dump_is_minimal():
    section = parse_section({"items": ["bright red"]}, "color", "mem")
    dumped = dump_section(section)
    assert set(dumped) == {"version", "label", "items"}
    assert dumped["items"] == [{"name": "bright-red", "text": "bright red"}]


def test_minimal_template_dump_is_minimal():
    template = parse_template({"slots": [{"id": "paint", "ref": "color"}]}, "tpl", "mem")
    dumped = dump_template(template)
    assert set(dumped) == {"version", "label", "slots"}
    assert dumped["slots"] == [{"id": "paint", "ref": "color"}]


def test_order_omitted_only_when_synthesized(lib):
    basic = dump_template(lib.load_template("basic"))  # file order == synthesized
    assert "order" not in basic
    varianted = dump_template(lib.load_template("varianted"))  # explicit custom order
    assert varianted["order"] == ["@variant", "paint"]


def test_render_omitted_when_default(lib):
    basic = dump_template(lib.load_template("basic"))  # string/", " == defaults
    assert "render" not in basic
    varianted = dump_template(lib.load_template("varianted"))
    assert varianted["render"] == {"format": "string_labeled"}


def test_slot_flags_survive(lib):
    dumped = dump_template(lib.load_template("basic"))
    extra = next(slot for slot in dumped["slots"] if slot["id"] == "extra")
    assert extra == {
        "id": "extra",
        "ref": "color",
        "allow_empty": True,
        "empty_weight": 100.0,
        "emphasis": 1.3,
    }


def test_dump_is_idempotent(lib):
    for slug in lib.section_slugs():
        once = dump_section(lib.load_section(slug))
        assert dump_section(parse_section(once, slug, "mem")) == once
    for slug in lib.template_slugs():
        once = dump_template(lib.load_template(slug))
        assert dump_template(parse_template(once, slug, "mem")) == once
