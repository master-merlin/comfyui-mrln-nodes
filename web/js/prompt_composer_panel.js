// MRLN Prompt Composer — panel implementation. Pure ES module: no imports,
// no top-level side effects (ComfyUI auto-loads every js file in
// WEB_DIRECTORY; the module cache makes that harmless). All computation
// happens server-side via /mrln/prompt/*; this file only moves state
// between DOM, endpoints, and node widgets. The node's selection-lines
// widget format is the single source of truth the panel reads and writes.

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

function parseToken(token) {
  const match = /^(?:🎲 )?random(?:@(\d+))?$/.exec((token ?? "").trim());
  if (match) return { random: true, seed: match[1] ?? "", item: "" };
  return { random: false, seed: "", item: (token ?? "").trim() };
}

function parseKvLines(text) {
  const map = {};
  for (const raw of (text ?? "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    map[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return map;
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
    variant: null, // active variant name | "random" | null
    mode: "as configured",
    seed: 0,
    format: "template default",
    trigger: "",
    variables: "",
    rows: new Map(), // slot id -> {random, seed, item}
    lastPreview: null,
    previewNo: 0,
    previewTimer: null,
    tab: "compose",
    editor: null, // {kind, slug} open in the library tab
  };

  // ---- skeleton ------------------------------------------------------------

  const composeTab = el("div");
  const libraryTab = el("div", { style: "display:none" });
  const tabButtons = el(
    "div",
    { class: "mrln-tabs" },
    el("button", { class: "mrln-active", onclick: () => switchTab("compose") }, "Compose"),
    el("button", { onclick: () => switchTab("library") }, "Library")
  );
  root.replaceChildren(tabButtons, composeTab, libraryTab);

  function switchTab(name) {
    state.tab = name;
    composeTab.style.display = name === "compose" ? "" : "none";
    libraryTab.style.display = name === "library" ? "" : "none";
    tabButtons.querySelectorAll("button").forEach((button, i) => {
      button.classList.toggle("mrln-active", (i === 0) === (name === "compose"));
    });
    if (name === "library") renderLibraryTab();
  }

  // ---- data loading --------------------------------------------------------

  async function loadLibrary(keepSelection = true) {
    try {
      state.library = await ctx.apiJson("/mrln/prompt/library");
    } catch (err) {
      composeTab.replaceChildren(
        el("div", { class: "mrln-error" }, `Library unavailable: ${err.message}`)
      );
      return;
    }
    const slugs = state.library.templates.map((t) => t.slug);
    if (!keepSelection || !slugs.includes(state.slug)) state.slug = slugs[0] ?? null;
    if (state.slug) await selectTemplate(state.slug, { keepRows: keepSelection });
    else renderComposeTab();
    if (state.tab === "library") renderLibraryTab();
  }

  async function selectTemplate(slug, { keepRows = false } = {}) {
    state.slug = slug;
    try {
      state.detail = await ctx.apiJson(
        `/mrln/prompt/template?slug=${encodeURIComponent(slug)}`
      );
    } catch (err) {
      composeTab.replaceChildren(
        el("div", { class: "mrln-error" }, `Cannot load '${slug}': ${err.message}`)
      );
      return;
    }
    const tpl = state.detail.template;
    if (!keepRows) {
      state.rows = new Map();
      state.variant = tpl.variants.length
        ? tpl.variant_default || tpl.variants[0].name
        : null;
    }
    for (const slot of allSlots()) {
      if (!state.rows.has(slot.id)) state.rows.set(slot.id, parseToken(slot.default));
    }
    state.lastPreview = null;
    renderComposeTab();
    schedulePreview();
  }

  function allSlots() {
    const tpl = state.detail?.template;
    if (!tpl) return [];
    return [...tpl.slots, ...tpl.variants.flatMap((v) => v.slots)];
  }

  function activeSlots() {
    const tpl = state.detail?.template;
    if (!tpl) return [];
    const active = [...tpl.slots];
    if (state.variant && state.variant !== "random") {
      const variant = tpl.variants.find((v) => v.name === state.variant);
      if (variant) active.push(...variant.slots);
    }
    return active;
  }

  // ---- selection lines (the persistence format) ----------------------------

  function rowToken(slot) {
    const row = state.rows.get(slot.id) ?? parseToken(slot.default);
    if (row.random) return row.seed ? `random@${row.seed}` : "random";
    return row.item;
  }

  function buildSelectionLines() {
    const tpl = state.detail.template;
    const lines = [];
    if (tpl.variants.length) {
      const fallback = tpl.variant_default || tpl.variants[0].name;
      if (state.variant !== fallback) lines.push(`variant=${state.variant}`);
    }
    for (const slot of activeSlots()) {
      const token = rowToken(slot);
      if (token && token !== slot.default) lines.push(`${slot.id}=${token}`);
    }
    return lines.join("\n");
  }

  function applyKvToRows(map) {
    const tpl = state.detail.template;
    if (map.variant && tpl.variants.length) state.variant = map.variant;
    for (const slot of allSlots()) {
      if (map[slot.id] !== undefined) state.rows.set(slot.id, parseToken(map[slot.id]));
    }
  }

  // ---- preview -------------------------------------------------------------

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(doPreview, 300);
  }

  async function doPreview() {
    if (!state.slug || !state.detail) return;
    const no = ++state.previewNo;
    const body = {
      template: state.slug,
      seed: state.seed,
      mode: state.mode,
      selection: buildSelectionLines(),
      variables: state.variables,
      trigger: state.trigger,
      format: state.format,
    };
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
  }

  // ---- compose tab rendering ----------------------------------------------

  const previewBox = el("div");

  function field(name, control) {
    return el("label", { class: "mrln-field" }, el("span", { class: "mrln-field-name" }, name), control);
  }

  function renderComposeTab() {
    const tpl = state.detail?.template;
    if (!tpl) {
      composeTab.replaceChildren(
        el("div", { class: "mrln-note" }, "No templates in the library yet — create one in the Library tab.")
      );
      return;
    }

    const templateSelect = el("select", {
      onchange: (e) => selectTemplate(e.target.value),
    });
    for (const t of state.library.templates) {
      templateSelect.append(
        el("option", { value: t.slug }, t.tier === "user" ? `${t.slug} (user)` : t.slug)
      );
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
      {
        class: "mrln-btn",
        title: "New random master seed",
        onclick: () => {
          state.seed = Math.floor(Math.random() * 0xffffffff);
          seedInput.value = state.seed;
          schedulePreview();
        },
      },
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
      field("Template", el("div", { class: "mrln-inline" }, templateSelect, tierChip(state.detail.tier))),
    ];
    if (tpl.description) parts.push(el("div", { class: "mrln-note" }, tpl.description));

    if (tpl.variants.length) {
      const variantSelect = el("select", {
        onchange: (e) => {
          state.variant = e.target.value;
          renderComposeTab();
          schedulePreview();
        },
      });
      variantSelect.append(el("option", { value: "random" }, "🎲 random"));
      for (const v of tpl.variants) {
        variantSelect.append(el("option", { value: v.name }, v.label || v.name));
      }
      variantSelect.value = state.variant;
      parts.push(field("Variant", variantSelect));
      if (state.variant === "random") {
        parts.push(
          el(
            "div",
            { class: "mrln-note" },
            "Variant is drawn from the seed — pick a variant to control its slots."
          )
        );
      }
    }

    parts.push(
      field("Mode", modeSelect),
      field("Master seed", el("div", { class: "mrln-inline" }, seedInput, reroll)),
      field("Format", formatSelect),
      el("hr", { class: "mrln-sep" }),
      el("div", {}, activeSlots().map((slot) => slotRow(slot))),
      el("hr", { class: "mrln-sep" })
    );

    const hasTriggerVar = tpl.variables.some((v) => v.name === "trigger");
    parts.push(
      field(
        hasTriggerVar ? "Trigger ({trigger})" : "Trigger",
        el("input", {
          type: "text",
          value: state.trigger,
          placeholder: tpl.variables.find((v) => v.name === "trigger")?.default ?? "",
          oninput: (e) => {
            state.trigger = e.target.value;
            schedulePreview();
          },
        })
      )
    );
    const extraVars = tpl.variables.filter((v) => v.name !== "trigger");
    if (extraVars.length || state.variables) {
      parts.push(
        field(
          `Variables (${extraVars.map((v) => v.name).join(", ") || "name=value"})`,
          el("textarea", {
            rows: 2,
            placeholder: extraVars.map((v) => `${v.name}=${v.default}`).join("\n"),
            oninput: (e) => {
              state.variables = e.target.value;
              schedulePreview();
            },
          }, state.variables)
        )
      );
    }

    parts.push(
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn mrln-primary", onclick: applyToNode }, "Apply to node"),
        el("button", { class: "mrln-btn", onclick: loadFromNode }, "Load from node"),
        el("button", { class: "mrln-btn", onclick: pinLastDraw }, "Pin last draw"),
        el("button", { class: "mrln-btn", onclick: saveAsTemplate }, "Save as template…")
      ),
      previewBox
    );

    composeTab.replaceChildren(...parts);
    renderPreview(state.lastPreview, null);
  }

  function slotRow(slot) {
    const pool = state.detail.pools[slot.ref] ?? [];
    const row = state.rows.get(slot.id) ?? parseToken(slot.default);
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
    itemSelect.append(el("option", { value: "random" }, "🎲 random"));
    for (const item of pool) {
      const marks = [
        item.name === parseToken(slot.default).item ? "•" : "",
        item.tier === "user" ? "(user)" : "",
      ]
        .filter(Boolean)
        .join(" ");
      itemSelect.append(
        el("option", { value: item.name, title: item.text }, marks ? `${item.name} ${marks}` : item.name)
      );
    }
    itemSelect.value = row.random ? "random" : row.item;
    if (itemSelect.value === "") itemSelect.value = "random"; // stale item name

    const seedInput = el("input", {
      class: "mrln-narrow",
      type: "text",
      inputmode: "numeric",
      placeholder: "seed",
      title: "Optional per-slot seed — decouples this slot from the master seed",
      value: row.seed,
      style: row.random ? "" : "display:none",
      oninput: (e) => {
        row.seed = e.target.value.replace(/\D/g, "");
        schedulePreview();
      },
    });

    const chips = [];
    if (slot.allow_empty) chips.push(el("span", { class: "mrln-chip" }, "optional"));
    if (slot.emphasis && slot.emphasis !== 1) {
      chips.push(el("span", { class: "mrln-chip" }, `×${slot.emphasis}`));
    }
    const labelText = slot.label && slot.label.length <= 60 ? slot.label : slot.id;
    return el(
      "div",
      { class: "mrln-slot" },
      el("div", { class: "mrln-slot-label", title: `${slot.id} → ${slot.ref}` }, labelText, chips),
      el("div", { class: "mrln-inline" }, itemSelect, seedInput)
    );
  }

  function renderPreview(preview, err) {
    if (err) {
      previewBox.replaceChildren(
        el("div", { class: "mrln-error" }, err.message, err.remediation ? `\n${err.remediation}` : "")
      );
      return;
    }
    if (!preview) {
      previewBox.replaceChildren(el("div", { class: "mrln-note" }, "Previewing…"));
      return;
    }
    const children = [];
    if (preview.variant && state.variant === "random") {
      children.push(el("div", { class: "mrln-note" }, `Drew variant: ${preview.variant}`));
    }
    children.push(
      el("span", { class: "mrln-field-name" }, "Prompt preview"),
      el("pre", { class: "mrln-pre" }, preview.positive),
      el("span", { class: "mrln-field-name" }, "Choices"),
      el("pre", { class: "mrln-pre" }, preview.choices)
    );
    if (preview.negative) {
      children.push(
        el(
          "details",
          { class: "mrln-fold" },
          el("summary", {}, "Negative"),
          el("pre", { class: "mrln-pre" }, preview.negative)
        )
      );
    }
    previewBox.replaceChildren(...children);
  }

  // ---- node interop --------------------------------------------------------

  function applyToNode() {
    const node = ctx.selectedTemplateNode();
    if (!node) {
      ctx.toast("warn", "No node selected", "Select a Prompt Template (MRLN) node first.");
      return;
    }
    ctx.setWidget(node, "template", state.slug);
    ctx.setWidget(node, "selection", buildSelectionLines());
    ctx.setWidget(node, "selection_mode", state.mode);
    ctx.setWidget(node, "seed", state.seed);
    ctx.setWidget(node, "format", state.format);
    ctx.setWidget(node, "trigger", state.trigger);
    ctx.setWidget(node, "variables", state.variables);
    ctx.markDirty();
    ctx.toast("success", "Applied to node", `template: ${state.slug}`);
  }

  async function loadFromNode() {
    const node = ctx.selectedTemplateNode();
    if (!node) {
      ctx.toast("warn", "No node selected", "Select a Prompt Template (MRLN) node first.");
      return;
    }
    const slug = ctx.getWidget(node, "template");
    if (slug && slug !== state.slug) await selectTemplate(slug);
    if (!state.detail) return;
    state.mode = ctx.getWidget(node, "selection_mode") ?? state.mode;
    state.seed = Number(ctx.getWidget(node, "seed") ?? state.seed) || 0;
    state.format = ctx.getWidget(node, "format") ?? state.format;
    state.trigger = ctx.getWidget(node, "trigger") ?? "";
    state.variables = ctx.getWidget(node, "variables") ?? "";
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

  async function askString(title, message, defaultValue = "") {
    if (ctx.dialog?.prompt) return await ctx.dialog.prompt({ title, message, defaultValue });
    return window.prompt(`${title}\n${message}`, defaultValue);
  }

  async function saveAsTemplate() {
    const slug = await askString(
      "Save as template",
      "Template slug (lowercase, '/' for folders):",
      `${state.slug}-mine`
    );
    if (!slug) return;
    const data = structuredClone(state.detail.raw);
    const setDefaults = (slots) => {
      for (const slot of slots ?? []) {
        if (state.rows.has(slot.id)) {
          slot.default = rowToken({ id: slot.id, default: slot.default ?? "random" });
        }
      }
    };
    setDefaults(data.slots);
    for (const variant of data.variants ?? []) {
      if (state.variant === variant.name) setDefaults(variant.slots);
    }
    if ((data.variants ?? []).length && state.variant) data.variant_default = state.variant;
    try {
      await ctx.apiJson("/mrln/prompt/save-template", { method: "POST", body: { slug, data } });
    } catch (err) {
      ctx.toast("error", "Save failed", err.message);
      return;
    }
    ctx.toast("success", "Template saved", `${slug} (user library)`);
    ctx.refreshCombos();
    await loadLibrary();
  }

  // ---- library tab ---------------------------------------------------------

  const editorBox = el("div");

  function renderLibraryTab() {
    if (!state.library) return;
    const lib = state.library;
    const sectionList = el("ul", { class: "mrln-tree" });
    for (const section of lib.sections) {
      sectionList.append(
        el(
          "li",
          { onclick: () => openSectionEditor(section.slug) },
          section.error ? `⚠ ${section.slug}` : section.label,
          el("span", { class: "mrln-slug" }, `${section.slug} · ${section.item_count ?? "?"} items`),
          tierChip(section.tier)
        )
      );
    }
    const templateList = el("ul", { class: "mrln-tree" });
    for (const template of lib.templates) {
      templateList.append(
        el(
          "li",
          { onclick: () => openTemplateEditor(template.slug) },
          template.error ? `⚠ ${template.slug}` : template.label,
          el("span", { class: "mrln-slug" }, template.slug),
          tierChip(template.tier)
        )
      );
    }
    libraryTab.replaceChildren(
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn", onclick: () => newSection() }, "New section…"),
        el("button", { class: "mrln-btn", onclick: () => loadLibrary() }, "Reload")
      ),
      el("div", { class: "mrln-tree-head" }, "Sections"),
      sectionList,
      el("div", { class: "mrln-tree-head" }, "Templates"),
      templateList,
      el("hr", { class: "mrln-sep" }),
      editorBox
    );
  }

  function newSection() {
    openSectionForm(null, {
      label: "",
      description: "",
      negative: "",
      items: [{ name: "", text: "" }],
      raw: { items: [] },
      tier: "",
    });
  }

  async function openSectionEditor(slug) {
    let body;
    try {
      body = await ctx.apiJson(`/mrln/prompt/section?slug=${encodeURIComponent(slug)}`);
    } catch (err) {
      editorBox.replaceChildren(el("div", { class: "mrln-error" }, err.message));
      return;
    }
    openSectionForm(slug, body);
  }

  function openSectionForm(slug, body) {
    const slugInput = el("input", { type: "text", value: slug ?? "", placeholder: "folder/name" });
    const labelInput = el("input", { type: "text", value: body.label ?? "" });
    const descInput = el("input", { type: "text", value: body.description ?? "" });
    const negInput = el("input", { type: "text", value: body.negative ?? "" });
    const itemRows = [];
    const table = el("table", { class: "mrln-items-table" });
    table.append(
      el(
        "tr",
        {},
        el("td", { class: "mrln-w-name mrln-note" }, "name"),
        el("td", { class: "mrln-note" }, "text"),
        el("td", { class: "mrln-w-weight mrln-note" }, "wt"),
        el("td", { class: "mrln-w-del" })
      )
    );

    function addItemRow(item = { name: "", text: "" }) {
      const row = {
        orig: item,
        name: el("input", { type: "text", value: item.name ?? "" }),
        text: el("input", { type: "text", value: item.text ?? "", title: item.text ?? "" }),
        weight: el("input", { type: "text", value: item.weight ?? "" }),
      };
      const tr = el(
        "tr",
        {},
        el("td", { class: "mrln-w-name" }, row.name),
        el("td", {}, row.text),
        el("td", { class: "mrln-w-weight" }, row.weight),
        el(
          "td",
          { class: "mrln-w-del" },
          el(
            "button",
            {
              class: "mrln-btn",
              title: "Remove item",
              onclick: () => {
                itemRows.splice(itemRows.indexOf(row), 1);
                tr.remove();
              },
            },
            "✕"
          )
        )
      );
      itemRows.push(row);
      table.append(tr);
    }
    for (const item of body.items ?? []) addItemRow(item);

    async function save() {
      const targetSlug = slugInput.value.trim();
      const data = { ...body.raw, version: 1 };
      if (labelInput.value.trim()) data.label = labelInput.value.trim();
      else delete data.label;
      if (descInput.value.trim()) data.description = descInput.value.trim();
      else delete data.description;
      if (negInput.value.trim()) data.negative = negInput.value.trim();
      else delete data.negative;
      data.items = itemRows.map((row) => {
        const item = { ...row.orig, name: row.name.value.trim(), text: row.text.value };
        if (!item.name) delete item.name;
        const weight = parseFloat(row.weight.value);
        if (!Number.isNaN(weight) && weight !== 1) item.weight = weight;
        else delete item.weight;
        if (!item.negative) delete item.negative;
        return item;
      });
      try {
        await ctx.apiJson("/mrln/prompt/save-section", {
          method: "POST",
          body: { slug: targetSlug, data },
        });
      } catch (err) {
        ctx.toast("error", "Save failed", err.message);
        return;
      }
      ctx.toast("success", "Section saved", `${targetSlug} (user library)`);
      ctx.refreshCombos();
      await loadLibrary();
      openSectionEditor(targetSlug);
    }

    const actions = [el("button", { class: "mrln-btn mrln-primary", onclick: save }, "Save to user library")];
    if (body.tier === "user") {
      actions.push(
        el(
          "button",
          { class: "mrln-btn", onclick: () => deleteEntry("sections", slug) },
          "Delete user file"
        )
      );
    }

    editorBox.replaceChildren(
      el("div", { class: "mrln-tree-head" }, slug ? `Section: ${slug}` : "New section", tierChip(body.tier)),
      body.tier === "factory"
        ? el("div", { class: "mrln-note" }, "Factory file — saving creates a user-tier override.")
        : null,
      field("Slug", slugInput),
      field("Label", labelInput),
      field("Description", descInput),
      field("Negative", negInput),
      el("span", { class: "mrln-field-name" }, "Items"),
      table,
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn", onclick: () => addItemRow() }, "+ item"),
        ...actions
      )
    );
  }

  async function openTemplateEditor(slug) {
    let body;
    try {
      body = await ctx.apiJson(`/mrln/prompt/template?slug=${encodeURIComponent(slug)}`);
    } catch (err) {
      editorBox.replaceChildren(el("div", { class: "mrln-error" }, err.message));
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
      ctx.toast("success", "Template saved", `${slugInput.value.trim()} (user library)`);
      ctx.refreshCombos();
      await loadLibrary();
    }

    const actions = [el("button", { class: "mrln-btn mrln-primary", onclick: save }, "Save to user library")];
    if (body.tier === "user") {
      actions.push(
        el(
          "button",
          { class: "mrln-btn", onclick: () => deleteEntry("templates", slug) },
          "Delete user file"
        )
      );
    }
    editorBox.replaceChildren(
      el("div", { class: "mrln-tree-head" }, `Template: ${slug}`, tierChip(body.tier)),
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
    editorBox.replaceChildren();
    ctx.refreshCombos();
    await loadLibrary();
  }

  // ---- boot ----------------------------------------------------------------

  loadLibrary(false);

  return () => clearTimeout(state.previewTimer);
}
