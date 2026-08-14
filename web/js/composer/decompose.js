// MRLN Prompt Composer — the De-compose tab: paste a finished prompt, map its
// fragments against the library (programmatic / llm / hybrid engines
// server-side), decide what happens to every fragment, save the result as a
// template plus the new sections/items it needs.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). The persistent
// progress row lives inside createDecompose().
import { defaultPlan, jsSlugify, uniqueName } from "./util.js";
import { armDestructive, autoArea, busy, el, field, loadingNote, mount, titled } from "./dom.js";
// Model-dropdown value encoding — the SAME builder and sentinels the Enhance
// node's combo uses (composer/api.js), so the two dropdowns cannot drift apart.
// Pure exports only; the fetching instance arrives as ctx.api.
import {
  CUSTOM_ENTRY,
  PULL_PREFIX,
  buildModelValues,
  isNoteEntry,
  isPullEntry,
} from "./api.js";

export function createDecompose(hub) {
  const { ctx, state, decomposeTab } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const askString = (...a) => hub.askString(...a);
  const libraryErrorNote = (...a) => hub.libraryErrorNote(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const sectionSelect = (...a) => hub.sectionSelect(...a);
  const selectTemplate = (...a) => hub.selectTemplate(...a);
  const switchTab = (...a) => hub.switchTab(...a);
  // composer/intake.js — the image → template card mounted above this tab's
  // text area, plus the two things this tab has to ask it: which path the user
  // is looking at (path A uses no LLM, so the engine controls are dead for it)
  // and what an intake-fed decomposition must not lose on the way to a file.
  const renderIntakeCard = (...a) => hub.renderIntakeCard(...a);
  const intakePath = (...a) => hub.intakePath(...a);
  const setIntakePath = (...a) => hub.setIntakePath(...a);
  const intakeCarry = (...a) => hub.intakeCarry(...a);
  const clearIntakeCarry = (...a) => hub.clearIntakeCarry(...a);

  // ---- de-compose tab ------------------------------------------------------
  // Paste a finished prompt, map every fragment against the library
  // (heuristic engine server-side; an Ollama/LLM engine plugs into the same
  // endpoint later), resolve the residue, store the result as a template.

  // model name -> already being watched. The Enhance node's twin watcher
  // (prompt_composer.js) learned this the hard way: the server answers an
  // already-running pull with 200, so without the guard every re-pick of the
  // same model added another 4 s poller and another completion toast.
  const activePulls = new Set();

  function watchDecomposePull(model) {
    if (activePulls.has(model)) return;
    activePulls.add(model);
    const started = Date.now();
    // every exit runs through stop(), so a finished pull is watchable again —
    // a guard that outlives its watcher would make the model un-re-pickable
    const stop = (severity, summary, detail = "") => {
      activePulls.delete(model);
      ctx.toast(severity, summary, detail);
    };
    const tick = async () => {
      if (Date.now() - started > 45 * 60 * 1000) {
        // a watcher that ends without a word leaves the user guessing whether
        // a multi-GB pull ever finished
        stop(
          "info",
          "Stopped watching the pull",
          `${model} — still running after 45 min. Ollama keeps downloading in the `
            + "background; check with `ollama list`."
        );
        return;
      }
      let body = null;
      try {
        body = await ctx.api.pullStatus(model);
      } catch {
        /* transient — keep polling */
      }
      if (body?.status === "done") {
        stop("success", "Model pulled", `${model} is installed`);
        if (state.tab === "decompose") renderDecomposeTab(); // list shows it now
        return;
      }
      if (body?.status === "error") {
        stop("error", `Pull failed: ${model}`, body.detail ?? "");
        return;
      }
      setTimeout(tick, 4000);
    };
    setTimeout(tick, 4000);
  }

  // Persistent progress row: renderDecomposeTab rebuilds the whole tab, so a
  // spinner mounted into the render would vanish on the first re-render.
  const decomposeBusyText = el("span", {}, " De-composing…");
  const decomposeBusy = el(
    "div",
    { class: "mrln-note mrln-loading", style: "display:none" },
    el("span", { class: "mrln-spinner" }),
    decomposeBusyText
  );

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
    }
    // A 4-second toast is not feedback for a two-minute call: hold a spinner
    // for the whole run, keep the button disabled across re-renders (d.running)
    // and let only the NEWEST run write d.report/d.plans (d.runNo).
    const no = ++d.runNo;
    d.running = true;
    // This run is of the PASTED text, so whatever an image intake asked to
    // carry into the saved template (the extracted negative) no longer belongs
    // to it — dropping it here is what keeps the two sources from crossing.
    clearIntakeCarry();
    decomposeBusyText.textContent =
      engine === "programmatic"
        ? " De-composing…"
        : ` De-composing… ${engine} engine via ${body.backend} — up to 2 minutes`;
    decomposeBusy.style.display = "";
    let report;
    try {
      report = await ctx.apiJson("/mrln/prompt/decompose", { method: "POST", body });
    } catch (err) {
      if (no === d.runNo) ctx.toast("error", "Decompose failed", err.message);
      return;
    } finally {
      if (no === d.runNo) {
        d.running = false;
        decomposeBusy.style.display = "none";
      }
    }
    if (no !== d.runNo) return; // a newer run owns the tab
    d.report = report;
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
        mount(conditional, 
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
      await armDestructive(button); // second click — run the armed overwrite
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
    const slotForItem = new Map(); // new item -> the slot whose default names it
    const dropped = []; // fragments whose target section was never chosen
    const usedIds = new Set();
    const slotId = (base) => {
      const id = uniqueName(base, usedIds);
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
        if (!section) {
          dropped.push(fragment.text); // no section picked — say so, don't vanish
          return;
        }
        const items = newItemsBySection.get(section) ?? [];
        const name = uniqueName(
          jsSlugify(fragment.suggested_name || fragment.text),
          new Set(items.map((item) => item.name))
        );
        const item = { name, text: prose };
        if (fragment.short) item.text_short = fragment.short;
        items.push(item);
        newItemsBySection.set(section, items);
        const slot = { id: slotId(section.split("/").pop()), ref: section, default: name };
        slots.push(slot);
        slotForItem.set(item, slot);
      }
    });
    if (dropped.length) {
      ctx.toast(
        "warn",
        `${dropped.length} fragment(s) dropped`,
        `No target section was chosen for: ${dropped
          .map((text) => `'${text.slice(0, 40)}'`)
          .join(", ")}`
      );
    }
    if (!slots.length) {
      ctx.toast("warn", "No slots", "Nothing is mapped to a section — template would be empty.");
      return;
    }
    // 1) new items land first (extend files for factory sections, appends
    //    for user sections, fresh files for new slugs)
    const written = [];
    for (const [section, items] of newItemsBySection) {
      let data = { version: 1, items };
      try {
        const existing = state.library.sections.find((s) => s.slug === section);
        if (existing) {
          const body = await ctx.apiJson(
            `/mrln/prompt/section?slug=${encodeURIComponent(section)}`
          );
          if (body.tier === "user") {
            // Uniquify against the section's EXISTING names too: the server
            // rejects a file with duplicate item names, which used to abort
            // this loop after earlier sections were already written.
            const taken = new Set((body.raw.items ?? []).map((item) => item.name));
            for (const item of items) {
              const fixed = uniqueName(item.name, taken);
              taken.add(fixed);
              if (fixed !== item.name) {
                item.name = fixed;
                const slot = slotForItem.get(item);
                if (slot) slot.default = fixed; // the template must name the item it saved
              }
            }
            data = { ...body.raw, items: [...(body.raw.items ?? []), ...items] };
          }
        } else if (type.length) {
          data.suits = type; // brand-new section inherits the template type
        }
        await ctx.apiJson("/mrln/prompt/save-section", {
          method: "POST",
          body: { slug: section, data },
        });
        written.push(section);
      } catch (err) {
        ctx.toast(
          "error",
          `Cannot save items into '${section}'`,
          err.message
            + (written.length
              ? ` — already written: ${written.join(", ")} (a retry re-appends those)`
              : "")
        );
        return;
      }
    }
    // 2) then the template that wires them together
    const data = { version: 1, slots };
    if (type.length) data.type = type;
    if (prefixParts.length) data.prefix = prefixParts.join("\n");
    if (suffixParts.length) data.suffix = suffixParts.join("\n");
    // /extract-apply forwards the POSITIVE prompt only (intake.py's allowlist),
    // so a negative that WAS in the image would otherwise be read, shown, and
    // then silently dropped between the two tabs. It is the one thing the
    // fragment cards cannot represent, so it rides straight into the file.
    const carried = intakeCarry();
    if (carried?.negative) data.negative = carried.negative;
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
      mount(decomposeTab, 
        state.libraryError
          ? libraryErrorNote(state.libraryError)
          : loadingNote("Loading prompt library…")
      );
      return;
    }
    const d = state.decompose;
    // Path A (verbatim image intake) consults no LLM at all — it is the path
    // that works with every backend unset. So while it is what the user is
    // looking at, these three controls are DISABLED rather than hidden: a
    // hidden control teaches nothing, a greyed one with a reason does.
    const verbatimLock = intakePath() === "verbatim";
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
      // short enough to survive the value column; the row's tooltip carries
      // what it means
      placeholder: "e.g. object, car",
      title: "Restricts matching to sections suiting these classifiers (plus universal ones)",
      oninput: (e) => {
        d.type = e.target.value;
      },
    });
    const engineSelect = el("select", {
      disabled: verbatimLock ? "" : null,
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
      disabled: verbatimLock ? "" : null,
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
      disabled: verbatimLock ? "" : null,
      title: "Model for the llm/hybrid engines. Locals list installed models; "
        + "'⬇ pull' entries download via Ollama when picked; clouds fall back "
        + "to a sensible default when empty.",
      onchange: async (e) => {
        const value = e.target.value;
        if (isNoteEntry(value)) {
          e.target.value = (d.model ?? "").trim(); // the ⚠ row is a message, not a model
          return;
        }
        if (value === CUSTOM_ENTRY) {
          const typed = await askString("Model name", "Exact model tag/id:", d.model ?? "");
          if (typed?.trim()) d.model = typed.trim();
          renderDecomposeTab();
          return;
        }
        if (isPullEntry(value)) {
          const model = value.slice(PULL_PREFIX.length);
          d.model = model; // set now — the pull lands in the background
          try {
            await ctx.api.startPull(model);
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
      // Supersede token: this tab re-renders on every engine/backend change,
      // and a slow response for the PREVIOUS backend must not write the shared
      // d.model (an Ollama tag posted to a cloud backend) behind the new one.
      const gen = ++d.modelGen;
      mount(modelSelect, el("option", { value: current }, current || "…"));
      // shared 30 s cache + in-flight dedup (composer/api.js): this ran on
      // EVERY render, and each local-backend probe blocks a server executor
      // thread for up to 5 s when the backend is down
      const entry = await ctx.api.llmModels(provider);
      if (gen !== d.modelGen) return; // the user switched backends mid-flight
      const models = entry.models ?? [];
      const isCloud = entry.keySet !== null; // llm-validate answers clouds offline
      if (entry.error) {
        modelNote.textContent = `✗ ${entry.error}`;
        modelNote.style.color = "#e88";
      } else if (isCloud) {
        modelNote.textContent = entry.keySet
          ? "✓ key stored"
          : "no key stored — add it in the Settings tab";
        modelNote.style.color = entry.keySet ? "#6ca" : "#e88";
      } else {
        modelNote.textContent = `✓ ${models.length} installed`;
        modelNote.style.color = "#6ca";
      }
      // one shared value encoding (⬇ pull … / ✏ custom… / ⚠ note) instead of
      // this tab's own __pull__:/__custom__ sentinels
      mount(modelSelect, 
        ...(isCloud ? [el("option", { value: "" }, "(backend default)")] : []),
        ...buildModelValues({ provider, current, entry }).map((value) =>
          el("option", { value }, value)
        )
      );
      if (!current && !isCloud && models.length) d.model = models[0]; // ollama needs one
      modelSelect.value = (d.model ?? "").trim();
      if (modelSelect.value !== (d.model ?? "").trim()) {
        modelSelect.value = isCloud ? "" : (models[0] ?? "");
      }
    })();
    const parts = [
      // composer/intake.js: drop an image, see what it carried, pick a path.
      // It sits ABOVE the text area on purpose — an extraction is an input to
      // this tab, and its two buttons are the first fork in the road.
      renderIntakeCard(),
      el("hr", { class: "mrln-sep" }),
      el(
        "div",
        { class: "mrln-note" },
        "Map a pasted prompt onto your library. The programmatic matcher is "
          + "offline and deterministic; llm/hybrid ask a configured backend "
          + "and validate every assignment against the real library."
      ),
      field("Prompt to decompose", promptArea),
      // One form, one label column, one right edge for the values — the four
      // settings used to be three different geometries stacked (a 108px label
      // row, a two-up grid, then a full-width row), which is what made this
      // tab read as clutter next to Compose.
      el(
        "div",
        { class: "mrln-grid2 pc-form" },
        titled(
          "Type",
          typeInput,
          "Template classifiers, e.g. object, car — they filter which sections "
            + "a fragment may be matched against. Empty means untyped."
        ),
        titled(
          "Engine",
          engineSelect,
          "programmatic is offline and deterministic; llm and hybrid ask a "
            + "configured backend and validate every assignment against the "
            + "real library."
        ),
        titled(
          "Backend",
          (d.engine ?? "programmatic") === "programmatic"
            ? el("span", { class: "mrln-note" }, "—")
            : backendSelect,
          "Which LLM answers. Only used by the llm and hybrid engines."
        ),
        (d.engine ?? "programmatic") === "programmatic"
          ? null
          : titled(
              "Model",
              el("div", { class: "mrln-inline" }, modelSelect, modelNote),
              "The model this backend runs. The list comes from the backend "
                + "itself — configure the URLs in the Settings tab."
            )
      ),
      verbatimLock
        ? el(
            "div",
            { class: "mrln-note mrln-inline" },
            el(
              "span",
              {},
              "Disabled: the image intake above is on the 'Use as-is' path, which "
                + "reproduces the found prompt with no LLM at all. These apply to "
                + "'Decompose' only."
            ),
            // the way out that does NOT run anything: without it the controls
            // needed to configure path B would be locked by path A
            el(
              "button",
              {
                class: "mrln-btn mrln-mini",
                title: "re-enable them — nothing is run",
                onclick: () => setIntakePath("decompose"),
              },
              "Enable"
            )
          )
        : null,
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: "mrln-btn mrln-primary",
            disabled: d.running ? "" : null,
            onclick: (e) => busy(e.currentTarget, runDecompose),
          },
          d.running ? "De-composing…" : "Decompose"
        )
      ),
      decomposeBusy,
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
              onclick: (e) => {
                const button = e.currentTarget; // cleared once the handler returns
                return busy(button, () => saveDecomposedTemplate(button));
              },
            },
            "Create template…"
          )
        )
      );
    }
    mount(decomposeTab, ...parts);
  }

  return { renderDecomposeTab };
}
