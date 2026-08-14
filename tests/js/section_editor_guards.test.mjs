// Invariants of the section editor's child-slot rows that only a browser can
// execute, pinned by source scan — the same technique as compose_guards.mjs,
// for the same reason: the failure mode is a UI that janks or silently lies,
// and nothing here runs a render path.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SRC = readFileSync(
  fileURLToPath(new URL("../../web/js/composer/section_editor.js", import.meta.url)),
  "utf8"
);

/**
 * The source between two anchors. `to` is searched FORWARD FROM `from`, not
 * from the top of the file: `const picker =` occurs four times, and anchoring
 * it at the first one silently produced an empty slice that matched nothing
 * and passed nothing. A zero-length region is a moved anchor, never a pass —
 * so it fails loudly here instead of vacuously in the assertion below.
 */
function region(from, to) {
  const start = SRC.indexOf(from);
  assert.ok(start !== -1, `section_editor.js no longer contains ${from}`);
  const end = to ? SRC.indexOf(to, start + from.length) : SRC.length;
  assert.ok(end !== -1, `no ${to} after ${from}`);
  assert.ok(end > start, `empty region between ${from} and ${to} — an anchor moved`);
  return SRC.slice(start, end);
}

/**
 * The body of `name`, however it is declared — plain function, async
 * function, or a const arrow — ending at ITS OWN closing brace (matched on
 * indentation, so a nested block never ends the slice early).
 */
function body(name) {
  const start = SRC.search(
    new RegExp(`^([ \\t]*)(?:async )?(?:function ${name}\\(|const ${name} = )`, "m")
  );
  assert.ok(start !== -1, `section_editor.js no longer defines ${name}`);
  const indent = /^([ \t]*)/.exec(SRC.slice(start).split("\n")[0])[1];
  const ends = [`\n${indent}}\n`, `\n${indent}};\n`]
    .map((close) => SRC.indexOf(close, start))
    .filter((at) => at !== -1);
  assert.ok(ends.length, `could not find the end of ${name}`);
  return SRC.slice(start, Math.min(...ends));
}

describe("a section already used stays pickable", () => {
  // {model-a} and {model-b} both want human/profile. itemRefOptions used to
  //     if (row.slots.some((s) => s.ref === sec.slug)) continue;
  // which removed a section from the menu the moment it was referenced once —
  // so a duo or a couple could not be built in the composer at all, only in a
  // text editor. Only the ID has to be unique.
  test("itemRefOptions never skips a section for being referenced already", () => {
    const src = body("itemRefOptions");
    assert.ok(
      !/\.some\(\([^)]*\)\s*=>\s*[\w.]*\.ref === sec\.slug\)\s*\)\s*continue/.test(src),
      "a used section must stay on offer — this is the multi-reference block"
    );
    assert.match(src, /while \(used\.has\(id\)\)/, "the ID is what gets suffixed, not the ref");
  });
});

describe("slot rows do not rebuild on the typing path", () => {
  // A rebuild constructs one section picker per slot — ~200 options each, and
  // 17 slots on a real nested item. Doing that per keystroke is the same
  // shape of mistake as the nested-pool fan-out that froze the Compose tab,
  // except it lands directly under the caret.
  test("the text input schedules a debounced render, never a direct one", () => {
    const listener = /addEventListener\("input",\s*\(\)\s*=>\s*\{([\s\S]*?)\}\);/.exec(
      SRC.slice(SRC.indexOf("row.text.addEventListener"))
    );
    assert.ok(listener, "the item text input listener moved or changed shape");
    assert.match(listener[1], /scheduleRenderSlots\(\)/);
    assert.ok(
      !/[^e]renderSlots\(\)/.test(listener[1]),
      "typing must not call renderSlots() directly"
    );
  });

  test("the debounce coalesces keystrokes into one render", () => {
    const src = body("scheduleRenderSlots");
    assert.match(src, /clearTimeout\(slotTimer\)/, "no debounce — every keystroke rebuilds");
    assert.match(src, /setTimeout\(renderSlots/);
  });

  test("an unchanged structure repaints in place instead of rebuilding", () => {
    const src = body("renderSlots");
    assert.match(src, /signature === slotSignature/, "no structural guard");
    assert.match(
      src,
      /for \(const line of slotLines\) line\.syncUsed\(\)/,
      "the cheap path must still refresh what typing changed, or the 'insert' "
        + "affordance lies about whether the placeholder is in the text"
    );
  });

  test("syncUsed touches only a class and a button, never the DOM tree", () => {
    const src = body("syncUsed") || "";
    assert.ok(!/mount\(|replaceChildren|sectionPicker\(/.test(src), "syncUsed must stay cheap");
  });
});

describe("editing a slot never silently edits the file", () => {
  test("renaming an id rewrites the placeholder in the same edit", () => {
    // otherwise every rename breaks the item, which is why nobody renamed the
    // auto-derived ids and {model-a} was unreachable
    const src = region("idInput.addEventListener", "const picker =");
    assert.match(src, /setText\(renameToken\(row\.text\.value, from, next\)\)/);
  });

  test("a duplicate or empty id is refused, not applied", () => {
    const src = region("idInput.addEventListener", "const picker =");
    assert.match(src, /idInput\.value = slot\.id/, "a refused rename must snap back");
    assert.match(src, /taken\.has\(next\)/);
  });

  test("changing the ref drops the default and tags that belonged to the old one", () => {
    const change = region("picker.select.addEventListener").slice(0, 500);
    for (const key of ["default", "tags_any", "tags_none"]) {
      assert.match(change, new RegExp(`delete slot\\.${key}`), `a stale ${key} would be a lie`);
    }
  });

  test("a default naming an item that no longer exists stays visible", () => {
    // dropping it on open would edit the file just by looking at it
    assert.match(SRC, /\(missing\)/);
  });
});

describe("child pools are fetched once per ref", () => {
  test("poolFor shares an in-flight request", () => {
    const src = body("poolFor");
    assert.match(src, /poolReqs\.get\(ref\)/, "N slots on one section must make ONE call");
    assert.match(src, /poolReqs\.set\(ref, request\)/);
  });

  test("a failure is not cached, so a later retry can succeed", () => {
    const src = body("poolFor");
    const cacheWrite = /pools\.set\(ref, items\)/.test(src);
    assert.ok(cacheWrite, "successes are cached");
    assert.ok(
      !/catch\(\(\) => \{[\s\S]*pools\.set/.test(src),
      "a failed fetch must not poison the cache"
    );
  });

  test("a pool that arrives after the ref changed is discarded", () => {
    assert.match(body("paintPool"), /if \(slot\.ref !== ref\) return/);
  });
});
