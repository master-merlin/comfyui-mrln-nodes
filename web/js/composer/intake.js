// MRLN Prompt Composer — image → template intake: drop a generated image (or
// paste its civitai.com URL), see what metadata it actually carried, then pick
// ONE of the two paths the server offers. This module owns the card; the
// De-compose tab mounts it above its own text box.
//
// THE TWO PATHS ARE THE POINT (promptapi/intake.py, SPEC §4.1). Extraction is
// identical for both; only what happens next differs, and the user decides:
//   A · "Use as-is"  → POST /extract-apply {path:"verbatim"} — the found prompt
//                      becomes a template that renders it back byte for byte.
//                      No LLM anywhere on this path, so it works with every
//                      backend unset. That is why the engine/backend/model
//                      selects below are DISABLED (not hidden) while this path
//                      is what the user is looking at: the reason has to be
//                      legible, not invisible.
//   B · "Decompose"  → POST /extract-apply {path:"decompose"} — the response IS
//                      handle_decompose's own report, so it feeds straight into
//                      state.decompose and the EXISTING fragment cards render
//                      it. No second de-composer lives here.
// An ambiguous ComfyUI graph gets a candidate picker BEFORE either path. The
// server refuses to guess which string is the positive; so does this.
//
// THE PAYLOAD CAP SHAPES THE UPLOAD. The route body cap is 1 MiB, so the
// decoded image cap is 700 KiB and a full 1024² PNG does not fit. composer/
// image.js owns that problem (rebuilt PNG carrying only text chunks, JPEG head
// slice, WebP whole-or-refuse); this module only ever sends what it hands back,
// and surfaces its {error, remediation} instead of POSTing anyway to collect a
// 413.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). Everything below is a
// declaration; every mutable thing lives inside createIntake() or state.intake.
import { armDestructive, busy, el, field, loadingNote } from "./dom.js";
import { defaultPlan, jsSlugify } from "./util.js";
import { dataUrl, metadataPayload, mimeFor, readForMetadata, wireDropZone } from "./image.js";

// ---- pure helpers ----------------------------------------------------------
// Everything derivable from a server payload without a DOM lives here so it can
// be unit-tested under node (tests/js/intake.test.mjs).

export const EXTRACT_IMAGE_ROUTE = "/mrln/prompt/extract-image";
export const EXTRACT_APPLY_ROUTE = "/mrln/prompt/extract-apply";

/** Parameter keys whose value is a JSON blob already surfaced as LoRAs. */
export const PARAM_BLOB_KEYS = ["civitai resources", "civitai metadata"];

/**
 * The dialect label for an extraction's `source` — the server states WHERE it
 * read from ("parameters", "exif-usercomment", "comfy-prompt",
 * "comfy-workflow", "civitai-api") and the user deserves that in words.
 */
export function sourceLabel(source) {
  const known = {
    parameters: "PNG text chunk 'parameters' — A1111 / Forge / Civitai dialect",
    "exif-usercomment": "EXIF UserComment — the same A1111 text in a JPEG/WebP",
    "comfy-prompt": "ComfyUI API-format graph (the 'prompt' chunk)",
    "comfy-workflow": "ComfyUI UI-format graph (the 'workflow' chunk)",
    "civitai-api": "civitai.com image record",
  };
  const key = String(source ?? "");
  return known[key] || (key ? `source: ${key}` : "unknown source");
}

/**
 * "installed" | "missing" | "unknown" for one extracted LoRA.
 *
 * The three states are the server's own and the difference matters:
 * `file: null` = there was no ComfyUI to ask, `file: ""` = asked and the file
 * is not installed here, a name = installed. Collapsing null into "missing"
 * would accuse a headless dev server of a broken library.
 */
export function loraStatus(entry) {
  const file = entry?.file;
  if (file === null || file === undefined) return "unknown";
  return String(file) ? "installed" : "missing";
}

/**
 * The generation settings as [key, value] display rows, in the order the
 * generator wrote them (Steps, Sampler, CFG… is meaningful) minus the JSON
 * blobs that are already rendered as LoRAs — a 2 KB `Civitai resources` array
 * pasted into a settings grid is noise, not information.
 */
export function paramRows(params) {
  const rows = [];
  for (const [key, value] of Object.entries(params ?? {})) {
    if (PARAM_BLOB_KEYS.includes(String(key).toLowerCase())) continue;
    if (value === null || value === undefined || value === "") continue;
    rows.push([String(key), String(value)]);
  }
  return rows;
}

/** A candidate's one-line identity: which node in the graph it came from. */
export function candidateLabel(candidate, index) {
  const role = String(candidate?.role || "unknown");
  const node = String(candidate?.node ?? "");
  const cls = String(candidate?.class_type ?? "");
  const where = [node ? `#${node}` : "", cls].filter(Boolean).join(" ");
  return `${index + 1}. ${role}${where ? ` · ${where}` : ""}`;
}

/**
 * True when the user has to resolve the graph before either path can run.
 *
 * `ambiguous` alone is not enough: extraction_from_candidates also sets it for
 * a graph that yielded NO candidates at all, and a picker with nothing to pick
 * is a dead end — that case is an error message, not a question.
 */
export function needsCandidatePicker(extraction) {
  return Boolean(extraction?.ambiguous) && (extraction?.candidates?.length ?? 0) > 0;
}

/**
 * The picker's starting selection: only ever what the SERVER already resolved
 * (extraction_from_candidates fills positive/negative when exactly one
 * candidate carries that role), never a guess at the ambiguous half. -1 = no
 * selection, and the Use-this-pick button stays disabled until a positive has
 * one.
 */
export function defaultCandidatePick(extraction) {
  const candidates = extraction?.candidates ?? [];
  const find = (text) =>
    text ? candidates.findIndex((c) => String(c?.text ?? "") === text) : -1;
  return {
    positive: find(String(extraction?.positive ?? "")),
    negative: find(String(extraction?.negative ?? "")),
  };
}

/**
 * The extraction with the user's candidate choice baked in. Returns the input
 * unchanged when no positive was chosen — resolving without one is exactly the
 * silent guess this picker exists to prevent.
 *
 * The candidate texts are RAW: the server's `_finish` pass (which pulls inline
 * `<lora:…>` tags out of a prompt) only ever ran on the fields it resolved
 * itself. A hand-picked candidate therefore keeps its tags, which is why the
 * card warns about them — see hasInlineLoraTag.
 */
export function applyCandidatePick(extraction, positiveIndex, negativeIndex) {
  const candidates = extraction?.candidates ?? [];
  const positive = candidates[positiveIndex];
  if (!positive) return extraction;
  const negative = negativeIndex >= 0 ? candidates[negativeIndex] : null;
  return {
    ...extraction,
    positive: String(positive.text ?? ""),
    negative: negative ? String(negative.text ?? "") : "",
    ambiguous: false,
    picked: { positive: positiveIndex, negative: negative ? negativeIndex : -1 },
    notes: [
      ...(extraction?.notes ?? []),
      `positive${negative ? " and negative" : ""} picked by hand out of `
        + `${candidates.length} candidate(s); nothing was guessed`,
    ],
  };
}

/** Detection only — the server owns every dialect. Used to warn, never parse. */
export function hasInlineLoraTag(text) {
  return /<(?:lora|lyco|lycoris)\s*:/i.test(String(text ?? ""));
}

/** POST body for /extract-image. `url` wins, mirroring the server's own order. */
export function extractImageBody({ image = "", url = "", resolve = false } = {}) {
  const body = {};
  const clean = String(url ?? "").trim();
  if (clean) body.url = clean;
  else body.image = String(image ?? "");
  if (resolve) body.resolve = true;
  return body;
}

/**
 * The extraction as /extract-apply reads it — exactly `_extraction_arg`'s
 * allowlist. NOT a trim for tidiness: the apply call rides the same 1 MiB body
 * cap, and `candidates`/`resources` (unread by both paths) can carry every
 * prompt string of a large ComfyUI graph. Values are copied verbatim, so what
 * the server saw is what it gets back.
 */
export function applyExtraction(extraction) {
  const source = extraction ?? {};
  return {
    positive: String(source.positive ?? ""),
    negative: String(source.negative ?? ""),
    params: source.params && typeof source.params === "object" ? source.params : {},
    loras: Array.isArray(source.loras) ? source.loras : [],
    source: String(source.source ?? ""),
  };
}

/**
 * POST body for /extract-apply.
 *
 * path 'decompose' forwards the de-composer's own knobs and NOTHING else —
 * slug/label/save belong to path A, and sending them on B would only describe
 * an intent the handler cannot honour. backend/model/timeout ride along only
 * for a non-programmatic engine, exactly as decompose.js's own runDecompose
 * does, so the two call sites cannot drift.
 */
export function extractApplyBody(options = {}) {
  // destructured in the body, not the signature: a multi-line parameter list
  // would put a non-declaration line at column 0, which the module-hygiene
  // guard (tests/js/composer_modules.test.mjs) rejects on sight
  const { path = "verbatim", extraction = null, slug = "", label = "" } = options;
  const { save = false, decompose = null } = options;
  const body = { path, extraction: applyExtraction(extraction) };
  if (path === "decompose") {
    const knobs = decompose ?? {};
    const engine = knobs.engine || "programmatic";
    body.type = String(knobs.type ?? "");
    body.engine = engine;
    if (engine !== "programmatic") {
      body.backend = knobs.backend || "ollama";
      body.model = String(knobs.model ?? "");
      body.timeout = knobs.timeout ?? 120;
    }
    return body;
  }
  const clean = String(slug ?? "").trim();
  if (clean) body.slug = clean;
  if (label) body.label = String(label);
  if (save) body.save = true;
  return body;
}

/**
 * One display string out of an apiJson failure. `.remediation` is the whole
 * point of the server's IntakeError — dropping it would turn "paste the URL
 * instead" into "413".
 */
export function intakeErrorText(err) {
  const message = String(err?.message ?? err ?? "unknown error").trim();
  const remediation = String(err?.remediation ?? "").trim();
  if (remediation) return `${message} — ${remediation}`;
  if (err?.status === 413) {
    return `${message} — paste the image's civitai.com URL instead, or drop the `
      + "PNG/JPEG the generator wrote";
  }
  return message;
}

/** A prefilled slug the user can accept or overwrite in the save dialog. */
export function defaultIntakeSlug(extraction) {
  const head = String(extraction?.positive ?? "").split(/[,\n]/)[0] ?? "";
  return `intake/${jsSlugify(head, 32)}`;
}

/**
 * What to tell the user after a path-A apply. `verbatim: false` is not a note:
 * path A's entire contract is byte-for-byte reproduction, so a false there is a
 * bug report waiting to happen and is surfaced as an error.
 */
export function verbatimResultNotes(body) {
  const out = [];
  if (body?.verbatim === false) {
    out.push({
      kind: "error",
      text:
        "the saved template does NOT render the extracted prompt byte for byte — "
        + "path A must never rewrite a prompt; please report this",
    });
  }
  for (const note of body?.notes ?? []) out.push({ kind: "note", text: String(note) });
  return out;
}

/**
 * Everything path B silently leaves behind, as user-facing lines. The
 * de-composer takes the POSITIVE text and nothing else, so params and LoRAs
 * found in the image do not reach the decomposed template — saying so is the
 * difference between a choice and a surprise.
 */
export function decomposeLossNotes(extraction) {
  const lines = [];
  const params = paramRows(extraction?.params).length;
  const loras = (extraction?.loras ?? []).length;
  if (params) {
    lines.push(
      `${params} generation setting(s) are not carried into a decomposed template `
        + "(Use as-is records them in the template description)"
    );
  }
  if (loras) {
    lines.push(
      `${loras} LoRA(s) are not carried into a decomposed template `
        + "(Use as-is records them as a companion section)"
    );
  }
  return lines;
}

/** LoRAs the server asked about with resolve=true — the re-run affordance. */
export function unresolvedAirCount(extraction) {
  return (extraction?.loras ?? []).filter(
    (entry) => !entry?.air && entry?.model_version_id
  ).length;
}

// ---- the card --------------------------------------------------------------

export function createIntake(hub) {
  const { ctx, state } = hub;
  // late-bound cross-module calls (see composer/state.js for the why): this
  // factory is built BEFORE createDecompose/createStore, so their exports are
  // not on the hub yet at construction time.
  const askString = (...a) => hub.askString(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const renderDecomposeTab = (...a) => hub.renderDecomposeTab(...a);
  const selectTemplate = (...a) => hub.selectTemplate(...a);
  const switchTab = (...a) => hub.switchTab(...a);

  // Card-local state. Deliberately NOT on state.intake: the declared slice is
  // {extraction, source, url, busy, runNo, error} and these are view concerns.
  let pathFocus = ""; // "" | "verbatim" | "decompose" — which path is on screen
  let pick = { positive: -1, negative: -1 }; // candidate picker selection
  let resolveAirs = false; // opt-in Civitai AIR lookup (one request per LoRA)
  let lastBody = null; // the /extract-image body, so "re-run" needs no re-drop
  let applied = null; // the last path-A response, kept for its notes
  let carry = null; // what path B must not lose: {negative}

  // ---- server calls --------------------------------------------------------

  function resetForNewRun(sourceText) {
    const intake = state.intake;
    const runNo = ++intake.runNo; // supersede: a slow drop must never win
    intake.busy = true;
    intake.error = null;
    intake.extraction = null;
    intake.source = sourceText;
    pathFocus = "";
    pick = { positive: -1, negative: -1 };
    applied = null;
    carry = null;
    renderDecomposeTab();
    return runNo;
  }

  function settle(runNo, { error = null, extraction = null }) {
    const intake = state.intake;
    if (runNo !== intake.runNo) return false; // a newer intake owns the card
    intake.busy = false;
    intake.error = error;
    intake.extraction = extraction;
    if (extraction) pick = defaultCandidatePick(extraction);
    renderDecomposeTab();
    return true;
  }

  async function sendExtract(runNo, body) {
    lastBody = body;
    let extraction;
    try {
      extraction = await ctx.apiJson(EXTRACT_IMAGE_ROUTE, { method: "POST", body });
    } catch (err) {
      const text = intakeErrorText(err);
      if (settle(runNo, { error: text })) ctx.toast("error", "No metadata read", text);
      return;
    }
    if (!settle(runNo, { extraction })) return;
    const found = (extraction.positive || "").trim();
    ctx.toast(
      found ? "success" : "warn",
      found ? "Metadata read" : "Nothing usable found",
      found
        ? `${sourceLabel(extraction.source)} — now pick a path`
        : "the image carried metadata but no positive prompt"
    );
  }

  async function intakeFile(file) {
    const runNo = resetForNewRun(file?.name ? `file · ${file.name}` : "dropped image");
    let payload;
    try {
      // image.js decides what is worth sending per format; a raw file would
      // only earn a 413 here
      payload = metadataPayload(await readForMetadata(file));
    } catch (err) {
      const text = intakeErrorText(err);
      if (settle(runNo, { error: text })) ctx.toast("error", "Could not read that file", text);
      return;
    }
    if (runNo !== state.intake.runNo) return;
    if (payload.error) {
      // refused BEFORE the request: posting anyway to collect the server's 413
      // would be slower and say less than image.js already does
      const text = `${payload.error} — ${payload.remediation}`;
      if (settle(runNo, { error: text })) ctx.toast("error", "That file cannot be sent", text);
      return;
    }
    await sendExtract(
      runNo,
      extractImageBody({
        image: dataUrl(payload.bytes, mimeFor(payload.container)),
        resolve: resolveAirs,
      })
    );
  }

  async function intakeUrl(url) {
    const clean = String(url ?? "").trim();
    if (!clean) {
      ctx.toast("warn", "No URL", "Paste a https://civitai.com/images/<id> link first.");
      return;
    }
    state.intake.url = clean;
    const runNo = resetForNewRun(`url · ${clean}`);
    await sendExtract(runNo, extractImageBody({ url: clean, resolve: resolveAirs }));
  }

  async function rerunWithResolve() {
    if (!lastBody) return;
    resolveAirs = true;
    const runNo = resetForNewRun(state.intake.source);
    await sendExtract(runNo, { ...lastBody, resolve: true });
  }

  function clearIntake() {
    const intake = state.intake;
    intake.runNo += 1; // supersede anything still in flight
    intake.busy = false;
    intake.error = null;
    intake.extraction = null;
    intake.source = "";
    pathFocus = "";
    applied = null;
    carry = null;
    lastBody = null;
    renderDecomposeTab();
  }

  // ---- path A: verbatim ----------------------------------------------------

  async function useAsIs(button) {
    const extraction = state.intake.extraction;
    if (!extraction) return;
    pathFocus = "verbatim";
    if (button?.mrlnArmed) {
      await armDestructive(button); // second click — run the armed overwrite
      return;
    }
    const slug = await askString(
      "Save the found prompt as a template",
      "Template slug (lowercase, '/' for folders):",
      defaultIntakeSlug(extraction)
    );
    if (!slug?.trim()) {
      renderDecomposeTab(); // the focus change still has to reach the screen
      return;
    }
    const clean = slug.trim();
    // Two files can be overwritten by one save: the template AND the companion
    // '<slug>-loras' section build_lora_section writes. Arming on either is
    // what keeps a second intake from silently eating the first.
    const templates = state.library?.templates ?? [];
    const sections = state.library?.sections ?? [];
    const clash =
      templates.some((entry) => entry.slug === clean)
      || sections.some((entry) => entry.slug === `${clean}-loras`);
    if (clash) {
      armDestructive(button, `Really overwrite '${clean}'?`, () => saveVerbatim(clean));
      return;
    }
    await saveVerbatim(clean);
  }

  async function saveVerbatim(slug) {
    const extraction = state.intake.extraction;
    if (!extraction) return;
    let body;
    try {
      body = await ctx.apiJson(EXTRACT_APPLY_ROUTE, {
        method: "POST",
        body: extractApplyBody({ path: "verbatim", extraction, slug, save: true }),
      });
    } catch (err) {
      ctx.toast("error", "Verbatim template failed", intakeErrorText(err));
      return;
    }
    applied = body;
    if (body.verbatim === false) {
      ctx.toast(
        "error",
        "Not byte-for-byte",
        "the template was saved but does not reproduce the extracted prompt exactly — "
          + "please report this"
      );
    } else {
      ctx.toast("success", "Template created", `${slug} — opening in Compose`);
    }
    ctx.refreshCombos();
    await loadLibrary();
    await selectTemplate(slug);
    switchTab("compose");
  }

  // ---- path B: decompose ---------------------------------------------------

  async function decomposePath() {
    const intake = state.intake;
    const extraction = intake.extraction;
    if (!extraction) return;
    pathFocus = "decompose";
    const decompose = state.decompose;
    // The response IS handle_decompose's report, so this runs through the SAME
    // supersede token and the SAME spinner the tab's own button uses — two
    // competing writers to state.decompose.report is exactly what runNo is for.
    const runNo = ++decompose.runNo;
    decompose.running = true;
    renderDecomposeTab();
    let report;
    try {
      report = await ctx.apiJson(EXTRACT_APPLY_ROUTE, {
        method: "POST",
        body: extractApplyBody({ path: "decompose", extraction, decompose }),
      });
    } catch (err) {
      if (runNo === decompose.runNo) {
        decompose.running = false;
        ctx.toast("error", "Decompose failed", intakeErrorText(err));
        renderDecomposeTab();
      }
      return;
    }
    if (runNo !== decompose.runNo) return; // a newer run owns the tab
    decompose.running = false;
    // The text area mirrors what was actually de-composed: the fragments below
    // are of THIS string, and the tab's own Decompose button re-runs it.
    decompose.text = String(extraction.positive ?? "");
    decompose.report = report;
    const fragments = report.fragments ?? [];
    decompose.plans = fragments.map((fragment, index) =>
      defaultPlan(fragment, index, fragments)
    );
    // extract-apply forwards the POSITIVE only. The negative was in the image
    // and would otherwise vanish between the two tabs, so it rides along to
    // performDecomposedSave instead of being lost silently.
    carry = { negative: String(extraction.negative ?? "") };
    if (report.llm_error) ctx.toast("warn", "LLM engine fell back", report.llm_error);
    renderDecomposeTab();
  }

  // ---- rendering -----------------------------------------------------------

  function dropZone() {
    const zone = el(
      "div",
      {
        class: "mrln-drop",
        tabindex: "0",
        title:
          "Drop a PNG/JPEG/WebP a generator wrote, or click here and press Ctrl+V. "
          + "Only the file's metadata is uploaded — the pixels never leave the browser.",
      },
      el("span", { class: "mrln-drop-label" }, "⬇  Drop or paste a generated image"),
      el(
        "span",
        { class: "mrln-note" },
        "PNG text chunks · JPEG/WebP EXIF · metadata only, never the pixels"
      )
    );
    wireDropZone(zone, {
      onImage: (file) => intakeFile(file),
      onUrl: (url) => intakeUrl(url),
    });
    return zone;
  }

  function urlRow() {
    const input = el("input", {
      type: "text",
      value: state.intake.url ?? "",
      placeholder: "https://civitai.com/images/12345678",
      title:
        "The image PAGE url. Only its numeric id is used — the server never fetches "
        + "the address you paste, so it can never be pointed at another host.",
      oninput: (event) => {
        state.intake.url = event.target.value;
      },
      onkeydown: (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          intakeUrl(state.intake.url);
        }
      },
    });
    return el(
      "div",
      { class: "mrln-inline" },
      input,
      el(
        "button",
        {
          class: "mrln-btn",
          onclick: (event) => busy(event.currentTarget, () => intakeUrl(state.intake.url)),
        },
        "Fetch"
      )
    );
  }

  function resolveRow() {
    return el(
      "label",
      { class: "mrln-note mrln-intake-opt" },
      el("input", {
        type: "checkbox",
        checked: resolveAirs ? "" : null,
        onchange: (event) => {
          resolveAirs = event.target.checked;
        },
      }),
      " look every LoRA's AIR up on Civitai (one request each — it is what lets a "
        + "missing file heal itself)"
    );
  }

  function candidateCard(candidate, index) {
    const isPositive = pick.positive === index;
    const isNegative = pick.negative === index;
    return el(
      "div",
      { class: `mrln-slot${isPositive || isNegative ? "" : " mrln-broken"}` },
      el(
        "div",
        { class: "mrln-slot-label" },
        el("span", {}, candidateLabel(candidate, index)),
        isPositive ? el("span", { class: "mrln-chip mrln-user" }, "positive") : null,
        isNegative ? el("span", { class: "mrln-chip mrln-factory" }, "negative") : null
      ),
      el("pre", { class: "mrln-pre" }, String(candidate?.text ?? "")),
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: `mrln-btn mrln-mini${isPositive ? " mrln-primary" : ""}`,
            onclick: () => {
              pick = {
                positive: index,
                negative: pick.negative === index ? -1 : pick.negative,
              };
              renderDecomposeTab();
            },
          },
          "positive"
        ),
        el(
          "button",
          {
            class: `mrln-btn mrln-mini${isNegative ? " mrln-primary" : ""}`,
            onclick: () => {
              pick = {
                positive: pick.positive === index ? -1 : pick.positive,
                negative: isNegative ? -1 : index,
              };
              renderDecomposeTab();
            },
          },
          isNegative ? "negative ✓" : "negative"
        )
      )
    );
  }

  function candidatePicker(extraction) {
    const candidates = extraction.candidates ?? [];
    return el(
      "div",
      { class: "mrln-intake-picker" },
      el(
        "div",
        { class: "mrln-error" },
        `This graph offers ${candidates.length} prompt string(s) and does not say `
          + "which is which. Pick before either path — nothing is guessed for you."
      ),
      el("div", { class: "mrln-slot-list" }, candidates.map(candidateCard)),
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: "mrln-btn mrln-primary",
            disabled: pick.positive < 0 ? "" : null,
            title:
              pick.positive < 0
                ? "choose which string is the positive prompt first"
                : "use this pick for both paths",
            onclick: () => {
              const resolved = applyCandidatePick(
                state.intake.extraction,
                pick.positive,
                pick.negative
              );
              if (resolved === state.intake.extraction) return;
              state.intake.extraction = resolved;
              renderDecomposeTab();
            },
          },
          "Use this pick"
        )
      )
    );
  }

  function loraRow(entry) {
    const status = loraStatus(entry);
    const chip = {
      installed: ["mrln-chip mrln-user", "installed"],
      missing: ["mrln-chip mrln-missing", "not installed here"],
      unknown: ["mrln-chip", "not checked"],
    }[status];
    const strength = entry?.strength_model;
    return el(
      "div",
      { class: `mrln-slot${status === "missing" ? " mrln-broken" : ""}` },
      el(
        "div",
        { class: "mrln-slot-label" },
        el("span", {}, `${entry?.name ?? "(unnamed)"}${strength == null ? "" : ` · ${strength}`}`),
        el("span", { class: chip[0] }, chip[1])
      ),
      entry?.air ? el("div", { class: "mrln-note" }, entry.air) : null,
      entry?.catchword ? el("div", { class: "mrln-note" }, `trigger: ${entry.catchword}`) : null
    );
  }

  function foundBlock(extraction) {
    const params = paramRows(extraction.params);
    const loras = extraction.loras ?? [];
    const negative = String(extraction.negative ?? "");
    const parts = [
      el(
        "div",
        { class: "mrln-slot-label" },
        el("span", { class: "mrln-field-name" }, "What the image carried"),
        el("span", { class: "mrln-chip mrln-user" }, extraction.dialect || "unknown dialect"),
        extraction.container ? el("span", { class: "mrln-chip" }, extraction.container) : null
      ),
      el("div", { class: "mrln-note" }, sourceLabel(extraction.source)),
    ];
    if (needsCandidatePicker(extraction)) {
      parts.push(candidatePicker(extraction));
      return parts;
    }
    if (extraction.ambiguous) {
      parts.push(
        el(
          "div",
          { class: "mrln-error" },
          "the graph was read but offered no prompt string to use — paste the prompt "
            + "into the box below instead"
        )
      );
    }
    parts.push(
      field("Positive", el("pre", { class: "mrln-pre" }, String(extraction.positive ?? "")))
    );
    if (negative) parts.push(field("Negative", el("pre", { class: "mrln-pre" }, negative)));
    if (hasInlineLoraTag(extraction.positive) || hasInlineLoraTag(negative)) {
      parts.push(
        el(
          "div",
          { class: "mrln-note mrln-intake-warn" },
          "this text still carries inline <lora:…> tags — they were never parsed out "
            + "(a hand-picked candidate skips that pass), so 'Use as-is' reproduces "
            + "them literally"
        )
      );
    }
    if (params.length) {
      parts.push(
        el("span", { class: "mrln-field-name" }, `Generation settings (${params.length})`),
        el(
          "div",
          { class: "mrln-params" },
          params.map(([key, value]) => [
            el("span", { class: "mrln-params-key" }, key),
            el("span", { class: "mrln-params-value" }, value),
          ])
        )
      );
    }
    if (loras.length) {
      parts.push(
        el("span", { class: "mrln-field-name" }, `LoRAs (${loras.length})`),
        el("div", { class: "mrln-slot-list" }, loras.map(loraRow))
      );
    }
    for (const note of extraction.notes ?? []) {
      parts.push(el("div", { class: "mrln-note" }, `· ${note}`));
    }
    if (unresolvedAirCount(extraction) && !resolveAirs) {
      parts.push(
        el(
          "div",
          { class: "mrln-actions" },
          el(
            "button",
            {
              class: "mrln-btn mrln-mini",
              title: "re-reads the same image with resolve=true — one Civitai request per LoRA",
              onclick: (event) => busy(event.currentTarget, rerunWithResolve),
            },
            "Look the AIRs up"
          )
        )
      );
    }
    parts.push(pathActions(extraction));
    return parts;
  }

  function pathActions(extraction) {
    const blocked = needsCandidatePicker(extraction) || !String(extraction.positive ?? "").trim();
    const reason = blocked
      ? "there is no positive prompt to act on yet"
      : "";
    const lines = decomposeLossNotes(extraction);
    return el(
      "div",
      { class: "mrln-intake-paths" },
      el(
        "div",
        { class: "mrln-actions" },
        el(
          "button",
          {
            class: `mrln-btn${pathFocus === "verbatim" ? " mrln-primary" : ""}`,
            disabled: blocked ? "" : null,
            title:
              reason
              || "Path A — the found prompt becomes a template that renders it back "
                + "exactly. No slotting, no library matching, no LLM: it works with "
                + "every backend unset, so the engine/backend/model selects below do "
                + "not apply to it.",
            onclick: (event) => {
              const button = event.currentTarget; // cleared once the handler returns
              return busy(button, () => useAsIs(button));
            },
          },
          "Use as-is"
        ),
        el(
          "button",
          {
            class: `mrln-btn${pathFocus === "decompose" ? " mrln-primary" : ""}`,
            disabled: blocked ? "" : null,
            title:
              reason
              || "Path B — the found text goes through the de-composer below, so the "
                + "engine/backend/model selects DO apply. The result lands in the "
                + "fragment cards, where you decide what happens to each one.",
            onclick: (event) => busy(event.currentTarget, decomposePath),
          },
          "Decompose"
        ),
        el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            title: "forget this extraction and unlock the controls below",
            onclick: clearIntake,
          },
          "Clear"
        )
      ),
      el(
        "div",
        { class: "mrln-note" },
        "'Use as-is' reproduces this prompt exactly and uses no LLM — the engine, "
          + "backend and model selects below apply to 'Decompose' only."
      ),
      lines.length
        ? el("div", { class: "mrln-note mrln-intake-warn" }, `Decompose drops: ${lines.join("; ")}`)
        : null
    );
  }

  function appliedBlock() {
    const notes = verbatimResultNotes(applied);
    if (!notes.length && !applied?.slug) return null;
    return el(
      "div",
      { class: "mrln-intake-applied" },
      applied?.slug
        ? el("div", { class: "mrln-note" }, `Saved verbatim as '${applied.slug}'`)
        : null,
      notes.map((note) =>
        el("div", { class: note.kind === "error" ? "mrln-error" : "mrln-note" }, note.text)
      )
    );
  }

  /** The card the De-compose tab mounts above its own text area. */
  function renderIntakeCard() {
    const intake = state.intake;
    const parts = [
      el("span", { class: "mrln-field-name" }, "Image → template"),
      el(
        "div",
        { class: "mrln-note" },
        "Read a generated image's own metadata, then pick what happens to it. "
          + "Nothing is decided for you and nothing runs until you press a button."
      ),
      dropZone(),
      urlRow(),
      resolveRow(),
    ];
    if (intake.source && (intake.busy || intake.extraction || intake.error)) {
      parts.push(el("div", { class: "mrln-note" }, intake.source));
    }
    if (intake.busy) parts.push(loadingNote("Reading the image's metadata…"));
    if (intake.error) {
      parts.push(
        el("div", { class: "mrln-error" }, intake.error),
        el(
          "div",
          { class: "mrln-actions" },
          el("button", { class: "mrln-btn mrln-mini", onclick: clearIntake }, "Dismiss")
        )
      );
    }
    if (intake.extraction) parts.push(...foundBlock(intake.extraction));
    parts.push(appliedBlock());
    return el("div", { class: "mrln-intake" }, ...parts);
  }

  /** "" | "verbatim" | "decompose" — which path the user is looking at. */
  function intakePath() {
    return state.intake.extraction ? pathFocus : "";
  }

  /** Lets the De-compose tab unlock its own selects without running anything. */
  function setIntakePath(path) {
    pathFocus = path === "verbatim" || path === "decompose" ? path : "";
    renderDecomposeTab();
  }

  /** What path B would otherwise lose between the two tabs, or null. */
  function intakeCarry() {
    return carry;
  }

  function clearIntakeCarry() {
    carry = null;
  }

  return { renderIntakeCard, intakePath, setIntakePath, intakeCarry, clearIntakeCarry };
}
