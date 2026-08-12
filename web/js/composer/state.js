// MRLN Prompt Composer — the panel's state store: the ONE state object, the
// state-bound wrappers over util.js, and the loaders/savers that own state
// (library, template detail, item pools, per-profile variants, preview).
//
// Two layers of state per template:
//   rawData  — editable working copy of the template FILE (the meta-prompt:
//              prefix/suffix prose, slots, labels, order, variants). Edits
//              preview live via POST /preview {template_data} and persist
//              only on Save (user tier, copy-on-write over factory).
//   rows     — the current per-slot picks (fixed item or random[@seed]).
//              Serialized as selection lines (the node's persistence
//              format) relative to the template's defaults; Save bakes
//              them in as the new defaults.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). Everything below is a
// declaration; all mutable state lives inside createState()/createStore(), so a
// second import cannot see it.
import * as util from "./util.js";
import { diffProfileOverrides, overrideTweakCount, parseToken, structuralDrift } from "./util.js";
import { el, loadingNote, mount } from "./dom.js";

/** The panel's single state object — created once, shared by every module. */
export function createState() {
  const state = {
    library: null, // GET /library body
    slug: null, // selected template slug
    detail: null, // GET /template body: {template, pools, raw, tier}
    rawData: null, // editable working copy: base ⊕ active profile's overrides
    baseRaw: null, // pristine detail.raw — the 'standard' state variant diffs compare to
    orderIds: [], // combined render order: shared slot ids + "@variant"
    modified: false, // rawData differs from the file on disk
    variant: null, // active variant name | "random" | null
    mode: "as configured",
    seed: 0,
    format: "template default",
    conflictPolicy: "negative prevails",
    textLength: "template default",
    trigger: "",
    variables: "",
    rows: new Map(), // slot id -> {random, seed, item}
    labelEdit: new Set(), // slot ids with the label editor open
    muted: new Set(), // audition only: slot ids (or "@variant") muted in preview
    soloed: new Set(), // audition only: solo set — non-empty means ONLY these render
    lastPreview: null,
    previewNo: 0,
    templateNo: 0, // selectTemplate supersede token, mirrors previewNo
    libraryError: null, // last /library failure — tabs render it with a Retry
    previewTimer: null,
    choicesOpen: false,
    negativeOpen: false,
    tab: "compose",
    // De-compose tab state — runNo/modelGen are supersede tokens (mirror
    // previewNo), running keeps the button disabled across re-renders
    decompose: { text: "", type: "", report: null, plans: [], runNo: 0, modelGen: 0, running: false },
    // Image intake, above the De-compose box: the extraction result and which
    // of the two paths (verbatim / decompose) the user is looking at. `runNo`
    // supersedes like every other async token here; `extraction` is the whole
    // server payload, passed back untouched on the second call.
    intake: { extraction: null, source: "", url: "", busy: false, runNo: 0, error: null },
    // Compose tab: the "Optimize for…" comparison. `profile` is the target
    // being compared against (empty = comparison closed), `result` holds the
    // two renders once both have landed.
    optimize: { profile: "", result: null, runNo: 0, busy: false },
    // History tab: one page of records plus the keyset cursor stack that walks
    // back to page 1 (the endpoint pages forward only).
    history: { records: [], cursor: "", stack: [], loading: false, error: null, settings: null },
    // Library tab: cards instead of rows, and the counter that busts the
    // browser's thumbnail cache after a set/reset (the URL is otherwise
    // identical and Last-Modified would keep the old tile on screen).
    grid: false,
    thumbEpoch: 0,
    libGroups: new Set(), // Library tab: expanded top-level slug groups
    nestOpen: new Set(), // nested-draw branches the user explicitly opened/closed
  };
  return state;
}

/**
 * Everything that reads or writes that state object. `hub` carries the state,
 * the ctx injected by prompt_composer.js and the other modules' exports.
 */
export function createStore(hub) {
  const { ctx, state, composeTab } = hub;
  // Cross-module calls ride the hub so the modules can be built in any order —
  // the single closure gave this late binding for free via function hoisting.
  const renderComposeTab = (...a) => hub.renderComposeTab(...a);
  const renderDecomposeTab = (...a) => hub.renderDecomposeTab(...a);
  const renderLibraryTab = (...a) => hub.renderLibraryTab(...a);
  const renderNested = (...a) => hub.renderNested(...a);
  const renderPreview = (...a) => hub.renderPreview(...a);
  const renderLoraBanner = (...a) => hub.renderLoraBanner(...a);
  const refreshLoraBanner = (...a) => hub.refreshLoraBanner(...a);
  const switchTab = (...a) => hub.switchTab(...a);

  // ---- two-step confirmations ----------------------------------------------
  // window.confirm/window.prompt throw on the Desktop (Electron) frontend,
  // so destructive actions confirm by ARMING instead: the first attempt
  // arms for ~4s, repeating it within the window executes.

  const armedActions = new Map(); // key -> arm deadline (ms epoch)

  function confirmTwoStep(key, title, detail) {
    if ((armedActions.get(key) ?? 0) > Date.now()) {
      armedActions.delete(key);
      return true;
    }
    armedActions.set(key, Date.now() + 4000);
    ctx.toast("warn", title, detail);
    return false;
  }

  function confirmDiscardEdits(key) {
    if (!state.modified) return true;
    return confirmTwoStep(
      `discard:${key}`,
      "Unsaved template changes",
      "Repeat the action within 4s to discard them — or Save first."
    );
  }

  // ---- data loading --------------------------------------------------------

  function libraryErrorNote(message) {
    // The panel is a singleton and boots loadLibrary exactly once — a dead
    // load must offer recovery, not a blank tab until a browser reload.
    return el(
      "div",
      {},
      el("div", { class: "mrln-error" }, `Library unavailable: ${message}`),
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn", onclick: () => loadLibrary(false) }, "Retry")
      )
    );
  }

  async function loadLibrary(keepSelection = true) {
    if (!state.library) {
      mount(composeTab, loadingNote("Loading prompt library…"));
    }
    try {
      state.library = await ctx.apiJson("/mrln/prompt/library");
    } catch (err) {
      if (state.library) {
        // a failed RELOAD must not nuke a working panel — keep loaded data
        ctx.toast("error", "Library reload failed", err.message);
        return;
      }
      state.libraryError = err.message;
      mount(composeTab, libraryErrorNote(err.message));
      if (state.tab === "library") renderLibraryTab();
      if (state.tab === "decompose") renderDecomposeTab();
      return;
    }
    state.libraryError = null;
    const slugs = state.library.templates.map((t) => t.slug);
    if (!keepSelection || !slugs.includes(state.slug)) state.slug = slugs[0] ?? null;
    if (state.slug && !state.detail) await selectTemplate(state.slug);
    else renderComposeTab();
    if (state.tab === "library") renderLibraryTab();
    if (state.tab === "decompose") renderDecomposeTab();
  }

  async function refreshDetail() {
    // Library edits change pools/defaults under the loaded template —
    // re-fetch the actual data without nuking the user's current picks.
    if (!state.slug) return;
    // Supersede guard, same token as selectTemplate: refreshDetail fires from
    // section/profile saves, LoRA heals and deletes, so a template switch
    // mid-flight must not let the OLD slug's body overwrite the new
    // template's detail (and, when unmodified, its baseRaw/rawData).
    const slug = state.slug;
    const no = state.templateNo;
    try {
      const detail = await ctx.apiJson(
        `/mrln/prompt/template?slug=${encodeURIComponent(slug)}`
      );
      if (no !== state.templateNo || slug !== state.slug) return; // newer pick owns the tab
      state.detail = detail;
      if (!state.modified) {
        state.baseRaw = structuredClone(state.detail.raw);
        state.rawData = effectiveRaw(state.profile);
        state.loadedLabel = state.rawData.label ?? null;
        state.orderIds = syncOrderIds();
      }
      renderComposeTab();
      schedulePreview();
      refreshLoraBanner(state.slug); // a library edit can add, repair or drop a LoRA item
    } catch {
      if (no !== state.templateNo || slug !== state.slug) return;
      schedulePreview(); // stale detail is survivable; the preview shows truth
    }
  }

  function applyItemRenames(sectionSlug, renames) {
    // Mirror of the server-side rewrite, applied to the loaded draft and
    // the current picks so the compose tab follows the rename live.
    if (!Object.keys(renames).length || !state.rawData) return;
    const fix = (ref, token) => {
      if (!token || !ref) return null;
      let rel = null;
      if (ref === sectionSlug) rel = "";
      else if (sectionSlug.startsWith(ref + "/")) rel = sectionSlug.slice(ref.length + 1) + "/";
      else return null;
      for (const [oldName, newName] of Object.entries(renames)) {
        for (const prefix of [rel, `${ref}/${rel}`]) {
          if (token === `${prefix}${oldName}`) return `${prefix}${newName}`;
        }
      }
      return null;
    };
    const allSlots = [
      ...(state.rawData.slots ?? []),
      ...(state.rawData.variants ?? []).flatMap((v) => v.slots ?? []),
      ...(state.baseRaw?.slots ?? []),
      ...(state.baseRaw?.variants ?? []).flatMap((v) => v.slots ?? []),
    ];
    for (const slot of allSlots) {
      const fixedDefault = fix(slot.ref, slot.default);
      if (fixedDefault) slot.default = fixedDefault; // disk already matches
      const row = state.rows.get(slot.id);
      if (row && !row.random) {
        const fixedPick = fix(slot.ref, row.item);
        if (fixedPick) row.item = fixedPick;
      }
    }
  }

  // ---- per-profile template variants ---------------------------------------
  // A template can carry profiles.<name>.overrides: a sparse diff vs the
  // standard render (prefix/suffix/negative/variant_default + slot default/
  // emphasis). Editing with a Target profile selected edits THAT variant;
  // Save stores only the diff, the base file stays untouched — 'standard'
  // is always the way back. Mirrors the trainer's family/definitions split.

  function overridesFor(profileName, raw = state.baseRaw) {
    return util.overridesFor(profileName, raw);
  }

  function effectiveRaw(profileName) {
    return util.effectiveRaw(profileName, state.baseRaw);
  }

  function rebuildForProfile(profileName) {
    // Re-derive the working copy from base ⊕ overrides — only safe when
    // there are no unsaved edits (callers guard on state.modified).
    state.rawData = effectiveRaw(profileName);
    state.loadedLabel = state.rawData.label ?? null;
    state.muted = new Set();
    state.soloed = new Set();
    initRowsFromRaw();
  }

  function setTargetProfile(name) {
    state.profile = name;
    if (!state.modified) rebuildForProfile(name);
    renderComposeTab();
    schedulePreview();
  }

  async function revertProfileTweaks() {
    // Confirmation happens via the armed ↺ button — window.confirm throws
    // on the Desktop (Electron) frontend.
    const profile = state.profile ?? "standard";
    const ov = overridesFor(profile);
    if (!ov || !state.slug) return;
    const data = structuredClone(state.baseRaw);
    delete data.profiles[profile].overrides;
    if (!Object.keys(data.profiles[profile]).length) delete data.profiles[profile];
    if (!Object.keys(data.profiles).length) delete data.profiles;
    data.version = 1;
    try {
      await ctx.apiJson("/mrln/prompt/save-template", {
        method: "POST",
        body: { slug: state.slug, data },
      });
    } catch (err) {
      ctx.toast("error", "Revert failed", err.message);
      return;
    }
    ctx.toast("success", "Profile tweaks removed", `${state.slug} · ${profile} = standard again`);
    const keep = profile;
    await loadLibrary();
    await selectTemplate(state.slug);
    setTargetProfile(keep);
  }

  async function selectTemplate(slug) {
    // Supersede guard (mirrors previewNo): out-of-order responses from fast
    // template switching must not land — and state.slug commits only AFTER
    // a successful fetch, so a failed load can never leave slug pointing at
    // one template while rawData holds another (Save would then overwrite
    // the wrong file). Returns true when this call took effect.
    const no = ++state.templateNo;
    if (!state.detail) {
      mount(composeTab, loadingNote(`Loading '${slug}'…`));
    }
    let detail;
    try {
      detail = await ctx.apiJson(
        `/mrln/prompt/template?slug=${encodeURIComponent(slug)}`
      );
    } catch (err) {
      if (no !== state.templateNo) return false; // a newer pick owns the tab
      const banner = el(
        "div",
        { class: "mrln-error" },
        `Cannot load '${slug}': ${err.message} `,
        el(
          "button",
          { class: "mrln-btn mrln-mini", onclick: () => selectTemplate(slug) },
          "Retry"
        )
      );
      if (state.rawData) {
        // previous template stays fully usable (combo, footer) — the failed
        // slug was never committed to state
        renderComposeTab();
        composeTab.prepend(banner);
      } else {
        mount(composeTab, banner);
      }
      return false;
    }
    if (no !== state.templateNo) return false; // superseded by a newer pick
    state.slug = slug;
    state.detail = detail;
    state.baseRaw = structuredClone(state.detail.raw);
    state.rawData = structuredClone(state.detail.raw);
    state.loadedLabel = state.rawData.label ?? null;
    state.modified = false;
    state.profile = "standard"; // profiles are per-template — reset on switch
    state.labelEdit = new Set();
    state.muted = new Set();
    state.soloed = new Set();
    initRowsFromRaw();
    state.lastPreview = null;
    renderLoraBanner(null); // the previous template's missing files must not linger
    renderComposeTab();
    schedulePreview();
    refreshLoraBanner(slug, no); // fills in when the file scan lands
    return true;
  }

  function initRowsFromRaw() {
    // Rows + M/S from the FILE: Apply bakes mutes as default "off" (and a
    // muted variant block as variant_default "off"), so the applied state
    // survives any workflow-serialization loss — the file is the truth.
    state.orderIds = syncOrderIds();
    state.rows = new Map();
    for (const slot of allSlots()) {
      const token = slot.default ?? "random";
      if (token === "off") {
        state.muted.add(slot.id);
        state.rows.set(slot.id, parseToken("random"));
      } else {
        state.rows.set(slot.id, parseToken(token));
      }
    }
    const variants = state.rawData.variants ?? [];
    if (!variants.length) {
      state.variant = null;
    } else if ((state.rawData.variant_default ?? "") === "off") {
      state.muted.add("@variant");
      state.variant = variants[0].name;
    } else {
      state.variant = state.rawData.variant_default || variants[0].name;
    }
  }

  function syncOrderIds() {
    return util.syncOrderIds(state.rawData);
  }

  function allSlots() {
    return util.allSlots(state.rawData);
  }

  const poolReqs = new Map(); // ref -> in-flight /items promise
  const poolFailedAt = new Map(); // ref -> ms epoch of the last failure
  const POOL_RETRY_MS = 30000;

  async function ensurePool(ref, { force = false } = {}) {
    // Three guards, because the naive version had three holes: the cache only
    // saw COMPLETED fetches (every child row of every renderNested pass fired
    // its own identical GET), a failure cached [] — truthy, so the guard below
    // never refetched and the slot showed only 'random' for the life of the
    // loaded template — and retrying on every render would storm a dead
    // endpoint with a toast each time. So: cache successes only, share the
    // in-flight promise, and back off after a failure (a user-initiated call
    // passes force to retry immediately).
    if (state.detail.pools[ref]) return;
    const pending = poolReqs.get(ref);
    if (pending) return pending;
    if (!force && Date.now() - (poolFailedAt.get(ref) ?? 0) < POOL_RETRY_MS) return;
    const detail = state.detail; // the detail this request was issued for
    const request = (async () => {
      try {
        const body = await ctx.apiJson(`/mrln/prompt/items?ref=${encodeURIComponent(ref)}`);
        detail.pools[ref] = body.items;
        poolFailedAt.delete(ref);
      } catch (err) {
        delete detail.pools[ref]; // never cache a failure
        poolFailedAt.set(ref, Date.now());
        ctx.toast("error", `Cannot load items for '${ref}'`, err.message);
      } finally {
        poolReqs.delete(ref);
      }
    })();
    poolReqs.set(ref, request);
    return request;
  }

  // ---- mute / solo audition (preview-only, DAW-style) ----------------------
  // Seeding is per-slot, so the surviving sections draw exactly the same
  // items — muting isolates the pure textual impact of a section.

  function auditionActive() {
    return util.auditionActive(state.muted, state.soloed);
  }

  function slotAudible(id, isVariantSlot) {
    return util.slotAudible(state.muted, state.soloed, id, isVariantSlot);
  }

  function variantBlockAudible() {
    return util.variantBlockAudible(state.rawData, state.muted, state.soloed);
  }

  // ---- selection lines (the node persistence format) -----------------------

  function buildSelectionLines() {
    return util.buildSelectionLines(state);
  }

  function applyKvToRows(map) {
    const offRe = /^(?:🔇 )?off$/;
    if (map.variant && (state.rawData.variants ?? []).length) {
      if (offRe.test(map.variant.trim())) {
        state.muted.add("@variant");
      } else {
        // an explicit pick overrides a baked "off" variant_default — the
        // node renders it, so the panel must un-mute to match
        state.muted.delete("@variant");
        state.variant = map.variant;
      }
    }
    for (const slot of allSlots()) {
      const token = map[slot.id];
      if (token === undefined) continue;
      if (offRe.test(token.trim())) {
        state.muted.add(slot.id);
      } else {
        // explicit selection lines override an "off" default server-side —
        // mirror that here or Apply would silently drop the pick
        state.muted.delete(slot.id);
        state.rows.set(slot.id, parseToken(token));
      }
    }
    for (const [key, token] of Object.entries(map)) {
      if (!key.includes(".")) continue; // nested pins from the node
      const row = offRe.test(token.trim())
        ? { random: false, seed: "", item: "off" }
        : parseToken(token);
      row.touched = true;
      state.rows.set(key, row);
    }
  }

  // ---- draft / save payloads ----------------------------------------------

  function buildDraftData() {
    return util.buildDraftData(state);
  }

  function buildSaveData() {
    return util.buildSaveData(state);
  }

  async function saveTemplate(slug, { asNew = false } = {}) {
    const profile = state.profile ?? "standard";
    let data;
    let savedNote = `${slug} (user library)`;
    if (profile !== "standard" && !asNew && slug === state.slug && state.baseRaw) {
      // Variant save: the base file keeps its standard state — only the
      // diff vs standard lands under profiles.<name>.overrides.
      const effective = buildSaveData();
      if (structuralDrift(effective, state.baseRaw)) {
        // the sparse diff cannot carry these — refuse instead of dropping
        // them behind a success toast (the edits stay in the working copy)
        ctx.toast(
          "error",
          "Structural edits don't fit a profile variant",
          `Added/removed/reordered slots or label edits cannot ride the '${profile}' `
            + "diff — switch Target profile to 'standard' to save them into the "
            + "base template, or use Save as…. Nothing was saved."
        );
        return false;
      }
      data = structuredClone(state.baseRaw);
      data.version = 1;
      const ov = diffProfileOverrides(effective, state.baseRaw);
      data.profiles = data.profiles ?? {};
      if (ov) {
        data.profiles[profile] = { ...(data.profiles[profile] ?? {}), overrides: ov };
        savedNote = `${slug} · '${profile}' variant (${overrideTweakCount(ov)} tweak(s) vs standard)`;
      } else if (data.profiles[profile]?.overrides) {
        delete data.profiles[profile].overrides;
        if (!Object.keys(data.profiles[profile]).length) delete data.profiles[profile];
        savedNote = `${slug} · '${profile}' now matches standard — stored tweaks removed`;
      }
    } else {
      // Standard save (or save-as fork: the CURRENT variant state becomes
      // the new template's standard).
      data = buildSaveData();
      if (asNew && profile !== "standard" && data.profiles?.[profile]?.overrides) {
        // the fork's standard IS this variant — carrying the diff too would
        // just restate it
        delete data.profiles[profile].overrides;
        if (!Object.keys(data.profiles[profile]).length) delete data.profiles[profile];
      }
    }
    if (asNew && slug !== state.slug && data.label && data.label === state.loadedLabel) {
      // Save-as under a new slug: an inherited label would masquerade as the
      // source template in every picker — drop it so the display name derives
      // from the new slug. A label the author typed themselves is kept.
      delete data.label;
    }
    try {
      await ctx.apiJson("/mrln/prompt/save-template", {
        method: "POST",
        body: { slug, data },
      });
    } catch (err) {
      ctx.toast("error", "Save failed", err.message);
      return false;
    }
    ctx.toast("success", "Template saved", savedNote);
    ctx.refreshCombos();
    await loadLibrary();
    await selectTemplate(slug);
    // selectTemplate resets to standard — keep tuning the variant the user
    // was on (also keeps Apply-to-node writing the right profile widget).
    if (profile !== "standard" && !asNew) setTargetProfile(profile);
    return true;
  }

  async function askString(title, message, defaultValue = "") {
    if (ctx.dialog?.prompt) return await ctx.dialog.prompt({ title, message, defaultValue });
    return window.prompt(`${title}\n${message}`, defaultValue);
  }

  async function newTemplate() {
    // Net-new composition: create a blank user-tier template and drop into
    // the normal compose flow — '+ Add section' builds it up, Save persists.
    if (!confirmDiscardEdits("new-template")) return; // it ends in selectTemplate
    const slug = await askString(
      "New template",
      "Slug for the new template (folder/name, lowercase-kebab — e.g. 'my/street-scene'):",
      ""
    );
    if (!slug?.trim()) return;
    const clean = slug.trim().toLowerCase().replace(/\s+/g, "-");
    if ((state.library?.templates ?? []).some((t) => t.slug === clean)) {
      ctx.toast(
        "error",
        "Template exists",
        `'${clean}' is already in the library — pick another slug or edit it directly`
      );
      return;
    }
    try {
      await ctx.apiJson("/mrln/prompt/save-template", {
        method: "POST",
        body: { slug: clean, data: { version: 1, slots: [] } },
      });
    } catch (err) {
      ctx.toast("error", "Cannot create template", err.message);
      return;
    }
    ctx.toast(
      "success",
      "Template created",
      `${clean} — add sections below, set prefix/suffix, then Save`
    );
    ctx.refreshCombos();
    await loadLibrary();
    await selectTemplate(clean);
    switchTab("compose");
  }

  // ---- preview -------------------------------------------------------------

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(doPreview, 300);
  }

  async function doPreview() {
    if (!state.slug || !state.rawData) return;
    const no = ++state.previewNo;
    const body = {
      template: state.slug,
      seed: state.seed,
      mode: state.mode,
      selection: buildSelectionLines(),
      variables: state.variables,
      trigger: state.trigger,
      format: state.format,
      conflict_policy: state.conflictPolicy,
      text_length: state.textLength,
      profile: state.profile ?? "standard",
    };
    if (state.modified) body.template_data = buildDraftData();
    let preview;
    try {
      preview = await ctx.apiJson("/mrln/prompt/preview", { method: "POST", body });
    } catch (err) {
      if (no === state.previewNo) renderPreview(null, err);
      return;
    }
    if (no !== state.previewNo) return; // a newer request superseded this one
    state.lastPreview = preview;
    renderPreview(preview, null);
    renderNested();
  }

  // ---- what Apply has to persist -------------------------------------------

  function appliedStateDiffers() {
    return util.appliedStateDiffers(state);
  }

  return {
    allSlots,
    appliedStateDiffers,
    applyItemRenames,
    applyKvToRows,
    askString,
    auditionActive,
    buildDraftData,
    buildSaveData,
    buildSelectionLines,
    confirmDiscardEdits,
    confirmTwoStep,
    doPreview,
    effectiveRaw,
    ensurePool,
    initRowsFromRaw,
    libraryErrorNote,
    loadLibrary,
    newTemplate,
    overridesFor,
    rebuildForProfile,
    refreshDetail,
    revertProfileTweaks,
    saveTemplate,
    schedulePreview,
    selectTemplate,
    setTargetProfile,
    slotAudible,
    syncOrderIds,
    variantBlockAudible,
  };
}
