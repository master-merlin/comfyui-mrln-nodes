// Behavior-pinning unit tests for web/js/composer/util.js.
//
// These exist so the REST of the panel split cannot silently change what the
// composer persists. Every assertion here describes behavior as it shipped —
// where current behavior looks wrong it is pinned with a "PINS CURRENT
// BEHAVIOR" note rather than corrected, so a deliberate fix has to come with
// a deliberate test change.
//
// Run: node --test tests/js/

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const UTIL_URL = new URL("../../web/js/composer/util.js", import.meta.url);

// ---------------------------------------------------------------------------
// Load the module with browser globals booby-trapped. ComfyUI auto-imports
// every .js under web/, so util.js is evaluated standalone in the browser as
// well as via the panel; it must do NOTHING at import time. If a future edit
// adds a top-level DOM/app/api/network touch, this import throws and the whole
// file fails loudly.
// ---------------------------------------------------------------------------

const TRAPPED = ["document", "window", "app", "api", "localStorage", "XMLHttpRequest", "alert"];
const saved = new Map();
for (const name of TRAPPED) {
  saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, {
    configurable: true,
    get() {
      throw new Error(`util.js touched the global '${name}' at import time`);
    },
  });
}
const savedFetch = globalThis.fetch;
globalThis.fetch = () => {
  throw new Error("util.js performed a network request at import time");
};

const util = await import(UTIL_URL.href);

globalThis.fetch = savedFetch;
for (const name of TRAPPED) {
  const desc = saved.get(name);
  if (desc) Object.defineProperty(globalThis, name, desc);
  else delete globalThis[name];
}

const {
  activeSlots,
  allSlots,
  appliedStateDiffers,
  auditionActive,
  buildDraftData,
  buildSaveData,
  buildSelectionLines,
  bundleFilename,
  combineItem,
  defaultPlan,
  diffProfileOverrides,
  downloadableAir,
  effectiveRaw,
  isCombineItem,
  itemRowEdited,
  jsSlugify,
  loraKey,
  loraProgressText,
  missingLoraRows,
  moveInArray,
  overrideTweakCount,
  overridesFor,
  parseKvLines,
  parseToken,
  rowToken,
  slotAudible,
  structuralDrift,
  syncOrderIds,
  uniqueName,
  variantBlockAudible,
  variantSlotIds,
} = util;

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

/** Template WITHOUT variants: 'style' ships baked off (default "off"). */
const flat = () => ({
  version: 1,
  label: "Street scene",
  prefix: "a photo of",
  suffix: "high detail",
  negative: "blurry",
  slots: [
    { id: "subject", ref: "people/portrait", default: "woman" },
    { id: "setting", ref: "places/urban" },
    { id: "style", ref: "styles/photo", default: "off" },
  ],
});

/** Template WITH variants and no explicit variant_default. */
const withVariants = () => ({
  version: 1,
  label: "Day or night",
  slots: [{ id: "subject", ref: "people/portrait", default: "woman" }],
  variants: [
    { name: "day", slots: [{ id: "light", ref: "light/day", default: "noon" }] },
    { name: "night", slots: [{ id: "neon", ref: "light/neon" }] },
  ],
});

/**
 * Mirrors the panel's initRowsFromRaw(): rows come from the FILE, and a slot
 * whose default is "off" loads as muted with a 'random' row. Tests that start
 * here describe "template just opened in the Composer".
 */
function loadState(raw, patch = {}) {
  const rawData = structuredClone(raw);
  const state = {
    rawData,
    baseRaw: structuredClone(raw),
    rows: new Map(),
    muted: new Set(),
    soloed: new Set(),
    variant: null,
    orderIds: syncOrderIds(rawData),
    profile: "standard",
    modified: false,
  };
  for (const slot of allSlots(rawData)) {
    const token = slot.default ?? "random";
    if (token === "off") {
      state.muted.add(slot.id);
      state.rows.set(slot.id, parseToken("random"));
    } else {
      state.rows.set(slot.id, parseToken(token));
    }
  }
  const variants = rawData.variants ?? [];
  if (!variants.length) state.variant = null;
  else if ((rawData.variant_default ?? "") === "off") {
    state.muted.add("@variant");
    state.variant = variants[0].name;
  } else state.variant = rawData.variant_default || variants[0].name;
  return Object.assign(state, patch);
}

const lines = (state) => buildSelectionLines(state).split("\n").filter(Boolean);

// ---------------------------------------------------------------------------

describe("module hygiene", () => {
  test("imports with zero top-level side effects", () => {
    // proven by the trapped import above; this asserts the module actually
    // produced its API rather than silently failing
    assert.equal(typeof util.parseToken, "function");
    assert.equal(typeof util.buildSelectionLines, "function");
  });

  test("source declares nothing but functions and consts", () => {
    const src = readFileSync(fileURLToPath(UTIL_URL), "utf8");
    const topLevel = src
      .split("\n")
      .filter((line) => line.length && !/^\s/.test(line))
      .filter((line) => !line.startsWith("//"));
    for (const line of topLevel) {
      assert.ok(
        line.startsWith("export ") || line === "}",
        `unexpected top-level statement in util.js: ${line}`
      );
    }
  });

  test("source has no imports and no browser/runtime globals", () => {
    const code = readFileSync(fileURLToPath(UTIL_URL), "utf8")
      .split("\n")
      .filter((line) => !line.trim().startsWith("//"))
      .join("\n");
    const banned =
      /\b(document|window|localStorage|sessionStorage|XMLHttpRequest|fetch|setTimeout|setInterval|requestAnimationFrame|addEventListener|registerExtension|alert)\b|\bapp\.|\bapi\.|\brequire\(|^import\s/m;
    const hit = banned.exec(code);
    assert.equal(hit, null, `util.js must stay pure — found '${hit?.[0]}'`);
  });
});

// ---------------------------------------------------------------------------

describe("parseToken", () => {
  test("plain item", () => {
    assert.deepEqual(parseToken("sunset"), { random: false, seed: "", item: "sunset" });
  });

  test("trims surrounding whitespace", () => {
    assert.deepEqual(parseToken("  sunset  "), { random: false, seed: "", item: "sunset" });
  });

  test("random without a seed", () => {
    assert.deepEqual(parseToken("random"), { random: true, seed: "", item: "" });
  });

  test("random with a seed", () => {
    assert.deepEqual(parseToken("random@123"), { random: true, seed: "123", item: "" });
  });

  test("dice-prefixed forms (what the UI shows) parse as random", () => {
    assert.deepEqual(parseToken("🎲 random"), { random: true, seed: "", item: "" });
    assert.deepEqual(parseToken("🎲 random@7"), { random: true, seed: "7", item: "" });
  });

  test("'off' is NOT a token form — it stays a plain item", () => {
    // mutes are recognized by the callers (applyKvToRows), never here
    assert.deepEqual(parseToken("off"), { random: false, seed: "", item: "off" });
  });

  test("blank / nullish input yields an empty item", () => {
    for (const value of ["", "   ", null, undefined]) {
      assert.deepEqual(parseToken(value), { random: false, seed: "", item: "" });
    }
  });

  test("near-misses are items, not random draws", () => {
    assert.equal(parseToken("random@abc").item, "random@abc");
    assert.equal(parseToken("Random").item, "Random"); // case sensitive
    assert.equal(parseToken("randomly").item, "randomly");
    assert.equal(parseToken("random@").item, "random@");
    assert.equal(parseToken("random@-1").item, "random@-1");
  });
});

describe("parseKvLines", () => {
  test("parses key=value lines and trims both sides", () => {
    assert.deepEqual(parseKvLines("  subject = woman \nsetting=street"), {
      subject: "woman",
      setting: "street",
    });
  });

  test("skips blank lines, '#' comments and lines without '='", () => {
    const text = ["# a comment", "", "   ", "not a pair", "subject=woman", "# subject=man"].join(
      "\n"
    );
    assert.deepEqual(parseKvLines(text), { subject: "woman" });
  });

  test("only the FIRST '=' splits; later ones belong to the value", () => {
    assert.deepEqual(parseKvLines("expr=a=b=c"), { expr: "a=b=c" });
  });

  test("a trailing '#' is part of the value, not a comment", () => {
    assert.deepEqual(parseKvLines("subject=woman # not a comment"), {
      subject: "woman # not a comment",
    });
  });

  test("CRLF input survives (the whole line is trimmed)", () => {
    assert.deepEqual(parseKvLines("subject=woman\r\nsetting=street\r\n"), {
      subject: "woman",
      setting: "street",
    });
  });

  test("last duplicate key wins", () => {
    assert.deepEqual(parseKvLines("a=1\na=2"), { a: "2" });
  });

  test("nullish input yields an empty map", () => {
    assert.deepEqual(parseKvLines(null), {});
    assert.deepEqual(parseKvLines(undefined), {});
  });
});

// ---------------------------------------------------------------------------

describe("raw-template shape helpers", () => {
  test("allSlots flattens shared + every variant's slots", () => {
    assert.deepEqual(
      allSlots(withVariants()).map((s) => s.id),
      ["subject", "light", "neon"]
    );
  });

  test("allSlots tolerates a missing template", () => {
    assert.deepEqual(allSlots(null), []);
    assert.deepEqual(allSlots({}), []);
  });

  test("activeSlots adds only the selected variant's slots", () => {
    const raw = withVariants();
    assert.deepEqual(
      activeSlots(raw, "night").map((s) => s.id),
      ["subject", "neon"]
    );
    assert.deepEqual(
      activeSlots(raw, "random").map((s) => s.id),
      ["subject"]
    );
    assert.deepEqual(
      activeSlots(raw, null).map((s) => s.id),
      ["subject"]
    );
    assert.deepEqual(
      activeSlots(raw, "nope").map((s) => s.id),
      ["subject"]
    );
  });

  test("variantSlotIds collects ids across all variants", () => {
    assert.deepEqual([...variantSlotIds(withVariants())], ["light", "neon"]);
    assert.deepEqual([...variantSlotIds({})], []);
  });

  test("syncOrderIds synthesizes file order when 'order' is absent", () => {
    assert.deepEqual(syncOrderIds(flat()), ["subject", "setting", "style"]);
    assert.deepEqual(syncOrderIds(withVariants()), ["subject", "@variant"]);
  });

  test("syncOrderIds drops unknown ids, appends new ones, keeps @variant last", () => {
    const raw = { ...flat(), order: ["style", "gone", "subject"] };
    assert.deepEqual(syncOrderIds(raw), ["style", "subject", "setting"]);
  });

  test("syncOrderIds removes @variant when the template has no variants", () => {
    assert.deepEqual(syncOrderIds({ ...flat(), order: ["subject", "@variant"] }), [
      "subject",
      "setting",
      "style",
    ]);
  });

  test("syncOrderIds appends @variant when variants exist but order omits it", () => {
    const raw = { ...withVariants(), order: ["subject"] };
    assert.deepEqual(syncOrderIds(raw), ["subject", "@variant"]);
  });
});

describe("mute / solo audition", () => {
  const M = (...ids) => new Set(ids);

  test("auditionActive is true as soon as anything is muted or soloed", () => {
    assert.equal(auditionActive(M(), M()), false);
    assert.equal(auditionActive(M("a"), M()), true);
    assert.equal(auditionActive(M(), M("a")), true);
  });

  test("solo wins over mute and silences everything else", () => {
    assert.equal(slotAudible(M("a"), M("a"), "a", false), true);
    assert.equal(slotAudible(M(), M("a"), "b", false), false);
  });

  test("a soloed @variant block carries its variant slots", () => {
    assert.equal(slotAudible(M(), M("@variant"), "light", true), true);
    assert.equal(slotAudible(M(), M("@variant"), "subject", false), false);
  });

  test("a muted @variant block silences its variant slots only", () => {
    assert.equal(slotAudible(M("@variant"), M(), "light", true), false);
    assert.equal(slotAudible(M("@variant"), M(), "subject", false), true);
  });

  test("variantBlockAudible is false without variants", () => {
    assert.equal(variantBlockAudible(flat(), M(), M()), false);
  });

  test("variantBlockAudible follows mute, solo-of-block and solo-of-member", () => {
    const raw = withVariants();
    assert.equal(variantBlockAudible(raw, M(), M()), true);
    assert.equal(variantBlockAudible(raw, M("@variant"), M()), false);
    assert.equal(variantBlockAudible(raw, M(), M("@variant")), true);
    assert.equal(variantBlockAudible(raw, M(), M("light")), true); // a member is soloed
    assert.equal(variantBlockAudible(raw, M(), M("subject")), false); // only shared soloed
  });
});

describe("rowToken", () => {
  const slot = { id: "subject", ref: "people/portrait", default: "woman" };

  test("falls back to the file default when there is no row", () => {
    assert.equal(rowToken(new Map(), slot), "woman");
    assert.equal(rowToken(new Map(), { id: "x", ref: "r" }), "random");
  });

  test("serializes random with and without a seed", () => {
    assert.equal(rowToken(new Map([["subject", parseToken("random")]]), slot), "random");
    assert.equal(rowToken(new Map([["subject", parseToken("random@42")]]), slot), "random@42");
  });

  test("an empty fixed pick degrades to random", () => {
    assert.equal(rowToken(new Map([["subject", parseToken("")]]), slot), "random");
  });
});

// ---------------------------------------------------------------------------

describe("buildSelectionLines", () => {
  test("a freshly loaded template selects nothing (every pick equals its default)", () => {
    assert.equal(buildSelectionLines(loadState(flat())), "");
  });

  test("only picks that differ from the file default are emitted", () => {
    const state = loadState(flat());
    state.rows.set("setting", parseToken("street"));
    assert.deepEqual(lines(state), ["setting=street"]);
  });

  test("a seeded random pick round-trips through parseKvLines", () => {
    const state = loadState(flat());
    state.rows.set("subject", parseToken("random@42"));
    assert.deepEqual(parseKvLines(buildSelectionLines(state)), { subject: "random@42" });
  });

  test("muting a slot emits '<id>=off'", () => {
    const state = loadState(flat());
    state.muted.add("subject");
    assert.deepEqual(lines(state), ["subject=off"]);
  });

  test("a slot whose FILE default is already 'off' needs no line", () => {
    // loadState mutes 'style' because the template bakes it off
    const state = loadState(flat());
    assert.ok(state.muted.has("style"));
    assert.equal(buildSelectionLines(state), "");
  });

  test("solo turns every other slot off", () => {
    const state = loadState(flat());
    state.soloed.add("subject");
    assert.deepEqual(lines(state), ["setting=off"]); // 'style' is already off on disk
  });

  test("the default variant needs no line; another one is named", () => {
    const state = loadState(withVariants());
    assert.equal(buildSelectionLines(state), "");
    state.variant = "night";
    assert.deepEqual(lines(state), ["variant=night"]);
  });

  test("variant slots ride the selection when their variant is active", () => {
    const state = loadState(withVariants());
    state.rows.set("light", parseToken("dusk"));
    assert.deepEqual(lines(state), ["light=dusk"]);
  });

  test("the inactive variant's slots never appear", () => {
    const state = loadState(withVariants());
    state.rows.set("neon", parseToken("pink"));
    assert.equal(buildSelectionLines(state), ""); // 'night' is not the active variant
  });

  test("muting the variant BLOCK emits variant=off and drops its slots", () => {
    const state = loadState(withVariants());
    state.muted.add("@variant");
    state.rows.set("light", parseToken("dusk"));
    assert.deepEqual(lines(state), ["variant=off"]);
  });

  test("a block muted on disk (variant_default 'off') emits no line", () => {
    const raw = { ...withVariants(), variant_default: "off" };
    const state = loadState(raw);
    assert.ok(state.muted.has("@variant"));
    assert.equal(buildSelectionLines(state), "");
  });

  test("an explicit variant pick un-mutes a baked-off default", () => {
    const raw = { ...withVariants(), variant_default: "off" };
    const state = loadState(raw);
    state.muted.delete("@variant");
    state.variant = "day";
    assert.deepEqual(lines(state), ["variant=day"]);
  });

  test("nested pins ride along only once the user touched them", () => {
    const state = loadState(flat());
    state.rows.set("subject.hair", { random: false, seed: "", item: "red" });
    assert.equal(buildSelectionLines(state), "", "untouched nested rows stay out");
    state.rows.get("subject.hair").touched = true;
    assert.deepEqual(lines(state), ["subject.hair=red"]);
  });

  test("touched nested rows serialize random and seeded-random too", () => {
    const state = loadState(flat());
    state.rows.set("subject.hair", { ...parseToken("random@9"), touched: true });
    assert.deepEqual(lines(state), ["subject.hair=random@9"]);
    state.rows.set("subject.hair", { ...parseToken(""), touched: true });
    assert.deepEqual(lines(state), ["subject.hair=random"]);
  });

  test("mute + pick + variant + nested pin all round-trip through parseKvLines", () => {
    const state = loadState(withVariants());
    state.variant = "night";
    state.muted.add("subject");
    state.rows.set("neon.glow", { random: false, seed: "", item: "hot", touched: true });
    assert.deepEqual(parseKvLines(buildSelectionLines(state)), {
      variant: "night",
      subject: "off",
      "neon.glow": "hot",
    });
  });
});

// ---------------------------------------------------------------------------

describe("buildSaveData", () => {
  test("does not mutate the state it reads", () => {
    const state = loadState(flat());
    const before = structuredClone(state.rawData);
    buildSaveData(state);
    assert.deepEqual(state.rawData, before);
  });

  test("bakes current picks as slot defaults", () => {
    const state = loadState(flat());
    state.rows.set("setting", parseToken("street"));
    const data = buildSaveData(state);
    assert.equal(data.slots[1].default, "street");
  });

  test("a random pick drops the default entirely", () => {
    const state = loadState(flat());
    state.rows.set("subject", parseToken("random"));
    assert.equal("default" in buildSaveData(state).slots[0], false);
  });

  test("a seeded random pick is stored verbatim", () => {
    const state = loadState(flat());
    state.rows.set("subject", parseToken("random@42"));
    assert.equal(buildSaveData(state).slots[0].default, "random@42");
  });

  test("muted slots bake as default 'off' (Apply persists what you see)", () => {
    const state = loadState(flat());
    state.muted.add("subject");
    assert.equal(buildSaveData(state).slots[0].default, "off");
  });

  test("a muted variant BLOCK rides variant_default, not per-slot 'off'", () => {
    const state = loadState(withVariants());
    state.muted.add("@variant");
    const data = buildSaveData(state);
    assert.equal(data.variant_default, "off");
    assert.equal(data.variants[0].slots[0].default, "noon"); // untouched
  });

  test("the active variant is baked as variant_default", () => {
    const state = loadState(withVariants());
    state.variant = "night";
    assert.equal(buildSaveData(state).variant_default, "night");
  });

  test("variant_default: an untouched first variant is NOT stamped", () => {
    // The file ships without a variant_default and the user changed nothing,
    // so stamping "day" would only restate what "" already means — and under a
    // profile that phantom value becomes a stored overrides block.
    const state = loadState(withVariants());
    assert.equal(state.variant, "day"); // the fallback, not a choice
    assert.equal("variant_default" in buildSaveData(state), false);
  });

  test("variant_default: a literal in the file is always re-stated", () => {
    // THE TRAP: a template deliberately saved as "night" must not fall back to
    // the first variant just because the user never touched the dropdown.
    const raw = { ...withVariants(), variant_default: "night" };
    const kept = loadState(raw);
    assert.equal(kept.variant, "night");
    assert.equal(buildSaveData(kept).variant_default, "night");

    // …and switching such a template BACK to the first variant must overwrite
    // the literal rather than silently keeping "night".
    const switched = loadState(raw);
    switched.variant = "day";
    assert.equal(buildSaveData(switched).variant_default, "day");
  });

  test("variant_default: un-muting a block baked 'off' stamps the active variant", () => {
    const raw = { ...withVariants(), variant_default: "off" };
    const state = loadState(raw);
    state.muted.delete("@variant"); // the user un-muted the block
    assert.equal(buildSaveData(state).variant_default, "day");
  });

  test("slots absent from rows keep their file default", () => {
    const state = loadState(flat());
    state.rows.delete("subject");
    assert.equal(buildSaveData(state).slots[0].default, "woman");
  });

  test("'order' is dropped when it matches file order and kept when it does not", () => {
    const state = loadState(flat());
    assert.equal("order" in buildSaveData(state), false);
    state.orderIds = ["style", "subject", "setting"];
    assert.deepEqual(buildSaveData(state).order, ["style", "subject", "setting"]);
  });

  test("the synthesized order of a variant template ends with @variant", () => {
    const state = loadState(withVariants());
    assert.equal("order" in buildSaveData(state), false);
    assert.deepEqual(state.orderIds, ["subject", "@variant"]);
  });

  test("empty prose fields and empty slot labels are removed; version is stamped", () => {
    const raw = { ...flat(), prefix: "", suffix: "", negative: "", description: "" };
    raw.slots = raw.slots.map((s) => ({ ...s, label: "" }));
    const data = buildSaveData(loadState(raw));
    for (const key of ["prefix", "suffix", "negative", "description"]) {
      assert.equal(key in data, false, `${key} should be dropped when empty`);
    }
    assert.equal("label" in data.slots[0], false);
    assert.equal(data.version, 1);
  });

  test("non-empty prose survives", () => {
    const data = buildSaveData(loadState(flat()));
    assert.equal(data.prefix, "a photo of");
    assert.equal(data.negative, "blurry");
  });
});

describe("buildDraftData", () => {
  test("carries the working order and leaves defaults alone", () => {
    const state = loadState(flat());
    state.rows.set("subject", parseToken("man")); // picks travel as selection lines
    const draft = buildDraftData(state);
    assert.deepEqual(draft.order, ["subject", "setting", "style"]);
    assert.equal(draft.slots[0].default, "woman");
  });

  test("strips the ACTIVE profile's overrides (the working copy already has them)", () => {
    const raw = {
      ...flat(),
      profiles: { krea2: { overrides: { prefix: "x" } }, sdxl: { overrides: { prefix: "y" } } },
    };
    const state = loadState(raw, { profile: "krea2" });
    const draft = buildDraftData(state);
    assert.equal("overrides" in draft.profiles.krea2, false);
    assert.deepEqual(draft.profiles.sdxl.overrides, { prefix: "y" });
  });

  test("keeps every override under the standard profile", () => {
    const raw = { ...flat(), profiles: { krea2: { overrides: { prefix: "x" } } } };
    const draft = buildDraftData(loadState(raw));
    assert.deepEqual(draft.profiles.krea2.overrides, { prefix: "x" });
  });
});

// ---------------------------------------------------------------------------

describe("appliedStateDiffers", () => {
  test("a freshly loaded template needs no save (including its baked-off slot)", () => {
    assert.equal(appliedStateDiffers(loadState(flat())), false);
    assert.equal(appliedStateDiffers(loadState(withVariants())), false);
  });

  test("an edited working copy always needs a save", () => {
    assert.equal(appliedStateDiffers(loadState(flat(), { modified: true })), true);
  });

  test("a changed pick, a mute and a changed variant each need a save", () => {
    const picked = loadState(flat());
    picked.rows.set("subject", parseToken("man"));
    assert.equal(appliedStateDiffers(picked), true);

    const muted = loadState(flat());
    muted.muted.add("subject");
    assert.equal(appliedStateDiffers(muted), true);

    const variant = loadState(withVariants());
    variant.variant = "night";
    assert.equal(appliedStateDiffers(variant), true);
  });

  test("un-muting a slot the file bakes off needs a save", () => {
    const state = loadState(flat());
    state.muted.delete("style");
    assert.equal(appliedStateDiffers(state), true);
  });
});

// ---------------------------------------------------------------------------

describe("profile overrides", () => {
  const based = () => ({
    ...flat(),
    profiles: {
      krea2: {
        overrides: {
          prefix: "cinematic still of",
          slots: { subject: { default: "man" }, style: { default: "random" } },
        },
      },
    },
  });

  test("overridesFor ignores the standard profile and unknown names", () => {
    assert.equal(overridesFor("standard", based()), null);
    assert.equal(overridesFor(null, based()), null);
    assert.equal(overridesFor("sdxl", based()), null);
    assert.equal(overridesFor("krea2", null), null);
    assert.equal(overridesFor("krea2", based()).prefix, "cinematic still of");
  });

  test("overrideTweakCount counts scalars plus per-slot entries", () => {
    assert.equal(overrideTweakCount(null), 0);
    assert.equal(overrideTweakCount({}), 0);
    assert.equal(overrideTweakCount({ prefix: "x" }), 1);
    assert.equal(overrideTweakCount({ negative: "" }), 1); // presence counts, not truthiness
    assert.equal(overrideTweakCount({ prefix: "x", slots: { a: {}, b: {} } }), 3);
  });

  test("effectiveRaw applies scalars and per-slot default/emphasis", () => {
    const eff = effectiveRaw("krea2", based());
    assert.equal(eff.prefix, "cinematic still of");
    assert.equal(eff.slots[0].default, "man");
    assert.equal("default" in eff.slots[2], false, "'random' clears the default");
    assert.equal(eff.suffix, "high detail", "untouched scalars survive");
  });

  test("effectiveRaw with no override is a detached copy of the base", () => {
    const base = based();
    const eff = effectiveRaw("standard", base);
    assert.deepEqual(eff, base);
    eff.slots[0].default = "mutated";
    assert.equal(base.slots[0].default, "woman");
  });

  test("effectiveRaw clears emphasis on null and sets it otherwise", () => {
    const base = { ...flat(), profiles: { p: { overrides: { slots: { subject: {} } } } } };
    base.slots[0].emphasis = 1.2;
    base.profiles.p.overrides.slots.subject = { emphasis: null };
    assert.equal("emphasis" in effectiveRaw("p", base).slots[0], false);
    base.profiles.p.overrides.slots.subject = { emphasis: 1.4 };
    assert.equal(effectiveRaw("p", base).slots[0].emphasis, 1.4);
  });

  test("effectiveRaw reaches into variant slots", () => {
    const base = {
      ...withVariants(),
      profiles: { p: { overrides: { slots: { light: { default: "dusk" } } } } },
    };
    assert.equal(effectiveRaw("p", base).variants[0].slots[0].default, "dusk");
  });
});

describe("diffProfileOverrides", () => {
  test("EMPTY DIFF: an unchanged template yields null (no fork of the factory file)", () => {
    const base = flat();
    assert.equal(diffProfileOverrides(structuredClone(base), base), null);
  });

  test("EMPTY DIFF survives the save-shaping round trip for a flat template", () => {
    // load → save → diff must still be null, or every Apply under a profile
    // would write an overrides block that says nothing
    const base = flat();
    assert.equal(diffProfileOverrides(buildSaveData(loadState(base)), base), null);
  });

  test("EMPTY DIFF survives the round trip for a VARIANT template too", () => {
    // Was pinned as "NOT an empty diff": buildSaveData used to stamp
    // variant_default unconditionally, so an untouched variant template saved
    // under a profile forked {variant_default: "<first variant>"} — a phantom
    // '✎ 1' tweak chip that also defeated the "now matches standard → stored
    // tweaks removed" cleanup. The stamp is now conditional; see the
    // buildSaveData › variant_default tests for what still gets stamped.
    const base = withVariants();
    assert.equal(diffProfileOverrides(buildSaveData(loadState(base)), base), null);
  });

  test("scalar edits are captured, including clearing one to ''", () => {
    const base = flat();
    const eff = structuredClone(base);
    eff.prefix = "cinematic still of";
    eff.negative = "";
    assert.deepEqual(diffProfileOverrides(eff, base), {
      prefix: "cinematic still of",
      negative: "",
    });
  });

  test("a missing scalar equals an empty one", () => {
    const base = { ...flat(), suffix: "" };
    const eff = structuredClone(base);
    delete eff.suffix;
    assert.equal(diffProfileOverrides(eff, base), null);
  });

  test("slot default and emphasis edits land under 'slots'", () => {
    const base = flat();
    const eff = structuredClone(base);
    eff.slots[0].default = "man";
    eff.slots[1].emphasis = 1.3;
    assert.deepEqual(diffProfileOverrides(eff, base), {
      slots: { subject: { default: "man" }, setting: { emphasis: 1.3 } },
    });
  });

  test("a cleared default/emphasis is recorded as 'random' / null", () => {
    const base = flat();
    base.slots[1].emphasis = 1.3;
    const eff = structuredClone(base);
    delete eff.slots[0].default;
    delete eff.slots[1].emphasis;
    assert.deepEqual(diffProfileOverrides(eff, base), {
      slots: { subject: { default: "random" }, setting: { emphasis: null } },
    });
  });

  test("an explicit 'random' default equals an absent one", () => {
    const base = flat();
    const eff = structuredClone(base);
    eff.slots[1].default = "random"; // base has none
    assert.equal(diffProfileOverrides(eff, base), null);
  });

  test("slots the base does not have are ignored (structure belongs to the base)", () => {
    const base = flat();
    const eff = structuredClone(base);
    eff.slots.push({ id: "extra", ref: "x/y", default: "z" });
    assert.equal(diffProfileOverrides(eff, base), null);
  });

  test("variant slots diff like shared ones", () => {
    const base = withVariants();
    const eff = structuredClone(base);
    eff.variants[1].slots[0].default = "pink";
    assert.deepEqual(diffProfileOverrides(eff, base), { slots: { neon: { default: "pink" } } });
  });

  test("effectiveRaw → diffProfileOverrides round-trips a stored override set", () => {
    const base = {
      ...flat(),
      profiles: {
        krea2: {
          overrides: {
            prefix: "cinematic still of",
            slots: { subject: { default: "man" }, style: { default: "random" } },
          },
        },
      },
    };
    const stored = base.profiles.krea2.overrides;
    assert.deepEqual(diffProfileOverrides(effectiveRaw("krea2", base), base), stored);
  });
});

describe("structuralDrift", () => {
  test("false for an unchanged template and for its save-shaped copy", () => {
    const base = flat();
    assert.equal(structuralDrift(structuredClone(base), base), false);
    assert.equal(structuralDrift(buildSaveData(loadState(base)), base), false);
  });

  test("picks, mutes and prose never count as structural", () => {
    const base = flat();
    const state = loadState(base);
    state.rows.set("subject", parseToken("man"));
    state.muted.add("setting");
    state.rawData.prefix = "totally different";
    assert.equal(structuralDrift(buildSaveData(state), base), false);
  });

  test("adding, removing, reordering or re-labelling slots counts", () => {
    const base = flat();
    const added = structuredClone(base);
    added.slots.push({ id: "extra", ref: "x/y" });
    assert.equal(structuralDrift(added, base), true);

    const removed = structuredClone(base);
    removed.slots.pop();
    assert.equal(structuralDrift(removed, base), true);

    const reordered = structuredClone(base);
    reordered.order = ["style", "subject", "setting"];
    assert.equal(structuralDrift(reordered, base), true);

    const relabelled = structuredClone(base);
    relabelled.slots[0].label = "Who";
    assert.equal(structuralDrift(relabelled, base), true);

    const reffed = structuredClone(base);
    reffed.slots[0].ref = "people/other";
    assert.equal(structuralDrift(reffed, base), true);
  });

  test("template label/type and variant structure count", () => {
    const base = withVariants();
    const renamed = structuredClone(base);
    renamed.label = "Something else";
    assert.equal(structuralDrift(renamed, base), true);

    const typed = structuredClone(base);
    typed.type = ["photo"];
    assert.equal(structuralDrift(typed, base), true);

    const dropped = structuredClone(base);
    dropped.variants.pop();
    assert.equal(structuralDrift(dropped, base), true);
  });

  test("an explicit order equal to the synthesized one is not drift", () => {
    const base = flat();
    const same = structuredClone(base);
    same.order = ["subject", "setting", "style"];
    assert.equal(structuralDrift(same, base), false);
  });
});

// ---------------------------------------------------------------------------

describe("missing-LoRA helpers", () => {
  test("loraKey normalizes separators and case", () => {
    assert.equal(loraKey("Style\\Neon.safetensors"), "style/neon.safetensors");
    assert.equal(loraKey(null), "");
  });

  test("missingLoraRows groups by file and keeps every use", () => {
    const status = {
      loras: [
        { file: "a\\x.safetensors", present: false, air: "urn:air:1", item: "one" },
        { file: "A/X.safetensors", present: false, air: "urn:air:1", item: "two" },
        { file: "b.safetensors", present: true },
        { file: "c.safetensors", present: false },
      ],
    };
    const rows = missingLoraRows(status);
    assert.equal(rows.length, 2);
    assert.equal(rows[0].item, "one", "the first entry supplies the row's fields");
    assert.deepEqual(
      rows[0].uses.map((u) => u.item),
      ["one", "two"]
    );
    assert.equal(rows[1].file, "c.safetensors");
  });

  test("missingLoraRows tolerates a missing status body", () => {
    assert.deepEqual(missingLoraRows(null), []);
    assert.deepEqual(missingLoraRows({}), []);
  });

  test("downloadableAir accepts only urn:air: values, verbatim", () => {
    assert.equal(downloadableAir({ air: " urn:air:sdxl:lora:civitai:1@2 " }), "urn:air:sdxl:lora:civitai:1@2");
    assert.equal(downloadableAir({ air: "URN:AIR:x" }), "URN:AIR:x", "case is preserved");
    assert.equal(downloadableAir({ air: "https://civitai.com/x" }), "");
    assert.equal(downloadableAir({}), "");
  });

  test("loraProgressText reports MB with and without a known total", () => {
    assert.equal(loraProgressText({ loaded: 1 << 20, total: 4 << 20 }), "1 / 4 MB");
    assert.equal(loraProgressText({ loaded: 3 << 20 }), "3 MB");
    assert.equal(loraProgressText({}), "0 MB");
  });
});

describe("de-compose helpers", () => {
  test("jsSlugify lowercases, collapses runs and trims dashes", () => {
    assert.equal(jsSlugify("Hello, World!"), "hello-world");
    assert.equal(jsSlugify("  --Neon  Nights--  "), "neon-nights");
  });

  test("jsSlugify drops non-ASCII and never returns an empty slug", () => {
    assert.equal(jsSlugify("Über cool"), "ber-cool");
    assert.equal(jsSlugify("   "), "item");
    assert.equal(jsSlugify(null), "item");
  });

  test("jsSlugify truncates and re-trims the cut", () => {
    assert.equal(jsSlugify("aaaa bbbb", 5), "aaaa");
    assert.equal(jsSlugify("abcdefghij", 4), "abcd");
  });

  test("defaultPlan slots a matched fragment", () => {
    const frs = [{ match: { section: "s", item: "i" } }];
    assert.deepEqual(defaultPlan(frs[0], 0, frs), { action: "slot", include: true });
  });

  test("defaultPlan proposes a new item when the suggestion is strong enough", () => {
    const fr = { suggestion: { section: "styles/photo", score: 0.5 } };
    assert.deepEqual(defaultPlan(fr, 0, [fr]), {
      action: "new-item",
      section: "styles/photo",
      include: true,
    });
    const weak = { suggestion: { section: "styles/photo", score: 0.29 } };
    assert.equal(defaultPlan(weak, 0, [weak]).action, "prefix");
  });

  test("unmatched fragments become prefix/suffix around the matched span", () => {
    const frs = [{}, { match: {} }, {}];
    assert.equal(defaultPlan(frs[0], 0, frs).action, "prefix");
    assert.equal(defaultPlan(frs[2], 2, frs).action, "suffix");
  });

  test("an unmatched fragment BETWEEN matches is skipped", () => {
    const frs = [{ match: {} }, {}, { match: {} }];
    assert.deepEqual(defaultPlan(frs[1], 1, frs), { action: "skip", include: false });
  });

  test("with no match anywhere everything is prefix", () => {
    const frs = [{}, {}];
    assert.equal(defaultPlan(frs[1], 1, frs).action, "prefix");
  });
});

describe("combine sections", () => {
  test("combineItem builds a delegating item named from the last slug parts", () => {
    assert.deepEqual(combineItem("places/urban/street", 1), {
      name: "urban-street",
      text: "{pick}",
      slots: [{ id: "pick", ref: "places/urban/street" }],
    });
  });

  test("a non-unit weight is carried, weight 1 is not", () => {
    assert.equal(combineItem("a/b", 2).weight, 2);
    assert.equal("weight" in combineItem("a/b", 1), false);
    assert.equal("weight" in combineItem("a/b", 0), false);
  });

  test("isCombineItem recognizes exactly the generated shape", () => {
    assert.equal(isCombineItem(combineItem("a/b", 1)), true);
    assert.equal(isCombineItem({ text: " {pick} ", slots: [{ id: "pick", ref: "a" }] }), true);
    assert.equal(isCombineItem({ text: "a {pick}", slots: [{ id: "pick", ref: "a" }] }), false);
    assert.equal(isCombineItem({ text: "{pick}", slots: [{ id: "other", ref: "a" }] }), false);
    assert.equal(isCombineItem({ text: "{pick}", slots: [] }), false);
    assert.equal(isCombineItem(null), false);
  });
});

describe("section editor: itemRowEdited", () => {
  // The gate for the thin extend diff — a factory-origin row is only written
  // into the user file when this says the row changed, so anything it cannot
  // see is an edit that gets silently dropped on save.
  const item = { name: "neon", text: "neon glow", weight: 2 };
  const row = (patch = {}) => ({ name: "neon", text: "neon glow", weight: "2", ...patch });

  test("an untouched row is not edited", () => {
    assert.equal(itemRowEdited(row(), item), false);
  });

  test("name, text and weight changes are caught (weight 1 == absent)", () => {
    assert.equal(itemRowEdited(row({ name: "neon2" }), item), true);
    assert.equal(itemRowEdited(row({ text: "x" }), item), true);
    assert.equal(itemRowEdited(row({ weight: "3" }), item), true);
    assert.equal(itemRowEdited({ name: "a", text: "b", weight: "" }, { name: "a", text: "b" }), false);
    assert.equal(itemRowEdited({ name: "a", text: "b", weight: "1" }, { name: "a", text: "b" }), false);
  });

  test("surrounding whitespace in the name does not count as an edit", () => {
    assert.equal(itemRowEdited(row({ name: "  neon  " }), item), false);
  });

  test("child slots are compared (adding one via '{' is an edit)", () => {
    const withSlot = { ...item, slots: [{ id: "pick", ref: "a/b" }] };
    assert.equal(itemRowEdited(row({ slots: [{ id: "pick", ref: "a/b" }] }), withSlot), false);
    assert.equal(itemRowEdited(row({ slots: [] }), withSlot), true);
    assert.equal(itemRowEdited(row({ slots: [{ id: "pick", ref: "a/c" }] }), withSlot), true);
  });

  test("every LoRA field counts — file, strengths, comment and base", () => {
    const lora = {
      name: "neon",
      text: "neon glow",
      data: { lora: "x.safetensors", strength_model: 1, strength_clip: 1 },
    };
    const loraRow = (patch = {}) => ({
      name: "neon",
      text: "neon glow",
      weight: "",
      lora: "x.safetensors",
      sm: "1",
      sc: "1",
      comment: "",
      base: "",
      ...patch,
    });
    assert.equal(itemRowEdited(loraRow(), lora), false);
    assert.equal(itemRowEdited(loraRow({ lora: "y.safetensors" }), lora), true);
    assert.equal(itemRowEdited(loraRow({ sm: "0.8" }), lora), true);
    assert.equal(itemRowEdited(loraRow({ sc: "0.5" }), lora), true);
    assert.equal(itemRowEdited(loraRow({ comment: "urn:air:sdxl:lora:civitai:1@2" }), lora), true);
    assert.equal(itemRowEdited(loraRow({ base: "sdxl" }), lora), true);
    assert.equal(itemRowEdited(loraRow({ base: "SDXL" }), { ...lora, data: { ...lora.data, base: "sdxl" } }), false);
  });

  test("strength_clip defaults to strength_model on both sides", () => {
    const lora = { name: "n", text: "t", data: { lora: "x", strength_model: 0.7 } };
    const base = { name: "n", text: "t", weight: "", lora: "x", sm: "0.7", comment: "", base: "" };
    assert.equal(itemRowEdited({ ...base, sc: "" }, lora), false);
    assert.equal(itemRowEdited({ ...base, sc: "0.7" }, lora), false);
    assert.equal(itemRowEdited({ ...base, sc: "0.9" }, lora), true);
  });

  test("a row without a LoRA editor never inspects the LoRA fields", () => {
    const lora = { name: "n", text: "t", data: { lora: "x", strength_model: 1 } };
    assert.equal(itemRowEdited({ name: "n", text: "t", weight: "" }, lora), false);
  });
});

describe("misc", () => {
  test("uniqueName appends -2, -3 … only on collision", () => {
    assert.equal(uniqueName("neon", new Set()), "neon");
    assert.equal(uniqueName("neon", new Set(["neon"])), "neon-2");
    assert.equal(uniqueName("neon", new Set(["neon", "neon-2", "neon-3"])), "neon-4");
    assert.equal(uniqueName("neon", new Set(["other"])), "neon");
  });

  test("moveInArray moves an element in place", () => {
    const arr = ["a", "b", "c", "d"];
    moveInArray(arr, 0, 2);
    assert.deepEqual(arr, ["b", "c", "a", "d"]);
    moveInArray(arr, 3, 0);
    assert.deepEqual(arr, ["d", "b", "c", "a"]);
  });

  test("bundleFilename flattens slug folders and falls back to 'bundle'", () => {
    assert.equal(bundleFilename("my/street-scene"), "my--street-scene.mrln.json");
    assert.equal(bundleFilename("flat"), "flat.mrln.json");
    assert.equal(bundleFilename(""), "bundle.mrln.json");
    assert.equal(bundleFilename(null), "bundle.mrln.json");
  });
});
