"""Coverage lint for the shipped factory library, from the USER's side.

Every other content test asks whether what we shipped is well-formed. This one
asks whether it is *there* — by typing plain-language intents into the same
search the composer's picker uses and failing on anything that comes back
empty.

It exists because coverage was a judgement call and that judgement had a
measurable bias: 214 sections and 2934 items, with twenty location sections a
camera crew shoots at and none where more than one person is having a good
time. wardrobe/historical shipped a gold-lame disco jumpsuit and there was
nowhere in the library to wear it. Every one of those was found by a user
typing a word and getting nothing.

The probe list is tests/intent_probes.py — see its docstring for the rules on
editing it. The short version: a gap a user reports gets APPENDED, never
removed to make this green.

`python _harness/tools/intent_probe.py` runs the same list as an authoring
loop, printing the misses grouped so they read as a work list.
"""

import pytest
import support
from intent_probes import KNOWN_GAPS, PROBES, all_probes

from mrln.promptapi import library
from mrln.promptlib import Library

FACTORY_ROOT = support.ROOT / "mrln" / "data" / "prompt"


@pytest.fixture(scope="module")
def lib():
    return Library(FACTORY_ROOT, None)


@pytest.mark.parametrize("group,term", all_probes(), ids=lambda v: v.replace(" ", "-"))
def test_a_plain_language_intent_finds_something(lib, group, term):
    hits = library.search_sections(lib, term, scope="both")
    assert hits, (
        f"nothing in the factory library answers '{term}' ({group}). This is a "
        f"content gap, not a test to delete — add the content, or move the probe "
        f"to intent_probes.KNOWN_GAPS with a reason it is out of scope."
    )


def test_the_probe_list_stays_honest():
    """Guards against the two ways this instrument gets quietly switched off."""
    terms = [term for terms in PROBES.values() for term in terms]
    assert len(terms) >= 300, "the probe list shrank — gaps are appended, never removed"
    assert len(terms) == len(set(terms)), "duplicate probes inflate the count without testing more"
    assert len(KNOWN_GAPS) <= 10, (
        "KNOWN_GAPS is the argued escape hatch; a long one means coverage is being "
        "declared rather than measured"
    )


def test_a_probe_that_cannot_fail_is_not_a_probe(lib):
    """A nonsense term must find nothing.

    Without this, a search that silently started matching everything would
    turn all 400 probes green and the suite would report perfect coverage of
    a library that had lost its content.
    """
    assert library.search_sections(lib, "zzqqxx", scope="both") == []
    assert library.search_sections(lib, "nightclub zzqqxx", scope="both") == []
