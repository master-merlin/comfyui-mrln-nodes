import pytest
import support  # noqa: F401

from mrln.promptlib import SchemaError, parse_section, parse_template

_DEFAULT_ITEMS = object()


def sec(items=_DEFAULT_ITEMS, **extra):
    if items is _DEFAULT_ITEMS:
        items = [{"name": "a", "text": "alpha"}]
    return {"items": items, **extra}


def test_section_roundtrip():
    section = parse_section(
        sec(
            items=[
                {
                    "name": "x",
                    "text": "tx",
                    "negative": "neg",
                    "weight": 2.5,
                    "data": {"hex": ["#fff"]},
                    "tags": ["t"],
                    "excludes": ["e"],
                    "requires": ["r"],
                }
            ],
            label="L",
            description="D",
            negative="N",
        ),
        "car/color",
        "f.json",
    )
    item = section.items[0]
    assert (section.label, section.description, section.negative) == ("L", "D", "N")
    assert (item.name, item.text, item.negative, item.weight) == ("x", "tx", "neg", 2.5)
    assert item.data == {"hex": ["#fff"]}
    assert (item.tags, item.excludes, item.requires) == (("t",), ("e",), ("r",))


def test_plain_string_item_normalized():
    section = parse_section(sec(items=["Foo Bar!"]), "s", "f.json")
    assert section.items[0].name == "foo-bar"
    assert section.items[0].text == "Foo Bar!"


def test_default_label_from_slug():
    section = parse_section(sec(), "location/urban-night", "f.json")
    assert section.label == "Urban Night"


def test_version_future_rejected():
    with pytest.raises(SchemaError, match="version"):
        parse_section(sec(version=2), "s", "f.json")
    parse_section(sec(version=1), "s", "f.json")  # explicit 1 fine
    parse_section(sec(), "s", "f.json")  # missing fine


@pytest.mark.parametrize(
    "data,match",
    [
        ([], "object"),
        (sec(items=[]), "items"),
        (sec(items=[{"text": ""}]), "text"),
        (sec(items=[{"name": "a", "text": "x"}, {"name": "a", "text": "y"}]), "duplicate item"),
        (sec(items=[{"name": "a", "text": "x", "weight": -1}]), "weight"),
    ],
)
def test_section_errors(data, match):
    with pytest.raises(SchemaError, match=match):
        parse_section(data, "s", "f.json")


def tpl(**kw):
    base = {"slots": [{"id": "a", "ref": "color"}]}
    base.update(kw)
    return base


def test_template_defaults():
    template = parse_template(tpl(), "car-shoot", "f.json")
    assert template.label == "Car Shoot"
    assert template.order == ("a",)
    assert template.render.format == "string"
    assert template.slots[0].default == "random"


def test_template_order_appends_variant():
    template = parse_template(
        tpl(order=["a"], variants=[{"name": "v", "slots": [{"id": "b", "ref": "color"}]}]),
        "t",
        "f.json",
    )
    assert template.order == ("a", "@variant")


@pytest.mark.parametrize(
    "data,match",
    [
        (tpl(slots=[{"id": "a", "ref": "c"}, {"id": "a", "ref": "c"}]), "duplicate slot"),
        (tpl(slots=[{"ref": "c"}]), "missing an 'id'"),
        (tpl(slots=[{"id": "a"}]), "missing a 'ref'"),
        (tpl(slots=[{"id": "variant", "ref": "c"}]), "reserved"),
        (tpl(slots=[{"id": "a/b", "ref": "c"}]), "reserved"),
        (tpl(variants=[{"name": "v", "slots": [{"id": "a", "ref": "c"}]}]), "collide"),
        (tpl(order=["nope"]), "unknown slot id"),
        (tpl(render={"format": "yaml"}), "unknown render format"),
        (tpl(slots=[{"id": "b", "ref": "c", "emphasis": 0}]), "emphasis"),
        (tpl(variant_default="missing", variants=[{"name": "v", "slots": []}]), "variant_default"),
        (
            tpl(variants=[{"name": "v", "slots": []}, {"name": "v", "slots": []}]),
            "duplicate variant",
        ),
    ],
)
def test_template_errors(data, match):
    with pytest.raises(SchemaError, match=match):
        parse_template(data, "t", "f.json")
