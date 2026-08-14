// MRLN Prompt Composer — the template editor: raw template JSON, validated by
// the server on save. The compose tab is the structured editor; this is the
// escape hatch for everything the compose tab does not surface.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js).
import { busy, el, field, tierChip } from "./dom.js";

export function createTemplateEditor(hub) {
  const { ctx, state } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const deleteEntry = (...a) => hub.deleteEntry(...a);
  const editorCloseBtn = (...a) => hub.editorCloseBtn(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const markEditorClean = (...a) => hub.markEditorClean(...a);
  const selectTemplate = (...a) => hub.selectTemplate(...a);
  const setEditor = (...a) => hub.setEditor(...a);
  const thumbControls = (...a) => hub.thumbControls(...a);

  /**
   * The tier pill, and a switch when the slug has a file in BOTH tiers — the
   * same one the Compose tab and the section editor carry. A user file shadows
   * the factory file everywhere, so this is the only way to read what you are
   * shadowing before deciding your version is the better one.
   */
  function tierSwitch(slug, body) {
    const tiers = body.tiers ?? [];
    const showing = body.viewing ?? body.tier;
    if (tiers.length < 2) return tierChip(body.tier);
    const other = showing === "factory" ? "user" : "factory";
    return el(
      "button",
      {
        class: `mrln-chip mrln-chip-btn mrln-${showing}`,
        title: `Showing the ${showing} file — click to read the ${other} one. `
          + "Your file wins every render either way.",
        onclick: (e) => busy(e.currentTarget, () => openTemplateEditor(slug, other)),
      },
      showing
    );
  }

  async function openTemplateEditor(slug, tier = "") {
    let body;
    try {
      // `tier` reads ONE tier's own file. The editor edits what it is SHOWN,
      // so the JSON below is that tier's — and saving it writes the user tier,
      // which is why the factory view says so out loud.
      const query = tier ? `&tier=${encodeURIComponent(tier)}` : "";
      body = await ctx.apiJson(`/mrln/prompt/template?slug=${encodeURIComponent(slug)}${query}`);
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
      markEditorClean(); // the editor now matches disk
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
      el(
        "button",
        { class: "mrln-btn mrln-primary", onclick: (e) => busy(e.currentTarget, save) },
        "Save to user library"
      ),
    ];
    if (body.tier === "user") {
      actions.push(
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: (e) => busy(e.currentTarget, () => deleteEntry("templates", slug)),
          },
          "Delete user file"
        )
      );
    }
    const viewingFactoryUnderMine = body.viewing === "factory" && body.tier === "user";
    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        `Template: ${slug}`,
        tierSwitch(slug, body),
        editorCloseBtn()
      ),
      body.tier === "factory"
        ? el("div", { class: "mrln-note" }, "Factory file — saving creates a user-tier override.")
        : null,
      // The one genuinely destructive combination: this JSON is the factory's,
      // and Save writes the user tier — which replaces your own version with
      // what you are reading.
      viewingFactoryUnderMine
        ? el(
            "div",
            { class: "mrln-note pc-tier-note" },
            el("span", { class: "pc-flag" }, "● reading the factory file"),
            " — your version still wins every render, and Save here would "
              + "REPLACE it with this one."
          )
        : null,
      // The thumbnail is the template's face in the browse grid, and it is
      // independent of the JSON below: setting one writes an image file, never
      // this template. thumbControls owns the whole set/reset interaction.
      thumbControls("templates", slug, {}),
      field("Slug", slugInput),
      el("span", { class: "mrln-field-name" }, "Template JSON (validated on save)"),
      textarea,
      errorLine,
      el("div", { class: "mrln-actions" }, ...actions)
    );
  }

  return { openTemplateEditor };
}
