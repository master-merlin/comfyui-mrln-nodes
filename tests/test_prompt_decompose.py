"""De-compose: pasted prompt -> fragments mapped against library items."""

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library

from mrln import promptapi
from mrln.promptlib import SelectionError, decompose


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def frags(report):
    return [(f["text"], f["match"]["item"] if f["match"] else None) for f in report["fragments"]]


def test_exact_item_lines_match(lib):
    report = decompose(lib, "shimmering gold\nalpine mountain pass\n")
    assert frags(report) == [
        ("shimmering gold", "gold"),
        ("alpine mountain pass", "alpine-pass"),
    ]
    assert report["matched"] == 2 and report["unmatched"] == 0
    assert report["fragments"][0]["match"]["section"] == "color"


def test_labeled_line_and_emphasis_strip(lib):
    report = decompose(lib, "Place: (Shibuya Crossing:1.2)")
    assert frags(report) == [("(Shibuya Crossing:1.2)", "shibuya")]


def test_comma_pieces_split_matched_and_residue(lib):
    text = "epic cinematic masterpiece, deep green, ultra sharp focus"
    report = decompose(lib, text)
    assert ("deep green", "green") in frags(report)
    residues = [f["text"] for f in report["fragments"] if not f["match"]]
    assert "epic cinematic masterpiece" in residues
    assert "ultra sharp focus" in residues


def test_residue_carries_nearest_section_suggestion(lib):
    report = decompose(lib, "glittering gold accents everywhere")
    fragment = report["fragments"][0]
    assert fragment["match"] is None
    assert fragment["suggestion"]["section"] == "color"  # nearest by tokens


def test_type_filter_narrows_candidates(tmp_path):
    lib = build_library(tmp_path)
    # 'mood' has no suits -> universal; sections suited elsewhere would drop
    report = decompose(lib, "epic composition", template_type=("object",))
    assert frags(report) == [("epic composition", "epic")]


def test_engine_validation(lib):
    with pytest.raises(SelectionError, match="not wired up yet"):
        decompose(lib, "x", engine="ollama")
    with pytest.raises(SelectionError, match="unknown engine"):
        decompose(lib, "x", engine="gpt")
    with pytest.raises(SelectionError, match="nothing to decompose"):
        decompose(lib, "   ")


def test_api_endpoint(lib):
    status, body = promptapi.handle_decompose(
        lib, {"prompt": "bright red\nmoonlit night", "type": "object, car"}
    )
    assert status == 200
    assert body["matched"] == 2 and body["engine"] == "heuristic"
    status, body = promptapi.handle_decompose(lib, {"prompt": "x", "engine": "ollama"})
    assert status == 400 and "not wired up yet" in body["error"]
    status, _ = promptapi.handle_decompose(lib, {})
    assert status == 400
