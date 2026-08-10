import pytest
import support  # noqa: F401

from mrln.promptlib import (
    RecursionLimitError,
    UnknownVariableError,
    WildcardSyntaxError,
)
from mrln.promptlib.seeding import derive_rng
from mrln.promptlib.textexpr import expand


def rng(seed=0, key="k"):
    return derive_rng(seed, key)


def test_plain_text_passthrough():
    assert expand("hello (world:1.2), ok", {}, rng()) == "hello (world:1.2), ok"


def test_escaped_braces():
    assert expand("a {{literal}} b", {}, rng()) == "a {literal} b"


def test_wildcard_deterministic():
    first = expand("{a|b|c}", {}, rng(seed=1))
    again = expand("{a|b|c}", {}, rng(seed=1))
    assert first == again
    assert first in {"a", "b", "c"}


def test_wildcard_varies_with_seed():
    picks = {expand("{a|b|c|d|e|f}", {}, rng(seed=s)) for s in range(30)}
    assert len(picks) > 1


def test_nested_wildcard():
    result = expand("{x{1|2}|y}", {}, rng(seed=3))
    assert result in {"x1", "x2", "y"}


def test_empty_alternative():
    results = {expand("{|opt}", {}, rng(seed=s)) for s in range(40)}
    assert results == {"", "opt"}


def test_variable_substitution():
    assert expand("a {name} c", {"name": "b"}, rng()) == "a b c"


def test_wildcard_inside_variable_value():
    result = expand("{v}", {"v": "{x|y}"}, rng(seed=2))
    assert result in {"x", "y"}


def test_variable_inside_wildcard():
    result = expand("{ {v}|z}", {"v": "q"}, rng(seed=5))
    assert result in {" q", "z"}


def test_unknown_variable():
    with pytest.raises(UnknownVariableError, match="nope"):
        expand("{nope}", {"known": "1"}, rng())


def test_unbalanced_braces():
    with pytest.raises(WildcardSyntaxError):
        expand("open {a|b", {}, rng())
    with pytest.raises(WildcardSyntaxError):
        expand("stray } here", {}, rng())


def test_recursion_limit():
    with pytest.raises(RecursionLimitError):
        expand("{a}", {"a": "{a}"}, rng())
