// MRLN Prompt Composer — panel implementation. ES module with NO top-level
// side effects (ComfyUI auto-loads every js file in WEB_DIRECTORY; the module
// cache makes that harmless). All computation happens server-side via
// /mrln/prompt/*; this file only moves state between DOM, endpoints, and node
// widgets. The pure helpers live in ./composer/util.js and are unit-tested by
// tests/js/util.test.mjs; the thin same-named wrappers below bind them to
// this panel's state object.
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

import * as util from "./composer/util.js";

const {
  bundleFilename,
  combineItem,
  defaultPlan,
  diffProfileOverrides,
  downloadableAir,
  isCombineItem,
  jsSlugify,
  loraKey,
  loraProgressText,
  missingLoraRows,
  moveInArray,
  overrideTweakCount,
  parseKvLines,
  parseToken,
  structuralDrift,
} = util;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (value !== undefined && value !== null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat(2)) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const REF_RE = /(?<!\{)\{([A-Za-z_][A-Za-z0-9_-]*)\}(?!\})/g;

function placeMenu(anchor, menu) {
  // FIXED positioning escapes the panel's overflow clipping (the menu was
  // being cut at the scroll container's edge, not the window's); flip
  // above the anchor when the window bottom is tight.
  const rect = anchor.getBoundingClientRect();
  const below = window.innerHeight - rect.bottom;
  const flipUp = below < 220 && rect.top > below;
  menu.style.position = "fixed";
  // content-sized: at least as wide as the anchor, growing up to 560px —
  // leftward when the panel hugs the window's right edge
  menu.style.width = "max-content";
  menu.style.minWidth = `${rect.width}px`;
  const spaceRight = window.innerWidth - rect.left - 12;
  if (spaceRight < 360) {
    menu.style.left = "auto";
    menu.style.right = `${window.innerWidth - rect.right}px`;
    menu.style.maxWidth = `${Math.max(rect.width, Math.min(560, rect.right - 12))}px`;
  } else {
    menu.style.right = "auto";
    menu.style.left = `${rect.left}px`;
    menu.style.maxWidth = `${Math.max(rect.width, Math.min(560, spaceRight))}px`;
  }
  if (flipUp) {
    menu.style.top = "auto";
    menu.style.bottom = `${window.innerHeight - rect.top}px`;
    menu.style.maxHeight = `${Math.max(120, rect.top - 16)}px`;
  } else {
    menu.style.bottom = "auto";
    menu.style.top = `${rect.bottom}px`;
    menu.style.maxHeight = `${Math.max(120, below - 16)}px`;
  }
  menu.classList.toggle("mrln-menu-up", flipUp);
}

function validateRefs(field, knownNames) {
  // red border when the text references a {name} that nothing provides
  const unknown = [...new Set([...field.value.matchAll(REF_RE)].map((m) => m[1]))].filter(
    (name) => !knownNames().has(name)
  );
  field.classList.toggle("mrln-input-error", unknown.length > 0);
  if (unknown.length) {
    field.title = `unknown reference {${unknown.join("}, {")}} — type '{' to pick from `
      + "what is available";
  } else if (field.dataset.mrlnBaseTitle !== undefined) {
    field.title = field.dataset.mrlnBaseTitle;
  }
  return unknown;
}

function braceAssist(field, getOptions, onPick) {
  // Typing '{' opens a picker over everything referencable at the caret;
  // choosing inserts the name and closes the brace. Returns a wrapper to
  // mount instead of the bare field (the menu anchors to it).
  const menu = el("div", { class: "mrln-brace-menu", style: "display:none" });
  const wrap = el("span", { class: "mrln-assist" }, field, menu);
  const onScroll = (e) => {
    if (!menu.contains(e.target)) hide(); // fixed menus must not desync
  };
  const hide = () => {
    if (menu.style.display !== "none") window.removeEventListener("scroll", onScroll, true);
    menu.style.display = "none";
  };
  const openBrace = () => {
    // last unclosed, unescaped '{' before the caret; returns {pos, partial}
    const caret = field.selectionStart ?? 0;
    const value = field.value;
    for (let i = caret - 1; i >= 0; i--) {
      const ch = value[i];
      if (ch === "}") return null;
      if (ch === "{") {
        if (value[i - 1] === "{") return null; // '{{' literal escape
        const partial = value.slice(i + 1, caret);
        return /^[A-Za-z0-9_-]*$/.test(partial) ? { pos: i, partial } : null;
      }
    }
    return null;
  };
  const refresh = () => {
    const at = openBrace();
    if (!at) {
      hide();
      return;
    }
    const lower = at.partial.toLowerCase();
    const options = getOptions().filter((o) => o.name.toLowerCase().startsWith(lower));
    if (!options.length) {
      hide();
      return;
    }
    menu.replaceChildren(
      ...options.slice(0, 14).map((option) =>
        el(
          "div",
          {
            class: "mrln-brace-item",
            onmousedown: (e) => {
              e.preventDefault(); // beat the blur
              field.setRangeText(`${option.name}}`, at.pos + 1, field.selectionStart, "end");
              hide();
              onPick?.(option);
              field.dispatchEvent(new Event("input", { bubbles: false }));
              field.focus();
            },
          },
          option.name,
          option.hint ? el("span", { class: "mrln-slug" }, ` ${option.hint}`) : null
        )
      )
    );
    if (menu.style.display === "none") window.addEventListener("scroll", onScroll, true);
    menu.style.display = "";
    placeMenu(field, menu);
  };
  field.addEventListener("input", refresh);
  field.addEventListener("click", refresh);
  field.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  });
  field.addEventListener("blur", () => setTimeout(hide, 150));
  return wrap;
}

function tierChip(tier) {
  if (!tier) return null;
  return el("span", { class: `mrln-chip mrln-${tier}` }, tier);
}

export function createComposerPanel(root, ctx) {
  root.classList.add("mrln-composer");

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
    decompose: { text: "", type: "", report: null, plans: [] }, // De-compose tab state
    libGroups: new Set(), // Library tab: expanded top-level slug groups
    nestOpen: new Set(), // nested-draw branches the user explicitly opened/closed
  };

  // ---- skeleton ------------------------------------------------------------

  const composeTab = el("div", { class: "mrln-tab-body" });
  const decomposeTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  const libraryTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  const settingsTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  const tabNames = ["compose", "decompose", "library", "settings"];
  const tabBodies = {
    compose: composeTab,
    decompose: decomposeTab,
    library: libraryTab,
    settings: settingsTab,
  };
  const tabButtons = el(
    "div",
    { class: "mrln-tabs" },
    el("button", { class: "mrln-active", onclick: () => switchTab("compose") }, "Compose"),
    el("button", { onclick: () => switchTab("decompose") }, "De-compose"),
    el("button", { onclick: () => switchTab("library") }, "Library"),
    el("button", { onclick: () => switchTab("settings") }, "Settings")
  );
  root.replaceChildren(tabButtons, composeTab, decomposeTab, libraryTab, settingsTab);

  function switchTab(name) {
    state.tab = name;
    for (const tab of tabNames) tabBodies[tab].style.display = tab === name ? "" : "none";
    tabButtons.querySelectorAll("button").forEach((button, i) => {
      button.classList.toggle("mrln-active", tabNames[i] === name);
    });
    if (name === "library") renderLibraryTab();
    if (name === "decompose") renderDecomposeTab();
    if (name === "settings") renderSettingsTab();
  }

  // Persistent element so markModified never re-renders (a re-render would
  // steal focus from the textarea the user is typing in).
  const modifiedNote = el(
    "div",
    { class: "mrln-note mrln-modified", style: "display:none" },
    "● unsaved template changes — Save writes them to your user library"
  );

  function markModified() {
    state.modified = true;
    modifiedNote.style.display = "";
  }

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

  function armDestructive(button, reallyLabel, action) {
    // Button flavor: the first click relabels the button to the question and
    // arms it; the second click (any call while armed) runs the stored
    // action. Auto-disarms after ~4s.
    if (button.mrlnArmed) {
      clearTimeout(button.mrlnArmed.timer);
      button.textContent = button.mrlnArmed.label;
      button.classList.remove("mrln-armed");
      const run = button.mrlnArmed.action;
      button.mrlnArmed = null;
      run();
      return;
    }
    button.mrlnArmed = {
      label: button.textContent,
      action,
      timer: setTimeout(() => {
        button.textContent = button.mrlnArmed.label;
        button.classList.remove("mrln-armed");
        button.mrlnArmed = null;
      }, 4000),
    };
    button.textContent = reallyLabel;
    button.classList.add("mrln-armed");
  }

  // ---- data loading --------------------------------------------------------

  function loadingNote(message) {
    return el(
      "div",
      { class: "mrln-note mrln-loading" },
      el("span", { class: "mrln-spinner" }),
      ` ${message}`
    );
  }

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
      composeTab.replaceChildren(loadingNote("Loading prompt library…"));
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
      composeTab.replaceChildren(libraryErrorNote(err.message));
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
    try {
      state.detail = await ctx.apiJson(
        `/mrln/prompt/template?slug=${encodeURIComponent(state.slug)}`
      );
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
      composeTab.replaceChildren(loadingNote(`Loading '${slug}'…`));
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
        composeTab.replaceChildren(banner);
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

  async function ensurePool(ref) {
    if (state.detail.pools[ref]) return;
    try {
      const body = await ctx.apiJson(`/mrln/prompt/items?ref=${encodeURIComponent(ref)}`);
      state.detail.pools[ref] = body.items;
    } catch (err) {
      state.detail.pools[ref] = [];
      ctx.toast("error", `Cannot load items for '${ref}'`, err.message);
    }
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

  function toggleAudition(set, id) {
    const other = set === state.muted ? state.soloed : state.muted;
    if (set.has(id)) {
      set.delete(id);
    } else {
      set.add(id);
      other.delete(id); // M/S are exclusive — a section is muted OR soloed
    }
    renderComposeTab();
    schedulePreview();
  }

  function clearAudition() {
    state.muted.clear();
    state.soloed.clear();
    renderComposeTab();
    schedulePreview();
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

  // ---- nested child rows (from the drawn items' child slots) ---------------
  // Children mount INSIDE their parent slot's card so the hierarchy is
  // spatially obvious: slot card → drawn item → its child draws → deeper
  // branches as collapsible sub-trees. Only these mounts re-render per
  // preview, so open dropdowns elsewhere stay untouched.

  function nestedBranch(resolvedSlot) {
    const rows = [];
    for (const child of resolvedSlot.children ?? []) {
      rows.push(childRow(child));
      if ((child.children ?? []).length) {
        const grand = child.children.length;
        const openKey = `open:${child.id}`;
        const closedKey = `closed:${child.id}`;
        const isOpen = state.nestOpen.has(openKey)
          ? true
          : state.nestOpen.has(closedKey)
            ? false
            : grand <= 6;
        rows.push(
          el(
            "details",
            {
              class: "mrln-nest-branch",
              open: isOpen ? "" : null,
              ontoggle: (e) => {
                state.nestOpen[e.target.open ? "add" : "delete"](openKey);
                state.nestOpen[e.target.open ? "delete" : "add"](closedKey);
              },
            },
            el(
              "summary",
              { title: `${child.id} → nested draws of the drawn item` },
              `${child.id.split(".").pop()} · ${grand} nested draws`
            ),
            el("div", { class: "mrln-nest" }, nestedBranch(child))
          )
        );
      }
    }
    return rows;
  }

  function renderNested() {
    const seen = new Set();
    const collect = (slots) => {
      for (const slot of slots ?? []) {
        for (const child of slot.children ?? []) {
          seen.add(child.id);
          collect([child]);
        }
      }
    };
    collect(state.lastPreview?.slots);
    if (state.lastPreview) {
      // Prune stale pins only against a REAL preview: right after a template
      // or Load-from-node switch lastPreview is null, and pruning then would
      // wipe the nested pins just read from the node before the first
      // preview (which is built FROM those pins) can confirm them.
      for (const key of [...state.rows.keys()]) {
        if (key.includes(".") && !seen.has(key)) state.rows.delete(key); // stale pins
      }
    }
    for (const mount of composeTab.querySelectorAll("[data-mrln-nested]")) {
      const resolved = (state.lastPreview?.slots ?? []).find(
        (s) => s.id === mount.dataset.mrlnNested
      );
      if (resolved && (resolved.children ?? []).length) {
        mount.replaceChildren(
          el(
            "div",
            {
              class: "mrln-field-name",
              title: "Child slots carried by the drawn item — sections define them, the template stays free of choice",
            },
            `↳ nested draws of '${resolved.item ?? ""}'`
          ),
          ...nestedBranch(resolved)
        );
        mount.style.display = "";
      } else {
        mount.replaceChildren();
        mount.style.display = "none";
      }
    }
  }

  function childRow(child) {
    let row = state.rows.get(child.id);
    if (!row) {
      row = {
        random: child.random,
        seed: "",
        item: child.random ? "" : (child.item ?? ""),
        touched: false,
      };
      state.rows.set(child.id, row);
    }
    const pool = state.detail.pools[child.ref];
    if (!pool) ensurePool(child.ref).then(() => renderNested());

    const itemSelect = el("select", {
      onchange: (e) => {
        const value = e.target.value;
        row.touched = true;
        if (value === "random") {
          row.random = true;
          row.item = "";
        } else {
          row.random = false;
          row.item = value;
          if (value !== "off") row.seed = "";
        }
        seedInput.style.display = row.random ? "" : "none";
        schedulePreview();
      },
    });
    const singleOnly = (pool ?? []).length === 1;
    if (!singleOnly) itemSelect.append(el("option", { value: "random" }, "🎲 random"));
    itemSelect.append(el("option", { value: "off" }, "🔇 off"));
    for (const item of pool ?? []) {
      itemSelect.append(el("option", { value: item.name, title: item.text }, item.name));
    }
    itemSelect.value = row.random
      ? singleOnly
        ? pool[0].name
        : "random"
      : row.item || (singleOnly ? pool[0].name : "random");

    const seedInput = el("input", {
      class: "mrln-narrow",
      type: "text",
      inputmode: "numeric",
      placeholder: "seed",
      title: "Optional per-child seed",
      value: row.seed,
      style: row.random ? "" : "display:none",
      oninput: (e) => {
        row.touched = true;
        row.seed = e.target.value.replace(/\D/g, "");
        schedulePreview();
      },
    });

    return el(
      "div",
      { class: "mrln-slot mrln-nest-row" },
      el(
        "div",
        { class: "mrln-slot-label", title: `${child.id} → ${child.ref}` },
        el("span", {}, child.id.split(".").pop()),
        el("span", { class: "mrln-chip" }, child.omitted ? "muted/empty" : child.item)
      ),
      el("div", { class: "mrln-inline" }, itemSelect, seedInput)
    );
  }

  // ---- compose tab ---------------------------------------------------------

  const previewBox = el("div");
  const footer = el(
    "div",
    { class: "mrln-footer" },
    el(
      "div",
      { class: "mrln-actions" },
      el(
        "button",
        {
          class: "mrln-btn mrln-primary",
          title: "Write template + settings to the node. Unsaved template edits "
            + "are saved to your user library first — the node always renders "
            + "the saved file.",
          onclick: () => applyToNode(),
        },
        "Apply to node"
      ),
      el(
        "button",
        {
          class: "mrln-btn",
          title: "Draw a new random selection (new master seed) — preview only, nothing is queued",
          onclick: () => rerollSeed(),
        },
        "🎲 Randomize"
      ),
      el("button", { class: "mrln-btn", onclick: () => loadFromNode() }, "Load"),
      el("button", { class: "mrln-btn", title: "Fix every random slot to what the preview just drew", onclick: () => pinLastDraw() }, "Pin draw"),
      el(
        "button",
        {
          class: "mrln-btn",
          title: "Save this template (current picks become its defaults) to your user library",
          onclick: () => saveTemplate(state.slug),
        },
        "Save"
      ),
      el(
        "button",
        {
          class: "mrln-btn",
          onclick: async (e) => {
            const button = e.currentTarget;
            if (button.mrlnArmed) {
              armDestructive(button); // second click — run the armed overwrite
              return;
            }
            const slug = await askString(
              "Save as template",
              "Template slug (lowercase, '/' for folders):",
              `${state.slug}-mine`
            );
            if (!slug?.trim()) return;
            const clean = slug.trim();
            if ((state.library?.templates ?? []).some((t) => t.slug === clean)) {
              // silent clobber guard — newTemplate refuses, Save as… arms
              armDestructive(button, `Really overwrite '${clean}'?`, () =>
                saveTemplate(clean, { asNew: true })
              );
              return;
            }
            await saveTemplate(clean, { asNew: true });
          },
        },
        "Save as…"
      )
    ),
    previewBox
  );

  function field(name, control) {
    return el(
      "label",
      { class: "mrln-field" },
      el("span", { class: "mrln-field-name" }, name),
      control
    );
  }

  // Auto-growing textarea: height follows content, capped at ~35% viewport.
  function autoSize(area) {
    const cap = Math.floor(window.innerHeight * 0.35);
    area.style.height = "auto";
    area.style.height = `${Math.min(area.scrollHeight + 2, cap)}px`;
  }

  function autoArea(attrs, text) {
    const area = el("textarea", attrs, text);
    area.classList.add("mrln-auto");
    area.addEventListener("input", () => autoSize(area));
    requestAnimationFrame(() => autoSize(area)); // after it is in the DOM
    return area;
  }

  function rerollSeed() {
    state.seed = Math.floor(Math.random() * 0xffffffff);
    renderComposeTab(); // header seed input shows the new value
    schedulePreview();
  }

  // ---- drag & drop reordering ----------------------------------------------

  let dragSrc = null; // {scope, index}

  function clearDropMarks() {
    for (const marked of composeTab.querySelectorAll(".mrln-drop-before, .mrln-drop-after")) {
      marked.classList.remove("mrln-drop-before", "mrln-drop-after");
    }
  }

  function attachDrag(card, handle, scope, index, mover) {
    handle.addEventListener("mousedown", () => (card.draggable = true));
    card.addEventListener("dragstart", (e) => {
      dragSrc = { scope, index };
      card.classList.add("mrln-dragging");
      e.dataTransfer?.setData("text/plain", "");
      if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
    });
    card.addEventListener("dragend", () => {
      card.draggable = false;
      card.classList.remove("mrln-dragging");
      clearDropMarks();
      dragSrc = null;
    });
    card.addEventListener("dragover", (e) => {
      if (!dragSrc || dragSrc.scope !== scope) return;
      e.preventDefault();
      const rect = card.getBoundingClientRect();
      const after = e.clientY > rect.top + rect.height / 2;
      card.classList.toggle("mrln-drop-after", after);
      card.classList.toggle("mrln-drop-before", !after);
    });
    card.addEventListener("dragleave", () =>
      card.classList.remove("mrln-drop-after", "mrln-drop-before")
    );
    card.addEventListener("drop", (e) => {
      if (!dragSrc || dragSrc.scope !== scope) return;
      e.preventDefault();
      const rect = card.getBoundingClientRect();
      let pos = index + (e.clientY > rect.top + rect.height / 2 ? 1 : 0);
      if (dragSrc.index < pos) pos -= 1;
      if (pos !== dragSrc.index) {
        mover(dragSrc.index, pos);
        markModified();
        renderComposeTab();
        schedulePreview();
      }
    });
  }

  function dragHandle() {
    return el("span", { class: "mrln-drag", title: "Drag to reorder" }, "⠿");
  }

  function msButtons(id) {
    return el(
      "span",
      { class: "mrln-ms" },
      el(
        "button",
        {
          class: `mrln-btn mrln-mini${state.muted.has(id) ? " mrln-m-on" : ""}`,
          title: "Mute — exclude this section ('slot=off'); Apply to node carries it over",
          onclick: () => toggleAudition(state.muted, id),
        },
        "M"
      ),
      el(
        "button",
        {
          class: `mrln-btn mrln-mini${state.soloed.has(id) ? " mrln-s-on" : ""}`,
          title: "Solo — only soloed sections render (others become 'off'; solo overrides mute)",
          onclick: () => toggleAudition(state.soloed, id),
        },
        "S"
      )
    );
  }

  function smallBtn(title, text, onclick, disabled = false) {
    return el(
      "button",
      { class: "mrln-btn mrln-mini", title, onclick, disabled: disabled ? "" : null },
      text
    );
  }

  function editorCloseBtn() {
    return el(
      "button",
      {
        class: "mrln-btn mrln-mini mrln-editor-close",
        title: "Close this editor (unsaved edits here are discarded)",
        onclick: () => {
          if (confirmReplaceEditor()) setEditor();
        },
      },
      "✕"
    );
  }

  // ---- missing-LoRA banner -------------------------------------------------
  // A LoRA item references its .safetensors by NAME and carries the Civitai
  // AIR in its comment. A template that travelled between machines therefore
  // LOADS fine and only dies later, inside LoRA Apply, with a
  // FileNotFoundError. This states it up front and offers the same AIR
  // download the Library tab's item rows do — in bulk.

  const loraBanner = el("div", { class: "mrln-lora-banner", style: "display:none" });

  async function fetchLoraStatus(slug) {
    // slug omitted = the whole library. An older server (or a failing scan)
    // must not break the tab: no answer simply means no banner.
    const query = slug ? `?template=${encodeURIComponent(slug)}` : "";
    try {
      return await ctx.apiJson(`/mrln/prompt/lora-status${query}`);
    } catch {
      return null;
    }
  }

  function pollLoraDownload(air, onProgress) {
    // Resolves with the TERMINAL status body ({status: "done"|"error"|…}).
    // Bounded on both axes — a poll loop that cannot end outlives the panel
    // that started it: ~40 min of progress ticks, or 5 consecutive unreadable
    // polls, then it stops watching (the server-side download runs on).
    return new Promise((resolve) => {
      let ticks = 0;
      let failures = 0;
      const tick = async () => {
        let body;
        try {
          body = await ctx.apiJson(`/mrln/prompt/lora-download?air=${encodeURIComponent(air)}`);
          failures = 0;
        } catch (err) {
          if (++failures >= 5) {
            resolve({ status: "error", detail: `progress unreadable: ${err.message}` });
            return;
          }
          setTimeout(tick, 3000);
          return;
        }
        if (body.status !== "downloading") {
          resolve(body);
          return;
        }
        onProgress?.(body);
        if (++ticks > 1600) {
          resolve({
            status: "error",
            detail: "still running after ~40 min — the download continues in the "
              + "background, reopen the template to check on it",
          });
          return;
        }
        setTimeout(tick, 1500);
      };
      setTimeout(tick, 1500);
    });
  }

  async function getMissingLoras(rows, notes) {
    // Sequential on purpose: multi-GB pulls in parallel starve each other and
    // the progress status is keyed by AIR, so one at a time stays readable.
    let done = 0;
    let failed = 0;
    let healed = false;
    for (const row of rows) {
      const air = downloadableAir(row);
      const note = notes.get(row);
      // No subfolder question in bulk (askString per file would be a modal
      // storm): the stored path's own folder is where the author had it —
      // the same value the section editor pre-fills.
      const parts = String(row.file ?? "").replaceAll("\\", "/").split("/");
      const filename = parts.pop();
      note.textContent = " — starting…";
      try {
        await ctx.apiJson("/mrln/prompt/lora-download", {
          method: "POST",
          body: {
            air,
            start: true,
            folder: parts.join("/"),
            filename,
            section: row.section ?? "",
            item: row.item ?? "",
            stored: row.file ?? "",
          },
        });
      } catch (err) {
        failed++;
        note.textContent = ` — ${err.message}`;
        continue;
      }
      const body = await pollLoraDownload(air, (progress) => {
        note.textContent = ` — downloading… ${loraProgressText(progress)}`;
      });
      if (body.status === "done") {
        done++;
        loraListCache = null; // pickers must list the new file
        if (body.healed) healed = true;
        note.textContent = body.healed ? ` — done → ${body.healed}` : " — done";
      } else {
        failed++;
        note.textContent = ` — failed: ${body.detail || body.status}`;
      }
    }
    return { done, failed, healed };
  }

  function renderLoraBanner(status) {
    const rows = missingLoraRows(status);
    if (!rows.length) {
      loraBanner.replaceChildren();
      loraBanner.style.display = "none";
      return;
    }
    const notes = new Map();
    const list = el(
      "ul",
      { class: "mrln-import-plan" },
      rows.map((row) => {
        const note = el(
          "span",
          { class: "mrln-note" },
          downloadableAir(row) ? "" : " — no AIR — pick a local file in the Library tab"
        );
        notes.set(row, note);
        return el(
          "li",
          {},
          el("b", {}, row.file || "(no file name)"),
          el(
            "span",
            { class: "mrln-slug" },
            ` ${row.uses.map((use) => `${use.section} · ${use.item}`).join(", ")}`
          ),
          note
        );
      })
    );
    const downloadable = rows.filter((row) => downloadableAir(row));
    let action = null;
    if (status?.can_download === false) {
      action = el(
        "div",
        { class: "mrln-note" },
        "Downloads need a running ComfyUI — copy these files into your loras folder by hand."
      );
    } else if (downloadable.length) {
      const button = el(
        "button",
        {
          class: "mrln-btn mrln-primary",
          title: "Download every missing file that carries an AIR from Civitai "
            + "(one after another, background, SHA256-verified) and re-point the "
            + "section items whose path changes",
          onclick: async () => {
            if (button.disabled) return;
            button.disabled = true;
            const slug = state.slug;
            const no = state.templateNo;
            try {
              const result = await getMissingLoras(downloadable, notes);
              if (result.healed && no === state.templateNo) await refreshDetail();
              ctx.toast(
                result.failed ? "warn" : "success",
                "LoRA downloads finished",
                `${result.done} downloaded`
                  + (result.failed ? `, ${result.failed} failed — see the list` : "")
              );
              await refreshLoraBanner(slug, no); // hides itself once nothing is missing
            } finally {
              button.disabled = false;
            }
          },
        },
        `⬇ Get all missing from Civitai (${downloadable.length})`
      );
      action = el("div", { class: "mrln-actions" }, button);
    }
    loraBanner.replaceChildren(
      el(
        "div",
        { class: "mrln-error" },
        `⚠ ${rows.length} LoRA file(s) this template draws are missing on this machine — `
          + "queueing the graph fails in LoRA Apply until they are there."
      ),
      list,
      action
    );
    loraBanner.style.display = "";
  }

  async function refreshLoraBanner(slug, token = state.templateNo) {
    // Supersede guard, same token as selectTemplate: a fast template switch
    // must never paint the previous template's missing files over the new one.
    if (!slug) {
      renderLoraBanner(null); // template-scoped only — never library-wide here
      return null;
    }
    const status = await fetchLoraStatus(slug);
    if (token !== state.templateNo || slug !== state.slug) return status;
    renderLoraBanner(status);
    return status;
  }

  async function warnMissingLoras(plan) {
    // Section-only bundles never reach the compose tab, so the banner never
    // fires for them — same status source, scoped to the files this bundle
    // actually brought, delivered as a toast.
    const wanted = new Set((plan.loras ?? []).map((entry) => loraKey(entry.file)));
    if (!wanted.size) return;
    const missing = missingLoraRows(await fetchLoraStatus(null)).filter((row) =>
      wanted.has(loraKey(row.file))
    );
    if (!missing.length) return;
    ctx.toast(
      "warn",
      `${missing.length} referenced LoRA file(s) missing`,
      `${missing.map((row) => row.file).join(", ")} — open the section in the Library `
        + "tab to download them from Civitai"
    );
  }

  function renderComposeTab() {
    if (!state.rawData) {
      composeTab.replaceChildren(
        el(
          "div",
          { class: "mrln-note" },
          "No templates in the library yet — start a new composition:"
        ),
        el(
          "div",
          { class: "mrln-actions" },
          el("button", { class: "mrln-btn mrln-primary", onclick: newTemplate }, "New template…")
        )
      );
      return;
    }

    const templateSelect = el("select", {
      onchange: (e) => {
        if (!confirmDiscardEdits("switch-template")) {
          e.target.value = state.slug; // revert until the pick is repeated
          return;
        }
        selectTemplate(e.target.value);
      },
    });
    // grouped by top-level folder — 38+ templates need structure in a combo
    const templateGroups = new Map();
    for (const t of state.library.templates) {
      const key = t.slug.includes("/") ? t.slug.split("/")[0] : "(root)";
      if (!templateGroups.has(key)) templateGroups.set(key, []);
      templateGroups.get(key).push(t);
    }
    for (const [key, members] of [...templateGroups.entries()].sort((a, b) =>
      a[0].localeCompare(b[0])
    )) {
      const group = el("optgroup", { label: key });
      for (const t of members) {
        group.append(
          el(
            "option",
            { value: t.slug },
            (t.label || t.slug) + (t.tier === "user" ? " (user)" : "")
          )
        );
      }
      templateSelect.append(group);
    }
    templateSelect.value = state.slug;

    const modeSelect = el("select", {
      onchange: (e) => {
        state.mode = e.target.value;
        schedulePreview();
      },
    });
    for (const mode of ["as configured", "randomize all", "all fixed defaults"]) {
      modeSelect.append(el("option", { value: mode }, mode));
    }
    modeSelect.value = state.mode;

    const seedInput = el("input", {
      type: "number",
      min: "0",
      value: state.seed,
      oninput: (e) => {
        state.seed = Math.max(0, Math.floor(Number(e.target.value) || 0));
        schedulePreview();
      },
    });
    const reroll = el(
      "button",
      { class: "mrln-btn", title: "New random master seed", onclick: () => rerollSeed() },
      "🎲"
    );

    const formatSelect = el("select", {
      onchange: (e) => {
        state.format = e.target.value;
        schedulePreview();
      },
    });
    for (const fmt of ["template default", "string", "string_labeled", "json", "json_flat"]) {
      formatSelect.append(el("option", { value: fmt }, fmt));
    }
    formatSelect.value = state.format;

    const parts = [
      field(
        "Template",
        el(
          "div",
          { class: "mrln-inline" },
          templateSelect,
          tierChip(state.detail.tier),
          el(
            "button",
            {
              class: "mrln-btn mrln-mini mrln-new-tpl",
              title: "Start a NEW empty template (net-new composition)",
              onclick: newTemplate,
            },
            "＋"
          ),
          exportBtn("template", state.slug)
        )
      ),
    ];
    modifiedNote.style.display = state.modified ? "" : "none";
    parts.push(modifiedNote);
    const missingRefs = (state.detail.missing_refs ?? []).filter((ref) =>
      allSlots().some((slot) => slot.ref === ref)
    );
    if (missingRefs.length) {
      parts.push(
        el(
          "div",
          { class: "mrln-error" },
          `⚠ ${missingRefs.length} section ref(s) point nowhere: ${missingRefs.join(", ")} — ` +
            "the node skips them with a warning; remap below and Save to repair the template."
        )
      );
    }
    // persistent node (like modifiedNote): a re-render during a download must
    // not throw away the progress list it is writing into
    parts.push(loraBanner);
    if (state.rawData.description) {
      parts.push(el("div", { class: "mrln-note" }, state.rawData.description));
    }

    const policySelect = el("select", {
      title: "When a negative term also appears in the prompt: keep it or drop it",
      onchange: (e) => {
        state.conflictPolicy = e.target.value;
        schedulePreview();
      },
    });
    for (const policy of ["negative prevails", "positive prevails"]) {
      policySelect.append(el("option", { value: policy }, policy));
    }
    policySelect.value = state.conflictPolicy;

    const lengthSelect = el("select", {
      title: "Item text verbosity: short = compact text_short variants for tight "
        + "tokenizers (SDXL); items without one fall back to their long text",
      onchange: (e) => {
        state.textLength = e.target.value;
        schedulePreview();
      },
    });
    for (const length of ["template default", "long", "short"]) {
      lengthSelect.append(el("option", { value: length }, length));
    }
    lengthSelect.value = state.textLength;

    const profileSelect = el("select", {
      title: "Target-model profile: applies its render overrides (format/length) "
        + "and carries its LLM system prompt on the node's llm output. Explicit "
        + "Format/Text length choices here still win. With a profile selected, "
        + "prose/default/emphasis edits + Save store a per-profile VARIANT of "
        + "this template (a diff vs standard — the base file stays untouched; "
        + "structural changes still need the 'standard' profile).",
      onchange: (e) => setTargetProfile(e.target.value),
    });
    profileSelect.append(el("option", { value: "standard" }, "standard"));
    for (const name of Object.keys(state.detail?.template?.profiles ?? {}).sort()) {
      profileSelect.append(el("option", { value: name }, name));
    }
    profileSelect.value = state.profile ?? "standard";
    if (profileSelect.value !== (state.profile ?? "standard")) profileSelect.value = "standard";
    const tweakCount = overrideTweakCount(overridesFor(state.profile));
    const profileWrap = el("div", { class: "mrln-inline" }, profileSelect);
    if (tweakCount) {
      profileWrap.append(
        el(
          "span",
          {
            class: "mrln-chip mrln-user",
            title: `This template stores ${tweakCount} tweak(s) for '${state.profile}' `
              + "(vs the standard render). Save with this profile selected updates "
              + "them; ↺ removes them.",
          },
          `✎ ${tweakCount}`
        ),
        smallBtn(
          "Remove this profile's stored tweaks — back to the standard render",
          "↺",
          (e) => armDestructive(e.currentTarget, "Really remove?", revertProfileTweaks)
        )
      );
    }

    parts.push(
      el("div", { class: "mrln-grid2" }, field("Mode", modeSelect), field("Format", formatSelect)),
      el(
        "div",
        { class: "mrln-grid2" },
        field("Conflicts", policySelect),
        field("Text length", lengthSelect)
      ),
      el(
        "div",
        { class: "mrln-grid2" },
        field("Target profile", profileWrap),
        field("Master seed", el("div", { class: "mrln-inline" }, seedInput, reroll))
      ),
      metaPromptBlock(),
      el("div", { class: "mrln-slot-list" }, orderedRows()),
      addSectionRow()
    );

    const variables = state.rawData.variables ?? [];
    const triggerVar = variables.find((v) => v.name === "trigger");
    parts.push(
      field(
        "Trigger word {trigger}",
        el("input", {
          title: "{trigger} is replaced everywhere: template text, lead-ins and item texts",
          type: "text",
          value: state.trigger,
          placeholder: triggerVar?.default ?? "",
          oninput: (e) => {
            state.trigger = e.target.value;
            schedulePreview();
          },
        })
      )
    );
    const extraVars = variables.filter((v) => v.name !== "trigger");
    parts.push(
      field(
        `Variables (${extraVars.map((v) => v.name).join(", ") || "name=value"})`,
        autoArea(
          {
            placeholder: extraVars.map((v) => `${v.name}=${v.default ?? ""}`).join("\n"),
            oninput: (e) => {
              state.variables = e.target.value;
              schedulePreview();
            },
          },
          state.variables
        )
      )
    );

    parts.push(footer);

    composeTab.replaceChildren(...parts);
    renderPreview(state.lastPreview, null);
    renderNested(); // fresh mounts need refilling from the last preview
  }

  function metaPromptBlock() {
    const labelInput = el("input", {
      type: "text",
      value: state.rawData.label ?? "",
      placeholder: "Display name in pickers — empty = derived from the slug",
      title: "How this template is listed in the Composer (the node always "
        + "shows the slug). Leave empty to derive it from the slug.",
      oninput: (e) => {
        const value = e.target.value.trim();
        if (value) state.rawData.label = value;
        else delete state.rawData.label;
        markModified();
      },
    });
    const prefixArea = autoArea(
      {
        placeholder: "Text before the first section — {trigger} works here; "
          + "{slot-id} weaves that slot's draw inline (it leaves the block list)",
        oninput: (e) => {
          state.rawData.prefix = e.target.value;
          markModified();
          schedulePreview();
        },
      },
      state.rawData.prefix ?? ""
    );
    const suffixArea = autoArea(
      {
        placeholder: "Text after the last section — {slot-id} weaving works here too",
        oninput: (e) => {
          state.rawData.suffix = e.target.value;
          markModified();
          schedulePreview();
        },
      },
      state.rawData.suffix ?? ""
    );
    const negativeInput = el("input", {
      type: "text",
      value: state.rawData.negative ?? "",
      placeholder: "template-level negative terms",
      oninput: (e) => {
        state.rawData.negative = e.target.value;
        markModified();
        schedulePreview();
      },
    });
    const typeInput = el("input", {
      type: "text",
      value: (state.rawData.type ?? []).join(", "),
      placeholder: "e.g. object, car — empty = untyped (sees everything)",
      title: "Template classifiers: filters the section picker and random draw "
        + "pools to matching + universal sections. Explicit picks are never restricted.",
      oninput: (e) => {
        const values = e.target.value.split(",").map((v) => v.trim()).filter(Boolean);
        if (values.length) state.rawData.type = values;
        else delete state.rawData.type;
        markModified();
        schedulePreview();
      },
    });
    const wrapRefOptions = () => {
      const options = [{ name: "trigger", hint: "node trigger widget" }];
      for (const v of state.rawData.variables ?? []) options.push({ name: v.name, hint: "variable" });
      for (const s of state.rawData.slots ?? [])
        options.push({ name: s.id, hint: `slot → ${s.ref} (weaves inline)` });
      for (const variant of state.rawData.variants ?? [])
        for (const s of variant.slots ?? [])
          options.push({ name: s.id, hint: `slot → ${s.ref} (${variant.name} only)` });
      return options;
    };
    const wrapKnown = () => new Set(wrapRefOptions().map((o) => o.name));
    for (const area of [prefixArea, suffixArea]) {
      area.dataset.mrlnBaseTitle = "";
      area.addEventListener("input", () => validateRefs(area, wrapKnown));
      validateRefs(area, wrapKnown);
    }
    const hasText = Boolean(state.rawData.prefix || state.rawData.suffix);
    return el(
      "details",
      { class: "mrln-fold", open: hasText ? "" : null },
      el("summary", {}, "Template text & type (label / prefix / suffix / negative / classifiers)"),
      field("Label (display name)", labelInput),
      field("Prefix", braceAssist(prefixArea, wrapRefOptions)),
      field("Suffix", braceAssist(suffixArea, wrapRefOptions)),
      field("Negative", negativeInput),
      field("Type (classifiers)", typeInput)
    );
  }

  function orderedRows() {
    const nodes = [];
    const variants = state.rawData.variants ?? [];
    for (let i = 0; i < state.orderIds.length; i++) {
      const id = state.orderIds[i];
      if (id === "@variant") {
        nodes.push(variantHeaderRow(i));
        const active = variants.find((v) => v.name === state.variant);
        if (active) {
          for (let vi = 0; vi < (active.slots ?? []).length; vi++) {
            nodes.push(slotRow(active.slots[vi], active.slots, vi, true));
          }
        } else if (variants.length) {
          nodes.push(
            el(
              "div",
              { class: "mrln-note mrln-indent" },
              "Variant is drawn from the seed — pick one to control its slots."
            )
          );
        }
        continue;
      }
      const shared = state.rawData.slots ?? [];
      const index = shared.findIndex((s) => s.id === id);
      if (index >= 0) nodes.push(slotRow(shared[index], shared, index, false, i));
    }
    return nodes;
  }

  function moveOrder(orderIndex, delta) {
    const target = orderIndex + delta;
    if (target < 0 || target >= state.orderIds.length) return;
    const order = state.orderIds;
    [order[orderIndex], order[target]] = [order[target], order[orderIndex]];
    markModified();
    renderComposeTab();
    schedulePreview();
  }

  function variantHeaderRow(orderIndex) {
    const variants = state.rawData.variants ?? [];
    const variantSelect = el("select", {
      onchange: (e) => {
        state.variant = e.target.value;
        renderComposeTab();
        schedulePreview();
      },
    });
    variantSelect.append(el("option", { value: "random" }, "🎲 random"));
    for (const v of variants) {
      variantSelect.append(el("option", { value: v.name }, v.label || v.name));
    }
    variantSelect.value = state.variant;
    const handle = dragHandle();
    const blockDimmed = auditionActive() && !variantBlockAudible();
    const card = el(
      "div",
      { class: `mrln-slot mrln-variant-head${blockDimmed ? " mrln-muted" : ""}` },
      el(
        "div",
        { class: "mrln-slot-label" },
        handle,
        msButtons("@variant"),
        el("span", {}, "Variant block"),
        el("span", { class: "mrln-chip" }, "@variant"),
        el(
          "span",
          { class: "mrln-rowbtns" },
          smallBtn("Move variant block up", "↑", () => moveOrder(orderIndex, -1)),
          smallBtn("Move variant block down", "↓", () => moveOrder(orderIndex, 1))
        )
      ),
      variantSelect
    );
    attachDrag(card, handle, "order", orderIndex, (from, to) =>
      moveInArray(state.orderIds, from, to)
    );
    return card;
  }

  function removeSlot(container, index, id, isVariantSlot) {
    container.splice(index, 1);
    if (!isVariantSlot) state.orderIds = state.orderIds.filter((oid) => oid !== id);
    state.rows.delete(id);
    state.labelEdit.delete(id);
    markModified();
    renderComposeTab();
    schedulePreview();
  }

  function missingSlotCard(slot, container, index, isVariantSlot, orderIndex) {
    // The ref points at no section (factory restructure, deleted user file).
    // Like ComfyUI's missing-node flow: the template still loads, the slot
    // renders as a repair card, and remapping + Save fixes the file.
    const remapSelect = sectionSelect();
    const remapButton = el(
      "button",
      {
        class: "mrln-btn",
        onclick: async () => {
          const ref = remapSelect.value;
          if (!ref) return;
          slot.ref = ref;
          delete slot.default; // the old default named an item of the dead section
          state.rows.set(slot.id, parseToken("random"));
          await ensurePool(ref);
          markModified();
          renderComposeTab();
          schedulePreview();
        },
      },
      "Remap"
    );
    const handle = dragHandle();
    const card = el(
      "div",
      { class: `mrln-slot mrln-broken${isVariantSlot ? " mrln-indent" : ""}` },
      el(
        "div",
        { class: "mrln-slot-label", title: `${slot.id} → ${slot.ref}` },
        handle,
        el("span", {}, slot.label && slot.label.length <= 60 ? slot.label : slot.id),
        el("span", { class: "mrln-chip mrln-missing" }, "missing"),
        el(
          "span",
          { class: "mrln-rowbtns" },
          smallBtn("Remove this slot from the template", "✕", () =>
            removeSlot(container, index, slot.id, isVariantSlot)
          )
        )
      ),
      el(
        "div",
        { class: "mrln-error" },
        `Section '${slot.ref}' no longer exists — remap it to a live section:`
      ),
      el("div", { class: "mrln-inline" }, remapSelect, remapButton)
    );
    if (isVariantSlot) {
      attachDrag(card, handle, `variant:${state.variant}`, index, (from, to) =>
        moveInArray(container, from, to)
      );
    } else if (orderIndex !== null) {
      attachDrag(card, handle, "order", orderIndex, (from, to) =>
        moveInArray(state.orderIds, from, to)
      );
    }
    return card;
  }

  function slotRow(slot, container, index, isVariantSlot, orderIndex = null) {
    if ((state.detail.missing_refs ?? []).includes(slot.ref)) {
      return missingSlotCard(slot, container, index, isVariantSlot, orderIndex);
    }
    const pool = state.detail.pools[slot.ref] ?? [];
    const row = state.rows.get(slot.id) ?? parseToken(slot.default ?? "random");
    state.rows.set(slot.id, row);

    const itemSelect = el("select", {
      onchange: (e) => {
        const value = e.target.value;
        if (value === "random") {
          row.random = true;
          row.item = "";
        } else {
          row.random = false;
          row.item = value;
          row.seed = "";
        }
        seedInput.style.display = row.random ? "" : "none";
        schedulePreview();
      },
    });
    // A 1-item pool has nothing random about it — show the item instead of
    // '🎲 random' (the draw is deterministic either way; allow_empty pools
    // keep 'random' since empty is a genuine second outcome).
    const singleOnly = pool.length === 1 && !slot.allow_empty;
    if (!singleOnly) itemSelect.append(el("option", { value: "random" }, "🎲 random"));
    const defaultItem = parseToken(slot.default ?? "random").item;
    for (const item of pool) {
      const marks = [
        item.name === defaultItem ? "•" : "",
        item.lora ? (item.base ? `(LoRA ${item.base})` : "(LoRA)") : "",
        item.tier === "user" ? "(user)" : "",
      ]
        .filter(Boolean)
        .join(" ");
      itemSelect.append(
        el(
          "option",
          { value: item.name, title: item.text },
          marks ? `${item.name} ${marks}` : item.name
        )
      );
    }
    itemSelect.value = row.random && !singleOnly ? "random" : row.item;
    if (itemSelect.value === "") {
      itemSelect.value = singleOnly ? pool[0].name : "random"; // stale item name
    }
    if (singleOnly) itemSelect.title = "Only item in this section — drawn every time";

    const seedInput = el("input", {
      class: "mrln-narrow",
      type: "text",
      inputmode: "numeric",
      placeholder: "seed",
      title: "Optional per-slot seed — decouples this slot from the master seed",
      value: row.seed,
      style: row.random && !singleOnly ? "" : "display:none",
      oninput: (e) => {
        row.seed = e.target.value.replace(/\D/g, "");
        schedulePreview();
      },
    });

    const chips = [];
    if (isVariantSlot) chips.push(el("span", { class: "mrln-chip" }, state.variant));
    if (slot.allow_empty) chips.push(el("span", { class: "mrln-chip" }, "optional"));
    const loraItem = pool.find((p) => p.lora);
    if (loraItem) {
      // name the target model on the pill: which one is DRAWN matters, since a
      // pool can hold several families and only one can match the checkpoint
      const drawn = pool.find((p) => p.lora && p.name === state.rows.get(slot.id)?.value);
      const bases = [...new Set(pool.filter((p) => p.base).map((p) => p.base))];
      const shown = drawn?.base ? [drawn.base] : bases;
      chips.push(
        el(
          "button",
          {
            class: "mrln-chip mrln-chip-btn mrln-user",
            title: "This section carries LoRA blocks (loaded via the loras "
              + "output → LoRA Apply node). Click to edit the section in the "
              + "Library tab."
              + (shown.length
                ? `\nTrained for: ${shown.join(", ")}${
                    drawn?.base ? " (the current pick)" : ""
                  } — LoRA Apply warns when the connected model differs.`
                : "\nNo base model declared — no compatibility check possible."),
            onclick: async () => {
              if (!confirmReplaceEditor()) return;
              switchTab("library");
              await openSectionEditor(loraItem.section_slug ?? slot.ref);
              editorBox.scrollIntoView({ block: "nearest" });
            },
          },
          shown.length ? `LoRA ${shown.join("/")}` : "LoRA"
        )
      );
    }
    const wrapperText = `${state.rawData?.prefix ?? ""}\n${state.rawData?.suffix ?? ""}`;
    if (wrapperText.includes(`{${slot.id}}`)) {
      chips.push(
        el(
          "span",
          {
            class: "mrln-chip mrln-merged",
            title: `Prefix/suffix reference {${slot.id}} — the drawn text is woven `
              + "into that sentence (with its emphasis) instead of joining the block list.",
          },
          "inline"
        )
      );
    }
    chips.push(
      el(
        "button",
        {
          class: `mrln-chip mrln-chip-btn${slot.emphasis && slot.emphasis !== 1 ? " mrln-override" : ""}`,
          title: "Prompt emphasis — the drawn text renders as (text:weight). "
            + "Click to edit this template's value.",
          onclick: () => {
            if (state.labelEdit.has(slot.id)) state.labelEdit.delete(slot.id);
            else state.labelEdit.add(slot.id);
            renderComposeTab();
          },
        },
        slot.emphasis && slot.emphasis !== 1 ? `×${slot.emphasis}` : "×1"
      )
    );

    const buttons = el(
      "span",
      { class: "mrln-rowbtns" },
      smallBtn("Edit the lead-in text rendered before this section", "✎", () => {
        if (state.labelEdit.has(slot.id)) state.labelEdit.delete(slot.id);
        else state.labelEdit.add(slot.id);
        renderComposeTab();
      }),
      isVariantSlot
        ? [
            smallBtn("Move up within the variant", "↑", () => {
              if (index > 0) {
                [container[index - 1], container[index]] = [container[index], container[index - 1]];
                markModified();
                renderComposeTab();
                schedulePreview();
              }
            }),
            smallBtn("Move down within the variant", "↓", () => {
              if (index < container.length - 1) {
                [container[index + 1], container[index]] = [container[index], container[index + 1]];
                markModified();
                renderComposeTab();
                schedulePreview();
              }
            }),
          ]
        : [
            smallBtn("Move up", "↑", () => moveOrder(orderIndex, -1)),
            smallBtn("Move down", "↓", () => moveOrder(orderIndex, 1)),
          ],
      smallBtn("Remove this section from the template", "✕", () =>
        removeSlot(container, index, slot.id, isVariantSlot)
      )
    );

    const labelText = slot.label && slot.label.length <= 60 ? slot.label : slot.id;
    const handle = dragHandle();
    const dimmed = auditionActive() && !slotAudible(slot.id, isVariantSlot);
    const parts = [
      el(
        "div",
        { class: "mrln-slot-label", title: `${slot.id} → ${slot.ref}` },
        handle,
        msButtons(slot.id),
        el("span", {}, labelText),
        chips,
        buttons
      ),
      el("div", { class: "mrln-inline" }, itemSelect, seedInput),
    ];
    if (state.labelEdit.has(slot.id)) {
      parts.push(
        autoArea(
          {
            placeholder: "Lead-in text rendered before this section ({trigger} works here; empty = section label)",
            oninput: (e) => {
              slot.label = e.target.value;
              markModified();
              schedulePreview();
            },
          },
          slot.label ?? ""
        ),
        el(
          "div",
          { class: "mrln-inline" },
          el(
            "span",
            {
              class: "mrln-field-name",
              title: "Wraps the drawn text as (text:weight) in the prompt — this "
                + "template's value, independent of any weights inside item texts",
            },
            "Emphasis"
          ),
          el("input", {
            class: "mrln-narrow",
            type: "number",
            step: "0.05",
            min: "0.1",
            max: "3",
            placeholder: "1",
            value: slot.emphasis ?? "",
            oninput: (e) => {
              const value = parseFloat(e.target.value);
              if (Number.isNaN(value) || value === 1) delete slot.emphasis;
              else slot.emphasis = value;
              markModified();
              schedulePreview();
            },
          })
        ),
        el(
          "div",
          {
            class: "mrln-note",
            title: "Put this placeholder into the template prefix/suffix and the "
              + "drawn text renders inside that sentence — the slot then leaves "
              + "the block list.",
          },
          `Weave inline from prefix/suffix with {${slot.id}}`
        )
      );
    }
    // persistent mount for this slot's nested draws — filled by renderNested()
    parts.push(
      el("div", {
        class: "mrln-nest",
        "data-mrln-nested": slot.id,
        style: "display:none",
      })
    );
    const card = el(
      "div",
      { class: `mrln-slot${isVariantSlot ? " mrln-indent" : ""}${dimmed ? " mrln-muted" : ""}` },
      parts
    );
    if (isVariantSlot) {
      attachDrag(card, handle, `variant:${state.variant}`, index, (from, to) =>
        moveInArray(container, from, to)
      );
    } else {
      attachDrag(card, handle, "order", orderIndex, (from, to) =>
        moveInArray(state.orderIds, from, to)
      );
    }
    return card;
  }

  function sectionSelect() {
    // Grouped picker over the live library: type-matching + universal
    // sections first, other domains behind an optgroup. Shared by the
    // add-section row and the missing-ref remap card.
    const type = state.rawData?.type ?? [];
    const matches = (suits) =>
      !type.length || !(suits ?? []).length || suits.some((s) => type.includes(s));
    const sections = state.library.sections.map((s) => ({
      value: s.slug,
      label:
        s.slug +
        ((s.suits ?? []).length ? `  [${s.suits.join(",")}]` : "") +
        (s.has_lora ? " [LoRA]" : "") +
        (s.merged ? " ⊕" : ""),
      match: matches(s.suits),
    }));
    const folders = state.library.folders.map((f) => ({
      value: f,
      label: `${f}/ (folder)`,
      match: true, // folder pools self-filter at draw time
    }));
    const options = [...folders, ...sections].sort((a, b) => a.value.localeCompare(b.value));
    const refSelect = el("select", {});
    const primary = options.filter((o) => o.match);
    const other = options.filter((o) => !o.match);
    if (other.length) {
      const groupA = el("optgroup", { label: type.length ? `matches type: ${type.join(", ")}` : "sections" });
      for (const opt of primary) groupA.append(el("option", { value: opt.value }, opt.label));
      const groupB = el("optgroup", { label: "other domains (suits elsewhere)" });
      for (const opt of other) groupB.append(el("option", { value: opt.value }, opt.label));
      refSelect.append(groupA, groupB);
    } else {
      for (const opt of primary) refSelect.append(el("option", { value: opt.value }, opt.label));
    }
    return refSelect;
  }

  function addSectionRow() {
    const refSelect = sectionSelect();
    const addButton = el(
      "button",
      {
        class: "mrln-btn",
        onclick: async () => {
          const ref = refSelect.value;
          if (!ref) return;
          const existing = new Set(allSlots().map((s) => s.id));
          let id = ref.split("/").pop();
          for (let n = 2; existing.has(id); n++) id = `${ref.split("/").pop()}-${n}`;
          state.rawData.slots = state.rawData.slots ?? [];
          state.rawData.slots.push({ id, ref });
          state.orderIds.push(id);
          state.rows.set(id, parseToken("random"));
          await ensurePool(ref);
          markModified();
          renderComposeTab();
          schedulePreview();
        },
      },
      "+ Add"
    );
    return el("div", { class: "mrln-addrow", title: "Add a section (or folder scope) as a new slot" }, refSelect, addButton);
  }

  function renderPreview(preview, err) {
    if (err) {
      previewBox.replaceChildren(
        el(
          "div",
          { class: "mrln-error" },
          err.message,
          err.remediation ? `\n${err.remediation}` : ""
        )
      );
      return;
    }
    if (!preview) {
      previewBox.replaceChildren(el("div", { class: "mrln-note" }, "Previewing…"));
      return;
    }
    const children = [
      el(
        "div",
        { class: "mrln-preview-head" },
        el(
          "span",
          { class: "mrln-field-name" },
          "Live preview",
          preview.variant && state.variant === "random" ? ` — drew variant: ${preview.variant}` : ""
        ),
        auditionActive()
          ? el(
              "button",
              {
                class: "mrln-btn mrln-mini mrln-m-on",
                title: "Mute/Solo is active — Apply to node writes these as 'off' selection "
                  + "lines. Click to clear all mutes and solos.",
                onclick: () => clearAudition(),
              },
              `clear M/S (${state.muted.size + state.soloed.size})`
            )
          : null,
        el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            title: "Copy the prompt to the clipboard",
            onclick: () => {
              navigator.clipboard?.writeText(preview.positive);
              ctx.toast("success", "Prompt copied");
            },
          },
          "⧉ copy"
        )
      ),
      el("pre", { class: "mrln-pre" }, preview.positive),
    ];
    const fold = (title, text, key) =>
      el(
        "details",
        {
          class: "mrln-fold",
          open: state[key] ? "" : null,
          ontoggle: (e) => {
            state[key] = e.target.open;
          },
        },
        el("summary", {}, title),
        el("pre", { class: "mrln-pre" }, text)
      );
    children.push(fold("Choices (what was drawn per section)", preview.choices, "choicesOpen"));
    if (preview.negative) children.push(fold("Negative", preview.negative, "negativeOpen"));
    previewBox.replaceChildren(...children);
  }

  // ---- node interop --------------------------------------------------------

  function appliedStateDiffers() {
    return util.appliedStateDiffers(state);
  }

  async function applyToNode() {
    const node = ctx.selectedTemplateNode();
    if (!node) {
      ctx.toast(
        "warn",
        "No target node",
        "Add a Prompt Template (MRLN) node — with several in the graph, select the target first."
      );
      return;
    }
    // HARDENED Apply: everything you see is persisted to the user library
    // (picks bake as defaults, mutes as "off" defaults) BEFORE the widgets
    // are written — the widgets become a convenience view, not the only
    // carrier. Selection/audition lines are captured beforehand: the
    // post-save reload restores mute/solo from the baked file.
    const selectionLines = buildSelectionLines();
    const withAudition = auditionActive();
    const needsPersist = appliedStateDiffers();
    if (needsPersist && !(await saveTemplate(state.slug))) {
      return; // save failed — its toast names the cause
    }
    ctx.setWidget(node, "template", state.slug);
    ctx.setWidget(node, "selection", selectionLines);
    ctx.setWidget(node, "selection_mode", state.mode);
    ctx.setWidget(node, "seed", state.seed);
    ctx.setWidget(node, "format", state.format);
    ctx.setWidget(node, "conflict_policy", state.conflictPolicy);
    ctx.setWidget(node, "text_length", state.textLength);
    ctx.setWidget(node, "trigger", state.trigger);
    ctx.setWidget(node, "variables", state.variables);
    ctx.setWidget(node, "profile", state.profile ?? "standard");
    ctx.markDirty();
    // Verify the writes actually landed in the node's serialized form —
    // a silent widget-write failure must be LOUD, not a lost render later.
    let persisted = false;
    try {
      const snapshot = node.serialize?.();
      const values = snapshot?.widgets_values;
      const flat = Array.isArray(values) ? values : values ? Object.values(values) : [];
      persisted =
        flat.includes(state.slug) &&
        (selectionLines === "" || flat.includes(selectionLines));
    } catch {
      persisted = false;
    }
    if (!persisted) {
      ctx.toast(
        "warn",
        "Widget write not confirmed",
        "This frontend did not report the applied values in the node's serialized "
          + "state. The applied state IS saved in your library, so the node still "
          + "reproduces it — but check the node's widgets before relying on the "
          + "workflow file."
      );
    }
    ctx.toast(
      "success",
      "Applied to node",
      `template: ${state.slug}` +
        (needsPersist ? " — applied state saved into your user library" : "") +
        (withAudition ? " (mute/solo baked as 'off' defaults)" : "")
    );
  }

  async function loadFromNode() {
    const node = ctx.selectedTemplateNode();
    if (!node) {
      ctx.toast(
        "warn",
        "No target node",
        "Add a Prompt Template (MRLN) node — with several in the graph, select the target first."
      );
      return;
    }
    const slug = ctx.getWidget(node, "template") || state.slug;
    if (!confirmDiscardEdits("load-from-node")) return;
    // fresh from disk — a failed/superseded load must not fall through to
    // applying the node's selection onto whatever template was loaded before
    if (!(await selectTemplate(slug))) return;
    state.mode = ctx.getWidget(node, "selection_mode") ?? state.mode;
    state.seed = Number(ctx.getWidget(node, "seed") ?? state.seed) || 0;
    state.format = ctx.getWidget(node, "format") ?? state.format;
    state.conflictPolicy = ctx.getWidget(node, "conflict_policy") ?? state.conflictPolicy;
    state.textLength = ctx.getWidget(node, "text_length") ?? state.textLength;
    state.trigger = ctx.getWidget(node, "trigger") ?? "";
    state.variables = ctx.getWidget(node, "variables") ?? "";
    state.profile = ctx.getWidget(node, "profile") ?? "standard";
    rebuildForProfile(state.profile); // rows/defaults reflect the node's variant
    applyKvToRows(parseKvLines(ctx.getWidget(node, "selection") ?? ""));
    renderComposeTab();
    schedulePreview();
    ctx.toast("info", "Loaded from node", `template: ${state.slug}`);
  }

  function pinLastDraw() {
    if (!state.lastPreview) return;
    let pinned = 0;
    for (const slot of state.lastPreview.slots) {
      const row = state.rows.get(slot.id);
      if (row?.random && slot.item) {
        state.rows.set(slot.id, { random: false, seed: "", item: slot.item });
        pinned += 1;
      }
    }
    if (state.lastPreview.variant && state.variant === "random") {
      state.variant = state.lastPreview.variant;
    }
    renderComposeTab();
    schedulePreview();
    ctx.toast("success", "Pinned last draw", `${pinned} slot(s) fixed`);
  }

  // ---- de-compose tab ------------------------------------------------------
  // Paste a finished prompt, map every fragment against the library
  // (heuristic engine server-side; an Ollama/LLM engine plugs into the same
  // endpoint later), resolve the residue, store the result as a template.

  function watchDecomposePull(model) {
    const started = Date.now();
    const tick = async () => {
      if (Date.now() - started > 45 * 60 * 1000) return; // stop polling silently
      let body = null;
      try {
        body = await ctx.apiJson(`/mrln/prompt/llm-pull?model=${encodeURIComponent(model)}`);
      } catch {
        /* transient — keep polling */
      }
      if (body?.status === "done") {
        ctx.toast("success", "Model pulled", `${model} is installed`);
        if (state.tab === "decompose") renderDecomposeTab(); // list shows it now
        return;
      }
      if (body?.status === "error") {
        ctx.toast("error", `Pull failed: ${model}`, body.detail ?? "");
        return;
      }
      setTimeout(tick, 4000);
    };
    setTimeout(tick, 4000);
  }

  async function runDecompose() {
    const d = state.decompose;
    if (!d.text.trim()) {
      ctx.toast("warn", "Nothing to decompose", "Paste a prompt first.");
      return;
    }
    const engine = d.engine ?? "programmatic";
    const body = { prompt: d.text, type: d.type, engine };
    if (engine !== "programmatic") {
      body.backend = d.backend ?? "ollama";
      body.model = d.model ?? "";
      body.timeout = 120; // an LLM chewing a mega-prompt needs headroom
      ctx.toast("info", "De-composing…", `${engine} engine via ${body.backend}`);
    }
    try {
      d.report = await ctx.apiJson("/mrln/prompt/decompose", { method: "POST", body });
    } catch (err) {
      ctx.toast("error", "Decompose failed", err.message);
      return;
    }
    if (d.report.llm_error) {
      ctx.toast("warn", "LLM engine fell back", d.report.llm_error);
    }
    d.plans = d.report.fragments.map((f, i) => defaultPlan(f, i, d.report.fragments));
    renderDecomposeTab();
  }

  function decomposeFragmentCard(fragment, plan, index) {
    const controls = [];
    if (fragment.match) {
      controls.push(
        el(
          "span",
          { class: "mrln-chip mrln-user" },
          `${fragment.match.section} / ${fragment.match.item} · ${Math.round(fragment.match.score * 100)}%`
        ),
        el(
          "label",
          { class: "mrln-note" },
          el("input", {
            type: "checkbox",
            checked: plan.include ? "" : null,
            onchange: (e) => {
              plan.include = e.target.checked;
            },
          }),
          " include as slot"
        )
      );
    } else {
      const sectionPicker = sectionSelect();
      if (plan.section) sectionPicker.value = plan.section;
      sectionPicker.addEventListener("change", () => {
        plan.section = sectionPicker.value;
      });
      plan.section = plan.section ?? sectionPicker.value;
      const newSectionInput = el("input", {
        type: "text",
        placeholder: "new-folder/new-section",
        value: plan.newSection ?? "",
        oninput: (e) => {
          plan.newSection = e.target.value.trim();
        },
      });
      const conditional = el("div", { class: "mrln-inline" });
      const syncConditional = () => {
        conditional.replaceChildren(
          plan.action === "new-item" ? sectionPicker : null,
          plan.action === "new-section" ? newSectionInput : null
        );
      };
      const actionSelect = el("select", {
        onchange: (e) => {
          plan.action = e.target.value;
          plan.include = plan.action !== "skip";
          syncConditional();
        },
      });
      for (const [value, label] of [
        ["new-item", "add as new item in existing section…"],
        ["new-section", "add as first item of a NEW section…"],
        ["prefix", "keep as template prefix prose"],
        ["suffix", "keep as template suffix prose"],
        ["skip", "drop this fragment"],
      ]) {
        actionSelect.append(el("option", { value }, label));
      }
      actionSelect.value = plan.action;
      syncConditional();
      controls.push(
        fragment.suggestion
          ? el(
              "span",
              { class: "mrln-chip" },
              `nearest: ${fragment.suggestion.section} · ${Math.round(fragment.suggestion.score * 100)}%`
            )
          : null,
        actionSelect,
        conditional
      );
    }
    return el(
      "div",
      { class: `mrln-slot${fragment.match ? "" : " mrln-broken"}` },
      el("div", { class: "mrln-slot-label", title: `fragment ${index + 1}` },
        el("span", {}, fragment.text)),
      fragment.rewrite
        ? el(
            "div",
            {
              class: "mrln-note",
              title: "Library-grade rewrite from the LLM engine — new items and "
                + "prefix/suffix prose use this instead of the raw fragment",
            },
            `✦ ${fragment.rewrite}`
          )
        : null,
      ...controls
    );
  }

  async function saveDecomposedTemplate(button) {
    const d = state.decompose;
    if (!d.report) return;
    if (button?.mrlnArmed) {
      armDestructive(button); // second click — run the armed overwrite
      return;
    }
    const slug = await askString(
      "Create template from decomposition",
      "Template slug (lowercase, '/' for folders):",
      "decomposed/my-prompt"
    );
    if (!slug?.trim()) return;
    const clean = slug.trim();
    if ((state.library?.templates ?? []).some((t) => t.slug === clean)) {
      // the prefilled default slug makes second-run collisions likely — arm
      // instead of silently overwriting the earlier decomposition
      armDestructive(button, `Really overwrite '${clean}'?`, () =>
        performDecomposedSave(clean)
      );
      return;
    }
    await performDecomposedSave(clean);
  }

  async function performDecomposedSave(slug) {
    const d = state.decompose;
    const type = d.type.split(",").map((t) => t.trim()).filter(Boolean);
    const prefixParts = [];
    const suffixParts = [];
    const slots = [];
    const newItemsBySection = new Map(); // section slug -> [{name, text}]
    const usedIds = new Set();
    const slotId = (base) => {
      let id = base;
      for (let n = 2; usedIds.has(id); n++) id = `${base}-${n}`;
      usedIds.add(id);
      return id;
    };
    d.report.fragments.forEach((fragment, i) => {
      const plan = d.plans[i] ?? { action: "skip" };
      if (fragment.match && plan.include) {
        slots.push({
          id: slotId(fragment.match.section.split("/").pop()),
          ref: fragment.match.section,
          default: fragment.match.item,
        });
        return;
      }
      if (fragment.match) return; // matched but excluded
      // llm/hybrid engines deliver a polished rewrite for the residue — the
      // raw fragment is coverage evidence, the rewrite is the library text
      const prose = fragment.rewrite || fragment.text;
      if (plan.action === "prefix") prefixParts.push(prose);
      else if (plan.action === "suffix") suffixParts.push(prose);
      else if (plan.action === "new-item" || plan.action === "new-section") {
        const section = plan.action === "new-item" ? plan.section : plan.newSection;
        if (!section) return;
        const items = newItemsBySection.get(section) ?? [];
        const base = jsSlugify(fragment.suggested_name || fragment.text);
        let name = base;
        for (let n = 2; items.some((item) => item.name === name); n++) name = `${base}-${n}`;
        const item = { name, text: prose };
        if (fragment.short) item.text_short = fragment.short;
        items.push(item);
        newItemsBySection.set(section, items);
        slots.push({ id: slotId(section.split("/").pop()), ref: section, default: name });
      }
    });
    if (!slots.length) {
      ctx.toast("warn", "No slots", "Nothing is mapped to a section — template would be empty.");
      return;
    }
    // 1) new items land first (extend files for factory sections, appends
    //    for user sections, fresh files for new slugs)
    for (const [section, items] of newItemsBySection) {
      let data = { version: 1, items };
      try {
        const existing = state.library.sections.find((s) => s.slug === section);
        if (existing) {
          const body = await ctx.apiJson(
            `/mrln/prompt/section?slug=${encodeURIComponent(section)}`
          );
          if (body.tier === "user") {
            data = { ...body.raw, items: [...(body.raw.items ?? []), ...items] };
          }
        } else if (type.length) {
          data.suits = type; // brand-new section inherits the template type
        }
        await ctx.apiJson("/mrln/prompt/save-section", {
          method: "POST",
          body: { slug: section, data },
        });
      } catch (err) {
        ctx.toast("error", `Cannot save items into '${section}'`, err.message);
        return;
      }
    }
    // 2) then the template that wires them together
    const data = { version: 1, slots };
    if (type.length) data.type = type;
    if (prefixParts.length) data.prefix = prefixParts.join("\n");
    if (suffixParts.length) data.suffix = suffixParts.join("\n");
    try {
      await ctx.apiJson("/mrln/prompt/save-template", {
        method: "POST",
        body: { slug, data },
      });
    } catch (err) {
      ctx.toast("error", "Template save failed", err.message);
      return;
    }
    ctx.toast("success", "Template created", `${slug} — opening in Compose`);
    ctx.refreshCombos();
    await loadLibrary();
    await selectTemplate(slug);
    switchTab("compose");
  }

  function renderDecomposeTab() {
    if (!state.library) {
      decomposeTab.replaceChildren(
        state.libraryError
          ? libraryErrorNote(state.libraryError)
          : loadingNote("Loading prompt library…")
      );
      return;
    }
    const d = state.decompose;
    const promptArea = autoArea(
      {
        placeholder: "Paste a full prompt here — each fragment is matched against your library…",
        oninput: (e) => {
          d.text = e.target.value;
        },
      },
      d.text
    );
    const typeInput = el("input", {
      type: "text",
      value: d.type,
      placeholder: "optional type filter, e.g. object, car",
      title: "Restricts matching to sections suiting these classifiers (plus universal ones)",
      oninput: (e) => {
        d.type = e.target.value;
      },
    });
    const engineSelect = el("select", {
      title: "programmatic: token matcher, offline. llm: the model splits and "
        + "maps against the library catalog. hybrid: the programmatic result "
        + "rides in the LLM system prompt as suggestions to verify or correct. "
        + "llm/hybrid fall back to programmatic when the backend fails.",
      onchange: (e) => {
        d.engine = e.target.value;
        renderDecomposeTab();
      },
    });
    for (const engine of ["programmatic", "llm", "hybrid"]) {
      engineSelect.append(el("option", { value: engine }, engine));
    }
    engineSelect.value = d.engine ?? "programmatic";
    const backendSelect = el("select", {
      title: "LLM backend for the llm/hybrid engines — locals need the URL, "
        + "clouds the API key from the Settings tab",
      onchange: (e) => {
        d.backend = e.target.value;
        renderDecomposeTab(); // the model dropdown follows the backend
      },
    });
    for (const backend of ["ollama", "lm studio", "anthropic", "openai", "gemini", "openrouter"]) {
      backendSelect.append(el("option", { value: backend }, backend));
    }
    backendSelect.value = d.backend ?? "ollama";
    const modelSelect = el("select", {
      title: "Model for the llm/hybrid engines. Locals list installed models; "
        + "'⬇ pull' entries download via Ollama when picked; clouds fall back "
        + "to a sensible default when empty.",
      onchange: async (e) => {
        const value = e.target.value;
        if (value === "__custom__") {
          const typed = await askString("Model name", "Exact model tag/id:", d.model ?? "");
          if (typed?.trim()) d.model = typed.trim();
          renderDecomposeTab();
          return;
        }
        if (value.startsWith("__pull__:")) {
          const model = value.slice(9);
          d.model = model; // set now — the pull lands in the background
          try {
            await ctx.apiJson("/mrln/prompt/llm-pull", {
              method: "POST",
              body: { model, start: true },
            });
            ctx.toast("info", "Pulling model", `${model} — Ollama downloads it in the background`);
            watchDecomposePull(model);
          } catch (err) {
            ctx.toast("error", "Pull failed to start", err.message);
          }
          renderDecomposeTab();
          return;
        }
        d.model = value;
      },
    });
    const modelNote = el("span", { class: "mrln-note" }, "");
    (async () => {
      const backend = d.backend ?? "ollama";
      const provider = backend === "lm studio" ? "lmstudio" : backend;
      const current = (d.model ?? "").trim();
      modelSelect.replaceChildren(el("option", { value: current }, current || "…"));
      let body = null;
      try {
        body = await ctx.apiJson(`/mrln/prompt/llm-validate?provider=${provider}`);
      } catch (err) {
        modelNote.textContent = `✗ ${err.message}`;
        modelNote.style.color = "#e88";
        if (!current) modelSelect.replaceChildren(el("option", { value: "" }, "(unreachable)"));
        modelSelect.append(el("option", { value: "__custom__" }, "✏ custom…"));
        return;
      }
      const models = body.models ?? [];
      const suggested = body.suggested ?? [];
      const isCloud = "key_set" in body;
      modelSelect.replaceChildren();
      if (isCloud) {
        modelSelect.append(el("option", { value: "" }, "(backend default)"));
        modelNote.textContent = body.key_set
          ? "✓ key stored"
          : "no key stored — add it in the Settings tab";
        modelNote.style.color = body.key_set ? "#6ca" : "#e88";
      } else {
        modelNote.textContent = `✓ ${models.length} installed`;
        modelNote.style.color = "#6ca";
      }
      if (current && !models.includes(current) && !suggested.includes(current)) {
        modelSelect.append(el("option", { value: current }, current));
      }
      for (const m of models) modelSelect.append(el("option", { value: m }, m));
      for (const s of suggested) {
        if (models.includes(s)) continue;
        if (provider === "ollama") {
          modelSelect.append(el("option", { value: `__pull__:${s}` }, `⬇ pull ${s}`));
        } else {
          modelSelect.append(el("option", { value: s }, s));
        }
      }
      modelSelect.append(el("option", { value: "__custom__" }, "✏ custom…"));
      if (!current && !isCloud && models.length) d.model = models[0]; // ollama needs one
      modelSelect.value = (d.model ?? "").trim();
      if (modelSelect.value !== (d.model ?? "").trim()) modelSelect.value = isCloud ? "" : models[0] ?? "";
    })();
    const parts = [
      el(
        "div",
        { class: "mrln-note" },
        "Map a pasted prompt onto your library. The programmatic matcher is "
          + "offline and deterministic; llm/hybrid ask a configured backend "
          + "and validate every assignment against the real library."
      ),
      field("Prompt to decompose", promptArea),
      field("Template type (classifiers)", typeInput),
      el("div", { class: "mrln-grid2" }, field("Engine", engineSelect), field("Backend", (d.engine ?? "programmatic") === "programmatic" ? el("span", { class: "mrln-note" }, "—") : backendSelect)),
      (d.engine ?? "programmatic") === "programmatic"
        ? null
        : field("Model", el("div", { class: "mrln-inline" }, modelSelect, modelNote)),
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn mrln-primary", onclick: () => runDecompose() }, "Decompose")
      ),
    ];
    if (d.report) {
      parts.push(
        el(
          "div",
          { class: "mrln-field-name" },
          `Fragments — ${d.report.matched} matched, ${d.report.unmatched} unmatched`
        ),
        el(
          "div",
          { class: "mrln-slot-list" },
          d.report.fragments.map((fragment, i) =>
            decomposeFragmentCard(fragment, d.plans[i], i)
          )
        ),
        el(
          "div",
          { class: "mrln-actions" },
          el(
            "button",
            {
              class: "mrln-btn mrln-primary",
              title: "Save new items/sections, then store the mapping as a template",
              onclick: (e) => saveDecomposedTemplate(e.currentTarget),
            },
            "Create template…"
          )
        )
      );
    }
    decomposeTab.replaceChildren(...parts);
  }

  // ---- library tab ---------------------------------------------------------

  const editorBox = el("div");
  // Editor forms live only in closures — replacing editorBox drops typed
  // content. Track typing (capture phase: some internal events don't
  // bubble) and gate every user-initiated replacement on confirmReplaceEditor.
  let editorDirty = false;
  editorBox.addEventListener("input", () => (editorDirty = true), true);
  editorBox.addEventListener("change", () => (editorDirty = true), true);

  function setEditor(...children) {
    editorBox.replaceChildren(...children);
    editorDirty = false; // fresh (or cleared) content — typing starts clean
  }

  function confirmReplaceEditor() {
    if (!editorDirty) return true;
    return confirmTwoStep(
      "editor",
      "Unsaved editor changes",
      "Repeat the click within 4s to replace the editor and discard them."
    );
  }

  function sectionLi(section) {
    return el(
      "li",
      {
        onclick: () => {
          if (confirmReplaceEditor()) openSectionEditor(section.slug);
        },
      },
      section.error ? `⚠ ${section.slug}` : section.label,
      el(
        "span",
        { class: "mrln-slug" },
        `${section.slug} · ${section.item_count ?? "?"} items`
      ),
      section.has_lora
        ? el(
            "span",
            {
              class: "mrln-chip mrln-user",
              title: "Carries LoRA blocks — drawn items load their file via "
                + "the template node's loras output → LoRA Apply (MRLN)."
                + ((section.lora_bases ?? []).length
                  ? ` Trained for: ${section.lora_bases.join(", ")} — LoRA Apply `
                    + "warns if the connected model is a different architecture."
                  : " None of them declares a base model, so no compatibility "
                    + "check is possible."),
            },
            (section.lora_bases ?? []).length
              ? `LoRA ${section.lora_bases.join("/")}`
              : "LoRA"
          )
        : null,
      section.merged
        ? el(
            "span",
            {
              class: "mrln-chip mrln-merged",
              title: "Combined view: your user file extends the factory section — "
                + "elements live in both tiers",
            },
            "factory+user"
          )
        : tierChip(section.tier),
      // factory-pure sections ship with every install — nothing to share
      section.tier === "user" || section.merged ? exportBtn("section", section.slug) : null
    );
  }

  function templateLi(template) {
    return el(
      "li",
      {
        onclick: () => {
          if (confirmReplaceEditor()) openTemplateEditor(template.slug);
        },
      },
      template.error ? `⚠ ${template.slug}` : template.label,
      el("span", { class: "mrln-slug" }, template.slug),
      tierChip(template.tier),
      exportBtn("template", template.slug)
    );
  }

  function groupedTree(kind, entries, itemEl) {
    // Collapsible group per top-level slug segment — the flat list would
    // overspill as soon as more domains land. Expansion state survives
    // re-renders within the session. An active filter narrows entries and
    // forces matching groups open.
    const filter = (state.libFilter ?? "").trim().toLowerCase();
    if (filter) {
      entries = entries.filter(
        (entry) =>
          entry.slug.toLowerCase().includes(filter) ||
          (entry.label ?? "").toLowerCase().includes(filter)
      );
    }
    const groups = new Map();
    for (const entry of entries) {
      const key = entry.slug.split("/")[0];
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entry);
    }
    const nodes = [];
    for (const [key, members] of [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const stateKey = `${kind}:${key}`;
      const isFolder = members.length > 1 || members[0].slug !== key;
      const userCount = members.filter((m) => m.tier === "user" || m.merged).length;
      nodes.push(
        el(
          "details",
          {
            class: "mrln-fold mrln-tree-group",
            open: filter || state.libGroups.has(stateKey) ? "" : null,
            ontoggle: (e) => {
              if (e.target.open) state.libGroups.add(stateKey);
              else state.libGroups.delete(stateKey);
            },
          },
          el(
            "summary",
            {},
            isFolder ? `${key}/` : key,
            el(
              "span",
              { class: "mrln-slug" },
              ` ${members.length}${userCount ? ` · ${userCount} yours` : ""}`
            )
          ),
          el("ul", { class: "mrln-tree" }, members.map(itemEl))
        )
      );
    }
    return nodes;
  }

  function treeBlock(kind, title, entries, itemEl) {
    // The whole block collapses too — with a multiverse-sized library even
    // the group list is long. Sections open by default, templates closed.
    const stateKey = `${kind}:@block`;
    if (!state.libGroups.has(stateKey) && !state.libGroups.has(`${stateKey}:touched`)) {
      if (kind === "sections") state.libGroups.add(stateKey); // default open
    }
    return el(
      "details",
      {
        class: "mrln-fold mrln-tree-block",
        open: (state.libFilter ?? "").trim() || state.libGroups.has(stateKey) ? "" : null,
        ontoggle: (e) => {
          state.libGroups.add(`${stateKey}:touched`);
          if (e.target.open) state.libGroups.add(stateKey);
          else state.libGroups.delete(stateKey);
        },
      },
      el(
        "summary",
        { class: "mrln-tree-head" },
        title,
        el("span", { class: "mrln-slug" }, ` ${entries.length}`)
      ),
      ...groupedTree(kind, entries, itemEl)
    );
  }

  // -- import / export (shareable bundles) ---------------------------------
  // Export embeds every USER-tier section a template draws from (thin
  // extend diffs stay thin); factory content resolves on the other install.
  // Import dry-runs first and shows the exact write/skip plan; a template
  // import ends by opening the template, where the existing missing-LoRA
  // banner offers the download-by-AIR healing.

  async function exportBundle(kind, slug) {
    let bundle;
    try {
      bundle = await ctx.apiJson(
        `/mrln/prompt/export?kind=${kind}&slug=${encodeURIComponent(slug)}`
      );
    } catch (err) {
      ctx.toast("error", "Export failed", err.message);
      return;
    }
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: bundleFilename(bundle.slug ?? slug) });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    const notes = [`${Object.keys(bundle.sections ?? {}).length} user section(s) embedded`];
    if ((bundle.factory_refs ?? []).length)
      notes.push(`${bundle.factory_refs.length} factory ref(s)`);
    if ((bundle.loras ?? []).length) notes.push(`${bundle.loras.length} LoRA link(s)`);
    ctx.toast("success", "Bundle exported", `${bundle.slug ?? slug} — ${notes.join(", ")}`);
  }

  function exportBtn(kind, slug) {
    return el(
      "button",
      {
        class: "mrln-btn mrln-mini",
        title:
          kind === "template"
            ? "Export as a shareable bundle — your user-tier sections ride embedded, "
              + "factory refs resolve on the other install, LoRA links carry their "
              + "Civitai AIR for auto-download"
            : "Export this section (plus nested user-tier sections) as a shareable bundle",
        onclick: (e) => {
          e.stopPropagation();
          exportBundle(kind, slug);
        },
      },
      "⤓"
    );
  }

  function importBundlePicker() {
    const input = el("input", { type: "file", accept: ".json,application/json" });
    input.addEventListener("change", async () => {
      const file = input.files?.[0];
      if (!file) return;
      let bundle;
      try {
        bundle = JSON.parse(await file.text());
      } catch (err) {
        ctx.toast("error", "Not a JSON file", err.message);
        return;
      }
      let plan;
      try {
        plan = await ctx.apiJson("/mrln/prompt/import", {
          method: "POST",
          body: { bundle, dry_run: true },
        });
      } catch (err) {
        ctx.toast("error", "Cannot import this file", err.message);
        return;
      }
      renderImportCard(bundle, plan);
    });
    input.click();
  }

  function importPlanLines(plan) {
    const lines = [];
    for (const w of plan.written ?? []) {
      lines.push(
        el(
          "li",
          {},
          `✓ writes ${w.kind} `,
          el("b", {}, w.slug),
          w.extends_factory
            ? " (extends your factory section)"
            : w.shadows_factory
              ? " (shadows the factory template)"
              : ""
        )
      );
    }
    for (const s of plan.skipped ?? []) {
      lines.push(
        el(
          "li",
          {},
          s.reason === "identical"
            ? `= already present unchanged: ${s.kind} `
            : `⚠ kept — you already have this ${s.kind}: `,
          el("b", {}, s.slug)
        )
      );
    }
    for (const ref of plan.missing_factory ?? []) {
      lines.push(
        el(
          "li",
          {},
          `⚠ this install lacks factory content '${ref}' — that slot will draw as `
            + "missing until the pack is updated"
        )
      );
    }
    if (!lines.length) lines.push(el("li", {}, "nothing to write"));
    return lines;
  }

  function renderImportCard(bundle, plan) {
    const isTemplate = bundle.kind === "template";
    const slugInput = el("input", {
      type: "text",
      value: String(bundle.slug ?? ""),
      title: "Template slug to import under — change it to keep your existing template untouched",
    });
    const overwriteBox = el("input", { type: "checkbox" });
    const errorLine = el("div", { class: "mrln-error" });
    const planList = el("ul", { class: "mrln-import-plan" }, importPlanLines(plan));

    function importBody(dryRun) {
      const body = { bundle, overwrite: overwriteBox.checked };
      if (dryRun) body.dry_run = true;
      if (isTemplate && slugInput.value.trim()) body.slug = slugInput.value.trim();
      return body;
    }

    async function replan() {
      try {
        const fresh = await ctx.apiJson("/mrln/prompt/import", {
          method: "POST",
          body: importBody(true),
        });
        planList.replaceChildren(...importPlanLines(fresh));
        errorLine.textContent = "";
      } catch (err) {
        errorLine.textContent = err.message;
      }
    }
    slugInput.addEventListener("change", replan);
    overwriteBox.addEventListener("change", replan);

    let busy = false;
    const importButton = el(
      "button",
      {
        class: "mrln-btn mrln-primary",
        onclick: async () => {
          if (busy) return;
          busy = true;
          importButton.disabled = true;
          try {
            const report = await ctx.apiJson("/mrln/prompt/import", {
              method: "POST",
              body: importBody(false),
            });
            const written = (report.written ?? []).length;
            const kept = (report.skipped ?? []).filter((s) => s.reason === "exists").length;
            ctx.toast(
              "success",
              "Bundle imported",
              `${written} file(s) written`
                + (kept ? `, ${kept} kept — tick overwrite to replace them` : "")
            );
            setEditor();
            ctx.refreshCombos();
            await loadLibrary();
            if (isTemplate && report.template_slug) {
              // opening the template runs the missing-LoRA check + download
              // offer (selectTemplate → refreshLoraBanner)
              if (await selectTemplate(report.template_slug)) switchTab("compose");
              else await warnMissingLoras(report); // load failed — no banner to reach
            } else {
              await warnMissingLoras(report); // section bundle: the banner never fires
            }
          } catch (err) {
            errorLine.textContent = err.message;
          } finally {
            busy = false;
            importButton.disabled = false;
          }
        },
      },
      "Import"
    );

    const loraCount = (plan.loras ?? []).length;
    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        `Import ${bundle.kind}`,
        el("span", { class: "mrln-slug" }, ` ${bundle.slug ?? ""}`),
        editorCloseBtn()
      ),
      isTemplate ? field("Import as", slugInput) : null,
      planList,
      loraCount
        ? el(
            "div",
            { class: "mrln-note" },
            `${loraCount} LoRA file(s) referenced — after import, `
              + (isTemplate
                ? "opening the template offers to download any that are missing "
                  + "(via their Civitai AIR)."
                : "any that are missing are named in a toast; the Library tab "
                  + "downloads them via their Civitai AIR.")
          )
        : null,
      el(
        "label",
        { class: "mrln-note" },
        overwriteBox,
        " overwrite my existing files where the bundle collides"
      ),
      errorLine,
      el("div", { class: "mrln-actions" }, importButton)
    );
  }

  function renderLibraryTab() {
    if (!state.library) {
      libraryTab.replaceChildren(
        state.libraryError
          ? libraryErrorNote(state.libraryError)
          : loadingNote("Loading prompt library…")
      );
      return;
    }
    const lib = state.library;
    const filterInput = el("input", {
      type: "text",
      class: "mrln-lib-filter",
      placeholder: "Filter sections & templates…",
      value: state.libFilter ?? "",
      oninput: (e) => {
        state.libFilter = e.target.value;
        renderLibraryTab();
        // re-render replaces the input — restore typing focus at the end
        const fresh = libraryTab.querySelector(".mrln-lib-filter");
        if (fresh) {
          fresh.focus();
          fresh.setSelectionRange(fresh.value.length, fresh.value.length);
        }
      },
    });
    libraryTab.replaceChildren(
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: () => {
              if (confirmReplaceEditor()) newSection();
            },
          },
          "New section…"
        ),
        el(
          "button",
          {
            class: "mrln-btn",
            title: "Group several sections into ONE draw pool — each picked "
              + "section becomes a weighted entry that delegates to it",
            onclick: () => {
              if (confirmReplaceEditor()) newCombineSection();
            },
          },
          "New combine…"
        ),
        el("button", { class: "mrln-btn", onclick: () => newTemplate() }, "New template…"),
        el(
          "button",
          {
            class: "mrln-btn",
            title: "Import a shared MRLN bundle (.mrln.json) — sections, template and "
              + "LoRA download links included",
            onclick: () => {
              if (confirmReplaceEditor()) importBundlePicker();
            },
          },
          "Import…"
        ),
        el("button", { class: "mrln-btn", onclick: () => loadLibrary() }, "Reload")
      ),
      filterInput,
      treeBlock("sections", "Sections", lib.sections, sectionLi),
      treeBlock("templates", "Templates", lib.templates, templateLi),
      profilesBlock(),
      el("hr", { class: "mrln-sep" }),
      editorBox
    );
  }

  function profilesBlock() {
    const rows = (state.library.profiles ?? []).map((p) =>
      el(
        "li",
        {
          onclick: () => {
            if (confirmReplaceEditor()) openProfileEditor(p.name);
          },
        },
        p.name,
        el("span", { class: "mrln-slug" }, " target-model profile"),
        el(
          "span",
          {
            class: `mrln-chip${p.tier === "factory" ? " mrln-factory" : " mrln-user"}`,
            title: p.tier === "factory+user"
              ? "Factory entry with your user-tier overlay"
              : p.tier === "user"
                ? "Your user-tier profile"
                : "Factory profile — saving creates a user overlay",
          },
          p.tier === "factory+user" ? "F+U" : p.tier === "user" ? "U" : "F"
        )
      )
    );
    const stateKey = "profiles:@block";
    return el(
      "details",
      {
        class: "mrln-fold mrln-tree-block",
        open: state.libGroups.has(stateKey) ? "" : null,
        ontoggle: (e) => {
          if (e.target.open) state.libGroups.add(stateKey);
          else state.libGroups.delete(stateKey);
        },
      },
      el(
        "summary",
        { class: "mrln-tree-head" },
        "Profiles (target models)",
        el("span", { class: "mrln-slug" }, ` ${rows.length}`)
      ),
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: () => {
              if (confirmReplaceEditor()) openProfileEditor(null);
            },
          },
          "New profile…"
        )
      ),
      el("ul", { class: "mrln-tree" }, rows)
    );
  }

  async function openProfileEditor(name) {
    let body = { name: "", merged: {}, factory: null, user: null };
    if (name) {
      try {
        body = await ctx.apiJson(`/mrln/prompt/profile?name=${encodeURIComponent(name)}`);
      } catch (err) {
        setEditor(el("div", { class: "mrln-error" }, err.message));
        return;
      }
    }
    const merged = body.merged ?? {};
    const nameInput = el("input", {
      type: "text",
      value: body.name ?? "",
      placeholder: "e.g. my-model (lowercase-kebab)",
    });
    const systemArea = autoArea(
      { placeholder: "System prompt the LLM enhancer uses for this target model" },
      merged.llm?.system ?? ""
    );
    const formatSelect = el("select", {
      title: "Render format this profile applies (explicit node widget still wins)",
    });
    for (const fmt of ["(inherit)", "string", "string_labeled", "json", "json_flat"]) {
      formatSelect.append(el("option", { value: fmt }, fmt));
    }
    formatSelect.value = merged.render?.format ?? "(inherit)";
    const lengthSelect = el("select", { title: "Item text length this profile applies" });
    for (const length of ["(inherit)", "long", "short"]) {
      lengthSelect.append(el("option", { value: length }, length));
    }
    lengthSelect.value = merged.render?.text_length ?? "(inherit)";
    const paramsArea = autoArea(
      { placeholder: '{"max_words": 220} — free JSON handed to the enhancer' },
      merged.llm?.params ? JSON.stringify(merged.llm.params, null, 2) : ""
    );
    const scaffoldArea = autoArea(
      { placeholder: 'optional json_template — e.g. {"prompt": "{positive}", "negative_prompt": "{negative}"}' },
      merged.json_template ? JSON.stringify(merged.json_template, null, 2) : ""
    );
    const errorLine = el("div", { class: "mrln-error" });

    async function save() {
      const target = nameInput.value.trim();
      const data = {};
      const render = {};
      if (formatSelect.value !== "(inherit)") render.format = formatSelect.value;
      if (lengthSelect.value !== "(inherit)") render.text_length = lengthSelect.value;
      if (Object.keys(render).length) data.render = render;
      const llm = {};
      if (systemArea.value.trim()) llm.system = systemArea.value.trim();
      if (paramsArea.value.trim()) {
        try {
          llm.params = JSON.parse(paramsArea.value);
        } catch (err) {
          errorLine.textContent = `params: ${err.message}`;
          return;
        }
      }
      if (Object.keys(llm).length) data.llm = llm;
      if (scaffoldArea.value.trim()) {
        try {
          data.json_template = JSON.parse(scaffoldArea.value);
        } catch (err) {
          errorLine.textContent = `json_template: ${err.message}`;
          return;
        }
      }
      try {
        await ctx.apiJson("/mrln/prompt/save-profile", {
          method: "POST",
          body: { name: target, data },
        });
      } catch (err) {
        errorLine.textContent = err.message;
        return;
      }
      errorLine.textContent = "";
      ctx.toast("success", "Profile saved", `${target} (user tier — overlays factory)`);
      ctx.refreshCombos(); // the node's profile combo picks it up
      await loadLibrary();
      if (state.slug) await refreshDetail();
      openProfileEditor(target);
    }

    const actions = [
      el("button", { class: "mrln-btn mrln-primary", onclick: save }, "Save to user tier"),
    ];
    if (body.user) {
      actions.push(
        el(
          "button",
          {
            class: "mrln-btn",
            title: "Remove your user-tier entry — factory content (if any) shows through again",
            // two-step arm: this wipes the profile file (system prompt,
            // params, json_template) irrecoverably, one row over from Save
            onclick: (e) =>
              armDestructive(e.currentTarget, "Really delete?", async () => {
                try {
                  await ctx.apiJson("/mrln/prompt/save-profile", {
                    method: "POST",
                    body: { name: body.name, data: null },
                  });
                } catch (err) {
                  ctx.toast("error", "Delete failed", err.message);
                  return;
                }
                ctx.toast("success", "User entry deleted", body.name);
                ctx.refreshCombos();
                await loadLibrary();
                if (state.slug) await refreshDetail();
                setEditor();
              }),
          },
          "Delete user entry"
        )
      );
    }

    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        name ? `Profile: ${name}` : "New profile",
        body.user
          ? el("span", { class: "mrln-chip mrln-user" }, body.factory ? "factory+user" : "user")
          : name
            ? el("span", { class: "mrln-chip mrln-factory" }, "factory")
            : null,
        editorCloseBtn()
      ),
      el(
        "div",
        { class: "mrln-note" },
        "Explicit per-model guidance: each target model gets its own entry even "
          + "when instructions overlap. Saving writes your USER tier, overlaying "
          + "the factory entry field by field; templates can extend further and "
          + "the node's profile widget selects (template guides, user decides)."
      ),
      field("Name", nameInput),
      field("System prompt (LLM enhancer)", systemArea),
      el(
        "div",
        { class: "mrln-grid2" },
        field("Render format", formatSelect),
        field("Text length", lengthSelect)
      ),
      field("Params (JSON)", paramsArea),
      field("json_template scaffold (optional)", scaffoldArea),
      errorLine,
      el("div", { class: "mrln-actions" }, ...actions)
    );
  }

  function renderSettingsTab() {
    // The key is stored SERVER-side in your user tier (settings.json) and
    // never echoed back — it must never live in a node widget, because
    // widget values persist into workflow PNGs.
    const keyInput = el("input", {
      type: "password",
      placeholder: state.civitaiKeySet
        ? "•••••••• (key stored — enter a new one to replace, empty to keep)"
        : "Civitai API key (optional — unlocks restricted models)",
      autocomplete: "off",
    });
    const status = el("span", { class: "mrln-note" });
    const backendRow = (label, key, provider) => {
      const urlInput = el("input", {
        type: "text",
        placeholder: `${label} URL`,
        title: `${label} endpoint used by the Prompt Enhance (MRLN) node`,
      });
      const rowStatus = el("span", { class: "mrln-note" }, "checking…");
      const check = async (persist = false) => {
        rowStatus.textContent = "…";
        try {
          if (persist) {
            await ctx.apiJson("/mrln/prompt/save-settings", {
              method: "POST",
              body: { llm: { [key]: urlInput.value } },
            });
          }
          const body = await ctx.apiJson(`/mrln/prompt/llm-validate?provider=${provider}`);
          rowStatus.textContent = `✓ ${body.models.length} model(s): ${body.models
            .slice(0, 3)
            .join(", ")}${body.models.length > 3 ? ", …" : ""}`;
          rowStatus.style.color = "#6ca";
          rowStatus.title = body.models.join("\n");
        } catch (err) {
          rowStatus.textContent = `✗ ${err.message}`;
          rowStatus.style.color = "#e88";
          rowStatus.title = "";
        }
      };
      const row = el(
        "div",
        { class: "mrln-inline" },
        urlInput,
        el("button", { class: "mrln-btn", onclick: () => check(true) }, "Validate")
      );
      return { row, rowStatus, urlInput, check };
    };
    const ollama = backendRow("Ollama", "ollama_url", "ollama");
    const lmstudio = backendRow("LM Studio", "lmstudio_url", "lmstudio");
    // Cloud keys: stored server-side (user tier settings.json), NEVER echoed
    // back — the response only says whether one exists (green check).
    const cloudRow = (label, provider) => {
      const input = el("input", {
        type: "password",
        autocomplete: "off",
        placeholder: `${label} API key`,
      });
      const mark = el("span", { class: "mrln-note" }, "");
      const setMark = (isSet) => {
        mark.textContent = isSet ? "✓ key stored" : "no key";
        mark.style.color = isSet ? "#6ca" : "";
      };
      const push = async (value) => {
        try {
          const body = await ctx.apiJson("/mrln/prompt/save-settings", {
            method: "POST",
            body: { llm_api_keys: { [provider]: value } },
          });
          input.value = "";
          setMark(body.llm_keys_set?.[provider]);
          ctx.toast("success", "Settings saved", `${label} key ${value ? "stored" : "cleared"}`);
        } catch (err) {
          ctx.toast("error", "Settings save failed", err.message);
        }
      };
      const row = el(
        "div",
        { class: "mrln-inline" },
        el("span", { class: "mrln-cloud-label" }, label),
        input,
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: () => {
              if (input.value.trim()) push(input.value.trim());
            },
          },
          "Save"
        ),
        el("button", { class: "mrln-btn", onclick: () => push("") }, "Clear"),
        mark
      );
      return { row, setMark };
    };
    const clouds = [
      ["anthropic", cloudRow("Anthropic", "anthropic")],
      ["openai", cloudRow("OpenAI", "openai")],
      ["gemini", cloudRow("Gemini", "gemini")],
      ["openrouter", cloudRow("OpenRouter", "openrouter")],
    ];
    const refresh = async () => {
      try {
        const body = await ctx.apiJson("/mrln/prompt/settings");
        state.civitaiKeySet = body.civitai_key_set;
        status.textContent = body.civitai_key_set
          ? "key stored (server-side, user tier)"
          : "no key stored — public models still resolve by hash";
        ollama.urlInput.value = body.llm?.ollama_url ?? "";
        lmstudio.urlInput.value = body.llm?.lmstudio_url ?? "";
        for (const [provider, cloud] of clouds) cloud.setMark(body.llm_keys_set?.[provider]);
      } catch {
        status.textContent = "";
      }
      // auto-check the local backends — green marks without a click
      ollama.check();
      lmstudio.check();
    };
    refresh();
    const save = async (clear) => {
      try {
        const body = await ctx.apiJson("/mrln/prompt/save-settings", {
          method: "POST",
          body: { civitai_api_key: clear ? "" : keyInput.value },
        });
        state.civitaiKeySet = body.civitai_key_set;
        keyInput.value = "";
        ctx.toast(
          "success",
          "Composer settings saved",
          body.civitai_key_set ? "Civitai key stored" : "Civitai key cleared"
        );
        refresh();
      } catch (err) {
        ctx.toast("error", "Settings save failed", err.message);
      }
    };
    settingsTab.replaceChildren(
      el("div", { class: "mrln-tree-head" }, "Civitai"),
      el(
        "div",
        { class: "mrln-note" },
        "Used by LoRA blocks to look up trigger words + AIR tags by file hash. "
          + "The key is stored server-side in your user tier and never echoed back."
      ),
      el(
        "div",
        { class: "mrln-inline" },
        keyInput,
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: () => {
              if (keyInput.value.trim()) save(false);
            },
          },
          "Save key"
        ),
        el("button", { class: "mrln-btn", onclick: () => save(true) }, "Clear")
      ),
      status,
      el("hr", { class: "mrln-sep" }),
      el("div", { class: "mrln-tree-head" }, "Local LLM backends"),
      el(
        "div",
        { class: "mrln-note" },
        "Used by the Prompt Enhance (MRLN) node — checked automatically on "
          + "open; Validate saves an edited URL and re-checks. The model list "
          + "feeds the node's dropdown."
      ),
      ollama.row,
      ollama.rowStatus,
      lmstudio.row,
      lmstudio.rowStatus,
      el("hr", { class: "mrln-sep" }),
      el("div", { class: "mrln-tree-head" }, "Cloud LLM API keys"),
      el(
        "div",
        { class: "mrln-note" },
        "Unlock the cloud backends of Prompt Enhance and the LLM de-composer. "
          + "Keys are stored server-side in your user tier, never echoed back "
          + "and never in a node widget (widgets persist into workflow PNGs)."
      ),
      ...clouds.flatMap(([, cloud]) => [cloud.row])
    );
  }

  // ---- combine sections ----------------------------------------------------
  // A "combine" is an ordinary section whose every item just delegates to
  // ANOTHER section through a child slot. Drawing it picks which source to
  // draw from (weights included), which is how you group several sections
  // into one pool. The engine has always supported this; only building one
  // by hand was tedious, so this generates the structure and hands it to the
  // normal section editor — no new schema, no new save path.

  function newCombineSection(existing = null) {
    // existing: {slug, body} when re-opening a section that IS a combine
    const chosen = new Map(); // section slug -> weight
    if (existing) {
      for (const item of existing.body.items ?? []) {
        if (isCombineItem(item)) chosen.set(item.slots[0].ref, Number(item.weight) || 1);
      }
    }
    const labelInput = el("input", {
      type: "text",
      value: existing?.body.label ?? "",
      placeholder: "e.g. Anywhere — urban, nature or studio",
    });
    const filterInput = el("input", {
      type: "text",
      placeholder: "Filter sections…",
      oninput: () => renderPicks(),
    });
    const pickList = el("div", { class: "mrln-combine-picks" });
    const chosenList = el("div", { class: "mrln-combine-chosen" });
    const errorLine = el("div", { class: "mrln-error" });

    function renderChosen() {
      const rows = [...chosen.entries()].map(([slug, weight]) =>
        el(
          "div",
          { class: "mrln-inline" },
          el("span", {}, slug),
          el("input", {
            type: "number",
            class: "mrln-narrow",
            min: "0.1",
            step: "0.1",
            value: String(weight),
            title: "Draw weight — 2 means twice as likely as a 1",
            oninput: (e) => chosen.set(slug, Math.max(0.1, Number(e.target.value) || 1)),
          }),
          smallBtn("Remove from the combine", "✕", () => {
            chosen.delete(slug);
            renderChosen();
            renderPicks();
          })
        )
      );
      chosenList.replaceChildren(
        el("div", { class: "mrln-field-name" }, `Combining ${chosen.size} section(s)`),
        ...(rows.length ? rows : [el("div", { class: "mrln-note" }, "nothing picked yet")])
      );
    }

    function renderPicks() {
      const filter = filterInput.value.trim().toLowerCase();
      const rows = (state.library?.sections ?? [])
        .filter((s) => !chosen.has(s.slug))
        .filter((s) => !filter || s.slug.toLowerCase().includes(filter))
        .slice(0, 40)
        .map((s) =>
          el(
            "div",
            {
              class: "mrln-combine-pick",
              onclick: () => {
                chosen.set(s.slug, 1);
                renderChosen();
                renderPicks();
              },
            },
            `+ ${s.slug}`,
            el("span", { class: "mrln-slug" }, ` ${s.item_count ?? "?"} items`)
          )
        );
      pickList.replaceChildren(
        ...(rows.length ? rows : [el("div", { class: "mrln-note" }, "no matches")])
      );
    }

    function build() {
      if (!chosen.size) {
        errorLine.textContent = "pick at least one section to combine";
        return;
      }
      const items = [...chosen.entries()].map(([slug, weight]) => combineItem(slug, weight));
      const names = new Set();
      for (const item of items) {
        let name = item.name;
        for (let n = 2; names.has(name); n++) name = `${item.name}-${n}`;
        item.name = name;
        names.add(name);
      }
      // hand off to the ordinary editor: from here it is just a section
      openSectionForm(existing?.slug ?? null, {
        label: labelInput.value.trim(),
        description: existing?.body.description ?? "",
        negative: existing?.body.negative ?? "",
        items,
        raw: existing?.body.raw ?? { items: [] },
        factory_raw: existing?.body.factory_raw ?? null,
        merged: false,
        replaces: Boolean(existing?.body.replaces),
        tier: existing?.body.tier ?? "",
      });
    }

    renderChosen();
    renderPicks();
    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        existing ? `Combine: ${existing.slug}` : "New combine section",
        editorCloseBtn()
      ),
      el(
        "div",
        { class: "mrln-note" },
        "Each picked section becomes one entry; drawing this section picks an "
          + "entry (by weight) and then draws from that section. Build it, then "
          + "the normal section editor opens so you can save it."
      ),
      field("Label", labelInput),
      chosenList,
      field("Add a section", filterInput),
      pickList,
      errorLine,
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn mrln-primary", onclick: build }, "Build →")
      )
    );
  }

  function newSection() {
    openSectionForm(null, {
      label: "",
      description: "",
      negative: "",
      items: [{ name: "", text: "" }],
      raw: { items: [] },
      factory_raw: null,
      merged: false,
      replaces: false,
      tier: "",
    });
  }

  async function openSectionEditor(slug) {
    let body;
    try {
      body = await ctx.apiJson(`/mrln/prompt/section?slug=${encodeURIComponent(slug)}`);
    } catch (err) {
      setEditor(el("div", { class: "mrln-error" }, err.message));
      return;
    }
    const items = body.items ?? [];
    if (items.length && items.every(isCombineItem)) {
      // a section that is nothing but delegations edits far better as the
      // pick-and-weight list it came from than as a table of "{pick}" rows
      newCombineSection({ slug, body });
      return;
    }
    openSectionForm(slug, body);
  }

  let loraListCache = null;
  async function installedLoras() {
    if (loraListCache?.length) return loraListCache;
    let names = [];
    // primary: the dedicated models endpoint (full list incl. subfolders —
    // modern frontends load combos lazily, so object_info may be incomplete)
    try {
      const viaModels = await ctx.apiJson("/models/loras");
      if (Array.isArray(viaModels)) {
        names = viaModels.map((entry) => (typeof entry === "string" ? entry : entry?.name)).filter(Boolean);
      }
    } catch {
      /* older server without /models */
    }
    if (!names.length) {
      try {
        const info = await ctx.apiJson("/object_info/LoraLoader");
        const spec = info?.LoraLoader?.input?.required?.lora_name;
        if (Array.isArray(spec)) {
          if (Array.isArray(spec[0])) names = spec[0];
          else if (Array.isArray(spec[1]?.options)) names = spec[1].options;
        }
      } catch {
        /* endpoint unavailable */
      }
    }
    loraListCache = [...new Set(names)].sort((a, b) => a.localeCompare(b));
    return loraListCache;
  }

  function loraPicker(current) {
    // A drill-down browser in dropdown clothes: the list shows the current
    // folder's subfolders + files, clicking a folder descends IN PLACE
    // (native selects close on click, so this is a custom menu), '..' goes
    // up a level. The filter searches flat across all folders.
    const value = el("input", { type: "hidden", value: current ?? "" });
    const baseOf = (name) => name.slice(Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\")) + 1);
    const dirOf = (name) => {
      const cut = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
      return cut === -1 ? "" : name.slice(0, cut).replace(/\\/g, "/");
    };
    const control = el(
      "button",
      { type: "button", class: "mrln-btn mrln-lora-current", title: current || "" },
      current ? baseOf(current) : "— choose LoRA —"
    );
    const menu = el("div", { class: "mrln-brace-menu mrln-lora-menu", style: "display:none" });
    const wrap = el("span", { class: "mrln-assist" }, control, menu, value);
    const filter = el("input", {
      type: "text",
      class: "mrln-lora-filter",
      placeholder: "filter…",
      title: "Substring filter across all folders",
    });
    let names = current ? [current] : [];
    let cwd = current ? dirOf(current) : "";
    let open = false;
    const onScroll = (e) => {
      if (!menu.contains(e.target)) hide(); // fixed menus must not desync
    };
    const hide = () => {
      open = false;
      menu.style.display = "none";
      window.removeEventListener("scroll", onScroll, true);
    };
    const choose = (name) => {
      value.value = name;
      control.textContent = baseOf(name);
      control.title = name;
      hide();
      value.dispatchEvent(new Event("change"));
    };
    const entry = (cls, text, action) =>
      el(
        "div",
        {
          class: `mrln-brace-item${cls ? ` ${cls}` : ""}`,
          title: text, // ellipsised rows reveal their full name on hover
          onmousedown: (e) => {
            e.preventDefault(); // keep focus → no blur-close before the click
            action();
          },
        },
        text
      );
    const render = () => {
      const needle = filter.value.trim().toLowerCase();
      const out = [];
      if (needle) {
        const hits = names.filter((n) => n.toLowerCase().includes(needle));
        for (const name of hits.slice(0, 200)) {
          out.push(entry(name === value.value ? "mrln-lora-sel" : "", name.replace(/\\/g, "/"), () => choose(name)));
        }
        if (!out.length) out.push(el("div", { class: "mrln-note", style: "padding:3px 6px" }, "no matches"));
      } else {
        if (cwd) {
          out.push(el("div", { class: "mrln-note", style: "padding:2px 6px" }, `📁 ${cwd}/`));
          out.push(
            entry("mrln-lora-dir", "📁 ..", () => {
              cwd = cwd.includes("/") ? cwd.slice(0, cwd.lastIndexOf("/")) : "";
              render();
            })
          );
        }
        const prefix = cwd ? `${cwd}/` : "";
        const subdirs = new Set();
        const files = [];
        for (const name of names) {
          const norm = name.replace(/\\/g, "/");
          if (!norm.startsWith(prefix)) continue;
          const rest = norm.slice(prefix.length);
          const slash = rest.indexOf("/");
          if (slash === -1) files.push(name);
          else subdirs.add(rest.slice(0, slash));
        }
        for (const d of [...subdirs].sort((a, b) => a.localeCompare(b))) {
          out.push(
            entry("mrln-lora-dir", `📁 ${d}/`, () => {
              cwd = prefix + d;
              render();
            })
          );
        }
        for (const name of files.sort((a, b) => baseOf(a).localeCompare(baseOf(b)))) {
          out.push(entry(name === value.value ? "mrln-lora-sel" : "", baseOf(name), () => choose(name)));
        }
      }
      menu.replaceChildren(...out);
      menu.style.display = "";
      placeMenu(control, menu);
      if (!open) window.addEventListener("scroll", onScroll, true);
      open = true;
    };
    control.addEventListener("click", () => {
      if (open) {
        hide();
        return;
      }
      cwd = value.value ? dirOf(value.value) : cwd;
      render();
    });
    control.addEventListener("blur", () => setTimeout(hide, 150));
    filter.addEventListener("input", render);
    filter.addEventListener("blur", () => setTimeout(hide, 150));
    installedLoras().then((list) => {
      if (list.length) names = list;
      if (value.value && !names.includes(value.value)) {
        control.textContent = `⚠ ${baseOf(value.value)}`;
        control.title = `${value.value} — not installed`;
      }
    });
    return { file: value, filter, control: wrap, set: choose };
  }

  function openSectionForm(slug, body) {
    // Factory sections COMPOUND: the default save writes only your changes
    // (edited/new items, tombstones for hidden ones) as a thin extend file
    // that survives factory updates. 'Replace' opts into a full frozen copy.
    const factoryBaseline = body.factory_raw ?? (body.tier === "factory" ? body.raw : null);
    const hasFactory = Boolean(factoryBaseline);
    let saveMode = hasFactory ? (body.replaces ? "replace" : "extend") : "standalone";

    const slugInput = el("input", { type: "text", value: slug ?? "", placeholder: "folder/name" });
    const labelInput = el("input", { type: "text", value: body.label ?? "" });
    const descInput = el("input", { type: "text", value: body.description ?? "" });
    const negInput = el("input", { type: "text", value: body.negative ?? "" });
    const suitsInput = el("input", {
      type: "text",
      value: (body.raw?.suits ?? body.factory_raw?.suits ?? []).join(", "),
      placeholder: "e.g. object, car — empty = universal (offered to every template)",
      title: "Which template types this section serves; typed templates filter "
        + "their pickers and random draws by this. Explicit picks are never restricted.",
    });
    const itemRows = [];
    const table = el("table", { class: "mrln-items-table" });
    table.append(
      el(
        "tr",
        {},
        el("td", { class: "mrln-w-origin" }),
        el("td", { class: "mrln-w-name mrln-note" }, "name"),
        el("td", { class: "mrln-note" }, "text"),
        el("td", { class: "mrln-w-weight mrln-note" }, "wt"),
        el("td", { class: "mrln-w-act" })
      )
    );

    function addItemRow(item = { name: "", text: "" }) {
      const row = {
        orig: item,
        hidden: Boolean(item.hidden),
        slots: (item.slots ?? []).map((s) => ({ ...s })), // child refs, grown via '{'
        name: el("input", { type: "text", value: item.name ?? "" }),
        text: el("input", { type: "text", value: item.text ?? "", title: item.text ?? "" }),
        weight: el("input", { type: "text", value: item.weight ?? "" }),
      };
      // '{' assist: pick a declared child, the trigger, or any section —
      // picking a section auto-declares it as a child slot of this item,
      // so its draw weaves into the text bare (no lead-in)
      const itemRefOptions = () => {
        const options = [];
        const used = new Set(["trigger"]);
        for (const s of row.slots) {
          options.push({ name: s.id, hint: `child → ${s.ref}` });
          used.add(s.id);
        }
        options.push({ name: "trigger", hint: "node trigger widget" });
        for (const sec of state.library?.sections ?? []) {
          if (row.slots.some((s) => s.ref === sec.slug)) continue;
          const base = sec.slug.split("/").pop();
          let id = base;
          let n = 2;
          while (used.has(id)) id = `${base}-${n++}`;
          used.add(id);
          options.push({ name: id, hint: sec.slug, create: { id, ref: sec.slug } });
        }
        return options;
      };
      const itemKnown = () => new Set(["trigger", ...row.slots.map((s) => s.id)]);
      row.text.dataset.mrlnBaseTitle = item.text ?? "";
      const textAssist = braceAssist(row.text, itemRefOptions, (option) => {
        if (option.create) row.slots.push({ ...option.create });
      });
      row.text.addEventListener("input", () => validateRefs(row.text, itemKnown));
      validateRefs(row.text, itemKnown);
      const fromFactory = item.origin === "factory";
      const originChip = fromFactory
        ? el("span", { class: "mrln-chip mrln-factory", title: "Lives in the factory tier" }, "F")
        : item.origin === "user"
          ? el("span", { class: "mrln-chip mrln-user", title: "Lives in your user tier" }, "U")
          : null;
      const actionButton = el(
        "button",
        {
          class: "mrln-btn",
          title: fromFactory
            ? "Hide this factory item from your pools (a tombstone in your user file — restorable)"
            : "Remove item",
          onclick: () => {
            if (fromFactory) {
              row.hidden = !row.hidden;
              tr.classList.toggle("mrln-hidden-item", row.hidden);
              actionButton.textContent = row.hidden ? "↩" : "🚫";
            } else {
              itemRows.splice(itemRows.indexOf(row), 1);
              tr.remove();
            }
          },
        },
        fromFactory ? (row.hidden ? "↩" : "🚫") : "✕"
      );
      const tr = el(
        "tr",
        { class: row.hidden ? "mrln-hidden-item" : null },
        el("td", { class: "mrln-w-origin" }, originChip),
        el("td", { class: "mrln-w-name" }, row.name),
        el("td", {}, textAssist),
        el("td", { class: "mrln-w-weight" }, row.weight),
        el("td", { class: "mrln-w-act" }, actionButton)
      );
      itemRows.push(row);
      table.append(tr);
      if (item.data?.lora !== undefined) {
        // LoRA block: an extra editor line for the loader metadata — the
        // text above stays the catchword that lands in the prompt.
        const picker = loraPicker(item.data.lora ?? "");
        row.lora = picker.file;
        row.loraFilter = picker.filter;
        row.loraControl = picker.control;
        row.loraSet = picker.set;
        row.sm = el("input", {
          type: "text",
          inputmode: "decimal",
          value: item.data.strength_model ?? 1.0,
          title: "strength_model",
        });
        row.sc = el("input", {
          type: "text",
          inputmode: "decimal",
          value: item.data.strength_clip ?? item.data.strength_model ?? 1.0,
          title: "strength_clip",
        });
        row.comment = el("input", {
          type: "text",
          value: item.data.comment ?? "",
          placeholder: "comment / AIR",
          title: "Free comment stored with this LoRA block — the Civitai lookup "
            + "fills in the model's AIR tag here",
        });
        // the base family lets LoRA Apply warn when this LoRA meets a model
        // of another architecture; an AIR already encodes it, so this only
        // has to be typed for files that carry none
        row.base = el("input", {
          type: "text",
          class: "mrln-narrow",
          value: item.data.base ?? "",
          placeholder: "base",
          title: "Base-model family this LoRA was trained for (flux1, sdxl, sd1, "
            + "qwen, pony…). LoRA Apply warns when it does not match the connected "
            + "model. Left empty, it is read from the AIR's ecosystem segment.",
        });
        let loraMetaReq = 0;
        row.lora.addEventListener("change", async () => {
          if (!row.name.value.trim()) {
            const stem = row.lora.value.split(/[\\/]/).pop().replace(/\.\w+$/, "");
            row.name.value = stem.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40);
          }
          // switching files switches the trigger: file metadata first, then
          // Civitai by file hash (trigger fallback + AIR tag for the comment)
          const file = row.lora.value;
          row.text.classList.remove("mrln-input-error");
          if (!file) return;
          const req = ++loraMetaReq;
          let found = false;
          try {
            const meta = await ctx.apiJson(
              `/mrln/prompt/lora-meta?name=${encodeURIComponent(file)}`
            );
            if (req !== loraMetaReq) return; // superseded by a newer switch
            row.text.value = meta.trigger;
            row.text.title = `trigger word from LoRA metadata (${meta.source})`;
            found = true;
          } catch {
            if (req !== loraMetaReq) return;
          }
          // auto-filled AIR follows the file on every switch; only a comment
          // the user typed themselves is preserved
          const commentIsAuto = () => {
            const current = row.comment.value.trim();
            return !current || current === row.autoAir || current.startsWith("urn:air:");
          };
          try {
            const civ = await ctx.apiJson(
              `/mrln/prompt/lora-civitai?name=${encodeURIComponent(file)}`
            );
            if (req !== loraMetaReq) return;
            if (!found && civ.trigger) {
              row.text.value = civ.trigger;
              row.text.title = `trigger word from Civitai (${civ.model_name ?? "model"}`
                + `${civ.trained_words?.length > 1 ? `; all: ${civ.trained_words.join(", ")}` : ""})`;
              found = true;
            }
            if (commentIsAuto()) {
              row.comment.value = civ.air ?? "";
              row.autoAir = civ.air ?? "";
            }
          } catch {
            if (req !== loraMetaReq) return;
            if (commentIsAuto()) {
              // the previous file's AIR must not stick to a file Civitai
              // doesn't know
              row.comment.value = "";
              row.autoAir = "";
            }
          }
          if (!found) {
            row.text.value = "";
            row.text.classList.add("mrln-input-error");
            row.text.title = "No trigger word found in file metadata or on Civitai — "
              + "type the trigger word / catchword yourself";
          }
        });
        // Missing-file healer: a template shared across machines carries the
        // file name + AIR urn — when the file is absent here, offer the
        // Civitai download (background, SHA256-verified) and re-point the
        // block if the chosen folder differs.
        row.missingBox = el("div", { class: "mrln-lora-missing", style: "display:none" });
        const norm = (n) => n.replaceAll("\\", "/").toLowerCase();
        const downloadMissing = async (file, air) => {
          const parts = file.replaceAll("\\", "/").split("/");
          const filename = parts.pop();
          const folder = await askString(
            "Download LoRA from Civitai",
            "Subfolder under your loras directory (empty = root):",
            parts.join("/")
          );
          if (folder == null) return; // cancelled
          try {
            await ctx.apiJson("/mrln/prompt/lora-download", {
              method: "POST",
              body: {
                air,
                start: true,
                folder,
                filename,
                section: slug ?? "",
                item: row.name.value.trim(),
                stored: file,
              },
            });
          } catch (err) {
            ctx.toast("error", "Download failed to start", err.message);
            return;
          }
          const progress = el("span", { class: "mrln-note" }, "starting download…");
          row.missingBox.replaceChildren(progress);
          const body = await pollLoraDownload(air, (tick) => {
            progress.textContent = `downloading… ${loraProgressText(tick)}`;
          });
          if (body.status !== "done") {
            ctx.toast("error", "LoRA download failed", body.detail ?? "");
            checkMissing();
            return;
          }
          loraListCache = null; // pickers must list the new file
          ctx.toast(
            "success",
            "LoRA downloaded",
            body.healed ? `${body.name} — LoRA block re-pointed` : body.name
          );
          if (body.healed) {
            row.loraSet(body.healed); // picker + trigger/AIR follow the heal
            await refreshDetail(); // compose tab pools reflect the new path
          } else {
            checkMissing();
            refreshLoraBanner(state.slug); // one entry fewer on the compose banner
          }
        };
        const checkMissing = async () => {
          const file = row.lora.value;
          if (!file) {
            row.missingBox.style.display = "none";
            return;
          }
          const names = await installedLoras();
          if (!names.length || names.some((n) => norm(n) === norm(file))) {
            row.missingBox.style.display = "none";
            return;
          }
          const air = (row.comment.value || "").trim();
          row.missingBox.style.display = "";
          row.missingBox.replaceChildren(
            el("span", { class: "mrln-error" }, "file missing on this machine"),
            air.toLowerCase().startsWith("urn:air:")
              ? el(
                  "button",
                  {
                    class: "mrln-btn",
                    title: `Download ${air} from Civitai into your loras folder `
                      + "(background, SHA256-verified) and re-point this block "
                      + "if the path changes",
                    onclick: () => downloadMissing(file, air),
                  },
                  "⬇ Get from Civitai"
                )
              : el(
                  "span",
                  { class: "mrln-note" },
                  "no AIR in the comment — pick a local file instead"
                )
          );
        };
        row.lora.addEventListener("change", checkMissing);
        checkMissing();
        table.append(
          el(
            "tr",
            { class: "mrln-lora-row" },
            el("td", { class: "mrln-w-origin" }, el("span", { class: "mrln-chip mrln-user" }, "LoRA")),
            el("td", { colspan: 2 }, el("div", { class: "mrln-inline" }, row.loraFilter, row.loraControl)),
            el("td", { class: "mrln-w-weight" }, el("div", { class: "mrln-inline" }, row.sm, row.sc)),
            el("td", { class: "mrln-w-act" })
          ),
          el(
            "tr",
            { class: "mrln-lora-row mrln-lora-end" },
            el("td", { class: "mrln-w-origin" }),
            el(
              "td",
              { colspan: 3 },
              el("div", { class: "mrln-inline" }, row.comment, row.base),
              row.missingBox
            ),
            el("td", { class: "mrln-w-act" })
          )
        );
      }
    }
    for (const item of body.items ?? []) addItemRow(item);

    function cleanedItem(row) {
      const item = { ...row.orig, name: row.name.value.trim(), text: row.text.value };
      if (row.slots.length) item.slots = row.slots.map((s) => ({ ...s }));
      delete item.origin; // runtime provenance, never persisted
      delete item.hidden;
      if (!item.name) delete item.name;
      const weight = parseFloat(row.weight.value);
      if (!Number.isNaN(weight) && weight !== 1) item.weight = weight;
      else delete item.weight;
      for (const key of ["negative", "text_short"]) if (!item[key]) delete item[key];
      for (const key of ["tags", "excludes", "requires", "slots"]) {
        if (Array.isArray(item[key]) && !item[key].length) delete item[key];
      }
      if (row.lora) {
        const name = row.lora.value.trim();
        if (name) {
          const sm = parseFloat(row.sm.value);
          const sc = parseFloat(row.sc.value);
          item.data = {
            ...(item.data ?? {}),
            lora: name,
            strength_model: Number.isNaN(sm) ? 1.0 : sm,
            strength_clip: Number.isNaN(sc) ? (Number.isNaN(sm) ? 1.0 : sm) : sc,
          };
          const comment = row.comment?.value.trim();
          if (comment) item.data.comment = comment;
          else delete item.data.comment;
          const base = row.base?.value.trim().toLowerCase();
          if (base) item.data.base = base;
          else delete item.data.base;
        } else if (item.data) {
          delete item.data.lora;
          delete item.data.strength_model;
          delete item.data.strength_clip;
          delete item.data.base;
        }
      }
      if (item.data == null || (typeof item.data === "object" && !Object.keys(item.data).length)) {
        delete item.data;
      }
      if (row.hidden) item.hidden = true;
      return item;
    }

    function rowEdited(row) {
      return (
        row.name.value.trim() !== (row.orig.name ?? "") ||
        row.text.value !== (row.orig.text ?? "") ||
        (parseFloat(row.weight.value) || 1) !== (row.orig.weight ?? 1)
      );
    }

    function fieldValue(input, factoryValue) {
      // extend mode: equal-to-factory (or empty) inherits — omit from the file
      const value = input.value.trim();
      if (saveMode === "extend" && (!value || value === (factoryValue ?? ""))) return null;
      return value || null;
    }

    async function save() {
      const targetSlug = slugInput.value.trim();
      const extending = saveMode === "extend" && factoryBaseline;
      const data = extending ? { version: 1 } : { ...body.raw, version: 1 };
      delete data.replaces;
      const fields = [
        ["label", labelInput, factoryBaseline?.label],
        ["description", descInput, factoryBaseline?.description],
        ["negative", negInput, factoryBaseline?.negative],
      ];
      for (const [key, input, factoryValue] of fields) {
        const value = extending ? fieldValue(input, factoryValue) : input.value.trim() || null;
        if (value) data[key] = value;
        else delete data[key];
      }
      const suits = suitsInput.value.split(",").map((v) => v.trim()).filter(Boolean);
      const factorySuits = factoryBaseline?.suits ?? [];
      if (extending && JSON.stringify(suits) === JSON.stringify(factorySuits)) delete data.suits;
      else if (suits.length) data.suits = suits;
      else delete data.suits;
      if (extending) {
        // thin diff: edited/new/user items + bare tombstones for hidden ones
        data.items = [];
        for (const row of itemRows) {
          if (row.orig.origin === "factory") {
            if (row.hidden) data.items.push({ name: row.orig.name, hidden: true });
            else if (rowEdited(row)) data.items.push(cleanedItem(row));
          } else {
            data.items.push(cleanedItem(row));
          }
        }
      } else {
        // replace: hidden factory items simply stay out of the copy;
        // a user's own hidden item persists WITH its flag (soft-disabled)
        data.items = itemRows
          .filter((row) => !(row.hidden && row.orig.origin === "factory"))
          .map(cleanedItem);
        if (saveMode === "replace") data.replaces = true;
      }
      // renames of YOUR items re-point every user-tier template default
      // server-side; factory-origin rows are copies, not renames
      const renames = {};
      for (const row of itemRows) {
        const oldName = (row.orig.name ?? "").trim();
        const newName = row.name.value.trim();
        if (oldName && newName && oldName !== newName && row.orig.origin !== "factory") {
          renames[oldName] = newName;
        }
      }
      let saved;
      try {
        saved = await ctx.apiJson("/mrln/prompt/save-section", {
          method: "POST",
          body: { slug: targetSlug, data, renames },
        });
      } catch (err) {
        ctx.toast("error", "Save failed", err.message);
        return;
      }
      const how = extending
        ? "extends factory — only your changes stored"
        : saveMode === "replace"
          ? "replaces factory entirely"
          : "user library";
      const repointed = saved?.templates_rewritten
        ? ` — ${saved.templates_rewritten} template file(s) re-pointed to renamed items`
        : "";
      ctx.toast("success", "Section saved", `${targetSlug} (${how})${repointed}`);
      state.libGroups.add("sections:@block"); // reveal where it landed
      state.libGroups.add(`sections:${targetSlug.split("/")[0]}`);
      ctx.refreshCombos();
      await loadLibrary();
      openSectionEditor(targetSlug);
      if (state.slug) {
        applyItemRenames(targetSlug, renames);
        await refreshDetail(); // the loaded template may draw this section
      }
    }

    const modeSelect = el("select", {
      title: "How your user file compounds with the factory section",
      onchange: (e) => {
        saveMode = e.target.value;
      },
    });
    if (hasFactory) {
      modeSelect.append(
        el(
          "option",
          { value: "extend" },
          "extend factory — save only my changes (survives factory updates)"
        ),
        el("option", { value: "replace" }, "replace factory — full frozen copy")
      );
      modeSelect.value = saveMode;
    }

    const actions = [
      el("button", { class: "mrln-btn mrln-primary", onclick: save }, "Save to user library"),
    ];
    if (body.tier === "user") {
      actions.push(
        el(
          "button",
          {
            class: "mrln-btn",
            title: hasFactory
              ? "Delete your user file — the slug reverts to pure factory content"
              : "Delete your user file",
            onclick: () => deleteEntry("sections", slug),
          },
          "Delete user file"
        )
      );
    }

    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        slug ? `Section: ${slug}` : "New section",
        body.merged
          ? el("span", { class: "mrln-chip mrln-merged" }, "factory+user")
          : tierChip(body.tier),
        editorCloseBtn()
      ),
      body.merged
        ? el(
            "div",
            { class: "mrln-note" },
            "Combined view — F/U marks where each item lives. Saving stores only your changes."
          )
        : body.tier === "factory"
          ? el(
              "div",
              { class: "mrln-note" },
              "Factory file — saving creates a user-tier file that extends (or replaces) it."
            )
          : null,
      field("Slug", slugInput),
      field("Label", labelInput),
      field("Description", descInput),
      field("Negative", negInput),
      field("Suits (template types)", suitsInput),
      hasFactory ? field("Save mode", modeSelect) : null,
      el("span", { class: "mrln-field-name" }, "Items"),
      table,
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn", onclick: () => addItemRow() }, "+ item"),
        el(
          "button",
          {
            class: "mrln-btn",
            title: "Add a LoRA block: catchword text for the prompt plus loader "
              + "metadata (file + strengths) signalled to the LoRA Apply (MRLN) "
              + "node via the template node's loras output",
            onclick: () =>
              addItemRow({
                name: "",
                text: "",
                data: { lora: "", strength_model: 1.0, strength_clip: 1.0 },
              }),
          },
          "+ LoRA block"
        ),
        ...actions
      )
    );
  }

  async function openTemplateEditor(slug) {
    let body;
    try {
      body = await ctx.apiJson(`/mrln/prompt/template?slug=${encodeURIComponent(slug)}`);
    } catch (err) {
      setEditor(el("div", { class: "mrln-error" }, err.message));
      return;
    }
    const slugInput = el("input", { type: "text", value: slug });
    const textarea = el("textarea", { rows: 16 }, JSON.stringify(body.raw, null, 2));
    const errorLine = el("div", { class: "mrln-error" });

    async function save() {
      let data;
      try {
        data = JSON.parse(textarea.value);
      } catch (err) {
        errorLine.textContent = `Not valid JSON: ${err.message}`;
        return;
      }
      try {
        await ctx.apiJson("/mrln/prompt/save-template", {
          method: "POST",
          body: { slug: slugInput.value.trim(), data },
        });
      } catch (err) {
        errorLine.textContent = err.message;
        return;
      }
      errorLine.textContent = "";
      editorDirty = false; // the editor now matches disk
      ctx.toast("success", "Template saved", `${slugInput.value.trim()} (user library)`);
      state.libGroups.add("templates:@block");
      state.libGroups.add(`templates:${slugInput.value.trim().split("/")[0]}`);
      ctx.refreshCombos();
      await loadLibrary();
      if (slugInput.value.trim() === state.slug) {
        // the JSON just saved IS the on-disk truth — reload the compose
        // working copy, or a later compose-side Save/Apply would post the
        // stale pre-edit structure back over these edits
        await selectTemplate(state.slug);
      }
    }

    const actions = [
      el("button", { class: "mrln-btn mrln-primary", onclick: save }, "Save to user library"),
    ];
    if (body.tier === "user") {
      actions.push(
        el(
          "button",
          { class: "mrln-btn", onclick: () => deleteEntry("templates", slug) },
          "Delete user file"
        )
      );
    }
    setEditor(
      el("div", { class: "mrln-tree-head" }, `Template: ${slug}`, tierChip(body.tier), editorCloseBtn()),
      body.tier === "factory"
        ? el("div", { class: "mrln-note" }, "Factory file — saving creates a user-tier override.")
        : null,
      field("Slug", slugInput),
      el("span", { class: "mrln-field-name" }, "Template JSON (validated on save)"),
      textarea,
      errorLine,
      el("div", { class: "mrln-actions" }, ...actions)
    );
  }

  async function deleteEntry(kind, slug) {
    const confirmed = ctx.dialog?.confirm
      ? await ctx.dialog.confirm({
          title: "Delete user file",
          message: `Delete the user-tier ${kind.slice(0, -1)} '${slug}'?`,
          type: "delete",
        })
      : window.confirm(`Delete the user-tier ${kind.slice(0, -1)} '${slug}'?`);
    if (!confirmed) return;
    try {
      const body = await ctx.apiJson("/mrln/prompt/delete", {
        method: "POST",
        body: { kind, slug },
      });
      ctx.toast(
        "success",
        "Deleted",
        body.reverted_to_factory ? `${slug} reverted to factory content` : slug
      );
    } catch (err) {
      ctx.toast("error", "Delete failed", err.message);
      return;
    }
    setEditor();
    ctx.refreshCombos();
    const wasLoaded = kind === "templates" && slug === state.slug;
    if (wasLoaded) {
      // The loaded working copy points at a file that just changed identity
      // (deleted, or reverted to factory). Drop it so loadLibrary runs a
      // full selectTemplate — otherwise stale rows/edits survive under a
      // reassigned slug and a later Save would post them over an unrelated
      // template.
      state.detail = null;
      state.modified = false;
    }
    await loadLibrary();
    if (state.slug && !wasLoaded) await refreshDetail(); // pools may have reverted to factory
  }

  // ---- boot ----------------------------------------------------------------

  loadLibrary(false);

  return () => clearTimeout(state.previewTimer);
}
