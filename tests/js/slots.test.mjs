// The child-slot helpers behind the section editor's per-{placeholder} row.
//
// Why these exist: an item's slots ({id, ref, default, tags_any, tags_none})
// were reachable only by hand-editing JSON, so boudoir/configuration's
// female-duo — four slots, two of them the SAME section under different ids —
// could not be authored in the composer at all. These are the pure halves of
// the row that fixes that; the DOM half is covered by the module guards.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  cycleSlotTag,
  dropToken,
  renameToken,
  tagUnion,
  textTokens,
  wordPrefixMatch,
} from "../../web/js/composer/util.js";

// the real thing, from mrln/data/prompt/sections/boudoir/configuration.json
const DUO_TEXT =
  "A high class photo of (two distinct female models:1.4), the first model is "
  + "{model-a}, while the second model is {model-b}, their silhouettes "
  + "complementary, ({interaction}:1.3), ({pose}:1.25)";

describe("textTokens", () => {
  test("reports every reference once, in order", () => {
    assert.deepEqual(textTokens(DUO_TEXT), ["model-a", "model-b", "interaction", "pose"]);
  });

  test("a repeated token is listed once — it is ONE slot", () => {
    assert.deepEqual(textTokens("{a} and {b} and {a} again"), ["a", "b"]);
  });

  test("'{{name}}' is the literal escape, not a reference", () => {
    assert.deepEqual(textTokens("a {{literal}} brace"), []);
    assert.deepEqual(textTokens("{{no}} but {yes}"), ["yes"]);
  });

  test("'trigger' is the node's own widget, never a child slot", () => {
    assert.deepEqual(textTokens("{trigger}, {model}"), ["model"]);
  });

  test("nothing, empty and undefined are all no references", () => {
    for (const value of ["", null, undefined, "plain text"]) {
      assert.deepEqual(textTokens(value), []);
    }
  });

  test("a brace that is not an identifier is not a token", () => {
    // emphasis syntax and stray braces must not become slots
    assert.deepEqual(textTokens("({pose}:1.15) and {not a token}"), ["pose"]);
  });
});

describe("renameToken", () => {
  test("carries the sentence with the rename", () => {
    const out = renameToken(DUO_TEXT, "model-a", "hero");
    assert.match(out, /the first model is \{hero\}/);
    assert.match(out, /the second model is \{model-b\}/); // untouched
  });

  test("renames every occurrence", () => {
    assert.equal(renameToken("{a} x {a}", "a", "b"), "{b} x {b}");
  });

  test("never touches the '{{literal}}' escape", () => {
    assert.equal(renameToken("{{a}} and {a}", "a", "b"), "{{a}} and {b}");
  });

  test("a name with regex metacharacters is matched literally", () => {
    // ids are sanitised to [A-Za-z0-9_-], but the helper must not be the
    // thing that decides that — a '.' here would otherwise match any char
    assert.equal(renameToken("{a.b} {axb}", "a.b", "z"), "{z} {axb}");
  });

  test("model-a and model-b do not collide on the '-'", () => {
    assert.equal(renameToken("{model-a} {model-b}", "model-a", "x"), "{x} {model-b}");
  });
});

describe("dropToken", () => {
  test("removes the reference and leaves the rest", () => {
    assert.equal(dropToken("one {a} two", "a"), "one  two");
  });

  test("leaves the escape alone", () => {
    assert.equal(dropToken("{{a}} {a}", "a"), "{{a}} ");
  });
});

describe("tagUnion", () => {
  test("every tag any item carries, sorted, deduplicated", () => {
    const items = [
      { name: "one", tags: ["female", "lingerie"] },
      { name: "two", tags: ["female"] },
      { name: "three" },
      null,
    ];
    assert.deepEqual(tagUnion(items), ["female", "lingerie"]);
  });

  test("a pool with no tags offers no filter", () => {
    assert.deepEqual(tagUnion([{ name: "one" }]), []);
    assert.deepEqual(tagUnion([]), []);
    assert.deepEqual(tagUnion(undefined), []);
  });
});

describe("cycleSlotTag", () => {
  test("off → only this → never this → off", () => {
    const slot = { id: "model", ref: "human/profile" };
    const only = cycleSlotTag(slot, "female");
    assert.deepEqual(only.tags_any, ["female"]);
    assert.equal(only.tags_none, undefined);

    const never = cycleSlotTag(only, "female");
    assert.equal(never.tags_any, undefined, "an emptied filter leaves NO key behind");
    assert.deepEqual(never.tags_none, ["female"]);

    const off = cycleSlotTag(never, "female");
    assert.equal(off.tags_any, undefined);
    assert.equal(off.tags_none, undefined);
  });

  test("the original slot is never mutated", () => {
    const slot = { id: "model", ref: "human/profile" };
    cycleSlotTag(slot, "female");
    assert.deepEqual(slot, { id: "model", ref: "human/profile" });
  });

  test("other tags in the same filter survive the cycle", () => {
    const slot = { id: "m", ref: "r", tags_any: ["female", "tall"] };
    const next = cycleSlotTag(slot, "female");
    assert.deepEqual(next.tags_any, ["tall"]);
    assert.deepEqual(next.tags_none, ["female"]);
  });

  test("id and ref ride through untouched", () => {
    const next = cycleSlotTag({ id: "model-a", ref: "human/profile" }, "female");
    assert.equal(next.id, "model-a");
    assert.equal(next.ref, "human/profile");
  });
});

describe("wordPrefixMatch — the one rule all three filters search by", () => {
  test("matches at a word start, never mid-word", () => {
    assert.ok(wordPrefixMatch("rainy street", "rain"));
    assert.ok(!wordPrefixMatch("terrain", "rain"), "'rain' must not match terrain");
    assert.ok(!wordPrefixMatch("training day", "rain"));
  });

  test("a slug splits on '/' and '-', so either half is reachable", () => {
    // this is the fix: '{location' used to find nothing because the id is
    // only the last segment
    assert.ok(wordPrefixMatch("everyday location/everyday", "location"));
    assert.ok(wordPrefixMatch("everyday location/everyday", "everyday"));
    assert.ok(wordPrefixMatch("model-a", "a"));
  });

  test("every term has to match", () => {
    assert.ok(wordPrefixMatch("location/urban night", "urban night"));
    assert.ok(!wordPrefixMatch("location/urban night", "urban forest"));
  });

  test("an empty query matches everything", () => {
    assert.ok(wordPrefixMatch("anything", ""));
    assert.ok(wordPrefixMatch("anything", "   "));
    assert.ok(wordPrefixMatch("anything", null));
  });

  test("regex metacharacters in the query are literal", () => {
    assert.ok(!wordPrefixMatch("abc", "a.c"));
    assert.ok(wordPrefixMatch("a.c", "a.c"));
  });
});
