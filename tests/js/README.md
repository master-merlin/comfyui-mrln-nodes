# JS unit tests

Run: `node --test "tests/js/*.test.mjs"` (quoted glob — works in bash and
PowerShell; a bare `node --test` from the repo root also discovers them).
Node ≥ 22 runs explicitly-passed paths as FILES and only recurses into
directories for its implicit cwd scan, so `node --test tests/js/` does not work.

`node:test` + `node:assert` only: zero dependencies, no package.json, no npm —
the pack ships to users with an empty `requirements.txt` and no JS toolchain.

- `util.test.mjs` — `web/js/composer/util.js` (pure helpers), behavior-pinned.
- `api.test.mjs` — `web/js/composer/api.js` (fetch wrappers, library
  fingerprint cache, LLM caches — driven through the injected transport, so no
  network).
- `composer_modules.test.mjs` — the hygiene guard for EVERY module in
  `web/js/composer/` plus `prompt_composer_panel.js`, whatever the directory
  holds: each is imported with `document`/`window`/`app`/`api`/`fetch`
  booby-trapped, and its source is scanned for top-level statements (no
  module-level `let`/`var`, no call in a top-level `const`). That is how the
  "zero top-level side effects" rule — the thing that makes ComfyUI's
  auto-import of every `web/**/*.js` harmless — is enforced rather than merely
  documented. A new module is covered the moment the file exists.

The panel's DOM code itself is not unit-tested (no jsdom, no npm); the split
was validated by driving the panel against a throwaway DOM shim instead.
