// MRLN Prompt Composer — HTTP layer: one JSON fetch wrapper, one library
// fingerprint cache, one LLM model/key cache, one query-string encoder.
//
// HARD RULE for this file: ZERO top-level side effects. ComfyUI auto-imports
// every .js file under WEB_DIRECTORY, so this module is evaluated standalone in
// the browser AND as an import of the entry file; the module cache makes that
// harmless only because evaluating it does nothing but declare things.
// Therefore: no app/api imports, no DOM access, no listeners, no fetch at
// import time, no module-level mutable state. All mutable state (caches) lives
// inside the object `createApi()` returns, so a second import cannot see it and
// tests get a fresh instance per case.
//
// Consequence of "no ComfyUI imports": the transport is INJECTED. The entry
// file passes ComfyUI's `api.fetchApi`; the tests pass a stub. That also keeps
// the module free of the `../../scripts/api.js` path, which differs by nesting
// level (WEB_DIRECTORY is ./web/js, so this file sits one directory deeper).

// ---- constants -------------------------------------------------------------

/** Requests slower than this get a console.debug note (boot-time diagnosis). */
export const SLOW_REQUEST_MS = 400;

/** Model/key lists are cached this long; a dropdown open is not a fetch. */
export const LLM_CACHE_TTL_MS = 30000;

export const LIBRARY_ROUTE = "/mrln/prompt/library";
export const SETTINGS_ROUTE = "/mrln/prompt/settings";
export const LLM_VALIDATE_ROUTE = "/mrln/prompt/llm-validate";
export const LLM_PULL_ROUTE = "/mrln/prompt/llm-pull";

/** Dropdown sentinels. A value carrying one of these is never a model name. */
export const PULL_PREFIX = "⬇ pull ";
export const CUSTOM_ENTRY = "✏ custom…";
export const NOTE_PREFIX = "⚠ ";

// Only the pre-fetch seed: the authoritative cloud-backend list is the KEY SET
// of `llm_keys_set` in the settings response (llm.py CLOUD_PROVIDERS), which
// `cloudBackends()` prefers as soon as one settings fetch has landed. Keeping a
// seed is what lets the synchronous combo `values()` work on the very first
// open, before any response exists.
export const CLOUD_BACKEND_SEED = ["anthropic", "openai", "gemini", "openrouter"];

// ---- pure helpers ----------------------------------------------------------

/**
 * Query string with EVERY value encoded exactly once — the single encoding
 * path, so no call site can forget it (a `provider` or `model` containing
 * '&', '#' or '+' used to corrupt the query instead of reaching the server's
 * clean "unknown provider" error). Empty/null/undefined values are dropped.
 */
export function queryString(params) {
  const parts = [];
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export function routeWithQuery(route, params) {
  return `${route}${queryString(params)}`;
}

export function isNoteEntry(value) {
  return String(value ?? "").startsWith(NOTE_PREFIX);
}

export function isPullEntry(value) {
  return String(value ?? "").startsWith(PULL_PREFIX);
}

/** True for every dropdown entry that is a UI affordance, not a model name. */
export function isSentinelEntry(value) {
  return value === CUSTOM_ENTRY || isNoteEntry(value) || isPullEntry(value);
}

/** One-line, length-bounded note explaining an empty model list. */
export function modelNoteEntry(provider, error) {
  const detail = String(error ?? "")
    .split("\n")[0]
    .trim();
  const text = detail || `${provider} unavailable`;
  return `${NOTE_PREFIX}${text.length > 90 ? `${text.slice(0, 89)}…` : text}`;
}

/**
 * The model dropdown's value list, from a cache entry as `llmModels()` stores
 * it. ONE builder for every consumer (Enhance node combo, De-compose tab), so
 * the two dropdowns cannot drift apart again:
 *   [note if the fetch failed] [current value] [installed] [suggested] [custom]
 * Suggestions carry the ⬇ pull prefix ONLY for ollama — the only backend with
 * a pull API. Cloud providers reach this with models: [] and the server's
 * curated `suggested` list (llm-validate answers them offline), which is why
 * there is no hardcoded cloud model list in the JS any more.
 */
export function buildModelValues({ provider, current = "", entry = null } = {}) {
  const values = [];
  const push = (value) => {
    if (value && !values.includes(value)) values.push(value);
  };
  if (entry?.error) push(modelNoteEntry(provider, entry.error));
  const now = String(current ?? "").trim();
  if (now && !isSentinelEntry(now)) push(now);
  for (const model of entry?.models ?? []) push(model);
  for (const model of entry?.suggested ?? []) {
    push(provider === "ollama" ? `${PULL_PREFIX}${model}` : model);
  }
  push(CUSTOM_ENTRY);
  return values;
}

/** A full library payload, as opposed to the `{unchanged: true}` envelope. */
export function isFullLibraryPayload(body) {
  return (
    !!body &&
    typeof body === "object" &&
    body.unchanged !== true &&
    typeof body.fingerprint === "string" &&
    body.fingerprint !== "" &&
    Array.isArray(body.templates)
  );
}

function clone(value) {
  return structuredClone(value);
}

function defaultClock() {
  return typeof performance !== "undefined" && performance ? performance.now() : Date.now();
}

// ---- the api instance ------------------------------------------------------

/**
 * @param {object} deps
 * @param {(route: string, init?: object) => Promise<Response>} deps.fetchApi
 *        ComfyUI's api.fetchApi (or any fetch-shaped stub).
 * @param {(message: string) => void} [deps.log] slow-request sink.
 * @param {() => number} [deps.now] wall clock for cache TTLs (ms).
 * @param {() => number} [deps.clock] monotonic clock for request timing (ms).
 * @param {number} [deps.ttlMs] model/key cache TTL.
 */
export function createApi(deps = {}) {
  const fetchApi = deps.fetchApi;
  const log = deps.log ?? ((message) => console.debug(message));
  const now = deps.now ?? (() => Date.now());
  const clock = deps.clock ?? defaultClock;
  const ttlMs = deps.ttlMs ?? LLM_CACHE_TTL_MS;

  // ---- transport ----------------------------------------------------------

  async function rawJson(route, options = {}) {
    const started = clock();
    const resp = await fetchApi(route, {
      headers: { "Content-Type": "application/json" },
      ...options,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const ms = clock() - started;
    if (ms > SLOW_REQUEST_MS) {
      // surfaces WHERE first-open time goes (busy boot loop, AV first-touch…)
      log(`[MRLN] slow request ${route.split("?")[0]} took ${Math.round(ms)}ms`);
    }
    let data = null;
    try {
      data = await resp.json();
    } catch {
      /* non-JSON error page */
    }
    if (!resp.ok) {
      const err = new Error(data?.error ?? `HTTP ${resp.status}`);
      err.remediation = data?.remediation;
      err.status = resp.status;
      throw err;
    }
    if (data === null || data === undefined) {
      // A 200 whose body is not JSON (a reverse proxy's login page, a captive
      // portal) used to return null here. Callers dereference the result far
      // from their try/catch — the panel's boot spinner then hung forever on
      // an unhandled TypeError while its own Retry card sat unused. Throwing
      // routes this failure into every existing catch/toast path.
      const err = new Error("invalid response from server (not JSON)");
      err.remediation = "something other than ComfyUI answered — check proxies and auth layers";
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  // ---- library fingerprint cache ------------------------------------------
  // handle_library takes `fp=<fingerprint>` and answers an unchanged catalog
  // with 200 {"unchanged": true, "fingerprint": fp} instead of parsing every
  // file. Cache rules (all of them guards against showing stale data):
  //  1. A payload is only ever stored under the fingerprint that arrived WITH
  //     it, and only when it really is a full payload (isFullLibraryPayload).
  //  2. `unchanged` is honoured only when its fingerprint equals the one we
  //     hold AND we hold the matching payload; anything else drops the cache
  //     and re-fetches WITHOUT fp (once — never recursively).
  //  3. Any non-GET request drops the cache. The server's fingerprint is
  //     mtime+size based, so this is belt-and-braces for a write that lands
  //     inside one filesystem timestamp tick.
  //  4. Callers get a CLONE, so panel-side mutation of state.library can
  //     never poison the cached copy.

  let libFingerprint = null;
  let libPayload = null;

  function invalidateLibrary() {
    libFingerprint = null;
    libPayload = null;
  }

  function libraryFingerprint() {
    return libFingerprint;
  }

  function storeLibrary(body) {
    if (isFullLibraryPayload(body)) {
      libPayload = clone(body);
      libFingerprint = body.fingerprint;
    } else {
      invalidateLibrary(); // malformed answer: never keep a stale fp around
    }
    return body;
  }

  async function libraryJson(route, options) {
    if (libFingerprint && libPayload) {
      const body = await rawJson(routeWithQuery(route, { fp: libFingerprint }), options);
      if (body?.unchanged === true) {
        if (body.fingerprint === libFingerprint) return clone(libPayload);
        invalidateLibrary(); // answered for a fingerprint we do not hold
        return storeLibrary(await rawJson(route, options));
      }
      return storeLibrary(body);
    }
    return storeLibrary(await rawJson(route, options));
  }

  /**
   * The one JSON call every consumer uses. Same signature as before the
   * extraction: apiJson(route, {method, body, …}); `body` is JSON-stringified,
   * errors carry `.remediation` (and now `.status`).
   */
  async function apiJson(route, options = {}) {
    const method = String(options.method ?? "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") invalidateLibrary();
    // Only the bare library GET is cached; a caller that brings its own query
    // (a future filter param) bypasses the cache instead of being lied to.
    if (method === "GET" && route === LIBRARY_ROUTE) return libraryJson(route, options);
    return rawJson(route, options);
  }

  // ---- LLM model lists (30 s TTL + in-flight dedup) -----------------------
  // Cache entry: {models, suggested, keySet, fetchedAt, error, pending}.
  // `fetchedAt` is only stamped when a request SETTLES, so without `pending`
  // every values() call during one menu interaction started another fetch.

  const modelCache = new Map();

  function llmModelsCached(provider) {
    return modelCache.get(provider);
  }

  function startModelFetch(provider) {
    const prev = modelCache.get(provider);
    const entry = {
      models: prev?.models ?? [],
      suggested: prev?.suggested ?? [],
      keySet: prev?.keySet ?? null,
      fetchedAt: prev?.fetchedAt ?? 0,
      error: prev?.error ?? null,
      pending: null,
    };
    modelCache.set(provider, entry);
    entry.pending = (async () => {
      let settled;
      try {
        const body = await rawJson(routeWithQuery(LLM_VALIDATE_ROUTE, { provider }));
        settled = {
          models: body.models ?? [],
          suggested: body.suggested ?? [],
          keySet: body.key_set ?? null,
          fetchedAt: now(),
          error: null,
          pending: null,
        };
      } catch (err) {
        // keep the previous suggestions: a curated list is still useful when
        // the backend is down, and `error` is what puts the ⚠ note in the list
        settled = {
          models: [],
          suggested: prev?.suggested ?? [],
          keySet: prev?.keySet ?? null,
          fetchedAt: now(),
          error: err.message,
          pending: null,
        };
      }
      modelCache.set(provider, settled);
      return settled;
    })();
    return entry.pending;
  }

  /** Fresh-enough entry, else one fetch. Never rejects: check `.error`. */
  function llmModels(provider) {
    const entry = modelCache.get(provider);
    if (entry?.pending) return entry.pending;
    if (entry && now() - entry.fetchedAt <= ttlMs) return Promise.resolve(entry);
    return startModelFetch(provider);
  }

  /** Ignore the TTL (a backend switch), but still never duplicate a fetch. */
  function refreshLlmModels(provider) {
    return modelCache.get(provider)?.pending ?? startModelFetch(provider);
  }

  // ---- which cloud backends hold a key ------------------------------------
  // Booleans only — API keys never leave the server (llm_keys_set flags).

  let keysEntry = { keys: {}, fetchedAt: 0, error: null, pending: null };

  function llmKeysCached() {
    return keysEntry;
  }

  function startKeysFetch() {
    const prev = keysEntry;
    const pending = (async () => {
      try {
        const body = await rawJson(SETTINGS_ROUTE);
        keysEntry = { keys: body.llm_keys_set ?? {}, fetchedAt: now(), error: null, pending: null };
      } catch (err) {
        keysEntry = { keys: prev.keys, fetchedAt: now(), error: err.message, pending: null };
      }
      return keysEntry;
    })();
    keysEntry = { ...keysEntry, pending };
    return pending;
  }

  function llmKeys() {
    if (keysEntry.pending) return keysEntry.pending;
    if (keysEntry.fetchedAt && now() - keysEntry.fetchedAt <= ttlMs) {
      return Promise.resolve(keysEntry);
    }
    return startKeysFetch();
  }

  function refreshLlmKeys() {
    return keysEntry.pending ?? startKeysFetch();
  }

  /** Every cloud backend the SERVER knows (seed only until one fetch lands). */
  function cloudBackends() {
    const names = Object.keys(keysEntry.keys ?? {});
    return names.length ? names : [...CLOUD_BACKEND_SEED];
  }

  /** Cloud backends with a stored key — what a dropdown should offer. */
  function keyedCloudBackends() {
    const keys = keysEntry.keys ?? {};
    return cloudBackends().filter((name) => keys[name]);
  }

  // ---- ollama pulls -------------------------------------------------------

  function startPull(model) {
    return apiJson(LLM_PULL_ROUTE, { method: "POST", body: { model, start: true } });
  }

  function pullStatus(model) {
    return apiJson(routeWithQuery(LLM_PULL_ROUTE, { model }));
  }

  return {
    apiJson,
    rawJson,
    invalidateLibrary,
    libraryFingerprint,
    llmModels,
    llmModelsCached,
    refreshLlmModels,
    llmKeys,
    llmKeysCached,
    refreshLlmKeys,
    cloudBackends,
    keyedCloudBackends,
    startPull,
    pullStatus,
  };
}
