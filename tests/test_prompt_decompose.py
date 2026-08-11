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
    with pytest.raises(SelectionError, match="Composer API"):
        decompose(lib, "x", engine="llm")
    with pytest.raises(SelectionError, match="Composer API"):
        decompose(lib, "x", engine="hybrid")
    with pytest.raises(SelectionError, match="unknown engine"):
        decompose(lib, "x", engine="gpt")
    with pytest.raises(SelectionError, match="nothing to decompose"):
        decompose(lib, "   ")
    # the pre-rename alias keeps working
    assert decompose(lib, "bright red", engine="heuristic")["engine"] == "programmatic"


def test_api_endpoint(lib):
    status, body = promptapi.handle_decompose(
        lib, {"prompt": "bright red\nmoonlit night", "type": "object, car"}
    )
    assert status == 200
    assert body["matched"] == 2 and body["engine"] == "programmatic"
    status, body = promptapi.handle_decompose(lib, {"prompt": "x", "engine": "gpt"})
    assert status == 400 and "unknown engine" in body["error"]
    status, _ = promptapi.handle_decompose(lib, {})
    assert status == 400
    status, body = promptapi.handle_decompose(lib, {"prompt": "x", "engine": "llm", "timeout": 3})
    assert status == 400 and "timeout" in body["error"]


# -- llm / hybrid engines ------------------------------------------------------


def test_score_match(lib):
    from mrln.promptlib import score_match

    assert score_match(lib, "bright red", "color", "red") == 1.0
    assert score_match(lib, "completely unrelated words", "color", "red") == 0.0
    assert score_match(lib, "x", "color", "ghost") is None  # unknown item
    assert score_match(lib, "x", "no-such-section", "red") is None


def test_llm_engine_falls_back_to_programmatic(lib, tmp_path):
    import json

    (tmp_path / "user").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user" / "settings.json").write_text(
        json.dumps({"llm": {"ollama_url": "http://127.0.0.1:9"}}), encoding="utf-8"
    )
    for engine in ("llm", "hybrid"):
        status, body = promptapi.handle_decompose(
            lib, {"prompt": "bright red", "engine": engine, "backend": "ollama", "model": "m"}
        )
        assert status == 200
        assert body["engine"] == "programmatic"  # honest fallback, render alive
        assert "llm_error" in body and body["fragments"]


def test_validate_llm_fragments(lib):
    raw = [
        {"text": "bright red", "section": "color", "item": "red"},
        {"text": "junk words", "section": "color", "item": "ghost"},
        {"text": "plain prose", "section": None, "item": None},
        {"text": "", "section": "color", "item": "red"},  # dropped: empty
        "garbage",  # dropped: not a dict
    ]
    validated = promptapi._validate_llm_fragments(lib, raw)
    assert len(validated) == 3
    assert validated[0]["match"] == {"section": "color", "item": "red", "score": 1.0}
    assert validated[1]["match"] is None  # invalid item demotes...
    assert validated[1]["suggestion"]["section"] == "color"  # ...to a suggestion
    assert validated[2]["match"] is None and "suggestion" not in validated[2]


def test_extract_json_tolerance():
    assert promptapi._extract_json('noise {"a": 1} trailing')["a"] == 1
    assert promptapi._extract_json('<think>{bad}</think>```json\n{"a": 2}\n```')["a"] == 2
    with pytest.raises(RuntimeError, match="no JSON"):
        promptapi._extract_json("nothing here")


def test_decompose_catalog_shrinks(lib):
    full = promptapi._decompose_catalog(lib, ())
    assert any(line.startswith("color: ") and "red" in line for line in full.splitlines())
    tiny = promptapi._decompose_catalog(lib, (), budget=20)
    assert tiny and all(line.endswith("…") for line in tiny.splitlines())
    assert "bright red" not in tiny
