"""Nested randomness (item-level child slots) and short/long text variants."""

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library, factory_only_library

from mrln.promptlib import (
    RecursionLimitError,
    SelectionError,
    parse_section,
    parse_template,
    render,
    resolve_template,
)
from mrln.promptlib.serialize import dump_section


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def rt(lib, seed=0, selection=None, mode="as configured", text_length=None):
    tpl = lib.load_template("nested")
    return resolve_template(
        lib,
        tpl,
        seed=seed,
        mode=mode,
        selection=selection or {},
        variables={},
        text_length=text_length,
    )


def crew(resolved):
    return next(s for s in resolved.slots if s.id == "crew")


# -- nested resolution -------------------------------------------------------


def test_children_resolve_and_substitute(lib):
    resolved = rt(lib, seed=3)
    slot = crew(resolved)
    assert slot.item_name == "pair"
    assert [c.id for c in slot.children] == ["crew.left", "crew.right"]
    right = slot.children[1]
    assert right.item_name == "red" and right.random is False  # child default
    assert slot.text.endswith("and bright red paint")
    assert "{" not in slot.text  # placeholders fully substituted


def test_children_deterministic_and_independent(lib):
    a = rt(lib, seed=9)
    b = rt(lib, seed=9)
    assert crew(a).text == crew(b).text
    drawn = {crew(rt(lib, seed=s)).children[0].item_name for s in range(12)}
    assert len(drawn) > 1  # left child really rolls with the master seed


def test_dotted_selection_pins_child(lib):
    resolved = rt(lib, selection={"crew.left": "gold"})
    assert crew(resolved).children[0].item_name == "gold"
    assert "shimmering gold paint and" in crew(resolved).text


def test_dotted_selection_off_mutes_child(lib):
    resolved = rt(lib, selection={"crew.right": "off"})
    right = crew(resolved).children[1]
    assert right.item_name is None and right.random is False
    assert resolved.slots[0].text.endswith("and  paint")  # empty substitution


def test_dotted_selection_random_with_seed(lib):
    a = rt(lib, seed=1, selection={"crew.left": "random@77"})
    b = rt(lib, seed=2, selection={"crew.left": "random@77"})
    assert crew(a).children[0].item_name == crew(b).children[0].item_name


def test_unknown_nested_key_errors(lib):
    with pytest.raises(SelectionError, match="no such nested slot"):
        rt(lib, selection={"crew.bogus": "x"})
    with pytest.raises(SelectionError, match="no such nested slot"):
        rt(lib, selection={"crew.left": "gold", "crew": "solo"})  # solo has no children


def test_child_negative_and_choices_indent(tmp_path):
    lib = factory_only_library(tmp_path)  # factory blue carries a negative
    resolved = rt(lib, selection={"crew.left": "blue"})
    assert "muddy tones" in resolved.negative
    tpl = lib.load_template("nested")
    out = render(resolved, "string", tpl.render)
    assert "\n  crew.left: blue  [fixed]" in out.choices  # indented child line


def test_recursion_capped(lib):
    tpl = parse_template({"slots": [{"id": "l", "ref": "loop"}]}, "loop-tpl", "mem")
    with pytest.raises(RecursionLimitError):
        resolve_template(lib, tpl, seed=0, mode="as configured", selection={}, variables={})


def test_nested_roundtrip_serialization(lib):
    section = lib.load_section("crew")
    assert parse_section(dump_section(section), "crew", "mem") == section
    dumped = dump_section(section)
    pair = next(i for i in dumped["items"] if i["name"] == "pair")
    assert pair["slots"][1] == {"id": "right", "ref": "color", "default": "red"}
    assert pair["text_short"] == "two drivers"


# -- short/long text ---------------------------------------------------------


def test_text_length_short_and_fallback(lib):
    long = rt(lib, seed=3)
    short = rt(lib, seed=3, text_length="short")
    assert crew(long).text.startswith("a pair:")
    assert crew(short).text == "two drivers"
    # same draws either way — only the surfaced text changes
    assert crew(long).children[0].item_name == crew(short).children[0].item_name
    # items without text_short fall back to their long text (color items)
    assert crew(short).children[0].text  # child resolved normally


def test_text_length_template_default_and_validation(lib):
    tpl = parse_template(
        {
            "slots": [{"id": "c", "ref": "crew", "default": "solo"}],
            "render": {"text_length": "short"},
        },
        "short-tpl",
        "mem",
    )
    resolved = resolve_template(lib, tpl, seed=0, mode="as configured", selection={}, variables={})
    assert resolved.slots[0].text == "a driver"
    with pytest.raises(SelectionError, match="unknown text length"):
        rt(lib, text_length="medium")
