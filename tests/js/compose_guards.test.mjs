// Invariants in compose.js that only a browser can execute, pinned by source
// scan because the failure mode is a FROZEN TAB — there is no assertion to be
// made after it happens, and no test here runs a render path.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SRC = readFileSync(
  fileURLToPath(new URL("../../web/js/composer/compose.js", import.meta.url)),
  "utf8"
);

describe("the nested-pool render loop", () => {
  // The bug: childRow did
  //     if (!pool) ensurePool(child.ref).then(() => renderNested());
  // ensurePool is async and resolves IMMEDIATELY when it declines to fetch
  // (already in flight, or backing off after a failure), so this became
  // renderNested -> childRow -> ensurePool -> renderNested, spinning as fast
  // as microtasks drain. The tab never paints again. It needs a parent whose
  // children point at an unloaded pool — i.e. changing a parent's draw, which
  // is how a user found it: "click on a parent of a nested object, site
  // freezes".
  test("re-rendering after ensurePool is gated on the pool having arrived", () => {
    const call = /ensurePool\(child\.ref\)\.then\(\s*\(\)\s*=>\s*renderNested\(\)\s*\)/;
    assert.ok(
      !call.test(SRC),
      "childRow re-renders unconditionally after ensurePool — that is the freeze loop"
    );
    // and the guarded form is present: the callback checks pools before rendering
    const guarded = /ensurePool\(child\.ref\)\.then\(\(\)\s*=>\s*\{[\s\S]{0,200}?pools\?\.\[child\.ref\][\s\S]{0,80}?renderNested\(\)/;
    assert.ok(
      guarded.test(SRC),
      "the re-render must be conditional on state.detail.pools[child.ref] existing"
    );
  });

  test("renderNested is never called synchronously from nestedBranch", () => {
    // nestedBranch recurses over child.children, which is bounded by the data.
    // A call back into renderNested from inside it would not be.
    const branch = SRC.slice(SRC.indexOf("function nestedBranch"), SRC.indexOf("function renderNested"));
    assert.ok(!/renderNested\(/.test(branch), "nestedBranch must not re-enter renderNested");
  });
});
