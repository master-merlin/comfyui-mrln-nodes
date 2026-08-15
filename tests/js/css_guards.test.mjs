// Invariants of prompt_composer.css that a browser proves but nothing here
// can execute, pinned by source scan — the same technique as
// compose_guards.mjs, for the same reason.
//
// This file exists because of one recurring failure mode, not for coverage:
// a rule in this sheet loses on SPECIFICITY to an older rule it was written
// to override, and the loss is silent. It has happened three times now —
// every button in the panel ignoring the design system, a table's column
// widths being tuned in a block that had already lost, and a history
// thumbnail rendering as a 6-pixel sliver because a slot rule gave it the
// text gutter. Each time the CSS looked correct in isolation.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const CSS = readFileSync(
  fileURLToPath(new URL("../../web/js/prompt_composer.css", import.meta.url)),
  "utf8"
);

/**
 * (ids, classes, elements) for ONE selector, counted the way the cascade
 * counts: :not(x) contributes x's specificity, not zero. Getting that wrong
 * is precisely how `.pc-panel .mrln-slot > :not(.pc-row):not(.mrln-nest)`
 * (0,4,0) quietly beat a three-class override.
 */
export function specificity(selector) {
  let s = selector.trim();
  // :not()/:is()/:where() — :where() is the only one that contributes nothing
  s = s.replace(/:where\([^)]*\)/g, "");
  const inner = [...s.matchAll(/:(?:not|is)\(([^)]*)\)/g)].map((m) => m[1]);
  s = s.replace(/:(?:not|is)\([^)]*\)/g, "");
  let ids = (s.match(/#[\w-]+/g) || []).length;
  let classes = (s.match(/\.[\w-]+/g) || []).length + (s.match(/\[[^\]]+\]/g) || []).length;
  // pseudo-CLASSES count as classes; pseudo-ELEMENTS (::x) count as elements
  const pseudoEl = (s.match(/::[\w-]+/g) || []).length;
  s = s.replace(/::[\w-]+/g, "");
  classes += (s.match(/(?<!:):[\w-]+/g) || []).length;
  let elements =
    pseudoEl + (s.replace(/[.#[][^\s>+~]*/g, "").match(/\b[a-zA-Z][\w-]*/g) || []).length;
  for (const part of inner) {
    const [i, c, e] = specificity(part);
    ids += i;
    classes += c;
    elements += e;
  }
  return [ids, classes, elements];
}

const cmp = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];

/** Every top-level rule as {selectors, body, index}, comments stripped. */
function rules() {
  const src = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  const out = [];
  let depth = 0;
  let buf = "";
  let body = "";
  let sel = null;
  for (const ch of src) {
    if (ch === "{") {
      if (depth === 0) {
        sel = buf.trim();
        body = "";
      } else body += ch;
      depth++;
      buf = "";
    } else if (ch === "}") {
      depth--;
      if (depth === 0 && sel && !sel.startsWith("@")) {
        out.push({ selectors: sel.split(",").map((x) => x.trim()), body, index: out.length });
        sel = null;
      } else body += ch;
      buf = "";
    } else {
      buf += ch;
      if (depth > 0) body += ch;
    }
  }
  return out;
}

/** The LAST rule that sets `prop` on a selector matching `probe`, by cascade. */
function winner(probe, prop) {
  const re = new RegExp(`(^|;)\\s*${prop}\\s*:`);
  let best = null;
  for (const rule of rules()) {
    if (!re.test(rule.body)) continue;
    for (const one of rule.selectors) {
      if (!probe(one)) continue;
      const spec = specificity(one);
      if (!best || cmp(spec, best.spec) > 0 || (cmp(spec, best.spec) === 0 && rule.index > best.index)) {
        best = { selector: one, spec, index: rule.index, body: rule.body };
      }
    }
  }
  return best;
}

describe("the specificity counter itself", () => {
  test(":not() contributes its argument, which is the trap", () => {
    assert.deepEqual(specificity(".pc-panel .mrln-slot > :not(.pc-row):not(.mrln-nest)"), [0, 4, 0]);
    assert.deepEqual(specificity(".pc-panel .mrln-slot.mrln-history-row > .mrln-history-thumb"), [
      0, 4, 0,
    ]);
    // the three-class override that lost, and why it lost
    assert.equal(
      cmp(
        specificity(".pc-panel .mrln-history-row > .mrln-history-thumb"),
        specificity(".pc-panel .mrln-slot > :not(.pc-row):not(.mrln-nest)")
      ) < 0,
      true
    );
  });
});

describe("a history thumbnail keeps its whole box", () => {
  // The bug: .mrln-history-row IS a .mrln-slot, so the rule giving slot
  // children the text gutter also gave the <img> padding-inline: 14px. With
  // border-box that leaves 6px of picture inside a 34px tile.
  test("the padding reset outranks the slot-gutter rule it undoes", () => {
    const win = winner((s) => /mrln-history-thumb$/.test(s), "padding-inline");
    assert.ok(win, "nothing resets padding on .mrln-history-thumb any more");
    assert.match(win.body, /padding-inline:\s*0/, "the winning rule does not zero the padding");
    const gutter = specificity(".pc-panel .mrln-slot > :not(.pc-row):not(.mrln-nest)");
    assert.ok(
      cmp(win.spec, gutter) >= 0,
      `${win.selector} scores ${win.spec} and must not score below the slot-gutter rule ${gutter}`
    );
  });

  test("hover magnifies with a transform, so no row is pushed around", () => {
    // The tile is stored at 64px and shown at 34. Hover shows the stored size.
    // It has to be a transform: width/height would reflow the list under the
    // pointer, which moves the very row you are aiming at.
    const rule = rules().find((r) =>
      r.selectors.some((s) => /\.mrln-history-thumb:hover$/.test(s))
    );
    assert.ok(rule, "the thumbnail no longer magnifies on hover");
    assert.match(rule.body, /transform:\s*scale\(/, "magnified by something that reflows");
    assert.doesNotMatch(rule.body, /(^|;)\s*(width|height)\s*:/, "hover changes layout size");
    const scale = Number(/scale\(([\d.]+)\)/.exec(rule.body)?.[1]);
    const stored = 64;
    const shown = Number(/width:\s*(\d+)px/.exec(
      rules().find((r) => r.selectors.some((s) => /\.mrln-history-thumb$/.test(s)))?.body ?? ""
    )?.[1]);
    assert.ok(
      Math.abs(shown * scale - stored) <= 1.5,
      `hover scales ${shown}px by ${scale} = ${(shown * scale).toFixed(1)}px, but tiles are `
        + `stored at ${stored}px — magnifying past the stored size only blurs it`
    );
  });

  test("the row keeps a gap, which .pc-panel .mrln-slot zeroes for every slot", () => {
    const win = winner((s) => /mrln-history-row$/.test(s), "gap");
    assert.ok(win, "no rule sets a gap on .mrln-history-row");
    assert.doesNotMatch(win.body, /gap:\s*0(px)?\s*(;|$)/, "the tile would touch the timestamp");
    assert.ok(
      cmp(win.spec, specificity(".pc-panel .mrln-slot")) >= 0,
      "a bare .mrln-history-row loses to .pc-panel .mrln-slot"
    );
  });
});
