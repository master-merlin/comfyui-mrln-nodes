// MRLN Prompt Composer — Library tab: the collapsible section/template tree,
// the browse CARD GRID over the same entries, the shared editor mount (every
// editor renders into it), the profile list and its editor, and the user-file
// delete flow.
//
// ROWS AND CARDS ARE ONE TREE. `state.grid` flips only the leaf renderer:
// grouping, the filter, the fold state and every action are shared, so the two
// views can never drift apart and a filtered/expanded state survives the
// toggle. Cards draw thumbs.js tiles (a thumbnail when the row has one, the
// domain glyph otherwise — see thumbs.js for the resolution order).
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). The editor mount and
// its dirty flag live inside createTree().
import { armDestructive, autoArea, busy, el, field, loadingNote, mount, tierChip } from "./dom.js";

export function createTree(hub) {
  const { ctx, state, libraryTab } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const confirmTwoStep = (...a) => hub.confirmTwoStep(...a);
  const exportBtn = (...a) => hub.exportBtn(...a);
  const importBundlePicker = (...a) => hub.importBundlePicker(...a);
  const libraryErrorNote = (...a) => hub.libraryErrorNote(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const newCombineSection = (...a) => hub.newCombineSection(...a);
  const newSection = (...a) => hub.newSection(...a);
  const newTemplate = (...a) => hub.newTemplate(...a);
  const openSectionEditor = (...a) => hub.openSectionEditor(...a);
  const openTemplateEditor = (...a) => hub.openTemplateEditor(...a);
  const refreshDetail = (...a) => hub.refreshDetail(...a);
  const thumbTile = (...a) => hub.thumbTile(...a);

  const editorBox = el("div");
  // Editor forms live only in closures — replacing editorBox drops typed
  // content. Track typing (capture phase: some internal events don't
  // bubble) and gate every user-initiated replacement on confirmReplaceEditor.
  let editorDirty = false;
  editorBox.addEventListener("input", () => (editorDirty = true), true);
  editorBox.addEventListener("change", () => (editorDirty = true), true);

  function setEditor(...children) {
    mount(editorBox, ...children);
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

  function markEditorClean() {
    // The template editor's JSON save makes the editor match disk again.
    // Exported because that editor is its own module now (in the single-closure
    // panel it just assigned the shared 'let').
    editorDirty = false;
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

  function openEntry(kind, slug) {
    if (!confirmReplaceEditor()) return;
    if (kind === "sections") openSectionEditor(slug);
    else openTemplateEditor(slug);
  }

  // Chips and the export button are identical in both views — built once here
  // so a row and a card can never end up saying different things about the
  // same entry.
  function sectionChips(section) {
    return [
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
      section.tier === "user" || section.merged ? exportBtn("section", section.slug) : null,
    ];
  }

  function templateChips(template) {
    return [tierChip(template.tier), exportBtn("template", template.slug)];
  }

  function sectionLi(section) {
    return el(
      "li",
      { onclick: () => openEntry("sections", section.slug) },
      section.error ? `⚠ ${section.slug}` : section.label,
      el(
        "span",
        { class: "mrln-slug" },
        `${section.slug} · ${section.item_count ?? "?"} items`
      ),
      ...sectionChips(section)
    );
  }

  function templateLi(template) {
    return el(
      "li",
      { onclick: () => openEntry("templates", template.slug) },
      template.error ? `⚠ ${template.slug}` : template.label,
      el("span", { class: "mrln-slug" }, template.slug),
      ...templateChips(template)
    );
  }

  function entryCard(kind, entry) {
    // The tile decides for itself whether to request an image: `has_thumb`
    // comes straight from the listing row (thumbs.annotate_entries), so a
    // library with no thumbnails at all costs ZERO requests here.
    return el(
      "div",
      {
        class: `mrln-lib-card${entry.error ? " mrln-lib-card-error" : ""}`,
        tabindex: "0",
        title: entry.error
          ? `⚠ ${entry.error}`
          : entry.description || `${entry.slug} — click to edit`,
        onclick: () => openEntry(kind, entry.slug),
        onkeydown: (e) => {
          // A card is a div (it holds the export button, and a button inside a
          // button is invalid HTML), so Enter/Space are wired by hand — but
          // ONLY for the card itself: a Space landing on the nested export
          // button bubbles here, and acting on it would both export and open
          // the editor.
          if (e.target !== e.currentTarget) return;
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          openEntry(kind, entry.slug);
        },
      },
      thumbTile(kind, entry.slug, {
        hasThumb: entry.has_thumb,
        size: "lg",
        alt: "",
      }),
      el(
        "span",
        { class: "mrln-lib-card-name" },
        entry.error ? `⚠ ${entry.slug}` : entry.label || entry.slug
      ),
      el(
        "span",
        { class: "mrln-slug mrln-lib-card-slug" },
        kind === "sections"
          ? `${entry.slug} · ${entry.item_count ?? "?"} items`
          : entry.slug
      ),
      el(
        "span",
        { class: "mrln-lib-card-chips" },
        ...(kind === "sections" ? sectionChips(entry) : templateChips(entry))
      )
    );
  }

  /** The leaf renderer — the ONLY thing `state.grid` changes. */
  function entryList(kind, members) {
    if (state.grid) {
      return el(
        "div",
        { class: "mrln-lib-cards" },
        members.map((entry) => entryCard(kind, entry))
      );
    }
    const itemEl = kind === "sections" ? sectionLi : templateLi;
    return el("ul", { class: "mrln-tree" }, members.map(itemEl));
  }

  function groupedTree(kind, entries) {
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
              // Setting the open attribute QUEUES a toggle event, which lands
              // after this listener is attached — so a filter-forced open
              // would record itself as user-opened and leave every matching
              // group expanded once the filter is cleared.
              if (filter) return;
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
          entryList(kind, members)
        )
      );
    }
    return nodes;
  }

  function treeBlock(kind, title, entries) {
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
          // same queued-toggle trap as groupedTree — and here a filter-forced
          // open also self-marks ':touched', permanently disabling the
          // 'sections default open' rule above
          if ((state.libFilter ?? "").trim()) return;
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
      ...groupedTree(kind, entries)
    );
  }

  function viewToggle() {
    return el(
      "button",
      {
        class: `mrln-btn${state.grid ? " mrln-toggled" : ""}`,
        title: state.grid
          ? "Back to the compact row list"
          : "Browse as thumbnail cards — rows without a thumbnail show their domain glyph",
        onclick: () => {
          state.grid = !state.grid;
          renderLibraryTab();
        },
      },
      state.grid ? "☰ Rows" : "▦ Cards"
    );
  }

  function renderLibraryTab() {
    if (!state.library) {
      mount(libraryTab, 
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
        // the re-render replaces this input — restore focus AND the exact
        // caret position, or typing anywhere but the end is impossible
        const caret = e.target.selectionStart ?? e.target.value.length;
        state.libFilter = e.target.value;
        renderLibraryTab();
        const fresh = libraryTab.querySelector(".mrln-lib-filter");
        if (fresh) {
          fresh.focus();
          fresh.setSelectionRange(caret, caret);
        }
      },
    });
    mount(libraryTab, 
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
        el(
          "button",
          { class: "mrln-btn", onclick: (e) => busy(e.currentTarget, newTemplate) },
          "New template…"
        ),
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
        el("button", { class: "mrln-btn", onclick: () => loadLibrary() }, "Reload"),
        viewToggle()
      ),
      filterInput,
      treeBlock("sections", "Sections", lib.sections),
      treeBlock("templates", "Templates", lib.templates),
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
      el(
        "button",
        { class: "mrln-btn mrln-primary", onclick: (e) => busy(e.currentTarget, save) },
        "Save to user tier"
      ),
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

  return {
    confirmReplaceEditor,
    deleteEntry,
    editorBox,
    editorCloseBtn,
    markEditorClean,
    renderLibraryTab,
    setEditor,
  };
}
