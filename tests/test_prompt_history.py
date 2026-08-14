"""Generation history (SPEC 6.2): the record the Prompt Template node writes,
the settings that gate it, the read/clear endpoints, and the one property that
makes any of it safe.

Two claims carry this feature, and everything else here supports them:

* THE ACCEPTANCE CRITERION — a record read back off disk, fed to
  `pl.compose()`, reproduces the render it describes byte for byte. Without
  that round trip history is a log, not a feature, so it is asserted at both
  layers: on the handler side (`test_a_recorded_render_recomposes_...`) and
  end to end through the node's own execute(), for a single render, an
  increment-seed batch and a combinatorial batch.
* A HISTORY FAILURE CAN NEVER BREAK A RENDER — the outputs are complete before
  the write is attempted, so every plausible failure (unwritable directory,
  raising settings read, raising record builder, a promptapi import that fails)
  is forced here and asserted to leave the render's six outputs identical to a
  control run.

Storage itself (rotation, the cursor, malformed lines, append-never-raises)
belongs to `promptlib/store.py` and is pinned in `tests/test_store.py`; this
file only tests what THIS layer adds on top of it.
"""

import importlib
import json
import logging
import sys

import pytest
import support
from promptlib_fixtures import build_library, factory_only_library

from mrln import promptlib as pl
from mrln.promptapi import history
from mrln.promptlib import store

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def write_settings(lib, **values):
    lib.user_root.mkdir(parents=True, exist_ok=True)
    (lib.user_root / "settings.json").write_text(json.dumps(values), encoding="utf-8")


def sample_record(**over):
    """A record exactly as the node builds one (no ts — record_renders stamps
    it), for the template the shared promptlib fixture ships."""
    fields = {
        "template": "basic",
        "profile": pl.STANDARD,
        "seed": 4242,
        "mode": "as configured",
        "selection": {"paint": "gold", "lighting": "random"},
        "variables": {"trigger": "roadster"},
        "format": "template default",
        "text_length": "template default",
        "conflict_policy": "negative prevails",
        "positive": "(filled in below)",
        "negative": "",
        "choices": "",
        "loras": [],
    }
    fields.update(over)
    return history.render_record(**fields)


def recompose(lib, record):
    """Replay a record through the engine — exactly what the History tab's
    'restore' does. Reads ONLY history.RESTORE_FIELDS, by name, so a record
    that is missing any input compose() needs fails right here."""
    payload = {field: record[field] for field in history.RESTORE_FIELDS}
    tpl = lib.load_template(payload.pop("template"))
    return pl.compose(lib, tpl, **payload)


def rendered_record(lib, **over):
    """A record whose positive/negative/choices really are what the engine
    renders for its own restore fields — the honest starting point for a round
    trip."""
    record = sample_record(**over)
    composed = recompose(lib, record)
    out = composed.rendered
    record.update(positive=out.positive, negative=out.negative, choices=out.choices)
    return record


@pytest.fixture()
def lib(tmp_path):
    return build_library(tmp_path)


def ok(result):
    status, body = result
    assert status == 200, body
    return body


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


def test_record_renders_round_trips_through_the_store(lib):
    written = history.record_renders(lib, [sample_record(positive="a"), sample_record(seed=9)])
    assert written == 2
    records = store.history_read(lib)
    assert len(records) == 2
    assert [r["seed"] for r in records] == [9, 4242]  # newest first
    assert records[1]["positive"] == "a"
    assert records[0]["selection"] == {"paint": "gold", "lighting": "random"}
    assert records[0]["variables"] == {"trigger": "roadster"}
    # user tier only: nothing in the repo may ship or receive history
    assert not list(lib.factory_root.rglob("*.jsonl"))


def test_record_fields_are_a_closed_set(lib):
    """The no-secrets property is structural: a record is assembled field by
    field from render outputs through render_record()'s keyword list, so the
    set of things a line can contain is fixed and reviewable — RECORD_FIELDS
    IS that list. A single render also carries no batch block at all."""
    history.record_renders(lib, [sample_record()])
    (record,) = store.history_read(lib)
    assert set(record) <= set(history.RECORD_FIELDS)
    assert set(history.RESTORE_FIELDS) <= set(record)
    assert "batch" not in record
    assert next(iter(record)) == "ts"  # first on the line a human tails
    assert set(history.RECORD_FIELDS) - set(record) == {"batch"}


def test_batch_timestamps_are_distinct_and_ordered(lib):
    """store.history_read's `before` cursor is a lexicographic ts compare, so
    two records sharing a ts would make a page boundary skip one. A batch is
    stamped from one clock read plus one microsecond per item: same second (the
    History tab groups on it), strictly increasing, fixed width."""
    history.record_renders(lib, [sample_record(seed=i) for i in range(5)])
    stamps = [r["ts"] for r in store.history_read(lib)]
    assert len(set(stamps)) == 5
    assert stamps == sorted(stamps, reverse=True)
    assert all(len(s) == len(stamps[0]) for s in stamps)  # microseconds never elided
    assert len({s[:19] for s in stamps}) == 1  # one queue click, one second


def test_records_are_not_mutated_by_the_write(lib):
    record = sample_record()
    history.record_renders(lib, [record])
    assert "ts" not in record  # the stamp goes on a copy


# ---------------------------------------------------------------------------
# the acceptance criterion: restore
# ---------------------------------------------------------------------------


def test_a_recorded_render_recomposes_byte_for_byte(lib):
    """THE criterion. Append, read back off disk, feed the record's restore
    fields to compose() — positive, negative and the choices report must all
    come back identical."""
    history.record_renders(lib, [rendered_record(lib)])
    (record,) = store.history_read(lib)
    out = recompose(lib, record).rendered
    assert out.positive == record["positive"]
    assert out.negative == record["negative"]
    assert out.choices == record["choices"]
    assert "roadster" in out.positive  # the trigger really did travel


def test_a_random_slot_survives_the_round_trip(lib):
    """The interesting half: 'lighting=random' is a seeded draw, so the round
    trip only holds because the seed AND the mode are on the record."""
    record = rendered_record(lib, selection={"lighting": "random"}, mode="randomize all")
    history.record_renders(lib, [record])
    (read_back,) = store.history_read(lib)
    assert recompose(lib, read_back).rendered.positive == record["positive"]
    # a different seed genuinely renders something else, so the assertion above
    # is not vacuous
    other = recompose(lib, {**read_back, "seed": read_back["seed"] + 1})
    assert other.rendered.positive != record["positive"]


def test_the_spec_field_list_alone_cannot_restore_a_render(lib):
    """Why the record carries three fields SPEC 6.2 does not list. Drop
    `variables` and the same template renders a different prompt (the trigger
    falls back to its declared default), so a record without it would restore
    something the user never rendered."""
    record = rendered_record(lib)
    stripped = recompose(lib, {**record, "variables": {}})
    assert stripped.rendered.positive != record["positive"]
    assert "roadster" not in stripped.rendered.positive
    # same for text_length: 'short' renders different item texts from the very
    # same draw, so a record without it restores the wrong prompt
    base = rendered_record(lib, template="nested", selection={})
    assert recompose(lib, {**base, "text_length": "short"}).rendered.positive != base["positive"]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_history_settings_defaults(lib):
    assert history.history_settings({}) == {"history_enabled": True, "history_months": 12}
    assert history.history_months({}) == history.DEFAULT_HISTORY_MONTHS


@pytest.mark.parametrize(
    ("value", "expected"),
    [(False, False), (True, True), ("false", False), ("off", False), (0, False), ("yes", True)],
)
def test_history_enabled_is_tolerant_of_a_hand_edited_file(value, expected):
    assert history.history_enabled({"history_enabled": value}) is expected


@pytest.mark.parametrize("value", ["nonsense", None, [], {}])
def test_history_months_falls_back_on_junk(value):
    assert history.history_months({"history_months": value}) == 12


def test_history_months_accepts_a_string_number():
    assert history.history_months({"history_months": "3"}) == 3


def test_disabled_history_writes_nothing(lib):
    write_settings(lib, history_enabled=False)
    assert history.record_renders(lib, [sample_record()]) == 0
    assert store.history_files(lib) == []
    assert not (lib.user_root / "history").exists()


def test_the_endpoint_reports_the_settings(lib):
    write_settings(lib, history_enabled=False, history_months=3)
    body = ok(history.handle_history(lib, {}))
    assert body["history_enabled"] is False
    assert body["history_months"] == 3


# ---------------------------------------------------------------------------
# pruning (runs from the boot warm-up thread)
# ---------------------------------------------------------------------------


def test_prune_history_keeps_the_configured_months(lib):
    # explicit ts values: rotation is per the record's OWN month
    for month in ("06", "07", "08"):
        store.history_append(lib, sample_record() | {"ts": f"2026-{month}-05T10:00:00.000000"})
    assert len(store.history_files(lib)) == 3
    write_settings(lib, history_months=2)
    history.prune_history(lib)
    assert [p.name for p in store.history_files(lib)] == [
        "render-202608.jsonl",
        "render-202607.jsonl",
    ]


def test_prune_history_keeps_a_year_by_default(lib):
    for month in ("07", "08"):
        store.history_append(lib, sample_record() | {"ts": f"2026-{month}-05T10:00:00.000000"})
    history.prune_history(lib)  # no settings.json at all
    assert len(store.history_files(lib)) == 2


def test_prune_history_prunes_even_when_recording_is_off(lib):
    """Retention is about what is KEPT, not about whether new lines are
    written — turning recording off must not freeze old months in place."""
    for month in ("07", "08"):
        store.history_append(lib, sample_record() | {"ts": f"2026-{month}-05T10:00:00.000000"})
    write_settings(lib, history_enabled=False, history_months=1)
    history.prune_history(lib)
    assert [p.name for p in store.history_files(lib)] == ["render-202608.jsonl"]


def test_prune_history_never_raises(tmp_path, lib):
    write_settings(lib, history_months="nonsense")
    history.prune_history(lib)  # junk setting
    history.prune_history(factory_only_library(tmp_path))  # no user tier
    blocker = lib.user_root / "history"
    blocker.write_text("not a directory", encoding="utf-8")
    history.prune_history(lib)
    assert blocker.read_text(encoding="utf-8") == "not a directory"


# ---------------------------------------------------------------------------
# record_renders never raises
# ---------------------------------------------------------------------------


def test_record_renders_never_raises(tmp_path, lib, caplog, monkeypatch):
    blocker = lib.user_root / "history"  # a FILE where the directory belongs
    blocker.write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert history.record_renders(lib, [sample_record()]) == 1  # store swallowed the IO error
    assert "history append skipped" in caplog.text
    assert store.history_read(lib) == []
    assert history.record_renders(factory_only_library(tmp_path), [sample_record()]) == 1
    assert history.record_renders(lib, []) == 0
    # a settings.json that cannot even be consulted must not raise either
    monkeypatch.setattr(history, "_read_settings", lambda _lib: 1 / 0)
    with caplog.at_level(logging.WARNING):
        assert history.record_renders(lib, [sample_record()]) == 0
    assert "history not recorded" in caplog.text


# ---------------------------------------------------------------------------
# GET /mrln/prompt/history
# ---------------------------------------------------------------------------


def test_handle_history_returns_newest_first_with_a_cursor(lib):
    history.record_renders(lib, [sample_record(seed=i) for i in range(3)])
    body = ok(history.handle_history(lib, {}))
    assert [r["seed"] for r in body["records"]] == [2, 1, 0]
    assert body["has_more"] is False
    assert body["next_before"] == ""
    assert body["limit"] == history.HISTORY_LIMIT_DEFAULT


def test_handle_history_pages_with_the_keyset_cursor(lib):
    history.record_renders(lib, [sample_record(seed=i) for i in range(5)])
    seen, cursor, pages = [], "", 0
    while True:
        body = ok(history.handle_history(lib, {"limit": "2", "before": cursor}))
        seen.extend(r["seed"] for r in body["records"])
        pages += 1
        if not body["has_more"]:
            assert body["next_before"] == ""
            break
        cursor = body["next_before"]
        assert cursor  # a page that promises more must say where to continue
    assert seen == [4, 3, 2, 1, 0]  # every record exactly once, newest first
    assert pages == 3


def test_handle_history_limit_is_bounded_and_tolerant(lib):
    history.record_renders(lib, [sample_record(seed=i) for i in range(3)])
    assert ok(history.handle_history(lib, {"limit": "abc"}))["limit"] == 100
    assert ok(history.handle_history(lib, {"limit": "99999"}))["limit"] == history.HISTORY_LIMIT_MAX
    assert ok(history.handle_history(lib, {"limit": "-4"}))["records"] == []
    body = ok(history.handle_history(lib, {"limit": 1}))
    assert len(body["records"]) == 1 and body["has_more"] is True


def test_handle_history_skips_malformed_lines(lib):
    history.record_renders(lib, [sample_record(seed=1)])
    (path,) = store.history_files(lib)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-08-12T12:00:00.000000", "template": "trunc\n')  # killed mid-write
        fh.write("not json at all\n")
    history.record_renders(lib, [sample_record(seed=2)])
    body = ok(history.handle_history(lib, {}))
    assert [r["seed"] for r in body["records"]] == [2, 1]


def test_handle_history_on_an_empty_install(lib):
    body = ok(history.handle_history(lib, {}))
    assert body["records"] == [] and body["has_more"] is False


# ---------------------------------------------------------------------------
# POST /mrln/prompt/history-clear
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"confirm": "true"}, {"confirm": 1}, {"confirm": "1"}])
def test_handle_history_clear_refuses_anything_but_json_true(lib, payload):
    """The codebase rule (same as the LoRA download's `start`): only JSON true
    confirms. GET query values are strings, so `?confirm=true` — a prefetched
    link, a stray cross-site GET — can never wipe a user's history."""
    history.record_renders(lib, [sample_record()])
    status, body = history.handle_history_clear(lib, payload)
    assert status == 400
    assert "confirm" in body["remediation"]
    assert len(store.history_read(lib)) == 1  # nothing was touched


def test_handle_history_clear_removes_every_month_file(lib):
    for month in ("07", "08"):
        store.history_append(lib, sample_record() | {"ts": f"2026-{month}-05T10:00:00.000000"})
    body = ok(history.handle_history_clear(lib, {"confirm": True}))
    assert body["ok"] is True and body["count"] == 2
    assert body["removed"] == ["render-202608.jsonl", "render-202607.jsonl"]
    assert body["failed"] == []
    assert store.history_files(lib) == []
    assert ok(history.handle_history(lib, {}))["records"] == []
    # clearing an already empty history is a no-op, not an error
    assert ok(history.handle_history_clear(lib, {"confirm": True}))["count"] == 0
    # and recording works again right afterwards
    assert history.record_renders(lib, [sample_record()]) == 1


# ---------------------------------------------------------------------------
# the node side
# ---------------------------------------------------------------------------

COLORS = [
    {"name": "red", "text": "bright red"},
    {"name": "green", "text": "deep green"},
    {"name": "blue", "text": "ocean blue"},
]
LIGHTS = [
    {"name": "day", "text": "bright daylight"},
    {"name": "night", "text": "moonlit night"},
]
KITS = [
    {"name": "stock", "text": "stock body"},
    {
        "name": "wide",
        "text": "WideBodyKit",
        "data": {"lora": "kits/wide.safetensors", "strength_model": 0.8},
    },
]


def _write(root, rel, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture()
def user_tier(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setenv("MRLN_PROMPT_DIR", str(user))
    _write(user, "sections/hist/color.json", {"label": "Color", "items": COLORS})
    _write(user, "sections/hist/light.json", {"label": "Light", "items": LIGHTS})
    _write(user, "sections/hist/kit.json", {"label": "Kit", "items": KITS})
    _write(
        user,
        "templates/hist/tiny.json",
        {
            "label": "Tiny",
            "prefix": "photo of a {trigger}",
            "slots": [
                {"id": "color", "ref": "hist/color", "default": "random"},
                {"id": "light", "ref": "hist/light", "default": "random"},
                {"id": "kit", "ref": "hist/kit", "default": "wide"},
            ],
            "render": {"format": "string", "joiner": ", "},
        },
    )
    return user


@pytest.fixture(scope="module")
def classes():
    return support.load_pack().NODE_CLASS_MAPPINGS


@pytest.fixture()
def node(classes):
    return classes["MRLN_PromptTemplate"]()


@pytest.fixture()
def node_history(classes):
    """The history module the LOADED PACK's node actually imports. ComfyUI (and
    support.load_pack) import the pack under its directory name, so
    `mrln.promptapi.history` and the node's copy are different module objects —
    monkeypatching the wrong one silently patches nothing."""
    node_mod = sys.modules[classes["MRLN_PromptTemplate"].__module__]
    return importlib.import_module(f"{node_mod.__package__.rsplit('.', 1)[0]}.promptapi.history")


def run(node, **kw):
    args = {
        "template": "hist/tiny",
        "selection": "",
        "selection_mode": "as configured",
        "seed": 7,
        "format": "template default",
        "trigger": "roadster",
    }
    args.update(kw)
    return node.execute(**args)


def records():
    """Every history line this install wrote, newest first."""
    return store.history_read(pl.open_library(), limit=1000)


def test_a_single_render_writes_one_restorable_line(node, user_tier):
    prompts, _llms, loras, negatives, choices, _gen = run(node)
    (record,) = records()
    assert record["template"] == "hist/tiny"
    assert (record["seed"], record["mode"]) == (7, "as configured")
    assert record["variables"] == {"trigger": "roadster"}
    assert record["positive"] == prompts[0]
    assert record["positive"].startswith("photo of a roadster, ")
    assert "WideBodyKit" in record["positive"]
    assert record["negative"] == negatives[0]
    assert record["choices"] == choices[0]
    assert record["loras"] == json.loads(loras[0])
    assert record["loras"][0]["lora"] == "kits/wide.safetensors"  # drawn blocks, as objects
    assert "batch" not in record
    # ... and it restores: the record alone reproduces the node's own render
    lib = pl.open_library()
    assert recompose(lib, record).rendered.positive == prompts[0]


def test_a_batch_writes_one_line_per_item_with_the_seed_it_used(node, user_tier):
    prompts, _neg, _ch, _lo, _llm, _gen = run(node, batch_count=4)
    rows = list(reversed(records()))  # oldest first == batch order
    assert len(rows) == 4
    assert [r["seed"] for r in rows] == [7, 8, 9, 10]
    assert [r["positive"] for r in rows] == prompts
    ids = {r["batch"]["id"] for r in rows}
    assert len(ids) == 1  # one queue click, one batch id: the tab collapses these
    assert [r["batch"]["index"] for r in rows] == [1, 2, 3, 4]
    assert {r["batch"]["total"] for r in rows} == {4}
    assert {r["batch"]["kind"] for r in rows} == {"increment seed"}
    # every item restores on its own — the point of one line per item
    lib = pl.open_library()
    for row in rows:
        assert recompose(lib, row).rendered.positive == row["positive"]
    # a second click is a second batch, not more of the first
    run(node, batch_count=2, seed=99)
    assert len({r["batch"]["id"] for r in records()}) == 2


def test_a_combinatorial_batch_records_the_pins_it_rendered_with(node, user_tier):
    prompts, _neg, _ch, _lo, _llm, _gen = run(
        node, batch_mode="combinatorial", selection_mode="randomize all"
    )
    rows = list(reversed(records()))
    assert len(rows) == len(prompts) == 12  # 3 colors x 2 lights x 2 kits
    assert {r["seed"] for r in rows} == {7}  # combinations share the master seed
    # the node renders enumerated combinations in 'as configured' with pins, so
    # that — not the widget's 'randomize all' — is what has to be recorded, or
    # a restore would re-roll instead of reproducing
    assert {r["mode"] for r in rows} == {"as configured"}
    assert rows[0]["selection"]["color"] and rows[0]["selection"]["light"]
    assert {r["batch"]["kind"] for r in rows} == {"combinatorial"}
    lib = pl.open_library()
    for row, prompt in zip(rows, prompts, strict=True):
        assert row["positive"] == prompt
        assert recompose(lib, row).rendered.positive == prompt


def test_history_disabled_leaves_the_render_untouched(node, user_tier):
    control = run(node)
    (user_tier / "settings.json").write_text('{"history_enabled": false}', encoding="utf-8")
    (user_tier / "history").rename(user_tier / "history-old")
    assert run(node) == control
    assert not (user_tier / "history").exists()


def test_the_node_added_no_widget_and_no_output(classes):
    """History is a global preference, so it is a SETTING, not a widget: a
    per-node widget would rewrite every saved workflow's positional
    widgets_values, and nothing about this feature belongs on the node face."""
    cls = classes["MRLN_PromptTemplate"]
    inputs = cls.INPUT_TYPES()
    assert list(inputs["optional"]) == ["variables", "profile", "batch_count", "batch_mode"]
    assert list(inputs["required"])[-1] == "conflict_policy"
    assert cls.RETURN_NAMES == ("prompt", "llm", "loras", "negative", "choices", "gen_info")
    assert cls.OUTPUT_IS_LIST == (True,) * 6


# ---------------------------------------------------------------------------
# the important one: a history failure can never break a render
# ---------------------------------------------------------------------------


def test_a_blocked_history_directory_does_not_break_a_render(node, user_tier, caplog):
    control = run(node)
    (user_tier / "history").rename(user_tier / "history-old")
    (user_tier / "history").write_text("not a directory", encoding="utf-8")  # e.g. read-only/full
    with caplog.at_level(logging.WARNING):
        assert run(node) == control
    assert "history append skipped" in caplog.text
    assert (user_tier / "history").read_text(encoding="utf-8") == "not a directory"


def test_a_raising_history_layer_does_not_break_a_render(
    node, user_tier, node_history, monkeypatch, caplog
):
    """The node's own guard, at every level it has to cover: the append call,
    the record builder, and the settings read underneath both. The render's six
    outputs must come out identical to a control run in all three cases."""
    control = run(node)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    for target in ("record_renders", "render_record", "_read_settings"):
        with monkeypatch.context() as patch:
            patch.setattr(node_history, target, boom)
            with caplog.at_level(logging.WARNING):
                assert run(node) == control, target


def test_a_failed_promptapi_import_does_not_break_a_render(
    node, user_tier, node_history, monkeypatch, caplog
):
    """The write reaches the settings layer through a lazy relative import. An
    install where that import fails (a partially unpacked update, a broken
    aiohttp) must still render."""
    control = run(node)
    monkeypatch.setitem(sys.modules, node_history.__name__, None)
    with caplog.at_level(logging.WARNING):
        assert run(node) == control
    assert "not written to history" in caplog.text


def test_a_secret_in_settings_never_reaches_a_record(node, user_tier):
    """API keys live in settings.json and travel to the client as booleans
    only. A history line is built from render outputs through a closed field
    list, so there is no path for one to leak into it — asserted both ways:
    the bytes are absent, and the field set stays closed."""
    secret = "civitai-key-do-not-log-4f2b9c"
    llm_secret = "sk-openai-do-not-log-77c1"
    (user_tier / "settings.json").write_text(
        json.dumps({"civitai_api_key": secret, "llm_api_keys": {"openai": llm_secret}}),
        encoding="utf-8",
    )
    run(node, batch_count=3)
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in store.history_files(pl.open_library())
    )
    assert text.strip()  # the render WAS recorded (a vacuous pass would be worthless)
    assert secret not in text
    assert llm_secret not in text
    for record in records():
        assert set(record) <= set(history.RECORD_FIELDS)
        assert "llm" not in record  # the llm wire (system prompt, params) is not recorded
