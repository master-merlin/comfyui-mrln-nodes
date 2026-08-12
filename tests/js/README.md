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
- `image.test.mjs` — `web/js/composer/image.js`: container sniffing, the PNG
  chunk walk and the rebuilt metadata file, the per-format upload strategy and
  its caps. The scaffold constants have a SECOND test on the Python side
  (`tests/test_prompt_image_intake.py`) that parses them out of the JS file and
  feeds them to Pillow, so the two halves of that contract cannot drift.
- `intake.test.mjs` — the image → template card: payload shaping, candidate
  resolution, the two paths' request bodies.
- `triggers.test.mjs` — trigger-word mute/solo derivations, mirroring the cases
  in `tests/test_prompt_lora.py` (the server twin is `mrln/promptapi/lora.py`).
- `optimize.test.mjs` — the authored-vs-optimized comparison.
- `thumbs.test.mjs` — thumbnail URL building (incl. the cache buster), the
  resolution order and the glyph fallback.
- `history.test.mjs` — keyset paging transitions, record → row derivation, and
  the restore payload.
- `settings_ui.test.mjs` — the save payloads and their refusals (the months
  validator, the "settings never loaded → refuse to save" guard).
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
