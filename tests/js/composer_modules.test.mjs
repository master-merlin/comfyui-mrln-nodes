// The hygiene guard for EVERY module of the composer panel.
//
// ComfyUI auto-imports every .js file under WEB_DIRECTORY, so each module here
// is evaluated standalone in the browser as well as via the panel's import
// graph. The browser's module cache makes that double load harmless ONLY while
// evaluating a module does nothing but declare things. That property is what
// makes the panel split safe, so it is enforced here rather than merely
// documented — two guards per file, applied to whatever is in the directory:
//
//   1. the module is imported with document/window/app/api/fetch booby-trapped
//      (top level of this file — a top-level DOM/network touch throws and the
//      whole suite fails loudly), then asserted to have produced its API;
//   2. its source is scanned for top-level statements: every column-0 line has
//      to be a comment, an import/export, a declaration or a closing brace, no
//      module-level `let`/`var` (mutable state must live inside the factory,
//      so a second import cannot see it) and no call expression in a top-level
//      `const` initializer.
//
// util.js and api.js keep their own, stricter, module-specific checks in
// util.test.mjs (no imports at all, no browser globals anywhere in the source).
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

const DIR = new URL("../../web/js/composer/", import.meta.url);
const PANEL = new URL("../../web/js/prompt_composer_panel.js", import.meta.url);

/** Every composer module, plus the panel shell that imports them all. */
const FILES = [
  ...readdirSync(fileURLToPath(DIR))
    .filter((name) => name.endsWith(".js"))
    .sort()
    .map((name) => ({ name, url: new URL(name, DIR) })),
  { name: "prompt_composer_panel.js", url: PANEL },
];

// ---------------------------------------------------------------------------
// guard 1 — import them all with the browser globals booby-trapped
// ---------------------------------------------------------------------------

const TRAPPED = ["document", "window", "app", "api", "localStorage", "XMLHttpRequest", "alert"];
const saved = new Map();
for (const name of TRAPPED) {
  saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, {
    configurable: true,
    get() {
      throw new Error(`a composer module touched the global '${name}' at import time`);
    },
  });
}
const savedFetch = globalThis.fetch;
globalThis.fetch = () => {
  throw new Error("a composer module performed a network request at import time");
};

const loaded = new Map();
try {
  for (const file of FILES) loaded.set(file.name, await import(file.url.href));
} finally {
  globalThis.fetch = savedFetch;
  for (const name of TRAPPED) {
    const desc = saved.get(name);
    if (desc) Object.defineProperty(globalThis, name, desc);
    else delete globalThis[name];
  }
}

// ---------------------------------------------------------------------------

const ALLOWED_TOP_LEVEL =
  /^(\/\/|\/\*|\*\/|import\b|export\b|function\s|const\s|class\s|\}$|\} from )/;

describe("composer module hygiene", () => {
  test("the whole module set is under test", () => {
    const names = FILES.map((f) => f.name);
    for (const expected of [
      "api.js",
      "bundles.js",
      "compose.js",
      "decompose.js",
      "dom.js",
      "history.js",
      "image.js",
      "intake.js",
      "loras.js",
      "section_editor.js",
      "settings.js",
      "state.js",
      "template_editor.js",
      "thumbs.js",
      "tree.js",
      "util.js",
      "prompt_composer_panel.js",
    ]) {
      assert.ok(names.includes(expected), `${expected} is missing from web/js/composer`);
    }
  });

  for (const file of FILES) {
    test(`${file.name} imports with zero top-level side effects`, () => {
      // proven by the trapped import above; this asserts the module actually
      // produced its API rather than silently failing
      const mod = loaded.get(file.name);
      const fns = Object.entries(mod).filter(([, value]) => typeof value === "function");
      assert.ok(fns.length > 0, `${file.name} exported no functions`);
    });

    test(`${file.name} declares nothing but imports, exports and declarations`, () => {
      const src = readFileSync(fileURLToPath(file.url), "utf8");
      const topLevel = src
        .split("\n")
        .filter((line) => line.length && !/^\s/.test(line));
      for (const line of topLevel) {
        assert.ok(
          ALLOWED_TOP_LEVEL.test(line),
          `unexpected top-level statement in ${file.name}: ${line}`
        );
        assert.ok(
          !/^(let|var)\s/.test(line),
          `module-level mutable state in ${file.name}: ${line}`
        );
        if (/^(export )?const\s/.test(line)) {
          // everything before a `=>` is a parameter list, not a call: an
          // exported arrow function is a declaration like any other
          const initializer = line.replace(/^(export )?const\s+[\w$]+\s*=\s*/, "").split("=>")[0];
          assert.ok(
            !/[\w$]\s*\(/.test(initializer),
            `top-level const calls something in ${file.name}: ${line}`
          );
        }
      }
    });
  }

  test("every panel module exposes exactly one create* factory", () => {
    // the panel's wiring calls these by name — a rename would break the shell
    for (const [name, factory] of [
      ["bundles.js", "createBundles"],
      ["compose.js", "createCompose"],
      ["decompose.js", "createDecompose"],
      ["history.js", "createHistory"],
      ["intake.js", "createIntake"],
      ["loras.js", "createLoras"],
      ["section_editor.js", "createSectionEditor"],
      ["settings.js", "createSettings"],
      ["template_editor.js", "createTemplateEditor"],
      ["thumbs.js", "createThumbs"],
      ["tree.js", "createTree"],
    ]) {
      const mod = loaded.get(name);
      assert.equal(typeof mod[factory], "function", `${name} must export ${factory}`);
      assert.deepEqual(
        Object.keys(mod).filter((key) => key.startsWith("create")),
        [factory],
        `${name} must export ${factory} and nothing else called create*`
      );
    }
    // state.js is the exception: the state object and its store are separate
    const state = loaded.get("state.js");
    assert.equal(typeof state.createState, "function");
    assert.equal(typeof state.createStore, "function");
    assert.equal(typeof loaded.get("prompt_composer_panel.js").createComposerPanel, "function");
  });

  test("createState hands out a fresh state object per call", () => {
    // one panel = one state object; two panels must never share Maps/Sets
    const { createState } = loaded.get("state.js");
    const a = createState();
    const b = createState();
    assert.notEqual(a, b);
    assert.notEqual(a.rows, b.rows);
    a.rows.set("subject", { random: true, seed: "", item: "" });
    a.muted.add("style");
    assert.equal(b.rows.size, 0);
    assert.equal(b.muted.size, 0);
    // the shape the modules rely on
    for (const key of ["rows", "labelEdit", "muted", "soloed", "libGroups", "nestOpen"]) {
      assert.ok(a[key] instanceof Map || a[key] instanceof Set, `state.${key} must be a Map/Set`);
    }
    assert.equal(a.tab, "compose");
    assert.equal(a.mode, "as configured");
    assert.equal(a.decompose.running, false);
  });
});

// ---------------------------------------------------------------------------
// Shadowing an imported helper.
//
// A mechanical rewrite turned `mount.replaceChildren(…)` into `mount(mount, …)`
// inside `for (const mount of …)`. The loop variable shadows dom.js's mount(),
// so the call invokes a DOM ELEMENT as a function — every nested draw vanished
// from the Compose tab, and no test noticed, because nothing here executes a
// render path. It reached the user.
//
// The rule is cheap and absolute: never bind a local with the name of a helper
// the module imported.
// ---------------------------------------------------------------------------

describe("no module shadows what it imports", () => {
  const BINDING = /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?:of|in|=)/g;

  for (const file of FILES) {
    test(`${file.name} never rebinds an imported name`, () => {
      const src = readFileSync(fileURLToPath(file.url), "utf8");
      const imported = new Set();
      for (const match of src.matchAll(/import\s*\{([^}]*)\}\s*from/g)) {
        for (const part of match[1].split(",")) {
          const name = part.split(" as ").pop().trim();
          if (name) imported.add(name);
        }
      }
      if (!imported.size) return;
      const clashes = new Set();
      for (const match of src.matchAll(BINDING)) {
        if (imported.has(match[1])) clashes.add(match[1]);
      }
      assert.deepEqual(
        [...clashes],
        [],
        `${file.name} rebinds imported name(s) ${[...clashes].join(", ")} — a call to the `
          + "import then hits the local instead (this is how every nested draw disappeared)"
      );
    });
  }
});
