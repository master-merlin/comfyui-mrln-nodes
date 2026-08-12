// The section picker's filter row (user request 2026-08-13).
//
// 210 sections is not a dropdown anyone can browse. These cover the two pure
// halves: which options survive a query, and how a deep hit labels itself.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { deepLabel, filterSectionOptions } from "../../web/js/composer/compose.js";

const OPTIONS = [
  { value: "location/everyday", label: "location/everyday", match: true },
  { value: "location/urban", label: "location/urban", match: true },
  { value: "battle/ground", label: "battle/ground  [battle]", match: false },
  { value: "atmosphere/weather", label: "atmosphere/weather", match: true },
  { value: "camera/film", label: "camera/film", match: true },
  { value: "wardrobe/historical", label: "wardrobe/historical  [human]", match: true },
];

const values = (rows) => rows.map((r) => r.value);

describe("filterSectionOptions", () => {
  test("an empty query keeps everything", () => {
    assert.equal(filterSectionOptions(OPTIONS, "").length, OPTIONS.length);
    assert.equal(filterSectionOptions(OPTIONS, "   ").length, OPTIONS.length);
  });

  test("matches at a word start, never mid-word", () => {
    // the bug this rule exists for: 'rain' inside terrain / grain / training
    const rows = filterSectionOptions(
      [
        { value: "weather/rain", label: "weather/rain" },
        { value: "battle/terrain", label: "battle/terrain" },
        { value: "camera/grain", label: "camera/grain" },
      ],
      "rain"
    );
    assert.deepEqual(values(rows), ["weather/rain"]);
  });

  test("a slash starts a word, so 'urban' finds location/urban", () => {
    assert.deepEqual(values(filterSectionOptions(OPTIONS, "urban")), ["location/urban"]);
  });

  test("every term must hit — two words narrow", () => {
    const rows = filterSectionOptions(
      [
        { value: "a", label: "location/urban neon street" },
        { value: "b", label: "location/urban quiet street" },
        { value: "c", label: "lighting/neon room" },
      ],
      "neon street"
    );
    assert.deepEqual(values(rows), ["a"]);
  });

  test("is case-insensitive", () => {
    assert.deepEqual(values(filterSectionOptions(OPTIONS, "WARDROBE")), ["wardrobe/historical"]);
  });

  test("regex metacharacters in the query are literal, not a pattern", () => {
    // a user typing '(' must not throw, and '.' must not match everything
    assert.doesNotThrow(() => filterSectionOptions(OPTIONS, "("));
    assert.deepEqual(filterSectionOptions(OPTIONS, "."), []);
    assert.deepEqual(filterSectionOptions(OPTIONS, "loc.tion"), []);
  });

  test("the tag suffix in a label is searchable too", () => {
    assert.deepEqual(values(filterSectionOptions(OPTIONS, "human")), ["wardrobe/historical"]);
  });

  test("no match is an empty list, not everything", () => {
    assert.deepEqual(filterSectionOptions(OPTIONS, "nightclub"), []);
  });
});

describe("deepLabel", () => {
  test("a name hit shows the slug alone", () => {
    assert.equal(deepLabel({ slug: "location/urban", where: ["name"], samples: [] }), "location/urban");
  });

  test("an item hit says WHERE it matched — that is the whole point", () => {
    // 'wardrobe/historical' alone would look like a dead end to someone
    // hunting a disco; naming the item is what turns it into a pick
    assert.equal(
      deepLabel({ slug: "wardrobe/historical", where: ["item"], samples: ["seventies-disco"] }),
      "wardrobe/historical  — via seventies-disco"
    );
  });

  test("at most two samples, so one row stays one line", () => {
    const label = deepLabel({ slug: "s", where: ["item"], samples: ["a", "b", "c", "d"] });
    assert.equal(label, "s  — via a, b");
  });

  test("survives a malformed row", () => {
    assert.equal(deepLabel({}), "");
    assert.equal(deepLabel({ slug: "s", where: ["item"], samples: [] }), "s");
  });
});
