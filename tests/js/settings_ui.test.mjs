// The Settings tab's pure logic: the payloads its controls build, the months
// validator, and the "the settings never loaded, so do not save" guard.
//
// WHY THESE ARE PURE FUNCTIONS AT ALL. Two of the tab's controls can do damage
// by saving the WRONG thing rather than by failing to save:
//   * `llm.allow_remote` widens what the SERVER is allowed to fetch (the LLM
//     backend URL is user-supplied and ComfyUI makes the request);
//   * a checkbox rendering "off" because GET /settings failed would persist
//     "off" over a stored "on" — every input in this tab is empty in that
//     state for a reason that has nothing to do with what is stored.
// So the decision "what body, if any, goes to POST /save-settings" is lifted
// out of the click handlers and tested here; the DOM wiring on top of it is
// only affordance (a disabled button), never the guarantee.
//
// The server-side rules these mirror live in mrln/promptapi/settings.py
// (`handle_save_settings`, `_validate_backend_url`, BACKEND_REMOTE_REMEDIATION)
// and mrln/promptapi/history.py (`history_settings`); the last describe block
// asserts the error strings still match that source word for word.
//
// Run: node --test "tests/js/*.test.mjs"   (quoted — an unquoted glob is one
// FILE to Node >= 22 and almost nothing runs)

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  ALLOW_REMOTE_ERROR,
  HISTORY_ENABLED_ERROR,
  HISTORY_MONTHS_ERROR,
  REMOTE_GATE_HINT,
  SETTINGS_NOT_LOADED,
  allowRemotePayload,
  backendFailureHint,
  describeHistory,
  historySavePayload,
  parseHistoryMonths,
} from "../../web/js/composer/settings.js";

const SETTINGS_PY = fileURLToPath(new URL("../../mrln/promptapi/settings.py", import.meta.url));
const HISTORY_PY = fileURLToPath(new URL("../../mrln/promptapi/history.py", import.meta.url));

describe("history retention: months validator", () => {
  test("whole months, as a string (what a number input hands over)", () => {
    for (const [raw, value] of [
      ["0", 0],
      ["1", 1],
      ["12", 12],
      ["  24  ", 24],
      ["0012", 12], // leading zeros are still a whole number of months
      ["120", 120],
    ]) {
      assert.deepEqual(parseHistoryMonths(raw), { ok: true, value }, `for ${JSON.stringify(raw)}`);
    }
  });

  test("whole months, as a number (the value read back from the server)", () => {
    assert.deepEqual(parseHistoryMonths(0), { ok: true, value: 0 });
    assert.deepEqual(parseHistoryMonths(12), { ok: true, value: 12 });
  });

  test("a bool can never become a month count", () => {
    // THE point of this validator. bool is an int in Python, so `true` would
    // land as 1 and silently mean "keep one month" — the server refuses it and
    // the client must not be able to produce it in the first place. Note
    // Number(true) === 1, so the bool check has to come before any coercion.
    assert.deepEqual(parseHistoryMonths(true), { ok: false, error: HISTORY_MONTHS_ERROR });
    assert.deepEqual(parseHistoryMonths(false), { ok: false, error: HISTORY_MONTHS_ERROR });
  });

  test("negative, fractional, empty and junk are refused", () => {
    for (const raw of [
      "", // an emptied input is not "keep everything" — that is an explicit 0
      "   ",
      "-1",
      "-0",
      "1.5",
      "12.0",
      "1e3",
      "+5",
      "12 months",
      "abc",
      "١٢", // non-ASCII digits: \d is ASCII-only in JS on purpose
      -1,
      1.5,
      NaN,
      Infinity,
      1e21, // not a safe integer — JSON would hand the server a value nobody typed
      null,
      undefined,
      {},
      [],
      [12],
    ]) {
      const got = parseHistoryMonths(raw);
      assert.equal(got.ok, false, `${JSON.stringify(raw)} must be refused`);
      assert.equal(got.error, HISTORY_MONTHS_ERROR);
    }
  });
});

describe("history retention: the save payload", () => {
  test("both keys, exactly as handle_save_settings reads them", () => {
    const got = historySavePayload({ settingsLoaded: true, enabled: true, months: "6" });
    assert.deepEqual(got, { ok: true, body: { history_enabled: true, history_months: 6 } });
  });

  test("0 months is a real value (keep everything), not a missing one", () => {
    const got = historySavePayload({ settingsLoaded: true, enabled: false, months: "0" });
    assert.deepEqual(got, { ok: true, body: { history_enabled: false, history_months: 0 } });
  });

  test("the wire types survive JSON: a real bool and a real int", () => {
    const { body } = historySavePayload({ settingsLoaded: true, enabled: true, months: "12" });
    const wire = JSON.parse(JSON.stringify(body));
    assert.equal(typeof wire.history_enabled, "boolean");
    assert.equal(typeof wire.history_months, "number");
    assert.ok(Number.isInteger(wire.history_months));
    assert.notEqual(wire.history_months, true); // the exact confusion the server refuses
  });

  test("a checkbox that is not a checkbox is refused", () => {
    // defence in depth: `.checked` is always a bool, but the payload builder is
    // the only thing between this tab and the server
    for (const enabled of ["true", 1, null, undefined]) {
      const got = historySavePayload({ settingsLoaded: true, enabled, months: "12" });
      assert.deepEqual(got, { ok: false, error: HISTORY_ENABLED_ERROR });
    }
  });

  test("a bad months value blocks the WHOLE save, enabled included", () => {
    // one POST carries both keys, so a half-save is not a thing: refusing the
    // months must refuse the record-toggle with it rather than sending one key
    const got = historySavePayload({ settingsLoaded: true, enabled: false, months: "" });
    assert.deepEqual(got, { ok: false, error: HISTORY_MONTHS_ERROR });
    assert.equal(got.body, undefined);
  });

  test("settings that never loaded are never overwritten", () => {
    // the guard this tab exists to keep: a failed GET leaves the checkbox
    // unchecked and the months box empty for reasons that have nothing to do
    // with what is stored
    const got = historySavePayload({ settingsLoaded: false, enabled: false, months: "12" });
    assert.deepEqual(got, { ok: false, error: SETTINGS_NOT_LOADED });
    assert.equal(got.body, undefined);
    // and it is checked BEFORE the value, so a valid-looking form cannot slip
    assert.equal(
      historySavePayload({ settingsLoaded: false, enabled: true, months: "12" }).error,
      SETTINGS_NOT_LOADED
    );
  });

  test("no argument at all is a refusal, not a crash", () => {
    assert.deepEqual(historySavePayload(), { ok: false, error: SETTINGS_NOT_LOADED });
  });
});

describe("the remote-backend gate payload", () => {
  test("the body is the nested llm object the handler expects", () => {
    assert.deepEqual(allowRemotePayload({ settingsLoaded: true, next: true }), {
      ok: true,
      body: { llm: { allow_remote: true } },
    });
    assert.deepEqual(allowRemotePayload({ settingsLoaded: true, next: false }), {
      ok: true,
      body: { llm: { allow_remote: false } },
    });
  });

  test("only a real bool — 'yes' is what the server rejects with a 400", () => {
    for (const next of ["yes", "true", 1, 0, null, undefined]) {
      assert.deepEqual(allowRemotePayload({ settingsLoaded: true, next }), {
        ok: false,
        error: ALLOW_REMOTE_ERROR,
      });
    }
    assert.deepEqual(allowRemotePayload(), { ok: false, error: ALLOW_REMOTE_ERROR });
  });

  test("refuses in BOTH directions when the settings never loaded", () => {
    // turning it on from a blind state would widen the server's reach on the
    // strength of a value we never read; turning it off would clamp a gate the
    // user cannot see the state of. The tab shows a default in that state, not
    // a stored value, so neither is a decision the user actually made.
    for (const next of [true, false]) {
      assert.deepEqual(allowRemotePayload({ settingsLoaded: false, next }), {
        ok: false,
        error: SETTINGS_NOT_LOADED,
      });
    }
  });
});

describe("backend failure hint", () => {
  test("the server's remediation wins whenever it survived the trip", () => {
    const hint = backendFailureHint(
      "'llm.ollama_url' points at '10.0.0.5', which is not this machine (loopback)",
      "enable 'allow_remote' in the Composer's Settings tab (LLM section)",
      false
    );
    assert.equal(hint, " — enable 'allow_remote' in the Composer's Settings tab (LLM section)");
  });

  test("a gate refusal still names the control when the remediation was lost", () => {
    // the cached probe path (api.js keeps only err.message in its cache entry)
    // is exactly the path the green/red marks use, so the refusal arrives
    // stripped of the one sentence that tells the user what to do
    const hint = backendFailureHint(
      "'llm.ollama_url' points at '10.0.0.5', which is not this machine (loopback)",
      undefined,
      false
    );
    assert.equal(hint, ` — ${REMOTE_GATE_HINT}`);
    assert.match(hint, /Allow remote backends/);
  });

  test("no hint when the gate is already open — it is not the cause", () => {
    assert.equal(
      backendFailureHint("ollama unreachable at http://10.0.0.5:11434: timed out", null, true),
      ""
    );
  });

  test("no hint for an ordinary failure", () => {
    assert.equal(backendFailureHint("ollama unreachable: connection refused", "", false), "");
    assert.equal(backendFailureHint(undefined, undefined, false), "");
  });
});

describe("retention, in words", () => {
  test("0 reads as 'kept forever', never as 'zero months'", () => {
    assert.equal(describeHistory(true, 0), "recording on · every month is kept");
    assert.equal(describeHistory(false, 0), "recording off · every month is kept");
  });

  test("a count reads as a count", () => {
    assert.equal(describeHistory(true, 12), "recording on · 12 month file(s) kept");
    assert.equal(describeHistory(false, 1), "recording off · 1 month file(s) kept");
  });
});

describe("the client's error strings still match the server's", () => {
  // These are quoted from the handlers on purpose: a user must not be able to
  // tell whether the client or the server refused their value. If the Python
  // wording is edited, this fails instead of the two drifting apart silently.
  const settingsPy = readFileSync(SETTINGS_PY, "utf8");
  const historyPy = readFileSync(HISTORY_PY, "utf8");

  test("handle_save_settings raises exactly what this module quotes", () => {
    for (const message of [HISTORY_MONTHS_ERROR, HISTORY_ENABLED_ERROR, ALLOW_REMOTE_ERROR]) {
      assert.ok(settingsPy.includes(message), `settings.py no longer says: ${message}`);
    }
  });

  test("the payload keys this tab sends are the keys the handlers read", () => {
    for (const key of ["history_enabled", "history_months"]) {
      assert.ok(settingsPy.includes(`"${key}" in payload`), `save-settings dropped ${key}`);
      assert.ok(historyPy.includes(`"${key}"`), `history.py dropped ${key}`);
    }
    assert.ok(settingsPy.includes('"allow_remote" in raw_llm'));
    assert.ok(settingsPy.includes('llm["allow_remote"] = flag'));
  });

  test("allow_remote still defaults to off, and the gate is still loopback-only", () => {
    // the note this tab renders promises both — if the server ever changes its
    // mind, the note becomes a lie and this catches it
    assert.ok(settingsPy.includes('return bool(_llm_settings(settings).get("allow_remote"))'));
    assert.ok(settingsPy.includes('LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})'));
  });

  test("a stored URL is still echoed back even when it would be refused now", () => {
    // the tab must keep SHOWING a stale LAN URL: that is how the user fixes it
    assert.ok(settingsPy.includes('"ollama_url": llm.get("ollama_url") or DEFAULT_OLLAMA_URL'));
    assert.ok(settingsPy.includes("the stored value is echoed as-is even when it would be refused"));
  });
});
