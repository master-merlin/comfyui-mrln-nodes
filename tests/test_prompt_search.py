"""The section picker's filter row (user request 2026-08-13).

210 sections is not browsable, and the thing a user is hunting is usually not
in a slug: looking for a disco, they reach for location/everyday and find
nothing, while the word actually lives inside wardrobe/historical's items. So
search covers item TEXT as well as names, and reports WHICH of the two matched.

It runs server-side because that is where the parsed library already is —
the client alternative is fetching every pool to filter it.
"""

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln.promptapi import library


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def slugs(results):
    return [r["slug"] for r in results]


def test_an_empty_query_returns_nothing_not_everything(lib):
    """210 sections IS the problem; answering with all of them is not help."""
    status, body = library.handle_search(lib, {"q": "   "})
    assert status == 200 and body["results"] == []


def test_a_term_matches_at_a_word_start_never_mid_word(lib, tmp_path):
    """Plain substring looked fine and was not: 'rain' hit terrain, grain and
    training, which is how a filter loses a user's trust."""
    lib.save_user(
        "sections",
        "weather/test",
        {
            "version": 1,
            "items": [
                {"name": "downpour", "text": "heavy rain sheeting across the road"},
                {"name": "hillside", "text": "broken terrain under a hard sun"},
            ],
        },
    )
    lib.invalidate()
    hits = library.search_sections(lib, "rain")
    names = [name for hit in hits for name in hit["samples"]]
    assert "downpour" in names
    assert "hillside" not in names


def test_every_term_must_hit_so_two_words_narrow(lib, tmp_path):
    lib.save_user(
        "sections",
        "weather/test",
        {
            "version": 1,
            "items": [
                {"name": "neon-street", "text": "a neon street after rain"},
                {"name": "quiet-street", "text": "an empty street at noon"},
                {"name": "neon-room", "text": "a neon interior"},
            ],
        },
    )
    lib.invalidate()
    hits = library.search_sections(lib, "neon street")
    samples = [name for hit in hits for name in hit["samples"]]
    assert samples == ["neon-street"]


def test_where_says_whether_the_name_or_the_content_matched(lib):
    """The useful half of the answer: a name hit is what you were looking for,
    an item hit is where it turned out to live."""
    lib.save_user("sections", "nightlife/club", {"version": 1, "items": [
        {"name": "mirrorball", "text": "a mirrorball over a full floor"},
    ]})
    lib.save_user("sections", "wardrobe/party", {"version": 1, "items": [
        {"name": "club-dress", "text": "a nightlife dress cut for movement"},
    ]})
    lib.invalidate()
    by_slug = {hit["slug"]: hit for hit in library.search_sections(lib, "nightlife")}
    assert by_slug["nightlife/club"]["where"] == ["name"]
    assert by_slug["wardrobe/party"]["where"] == ["item"]


def test_name_hits_sort_before_item_hits(lib):
    lib.save_user("sections", "nightlife/club", {"version": 1, "items": [
        {"name": "floor", "text": "a full floor"},
    ]})
    lib.save_user("sections", "wardrobe/party", {"version": 1, "items": [
        {"name": "dress", "text": "a nightlife dress"},
    ]})
    lib.invalidate()
    assert slugs(library.search_sections(lib, "nightlife"))[0] == "nightlife/club"


def test_scope_narrows_to_names_or_to_content(lib):
    lib.save_user("sections", "nightlife/club", {"version": 1, "items": [
        {"name": "floor", "text": "a full floor"},
    ]})
    lib.save_user("sections", "wardrobe/party", {"version": 1, "items": [
        {"name": "dress", "text": "a nightlife dress"},
    ]})
    lib.invalidate()
    assert slugs(library.search_sections(lib, "nightlife", scope="name")) == ["nightlife/club"]
    assert slugs(library.search_sections(lib, "nightlife", scope="text")) == ["wardrobe/party"]


def test_a_hidden_item_never_answers_a_search(lib):
    """A tombstone is not content — offering it as a reason to pick a section
    would send the user to something they cannot draw."""
    lib.save_user("sections", "wardrobe/party", {"version": 1, "items": [
        {"name": "retired", "text": "a nightlife dress", "hidden": True},
        {"name": "kept", "text": "a plain coat"},
    ]})
    lib.invalidate()
    assert slugs(library.search_sections(lib, "nightlife")) == []


def test_the_endpoint_caps_and_flags_truncation(lib):
    status, body = library.handle_search(lib, {"q": "a", "limit": 2})
    assert status == 200 and len(body["results"]) <= 2
    assert isinstance(body["truncated"], bool)
    assert body["scope"] == "both"


def test_an_unknown_scope_falls_back_to_both(lib):
    _status, body = library.handle_search(lib, {"q": "x", "scope": "sideways"})
    assert body["scope"] == "both"


def test_the_route_is_a_plain_get():
    from mrln import promptapi

    table = {(m, p): (h, reads) for m, p, h, reads in promptapi.ROUTES}
    assert table[("get", "/mrln/prompt/search")] == (promptapi.handle_search, False)
