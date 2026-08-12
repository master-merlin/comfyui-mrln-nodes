// Multiple trigger words per LoRA — the EDITOR half (SPEC 4.3).
//
// web/js/composer/util.js mirrors three functions of mrln/promptapi/lora.py
// (split_catchword / render_catchword / trigger_selection) so the section
// editor can draw its chips per keystroke without a round trip. The cases in
// the first half below are deliberately the same cases
// tests/test_prompt_trigger_words.py asserts server-side, with the same
// fixture words — that pairing IS the drift alarm. If one side changes, the
// two files disagree in a diff a human can read.
//
// What a drift would cost is display-only: the file's `catchword` is what
// renders, and only the server renders it, so a disagreement shows a wrong
// CHIP and never a wrong prompt (see the header comment in util.js).
//
// The second half covers what has NO server twin: what the M/S buttons do.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

const {
  CATCHWORD_JOINER,
  cleanTriggerWords,
  dropTriggerWord,
  itemRowEdited,
  muteTriggerWord,
  renderCatchword,
  soloTriggerWord,
  splitCatchword,
  triggerSelection,
  triggerSoloed,
} = await import(new URL("../../web/js/composer/util.js", import.meta.url).href);

// the exact fixture tests/test_prompt_trigger_words.py uses
const WORDS = ["CarKit", "wide body", "carbon fibre"];

// ---------------------------------------------------------------------------
// mirrored from the server twin
// ---------------------------------------------------------------------------

describe("splitCatchword (server twin: split_catchword)", () => {
  test("tolerates spacing and empties", () => {
    assert.deepEqual(splitCatchword(" a ,  b ,, c "), ["a", "b", "c"]);
    assert.deepEqual(splitCatchword(""), []);
    assert.deepEqual(splitCatchword(null), []);
    assert.deepEqual(splitCatchword(undefined), []);
  });

  test("the joiner round-trips its own output", () => {
    assert.equal(CATCHWORD_JOINER, ", ");
    assert.deepEqual(splitCatchword(WORDS.join(CATCHWORD_JOINER)), WORDS);
  });
});

describe("cleanTriggerWords", () => {
  test("trims, drops blanks and tolerates a missing list", () => {
    assert.deepEqual(cleanTriggerWords([" CarKit ", "wide body", "  ", ""]), [
      "CarKit",
      "wide body",
    ]);
    assert.deepEqual(cleanTriggerWords(null), []);
    assert.deepEqual(cleanTriggerWords([null, undefined]), []);
  });
});

describe("renderCatchword (server twin: render_catchword)", () => {
  test("the default selection is the first word and nothing else", () => {
    // back-compat: exactly what the Civitai lookup has always written
    assert.equal(renderCatchword(WORDS, [WORDS[0]]), "CarKit");
    assert.equal(renderCatchword([], []), "");
  });

  test("a multi-selection renders in PROVENANCE order, not click order", () => {
    assert.equal(renderCatchword(WORDS, ["carbon fibre", "CarKit"]), "CarKit, carbon fibre");
    assert.equal(renderCatchword(WORDS, WORDS), "CarKit, wide body, carbon fibre");
    assert.equal(renderCatchword(WORDS, [...WORDS].reverse()), "CarKit, wide body, carbon fibre");
  });

  test("all muted renders nothing", () => {
    assert.equal(renderCatchword(WORDS, []), "");
  });

  test("free text is kept and appended AFTER the known words", () => {
    assert.equal(renderCatchword(WORDS, ["my own phrase", "CarKit"]), "CarKit, my own phrase");
  });

  test("free text keeps the order it was given in", () => {
    assert.equal(renderCatchword(WORDS, ["b phrase", "a phrase"]), "b phrase, a phrase");
  });

  test("selection matching ignores case but renders the provenance spelling", () => {
    assert.equal(renderCatchword(WORDS, ["carkit"]), "CarKit");
  });

  test("a duplicate free-text word is emitted once", () => {
    assert.equal(renderCatchword(WORDS, ["mine", "MINE"]), "mine");
  });

  test("blank and nullish input is ignored on both sides", () => {
    assert.equal(renderCatchword(WORDS, ["  ", null]), "");
    assert.equal(renderCatchword(null, null), "");
  });
});

describe("triggerSelection (server twin: trigger_selection)", () => {
  test("the state is a set difference over the two stored fields", () => {
    const state = triggerSelection(WORDS, "CarKit, carbon fibre, my own phrase");
    assert.deepEqual(state.words, WORDS);
    assert.deepEqual(state.active, ["CarKit", "carbon fibre"]);
    assert.deepEqual(state.muted, ["wide body"]);
    assert.deepEqual(state.extra, ["my own phrase"]); // user-added, no provenance entry
    assert.equal(state.catchword, "CarKit, carbon fibre, my own phrase");
  });

  test("all muted: every word muted, nothing active, no extras", () => {
    const state = triggerSelection(WORDS, "");
    assert.deepEqual(state.active, []);
    assert.deepEqual(state.muted, WORDS);
    assert.deepEqual(state.extra, []);
    assert.equal(state.catchword, "");
  });

  test("mute is absence, and soloing one word IS muting the other two", () => {
    const solo = renderCatchword(WORDS, ["wide body"]);
    assert.equal(solo, "wide body");
    assert.equal(
      solo,
      renderCatchword(
        WORDS,
        WORDS.filter((w) => w === "wide body")
      )
    );
    const state = triggerSelection(WORDS, solo);
    assert.deepEqual(state.active, ["wide body"]);
    assert.deepEqual(state.muted, ["CarKit", "carbon fibre"]);
  });

  test("case-insensitive: a lowercased catchword still names the provenance word", () => {
    assert.deepEqual(triggerSelection(WORDS, "carkit").active, ["CarKit"]);
    assert.deepEqual(triggerSelection(WORDS, "carkit").extra, []);
  });

  test("provenance is echoed trimmed, and never edited by a selection", () => {
    const state = triggerSelection([" CarKit ", "wide body", "  "], "CarKit");
    assert.deepEqual(state.words, ["CarKit", "wide body"]);
  });

  test("without provenance every rendered word is free text", () => {
    const state = triggerSelection([], "anything, at all");
    assert.deepEqual(state.words, []);
    assert.deepEqual(state.active, []);
    assert.deepEqual(state.extra, ["anything", "at all"]);
  });
});

describe("save -> reload -> re-derive (the FILE is the state)", () => {
  // mirrors test_mute_solo_round_trips_through_save_reload_rederive: the only
  // thing an edit writes is the item's text; provenance is untouched.
  const item = (text) => ({
    name: "carkit",
    text,
    data: { lora: "kits/CarKit.safetensors", lora_info: { trained_words: [...WORDS] } },
  });
  const reopen = (stored) => triggerSelection(stored.data.lora_info.trained_words, stored.text);

  test("an untouched item re-derives 'first word active, rest muted'", () => {
    const state = reopen(item("CarKit"));
    assert.deepEqual(state.active, ["CarKit"]);
    assert.deepEqual(state.muted, ["wide body", "carbon fibre"]);
  });

  test("a solo survives the round trip with no new schema field", () => {
    const stored = item(soloTriggerWord(WORDS, "CarKit", "wide body"));
    assert.equal(stored.text, "wide body");
    const state = reopen(stored);
    assert.deepEqual(state.active, ["wide body"]);
    assert.deepEqual(state.muted, ["CarKit", "carbon fibre"]);
    assert.equal(triggerSoloed(state, "wide body"), true, "solo re-derives, it is not stored");
    assert.deepEqual(stored.data.lora_info.trained_words, WORDS, "provenance untouched");
  });

  test("a user-added word survives a reload", () => {
    const stored = item(renderCatchword(WORDS, ["CarKit", "shot on Portra"]));
    assert.equal(stored.text, "CarKit, shot on Portra");
    const state = reopen(stored);
    assert.deepEqual(state.extra, ["shot on Portra"]);
    assert.deepEqual(state.active, ["CarKit"]);
  });
});

// ---------------------------------------------------------------------------
// editor-only: what the M/S buttons on a chip actually do
// ---------------------------------------------------------------------------

describe("triggerSoloed", () => {
  test("true only for the single surviving trained word", () => {
    const alone = triggerSelection(WORDS, "wide body");
    assert.equal(triggerSoloed(alone, "wide body"), true);
    assert.equal(triggerSoloed(alone, "CarKit"), false);
    assert.equal(triggerSoloed(triggerSelection(WORDS, "CarKit, wide body"), "CarKit"), false);
    assert.equal(triggerSoloed(triggerSelection(WORDS, ""), "CarKit"), false);
  });

  test("free text alongside a lone word does NOT cancel the solo", () => {
    // extras are typed text, not pool members: the solo is about the pool
    const state = triggerSelection(WORDS, "wide body, my own phrase");
    assert.equal(triggerSoloed(state, "wide body"), true);
  });

  test("a one-word LoRA is never 'soloed' (there is nothing to solo against)", () => {
    assert.equal(triggerSoloed(triggerSelection(["only"], "only"), "only"), false);
  });
});

describe("muteTriggerWord (the M button on a provenance chip)", () => {
  test("mutes and un-mutes, always re-rendering in provenance order", () => {
    const muted = muteTriggerWord(WORDS, "CarKit, wide body", "CarKit");
    assert.equal(muted, "wide body");
    assert.equal(muteTriggerWord(WORDS, muted, "CarKit"), "CarKit, wide body");
  });

  test("un-muting inserts at the provenance position, not at the end", () => {
    assert.equal(muteTriggerWord(WORDS, "carbon fibre", "wide body"), "wide body, carbon fibre");
  });

  test("muting the last word is allowed and yields an empty catchword", () => {
    // legal as a SELECTION; the editor blocks the save separately, because
    // promptlib's schema rejects an item without non-empty text
    assert.equal(muteTriggerWord(WORDS, "CarKit", "CarKit"), "");
  });

  test("free text is carried through untouched", () => {
    const next = muteTriggerWord(WORDS, "CarKit, my own phrase", "CarKit");
    assert.equal(next, "my own phrase");
    assert.equal(muteTriggerWord(WORDS, next, "wide body"), "wide body, my own phrase");
  });

  test("case-insensitive, and a word that is not provenance is a no-op", () => {
    assert.equal(muteTriggerWord(WORDS, "CarKit", "carkit"), "");
    assert.equal(muteTriggerWord(WORDS, "CarKit, mine", "mine"), "CarKit, mine");
  });
});

describe("soloTriggerWord (the S button on a provenance chip)", () => {
  test("solo mutes every OTHER trained word", () => {
    assert.equal(soloTriggerWord(WORDS, "CarKit, wide body", "wide body"), "wide body");
    assert.equal(soloTriggerWord(WORDS, "", "carbon fibre"), "carbon fibre");
  });

  test("clicking S on the word that is already alone un-mutes them all", () => {
    // there is no remembered mute set to restore — the file is the only state
    assert.equal(soloTriggerWord(WORDS, "wide body", "wide body"), WORDS.join(", "));
  });

  test("SPEC DIVERGENCE: solo does not equal 'mute everything else' with free text", () => {
    // Muting each other chip one by one would REMOVE the user's typed word
    // (an extra has no provenance entry to fall back to), so the two paths do
    // NOT collapse to the same persisted state. Solo keeps extras, because a
    // solo that destroys typed text could not be undone — and reversibility is
    // what M/S means everywhere else in this system.
    const withExtra = "CarKit, wide body, my own phrase";
    assert.equal(soloTriggerWord(WORDS, withExtra, "CarKit"), "CarKit, my own phrase");

    // the same intent clicked chip by chip: mute the other trained word, then
    // the only way to silence the typed one — remove it, unrecoverably
    let manual = muteTriggerWord(WORDS, withExtra, "wide body");
    manual = dropTriggerWord(manual, "my own phrase");
    assert.equal(manual, "CarKit");
    assert.notEqual(soloTriggerWord(WORDS, withExtra, "CarKit"), manual);
  });

  test("with no free text solo IS exactly 'mute every other word'", () => {
    let manual = WORDS.join(", ");
    for (const word of ["CarKit", "carbon fibre"]) manual = muteTriggerWord(WORDS, manual, word);
    assert.equal(soloTriggerWord(WORDS, WORDS.join(", "), "wide body"), manual);
  });

  test("solo re-derives as soloed, so the S button lights up after a reload", () => {
    const text = soloTriggerWord(WORDS, WORDS.join(", "), "carbon fibre");
    assert.equal(triggerSoloed(triggerSelection(WORDS, text), "carbon fibre"), true);
  });
});

describe("dropTriggerWord (the M button on a user-added chip)", () => {
  test("removes the word and keeps the rest in written order", () => {
    assert.equal(dropTriggerWord("CarKit, mine, wide body", "mine"), "CarKit, wide body");
    assert.equal(dropTriggerWord("mine", "mine"), "");
  });

  test("case-insensitive, and unknown words leave the text alone", () => {
    assert.equal(dropTriggerWord("CarKit, Mine", "mine"), "CarKit");
    assert.equal(dropTriggerWord("CarKit", "nope"), "CarKit");
  });

  test("removal is irreversible — which is why the editor arms this button", () => {
    const gone = dropTriggerWord("CarKit, my own phrase", "my own phrase");
    assert.deepEqual(triggerSelection(WORDS, gone).extra, []);
  });
});

// ---------------------------------------------------------------------------

describe("itemRowEdited sees trigger-word provenance", () => {
  // The gate for the thin extend diff. '⟳ words' changes lora_info and NOTHING
  // else on the row, so without this a refreshed factory row would be dropped
  // from the diff and its chips would be gone on the next open.
  const item = {
    name: "carkit",
    text: "CarKit",
    data: { lora: "x.safetensors", strength_model: 1, strength_clip: 1 },
  };
  const row = (patch = {}) => ({
    name: "carkit",
    text: "CarKit",
    weight: "",
    lora: "x.safetensors",
    sm: "1",
    sc: "1",
    comment: "",
    base: "",
    ...patch,
  });

  test("no provenance on either side is not an edit", () => {
    assert.equal(itemRowEdited(row({ loraInfo: {} }), item), false);
    assert.equal(itemRowEdited(row(), item), false);
  });

  test("gaining provenance counts as an edit", () => {
    assert.equal(itemRowEdited(row({ loraInfo: { trained_words: WORDS } }), item), true);
  });

  test("the same provenance in a different key order is NOT an edit", () => {
    const stored = { ...item, data: { ...item.data, lora_info: { trained_words: WORDS, air: "a" } } };
    assert.equal(itemRowEdited(row({ loraInfo: { air: "a", trained_words: WORDS } }), stored), false);
  });

  test("losing or changing provenance counts", () => {
    const stored = { ...item, data: { ...item.data, lora_info: { trained_words: WORDS } } };
    assert.equal(itemRowEdited(row({ loraInfo: {} }), stored), true);
    assert.equal(itemRowEdited(row({ loraInfo: { trained_words: ["CarKit"] } }), stored), true);
  });

  test("a row that passes no loraInfo at all is unaffected (old behavior)", () => {
    const stored = { ...item, data: { ...item.data, lora_info: { trained_words: WORDS } } };
    assert.equal(itemRowEdited(row(), stored), false);
  });

  test("muting a word is caught as a TEXT edit, provenance untouched", () => {
    const stored = { ...item, data: { ...item.data, lora_info: { trained_words: WORDS } } };
    const info = { trained_words: WORDS };
    assert.equal(itemRowEdited(row({ loraInfo: info, text: "" }), stored), true);
    assert.equal(itemRowEdited(row({ loraInfo: info, text: "CarKit" }), stored), false);
  });
});
