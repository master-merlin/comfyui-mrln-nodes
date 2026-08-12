// Unit tests for the "Optimize for …" comparison (SPEC 5.3) — the pure half of
// web/js/composer/compose.js.
//
// Reading order is a render-time function of the PROFILE, so the Compose tab
// renders the same draw twice and shows the two orders side by side. Three
// things there can be wrong in ways no eyeball catches, so each is a pure
// exported function tested here:
//
//   optimizeBodies   — request shaping. `state.profile` is the TEMPLATE VARIANT
//                      target and rides the very same `profile` key of the
//                      preview body, so the comparison must differ from the
//                      live preview in exactly ONE field and must never write
//                      state.profile.
//   orderComparison  — "did anything actually move", and the differences that
//                      are NOT order (a profile can also change the format, the
//                      negative or the drawn text).
//   orderWriteBack   — resolved slot ids -> a template `order` array, refusing
//                      the shapes a template cannot store.
//
// The sort itself lives in mrln/promptlib/render.py and is never reimplemented
// here: `render_order` comes off the preview response.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  optimizeBodies,
  optimizeSignature,
  orderComparison,
  orderWriteBack,
} from "../../web/js/composer/compose.js";

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

/** A composer state slice shaped exactly like createState() leaves it. */
function makeState(over = {}) {
  const rawData = {
    version: 1,
    slots: [
      { id: "subject", ref: "subject/people", default: "driver" },
      { id: "style", ref: "style/paint", default: "oil" },
      { id: "camera", ref: "camera/lens", default: "35mm" },
    ],
  };
  const state = {
    slug: "shot",
    seed: 7,
    mode: "as configured",
    format: "template default",
    conflictPolicy: "negative prevails",
    textLength: "template default",
    trigger: "",
    variables: "",
    profile: "standard",
    modified: false,
    variant: null,
    rawData,
    baseRaw: structuredClone(rawData),
    orderIds: ["subject", "style", "camera"],
    rows: new Map([
      ["subject", { random: false, seed: "", item: "driver" }],
      ["style", { random: true, seed: "", item: "" }],
      ["camera", { random: false, seed: "", item: "35mm" }],
    ]),
    muted: new Set(),
    soloed: new Set(),
    ...over,
  };
  return state;
}

/** A preview response, trimmed to the keys the comparison reads. */
function preview(order, over = {}) {
  return {
    positive: order.join(", "),
    negative: "lowres",
    format: "string",
    render_order: order,
    slots: order.map((id) => ({
      id,
      label: id[0].toUpperCase() + id.slice(1),
      section_slug: `${id}/pool`,
      item: `${id}-item`,
    })),
    ...over,
  };
}

// ---------------------------------------------------------------------------
// request shaping
// ---------------------------------------------------------------------------

describe("optimizeBodies", () => {
  test("the two bodies differ in exactly one field: profile", () => {
    const [base, target] = optimizeBodies(makeState(), "krea2");
    assert.equal(base.profile, "standard");
    assert.equal(target.profile, "krea2");
    const differing = Object.keys({ ...base, ...target }).filter(
      (key) => JSON.stringify(base[key]) !== JSON.stringify(target[key])
    );
    assert.deepEqual(differing, ["profile"]);
  });

  test("it carries every knob the live preview carries", () => {
    const state = makeState({
      seed: 11,
      mode: "randomize all",
      format: "json",
      conflictPolicy: "positive prevails",
      textLength: "short",
      trigger: "SkylineGTR",
      variables: "mood=calm",
    });
    const [base] = optimizeBodies(state, "sdxl");
    assert.deepEqual(base, {
      template: "shot",
      seed: 11,
      mode: "randomize all",
      selection: "style=random", // rows that differ from the file's defaults
      variables: "mood=calm",
      trigger: "SkylineGTR",
      format: "json",
      conflict_policy: "positive prevails",
      text_length: "short",
      profile: "standard",
    });
  });

  test("the baseline is the CURRENT target profile, not a hardcoded 'standard'", () => {
    // state.profile is the template-variant target; the comparison's 'before'
    // has to be what the live preview is actually showing.
    const [base, target] = optimizeBodies(makeState({ profile: "sdxl" }), "krea2");
    assert.equal(base.profile, "sdxl");
    assert.equal(target.profile, "krea2");
  });

  test("it never writes state.profile (the trap this feature had to dodge)", () => {
    const state = makeState({ profile: "sdxl" });
    const before = JSON.stringify({ profile: state.profile, seed: state.seed });
    optimizeBodies(state, "krea2");
    optimizeSignature(state, "krea2");
    assert.equal(JSON.stringify({ profile: state.profile, seed: state.seed }), before);
  });

  test("unsaved edits travel as a draft on BOTH sides, like doPreview", () => {
    const clean = optimizeBodies(makeState(), "krea2");
    assert.ok(!("template_data" in clean[0]) && !("template_data" in clean[1]));
    const [base, target] = optimizeBodies(makeState({ modified: true }), "krea2");
    assert.deepEqual(base.template_data, target.template_data);
    assert.deepEqual(base.template_data.order, ["subject", "style", "camera"]);
  });

  test("a missing state.profile falls back to standard", () => {
    const state = makeState();
    delete state.profile;
    assert.equal(optimizeBodies(state, "krea2")[0].profile, "standard");
  });
});

describe("optimizeSignature", () => {
  test("equal inputs, equal signature", () => {
    assert.equal(optimizeSignature(makeState(), "krea2"), optimizeSignature(makeState(), "krea2"));
  });

  test("anything that changes the render changes it — the staleness banner", () => {
    const base = optimizeSignature(makeState(), "krea2");
    for (const over of [
      { seed: 8 },
      { mode: "randomize all" },
      { format: "json" },
      { textLength: "short" },
      { conflictPolicy: "positive prevails" },
      { trigger: "x" },
      { variables: "a=b" },
      { profile: "sdxl" },
      { modified: true },
    ]) {
      assert.notEqual(optimizeSignature(makeState(over), "krea2"), base, JSON.stringify(over));
    }
    // and so does picking a different target
    assert.notEqual(optimizeSignature(makeState(), "sdxl"), base);
    // …including a changed pick, which travels as a selection line
    const repicked = makeState();
    repicked.rows.set("style", { random: false, seed: "", item: "ink" });
    assert.notEqual(optimizeSignature(repicked, "krea2"), base);
  });
});

// ---------------------------------------------------------------------------
// the diff
// ---------------------------------------------------------------------------

describe("orderComparison", () => {
  const authored = preview(["subject", "style", "camera"]);

  test("it reports what moved, and where each block came from", () => {
    const cmp = orderComparison(authored, preview(["camera", "subject", "style"]));
    assert.ok(cmp.known && cmp.sameSet && cmp.moved);
    assert.deepEqual(cmp.rows, [
      { id: "camera", at: 0, was: 2 },
      { id: "subject", at: 1, was: 0 },
      { id: "style", at: 2, was: 1 },
    ]);
  });

  test("same order = nothing moved, even when the text differs", () => {
    // a profile can change text_length/format without touching the order —
    // 'moved' must answer the order question only
    const same = preview(["subject", "style", "camera"], { positive: "SHORTER TEXT" });
    const cmp = orderComparison(authored, same);
    assert.equal(cmp.moved, false);
    assert.equal(cmp.sameSet, true);
    assert.equal(cmp.textChanged, true);
  });

  test("a different SET of blocks is not a reorder", () => {
    // e.g. the profile's overrides mute a slot or switch the variant: there is
    // no order to write back, and claiming 'nothing moved' would be a lie
    const cmp = orderComparison(authored, preview(["subject", "style"]));
    assert.equal(cmp.sameSet, false);
    assert.equal(cmp.moved, false);
  });

  test("a server that does not report render_order degrades, it does not throw", () => {
    const legacy = preview(["subject", "style", "camera"]);
    delete legacy.render_order;
    const cmp = orderComparison(authored, legacy);
    assert.equal(cmp.known, false);
    assert.equal(cmp.moved, false);
    assert.deepEqual(cmp.rows, []);
    assert.equal(orderComparison(undefined, undefined).known, false);
  });

  test("it flags the differences that are NOT order", () => {
    const other = preview(["subject", "style", "camera"], {
      format: "json",
      negative: "",
      slots: [
        { id: "subject", section_slug: "subject/people", item: "racer" },
        { id: "style", section_slug: "style/paint", item: "style-item" },
        { id: "camera", section_slug: "camera/lens", item: "camera-item" },
      ],
    });
    const cmp = orderComparison(authored, other);
    assert.equal(cmp.formatChanged, true);
    assert.equal(cmp.negativeChanged, true);
    assert.equal(cmp.drawChanged, true); // a different item was drawn
    const twin = orderComparison(authored, preview(["subject", "style", "camera"]));
    assert.deepEqual(
      [twin.formatChanged, twin.negativeChanged, twin.drawChanged, twin.textChanged],
      [false, false, false, false]
    );
  });

  test("slots that drew nothing are listed: block_order cannot rank them", () => {
    // a muted/omitted slot carries no section_slug, so it sorts at the neutral
    // rank whatever its domain — the write-back has to warn about it
    const optimized = preview(["camera", "subject", "style"]);
    optimized.slots[1] = { id: "subject", section_slug: "", item: null };
    assert.deepEqual(orderComparison(authored, optimized).unranked, ["subject"]);
    assert.deepEqual(orderComparison(authored, preview(["subject"])).unranked, []);
  });
});

// ---------------------------------------------------------------------------
// the write-back mapping
// ---------------------------------------------------------------------------

describe("orderWriteBack", () => {
  test("a plain reorder is the order array itself", () => {
    assert.deepEqual(
      orderWriteBack(["camera", "subject", "style"], ["subject", "style", "camera"]),
      { order: ["camera", "subject", "style"] }
    );
  });

  test("a contiguous variant block collapses into one '@variant' entry", () => {
    // resolved variant slots render as '<variant>/<slot id>'; a template stores
    // the whole block as a single token
    assert.deepEqual(
      orderWriteBack(
        ["night/mood", "night/glow", "subject", "style"],
        ["subject", "style", "@variant"]
      ),
      { order: ["@variant", "subject", "style"] }
    );
    assert.deepEqual(
      orderWriteBack(
        ["subject", "night/mood", "night/glow", "style"],
        ["subject", "style", "@variant"]
      ),
      { order: ["subject", "@variant", "style"] }
    );
  });

  test("a split variant block is refused, not silently rejoined", () => {
    const out = orderWriteBack(
      ["night/mood", "subject", "night/glow", "style"],
      ["subject", "style", "@variant"]
    );
    assert.ok(!out.order);
    assert.match(out.error, /splits the variant block/);
  });

  test("an order that cannot place every slot is refused", () => {
    // the draw muted 'camera', so nothing says where it belongs
    const out = orderWriteBack(["subject", "style"], ["subject", "style", "camera"]);
    assert.ok(!out.order);
    assert.match(out.error, /camera/);
    // …and the same for a template whose variant block never rendered
    assert.match(
      orderWriteBack(["subject"], ["subject", "@variant"]).error,
      /@variant/
    );
  });

  test("an id the template does not know is refused", () => {
    const out = orderWriteBack(["subject", "ghost"], ["subject"]);
    assert.ok(!out.order);
    assert.match(out.error, /'ghost' is not a slot/);
  });

  test("empty inputs never produce a half-written order", () => {
    assert.deepEqual(orderWriteBack([], []), { order: [] });
    assert.match(orderWriteBack(undefined, ["subject"]).error, /subject/);
  });
});
