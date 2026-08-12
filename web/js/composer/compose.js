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
  uniqueName,
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
  validateRefs,
} from "./dom.js";

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
 * render's reading order, or `{error}` when it cannot be expressed.
 *
 * Resolved variant slots render as "<variant>/<slot id>" and the whole block
 * rides ONE "@variant" token, so a policy that interleaves variant slots with
 * shared ones has no representation on disk — refuse instead of writing an
 * order that renders differently from the comparison the user just approved.
 */
export function orderWriteBack(renderOrder, orderIds) {
  const shared = new Set((orderIds ?? []).filter((id) => id !== "@variant"));
  const out = [];
  let variantPlaced = false;
  for (const id of renderOrder ?? []) {
    if (shared.has(id)) {
      out.push(id);
      continue;
    }
    if (!id.includes("/")) return { error: `'${id}' is not a slot of this template` };
    if (out[out.length - 1] === "@variant") continue; // still inside the block
    if (variantPlaced) {
      return {
        error: "this order splits the variant block apart, and a template stores it as one "
          + "'@variant' entry — reorder the slots by hand instead",
      };
    }
    out.push("@variant");
    variantPlaced = true;
  }
  const missing = (orderIds ?? []).filter((id) => !out.includes(id));
  if (missing.length) {
    return {
      error: `this draw never rendered ${missing.join(", ")}, so the order cannot place `
        + "them — un-mute them (and pick a variant) and compare again",
    };
  }
  return { order: out };
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
  const slotAudible = (...a) => hub.slotAudible(...a);
  const switchTab = (...a) => hub.switchTab(...a);
  const variantBlockAudible = (...a) => hub.variantBlockAudible(...a);

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
          title: "Write template + settings to the node. Unsaved template edits "
            + "are saved to your user library first — the node always renders "
            + "the saved file.",
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
      el("button", { class: "mrln-btn", onclick: () => loadFromNode() }, "Load"),
      el("button", { class: "mrln-btn", title: "Fix every random slot to what the preview just drew", onclick: () => pinLastDraw() }, "Pin draw"),
      el(
        "button",
        {
          class: "mrln-btn",
          title: "Save this template (current picks become its defaults) to your user library",
          onclick: (e) => busy(e.currentTarget, () => saveTemplate(state.slug)),
        },
        "Save"
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
        mount(mount, 
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
        mount(mount);
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
              onclick: (e) => busy(e.currentTarget, newTemplate),
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
          smallBtn(
            "Move variant block up",
            "↑",
            () => moveOrder(orderIndex, -1),
            orderIndex === 0
          ),
          smallBtn(
            "Move variant block down",
            "↓",
            () => moveOrder(orderIndex, 1),
            orderIndex === state.orderIds.length - 1
          )
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
            smallBtn(
              "Move up within the variant",
              "↑",
              () => {
                if (index > 0) {
                  [container[index - 1], container[index]] = [
                    container[index],
                    container[index - 1],
                  ];
                  markModified();
                  renderComposeTab();
                  schedulePreview();
                }
              },
              index === 0
            ),
            smallBtn(
              "Move down within the variant",
              "↓",
              () => {
                if (index < container.length - 1) {
                  [container[index + 1], container[index]] = [
                    container[index],
                    container[index + 1],
                  ];
                  markModified();
                  renderComposeTab();
                  schedulePreview();
                }
              },
              index === container.length - 1
            ),
          ]
        : [
            smallBtn("Move up", "↑", () => moveOrder(orderIndex, -1), orderIndex === 0),
            smallBtn(
              "Move down",
              "↓",
              () => moveOrder(orderIndex, 1),
              orderIndex === state.orderIds.length - 1
            ),
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
      refSelect,
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
    children.push(fold("Choices (what was drawn per section)", preview.choices, "choicesOpen"));
    if (preview.negative) children.push(fold("Negative", preview.negative, "negativeOpen"));
    mount(previewBox, ...children);
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
      `${slug} — ${order.length} block(s) in '${target}' reading order. The profile still `
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

    const write = orderWriteBack(result.optimized.render_order, state.orderIds);
    const blocked = state.modified
      ? "save your unsaved template edits first — the copy is made from the saved file"
      : (write.error ?? null);
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
    markModified,
    renderComposeTab,
    renderNested,
    renderOptimize,
    renderPreview,
    sectionSelect,
  };
}
