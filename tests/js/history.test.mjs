// Unit tests for web/js/composer/history.js — the History tab's PURE logic:
// the keyset paging transitions, record → row derivation, the restore payload
// built from a record, timestamp formatting and excerpt truncation.
//
// The DOM half of the module is not tested here (no jsdom, no npm — see
// tests/js/README.md); composer_modules.test.mjs already imports this file
// with document/window/app/api/fetch booby-trapped and scans it for top-level
// statements, so the hygiene rule is covered there.
//
// The fixtures below are the shape `promptapi/history.py::render_record`
// actually writes — objects for `selection` and `variables`, a `trigger` key
// living INSIDE `variables`, and an ISO-8601 LOCAL `ts` with microseconds.
// They exist so a server-side field rename fails here rather than silently
// restoring nothing.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_HISTORY_MONTHS,
  EXCERPT_MAX,
  batchOf,
  excerpt,
  currentPageRequest,
  firstPageRequest,
  formatStamp,
  groupRows,
  hasNextPage,
  hasPreviousPage,
  kvLines,
  nextPageRequest,
  pageLanded,
  pageNumber,
  previousPageRequest,
  restorePayload,
  retentionNote,
  retentionSettings,
  seedOf,
  seedSummary,
  selectionMap,
  splitVariables,
  toRow,
} from "../../web/js/composer/history.js";
import { parseKvLines } from "../../web/js/composer/util.js";

// ---------------------------------------------------------------------------
// fixtures — one line as the node writes it
// ---------------------------------------------------------------------------

/** A single (non-batch) render, every RECORD_FIELD present. */
function record(overrides = {}) {
  return {
    ts: "2026-08-12T18:07:31.123456",
    template: "photo/street-scene",
    profile: "sdxl",
    seed: 4242,
    mode: "as configured",
    selection: { subject: "lone-cyclist", light: "random@7", weather: "off", "subject.hat": "cap" },
    variables: { trigger: "mrln-style", city: "Lisbon" },
    format: "string",
    text_length: "short",
    conflict_policy: "positive prevails",
    positive: "a photo of a lone cyclist, wet cobblestones, low sun",
    negative: "blurry, watermark",
    choices: "subject = lone-cyclist (fixed)",
    loras: [{ lora: "style.safetensors", strength_model: 0.8 }],
    ...overrides,
  };
}

/** One item of a queue click that rendered `total` of them. */
function batchRecord(index, total, id = "b1c2d3e4f5a6", overrides = {}) {
  return record({
    ts: `2026-08-12T18:07:31.${String(100000 + index).padStart(6, "0")}`,
    seed: 4242 + index - 1,
    batch: { id, index, total, kind: "increment seed" },
    ...overrides,
  });
}

/** A page as `handle_history` answers it. */
function page(records, { has_more = false, next_before = "", ...rest } = {}) {
  return {
    records,
    limit: 25,
    before: "",
    next_before,
    has_more,
    history_enabled: true,
    history_months: 12,
    ...rest,
  };
}

// ---------------------------------------------------------------------------

describe("timestamp formatting", () => {
  test("an ISO local stamp with microseconds splits into date and time", () => {
    // shown VERBATIM: the server writes local time and the cursor compares
    // these strings lexicographically, so Date parsing would print a second,
    // different clock in the same tab
    const stamp = formatStamp("2026-08-12T18:07:31.123456");
    assert.equal(stamp.date, "2026-08-12");
    assert.equal(stamp.time, "18:07:31");
    assert.equal(stamp.full, "2026-08-12 18:07:31");
    assert.equal(stamp.raw, "2026-08-12T18:07:31.123456");
  });

  test("seconds-precision and space-separated stamps parse too", () => {
    // store.history_append stamps a record without a usable ts with
    // isoformat(timespec="seconds")
    assert.equal(formatStamp("2026-08-12T18:07:31").time, "18:07:31");
    assert.equal(formatStamp("2026-08-12 18:07:31.5").date, "2026-08-12");
  });

  test("garbage survives as raw text instead of throwing or showing NaN", () => {
    // a hand-edited line must not take the row down
    assert.deepEqual(formatStamp("not a date"), {
      raw: "not a date",
      date: "",
      time: "not a date",
      full: "not a date",
    });
    assert.deepEqual(formatStamp(undefined), { raw: "", date: "", time: "", full: "" });
  });
});

describe("excerpt truncation", () => {
  test("short text passes through, whitespace collapses to one line", () => {
    assert.equal(excerpt("a photo\nof   a cyclist"), "a photo of a cyclist");
    assert.equal(excerpt("   padded   "), "padded");
  });

  test("the result never exceeds max, ellipsis included", () => {
    const long = "x".repeat(500);
    assert.equal(excerpt(long, 10).length, 10);
    assert.equal(excerpt(long, 10), `${"x".repeat(9)}…`);
    assert.equal(excerpt(long).length, EXCERPT_MAX);
  });

  test("exactly max characters is not truncated", () => {
    const exact = "y".repeat(10);
    assert.equal(excerpt(exact, 10), exact);
    assert.equal(excerpt(`${exact}z`, 10), `${"y".repeat(9)}…`);
  });

  test("empty / missing text yields an empty string", () => {
    assert.equal(excerpt(undefined), "");
    assert.equal(excerpt(null), "");
    assert.equal(excerpt(""), "");
  });
});

describe("record → row", () => {
  test("a row carries what SPEC 6.2 puts on screen: time, template, seed, excerpt", () => {
    const row = toRow(record());
    assert.equal(row.time, "18:07:31");
    assert.equal(row.date, "2026-08-12");
    assert.equal(row.template, "photo/street-scene");
    assert.equal(row.seed, 4242);
    assert.equal(row.excerpt, "a photo of a lone cyclist, wet cobblestones, low sun");
    assert.equal(row.profile, "sdxl");
    assert.equal(row.negative, "blurry, watermark");
    assert.equal(row.batch, null);
    assert.equal(row.record.template, "photo/street-scene");
  });

  test("a record missing its template is labelled, never rendered as 'undefined'", () => {
    const row = toRow({ ts: "2026-08-12T18:07:31" });
    assert.equal(row.template, "(unknown template)");
    assert.equal(row.seed, 0);
    assert.equal(row.excerpt, "");
  });

  test("seeds are coerced to a non-negative integer", () => {
    assert.equal(seedOf({ seed: "17" }), 17);
    assert.equal(seedOf({ seed: 17.9 }), 17);
    assert.equal(seedOf({ seed: -5 }), 0);
    assert.equal(seedOf({ seed: "nonsense" }), 0);
    assert.equal(seedOf({}), 0);
  });

  test("batch metadata is read only when it names an id", () => {
    assert.deepEqual(batchOf(batchRecord(3, 8)), {
      id: "b1c2d3e4f5a6",
      index: 3,
      total: 8,
      kind: "increment seed",
    });
    assert.equal(batchOf(record()), null);
    assert.equal(batchOf({ batch: {} }), null);
    assert.equal(batchOf({ batch: "b1" }), null);
  });
});

describe("batch grouping", () => {
  test("one queue click collapses into one group, newest item first", () => {
    // the store reads a month file in reverse, so a batch arrives 8,7,…,1
    const records = [8, 7, 6, 5, 4, 3, 2, 1].map((i) => batchRecord(i, 8));
    const groups = groupRows(records);
    assert.equal(groups.length, 1);
    assert.equal(groups[0].rows.length, 8);
    assert.equal(groups[0].batch.total, 8);
    assert.equal(groups[0].rows[0].batch.index, 8);
  });

  test("single renders stay their own rows and never merge", () => {
    const groups = groupRows([record(), record({ ts: "2026-08-12T18:06:00.000000" })]);
    assert.equal(groups.length, 2);
    assert.equal(groups[0].batch, null);
  });

  test("two different clicks never merge, even back to back", () => {
    const groups = groupRows([
      batchRecord(2, 2, "second-click"),
      batchRecord(1, 2, "second-click"),
      batchRecord(2, 2, "first-click"),
      batchRecord(1, 2, "first-click"),
    ]);
    assert.equal(groups.length, 2);
    assert.deepEqual(
      groups.map((g) => g.batch.id),
      ["second-click", "first-click"]
    );
  });

  test("a batch split across a page boundary shows the part this page holds", () => {
    // paging cannot be undone by grouping: the group says "2 of 8"
    const groups = groupRows([batchRecord(8, 8), batchRecord(7, 8)]);
    assert.equal(groups.length, 1);
    assert.equal(groups[0].rows.length, 2);
    assert.equal(groups[0].batch.total, 8);
  });

  test("row keys are unique even when two lines share a timestamp", () => {
    const same = record();
    const keys = groupRows([same, { ...same }]).map((group) => group.key);
    assert.notEqual(keys[0], keys[1]);
  });

  test("an empty/absent page groups to nothing", () => {
    assert.deepEqual(groupRows([]), []);
    assert.deepEqual(groupRows(undefined), []);
  });

  test("seedSummary distinguishes an incrementing batch from a combinatorial one", () => {
    const incrementing = groupRows([3, 2, 1].map((i) => batchRecord(i, 3)));
    assert.equal(seedSummary(incrementing[0].rows), "seeds 4242–4244");
    const combinatorial = groupRows(
      [3, 2, 1].map((i) => batchRecord(i, 3, "combo", { seed: 99, batch: { id: "combo", index: i, total: 3, kind: "combinatorial" } }))
    );
    assert.equal(seedSummary(combinatorial[0].rows), "seed 99");
    assert.equal(seedSummary([]), "");
  });
});

describe("selection: the disk shape vs what applyKvToRows wants", () => {
  test("the stored selection is a MAP and stays one — never re-parsed as lines", () => {
    // render_record writes {str(k): str(v)} — the dict compose() consumed.
    // applyKvToRows takes exactly that map; feeding it a joined string (or
    // feeding parseKvLines an object) restores NOTHING, silently.
    const map = selectionMap(record().selection);
    assert.deepEqual(map, {
      subject: "lone-cyclist",
      light: "random@7",
      weather: "off",
      "subject.hat": "cap",
    });
  });

  test("values are coerced to strings — applyKvToRows calls .trim() on them", () => {
    assert.deepEqual(selectionMap({ subject: 3, style: null }), { subject: "3", style: "" });
  });

  test("an absent selection is an empty map, not a crash", () => {
    assert.deepEqual(selectionMap(undefined), {});
    assert.deepEqual(selectionMap(null), {});
  });

  test("a line-shaped selection is still accepted (robustness, never produced)", () => {
    assert.deepEqual(selectionMap("subject=cyclist\nlight=random"), {
      subject: "cyclist",
      light: "random",
    });
  });
});

describe("variables: trigger rides inside them on disk, beside them in state", () => {
  test("trigger is lifted out and the rest becomes kv LINES", () => {
    // the node merges variable_map["trigger"] = trigger before composing; the
    // panel keeps state.trigger and state.variables apart and /preview
    // re-merges them the same way
    assert.deepEqual(splitVariables({ trigger: "mrln-style", city: "Lisbon" }), {
      trigger: "mrln-style",
      variables: "city=Lisbon",
    });
  });

  test("no trigger means an empty trigger field, not the string 'undefined'", () => {
    assert.deepEqual(splitVariables({ city: "Lisbon", mood: "calm" }), {
      trigger: "",
      variables: "city=Lisbon\nmood=calm",
    });
    assert.deepEqual(splitVariables(undefined), { trigger: "", variables: "" });
    assert.deepEqual(splitVariables({}), { trigger: "", variables: "" });
  });

  test("kvLines is the exact inverse of util.parseKvLines", () => {
    const map = { city: "Lisbon", mood: "calm and wet" };
    assert.deepEqual(parseKvLines(kvLines(map)), map);
    assert.equal(kvLines(undefined), "");
  });
});

describe("restore payload", () => {
  test("a full record maps onto the panel's state names", () => {
    assert.deepEqual(restorePayload(record()), {
      template: "photo/street-scene",
      profile: "sdxl",
      seed: 4242,
      mode: "as configured",
      selection: {
        subject: "lone-cyclist",
        light: "random@7",
        weather: "off",
        "subject.hat": "cap",
      },
      variables: "city=Lisbon",
      trigger: "mrln-style",
      format: "string",
      textLength: "short",
      conflictPolicy: "positive prevails",
    });
  });

  test("it carries RESTORE_FIELDS and nothing the render produced", () => {
    // positive/negative/choices/loras/ts describe the OUTPUT — restoring them
    // into Compose would overwrite the live preview with a stale render
    const payload = restorePayload(record());
    assert.deepEqual(Object.keys(payload).sort(), [
      "conflictPolicy",
      "format",
      "mode",
      "profile",
      "seed",
      "selection",
      "template",
      "textLength",
      "trigger",
      "variables",
    ]);
  });

  test("a partial record restores the node's own defaults, never undefined", () => {
    assert.deepEqual(restorePayload({ template: "photo/street-scene" }), {
      template: "photo/street-scene",
      profile: "standard",
      seed: 0,
      mode: "as configured",
      selection: {},
      variables: "",
      trigger: "",
      format: "template default",
      textLength: "template default",
      conflictPolicy: "negative prevails",
    });
  });

  test("a record without a template yields an empty template (the caller refuses)", () => {
    assert.equal(restorePayload({}).template, "");
    assert.equal(restorePayload(undefined).template, "");
  });

  test("the field names are the SERVER's snake_case, not the panel's camelCase", () => {
    // text_length / conflict_policy are what history.py writes; reading
    // record.textLength would silently restore the default
    const payload = restorePayload(record({ text_length: "long", conflict_policy: "positive prevails" }));
    assert.equal(payload.textLength, "long");
    assert.equal(payload.conflictPolicy, "positive prevails");
  });
});

describe("retention state", () => {
  test("the payload's settings are read under their settings.json names", () => {
    assert.deepEqual(retentionSettings(page([])), {
      history_enabled: true,
      history_months: 12,
      history_thumbs: true,
      history_thumb_px: null,
    });
    assert.deepEqual(
      retentionSettings({
        history_enabled: false,
        history_months: 3,
        history_thumbs: false,
        history_thumb_px: 96,
      }),
      {
        history_enabled: false,
        history_months: 3,
        history_thumbs: false,
        history_thumb_px: 96,
      }
    );
  });

  test("a field this function forgets never reaches the panel at all", () => {
    // THE bug this shape caused: it is a WHITELIST, so history_thumbs and
    // history_thumb_px were silently dropped on their way to the tab. The
    // tiles kept their browser-cached size because px never reached the URL,
    // and the opt-out only appeared to work because the server 404s each row.
    const served = {
      history_enabled: true,
      history_months: 12,
      history_thumbs: false,
      history_thumb_px: 96,
    };
    const seen = retentionSettings(served);
    for (const key of Object.keys(served)) {
      assert.ok(key in seen, `the server sends ${key} and retentionSettings drops it`);
    }
  });

  test("an answer without the settings defaults to ON (history.py's own default)", () => {
    assert.deepEqual(retentionSettings({}), {
      history_enabled: true,
      history_months: DEFAULT_HISTORY_MONTHS,
      history_thumbs: true,
      history_thumb_px: null,
    });
    assert.equal(retentionSettings(undefined).history_enabled, true);
    // an older server that never sends a size must not put "px=null" in a URL
    assert.equal(retentionSettings({}).history_thumb_px, null);
  });

  test("nonsense months fall back instead of rendering NaN", () => {
    assert.equal(retentionSettings({ history_months: "twelve" }).history_months, 12);
    assert.equal(retentionSettings({ history_months: -4 }).history_months, 0);
    assert.equal(retentionSettings({ history_months: 6.7 }).history_months, 6);
  });

  test("the note says whether recording is on and how long months are kept", () => {
    const on = retentionNote({ history_enabled: true, history_months: 12 });
    assert.match(on, /Recording is on/);
    assert.match(on, /newest 12 month file/);
    assert.match(on, /Settings tab/);
    const off = retentionNote({ history_enabled: false, history_months: 12 });
    assert.match(off, /Recording is OFF/);
    assert.match(off, /Settings tab/);
  });

  test("0 months means nothing is pruned — never 'keeping the newest 0'", () => {
    // history_prune treats <= 0 as "prune nothing"; saying otherwise would
    // read as "history is about to be wiped"
    assert.match(retentionNote({ history_enabled: true, history_months: 0 }), /every month file is kept/);
  });
});

describe("keyset paging", () => {
  test("page 1 asks with no cursor and lands with stack ['']", () => {
    const request = firstPageRequest();
    assert.deepEqual(request, { stack: [], before: "" });
    const landed = pageLanded(request, page([record()], { has_more: true, next_before: "T1" }));
    assert.deepEqual(landed.stack, [""]);
    assert.equal(landed.cursor, "T1");
    assert.equal(landed.loading, false);
    assert.equal(landed.error, null);
    assert.equal(landed.records.length, 1);
    assert.deepEqual(landed.settings, {
      history_enabled: true,
      history_months: 12,
      history_thumbs: true,
      history_thumb_px: null,
    });
  });

  test("stack length IS the page number and answers 'can I go back'", () => {
    assert.equal(pageNumber({ stack: [] }), 1);
    assert.equal(pageNumber({ stack: [""] }), 1);
    assert.equal(pageNumber({ stack: ["", "T1"] }), 2);
    assert.equal(hasPreviousPage({ stack: [""] }), false);
    assert.equal(hasPreviousPage({ stack: ["", "T1"] }), true);
  });

  test("deleting a row re-fetches THIS page, not page 1", () => {
    // A keyset page is defined by the cursor that fetched it, and pageLanded
    // pushes that cursor onto the stack — so popping it reproduces the exact
    // request. Falling back to page 1 would throw someone deleting a row on
    // page 7 back to the top of the list.
    assert.deepEqual(currentPageRequest({ stack: [""] }), { stack: [], before: "" });
    assert.deepEqual(currentPageRequest({ stack: ["", "T1"] }), { stack: [""], before: "T1" });
    assert.deepEqual(currentPageRequest({ stack: ["", "T1", "T2"] }), {
      stack: ["", "T1"],
      before: "T2",
    });
    // and it round-trips: re-landing the reproduced request restores the stack
    const history = { stack: ["", "T1", "T2"] };
    const again = pageLanded(currentPageRequest(history), page([record()]));
    assert.deepEqual(again.stack, history.stack);
    // an empty/absent history must not throw
    assert.deepEqual(currentPageRequest({}), { stack: [], before: "" });
    assert.deepEqual(currentPageRequest(), { stack: [], before: "" });
  });

  test("the cursor alone answers 'is there an older page'", () => {
    assert.equal(hasNextPage({ cursor: "T1" }), true);
    assert.equal(hasNextPage({ cursor: "" }), false);
    assert.equal(hasNextPage({}), false);
  });

  test("an exhausted page clears the cursor, so Older cannot re-serve it", () => {
    // has_more false ⇒ next_before is "" server-side; belt and braces here
    const landed = pageLanded({ stack: [""], before: "T1" }, page([record()]));
    assert.equal(landed.cursor, "");
    assert.equal(nextPageRequest(landed), null);
  });

  test("has_more with an empty next_before still clears the cursor", () => {
    const landed = pageLanded(firstPageRequest(), page([], { has_more: true, next_before: "" }));
    assert.equal(landed.cursor, "");
  });

  test("a full walk forward and back sends the exact cursors the server named", () => {
    let history = { records: [], cursor: "", stack: [] };
    const sent = [];
    const fetchPage = (request, next) => {
      assert.ok(request, "the button that produced this request should have been disabled");
      sent.push(request.before);
      history = { ...history, ...pageLanded(request, page([record()], {
        has_more: !!next,
        next_before: next ?? "",
      })) };
    };
    fetchPage(firstPageRequest(), "T1"); // page 1
    fetchPage(nextPageRequest(history), "T2"); // page 2
    fetchPage(nextPageRequest(history), null); // page 3 — last
    assert.deepEqual(sent, ["", "T1", "T2"]);
    assert.deepEqual(history.stack, ["", "T1", "T2"]);
    assert.equal(pageNumber(history), 3);
    assert.equal(hasNextPage(history), false);

    fetchPage(previousPageRequest(history), "T2"); // back to page 2
    assert.deepEqual(sent, ["", "T1", "T2", "T1"]);
    assert.deepEqual(history.stack, ["", "T1"]);
    assert.equal(pageNumber(history), 2);
    assert.equal(hasNextPage(history), true); // Older works again

    fetchPage(previousPageRequest(history), "T1"); // back to page 1
    assert.deepEqual(sent, ["", "T1", "T2", "T1", ""]);
    assert.deepEqual(history.stack, [""]);
    assert.equal(pageNumber(history), 1);
    assert.equal(hasPreviousPage(history), false);
  });

  test("page 1 has nowhere back to go", () => {
    assert.equal(previousPageRequest({ stack: [""] }), null);
    assert.equal(previousPageRequest({ stack: [] }), null);
    assert.equal(previousPageRequest(undefined), null);
  });

  test("a page whose body is malformed lands as an empty page, not a crash", () => {
    // the store skips malformed LINES; a malformed BODY (a proxy's HTML page
    // that still parsed as JSON) must not take the tab down either
    const landed = pageLanded(firstPageRequest(), { records: "not a list" });
    assert.deepEqual(landed.records, []);
    assert.equal(landed.cursor, "");
    assert.equal(landed.settings.history_enabled, true);
  });
});
