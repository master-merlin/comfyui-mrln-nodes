// composer/intake.js — the PURE half of the image → template intake card.
//
// What is tested here is everything that turns a server payload into a
// decision: the two request bodies, the extraction → display derivations, the
// candidate resolution (the thing that must never guess silently) and the error
// mapping. The render functions are not: there is no jsdom in this repo by
// design, and faking a browser to assert element trees would test the fake.
//
// The module is imported with document/window booby-trapped in
// composer_modules.test.mjs; here it is imported plainly, which is safe for the
// same reason — importing it declares functions and does nothing else.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  EXTRACT_APPLY_ROUTE,
  EXTRACT_IMAGE_ROUTE,
  applyCandidatePick,
  applyExtraction,
  candidateLabel,
  decomposeLossNotes,
  defaultCandidatePick,
  defaultIntakeSlug,
  extractApplyBody,
  extractImageBody,
  hasInlineLoraTag,
  intakeErrorText,
  loraStatus,
  needsCandidatePicker,
  paramRows,
  sourceLabel,
  unresolvedAirCount,
  verbatimResultNotes,
} from "../../web/js/composer/intake.js";

// An /extract-image response, keys exactly as promptapi/intake.py returns them
// (handle_extract_image's docstring: source, dialect, container, positive,
// negative, params, loras, resources?, candidates?, ambiguous?, notes).
function a1111Extraction(overrides = {}) {
  return {
    source: "parameters",
    dialect: "a1111",
    container: "PNG",
    positive: "a red sports car, cinematic lighting",
    negative: "blurry, watermark",
    params: {
      Steps: "28",
      Sampler: "DPM++ 2M",
      "CFG scale": "6",
      Seed: "12345",
      "Civitai resources": '[{"type":"lora","modelName":"Neon","weight":0.8}]',
    },
    loras: [
      {
        name: "neon-glow",
        strength_model: 0.8,
        strength_clip: 0.8,
        file: "neon-glow.safetensors",
        air: "urn:air:sdxl:lora:civitai:123@456",
      },
    ],
    notes: ["1 LoRA(s) carry a Civitai modelVersionId but no AIR — …"],
    ...overrides,
  };
}

function comfyAmbiguous(overrides = {}) {
  return {
    source: "comfy-prompt",
    dialect: "comfyui",
    container: "PNG",
    positive: "",
    negative: "",
    params: {},
    loras: [],
    ambiguous: true,
    candidates: [
      { role: "positive", text: "hero shot of a car", node: "6", class_type: "CLIPTextEncode" },
      { role: "positive", text: "a second positive", node: "9", class_type: "CLIPTextEncode" },
      { role: "negative", text: "lowres, jpeg artifacts", node: "7", class_type: "CLIPTextEncode" },
    ],
    notes: [],
    ...overrides,
  };
}

describe("routes", () => {
  test("the two endpoints are the ones routes.py registers", () => {
    assert.equal(EXTRACT_IMAGE_ROUTE, "/mrln/prompt/extract-image");
    assert.equal(EXTRACT_APPLY_ROUTE, "/mrln/prompt/extract-apply");
  });
});

describe("extractImageBody", () => {
  test("a data: URI travels as `image`", () => {
    const body = extractImageBody({ image: "data:image/png;base64,AAAA" });
    assert.deepEqual(body, { image: "data:image/png;base64,AAAA" });
  });

  test("a URL travels as `url` and wins over any image, mirroring the server", () => {
    // handle_extract_image checks `url` FIRST; sending both would silently pick
    // the URL server-side, so this never sends both
    const body = extractImageBody({ image: "data:image/png;base64,AAAA", url: " x " });
    assert.deepEqual(body, { url: "x" });
    assert.equal("image" in body, false);
  });

  test("resolve is opt-in and only present when asked for", () => {
    assert.equal("resolve" in extractImageBody({ image: "d" }), false);
    assert.equal(extractImageBody({ image: "d", resolve: true }).resolve, true);
    assert.equal(extractImageBody({ url: "u", resolve: true }).resolve, true);
  });

  test("no arguments still produces a well-formed (if empty) body", () => {
    assert.deepEqual(extractImageBody(), { image: "" });
  });
});

describe("applyExtraction", () => {
  test("carries exactly _extraction_arg's allowlist, values verbatim", () => {
    const extraction = a1111Extraction();
    const sent = applyExtraction(extraction);
    assert.deepEqual(Object.keys(sent).sort(), [
      "loras",
      "negative",
      "params",
      "positive",
      "source",
    ]);
    assert.equal(sent.positive, extraction.positive);
    assert.equal(sent.negative, extraction.negative);
    assert.equal(sent.source, "parameters");
    assert.equal(sent.params, extraction.params); // same object, not a rewrite
    assert.equal(sent.loras, extraction.loras);
  });

  test("drops candidates/resources — they are unread AND ride the same 1 MiB cap", () => {
    const sent = applyExtraction(comfyAmbiguous({ resources: [{ type: "lora" }] }));
    assert.equal("candidates" in sent, false);
    assert.equal("resources" in sent, false);
    assert.equal("ambiguous" in sent, false);
  });

  test("a malformed extraction degrades into the empty shape instead of throwing", () => {
    assert.deepEqual(applyExtraction(null), {
      positive: "",
      negative: "",
      params: {},
      loras: [],
      source: "",
    });
    assert.deepEqual(applyExtraction({ params: "nope", loras: "nope" }).params, {});
    assert.deepEqual(applyExtraction({ params: "nope", loras: "nope" }).loras, []);
  });
});

describe("extractApplyBody — path A (verbatim)", () => {
  test("slug/label/save ride along, decomposer knobs never do", () => {
    const body = extractApplyBody({
      path: "verbatim",
      extraction: a1111Extraction(),
      slug: "  intake/red-car  ",
      label: "Red car",
      save: true,
      decompose: { engine: "llm", backend: "ollama", model: "qwen" },
    });
    assert.equal(body.path, "verbatim");
    assert.equal(body.slug, "intake/red-car");
    assert.equal(body.label, "Red car");
    assert.equal(body.save, true);
    for (const key of ["engine", "backend", "model", "timeout", "type"]) {
      assert.equal(key in body, false, `path A must not send '${key}'`);
    }
  });

  test("a dry run sends no save flag at all (the server defaults to not saving)", () => {
    const body = extractApplyBody({ path: "verbatim", extraction: a1111Extraction() });
    assert.equal("save" in body, false);
    assert.equal("slug" in body, false);
    assert.equal("label" in body, false);
  });

  test("a whitespace-only slug is not sent — the server would reject it anyway", () => {
    const body = extractApplyBody({ path: "verbatim", extraction: {}, slug: "   ", save: true });
    assert.equal("slug" in body, false);
    assert.equal(body.save, true); // handle_extract_apply answers the real error
  });
});

describe("extractApplyBody — path B (decompose)", () => {
  test("programmatic sends no backend/model/timeout, exactly like runDecompose", () => {
    const body = extractApplyBody({
      path: "decompose",
      extraction: a1111Extraction(),
      decompose: { engine: "programmatic", type: "object, car" },
    });
    assert.equal(body.path, "decompose");
    assert.equal(body.engine, "programmatic");
    assert.equal(body.type, "object, car");
    for (const key of ["backend", "model", "timeout"]) {
      assert.equal(key in body, false, `programmatic must not send '${key}'`);
    }
  });

  test("llm/hybrid send backend, model and a timeout with LLM headroom", () => {
    const body = extractApplyBody({
      path: "decompose",
      extraction: a1111Extraction(),
      decompose: { engine: "hybrid", backend: "anthropic", model: "claude-x", type: "" },
    });
    assert.equal(body.engine, "hybrid");
    assert.equal(body.backend, "anthropic");
    assert.equal(body.model, "claude-x");
    assert.ok(body.timeout >= 5 && body.timeout <= 600, "handle_decompose's accepted range");
  });

  test("missing knobs fall back the way the tab's own defaults do", () => {
    const body = extractApplyBody({ path: "decompose", extraction: a1111Extraction() });
    assert.equal(body.engine, "programmatic");
    assert.equal(body.type, "");
  });

  test("slug/label/save are never sent on path B", () => {
    const body = extractApplyBody({
      path: "decompose",
      extraction: a1111Extraction(),
      slug: "intake/x",
      label: "X",
      save: true,
      decompose: { engine: "programmatic" },
    });
    for (const key of ["slug", "label", "save"]) {
      assert.equal(key in body, false, `path B must not claim '${key}'`);
    }
  });

  test("both paths send the same trimmed extraction", () => {
    const extraction = comfyAmbiguous({ positive: "picked" });
    const a = extractApplyBody({ path: "verbatim", extraction });
    const b = extractApplyBody({ path: "decompose", extraction });
    assert.deepEqual(a.extraction, b.extraction);
    assert.equal(a.extraction.positive, "picked");
  });
});

describe("candidate resolution — never guess silently", () => {
  test("a picker is offered only when there is something to pick", () => {
    assert.equal(needsCandidatePicker(comfyAmbiguous()), true);
    assert.equal(needsCandidatePicker(a1111Extraction()), false);
    // extraction_from_candidates also sets ambiguous for a graph that yielded
    // NOTHING — a picker with no options is a dead end, not a question
    assert.equal(needsCandidatePicker({ ambiguous: true, candidates: [] }), false);
    assert.equal(needsCandidatePicker(null), false);
  });

  test("the default pick only echoes what the server itself resolved", () => {
    // two positives, one negative: the server resolved the negative and left
    // the positive ambiguous, so exactly the negative is pre-selected
    const extraction = comfyAmbiguous({ negative: "lowres, jpeg artifacts" });
    assert.deepEqual(defaultCandidatePick(extraction), { positive: -1, negative: 2 });
  });

  test("nothing is pre-selected when the server resolved nothing", () => {
    assert.deepEqual(defaultCandidatePick(comfyAmbiguous()), { positive: -1, negative: -1 });
  });

  test("applying a pick resolves the extraction and records that a human did it", () => {
    const extraction = comfyAmbiguous();
    const resolved = applyCandidatePick(extraction, 1, 2);
    assert.equal(resolved.positive, "a second positive");
    assert.equal(resolved.negative, "lowres, jpeg artifacts");
    assert.equal(resolved.ambiguous, false);
    assert.deepEqual(resolved.picked, { positive: 1, negative: 2 });
    assert.match(resolved.notes.at(-1), /picked by hand/);
    assert.equal(extraction.ambiguous, true, "the input must not be mutated");
    assert.equal(extraction.positive, "");
  });

  test("a pick with no negative resolves the negative to empty, explicitly", () => {
    const resolved = applyCandidatePick(comfyAmbiguous(), 0, -1);
    assert.equal(resolved.positive, "hero shot of a car");
    assert.equal(resolved.negative, "");
    assert.deepEqual(resolved.picked, { positive: 0, negative: -1 });
    assert.match(resolved.notes.at(-1), /^positive picked by hand/);
  });

  test("without a positive nothing resolves — the extraction comes back untouched", () => {
    const extraction = comfyAmbiguous();
    assert.equal(applyCandidatePick(extraction, -1, 2), extraction);
    assert.equal(applyCandidatePick(extraction, 99, 2), extraction);
  });

  test("candidate labels name the graph node they came from", () => {
    const [first] = comfyAmbiguous().candidates;
    assert.equal(candidateLabel(first, 0), "1. positive · #6 CLIPTextEncode");
    assert.equal(candidateLabel({ role: "", text: "x" }, 3), "4. unknown");
  });
});

describe("display derivations", () => {
  test("the LoRA tri-state is preserved: installed / missing / not checked", () => {
    // attach_local_files: file=None means there was no ComfyUI to ask, file=""
    // means asked and absent. Collapsing them would accuse a headless server.
    assert.equal(loraStatus({ file: "sub/neon.safetensors" }), "installed");
    assert.equal(loraStatus({ file: "" }), "missing");
    assert.equal(loraStatus({ file: null }), "unknown");
    assert.equal(loraStatus({}), "unknown");
  });

  test("param rows keep the generator's order and drop the JSON blobs", () => {
    const rows = paramRows(a1111Extraction().params);
    assert.deepEqual(rows.map(([key]) => key), ["Steps", "Sampler", "CFG scale", "Seed"]);
    assert.deepEqual(rows[0], ["Steps", "28"]);
    // matched case-insensitively, the way the server's own _PARAM_SKIP does
    assert.deepEqual(paramRows({ "civitai metadata": "{}", "CIVITAI RESOURCES": "[]" }), []);
    assert.deepEqual(paramRows(null), []);
  });

  test("empty parameter values are not rendered as blank rows", () => {
    assert.deepEqual(paramRows({ Steps: "", Seed: null, Model: "x" }), [["Model", "x"]]);
  });

  test("every server `source` value has words, and an unknown one still reads", () => {
    for (const source of [
      "parameters",
      "exif-usercomment",
      "comfy-prompt",
      "comfy-workflow",
      "civitai-api",
    ]) {
      assert.ok(sourceLabel(source).length > source.length, `${source} has no label`);
    }
    assert.equal(sourceLabel("something-new"), "source: something-new");
    assert.equal(sourceLabel(""), "unknown source");
  });

  test("inline LoRA tags are detected (to warn), never parsed", () => {
    assert.equal(hasInlineLoraTag("a car <lora:neon:0.8> at night"), true);
    assert.equal(hasInlineLoraTag("<LYCORIS:x:1>"), true);
    assert.equal(hasInlineLoraTag("<lyco :x:1>"), true);
    assert.equal(hasInlineLoraTag("a car at night"), false);
    assert.equal(hasInlineLoraTag("<loras of the rings>"), false);
    assert.equal(hasInlineLoraTag(null), false);
  });

  test("path B's losses are stated in numbers, not hand-waved", () => {
    const lines = decomposeLossNotes(a1111Extraction());
    assert.equal(lines.length, 2);
    assert.match(lines[0], /^4 generation setting\(s\)/); // blobs excluded
    assert.match(lines[1], /^1 LoRA\(s\)/);
    assert.deepEqual(decomposeLossNotes({ params: {}, loras: [] }), []);
    assert.deepEqual(decomposeLossNotes(null), []);
  });

  test("the AIR re-run is offered only for LoRAs that can actually resolve", () => {
    // _finish's own rule: an entry with a modelVersionId and no AIR
    assert.equal(unresolvedAirCount(a1111Extraction()), 0); // it already has an AIR
    assert.equal(
      unresolvedAirCount({ loras: [{ model_version_id: 456 }, { name: "x" }] }),
      1
    );
    assert.equal(unresolvedAirCount(null), 0);
  });

  test("the prefilled slug comes from the prompt's first phrase", () => {
    assert.equal(defaultIntakeSlug(a1111Extraction()), "intake/a-red-sports-car");
    assert.equal(defaultIntakeSlug({ positive: "one line\nsecond line" }), "intake/one-line");
    assert.ok(defaultIntakeSlug({}).startsWith("intake/"));
    assert.ok(defaultIntakeSlug(null).startsWith("intake/"));
  });
});

describe("error mapping", () => {
  test("the server's remediation is the message's other half, never dropped", () => {
    const err = new Error("the image payload is about 900 KiB, over the 700 KiB intake limit");
    err.remediation = "paste the image's civitai.com URL instead";
    err.status = 413;
    assert.equal(
      intakeErrorText(err),
      "the image payload is about 900 KiB, over the 700 KiB intake limit "
        + "— paste the image's civitai.com URL instead"
    );
  });

  test("a 413 without a remediation still says what to do about it", () => {
    const err = new Error("HTTP 413");
    err.status = 413;
    assert.match(intakeErrorText(err), /civitai\.com URL/);
  });

  test("an ordinary failure is its message and nothing invented", () => {
    assert.equal(intakeErrorText(new Error("Failed to fetch")), "Failed to fetch");
    assert.equal(intakeErrorText(null), "unknown error");
    assert.equal(intakeErrorText("boom"), "boom");
  });
});

describe("path A result", () => {
  test("verbatim:false is an ERROR, not a note — it is path A's whole contract", () => {
    const notes = verbatimResultNotes({
      verbatim: false,
      notes: ["2 LoRA(s) were recorded as items in 'intake/x-loras'."],
    });
    assert.equal(notes[0].kind, "error");
    assert.match(notes[0].text, /byte for byte/);
    assert.equal(notes[1].kind, "note");
  });

  test("a clean save surfaces the server's notes as notes", () => {
    const notes = verbatimResultNotes({ verbatim: true, notes: ["a", "b"] });
    assert.deepEqual(notes, [
      { kind: "note", text: "a" },
      { kind: "note", text: "b" },
    ]);
  });

  test("nothing to say produces nothing", () => {
    assert.deepEqual(verbatimResultNotes(null), []);
    assert.deepEqual(verbatimResultNotes({ verbatim: true }), []);
  });
});
