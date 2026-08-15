// MRLN Prompt Composer — the History tab: what this installation actually
// rendered, newest first, with the two actions that make a record worth
// keeping — restore it into Compose, or copy the prompt.
//
// THE RECORD, as `promptapi/history.py` writes it (RECORD_FIELDS):
//   {ts, template, profile, seed, mode, selection, variables, format,
//    text_length, conflict_policy, positive, negative, choices, loras, batch?}
// `selection` and `variables` are OBJECTS on disk ({slot_id: token} and
// {name: value}) — the exact dicts pl.compose() consumed, not the node's raw
// text — so restoring needs no re-parsing, only re-splitting: the panel keeps
// `trigger` in its own state field and the remaining variables as kv LINES
// (state.variables is a string), exactly the split the node re-merges before
// composing. Getting that wrong restores a prompt whose {trigger} is empty.
//
// PAGING is keyset and FORWARD ONLY: `next_before` names the next (older)
// page, "" means this was the last one. Walking back therefore means
// remembering the cursors already used, which is what state.history.stack is:
//   stack = [cursor used for page 1 (""), cursor used for page 2, …]
//   cursor = the cursor for the NEXT page, "" when there is none
// so `stack.length` IS the page number, `cursor` alone answers "is there an
// older page", and going back is "pop the current cursor, re-fetch with the
// one under it". No extra state field, no count the server never sent.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js).
import { routeWithQuery } from "./api.js";
import { parseKvLines } from "./util.js";
import { armDestructive, busy, el, loadingNote, mount } from "./dom.js";

// ---- constants -------------------------------------------------------------

export const HISTORY_ROUTE = "/mrln/prompt/history";
export const HISTORY_CLEAR_ROUTE = "/mrln/prompt/history-clear";

/** Records per page. The endpoint caps at 500; a sidebar shows a screenful. */
export const PAGE_SIZE = 25;

/** Positive-prompt excerpt in a row, in characters (the title carries all). */
export const EXCERPT_MAX = 140;

/** Re-entering the tab refetches page 1 when the last load is older than this.
 *  Bounded on purpose: renderHistoryTab is also what a landing page calls, so
 *  an unbounded "always refetch" would loop through its own re-render. */
export const STALE_MS = 4000;

/** history.py's DEFAULT_HISTORY_MONTHS — only a fallback for a payload that
 *  did not carry the setting (an older server). */
export const DEFAULT_HISTORY_MONTHS = 12;

// ---- pure: record → row ----------------------------------------------------

export function formatStamp(ts) {
  // The server writes LOCAL time with microseconds ("2026-08-12T18:07:31.123456")
  // and pages on a lexicographic compare of that exact string. It is therefore
  // shown VERBATIM rather than through Date/toLocaleString: parsing a naive
  // stamp re-interprets it in the browser's timezone and reprints it in a
  // format the cursor does not share — two clocks in one tab.
  const raw = String(ts ?? "");
  const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/.exec(raw);
  if (!match) return { raw, date: "", time: raw, full: raw };
  return { raw, date: match[1], time: match[2], full: `${match[1]} ${match[2]}` };
}

export function excerpt(text, max = EXCERPT_MAX) {
  // One line, never longer than `max` INCLUDING the ellipsis — a row must not
  // grow with the prompt.
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `${flat.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

export function seedOf(record) {
  const seed = Number(record?.seed);
  return Number.isFinite(seed) ? Math.max(0, Math.floor(seed)) : 0;
}

export function batchOf(record) {
  // `batch` is present only when one queue click rendered more than one item.
  const batch = record?.batch;
  if (!batch || typeof batch !== "object") return null;
  const id = String(batch.id ?? "");
  if (!id) return null;
  return {
    id,
    index: Number(batch.index) || 0,
    total: Number(batch.total) || 0,
    kind: String(batch.kind ?? ""),
  };
}

export function toRow(record, index = 0) {
  const stamp = formatStamp(record?.ts);
  const positive = String(record?.positive ?? "");
  return {
    // ts is unique per record by construction (history.py stamps +1µs per
    // batch item), but the index keeps the key stable even for a hand-edited
    // file with duplicates
    key: `${stamp.raw}#${index}`,
    ts: stamp.raw,
    date: stamp.date,
    time: stamp.time,
    template: String(record?.template ?? "") || "(unknown template)",
    profile: String(record?.profile ?? ""),
    seed: seedOf(record),
    positive,
    negative: String(record?.negative ?? ""),
    excerpt: excerpt(positive),
    batch: batchOf(record),
    record: record ?? {},
  };
}

export function groupRows(records) {
  // One queue click = one `batch` id = ONE collapsed row: a 64-item batch
  // exists precisely so the images differ, so every item keeps its own line on
  // disk — but burying a page under one click's items would make the tab
  // useless. Grouping is adjacency-based (a batch is written consecutively and
  // read back reversed); a batch split across a page boundary simply shows the
  // part that is on this page, which is exactly what the page contains.
  const groups = [];
  for (const [index, record] of (records ?? []).entries()) {
    const row = toRow(record, index);
    const last = groups[groups.length - 1];
    if (row.batch && last?.batch?.id === row.batch.id) {
      last.rows.push(row);
      continue;
    }
    groups.push({ key: row.key, batch: row.batch, date: row.date, rows: [row] });
  }
  return groups;
}

export function seedSummary(rows) {
  // 'increment seed' walks the seed per item, 'combinatorial' keeps one seed
  // for the whole product — say which without making the reader open the row.
  const seeds = (rows ?? []).map((row) => row.seed);
  if (!seeds.length) return "";
  const low = Math.min(...seeds);
  const high = Math.max(...seeds);
  return low === high ? `seed ${low}` : `seeds ${low}–${high}`;
}

// ---- pure: record → what Compose has to become -----------------------------

export function selectionMap(selection) {
  // The stored `selection` is the dict pl.compose() consumed; applyKvToRows
  // wants exactly that map. A string is accepted only for robustness (a
  // hand-written or pre-object line), never produced by this pack.
  if (typeof selection === "string") return parseKvLines(selection);
  const map = {};
  for (const [key, value] of Object.entries(selection ?? {})) map[String(key)] = String(value ?? "");
  return map;
}

export function kvLines(map) {
  // The exact inverse of util.parseKvLines — the format state.variables holds.
  return Object.entries(map ?? {})
    .map(([name, value]) => `${name}=${value}`)
    .join("\n");
}

export function splitVariables(variables) {
  // The node merges the trigger widget INTO the variables map before
  // composing ({"trigger": …}); the panel keeps the two apart (state.trigger +
  // state.variables lines) and the preview endpoint re-merges them the same
  // way. Restoring therefore has to split them back, or the trigger field
  // stays empty while 'trigger=x' sits in the variables box — where clearing
  // the (empty) trigger field would not remove it.
  const map = {};
  const source = typeof variables === "string" ? parseKvLines(variables) : (variables ?? {});
  for (const [key, value] of Object.entries(source)) map[String(key)] = String(value ?? "");
  const trigger = map.trigger ?? "";
  delete map.trigger;
  return { trigger, variables: kvLines(map) };
}

export function restorePayload(record) {
  // RESTORE_FIELDS (history.py) → the panel's state names. Defaults mirror the
  // node's widget defaults, so a record from an older/partial line restores as
  // the node would have run it rather than as `undefined`.
  const split = splitVariables(record?.variables);
  return {
    template: String(record?.template ?? ""),
    profile: String(record?.profile || "standard"),
    seed: seedOf(record),
    mode: String(record?.mode || "as configured"),
    selection: selectionMap(record?.selection),
    variables: split.variables,
    trigger: split.trigger,
    format: String(record?.format || "template default"),
    textLength: String(record?.text_length || "template default"),
    conflictPolicy: String(record?.conflict_policy || "negative prevails"),
  };
}

// ---- pure: retention note --------------------------------------------------

export function retentionSettings(body) {
  const enabled = body?.history_enabled;
  const months = Number(body?.history_months ?? DEFAULT_HISTORY_MONTHS);
  return {
    // absent means "an answer that predates the setting" — history.py's own
    // default is ON, so absence must not read as OFF
    history_enabled: enabled === undefined || enabled === null ? true : enabled !== false,
    history_months: Number.isFinite(months) ? Math.max(0, Math.floor(months)) : DEFAULT_HISTORY_MONTHS,
  };
}

export function retentionNote(settings) {
  const months = settings?.history_months ?? DEFAULT_HISTORY_MONTHS;
  const kept =
    months > 0
      ? `the newest ${months} month file(s) are kept, older ones are pruned at startup`
      : "every month file is kept (pruning is off)";
  if (settings?.history_enabled === false) {
    return (
      `Recording is OFF — new renders are not written to history; ${kept}. `
      + "Turn it back on in the Settings tab."
    );
  }
  return (
    `Recording is on — one line per rendered prompt, one per item for a batch; ${kept}. `
    + "Both are settings — change them in the Settings tab."
  );
}

// ---- pure: keyset paging ---------------------------------------------------

export function firstPageRequest() {
  return { stack: [], before: "" };
}

export function nextPageRequest(history) {
  // `cursor` is the previous page's next_before — empty means that page was
  // the last one, and asking again would re-serve it.
  const before = String(history?.cursor ?? "");
  if (!before) return null;
  return { stack: [...(history?.stack ?? [])], before };
}

export function previousPageRequest(history) {
  const stack = [...(history?.stack ?? [])];
  if (stack.length < 2) return null; // already on page 1
  stack.pop(); // the cursor that fetched the page on screen
  const before = stack.pop() ?? ""; // the one that fetched the page before it
  return { stack, before };
}

export function pageLanded(request, body) {
  // The state patch a landed page produces. `cursor` is only kept when the
  // server said there IS more: has_more without a next_before (an empty last
  // page) must not arm the Older button with "".
  const hasMore = body?.has_more === true;
  return {
    records: Array.isArray(body?.records) ? body.records : [],
    cursor: hasMore ? String(body?.next_before ?? "") : "",
    stack: [...(request?.stack ?? []), String(request?.before ?? "")],
    settings: retentionSettings(body),
    loading: false,
    error: null,
  };
}

export function currentPageRequest(history) {
  // pageLanded pushes the cursor that fetched the visible page onto `stack`,
  // so popping it reproduces that exact request. Deleting one record shifts
  // what a keyset page contains, so the page has to be re-fetched rather than
  // patched in place — and re-fetching THIS page keeps the user where they
  // were instead of throwing them back to page 1.
  const stack = [...(history?.stack ?? [])];
  const before = stack.pop() ?? "";
  return { stack, before: String(before) };
}

export function pageNumber(history) {
  return Math.max(1, (history?.stack ?? []).length);
}

export function hasNextPage(history) {
  return !!String(history?.cursor ?? "");
}

export function hasPreviousPage(history) {
  return (history?.stack ?? []).length > 1;
}

// ---- the tab ---------------------------------------------------------------

export function createHistory(hub) {
  const { ctx, state, historyTab } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const applyKvToRows = (...a) => hub.applyKvToRows(...a);
  const confirmDiscardEdits = (...a) => hub.confirmDiscardEdits(...a);
  const rebuildForProfile = (...a) => hub.rebuildForProfile(...a);
  const applyToNode = (...a) => hub.applyToNode(...a);
  const renderComposeTab = (...a) => hub.renderComposeTab(...a);
  const schedulePreview = (...a) => hub.schedulePreview(...a);
  const selectTemplate = (...a) => hub.selectTemplate(...a);
  const switchTab = (...a) => hub.switchTab(...a);

  // Which batch groups the user opened. Lives in the closure, not in state:
  // it is view sugar, and a panel-wide state key would outlive the records it
  // refers to.
  const expanded = new Set();
  let lastLoadAt = 0;

  // ---- data ---------------------------------------------------------------

  async function loadPage(request) {
    if (!request) return; // no such page (first/last) — the button is disabled too
    lastLoadAt = Date.now();
    state.history.loading = true;
    state.history.error = null;
    renderHistoryTab(); // spinner now, not after the round trip
    let body;
    try {
      body = await ctx.apiJson(
        routeWithQuery(HISTORY_ROUTE, { limit: PAGE_SIZE, before: request.before })
      );
    } catch (err) {
      state.history.loading = false;
      state.history.error = err.remediation ? `${err.message} — ${err.remediation}` : err.message;
      renderHistoryTab();
      return;
    }
    Object.assign(state.history, pageLanded(request, body));
    renderHistoryTab();
  }

  async function clearHistory() {
    // Unrecoverable: every month file is deleted. The button arms first
    // (window.confirm throws on the Electron frontend) and only JSON `true`
    // counts server-side.
    try {
      const body = await ctx.apiJson(HISTORY_CLEAR_ROUTE, {
        method: "POST",
        body: { confirm: true },
      });
      const failed = body?.failed ?? [];
      if (failed.length) {
        ctx.toast("error", "History only partly cleared", failed.join("\n"));
      } else {
        ctx.toast("success", "History cleared", `${body?.count ?? 0} month file(s) deleted`);
      }
    } catch (err) {
      ctx.toast("error", "Clear failed", err.message);
      return;
    }
    state.history.stack = [];
    state.history.cursor = "";
    expanded.clear();
    await loadPage(firstPageRequest());
  }

  // ---- actions ------------------------------------------------------------

  async function restore(record) {
    const payload = restorePayload(record);
    if (!payload.template) {
      ctx.toast("error", "Nothing to restore", "this record does not name a template");
      return false;
    }
    // selectTemplate reloads the template FROM DISK — the same discard guard
    // Load-from-node uses, or unsaved edits vanish on a misclick.
    if (!confirmDiscardEdits("history-restore")) return false;
    if (!(await selectTemplate(payload.template))) {
      ctx.toast(
        "error",
        "Template unavailable",
        `'${payload.template}' could not be loaded — it may have been renamed or deleted `
          + "since that render. The Compose tab shows the error and a Retry."
      );
      switchTab("compose"); // that banner is in the compose body, not here
      return false;
    }
    state.seed = payload.seed;
    state.mode = payload.mode;
    state.format = payload.format;
    state.textLength = payload.textLength;
    state.conflictPolicy = payload.conflictPolicy;
    state.trigger = payload.trigger;
    state.variables = payload.variables;
    state.profile = payload.profile;
    if (
      payload.profile !== "standard" &&
      !(payload.profile in (state.detail?.template?.profiles ?? {}))
    ) {
      // keep the value (the record carries it, Apply must round-trip it) but
      // say so once — mirrors Load from node
      ctx.toast(
        "warn",
        "Profile not installed here",
        `That render used '${payload.profile}', which this library does not define — `
          + "the restored render falls back to the standard one."
      );
    }
    rebuildForProfile(payload.profile); // rows/defaults reflect that variant
    // The stored selection is already a MAP (the dict compose() consumed), so
    // it goes into applyKvToRows untouched — parsing it as lines would restore
    // nothing at all.
    applyKvToRows(payload.selection);
    renderComposeTab();
    schedulePreview();
    switchTab("compose");
    ctx.toast(
      "success",
      "Restored from history",
      `${payload.template} · seed ${payload.seed} · ${formatStamp(record?.ts).full}`
    );
    return true;
  }

  /** Restore, then write it straight to the node — the two-step you would
   *  otherwise do by hand, and the reason History exists at all: the render
   *  you are looking at is usually the one you want back in the graph.
   *
   *  Goes through compose's own applyToNode so it inherits the hardened path:
   *  the state is persisted to the user library BEFORE the widgets are
   *  written, and the writes are verified afterwards. */
  async function restoreAndApply(record) {
    if (!(await restore(record))) return; // its own toast named the cause
    await applyToNode();
  }

  function copyViaSelection(text) {
    // Plain http over a LAN (a normal ComfyUI setup) exposes no
    // navigator.clipboard at all, and a denied permission rejects writeText
    // even where it exists. A temporary textarea + execCommand("copy") is the
    // one path that still works there; deprecated, never removed, and a
    // failure only falls through to the toast that says "copy it manually".
    try {
      const area = el(
        "textarea",
        { style: "position:fixed;top:-2000px;left:0;opacity:0", readonly: "" },
        text
      );
      document.body.append(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      return copied;
    } catch {
      return false;
    }
  }

  async function copyPrompt(row) {
    if (!row.positive) {
      ctx.toast("warn", "Nothing to copy", "this record stored an empty positive prompt");
      return;
    }
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(row.positive);
        ctx.toast("success", "Prompt copied", `${row.template} · seed ${row.seed}`);
        return;
      } catch {
        /* denied permission — the manual path below may still work */
      }
    }
    if (copyViaSelection(row.positive)) {
      ctx.toast("success", "Prompt copied", `${row.template} · seed ${row.seed}`);
      return;
    }
    ctx.toast(
      "error",
      "Clipboard unavailable",
      "This frontend refused both clipboard paths (a plain-http page exposes no "
        + "clipboard API). Restore the record instead — the Compose tab shows the "
        + "prompt as selectable text."
    );
  }

  // ---- rendering ----------------------------------------------------------

  function fullText(row) {
    return row.negative ? `${row.positive}\n\nnegative: ${row.negative}` : row.positive;
  }

  function chips(row) {
    return [
      el(
        "span",
        { class: "mrln-chip", title: "the seed this item was drawn with" },
        `seed ${row.seed}`
      ),
      row.profile && row.profile !== "standard"
        ? el("span", { class: "mrln-chip mrln-merged", title: "target profile" }, row.profile)
        : null,
      row.batch
        ? el(
            "span",
            { class: "mrln-chip", title: `batch ${row.batch.kind}` },
            `${row.batch.index}/${row.batch.total}`
          )
        : null,
    ];
  }

  // Opt-OUT, so an install that has never opened Settings still gets the
  // tiles; only an explicit false turns them off.
  const thumbsEnabled = () => state.history.settings?.history_thumbs !== false;

  /** The mini render thumbnail, or null.
   *
   * `loading="lazy"` rather than an IntersectionObserver on purpose: with
   * thousands of rows the browser's own viewport culling is both cheaper and
   * better tested than anything written here, and it costs one attribute.
   * The server answers 404 for a render whose image is not on disk (or was
   * never saved), which is a NORMAL answer — onerror removes the tile so the
   * row closes up instead of showing a broken-image glyph. */
  // A row younger than this is one whose image may still be on its way: the
  // history line is written while the prompt is composed, and the sampler
  // writes the PNG afterwards, so the first fetch can legitimately 404.
  const FRESH_MS = 20 * 60 * 1000;
  const RETRY_MS = [4000, 12000, 30000];

  function rowThumb(row) {
    if (!thumbsEnabled()) return null;
    const src =
      "/mrln/prompt/history-thumb?template="
      + encodeURIComponent(row.template)
      + "&seed="
      + encodeURIComponent(String(row.seed));
    const started = Date.parse(row.ts);
    let attempt = 0;
    const img = el("img", {
      class: "mrln-history-thumb",
      src,
      loading: "lazy",
      decoding: "async",
      alt: "",
      title: "The render this prompt produced",
      onerror: (e) => {
        const tile = e.currentTarget;
        // A row from last week that has no image never will; drop it and stop.
        // A row from a minute ago is probably still rendering, so come back —
        // three times, widening, then give up for good.
        const fresh = Number.isFinite(started) && Date.now() - started < FRESH_MS;
        if (!fresh || attempt >= RETRY_MS.length) {
          tile.remove();
          return;
        }
        const wait = RETRY_MS[attempt++];
        tile.classList.add("mrln-history-thumb-waiting");
        setTimeout(() => {
          if (!tile.isConnected) return; // the tab re-rendered under us
          tile.classList.remove("mrln-history-thumb-waiting");
          tile.src = `${src}&retry=${attempt}`; // a new URL, or the 404 is reused
        }, wait);
      },
    });
    return img;
  }

  function rowCard(row) {
    return el(
      "div",
      { class: "mrln-slot mrln-history-row" },
      rowThumb(row),
      el(
        "div",
        { class: "mrln-history-body" },
        el(
          "div",
          { class: "mrln-history-line" },
          el("span", { class: "mrln-history-time", title: row.ts }, row.time || "—"),
          el("span", { class: "mrln-history-template", title: row.template }, row.template),
          ...chips(row)
        ),
        el(
          "div",
          { class: "mrln-history-excerpt", title: fullText(row) },
          row.excerpt || "(empty prompt)"
        ),
        el(
          "div",
          { class: "mrln-actions" },
          el(
            "button",
            {
              class: "mrln-btn mrln-mini",
              title: "Load this render back into Compose — template, profile, seed, mode, "
                + "picks, variables, format, length and conflict policy",
              onclick: (e) => busy(e.currentTarget, () => restore(row.record)),
            },
            "↩ restore"
          ),
          el(
            "button",
            {
              class: "mrln-btn mrln-mini",
              title:
                "Restore it AND write it straight to the Prompt Template node — the "
                + "same Apply the Compose tab does, so it saves to your user library "
                + "first. Automatic with one Prompt node in the graph; with more than "
                + "one, select the target node first.",
              onclick: (e) => busy(e.currentTarget, () => restoreAndApply(row.record)),
            },
            "⇥ apply"
          ),
          el(
            "button",
            {
              class: "mrln-btn mrln-mini",
              title: "Copy the positive prompt to the clipboard",
              onclick: (e) => busy(e.currentTarget, () => copyPrompt(row)),
            },
            "⧉ copy prompt"
          ),
          el(
            "button",
            {
              class: "mrln-btn mrln-mini mrln-history-drop",
              title:
                "Delete this one record, and the thumbnail cached for it. The "
                + "rendered image itself is not touched. Unrecoverable — click twice.",
              onclick: (e) => {
                // capture: currentTarget is null by the time the armed action
                // runs, which is on the NEXT click
                const button = e.currentTarget;
                armDestructive(button, "Delete this record?", () =>
                  busy(button, () => removeRecords(row, `${row.time} ${row.template}`))
                );
              },
            },
            "✕ delete"
          )
        )
      )
    );
  }

  /** Drop one row, or every row of a batch. Server first, then the list, so a
   *  failure changes nothing on screen. */
  async function removeRecords(rows, what) {
    const list = [].concat(rows);
    try {
      await ctx.apiJson("/mrln/prompt/history-delete", {
        method: "POST",
        // the stamps identify the records; the template/seed pairs are what
        // the server needs to drop the tile cached for each one
        body: {
          ts: list.map((r) => r.ts),
          thumbs: list.map((r) => ({ template: r.template, seed: r.seed })),
          confirm: true,
        },
      });
    } catch (err) {
      ctx.toast("error", list.length > 1 ? "Batch not deleted" : "Record not deleted", err.message);
      return;
    }
    ctx.toast("success", list.length > 1 ? "Batch deleted" : "Record deleted", what);
    // reload the page we are on rather than splicing rows out: the page is
    // keyset-paged, so removing records changes what the page contains
    await loadPage(currentPageRequest(state.history));
  }

  function batchCard(group) {
    // <details> keeps the open/closed state in the DOM; `expanded` keeps it
    // across the re-render a page change causes.
    const head = group.rows[0];
    const box = el(
      "details",
      {
        class: "mrln-history-batch",
        open: expanded.has(group.batch.id) ? "" : null,
        ontoggle: (e) => {
          if (e.currentTarget.open) expanded.add(group.batch.id);
          else expanded.delete(group.batch.id);
        },
      },
      el(
        "summary",
        { class: "mrln-history-line mrln-history-batch-line" },
        // A COLLAPSED batch is exactly the row you want to recognise by its
        // picture, so the summary carries one too. The head row is the batch's
        // first item, whose seed is the node's base seed — the one the saved
        // graph records — so this is the item that can actually be matched.
        rowThumb(head),
        el("span", { class: "mrln-history-time", title: head.ts }, head.time || "—"),
        el("span", { class: "mrln-history-template", title: head.template }, head.template),
        el(
          "span",
          { class: "mrln-chip", title: `one queue click, batch mode '${group.batch.kind}'` },
          `${group.rows.length} of ${group.batch.total} items`
        ),
        el("span", { class: "mrln-chip" }, seedSummary(group.rows)),
        // Deleting a batch takes every row it holds, so the button SAYS how
        // many — "delete" on a collapsed group that quietly removes 64 records
        // is the kind of surprise an arming step alone does not cover.
        el(
          "button",
          {
            class: "mrln-btn mrln-mini mrln-history-drop",
            title:
              `Delete all ${group.rows.length} record(s) of this batch, and the `
              + "thumbnails cached for them. The rendered images are not touched. "
              + "Unrecoverable — click twice.",
            onclick: (e) => {
              // a <summary> click toggles the <details>; this button is inside
              // one, so its click must not also open/close the group
              e.preventDefault();
              e.stopPropagation();
              const button = e.currentTarget;
              armDestructive(button, `Delete all ${group.rows.length}?`, () =>
                busy(button, () =>
                  removeRecords(group.rows, `${group.rows.length} records · ${head.template}`)
                )
              );
            },
          },
          `✕ delete ${group.rows.length}`
        )
      ),
      el("div", { class: "mrln-slot-list mrln-history-items" }, ...group.rows.map(rowCard))
    );
    return box;
  }

  function emptyNote() {
    const enabled = state.history.settings?.history_enabled !== false;
    if (hasPreviousPage(state.history)) {
      return el("div", { class: "mrln-note" }, "No older records on this page.");
    }
    return el(
      "div",
      { class: "mrln-note" },
      enabled
        ? "Nothing recorded yet — the Prompt Template (MRLN) node writes one line per "
          + "rendered prompt (one per item for a batch) every time you queue it."
        : "Nothing recorded — history recording is currently off in the Settings tab."
    );
  }

  function pager() {
    const history = state.history;
    const back = el(
      "button",
      {
        class: "mrln-btn mrln-mini",
        title: "Newer records",
        disabled: hasPreviousPage(history) ? null : "",
        onclick: (e) => busy(e.currentTarget, () => loadPage(previousPageRequest(history))),
      },
      "← Newer"
    );
    const forward = el(
      "button",
      {
        class: "mrln-btn mrln-mini",
        title: "Older records",
        disabled: hasNextPage(history) ? null : "",
        onclick: (e) => busy(e.currentTarget, () => loadPage(nextPageRequest(history))),
      },
      "Older →"
    );
    return el(
      "div",
      { class: "mrln-history-pager" },
      back,
      el(
        "span",
        { class: "mrln-note" },
        `page ${pageNumber(history)} · ${history.records.length} record(s)`
      ),
      forward
    );
  }

  function renderHistoryTab() {
    const history = state.history;
    // Landing on the tab shows CURRENT history: anything rendered since the
    // last look is what the user came for. Bounded by STALE_MS so the
    // re-render this very load triggers cannot fire another one, and only on
    // page 1 — a refetch while paging would silently teleport the user.
    if (!history.loading && !hasPreviousPage(history) && Date.now() - lastLoadAt > STALE_MS) {
      loadPage(firstPageRequest()); // async; sets loading and re-renders
    }
    const groups = groupRows(history.records);
    const list = el("div", { class: "mrln-slot-list" });
    let day = "";
    for (const group of groups) {
      if (group.date && group.date !== day) {
        day = group.date;
        list.append(el("div", { class: "mrln-history-day" }, day));
      }
      list.append(group.batch && group.rows.length > 1 ? batchCard(group) : rowCard(group.rows[0]));
    }
    // Conditional children stay null here and are filtered by dom.js's mount()
    // — the raw replaceChildren stringifies a null into a "null" text node.
    // Refresh and Clear act on the LIST, so they sit directly above it rather
    // than riding the title line at the very top edge of the tab. The head is
    // then a heading and nothing else, which is what every other tab opens
    // with (Compose's TEMPLATE bar, De-compose's IMAGE → TEMPLATE).
    const historyActions = el(
      "div",
      { class: "mrln-actions mrln-history-actions" },
      el(
        "button",
        {
          class: "mrln-btn",
          title: "Reload the newest page",
          onclick: (e) => busy(e.currentTarget, () => loadPage(firstPageRequest())),
        },
        "Refresh"
      ),
      el(
        "button",
        {
          class: "mrln-btn",
          title: "Delete every history month file — unrecoverable",
          onclick: (e) => {
            // capture: currentTarget is null once the event finished, and
            // the armed action runs on the NEXT click
            const button = e.currentTarget;
            armDestructive(button, "Really delete every record?", () =>
              busy(button, clearHistory)
            );
          },
        },
        "Clear history"
      )
    );
    const parts = [
      el(
        "div",
        { class: "mrln-history-head" },
        el("div", { class: "mrln-tree-head" }, "Generation history")
      ),
      el(
        "div",
        { class: "mrln-note" },
        history.settings ? retentionNote(history.settings) : "Reading the retention settings…"
      ),
      history.error
        ? el(
            "div",
            {},
            el("div", { class: "mrln-error" }, `History unavailable: ${history.error}`),
            el(
              "div",
              { class: "mrln-actions" },
              el(
                "button",
                {
                  class: "mrln-btn",
                  onclick: (e) => busy(e.currentTarget, () => loadPage(firstPageRequest())),
                },
                "Retry"
              )
            )
          )
        : null,
      history.loading ? loadingNote("Loading history…") : null,
      // above the list, and above the empty note too — Refresh is exactly what
      // you reach for when the list is empty
      historyActions,
      !history.loading && !history.error && !groups.length ? emptyNote() : null,
      groups.length ? list : null,
      groups.length || hasPreviousPage(history) ? pager() : null,
    ];
    mount(historyTab, ...parts.filter(Boolean));
  }

  return { renderHistoryTab };
}
