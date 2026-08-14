// MRLN Prompt Composer — shareable bundles: export a template/section (with
// its user-tier dependencies embedded) and import one after showing the exact
// dry-run write/skip plan.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js).
import { bundleFilename } from "./util.js";
import { busy, el, field, loadingNote, mount } from "./dom.js";

export function createBundles(hub) {
  const { ctx } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const editorCloseBtn = (...a) => hub.editorCloseBtn(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const selectTemplate = (...a) => hub.selectTemplate(...a);
  const setEditor = (...a) => hub.setEditor(...a);
  const switchTab = (...a) => hub.switchTab(...a);
  const warnMissingLoras = (...a) => hub.warnMissingLoras(...a);

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
        mount(planList, ...importPlanLines(fresh));
        errorLine.textContent = "";
      } catch (err) {
        errorLine.textContent = err.message;
      }
    }
    slugInput.addEventListener("change", replan);
    overwriteBox.addEventListener("change", replan);

    // NOT `busy`: this module imports dom.js's busy(), and a local of that
    // name shadows it for the whole function.
    let importing = false;
    const importButton = el(
      "button",
      {
        class: "mrln-btn mrln-primary",
        onclick: async () => {
          if (importing) return;
          importing = true;
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
            importing = false;
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


  // ---- migration imports ---------------------------------------------------
  // Three sources that all answer the SAME plan shape as a bundle, which is
  // why they share this card and importPlanLines(): a wildcard folder or the
  // .zip a pack ships as, an A1111 styles.csv, and a Civitai 'Wildcards' model
  // (796 of them, all .zip). Dry-run first, always — these write user files.

  const MIGRATION_SOURCES = [
    {
      id: "wildcards",
      label: "Wildcard folder or .zip",
      route: "/mrln/prompt/import-wildcards",
      key: "path",
      placeholder: "D:\stable-diffusion\wildcards   or   …\pack.zip",
      hint:
        "A folder of .txt/.yaml wildcards, or the .zip a published pack ships as. "
        + "Lands in user sections under 'wildcards/…'; weighted lines (3::rare) are kept.",
    },
    {
      id: "styles",
      label: "A1111 styles.csv",
      route: "/mrln/prompt/import-styles",
      key: "path",
      placeholder: "D:\stable-diffusion-webui\styles.csv",
      hint:
        "Rows containing {prompt} become templates; the rest become items in "
        + "'styles/a1111'.",
    },
    {
      id: "civitai",
      label: "Civitai wildcard pack",
      route: "/mrln/prompt/import-civitai-wildcards",
      key: "url",
      placeholder: "https://civitai.com/models/615967",
      hint:
        "Paste the link of a Civitai model of type 'Wildcards' (or its id). The pack "
        + "is downloaded, hash-checked and planned — the creator's licence is shown "
        + "before anything is written.",
    },
  ];

  function warningLines(plan) {
    const warnings = plan.warnings ?? [];
    if (!warnings.length) return [];
    return [
      el("div", { class: "mrln-field-name" }, `Notes (${warnings.length})`),
      el(
        "ul",
        { class: "mrln-import-plan" },
        warnings.slice(0, 40).map((text) => el("li", {}, text))
      ),
    ];
  }

  function creditLine(plan) {
    // A Civitai import names who made the pack. Nothing else in the panel
    // knows this, and a licence the user only sees once is worth repeating.
    const info = plan.civitai;
    if (!info) return null;
    return el(
      "div",
      { class: "mrln-note" },
      `${info.model} `,
      info.version ? el("span", { class: "mrln-chip" }, info.version) : null,
      info.creator ? ` by ${info.creator} — ` : " — ",
      el("span", {}, info.licence?.summary ?? "")
    );
  }

  function migrationImportPicker() {
    const select = el("select", {});
    for (const source of MIGRATION_SOURCES) {
      select.append(el("option", { value: source.id }, source.label));
    }
    const input = el("input", { type: "text" });
    const hint = el("div", { class: "mrln-note" });
    const errorLine = el("div", { class: "mrln-error" });
    const planBox = el("div", {});
    const overwriteBox = el("input", { type: "checkbox" });
    let plan = null;

    const current = () => MIGRATION_SOURCES.find((s) => s.id === select.value) ?? MIGRATION_SOURCES[0];
    const syncSource = () => {
      const source = current();
      input.setAttribute("placeholder", source.placeholder);
      hint.textContent = source.hint;
      plan = null;
      mount(planBox);
      errorLine.textContent = "";
    };
    select.addEventListener("change", syncSource);
    syncSource();

    function body(dryRun) {
      const source = current();
      return { [source.key]: input.value.trim(), overwrite: overwriteBox.checked, dry_run: dryRun };
    }

    async function preview() {
      const source = current();
      if (!input.value.trim()) {
        errorLine.textContent = "nothing to import yet — fill the field above";
        return;
      }
      errorLine.textContent = "";
      mount(planBox, loadingNote("Reading the source…"));
      try {
        plan = await ctx.apiJson(source.route, { method: "POST", body: body(true) });
      } catch (err) {
        plan = null;
        mount(planBox);
        errorLine.textContent = [err.message, err.remediation].filter(Boolean).join(" — ");
        return;
      }
      mount(
        planBox,
        creditLine(plan),
        el("ul", { class: "mrln-import-plan" }, importPlanLines(plan)),
        ...warningLines(plan)
      );
    }

    async function apply(button) {
      const source = current();
      if (!plan) {
        errorLine.textContent = "preview it first — this writes files";
        return;
      }
      try {
        const report = await ctx.apiJson(source.route, { method: "POST", body: body(false) });
        const written = (report.written ?? []).length;
        const kept = (report.skipped ?? []).length;
        ctx.toast(
          "success",
          "Import complete",
          `${written} file(s) written${kept ? `, ${kept} kept — tick overwrite to replace them` : ""}`
        );
        setEditor();
        ctx.refreshCombos();
        await loadLibrary();
      } catch (err) {
        errorLine.textContent = [err.message, err.remediation].filter(Boolean).join(" — ");
      }
      void button;
    }

    setEditor(
      el("div", { class: "mrln-tree-head" }, "Import from another tool", editorCloseBtn()),
      field("Source", select),
      hint,
      field("Folder, file or link", input),
      // The overwrite switch is a DECISION about what this button is going to
      // do, so it gets room of its own and says what it means — stacked
      // directly on the actions it changes, it read as one dense block and the
      // consequence of ticking it was easy to miss.
      el(
        "label",
        { class: "mrln-check pc-danger-check" },
        overwriteBox,
        el("span", {}, " overwrite files I already have"),
        el(
          "span",
          { class: "mrln-note" },
          "off: existing files are kept and reported as skipped"
        )
      ),
      el(
        "div",
        { class: "mrln-actions pc-import-actions" },
        el("button", { class: "mrln-btn", onclick: (e) => busy(e.currentTarget, preview) }, "Preview"),
        el(
          "button",
          {
            class: "mrln-btn mrln-primary",
            onclick: (e) => busy(e.currentTarget, () => apply(e.currentTarget)),
          },
          "Import"
        )
      ),
      errorLine,
      planBox
    );
  }

  return { exportBtn, importBundlePicker, migrationImportPicker };
}
