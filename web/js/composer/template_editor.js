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

  return { openTemplateEditor };
}
