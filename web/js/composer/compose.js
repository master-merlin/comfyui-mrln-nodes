// MRLN Prompt Composer — the Compose tab: template header, meta-prompt fold,
// the slot rows (with mute/solo, emphasis, drag reordering and nested child
// draws), the live preview, and the node interop (Apply / Load / Pin draw).
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). Every element this
// tab keeps across re-renders is created inside createCompose().
import {
  buildDraftData,
  buildSelectionLines as selectionLinesFor,
  moveInArray,
  overrideTweakCount,
  parseKvLines,
  parseToken,
  renameToken,
  uniqueName,
  wordPrefixMatch,
} from "./util.js";
import {
  armDestructive,
  autoArea,
  braceAssist,
  busy,
  dragHandle,
  el,
  field,
  loadingNote,
  mount,
  smallBtn,
  tierChip,
  titled,
  validateRefs,
} from "./dom.js";
import { closePicker, openPicker, pickerIsOpen } from "./picker.js";

// ---- "Optimize for …": the pure half (SPEC 5.3) -----------------------------
// Reading order is a render-time function of the PROFILE (its block_order), so
// one template on disk reads differently per target model — which is exactly
// what the per-model showcases proved changes results. The comparison is two
// preview renders that differ in exactly ONE request field. Request shaping,
// the authored/optimized diff and the write-back mapping live here so they are
// testable without a DOM (tests/js/optimize.test.mjs).

/**
 * The two preview bodies of one comparison — identical except `profile`.
 *
 * The trap: `state.profile` is the TEMPLATE VARIANT target (a template's
 * `profiles.<name>.overrides`) and rides the very same `profile` key. So the
 * baseline is whatever the live preview is showing — the "before" the user
 * would otherwise ship — and this builds its own bodies from the same pieces
 * doPreview uses instead of going through it. Nothing here writes state.
 */
export function optimizeBodies(state, target) {
  const base = {
    template: state.slug,
    seed: state.seed,
    mode: state.mode,
    selection: selectionLinesFor(state),
    variables: state.variables,
    trigger: state.trigger,
    format: state.format,
    conflict_policy: state.conflictPolicy,
    text_length: state.textLength,
  };
  // same rule as doPreview: unsaved structural edits travel as a draft
  if (state.modified) base.template_data = buildDraftData(state);
  return [
    { ...base, profile: state.profile ?? "standard" },
    { ...base, profile: target },
  ];
}

/** Fingerprint of a comparison's inputs — a result whose signature no longer
 * matches the current settings is stale and says so rather than lying. */
export function optimizeSignature(state, target) {
  return JSON.stringify(optimizeBodies(state, target));
}

/**
 * What actually differs between the two renders. `render_order` is the server's
 * one additive key for this feature (mrln/promptapi/library.py::_render_order):
 * the top-level slot ids in the reading order the render used. The sort itself
 * lives in mrln/promptlib/render.py and is never reimplemented here — an older
 * server that does not send the key leaves `known` false and the comparison
 * degrades to the two rendered texts.
 */
export function orderComparison(authored, optimized) {
  const before = Array.isArray(authored?.render_order) ? authored.render_order : null;
  const after = Array.isArray(optimized?.render_order) ? optimized.render_order : null;
  const key = (ids) => ids.join("\u0000");
  const sameSet = !!before && !!after && key([...before].sort()) === key([...after].sort());
  const rows = [];
  for (const [at, id] of (after ?? []).entries()) {
    rows.push({ id, at, was: before ? before.indexOf(id) : -1 });
  }
  const drawn = (body) => (body?.slots ?? []).map((s) => `${s.id}=${s.item ?? ""}`).join("\u0000");
  return {
    known: !!before && !!after,
    sameSet,
    moved: sameSet && key(before) !== key(after),
    rows,
    textChanged: (authored?.positive ?? "") !== (optimized?.positive ?? ""),
    negativeChanged: (authored?.negative ?? "") !== (optimized?.negative ?? ""),
    formatChanged: (authored?.format ?? "") !== (optimized?.format ?? ""),
    // a profile can also carry template overrides or a different text_length —
    // then the two sides are not a pure order difference and must say so
    drawChanged: drawn(authored) !== drawn(optimized),
    // slots that drew nothing carry no section, so block_order cannot rank
    // them: they keep their authored position in anything written back
    unranked: (optimized?.slots ?? []).filter((s) => !s.section_slug).map((s) => s.id),
  };
}

/**
 * The template `order` array (slot ids + "@variant") that reproduces a
 * render's reading order — as closely as a template file can store it.
 *
 * Two shapes have no verbatim representation on disk, and neither is a reason
 * to refuse the write (UAT: a refusal reads as a dead button, and the order
 * that IS storable is still the one the user asked for). They are approximated
 * and reported in `notes` instead:
 *
 *  - A variant slot keeps its BARE id in a render — "<variant>/<id>" is only
 *    its seed key (mrln/promptlib/resolve.py) — and the whole block rides ONE
 *    "@variant" token. A policy that interleaves variant slots with shared
 *    ones collapses to the block's FIRST position; the rest of the block keeps
 *    its place inside it.
 *  - A slot that drew nothing this run (muted, or a variant block that is off)
 *    gets no position from the render, so it keeps its authored one, anchored
 *    to the nearest authored neighbour that did render. It must still be
 *    LISTED: a partial 'order' silently drops the slot and every pick aimed at
 *    it (mrln/promptlib/resolve.py), which is the one outcome worse than a
 *    slightly approximate order.
 *
 * `variantIds` (the ids of every variant's slots) tells a variant slot from a
 * stale id; without it, any unknown id is read as a variant slot when the
 * template has a block for it.
 */
export function orderWriteBack(renderOrder, orderIds, variantIds = null) {
  const authored = (orderIds ?? []).filter(Boolean);
  const shared = new Set(authored.filter((id) => id !== "@variant"));
  const hasVariant = authored.includes("@variant");
  const known = variantIds ? new Set(variantIds) : null;
  const out = [];
  const carried = []; // variant slots the block cannot carry to a new position
  const unknown = [];
  let variantPlaced = false;
  for (const id of renderOrder ?? []) {
    if (shared.has(id)) {
      if (!out.includes(id)) out.push(id);
      continue;
    }
    if (!hasVariant || (known && !known.has(id))) {
      if (!unknown.includes(id)) unknown.push(id);
      continue;
    }
    if (out[out.length - 1] === "@variant") continue; // still inside the block
    if (variantPlaced) {
      if (!carried.includes(id)) carried.push(id);
      continue;
    }
    out.push("@variant");
    variantPlaced = true;
  }
  const missing = authored.filter((id) => !out.includes(id));
  for (const id of missing) {
    let anchor = -1;
    for (let i = authored.indexOf(id) - 1; i >= 0 && anchor < 0; i--) {
      anchor = out.indexOf(authored[i]);
    }
    out.splice(anchor + 1, 0, id);
  }
  const notes = [];
  if (carried.length) {
    notes.push(
      `${carried.join(", ")} sit inside the variant block, and a template stores that block as `
        + "one '@variant' entry — they keep their place inside it rather than moving alone."
    );
  }
  if (missing.length) {
    notes.push(
      `${missing.join(", ")} drew nothing in this comparison, so the order keeps `
        + (missing.length > 1 ? "their" : "its")
        + " authored position — listing every slot is what stops the file from dropping it."
    );
  }
  if (unknown.length) {
    notes.push(`${unknown.join(", ")} is not a slot of this template — skipped.`);
  }
  return { order: out, notes };
}

/**
 * Section options whose label matches every term, each term at a WORD START.
 *
 * Plain substring looked fine and was not: 'rain' matched terrain, grain and
 * training. Word-prefix keeps rainy and rainfall and drops the noise — the
 * same rule the server's /search uses, so the two modes agree about what a
 * match is.
 */
export function filterSectionOptions(options, query) {
  if (!String(query ?? "").trim()) return [...(options ?? [])];
  return (options ?? []).filter((option) =>
    wordPrefixMatch(String(option.label ?? option.value ?? ""), query)
  );
}

/** A deep hit's label: say WHERE it matched, or the slug alone is a dead end. */
export function deepLabel(row) {
  const slug = String(row?.slug ?? "");
  if ((row?.where ?? []).includes("name")) return slug;
  const samples = (row?.samples ?? []).slice(0, 2).join(", ");
  return samples ? `${slug}  — via ${samples}` : slug;
}

export function createCompose(hub) {
  const { ctx, state, composeTab } = hub;
  // Persistent elements owned by other modules. Destructured (not forwarded):
  // they are created once and never replaced, so the panel only has to build
  // those modules BEFORE this one.
  const { editorBox, loraBanner } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const allSlots = (...a) => hub.allSlots(...a);
  const appliedStateDiffers = (...a) => hub.appliedStateDiffers(...a);
  const applyKvToRows = (...a) => hub.applyKvToRows(...a);
  const askString = (...a) => hub.askString(...a);
  const auditionActive = (...a) => hub.auditionActive(...a);
  const buildSelectionLines = (...a) => hub.buildSelectionLines(...a);
  const confirmDiscardEdits = (...a) => hub.confirmDiscardEdits(...a);
  const confirmReplaceEditor = (...a) => hub.confirmReplaceEditor(...a);
  const ensurePool = (...a) => hub.ensurePool(...a);
  const exportBtn = (...a) => hub.exportBtn(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const newTemplate = (...a) => hub.newTemplate(...a);
  const openSectionEditor = (...a) => hub.openSectionEditor(...a);
  const overridesFor = (...a) => hub.overridesFor(...a);
  const rebuildForProfile = (...a) => hub.rebuildForProfile(...a);
  const revertProfileTweaks = (...a) => hub.revertProfileTweaks(...a);
  const saveTemplate = (...a) => hub.saveTemplate(...a);
  const schedulePreview = (...a) => hub.schedulePreview(...a);
  const selectTemplate = (...a) => hub.selectTemplate(...a);
  const setTargetProfile = (...a) => hub.setTargetProfile(...a);
  const viewTier = (...a) => hub.viewTier(...a);
  const slotAudible = (...a) => hub.slotAudible(...a);
  const switchTab = (...a) => hub.switchTab(...a);
  const variantBlockAudible = (...a) => hub.variantBlockAudible(...a);

  // Persistent element so markModified never re-renders (a re-render would
  // steal focus from the textarea the user is typing in).
  // The state word carries the amber; the sentence explaining it stays quiet.
  // As one grey line it read like every other note in the panel — which is
  // exactly what an unsaved-work warning must not do.
  const modifiedNote = el(
    "div",
    { class: "mrln-note mrln-modified", style: "display:none" },
    el("span", { class: "pc-flag" }, "● unsaved template changes"),
    " — Save writes them to your user library"
  );

  function markModified() {
    state.modified = true;
    modifiedNote.style.display = "";
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

  // ---- compose tab ---------------------------------------------------------

  const previewBox = el("div");
  // Persistent like previewBox/modifiedNote: an async comparison must survive
  // the re-renders that fire while it is in flight.
  const optimizeBox = el("div", { class: "mrln-optimize" });
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
          title: "Write template + settings to the node. One Prompt Template node "
            + "in the graph is targeted automatically; with several, select the "
            + "one you mean first. Unsaved template edits are saved to your user "
            + "library first — the node always renders the saved file.",
          onclick: (e) => busy(e.currentTarget, applyToNode),
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
      el(
        "button",
        {
          class: "mrln-btn",
          title: "Save this template (current picks become its defaults) to your user library",
          onclick: (e) => busy(e.currentTarget, () => saveTemplate(state.slug)),
        },
        "Save"
      ),
      overflowMenu(
        el("button", { class: "mrln-btn", onclick: () => loadFromNode() }, "Load"),
        el(
          "button",
          {
            class: "mrln-btn",
            title: "Fix every random slot to what the preview just drew",
            onclick: () => pinLastDraw(),
          },
          "Pin draw"
        ),
        el(
        "button",
        {
          class: "mrln-btn",
          onclick: (e) => {
            const button = e.currentTarget;
            return busy(button, async () => {
              if (button.mrlnArmed) {
                await armDestructive(button); // second click — run the armed overwrite
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
            });
          },
        },
        "Save as…"
        )
      )
    ),
    previewBox
  );

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
    handle.addEventListener("mousedown", () => {
      // dragend is the only other reset, and it never fires when the grab is
      // aborted — a card left draggable hijacks text selection inside its own
      // inputs. dragstart still fires: draggable is true at drag initiation.
      card.draggable = true;
      window.addEventListener("mouseup", () => (card.draggable = false), { once: true });
    });
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

  /**
   * A `⋯` button holding the actions that do not earn a permanent slot.
   * Same shape as the row menu, one primary per view being the point.
   */
  function overflowMenu(...actions) {
    const menu = el("div", { class: "pc-rowmenu pc-overflow", style: "display:none" }, ...actions);
    const button = el(
      "button",
      {
        class: "mrln-btn pc-overflow-btn",
        "aria-haspopup": "menu",
        "aria-expanded": "false",
        title: "More actions",
        onclick: (e) => {
          e.stopPropagation();
          const open = menu.style.display === "none";
          menu.style.display = open ? "" : "none";
          button.setAttribute("aria-expanded", open ? "true" : "false");
        },
      },
      "⋯"
    );
    return el("span", { class: "pc-overflow-wrap" }, button, menu);
  }

  // ---- the design-pass row grid -------------------------------------------
  // One line per draw: state · name · value · weight · seed · menu, shared by
  // top-level slots and nested child draws so the two read as one table.
  //
  // WT is the DRAWN ITEM's draw weight — per-item, exactly as the library
  // stores it — not the slot's prompt emphasis. The handoff conflates them;
  // they are different numbers with different consequences, and emphasis keeps
  // its own control in the row's editor disclosure. Read-only here: editing it
  // would write to a section file from the Compose tab, which is a library
  // mutation this pass is not allowed to introduce.

  /** random (unpinned) · held (random but seeded) · fixed (explicit item). */
  function rowMode(row) {
    if (!row.random) return "fixed";
    return row.seed ? "held" : "random";
  }

  const STATE_GLYPH = { random: "◆", held: "🔒", fixed: "🔒" };
  const STATE_TITLE = {
    random: "Random — click to hold this draw (freezes it on the seed it just used)",
    held: "Held on a pinned seed — click to let it draw again",
    fixed: "Fixed to one item — pick 'random' in the value list to unfix",
  };

  // ---- row selection ------------------------------------------------------
  // A selected row is the keyboard's subject: E edits, ↑/↓ move it, Del takes
  // it out. The selection is an id in state, not a DOM flag, so it survives the
  // re-render every one of those actions triggers.

  function selectRow(id, node) {
    state.selectedRow = id;
    for (const other of composeTab.querySelectorAll('.pc-row[data-selected="true"]')) {
      if (other !== node) other.removeAttribute("data-selected");
    }
    node?.setAttribute("data-selected", "true");
  }

  /**
   * Wrap a row action so finishing it with the POINTER drops the selection.
   *
   * Only the pointer: the keyboard shortcuts call the same actions and must
   * keep the row selected, or ↑↑↑ would move a row once and then lose it.
   * The amber outline says "the keyboard is aimed here" — once a mouse has
   * finished the job, the aim is spent.
   */
  function finishing(action) {
    return (...args) => {
      const result = action?.(...args);
      clearSelection();
      return result;
    };
  }

  /** Let go. A selection you cannot drop is a mode, not a selection. */
  function clearSelection() {
    if (!state.selectedRow) return;
    state.selectedRow = null;
    for (const row of composeTab.querySelectorAll('.pc-row[data-selected="true"]')) {
      row.removeAttribute("data-selected");
      if (document.activeElement === row) row.blur();
    }
  }

  // Clicking anywhere in the tab that is not a row drops the selection — the
  // same way clicking empty canvas deselects a node.
  composeTab.addEventListener("mousedown", (e) => {
    if (!e.target.closest?.(".pc-row")) clearSelection();
  });

  /** Put focus back on the selected row after an action rebuilt the table. */
  function focusSelectedRow() {
    composeTab.querySelector('.pc-row[data-selected="true"]')?.focus({ preventScroll: true });
  }

  /**
   * Wire a row as selectable. `actions` supplies only what the row can do —
   * the variant header has no editor and cannot be removed, so it passes just
   * the move, and the keys it does not implement stay unbound rather than
   * silently doing nothing.
   */
  function selectableRow(node, id, actions) {
    node.tabIndex = 0;
    if (state.selectedRow === id) node.dataset.selected = "true";
    // mousedown, not click: a click on a control inside the row focuses THAT
    // control, and the row still has to become the selection.
    node.addEventListener("mousedown", () => selectRow(id, node));
    node.addEventListener("focus", () => selectRow(id, node));
    node.addEventListener("keydown", (e) => {
      // Only when the ROW itself has focus. Inside a select or an input the
      // arrows and letters belong to that control, and stealing them would
      // make the table unusable with a keyboard.
      if (e.target !== node) return;
      if (e.key === "e" || e.key === "E") {
        if (!actions.edit) return;
        actions.edit();
      } else if (e.key === "ArrowUp") {
        actions.move?.(-1);
      } else if (e.key === "ArrowDown") {
        actions.move?.(1);
      } else if (e.key === "Delete") {
        if (!actions.remove) return;
        actions.remove();
      } else if (e.key === "Enter" || e.key === "Escape") {
        // Enter confirms, Escape abandons — with actions applied as you make
        // them, both mean the same thing here: the row is no longer the
        // keyboard's subject.
        clearSelection();
        e.preventDefault();
        return; // nothing left to focus
      } else return;
      e.preventDefault();
      focusSelectedRow();
    });
    return node;
  }

  /**
   * The DRAWN VALUE cell: our own popover over the row's <select>.
   *
   * The select is NOT replaced — it stays in the DOM, hidden, as the source of
   * truth. Committing a pick writes select.value and dispatches `change`, so
   * every handler that already hangs off it runs untouched; this only owns how
   * the list looks and how the keyboard walks it. That is also why the native
   * control is still there for anything (tests, ComfyUI, a screen reader) that
   * reads the row's value out of the DOM.
   */
  /**
   * A slot's random-pool subset, as the picker's `subset` contract.
   *
   * It lives on the TEMPLATE (slot.include), not on the row: the node reads
   * the template, so a pool narrowed here is what renders headless too. Empty
   * means the whole section — which is what every template written before this
   * said and still says.
   */
  function subsetFor(slot, pool) {
    if (!slot) return null;
    const names = () => slot.include ?? [];
    const write = (list) => {
      const clean = [...new Set(list)];
      if (clean.length) slot.include = clean;
      else delete slot.include;
      markModified();
      schedulePreview();
    };
    return {
      enabled: () => (slot.include ?? []).length > 0,
      setEnabled: (on) => {
        // Turning it on with nothing ticked would be a pool of zero items, so
        // it starts as everything and the user takes items OUT — the same
        // direction the 'all off' button offers.
        write(on ? (pool ?? []).map((item) => item.name) : []);
      },
      has: (name) => names().includes(name),
      toggle: (name) => {
        const list = names();
        write(list.includes(name) ? list.filter((n) => n !== name) : [...list, name]);
      },
      setAll: (list) => write(list),
    };
  }

  function valueCell(select, pool, sectionRef, slot, extra = {}) {
    select.classList.add("pc-field-native");
    const trigger = el("button", {
      class: `pc-field pc-trigger${extra.triggerClass ? ` ${extra.triggerClass}` : ""}`,
      "aria-haspopup": "listbox",
      "aria-expanded": "false",
      onclick: (e) => {
        e.stopPropagation();
        if (pickerIsOpen()) {
          closePicker();
          return;
        }
        trigger.setAttribute("aria-expanded", "true");
        const node = openPicker({
          select,
          pool,
          anchor: trigger,
          sectionRef,
          subset: subsetFor(slot, pool),
          itemsLabel: extra.itemsLabel,
          sideLabel: extra.sideLabel,
          sideOf: extra.sideOf,
          minWidth: extra.minWidth,
          onEditSection: sectionRef
            ? async () => {
                if (!confirmReplaceEditor()) return;
                switchTab("library");
                await openSectionEditor(sectionRef);
                editorBox.scrollIntoView({ block: "nearest" });
              }
            : null,
        });
        // the popover owns its own teardown; the trigger only mirrors it
        new MutationObserver((changes, observer) => {
          if (node.isConnected) return;
          trigger.setAttribute("aria-expanded", "false");
          observer.disconnect();
        }).observe(document.body, { childList: true });
      },
    });
    const paint = () => {
      const option = select.selectedOptions[0];
      trigger.textContent = option ? option.textContent : select.value;
      trigger.title = option?.title || "";
    };
    paint();
    // A committed pick is a finished row action — nothing dispatches `change`
    // on these but the picker and a person.
    select.addEventListener("change", () => {
      paint();
      clearSelection();
    });
    return el("span", { class: "pc-cell-value" }, select, trigger);
  }

  /** Repaint the glyph in place, so typing a seed does not need a re-render. */
  function paintState(button, row) {
    const mode = rowMode(row);
    button.dataset.mode = mode;
    button.title = STATE_TITLE[mode];
    button.textContent = STATE_GLYPH[mode];
  }

  function stateCell(row, resolved, onChange) {
    const button = el(
      "button",
      {
        class: "pc-state",
        // Read the mode at CLICK time, not at build time: the seed field can
        // change it under us (typing a seed turns random into held), and a
        // captured mode would then run the wrong branch.
        onclick: () => {
          const mode = rowMode(row);
          if (mode === "held") row.seed = "";
          else if (mode === "random") {
            const used = resolved?.seed_used;
            if (used === undefined || used === null) {
              ctx.toast(
                "warn",
                "Nothing drawn yet",
                "Holding freezes the seed the live preview last used — wait for it."
              );
              return;
            }
            row.seed = String(used);
          } else return; // fixed: the value list owns that state
          row.touched = true;
          onChange();
        },
      }
    );
    paintState(button, row);
    return button;
  }

  function weightCell(pool, row, resolved) {
    const name = row.random ? resolved?.item : row.item;
    const item = (pool ?? []).find((p) => p.name === name);
    const weight = Number(item?.weight ?? 1);
    const shown = Number.isFinite(weight) ? weight : 1;
    return el(
      "span",
      {
        class: "pc-cell-wt",
        "data-weighted": shown !== 1 ? "true" : "false",
        // read-only: there is nothing here to finish, so a click on it should
        // not leave the row sitting selected either
        onclick: () => clearSelection(),
        title:
          `Draw weight of '${name ?? "—"}' — how often this item comes up `
          + "relative to its siblings. Lives on the item in the section file; "
          + "edit it in the Library.",
      },
      String(shown)
    );
  }

  /**
   * The seed cell: one click pins this draw, a double-click types a seed.
   *
   * The interaction is right; it was unreliable for two structural reasons,
   * neither of them timing:
   *  - the editor mounted INSIDE the cell whose own click handler pins, so the
   *    click that put the caret in the field also armed the pin — and the seed
   *    snapped back to auto under you;
   *  - a live preview finishing calls renderNested(), which replaces nested
   *    rows wholesale, so a nested row's editor vanished mid-typing.
   * So the editing flag lives in state (the same shape as labelEdit), the cell
   * comes back as an editor after any re-render and refocuses itself, and every
   * pointer event inside the field stops before the pin handler sees it.
   */
  function seedCell(id, row, resolved, input, onChange) {
    const mode = rowMode(row);
    if (state.seedEdit.has(id)) {
      input.classList.remove("mrln-narrow");
      input.classList.add("pc-seed-field");
      input.style.display = "";
      input.placeholder = "auto";
      input.title = "Seed for this draw — blank follows the master seed";
      for (const type of ["mousedown", "click", "dblclick"]) {
        input.addEventListener(type, (e) => e.stopPropagation());
      }
      const close = () => {
        state.seedEdit.delete(id);
        onChange();
        clearSelection(); // the seed is typed and committed: action finished
      };
      input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== "Escape") return;
        e.preventDefault();
        close();
      });
      // A re-render detaches the field and fires blur — closing on THAT would
      // cancel editing every time the preview came back, which is the bug one
      // level up. Only a blur that leaves the field in the document is a real
      // one; the rebuilt cell refocuses instead.
      input.addEventListener("blur", () => {
        setTimeout(() => {
          if (input.isConnected) close();
        }, 0);
      });
      requestAnimationFrame(() => {
        if (!input.isConnected || document.activeElement === input) return;
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
      return el(
        "span",
        { class: "pc-cell-seed", "data-mode": mode, "data-editing": "true" },
        input
      );
    }
    const cell = el(
      "span",
      {
        class: "pc-cell-seed",
        "data-mode": mode,
        tabindex: mode === "fixed" ? null : "0",
        title:
          mode === "fixed"
            ? "This slot is fixed to an item, so no seed is involved"
            : "Click to pin this draw's seed · double-click to type one",
      },
      String(mode === "fixed" ? "fixed" : mode === "held" ? row.seed : "auto")
    );
    // Click pins, double-click types. A click always precedes a double-click,
    // so the single-click action waits one interval and cancels if the second
    // click arrives — otherwise every attempt to type would first pin.
    let timer = null;
    cell.addEventListener("click", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        // live, not captured: the value select can retire the seed after this
        // cell was built
        const now = rowMode(row);
        const used = resolved?.seed_used;
        if (now === "held") {
          row.seed = "";
          row.touched = true;
          onChange();
        } else if (now === "random" && used !== undefined && used !== null) {
          row.seed = String(used);
          row.touched = true;
          onChange();
        }
        // Whatever the pin decided — including deciding there was nothing to
        // pin yet — the pointer is finished with this row.
        clearSelection();
      }, 220);
    });
    cell.addEventListener("dblclick", () => {
      clearTimeout(timer);
      if (rowMode(row) === "fixed") return;
      state.seedEdit.add(id);
      onChange();
    });
    cell.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      clearTimeout(timer);
      if (rowMode(row) === "fixed") return;
      state.seedEdit.add(id);
      onChange();
    });
    return cell;
  }

  /**
   * Repaint one row's mode-dependent chrome in place. Called by the seed field
   * (typing must not re-render — focus dies) and by the value select (picking a
   * fixed item retires the seed), so the glyph, the row tint and the field's
   * enabled state never disagree with the row they describe.
   */
  function paintRowMode(row, stateBtn, seedNode) {
    const mode = rowMode(row);
    if (stateBtn) paintState(stateBtn, row);
    if (seedNode && seedNode.dataset.editing !== "true") {
      seedNode.dataset.mode = mode;
      seedNode.textContent = mode === "fixed" ? "fixed" : mode === "held" ? row.seed : "auto";
    }
    (stateBtn ?? seedNode)?.closest(".pc-row")?.setAttribute("data-state", mode);
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
          // finishing(): toggling an audition is a COMPLETE action, so the
          // pointer should not leave the row selected behind it — the same
          // rule the dropdown, the weight, the seed and the ✎↑↓✕ icons follow.
          onclick: finishing(() => toggleAudition(state.muted, id)),
        },
        "M"
      ),
      el(
        "button",
        {
          class: `mrln-btn mrln-mini${state.soloed.has(id) ? " mrln-s-on" : ""}`,
          title: "Solo — only soloed sections render (others become 'off'; solo overrides mute)",
          onclick: finishing(() => toggleAudition(state.soloed, id)),
        },
        "S"
      )
    );
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

  // ONE request per ref, ONE re-render per frame.
  //
  // This is where the panel froze. childRow used to do
  //     if (!pool) ensurePool(child.ref).then(() => renderNested());
  // which looks harmless and is quadratic-then-worse: a template with N
  // nested children whose pools are not loaded schedules N callbacks; EVERY
  // resolution re-renders; every re-render walks all N children again and
  // attaches a fresh callback to each still-pending request (ensurePool hands
  // back the same in-flight promise, so they stack). N renders x N children
  // compounds until the tab stops painting — which is what "click on a parent
  // of a nested object" did, because changing a parent's draw gives its
  // children new refs that are all unloaded at once.
  //
  // The two rules that make it linear: never ask for the same ref twice while
  // it is in flight, and collapse any number of arrivals into a single render
  // on the next frame. A pool that never arrives (declined, failed, backed
  // off) renders nothing, so nothing re-schedules.
  const nestedAsked = new Set();
  let nestedFrame = null;

  function requestNestedPool(ref) {
    if (nestedAsked.has(ref)) return; // already in flight — its arrival renders
    nestedAsked.add(ref);
    ensurePool(ref)
      .then(() => {
        nestedAsked.delete(ref);
        if (!state.detail?.pools?.[ref]) return; // declined or failed: end here
        if (nestedFrame !== null) return; // a render is already queued
        nestedFrame = requestAnimationFrame(() => {
          nestedFrame = null;
          renderNested();
        });
      })
      .catch(() => nestedAsked.delete(ref));
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
    // `host`, NOT `mount`: this used to be a loop variable called `mount`, and
    // naming it that again shadows dom.js's mount() — `mount(mount, …)` then
    // calls a DOM element as a function and every nested draw disappears from
    // the panel. That is exactly what happened, and it reached UAT.
    for (const host of composeTab.querySelectorAll("[data-mrln-nested]")) {
      const resolved = (state.lastPreview?.slots ?? []).find(
        (s) => s.id === host.dataset.mrlnNested
      );
      if (resolved && (resolved.children ?? []).length) {
        mount(
          host,
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
        host.style.display = "";
      } else {
        mount(host);
        host.style.display = "none";
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
    if (!pool) requestNestedPool(child.ref);

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
        paintRowMode(row, stateBtn, seedNode);
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
      oninput: (e) => {
        row.touched = true;
        row.seed = e.target.value.replace(/\D/g, "");
        schedulePreview();
      },
    });

    const redraw = () => renderNested();
    itemSelect.classList.add("pc-field");
    const stateBtn = stateCell(row, child, redraw);
    const seedNode = seedCell(child.id, row, child, seedInput, redraw);
    return el(
      "div",
      {
        class: "mrln-slot mrln-nest-row pc-row",
        "data-level": "nested",
        "data-state": rowMode(row),
      },
      stateBtn,
      el(
        "span",
        { class: "pc-cell-name", title: `${child.id} → ${child.ref}` },
        child.id.split(".").pop(),
        child.omitted ? el("span", { class: "mrln-chip" }, "muted/empty") : null
      ),
      valueCell(itemSelect, pool, child.ref),
      weightCell(pool, row, child),
      seedNode,
      // a child draw has nothing to reorder or remove — the column stays
      // reserved so nested rows keep the parent table's alignment
      el("span", { class: "pc-cell-actions" })
    );
  }

  function renderComposeTab() {
    if (!state.rawData) {
      mount(composeTab, 
        el(
          "div",
          { class: "mrln-note" },
          "No templates in the library yet — start a new composition:"
        ),
        el(
          "div",
          { class: "mrln-actions" },
          el(
            "button",
            {
              class: "mrln-btn mrln-primary",
              onclick: (e) => busy(e.currentTarget, newTemplate),
            },
            "New template…"
          )
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
      el(
        "label",
        { class: "mrln-field" },
        el("span", { class: "mrln-field-name" }, "Template"),
        // The control row rides the TABLE's grid: the picker's trigger spans
        // the section columns, and everything after it starts where DRAWN
        // VALUE starts — slug first, badges hard right.
        el(
          "div",
          { class: "pc-tplbar" },
          // the same picker the value fields use: a filter over 70+ templates,
          // and every row showing the slug it will write to the node
          valueCell(templateSelect, null, null, null, {
            triggerClass: "pc-tpl-trigger",
            itemsLabel: "Templates",
            sideLabel: "slug",
            sideOf: (entry) => entry.value,
            // a row here carries a name AND a slug; no trigger is that wide
            minWidth: 460,
          }),
          el(
            "div",
            { class: "pc-tpl-meta" },
            // the slug is what the NODE's template widget holds — the one thing
            // on this row you cannot look up anywhere else on screen
            el(
              "span",
              {
                class: "pc-tpl-slug",
                title: "The template's slug — the identifier the Prompt Template node's "
                  + "template widget holds (set that node's 'template_names' to 'label' "
                  + "if you would rather it held the name above).",
              },
              state.slug
            ),
            el(
              "span",
              { class: "pc-tpl-actions" },
              tierToggle(),
              el(
                "button",
                {
                  class: "mrln-btn mrln-mini mrln-new-tpl",
                  title: "Start a NEW empty template (net-new composition)",
                  onclick: (e) => busy(e.currentTarget, newTemplate),
                },
                "＋"
              ),
              exportBtn("template", state.slug)
            )
          )
        )
      ),
    ];
    modifiedNote.style.display = state.modified ? "" : "none";
    parts.push(modifiedNote);
    if (state.tierView === "factory") {
      // Two things behave differently here and both are surprising if unsaid.
      parts.push(
        el(
          "div",
          { class: "mrln-note pc-tier-note" },
          el("span", { class: "pc-flag" }, "● reading the factory version"),
          " — your file still wins every render. Apply to node writes "
            + `'factory:${state.slug}' so the node renders THIS one; Save would `
            + "overwrite your version with it."
        )
      );
    }
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
      parts.push(el("div", { class: "mrln-note pc-desc" }, state.rawData.description));
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
    if (profileSelect.value !== (state.profile ?? "standard")) {
      // A profile this install does not offer (loaded from a node built
      // elsewhere). Show it instead of silently displaying 'standard' while
      // preview and Apply keep sending the foreign name — same treatment the
      // node's backend combo gives an unknown value.
      profileSelect.append(
        el(
          "option",
          { value: state.profile, title: "Not installed here — the render falls back" },
          `${state.profile} (not installed)`
        )
      );
      profileSelect.value = state.profile;
    }
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
        // dice FIRST: the number then ends on the same right edge as every
        // other value in this grid. DOM order, not CSS `order` — a visual
        // order that contradicts tab order is a trap for keyboard users.
        //
        // And a DIV, not the usual label: a <button> is a labelable element, so
        // with the dice first the whole row became the dice's hit area and
        // clicking anywhere on it rerolled the master seed.
        el(
          "div",
          { class: "mrln-field" },
          el("span", { class: "mrln-field-name" }, "Master seed"),
          el("div", { class: "mrln-inline" }, reroll, seedInput)
        )
      ),
      metaPromptBlock(),
      // The header the row grid aligns to. Same six-column template, so a
      // column that moves moves in both — and it is what turns a stack of
      // rows into a table you can read down.
      el(
        "div",
        { class: "pc-thead", role: "presentation" },
        el("span", {}, ""),
        el("span", {}, "Section"),
        el("span", {}, "Drawn value"),
        el("span", { title: "Draw weight of the drawn item" }, "Wt"),
        el("span", {}, "Seed"),
        el("span", {}, "Actions")
      ),
      el("div", { class: "mrln-slot-list" }, orderedRows()),
      addSectionRow()
    );

    const variables = state.rawData.variables ?? [];
    const triggerVar = variables.find((v) => v.name === "trigger");
    parts.push(
      titled(
        "Trigger",
        el("input", {
          type: "text",
          value: state.trigger,
          placeholder: triggerVar?.default ?? "",
          oninput: (e) => {
            state.trigger = e.target.value;
            schedulePreview();
          },
        }),
        "The template's {trigger} word — replaced everywhere: template text, "
          + "lead-ins and item texts."
      )
    );
    const extraVars = variables.filter((v) => v.name !== "trigger");
    parts.push(
      titled(
        "Variables",
        autoArea(
          {
            placeholder: extraVars.map((v) => `${v.name}=${v.default ?? ""}`).join("\n"),
            oninput: (e) => {
              state.variables = e.target.value;
              schedulePreview();
            },
          },
          state.variables
        ),
        `One name=value per line. This template declares: ${
          extraVars.map((v) => v.name).join(", ") || "none"
        }.`
      )
    );

    parts.push(optimizeBox, footer);

    mount(composeTab, ...parts);
    renderPreview(state.lastPreview, null);
    renderOptimize(); // persistent box — refill it for the fresh mount
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
    // The summary has to say what is inside, or a closed fold hides work
    // silently. Counts the five fields the fold owns.
    const textSet = [
      state.rawData.label,
      state.rawData.prefix,
      state.rawData.suffix,
      state.rawData.negative,
      (state.rawData.type ?? []).length,
    ].filter(Boolean).length;
    return el(
      "details",
      { class: "mrln-fold" },
      el(
        "summary",
        {},
        "Template text",
        el(
          "span",
          { class: "pc-summary-note" },
          `label, prefix, suffix, negative, type · ${textSet} of 5 set`
        )
      ),
      // One word each. The label column is narrow by design, and a parenthetical
      // there only ever renders as 'Label (displa…' — the explanation belongs in
      // the tooltip, where it is not competing for 70px.
      titled(
        "Label",
        labelInput,
        "Display name — what the template picker and this tab show. The slug "
          + "stays the file path."
      ),
      titled(
        "Prefix",
        braceAssist(prefixArea, wrapRefOptions),
        "Text rendered before the first section. {slot-id} weaves a slot's drawn "
          + "text inline."
      ),
      titled(
        "Suffix",
        braceAssist(suffixArea, wrapRefOptions),
        "Text rendered after the last section. {slot-id} weaves inline here too."
      ),
      titled("Negative", negativeInput, "Template-level negative terms."),
      titled(
        "Type",
        typeInput,
        "Classifiers: filter the section picker and the random draw pools to "
          + "matching + universal sections. Explicit picks are never restricted."
      )
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
    variantSelect.classList.add("pc-field");
    const card = el(
      "div",
      { class: `mrln-slot mrln-variant-head${blockDimmed ? " mrln-muted" : ""}` },
      // Same grid as every other row — this is the design's group header, and
      // a three-line card here was the last thing breaking the table's rhythm.
      // Selectable for ↑/↓ only: the block has no label of its own to edit and
      // is not removable from here.
      selectableRow(
        el(
          "div",
          {
            class: "pc-row",
            "data-level": "top",
            "data-state": "random",
            title: "Selected: ↑ ↓ move the whole variant block",
          },
          el("span", { class: "pc-dot", title: "This block draws a variant" }),
          el(
            "span",
            { class: "pc-cell-name" },
            handle,
            msButtons("@variant"),
            // "Variant", not "Variant block": the @variant chip beside it says
            // the rest, and the two together were the widest name in the table
            el("span", { class: "pc-cell-label" }, "Variant"),
            el("span", { class: "pc-cell-chips" }, el("span", { class: "mrln-chip" }, "@variant"))
          ),
          // the same picker every other row uses: a variant list is a value
          // list like any other, and two dropdown vocabularies in one table is
          // one too many
          valueCell(variantSelect, null, null),
          el("span", { class: "pc-cell-wt" }, ""),
          el("span", { class: "pc-cell-seed" }, ""),
          el(
            "span",
            { class: "pc-cell-actions" },
            el(
              "span",
              { class: "mrln-rowbtns" },
              smallBtn(
                "Move variant block up (↑)",
                "↑",
                () => moveOrder(orderIndex, -1),
                orderIndex === 0
              ),
              smallBtn(
                "Move variant block down (↓)",
                "↓",
                () => moveOrder(orderIndex, 1),
                orderIndex === state.orderIds.length - 1
              )
            )
          )
        ),
        "@variant",
        { move: (delta) => moveOrder(orderIndex, delta) }
      )
    );
    attachDrag(card, handle, "order", orderIndex, (from, to) =>
      moveInArray(state.orderIds, from, to)
    );
    return card;
  }

  /**
   * The tier pill. When the slug exists in BOTH tiers it is a button that
   * switches which one the panel is showing — a user file shadows the factory
   * file everywhere, and this is the only way to read what you are shadowing
   * before deciding your version is the better one.
   */
  function tierToggle() {
    const tiers = state.detail?.tiers ?? [];
    const showing = state.detail?.viewing ?? state.detail?.tier;
    if (tiers.length < 2) return tierChip(state.detail?.tier);
    const other = showing === "factory" ? "user" : "factory";
    const chip = el(
      "button",
      {
        class: `mrln-chip mrln-chip-btn mrln-${showing}`,
        title:
          `Showing the ${showing} version — click to read the ${other} one. `
          + "Your file wins every render; the factory version renders only if you "
          + "Apply while looking at it.",
        onclick: (e) => busy(e.currentTarget, () => viewTier(other)),
      },
      showing
    );
    return chip;
  }

  /** Every slot id in the template — a placeholder has to be unique. */
  function allSlotIds() {
    const ids = new Set();
    for (const slot of state.rawData?.slots ?? []) ids.add(slot.id);
    for (const variant of state.rawData?.variants ?? []) {
      for (const slot of variant.slots ?? []) ids.add(slot.id);
    }
    return ids;
  }

  /**
   * Rename a slot's id — the {placeholder} the template weaves inline.
   *
   * The id is a KEY, not a label: it names the slot in the prefix/suffix text,
   * in the node's selection lines, in the audition sets and in the row map. A
   * rename that moved only `slot.id` would leave a {token} in the prose
   * pointing at a slot that no longer exists — the drawn text would silently
   * stop appearing. So everything that keys off it moves in the same step.
   */
  function renameSlotId(slot, raw, isVariantSlot, input) {
    const next = String(raw ?? "")
      .trim()
      .replace(/[^A-Za-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    if (!next || next === slot.id) {
      input.value = slot.id; // nothing to do, and never leave a half-typed id on screen
      return;
    }
    if (allSlotIds().has(next)) {
      input.value = slot.id;
      ctx.toast(
        "error",
        "That id is taken",
        `Another slot in this template is already called '${next}'. Ids are the `
          + "placeholders the prefix/suffix weave in, so they have to be unique."
      );
      return;
    }
    const from = slot.id;
    if (state.rawData) {
      state.rawData.prefix = renameToken(state.rawData.prefix ?? "", from, next);
      state.rawData.suffix = renameToken(state.rawData.suffix ?? "", from, next);
    }
    const move = (set) => {
      if (set.has(from)) {
        set.delete(from);
        set.add(next);
      }
    };
    if (state.rows.has(from)) {
      state.rows.set(next, state.rows.get(from));
      state.rows.delete(from);
    }
    move(state.muted);
    move(state.soloed);
    move(state.labelEdit);
    move(state.seedEdit);
    if (!isVariantSlot) {
      state.orderIds = state.orderIds.map((id) => (id === from ? next : id));
    }
    if (state.selectedRow === from) state.selectedRow = next;
    slot.id = next;
    markModified();
    renderComposeTab();
    schedulePreview();
  }

  /** Point a slot at a different section — the Remap flow, off the row. */
  async function remapSlot(slot, ref) {
    if (!ref || ref === slot.ref) return;
    slot.ref = ref;
    delete slot.default; // the old default named an item of the old section
    state.rows.set(slot.id, parseToken("random"));
    await ensurePool(ref, { force: true });
    markModified();
    renderComposeTab();
    schedulePreview();
  }

  function removeSlot(container, index, id, isVariantSlot) {
    container.splice(index, 1);
    if (!isVariantSlot) state.orderIds = state.orderIds.filter((oid) => oid !== id);
    state.rows.delete(id);
    state.labelEdit.delete(id);
    state.seedEdit.delete(id);
    // a selection pointing at a row that no longer exists would silently take
    // the next Del with it
    if (state.selectedRow === id) state.selectedRow = null;
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
        // this card exists to un-stick a broken template — an inert button
        // with no explanation is the worst possible zero state here
        disabled: remapSelect.options.length ? null : "",
        onclick: (e) =>
          busy(e.currentTarget, async () => {
            const ref = remapSelect.value;
            if (!ref) return;
            slot.ref = ref;
            delete slot.default; // the old default named an item of the dead section
            state.rows.set(slot.id, parseToken("random"));
            await ensurePool(ref, { force: true }); // user-initiated: retry a failed pool now
            markModified();
            renderComposeTab();
            schedulePreview();
          }),
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
      el("div", { class: "mrln-inline" }, remapSelect, remapButton),
      remapSelect.options.length ? null : emptyLibraryNote()
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
        paintRowMode(row, stateBtn, seedNode);
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
      // The row names an item this pool no longer has (renamed/removed in the
      // library). Repair the ROW, not just the display — otherwise the select
      // shows 'random' while rowToken keeps emitting the dead name to the
      // preview and to Apply.
      const fixed = parseToken(singleOnly ? pool[0].name : "random");
      Object.assign(row, fixed);
      state.rows.set(slot.id, row);
      itemSelect.value = singleOnly ? pool[0].name : "random";
    }
    if (singleOnly) itemSelect.title = "Only item in this section — drawn every time";

    const seedInput = el("input", {
      class: "mrln-narrow",
      type: "text",
      inputmode: "numeric",
      placeholder: "seed",
      title: "Optional per-slot seed — decouples this slot from the master seed",
      value: row.seed,
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
      // rows are {random, seed, item} — reading '.value' here always came back
      // undefined, so the pill never narrowed to the picked item's base
      const drawn = pool.find((p) => p.lora && p.name === state.rows.get(slot.id)?.item);
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

    // One implementation per action, shared by the buttons and the keyboard —
    // a shortcut that drifts from the button it mirrors is worse than no
    // shortcut.
    const toggleLabelEdit = () => {
      if (state.labelEdit.has(slot.id)) state.labelEdit.delete(slot.id);
      else state.labelEdit.add(slot.id);
      renderComposeTab();
    };
    const moveRow = (delta) => {
      if (!isVariantSlot) {
        moveOrder(orderIndex, delta);
        return;
      }
      const target = index + delta;
      if (target < 0 || target >= container.length) return;
      [container[target], container[index]] = [container[index], container[target]];
      markModified();
      renderComposeTab();
      schedulePreview();
    };
    const firstRow = isVariantSlot ? index === 0 : orderIndex === 0;
    const lastRow = isVariantSlot
      ? index === container.length - 1
      : orderIndex === state.orderIds.length - 1;

    const buttons = el(
      "span",
      { class: "mrln-rowbtns" },
      smallBtn(
        "Edit the lead-in text rendered before this section (E)",
        "✎",
        finishing(toggleLabelEdit)
      ),
      smallBtn(
        isVariantSlot ? "Move up within the variant (↑)" : "Move up (↑)",
        "↑",
        finishing(() => moveRow(-1)),
        firstRow
      ),
      smallBtn(
        isVariantSlot ? "Move down within the variant (↓)" : "Move down (↓)",
        "↓",
        finishing(() => moveRow(1)),
        lastRow
      ),
      smallBtn(
        "Remove this section from the template (Del)",
        "✕",
        finishing(() => removeSlot(container, index, slot.id, isVariantSlot))
      )
    );

    const labelText = slot.label && slot.label.length <= 60 ? slot.label : slot.id;
    const handle = dragHandle();
    const dimmed = auditionActive() && !slotAudible(slot.id, isVariantSlot);
    const resolved = (state.lastPreview?.slots ?? []).find((s) => s.id === slot.id);
    const redraw = () => {
      renderComposeTab();
      schedulePreview();
    };
    itemSelect.classList.add("pc-field");
    const stateBtn = stateCell(row, resolved, redraw);
    const seedNode = seedCell(slot.id, row, resolved, seedInput, redraw);
    const parts = [
      selectableRow(
        el(
          "div",
          {
            class: "pc-row",
            "data-level": isVariantSlot ? "nested" : "top",
            "data-state": rowMode(row),
            title: "Selected: E edits the label, ↑ ↓ move it, Del removes it",
          },
          stateBtn,
        el(
          "span",
          { class: "pc-cell-name", title: `${slot.id} → ${slot.ref}` },
          handle,
          // M/S stay ON the row, not in the menu: this is a DAW-style audition
          // and muting has to be one click from the thing being muted. M IS
          // the design's 'off' state — same meaning, so there is no third
          // vocabulary, and the row dims exactly as 'off' would.
          msButtons(slot.id),
          el("span", { class: "pc-cell-label" }, labelText),
          el("span", { class: "pc-cell-chips" }, chips)
        ),
          valueCell(itemSelect, pool, slot.ref, slot),
          weightCell(pool, row, resolved),
          seedNode,
          // ✎ ↑ ↓ ✕ ride the row under an ACTIONS header instead of hiding in
          // a ⋯ menu: they are the four things a composer does most, and a
          // click to reveal a click is a click too many.
          el("span", { class: "pc-cell-actions" }, buttons)
        ),
        slot.id,
        {
          edit: toggleLabelEdit,
          move: moveRow,
          remove: () => removeSlot(container, index, slot.id, isVariantSlot),
        }
      ),
    ];
    if (state.labelEdit.has(slot.id)) {
      // One block, and every field says what it is. The lead-in used to open as
      // a bare textarea holding the row's name — nothing on screen said THAT
      // text was what you were editing.
      const idInput = el("input", {
        type: "text",
        value: slot.id,
        // `change`, not `input`: renaming on every keystroke would rewrite the
        // prefix/suffix tokens to every half-typed prefix on the way to the
        // name actually wanted
        onchange: (e) => renameSlotId(slot, e.target.value, isVariantSlot, e.target),
      });
      const refPicker = sectionPicker({
        typeOf: state.rawData?.type,
        initial: slot.ref,
        compact: true,
      });
      refPicker.select.addEventListener("change", () =>
        busy(refPicker.select, () => remapSlot(slot, refPicker.select.value))
      );
      parts.push(
        el(
          "div",
          { class: "pc-slot-edit" },
          titled(
            "Id",
            idInput,
            "The {placeholder} this slot answers to: in the prefix/suffix text, "
              + "in the node's selection lines and in nested references. Renaming it "
              + "here rewrites the prefix/suffix references with it."
          ),
          titled(
            "Section",
            refPicker.node,
            "Which section this slot draws from. Changing it clears the slot's "
              + "default — the old one named an item of the old section."
          ),
          titled(
            "Label",
            autoArea(
              {
                placeholder: "empty = the section's own label",
                oninput: (e) => {
                  slot.label = e.target.value;
                  markModified();
                  schedulePreview();
                },
              },
              slot.label ?? ""
            ),
            "The name shown on this row AND the lead-in rendered before the "
              + "section's drawn text. {trigger} works here; empty falls back to "
              + "the section's own label."
          ),
          titled(
            "Emphasis",
            el("input", {
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
            }),
            "Wraps the drawn text as (text:weight) in the prompt — this "
              + "template's value, independent of any weights inside item texts"
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

  function emptyLibraryNote() {
    // Zero state for every control fed by sectionSelect(): a picker with no
    // options makes its paired button inert, which must be explained rather
    // than left as a click that does nothing.
    return el(
      "div",
      { class: "mrln-note" },
      "No sections in the library yet — create one in the Library tab ('New section…')."
    );
  }


  function sectionOptions(typeOf) {
    // The picker's DATA — one source for the plain select and the filtered
    // one. Type-matching sections lead; folders are always offered because a
    // folder pool self-filters at draw time.
    //
    // `typeOf` says what "matches type" means for THIS picker. The compose tab
    // ranks against the open template's type; the section editor passes its
    // own section's `suits`, so a boudoir section ranks boudoir sections first
    // instead of ranking against whatever template happens to be loaded.
    const type = (typeof typeOf === "function" ? typeOf() : state.rawData?.type) ?? [];
    const matches = (suits) =>
      !type.length || !(suits ?? []).length || suits.some((s) => type.includes(s));
    const sections = (state.library?.sections ?? []).map((s) => ({
      value: s.slug,
      label:
        s.slug +
        ((s.suits ?? []).length ? `  [${s.suits.join(",")}]` : "") +
        (s.has_lora ? " [LoRA]" : "") +
        (s.merged ? " ⊕" : ""),
      match: matches(s.suits),
    }));
    const folders = (state.library?.folders ?? []).map((f) => ({
      value: f,
      label: `${f}/ (folder)`,
      match: true,
    }));
    return { type, options: [...folders, ...sections].sort((a, b) => a.value.localeCompare(b.value)) };
  }

  function buildSectionSelect(select, type, options) {
    const primary = options.filter((o) => o.match);
    const other = options.filter((o) => !o.match);
    if (other.length) {
      const groupA = el("optgroup", {
        label: type.length ? `matches type: ${type.join(", ")}` : "sections",
      });
      for (const opt of primary) groupA.append(el("option", { value: opt.value }, opt.label));
      const groupB = el("optgroup", { label: "other domains (suits elsewhere)" });
      for (const opt of other) groupB.append(el("option", { value: opt.value }, opt.label));
      mount(select, groupA, groupB);
    } else {
      mount(select, ...primary.map((opt) => el("option", { value: opt.value }, opt.label)));
    }
    return select;
  }

  function sectionSelect() {
    // Grouped picker over the live library: type-matching + universal
    // sections first, other domains behind an optgroup. Shared by the
    // add-section row and the missing-ref remap card.
    const { type, options } = sectionOptions();
    return buildSectionSelect(el("select", {}), type, options);
  }

  // A filter row over that picker. 210 sections is not a dropdown anyone can
  // browse, and the word a user is hunting is usually not in a slug: looking
  // for a disco they reach for location/everyday, while the word actually
  // lives inside wardrobe/historical's ITEMS. Hence two modes, and a result
  // that says which one found the hit.
  //
  //   Names — instant, local, over slug + label. No request.
  //   Deep  — GET /mrln/prompt/search, which also reads item text and reports
  //           the item that matched.
  //
  // Returns {node, select}: `select` is the same element sectionSelect()
  // always returned, so callers keep reading .value off it.
  //
  // Options:
  //   typeOf   — see sectionOptions(); which suits rank first.
  //   initial  — a ref to start on. It is KEPT selectable through every filter
  //              (pinned at the top when a query excludes it), because a
  //              picker bound to an existing slot must never let a keystroke
  //              silently drop the ref the slot already has.
  //   compact  — one line instead of a column, for a picker sitting in a row
  //              of a table.
  function sectionPicker({ typeOf, initial = "", compact = false } = {}) {
    const { type, options } = sectionOptions(typeOf);
    const current = String(initial ?? "");
    const withCurrent = (rows) =>
      current && !rows.some((row) => (row.value ?? row.slug) === current)
        ? [{ value: current, label: `${current}  (current)`, match: true }, ...rows]
        : rows;
    const select = buildSectionSelect(el("select", {}), type, withCurrent(options));
    if (current) select.value = current;
    const note = el("span", { class: "mrln-note" }, "");
    let deep = false;
    let timer = null;
    let generation = 0;

    function applyLocal(query) {
      const kept = filterSectionOptions(options, query);
      buildSectionSelect(select, type, withCurrent(kept));
      if (current && !select.value) select.value = current;
      note.textContent = kept.length
        ? `${kept.length} match${kept.length === 1 ? "" : "es"}`
        : "no section NAME matches — switch to Deep to search what is inside them";
    }

    async function applyDeep(query) {
      const mine = ++generation;
      note.textContent = "searching…";
      let body;
      try {
        body = await ctx.apiJson(`/mrln/prompt/search?q=${encodeURIComponent(query)}&scope=both`);
      } catch (err) {
        if (mine !== generation) return;
        if (err.status === 404) {
          // The endpoint is newer than the running server. Routes register at
          // ComfyUI startup, so a pack updated underneath a live ComfyUI 404s
          // here — "HTTP 404" alone sends the user hunting for a bug that is
          // really a restart. Fall back to the local filter so the control
          // still does something useful in the meantime.
          applyLocal(query);
          note.textContent =
            "deep search needs a ComfyUI restart (the endpoint is newer than the running "
            + "server) — showing name matches instead";
          return;
        }
        note.textContent = [err.message, err.remediation].filter(Boolean).join(" — ");
        return;
      }
      if (mine !== generation) return; // a newer keystroke owns the list
      const results = body.results ?? [];
      mount(
        select,
        ...withCurrent(results).map((row) =>
          el("option", { value: row.slug ?? row.value }, row.slug ? deepLabel(row) : row.label)
        )
      );
      if (current && !select.value) select.value = current;
      note.textContent = results.length
        ? `${results.length}${body.truncated ? "+" : ""} section(s)`
        : "nothing in the library mentions that";
    }

    const run = () => {
      const query = filter.value.trim();
      clearTimeout(timer);
      if (!query) {
        generation++; // cancel a deep search still in flight
        buildSectionSelect(select, type, withCurrent(options));
        if (current && !select.value) select.value = current;
        note.textContent = "";
        return;
      }
      if (!deep) {
        applyLocal(query);
        return;
      }
      timer = setTimeout(() => applyDeep(query), 250); // one request per pause
    };

    const filter = el("input", {
      type: "text",
      placeholder: "filter sections…",
      title:
        "Narrow the list. 'Names' matches the slug and label; 'Deep' also searches "
        + "what is INSIDE each section and tells you which item matched.",
      oninput: run,
    });
    // A single button that just said "Names" read as a label, not a control —
    // nothing about it said it toggled, and the state it would toggle TO was
    // invisible. Both scopes are on screen now, the active one filled.
    const segments = [];
    const paintSegments = () => {
      for (const seg of segments) {
        seg.setAttribute("aria-pressed", String(seg.dataset.deep === String(deep)));
      }
    };
    const segment = (label, value, tip) => {
      const button = el(
        "button",
        {
          class: "mrln-btn pc-seg",
          "data-deep": String(value),
          title: tip,
          onclick: () => {
            if (deep === value) return;
            deep = value;
            paintSegments();
            run();
          },
        },
        label
      );
      segments.push(button);
      return button;
    };
    const modeButton = el(
      "span",
      {
        class: "pc-segmented",
        role: "group",
        "aria-label": "Search scope",
      },
      segment("Names", false, "Match the section slug and label — instant, no request"),
      segment("Deep", true, "Also search what is INSIDE each section, and say which item matched")
    );
    paintSegments();
    const node = compact
      ? el("div", { class: "mrln-picker mrln-picker-compact" }, select, filter, modeButton, note)
      : el(
          "div",
          { class: "mrln-picker" },
          el("div", { class: "mrln-inline" }, filter, modeButton),
          select,
          note
        );
    return { node, select };
  }

  function addSectionRow() {
    const picker = sectionPicker();
    const refSelect = picker.select;
    const addButton = el(
      "button",
      {
        class: "mrln-btn",
        disabled: refSelect.options.length ? null : "",
        onclick: (e) =>
          busy(e.currentTarget, async () => {
            const ref = refSelect.value;
            if (!ref) return;
            const id = uniqueName(ref.split("/").pop(), new Set(allSlots().map((s) => s.id)));
            state.rawData.slots = state.rawData.slots ?? [];
            state.rawData.slots.push({ id, ref });
            state.orderIds.push(id);
            state.rows.set(id, parseToken("random"));
            await ensurePool(ref, { force: true }); // user-initiated: retry a failed pool now
            markModified();
            renderComposeTab();
            schedulePreview();
          }),
      },
      "+ Add"
    );
    return el(
      "div",
      { class: "mrln-addrow", title: "Add a section (or folder scope) as a new slot" },
      // the picker's node carries the filter row AND the select
      picker.node,
      addButton,
      refSelect.options.length ? null : emptyLibraryNote()
    );
  }

  function renderPreview(preview, err) {
    if (err) {
      mount(previewBox, 
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
      mount(previewBox, el("div", { class: "mrln-note" }, "Previewing…"));
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
        // The design puts a length readout on this line, and it earns the
        // space: prompt length is the one thing about a composed prompt you
        // cannot see by reading it. A word/comma count, NOT a real tokenizer —
        // labelled 'terms' rather than 'tokens' so it never claims to be the
        // model's count.
        el(
          "span",
          {
            class: "pc-meta",
            title: "Comma-separated fragments and words in the positive prompt. "
              + "An indication of length, not a model tokenizer count.",
          },
          `${(preview.positive.match(/[^\s,]+/g) ?? []).length} terms`
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
            onclick: async () => {
              // Over plain http from another machine (a normal ComfyUI LAN
              // setup) navigator.clipboard does not exist at all, and even
              // where it does writeText rejects on a denied permission — the
              // success toast has to follow the actual outcome.
              if (!navigator.clipboard?.writeText) {
                ctx.toast(
                  "error",
                  "Clipboard unavailable",
                  "This page is not a secure context (plain http), so the browser "
                    + "exposes no clipboard API — select the text above and copy it."
                );
                return;
              }
              try {
                await navigator.clipboard.writeText(preview.positive);
                ctx.toast("success", "Prompt copied");
              } catch (err) {
                ctx.toast("error", "Copy failed", err.message);
              }
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
    children.push(choicesFold(preview));
    if (preview.negative) children.push(fold("Negative", preview.negative, "negativeOpen"));
    mount(previewBox, ...children);
  }

  /**
   * CHOICES DRAWN as a table, not a paragraph.
   *
   * Built from the preview's structured slots, NOT by parsing `preview.choices`
   * — that string is a node OUTPUT with a frozen shape, and re-deriving it here
   * with a regex would make a display change able to break a render report.
   * The dotted id already carries the hierarchy, so nested draws need no indent.
   */
  function choicesFold(preview) {
    const rows = [];
    if (preview.variant) {
      rows.push({
        id: "variant",
        item: preview.variant,
        state: preview.variant_random ? "random" : "fixed",
      });
    }
    const walk = (slots) => {
      for (const slot of slots ?? []) {
        rows.push({
          id: slot.id,
          item: slot.missing
            ? `section '${slot.ref}' is missing`
            : (slot.item ?? (slot.random ? "(omitted)" : "(muted)")),
          state: slot.missing
            ? "missing"
            : slot.item === null || slot.item === undefined
              ? slot.random
                ? "omitted"
                : "muted"
              : slot.fixed_first
                ? "default"
                : slot.random
                  ? "random"
                  : "fixed",
          note: [
            // the preview body carries no master seed — state.seed IS the one
            // it was rendered with, so a per-slot pin is what differs from it
            slot.random && slot.seed_used !== state.seed ? `@${slot.seed_used}` : "",
            slot.tier === "user" ? "user" : "",
            slot.inline ? "inline" : "",
          ]
            .filter(Boolean)
            .join(" · "),
          stale: slot.stale_note ?? "",
        });
        walk(slot.children);
      }
    };
    walk(preview.slots);
    return el(
      "details",
      {
        // pc-fold-top: the folds carry their rule as a border-BOTTOM, so the
        // first one after the preview had nothing above it — NEGATIVE gets its
        // line from this fold's bottom, and this one needs its own.
        class: "mrln-fold pc-fold-top",
        open: state.choicesOpen ? "" : null,
        ontoggle: (e) => {
          state.choicesOpen = e.target.open;
        },
      },
      el(
        "summary",
        {},
        "Choices drawn",
        el("span", { class: "pc-summary-note" }, `${rows.length} per section`)
      ),
      el(
        "div",
        { class: "pc-choices" },
        rows.map((row) =>
          el(
            "div",
            { class: "pc-choice", "data-state": row.state, title: row.stale || "" },
            // The PARENT path gives way, never the leaf: at a nested depth of
            // three, 'configuration.model.natio…' hides the one word that says
            // what was drawn.
            el(
              "span",
              { class: "pc-choice-id", title: row.id },
              row.id.includes(".")
                ? el("span", { class: "pc-choice-path" }, `${row.id.slice(0, row.id.lastIndexOf(".") + 1)}`)
                : null,
              el("span", { class: "pc-choice-leaf" }, row.id.split(".").pop())
            ),
            el(
              "span",
              { class: "pc-choice-item" },
              row.item,
              row.note ? el("span", { class: "pc-choice-note" }, ` ${row.note}`) : null
            ),
            el("span", { class: "pc-choice-state" }, row.state)
          )
        )
      )
    );
  }

  // ---- "Optimize for …" (SPEC 5.3) -----------------------------------------
  // Two preview renders side by side: the template as it reads now, and the
  // same draw in the reading order the target profile asks for. Writing that
  // order into a template is a separate, explicit step — never automatic.

  function optimizeProfiles() {
    return Object.entries(state.detail?.template?.profiles ?? {}).sort((a, b) =>
      a[0].localeCompare(b[0])
    );
  }

  function baselineLabel() {
    const profile = state.profile ?? "standard";
    return profile === "standard" ? "authored order" : `current: ${profile}`;
  }

  function setOptimizeProfile(name) {
    state.optimize.profile = name;
    state.optimize.result = null;
    state.optimize.busy = false;
    state.optimize.runNo += 1; // orphan whatever is in flight
    renderOptimize();
    if (name) runOptimize();
  }

  async function runOptimize() {
    const target = state.optimize.profile;
    if (!target || !state.slug || !state.rawData) return;
    const no = ++state.optimize.runNo;
    const signature = optimizeSignature(state, target);
    const [baseBody, targetBody] = optimizeBodies(state, target);
    state.optimize.busy = true;
    renderOptimize();
    let result;
    try {
      // one round trip, not two sequential ones — the endpoint is pure
      const [authored, optimized] = await Promise.all([
        ctx.apiJson("/mrln/prompt/preview", { method: "POST", body: baseBody }),
        ctx.apiJson("/mrln/prompt/preview", { method: "POST", body: targetBody }),
      ]);
      result = { slug: state.slug, target, signature, authored, optimized };
    } catch (err) {
      result = { slug: state.slug, target, signature, error: err.message };
    }
    if (no !== state.optimize.runNo) return; // a newer comparison owns the box
    state.optimize.busy = false;
    state.optimize.result = result;
    renderOptimize();
  }

  function optimizeColumn(title, body, showNegative) {
    return el(
      "div",
      { class: "mrln-optimize-col" },
      el(
        "div",
        { class: "mrln-optimize-side" },
        el("span", { class: "mrln-field-name" }, title),
        el("span", { class: "mrln-chip" }, body.format)
      ),
      el("pre", { class: "mrln-pre" }, body.positive),
      showNegative
        ? el("div", { class: "mrln-note" }, `negative: ${body.negative || "(empty)"}`)
        : null
    );
  }

  function optimizeOrderList(cmp, body) {
    const byId = new Map((body.slots ?? []).map((slot) => [slot.id, slot]));
    return el(
      "div",
      { class: "mrln-optimize-order" },
      ...cmp.rows.map((row) => {
        const slot = byId.get(row.id);
        const domain = (slot?.section_slug ?? "").split("/")[0];
        return el(
          "div",
          { class: row.was === row.at ? "mrln-optimize-row" : "mrln-optimize-row mrln-moved" },
          el("span", { class: "mrln-optimize-idx" }, `${row.at + 1}`),
          el("span", { class: "mrln-optimize-name" }, slot?.label || row.id),
          domain ? el("span", { class: "mrln-chip" }, domain) : null,
          row.was === row.at
            ? null
            : el("span", { class: "mrln-note" }, row.was < 0 ? "new" : `was ${row.was + 1}`)
        );
      })
    );
  }

  function optimizeVerdict(cmp, target) {
    const notes = [];
    if (!cmp.known) {
      notes.push(
        "This server does not report the reading order, so only the two renders can be "
          + "compared — the write-back needs it."
      );
    } else if (!cmp.sameSet) {
      notes.push(
        `'${target}' renders a different SET of blocks (it changes which slots draw, not just `
          + "their order) — compare the two texts; there is no order to write back."
      );
    } else if (cmp.moved) {
      const moves = cmp.rows.filter((row) => row.was !== row.at).length;
      notes.push(`'${target}' reads this template in a different order — ${moves} block(s) move.`);
    } else {
      notes.push(`'${target}' asks for the order this template already has — nothing to write.`);
    }
    if (cmp.drawChanged) {
      notes.push(
        `⚠ '${target}' also draws different text (its template overrides or text_length), so `
          + "what you see is not the order alone."
      );
    }
    if (cmp.formatChanged) notes.push(`⚠ '${target}' also changes the render format.`);
    if (cmp.negativeChanged) {
      notes.push(`⚠ '${target}' also changes the negative (its negative_policy).`);
    }
    if (cmp.unranked.length) {
      notes.push(
        `${cmp.unranked.length} slot(s) drew nothing here (${cmp.unranked.join(", ")}); they carry `
          + "no section, so the profile cannot rank them — they keep their authored position."
      );
    }
    return notes;
  }

  async function writeOptimizedOrder(button, order) {
    const target = state.optimize.profile;
    const slug = await askString(
      "Write the optimized order",
      `Save a copy of '${state.slug}' whose slot order IS the one '${target}' asks for `
        + "(the saved file is the source — unsaved edits are not included).\n"
        + "Slug for the copy (lowercase, '/' for folders):",
      `${state.slug}-${target}`
    );
    if (!slug?.trim()) return;
    const clean = slug.trim().toLowerCase().replace(/\s+/g, "-");
    if ((state.library?.templates ?? []).some((t) => t.slug === clean)) {
      // overwriting an existing template is destructive — arm, never confirm()
      armDestructive(button, `Really overwrite '${clean}'?`, () => saveOrderedCopy(clean, order));
      return;
    }
    await saveOrderedCopy(clean, order);
  }

  async function saveOrderedCopy(slug, order) {
    // Source is baseRaw — the file on DISK — not rawData: the working copy has
    // the active Target profile's overrides baked in and may carry unsaved
    // edits, and this action must change the reading order and nothing else.
    const target = state.optimize.profile;
    const data = structuredClone(state.baseRaw);
    data.order = [...order];
    data.version = 1;
    try {
      await ctx.apiJson("/mrln/prompt/save-template", {
        method: "POST",
        body: { slug, data },
      });
    } catch (err) {
      ctx.toast("error", "Write failed", err.message);
      return;
    }
    ctx.toast(
      "success",
      "Optimized order written",
      order.join(" ") === state.orderIds.join(" ")
        ? `${slug} — a copy, but the storable part of '${target}' order is the order this `
          + "template already had, so its 'order' is unchanged."
        : `${slug} — ${order.length} block(s) in '${target}' reading order. The profile still `
          + "applies its own order on top; this is the order every other profile now reads."
    );
    ctx.refreshCombos();
    // the comparison described the template we just left
    state.optimize.profile = "";
    state.optimize.result = null;
    state.optimize.runNo += 1;
    await loadLibrary();
    await selectTemplate(slug);
  }

  function optimizeResultNodes(result) {
    const cmp = orderComparison(result.authored, result.optimized);
    const nodes = [];
    if (result.signature !== optimizeSignature(state, result.target)) {
      nodes.push(
        el(
          "div",
          { class: "mrln-note mrln-optimize-stale" },
          "⚠ settings changed since this comparison — press ↻ to re-run it"
        )
      );
    }
    nodes.push(
      el(
        "div",
        { class: "mrln-optimize-cols" },
        optimizeColumn(baselineLabel(), result.authored, cmp.negativeChanged),
        optimizeColumn(`optimized for ${result.target}`, result.optimized, cmp.negativeChanged)
      )
    );
    if (cmp.known) nodes.push(optimizeOrderList(cmp, result.optimized));
    for (const note of optimizeVerdict(cmp, result.target)) {
      nodes.push(el("div", { class: "mrln-note" }, note));
    }
    if (!cmp.moved) return nodes; // nothing moved (or nothing comparable) — no button at all

    const variantIds = (state.rawData?.variants ?? []).flatMap((v) =>
      (v.slots ?? []).map((slot) => slot.id)
    );
    const write = orderWriteBack(result.optimized.render_order, state.orderIds, variantIds);
    // Only an unsaved edit blocks the write: the copy is made from the saved
    // file, so the order on screen is not the one that would be written.
    // Anything a template cannot store verbatim is approximated and said out
    // loud (write.notes) — the storable order still wins.
    const blocked = state.modified
      ? "save your unsaved template edits first — the copy is made from the saved file"
      : null;
    const unchanged = write.order.join("\u0000") === state.orderIds.join("\u0000");
    for (const note of write.notes) nodes.push(el("div", { class: "mrln-note" }, note));
    if (unchanged && !blocked) {
      nodes.push(
        el(
          "div",
          { class: "mrln-note" },
          "Everything that moves here moves inside the variant block, so the order a template "
            + "file can store is the one this template already has — the copy would differ only "
            + "in name. To render this reading order, set Target profile to "
            + `'${result.target}' and Apply instead.`
        )
      );
    }
    nodes.push(
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: "mrln-btn mrln-primary",
            disabled: blocked ? "" : null,
            title: blocked
              ?? `Save a COPY of '${state.slug}' whose slot order is this one. Never automatic, `
                + "and the original is untouched.",
            onclick: (e) => {
              const button = e.currentTarget;
              return busy(button, async () => {
                if (button.mrlnArmed) {
                  await armDestructive(button); // second click — run the armed overwrite
                  return;
                }
                await writeOptimizedOrder(button, write.order);
              });
            },
          },
          "write this order into the template…"
        )
      )
    );
    if (blocked) nodes.push(el("div", { class: "mrln-note" }, blocked));
    return nodes;
  }

  function renderOptimize() {
    const opt = state.optimize;
    if (opt.result && opt.result.slug !== state.slug) opt.result = null; // template switched
    const profiles = optimizeProfiles();
    if (opt.profile && !profiles.some(([name]) => name === opt.profile)) {
      opt.profile = ""; // this template does not offer it
      opt.result = null;
    }
    const select = el("select", {
      title: "Render this template twice — as it reads now, and in the reading order the "
        + "target profile asks for (its block_order). Nothing is written and the Target "
        + "profile above is not touched.",
      onchange: (e) => setOptimizeProfile(e.target.value),
    });
    select.append(el("option", { value: "" }, "— off —"));
    for (const [name, profile] of profiles) {
      const render = profile?.render;
      const ranked =
        render && typeof render === "object" && Object.keys(render.block_order ?? {}).length > 0;
      select.append(el("option", { value: name }, ranked ? `${name} · reading order` : name));
    }
    select.value = opt.profile ?? "";
    const controls = el("div", { class: "mrln-inline" }, select);
    if (opt.profile) {
      controls.append(
        smallBtn(
          "Run the comparison again with the current settings",
          "↻",
          (e) => busy(e.currentTarget, runOptimize),
          opt.busy // the box re-renders while it runs; a live ↻ would just queue work
        ),
        smallBtn("Close the comparison", "✕", () => setOptimizeProfile(""))
      );
    }
    const children = [field("Optimize for", controls)];
    if (!opt.profile) {
      children.push(
        el(
          "div",
          { class: "mrln-note" },
          "Pick a target model to see this template read the way that model wants it — "
            + "authored vs optimized, side by side. Nothing is written unless you ask."
        )
      );
    } else if (opt.busy) {
      children.push(loadingNote(`Rendering ${baselineLabel()} and '${opt.profile}' …`));
    } else if (opt.result?.error) {
      children.push(el("div", { class: "mrln-error" }, opt.result.error));
    } else if (opt.result) {
      children.push(...optimizeResultNodes(opt.result));
    }
    mount(optimizeBox, ...children);
  }

  // ---- node interop --------------------------------------------------------

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
    // The node decides which form it wants to hold. Slug is the default and
    // the stable identifier; a node switched to 'label' gets the human name,
    // and the server reads a known slug as a slug either way.
    const byLabel = ctx.getWidget(node, "template_names") === "label";
    const label = (state.detail?.template?.label ?? state.rawData?.label ?? "").trim();
    // Applying the FACTORY view has to render the factory version — otherwise
    // the panel would show one thing and the node would render another. The
    // prefix is the one and only way a factory file wins over a user file of
    // the same slug, and it is written only because you asked for it here.
    const prefix = state.tierView === "factory" ? "factory:" : "";
    ctx.setWidget(node, "template", prefix + (byLabel && label ? label : state.slug));
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
    if (
      state.profile !== "standard" &&
      !(state.profile in (state.detail?.template?.profiles ?? {}))
    ) {
      // keep the value (the node carries it; Apply must round-trip it) but say
      // so once — the select renders it as '(not installed)'
      ctx.toast(
        "warn",
        "Profile not installed here",
        `The node asks for '${state.profile}', which this library does not define — `
          + "the render falls back to the standard one. Pick another Target profile "
          + "to replace it."
      );
    }
    rebuildForProfile(state.profile); // rows/defaults reflect the node's variant
    applyKvToRows(parseKvLines(ctx.getWidget(node, "selection") ?? ""));
    renderComposeTab();
    schedulePreview();
    ctx.toast("info", "Loaded from node", `template: ${state.slug}`);
  }

  function pinLastDraw() {
    if (!state.lastPreview) {
      ctx.toast(
        "warn",
        "Nothing drawn yet",
        "Pin draw fixes what the live preview last drew — wait for the preview below."
      );
      return;
    }
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

  return {
    // exported so the History tab's Apply can go through the SAME hardened
    // path the Compose button uses — persist to the library first, write the
    // widgets, then verify they landed. A second implementation over there
    // would be a second thing to keep correct.
    applyToNode,
    markModified,
    renderComposeTab,
    renderNested,
    renderOptimize,
    renderPreview,
    sectionPicker,
    sectionSelect,
  };
}
