// Invariants in compose.js that only a browser can execute, pinned by source
// scan because the failure mode is a FROZEN TAB — there is nothing left to
// assert after it happens, and nothing here runs a render path.
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

/**
 * The body of a top-level `function name(`, ending at ITS closing brace — the
 * first line that is exactly two spaces and a brace. Slicing to the next
 * `function` instead swept up the following helper and its comments, so an
 * assertion matched text from a function it was not about. That is how this
 * very test first failed.
 */
function body(name) {
  const start = SRC.indexOf(`  function ${name}(`);
  assert.ok(start !== -1, `compose.js no longer defines ${name}()`);
  const end = SRC.indexOf("\n  }\n", start);
  assert.ok(end !== -1, `could not find the end of ${name}()`);
  return SRC.slice(start, end);
}

describe("nested pool loading stays linear", () => {
  // The freeze: childRow did
  //     if (!pool) ensurePool(child.ref).then(() => renderNested());
  // With N nested children whose pools are unloaded, that schedules N
  // callbacks; every arrival re-renders; every re-render walks all N children
  // and attaches ANOTHER callback to each still-pending request (ensurePool
  // returns the same in-flight promise, so they stack). It compounds until the
  // tab stops painting. Changing a parent's draw gives its children new refs,
  // all unloaded at once — "click on a parent of a nested object, site
  // freezes".
  test("childRow never chains a render onto ensurePool itself", () => {
    assert.ok(
      !/ensurePool\([^)]*\)\s*\.then/.test(body("childRow")),
      "childRow must go through the coalescing requester, not ensurePool().then()"
    );
  });

  test("the requester asks for each ref at most once while it is in flight", () => {
    const src = body("requestNestedPool");
    assert.match(src, /nestedAsked\.has\(ref\)/, "no in-flight guard — requests will stack");
    assert.match(src, /nestedAsked\.add\(ref\)/);
  });

  test("arrivals are collapsed into ONE render per frame", () => {
    const src = body("requestNestedPool");
    assert.match(src, /requestAnimationFrame/, "renders must be coalesced, not one per arrival");
    assert.match(
      src,
      /nestedFrame !== null/,
      "a render already queued must suppress further ones"
    );
  });

  test("a pool that never arrives renders nothing, so nothing re-schedules", () => {
    assert.match(
      body("requestNestedPool"),
      /if \(!state\.detail\?\.pools\?\.\[ref\]\) return/,
      "a declined or failed pool must terminate the chain"
    );
  });

  test("nestedBranch never re-enters renderNested", () => {
    // its recursion is bounded by the data; a call back into renderNested
    // would not be
    assert.ok(!/renderNested\(/.test(body("nestedBranch")));
  });
});
