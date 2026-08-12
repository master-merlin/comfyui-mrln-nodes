// Unit tests for web/js/composer/api.js — the composer's fetch layer.
//
// The module takes its transport by injection (no ComfyUI imports), so every
// case here drives it with a stub `fetchApi` and asserts on the exact routes it
// produced and the exact values it returned. What is pinned: the error contract
// callers depend on, the single URL-encoding path, and every invalidation rule
// of the library fingerprint cache — a cache that hands back stale data is
// worse than no cache, so each hazard has its own test.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const API_URL = new URL("../../web/js/composer/api.js", import.meta.url);

// ---------------------------------------------------------------------------
// Load the module with browser globals booby-trapped. ComfyUI auto-imports
// every .js under web/, so api.js is evaluated standalone in the browser as
// well as via the entry file; it must do NOTHING at import time — no DOM, no
// app/api touch and above all no network. If a future edit adds a top-level
// side effect, this import throws and the whole file fails loudly.
// ---------------------------------------------------------------------------

const TRAPPED = ["document", "window", "app", "api", "localStorage", "XMLHttpRequest", "alert"];
const saved = new Map();
for (const name of TRAPPED) {
  saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, {
    configurable: true,
    get() {
      throw new Error(`api.js touched the global '${name}' at import time`);
    },
  });
}
const savedFetch = globalThis.fetch;
globalThis.fetch = () => {
  throw new Error("api.js performed a network request at import time");
};

const mod = await import(API_URL.href);

globalThis.fetch = savedFetch;
for (const name of TRAPPED) {
  const desc = saved.get(name);
  if (desc) Object.defineProperty(globalThis, name, desc);
  else delete globalThis[name];
}

const {
  CLOUD_BACKEND_SEED,
  CUSTOM_ENTRY,
  LIBRARY_ROUTE,
  LLM_PULL_ROUTE,
  LLM_VALIDATE_ROUTE,
  NOTE_PREFIX,
  PULL_PREFIX,
  SETTINGS_ROUTE,
  buildModelValues,
  createApi,
  isFullLibraryPayload,
  isNoteEntry,
  isPullEntry,
  isSentinelEntry,
  modelNoteEntry,
  queryString,
  routeWithQuery,
} = mod;

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------

const jsonResp = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

/** A 200 that is NOT JSON — a proxy login page, a captive portal. */
const htmlResp = (status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => {
    throw new SyntaxError("Unexpected token '<'");
  },
});

/**
 * @param {(route: string, init: object, callNo: number) => object} responder
 * @returns transport plus the recorded calls
 */
function harness(responder, extra = {}) {
  const calls = [];
  const logged = [];
  const fetchApi = async (route, init) => {
    calls.push({ route, init, path: route.split("?")[0], query: route.split("?")[1] ?? "" });
    return responder(route, init, calls.length);
  };
  const api = createApi({ fetchApi, log: (m) => logged.push(m), ...extra });
  return { api, calls, logged, routes: () => calls.map((c) => c.route) };
}

const libraryPayload = (fingerprint, label = "one") => ({
  fingerprint,
  templates: [{ slug: "a/one", label }],
  sections: [],
  folders: [],
  profiles: [],
});

// ---------------------------------------------------------------------------

describe("apiJson transport", () => {
  test("returns the parsed body of a 200", async () => {
    const { api } = harness(() => jsonResp({ ok: true, n: 1 }));
    assert.deepEqual(await api.apiJson("/mrln/prompt/ping"), { ok: true, n: 1 });
  });

  test("throws on a 200 whose body is not JSON (the permanent-spinner bug)", async () => {
    // Regression: returning null here left loadLibrary dereferencing null
    // OUTSIDE its try/catch — unhandled rejection, boot spinner forever, and
    // the panel's own Retry card unreachable. It must throw instead.
    const { api } = harness(() => htmlResp(200));
    await assert.rejects(() => api.apiJson("/mrln/prompt/library"), {
      message: "invalid response from server (not JSON)",
    });
  });

  test("the not-JSON error carries remediation and the status", async () => {
    const { api } = harness(() => htmlResp(200));
    const err = await api.apiJson("/mrln/prompt/library").catch((e) => e);
    assert.equal(err.status, 200);
    assert.match(err.remediation, /proxies/);
  });

  test("re-throws the server's error message and remediation on a non-2xx", async () => {
    const { api } = harness(() => jsonResp({ error: "no such slug", remediation: "pick one" }, 404));
    const err = await api.apiJson("/mrln/prompt/template?slug=x").catch((e) => e);
    assert.equal(err.message, "no such slug");
    assert.equal(err.remediation, "pick one");
    assert.equal(err.status, 404);
  });

  test("falls back to 'HTTP <status>' when an error body is not JSON", async () => {
    const { api } = harness(() => htmlResp(502));
    await assert.rejects(() => api.apiJson("/mrln/prompt/library"), { message: "HTTP 502" });
  });

  test("serializes a body and keeps the JSON content type", async () => {
    const { api, calls } = harness(() => jsonResp({ ok: true }));
    await api.apiJson("/mrln/prompt/save", { method: "POST", body: { slug: "a/b", n: 2 } });
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.body, JSON.stringify({ slug: "a/b", n: 2 }));
    assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  });

  test("a GET carries no body", async () => {
    const { api, calls } = harness(() => jsonResp({ ok: true }));
    await api.apiJson("/mrln/prompt/settings");
    assert.equal(calls[0].init.body, undefined);
  });

  test("logs only requests slower than the threshold", async () => {
    let t = 0;
    const { api, logged } = harness(() => jsonResp({ ok: true }), {
      clock: () => (t += 500), // start=500, end=1000 -> 500 ms
    });
    await api.apiJson("/mrln/prompt/library?x=1");
    assert.equal(logged.length, 1);
    assert.match(logged[0], /slow request \/mrln\/prompt\/library took 500ms/);

    let fast = 0;
    const quick = harness(() => jsonResp({ ok: true }), { clock: () => (fast += 5) });
    await quick.api.apiJson("/mrln/prompt/library");
    assert.deepEqual(quick.logged, []);
  });
});

describe("query encoding", () => {
  test("queryString encodes every key and value exactly once", () => {
    assert.equal(queryString({ a: "x y", "b&c": "1+2" }), "?a=x%20y&b%26c=1%2B2");
  });

  test("queryString drops empty, null and undefined values", () => {
    assert.equal(queryString({ a: "", b: null, c: undefined, d: "1" }), "?d=1");
    assert.equal(queryString({}), "");
    assert.equal(queryString(null), "");
  });

  test("routeWithQuery appends nothing when there is nothing to append", () => {
    assert.equal(routeWithQuery("/r", {}), "/r");
    assert.equal(routeWithQuery("/r", { fp: "abc" }), "/r?fp=abc");
  });

  test("llm-validate encodes the provider (a foreign backend value cannot corrupt the query)", async () => {
    // The backend combo deliberately keeps unknown values so foreign workflows
    // load intact, and that value flows straight into this URL.
    const { api, calls } = harness(() => jsonResp({ models: [], suggested: [] }));
    await api.llmModels("we&ird #1");
    assert.equal(calls[0].route, `${LLM_VALIDATE_ROUTE}?provider=we%26ird%20%231`);
    assert.equal(calls[0].query.split("&").length, 1); // one parameter, not two
  });

  test("pull status and pull start encode the model name", async () => {
    const { api, calls } = harness(() => jsonResp({ status: "pulling" }));
    await api.pullStatus("qwen3:14b");
    await api.startPull("qwen3:14b");
    assert.equal(calls[0].route, `${LLM_PULL_ROUTE}?model=qwen3%3A14b`);
    assert.equal(calls[1].route, LLM_PULL_ROUTE);
    assert.equal(calls[1].init.body, JSON.stringify({ model: "qwen3:14b", start: true }));
  });
});

describe("library fingerprint cache", () => {
  test("the first fetch sends no fp, the second sends the fingerprint it got", async () => {
    const { api, calls } = harness((route) =>
      route.includes("fp=") ? jsonResp({ unchanged: true, fingerprint: "fp1" }) : jsonResp(libraryPayload("fp1"))
    );
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(calls[0].route, LIBRARY_ROUTE);
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(calls[1].route, `${LIBRARY_ROUTE}?fp=fp1`);
    assert.equal(api.libraryFingerprint(), "fp1");
  });

  test("an 'unchanged' answer replays the cached payload", async () => {
    const { api } = harness((route) =>
      route.includes("fp=") ? jsonResp({ unchanged: true, fingerprint: "fp1" }) : jsonResp(libraryPayload("fp1"))
    );
    const first = await api.apiJson(LIBRARY_ROUTE);
    const second = await api.apiJson(LIBRARY_ROUTE);
    assert.deepEqual(second, first);
    assert.equal(second.templates[0].slug, "a/one");
  });

  test("each hit is a detached copy — a caller mutating it cannot poison the cache", async () => {
    const { api } = harness((route) =>
      route.includes("fp=") ? jsonResp({ unchanged: true, fingerprint: "fp1" }) : jsonResp(libraryPayload("fp1"))
    );
    const first = await api.apiJson(LIBRARY_ROUTE);
    first.templates[0].label = "MUTATED";
    first.templates.push({ slug: "junk" });
    const second = await api.apiJson(LIBRARY_ROUTE);
    assert.equal(second.templates.length, 1);
    assert.equal(second.templates[0].label, "one");
    second.templates[0].label = "AGAIN";
    const third = await api.apiJson(LIBRARY_ROUTE);
    assert.equal(third.templates[0].label, "one");
  });

  test("'unchanged' for a fingerprint we do not hold falls back to a full fetch", async () => {
    // Hazard: replaying the cached payload here would show a catalog the
    // server no longer has. One retry, without fp, and never recursive.
    let phase = "first";
    const { api, calls } = harness((route) => {
      if (phase === "first") return jsonResp(libraryPayload("fp1"));
      if (route.includes("fp=")) return jsonResp({ unchanged: true, fingerprint: "SOMETHING-ELSE" });
      return jsonResp(libraryPayload("fp2", "two"));
    });
    await api.apiJson(LIBRARY_ROUTE);
    phase = "second";
    const body = await api.apiJson(LIBRARY_ROUTE);
    assert.equal(body.templates[0].label, "two");
    assert.equal(calls.length, 3);
    assert.equal(calls[2].route, LIBRARY_ROUTE); // the retry carries no fp
    assert.equal(api.libraryFingerprint(), "fp2");
  });

  test("'unchanged' with nothing cached is impossible to serve — no fp is ever sent", async () => {
    const { api, calls } = harness(() => jsonResp({ unchanged: true, fingerprint: "fp1" }));
    const body = await api.apiJson(LIBRARY_ROUTE);
    assert.equal(body.unchanged, true); // handed through, never cached
    assert.equal(api.libraryFingerprint(), null);
    await api.apiJson(LIBRARY_ROUTE);
    assert.deepEqual(calls.map((c) => c.route), [LIBRARY_ROUTE, LIBRARY_ROUTE]);
  });

  test("a payload without a fingerprint is never cached", async () => {
    // Rule: never store a payload under a fingerprint that did not arrive
    // with it. Without this, an older/patched server would pin one payload.
    const { api, calls } = harness(() => jsonResp({ templates: [], sections: [] }));
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(api.libraryFingerprint(), null);
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(calls[1].route, LIBRARY_ROUTE);
  });

  test("a malformed payload drops an existing cache instead of keeping a stale fp", async () => {
    let phase = "good";
    const { api, calls } = harness(() =>
      phase === "good" ? jsonResp(libraryPayload("fp1")) : jsonResp({ fingerprint: "fp2" })
    );
    await api.apiJson(LIBRARY_ROUTE);
    phase = "broken";
    await api.apiJson(LIBRARY_ROUTE); // sends fp1, gets a payload with no templates
    assert.equal(api.libraryFingerprint(), null);
    phase = "good";
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(calls[2].route, LIBRARY_ROUTE); // no stale fp on the next try
  });

  test("any non-GET request invalidates the cache", async () => {
    // The server fingerprint is mtime+size based; this is the belt-and-braces
    // guard for a write landing inside one filesystem timestamp tick.
    const { api, calls } = harness(() => jsonResp(libraryPayload("fp1")));
    await api.apiJson(LIBRARY_ROUTE);
    await api.apiJson("/mrln/prompt/save-template", { method: "POST", body: { slug: "a/one" } });
    assert.equal(api.libraryFingerprint(), null);
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(calls[2].route, LIBRARY_ROUTE);
  });

  test("invalidateLibrary() forces the next fetch to be full", async () => {
    const { api, calls } = harness(() => jsonResp(libraryPayload("fp1")));
    await api.apiJson(LIBRARY_ROUTE);
    api.invalidateLibrary();
    await api.apiJson(LIBRARY_ROUTE);
    assert.equal(calls[1].route, LIBRARY_ROUTE);
  });

  test("a library GET that brings its own query bypasses the cache entirely", async () => {
    const { api, calls } = harness(() => jsonResp(libraryPayload("fp1")));
    await api.apiJson(LIBRARY_ROUTE);
    await api.apiJson(`${LIBRARY_ROUTE}?filter=x`);
    assert.equal(calls[1].route, `${LIBRARY_ROUTE}?filter=x`); // no fp spliced in
  });

  test("other routes are never cached", async () => {
    const { api, calls } = harness(() => jsonResp({ raw: {}, fingerprint: "fp1", templates: [] }));
    await api.apiJson("/mrln/prompt/template?slug=a%2Fone");
    await api.apiJson("/mrln/prompt/template?slug=a%2Fone");
    assert.equal(calls.length, 2);
    assert.equal(api.libraryFingerprint(), null);
  });
});

describe("llm model cache", () => {
  const models = (n) => jsonResp({ models: [`m${n}`], suggested: ["s1"] });

  test("a second call inside the TTL does not hit the network", async () => {
    let clockMs = 1000;
    const { api, calls } = harness((route, init, n) => models(n), { now: () => clockMs, ttlMs: 30000 });
    await api.llmModels("ollama");
    clockMs += 29000;
    const entry = await api.llmModels("ollama");
    assert.equal(calls.length, 1);
    assert.deepEqual(entry.models, ["m1"]);
  });

  test("a call past the TTL refetches", async () => {
    let clockMs = 1000;
    const { api, calls } = harness((route, init, n) => models(n), { now: () => clockMs, ttlMs: 30000 });
    await api.llmModels("ollama");
    clockMs += 30001;
    const entry = await api.llmModels("ollama");
    assert.equal(calls.length, 2);
    assert.deepEqual(entry.models, ["m2"]);
  });

  test("concurrent calls share ONE in-flight request", async () => {
    // values() can fire several times per menu open; without the in-flight
    // marker each one started its own request (fetchedAt is only stamped on
    // settle, so nothing suppressed the duplicates).
    const { api, calls } = harness((route, init, n) => models(n));
    const [a, b, c] = await Promise.all([
      api.llmModels("ollama"),
      api.llmModels("ollama"),
      api.llmModels("ollama"),
    ]);
    assert.equal(calls.length, 1);
    assert.deepEqual(a.models, ["m1"]);
    assert.deepEqual(b, a);
    assert.deepEqual(c, a);
  });

  test("refreshLlmModels ignores the TTL but still joins an in-flight request", async () => {
    const { api, calls } = harness((route, init, n) => models(n));
    await api.llmModels("ollama");
    await Promise.all([api.refreshLlmModels("ollama"), api.refreshLlmModels("ollama")]);
    assert.equal(calls.length, 2); // 1 initial + 1 shared refresh
  });

  test("a failed fetch records the error, keeps prior suggestions and never rejects", async () => {
    let phase = "ok";
    const { api } = harness(() =>
      phase === "ok"
        ? jsonResp({ models: ["m1"], suggested: ["s1"] })
        : jsonResp({ error: "ollama unreachable at http://127.0.0.1:11434" }, 502)
    );
    await api.llmModels("ollama");
    phase = "down";
    const entry = await api.refreshLlmModels("ollama");
    assert.deepEqual(entry.models, []);
    assert.deepEqual(entry.suggested, ["s1"]);
    assert.match(entry.error, /unreachable/);
  });

  test("the cached entry is readable synchronously (the combo needs its list now)", async () => {
    const { api } = harness(() => models(1));
    assert.equal(api.llmModelsCached("ollama"), undefined);
    const pending = api.llmModels("ollama");
    assert.ok(api.llmModelsCached("ollama").pending, "an in-flight marker is visible");
    await pending;
    assert.equal(api.llmModelsCached("ollama").pending, null);
    assert.deepEqual(api.llmModelsCached("ollama").models, ["m1"]);
  });

  test("providers are cached independently", async () => {
    const { api, calls } = harness((route, init, n) => models(n));
    await api.llmModels("ollama");
    await api.llmModels("lmstudio");
    assert.equal(calls.length, 2);
    assert.deepEqual(api.llmModelsCached("lmstudio").models, ["m2"]);
  });
});

describe("cloud backends derived from the server's key flags", () => {
  const keys = (flags) => jsonResp({ llm_keys_set: flags });

  test("the seed answers before any response, the server's key set after", async () => {
    const { api } = harness(() => keys({ anthropic: true, openai: false, mistral: false }));
    assert.deepEqual(api.cloudBackends(), CLOUD_BACKEND_SEED);
    await api.llmKeys();
    assert.deepEqual(api.cloudBackends(), ["anthropic", "openai", "mistral"]);
  });

  test("only keyed backends are offered", async () => {
    const { api } = harness(() => keys({ anthropic: true, openai: false, gemini: true }));
    await api.llmKeys();
    assert.deepEqual(api.keyedCloudBackends(), ["anthropic", "gemini"]);
  });

  test("llmKeys dedups concurrent calls and honours the TTL", async () => {
    let clockMs = 1000;
    const { api, calls } = harness(() => keys({ anthropic: true }), {
      now: () => clockMs,
      ttlMs: 30000,
    });
    await Promise.all([api.llmKeys(), api.llmKeys()]);
    assert.equal(calls.length, 1);
    clockMs += 10;
    await api.llmKeys();
    assert.equal(calls.length, 1);
    clockMs += 30001;
    await api.llmKeys();
    assert.equal(calls.length, 2);
    assert.equal(calls[0].route, SETTINGS_ROUTE);
  });

  test("a failed settings fetch keeps the last known flags", async () => {
    let phase = "ok";
    const { api } = harness(() => (phase === "ok" ? keys({ anthropic: true }) : jsonResp({}, 500)));
    await api.llmKeys();
    phase = "down";
    const entry = await api.refreshLlmKeys();
    assert.deepEqual(entry.keys, { anthropic: true });
    assert.equal(entry.error, "HTTP 500");
  });
});

describe("model dropdown values (one builder for every dropdown)", () => {
  test("ollama: current, installed, ⬇ pull suggestions, custom last", () => {
    const values = buildModelValues({
      provider: "ollama",
      current: "llama3.2:3b",
      entry: { models: ["gemma3:4b", "llama3.2:3b"], suggested: ["qwen3:8b"] },
    });
    assert.deepEqual(values, [
      "llama3.2:3b",
      "gemma3:4b",
      `${PULL_PREFIX}qwen3:8b`,
      CUSTOM_ENTRY,
    ]);
  });

  test("cloud: the SERVER's suggestions, unprefixed (no hardcoded JS copy)", () => {
    const values = buildModelValues({
      provider: "anthropic",
      current: "",
      entry: { models: [], suggested: ["claude-haiku-4-5-20251001", "claude-sonnet-5"] },
    });
    assert.deepEqual(values, ["claude-haiku-4-5-20251001", "claude-sonnet-5", CUSTOM_ENTRY]);
  });

  test("lm studio: suggestions are never pull-prefixed (no pull API)", () => {
    const values = buildModelValues({
      provider: "lmstudio",
      current: "",
      entry: { models: ["local-model"], suggested: ["x"] },
    });
    assert.deepEqual(values, ["local-model", "x", CUSTOM_ENTRY]);
  });

  test("a failed fetch explains the empty list with a ⚠ entry at the top", () => {
    const values = buildModelValues({
      provider: "ollama",
      current: "gemma3:4b",
      entry: { models: [], suggested: [], error: "ollama unreachable at http://127.0.0.1:11434" },
    });
    assert.ok(isNoteEntry(values[0]));
    assert.match(values[0], /unreachable/);
    assert.deepEqual(values.slice(1), ["gemma3:4b", CUSTOM_ENTRY]);
  });

  test("sentinels are never re-listed as the current value", () => {
    for (const current of [CUSTOM_ENTRY, `${PULL_PREFIX}qwen3:8b`, `${NOTE_PREFIX}down`]) {
      const values = buildModelValues({ provider: "ollama", current, entry: { models: [] } });
      assert.deepEqual(values, [CUSTOM_ENTRY]);
    }
  });

  test("no entry at all still yields a usable list", () => {
    assert.deepEqual(buildModelValues({ provider: "ollama", current: "m" }), ["m", CUSTOM_ENTRY]);
    assert.deepEqual(buildModelValues(), [CUSTOM_ENTRY]);
  });

  test("duplicates collapse (a current value that is also installed)", () => {
    const values = buildModelValues({
      provider: "ollama",
      current: "m1",
      entry: { models: ["m1", "m1"], suggested: [] },
    });
    assert.deepEqual(values, ["m1", CUSTOM_ENTRY]);
  });

  test("modelNoteEntry is one bounded line", () => {
    const note = modelNoteEntry("ollama", `${"x".repeat(200)}\nsecond line`);
    assert.ok(note.startsWith(NOTE_PREFIX));
    assert.ok(note.length <= NOTE_PREFIX.length + 90);
    assert.equal(modelNoteEntry("ollama", ""), `${NOTE_PREFIX}ollama unavailable`);
  });

  test("sentinel predicates", () => {
    assert.ok(isSentinelEntry(CUSTOM_ENTRY));
    assert.ok(isSentinelEntry(`${PULL_PREFIX}m`));
    assert.ok(isSentinelEntry(`${NOTE_PREFIX}m`));
    assert.ok(isPullEntry(`${PULL_PREFIX}m`));
    assert.ok(!isSentinelEntry("gemma3:4b"));
    assert.ok(!isNoteEntry(undefined));
  });
});

describe("isFullLibraryPayload", () => {
  test("accepts a real payload only", () => {
    assert.ok(isFullLibraryPayload(libraryPayload("fp1")));
    assert.ok(!isFullLibraryPayload({ unchanged: true, fingerprint: "fp1", templates: [] }));
    assert.ok(!isFullLibraryPayload({ templates: [] }));
    assert.ok(!isFullLibraryPayload({ fingerprint: "", templates: [] }));
    assert.ok(!isFullLibraryPayload({ fingerprint: "fp1" }));
    assert.ok(!isFullLibraryPayload(null));
    assert.ok(!isFullLibraryPayload("nope"));
  });
});
