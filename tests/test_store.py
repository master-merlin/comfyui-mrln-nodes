"""Store facade: thumbnail path resolution (user shadows factory) and the
append-only render history. Fixtures are written next to the assertions, like
the rest of the promptlib suite."""

import json
import logging
from pathlib import Path

import pytest
import support  # noqa: F401
from promptlib_fixtures import build_library, factory_only_library

from mrln import promptapi
from mrln.promptlib import SchemaError, store


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def _write_thumb(root, kind, slug, data=b"RIFFwebp"):
    path = Path(root) / "thumbs" / kind / f"{slug}.webp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _append_all(lib, records):
    for record in records:
        store.history_append(lib, record)


# -- thumbnails --------------------------------------------------------------


def test_thumb_missing_in_both_tiers(lib):
    assert store.thumb_path(lib, "templates", "basic") is None
    assert store.has_thumb(lib, "templates", "basic") is False


def test_thumb_layout_and_factory_fallback(lib):
    factory = _write_thumb(lib.factory_root, "templates", "basic")
    assert factory == lib.factory_root / "thumbs" / "templates" / "basic.webp"
    assert store.thumb_path(lib, "templates", "basic") == factory
    assert store.has_thumb(lib, "templates", "basic") is True


def test_user_thumb_shadows_factory(lib):
    _write_thumb(lib.factory_root, "templates", "basic", b"factory")
    user = _write_thumb(lib.user_root, "templates", "basic", b"user")
    assert store.thumb_path(lib, "templates", "basic") == user
    # deleting the user thumb lets the factory one reappear — the D3 model
    user.unlink()
    lib.invalidate()
    assert store.thumb_path(lib, "templates", "basic").read_bytes() == b"factory"


def test_nested_slug_becomes_directories(lib):
    user = _write_thumb(lib.user_root, "sections", "location/urban")
    assert user == lib.user_root / "thumbs" / "sections" / "location" / "urban.webp"
    assert store.thumb_path(lib, "sections", "location/urban") == user
    target = store.user_thumb_target(lib, "sections", "location/urban")
    assert target == user


def test_user_thumb_target_never_points_at_factory(lib):
    target = store.user_thumb_target(lib, "sections", "color")
    assert target == lib.user_root / "thumbs" / "sections" / "color.webp"
    assert lib.factory_root not in target.parents
    assert not target.exists()  # pure path computation, no side effects


def test_user_thumb_target_needs_a_user_root(tmp_path):
    lib = factory_only_library(tmp_path)
    with pytest.raises(SchemaError, match="user library directory"):
        store.user_thumb_target(lib, "sections", "color")
    _write_thumb(lib.factory_root, "sections", "color")
    assert store.thumb_path(lib, "sections", "color") is not None  # reads still work


@pytest.mark.parametrize("slug", ["../../evil", "..", "/etc/passwd", "..\\evil", "a/../../b"])
def test_traversal_slugs_cannot_escape(lib, tmp_path, slug):
    with pytest.raises(SchemaError):
        store.thumb_path(lib, "sections", slug)
    with pytest.raises(SchemaError):
        store.user_thumb_target(lib, "sections", slug)
    assert store.has_thumb(lib, "sections", slug) is False  # predicate never raises
    assert list(tmp_path.glob("*.webp")) == []


def test_containment_check_survives_a_bypassed_validator(lib, monkeypatch):
    """Defense in depth: even a caller that skips validate_slug cannot write
    outside the tier — same belt-and-braces as Library.save_user."""
    monkeypatch.setattr(store, "validate_slug", lambda slug: slug)
    with pytest.raises(SchemaError, match="escapes"):
        store.user_thumb_target(lib, "sections", "../../evil")


def test_unknown_kind_is_rejected(lib):
    with pytest.raises(SchemaError, match="unknown thumbnail kind"):
        store.thumb_path(lib, "profiles", "color")
    assert store.has_thumb(lib, "profiles", "color") is False


def test_has_thumb_is_cached_and_cleared_by_invalidate(lib):
    assert store.has_thumb(lib, "templates", "basic") is False
    _write_thumb(lib.user_root, "templates", "basic")
    assert store.has_thumb(lib, "templates", "basic") is False  # memoized snapshot
    lib.invalidate()
    assert store.has_thumb(lib, "templates", "basic") is True


# -- history -----------------------------------------------------------------


def test_history_append_read_round_trip(lib):
    store.history_append(lib, {"ts": "2026-08-12T10:00:00", "template": "basic", "seed": 7})
    store.history_append(lib, {"ts": "2026-08-12T11:00:00", "template": "varianted", "seed": 8})
    records = store.history_read(lib)
    assert [r["template"] for r in records] == ["varianted", "basic"]  # newest first
    assert records[0]["seed"] == 8
    path = lib.user_root / "history" / "render-202608.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_history_append_stamps_a_timestamp_and_stays_one_line(lib):
    store.history_append(lib, {"template": "basic", "positive": "a\nb"})
    (record,) = store.history_read(lib)
    assert record["ts"][:2] == "20" and record["positive"] == "a\nb"
    (path,) = store.history_files(lib)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1  # newline escaped


def test_history_read_limit_and_before_cursor(lib):
    _append_all(lib, [{"ts": f"2026-08-12T10:0{i}:00", "seed": i} for i in range(5)])
    assert [r["seed"] for r in store.history_read(lib, limit=2)] == [4, 3]
    assert [r["seed"] for r in store.history_read(lib, before="2026-08-12T10:02:00")] == [1, 0]
    assert store.history_read(lib, limit=0) == []


def test_history_skips_malformed_lines(lib):
    store.history_append(lib, {"ts": "2026-08-12T10:00:00", "template": "basic"})
    path = lib.user_root / "history" / "render-202608.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-08-12T12:00:00", "template": "trunc\n')  # killed mid-write
        fh.write("\n")
        fh.write('"not an object"\n')
    store.history_append(lib, {"ts": "2026-08-12T13:00:00", "template": "later"})
    assert [r["template"] for r in store.history_read(lib)] == ["later", "basic"]


def test_history_rotates_per_month(lib):
    _append_all(
        lib,
        [
            {"ts": "2026-07-30T23:00:00", "template": "july"},
            {"ts": "2026-08-01T00:30:00", "template": "august"},
        ],
    )
    assert [p.name for p in store.history_files(lib)] == [
        "render-202608.jsonl",
        "render-202607.jsonl",
    ]
    assert [r["template"] for r in store.history_read(lib)] == ["august", "july"]


def test_history_files_ignores_strangers(lib):
    store.history_append(lib, {"ts": "2026-08-12T10:00:00"})
    directory = lib.user_root / "history"
    (directory / "notes.txt").write_text("hi", encoding="utf-8")
    (directory / "render-2026.jsonl").write_text("{}\n", encoding="utf-8")
    assert [p.name for p in store.history_files(lib)] == ["render-202608.jsonl"]


def test_history_prune_keeps_the_newest_months(lib):
    _append_all(lib, [{"ts": f"2026-0{m}-05T10:00:00", "seed": m} for m in (5, 6, 7, 8)])
    store.history_prune(lib, 2)
    assert [p.name for p in store.history_files(lib)] == [
        "render-202608.jsonl",
        "render-202607.jsonl",
    ]
    store.history_prune(lib, 0)  # a stray settings value must never wipe history
    store.history_prune(lib, "nonsense")
    assert len(store.history_files(lib)) == 2


def test_history_append_never_raises(lib, tmp_path, caplog):
    blocker = lib.user_root / "history"  # a FILE where the directory belongs
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        store.history_append(lib, {"template": "basic"})
    assert "history append skipped" in caplog.text
    assert blocker.read_text(encoding="utf-8") == "not a directory"
    assert store.history_read(lib) == []
    assert store.history_files(lib) == []


def test_history_append_never_raises_without_a_user_root(tmp_path):
    lib = factory_only_library(tmp_path)
    store.history_append(lib, {"template": "basic"})
    store.history_append(lib, "not a dict")
    store.history_prune(lib, 12)
    assert store.history_read(lib) == []
    assert not list(Path(lib.factory_root).rglob("*.jsonl"))  # factory tier untouched


def test_history_append_survives_an_unserializable_record(lib):
    store.history_append(lib, {"ts": "2026-08-12T10:00:00", "path": Path("x") / "y"})
    (record,) = store.history_read(lib)
    assert isinstance(record["path"], str)
    assert json.loads(json.dumps(record))  # the line really is JSON


# -- catalog fetch short-circuit ---------------------------------------------


def ok(result):
    status, body = result
    assert status == 200, body
    return body


def test_library_matching_fp_short_circuits(lib):
    fingerprint = lib.fingerprint()
    body = ok(promptapi.handle_library(lib, {"fp": fingerprint}))
    assert body == {"unchanged": True, "fingerprint": fingerprint}


def test_library_stale_fp_returns_the_payload(lib):
    body = ok(promptapi.handle_library(lib, {"fp": "deadbeef"}))
    assert "unchanged" not in body
    assert body["fingerprint"] == lib.fingerprint()
    assert {t["slug"] for t in body["templates"]} >= {"basic", "varianted"}


def test_library_without_fp_is_unchanged_behavior(lib):
    body = ok(promptapi.handle_library(lib, {}))
    assert "unchanged" not in body
    assert set(body) == {"fingerprint", "templates", "sections", "folders", "profiles"}
    assert body == ok(promptapi.handle_library(lib, {"fp": ""}))  # empty fp is no fp


def test_library_fp_expires_on_a_write(lib):
    fingerprint = lib.fingerprint()
    lib.save_user("sections", "mood", {"items": [{"name": "calm", "text": "calm mood"}]})
    body = ok(promptapi.handle_library(lib, {"fp": fingerprint}))
    assert "unchanged" not in body
    assert body["fingerprint"] != fingerprint
    assert ok(promptapi.handle_library(lib, {"fp": body["fingerprint"]}))["unchanged"] is True
