"""Text-domain nodes: MRLN_ShowText display + passthrough."""

import json

import pytest
import support


@pytest.fixture(scope="module")
def classes():
    return support.load_pack().NODE_CLASS_MAPPINGS


@pytest.fixture()
def node(classes):
    return classes["MRLN_ShowText"]()


def test_registered(classes):
    assert "MRLN_ShowText" in classes
    cls = classes["MRLN_ShowText"]
    assert cls.OUTPUT_NODE is True
    assert cls.CATEGORY.endswith("/text")


def test_wildcard_input_accepts_any_type(classes):
    cls = classes["MRLN_ShowText"]
    assert cls.INPUT_TYPES()["required"]["value"][0] == "*"
    assert cls.VALIDATE_INPUTS(input_types={"value": "IMAGE"}) is True


def test_string_passthrough(node):
    out = node.execute("hello prompt")
    assert out["result"] == ("hello prompt",)
    assert out["ui"]["text"] == ["hello prompt"]


def test_non_string_values_stringified(node):
    assert node.execute(42)["result"] == ("42",)
    obj = {"prompt": "x", "n": 1}
    text = node.execute(obj)["result"][0]
    assert json.loads(text) == obj
    assert "\n" in text  # pretty-printed


def test_every_widget_has_tooltip(classes):
    for group in classes["MRLN_ShowText"].INPUT_TYPES().values():
        for name, spec in group.items():
            assert len(spec) == 2 and "tooltip" in spec[1], name
