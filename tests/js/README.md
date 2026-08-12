# JS unit tests

Run: `node --test "tests/js/*.test.mjs"` (quoted glob — works in bash and
PowerShell; a bare `node --test` from the repo root also discovers them).
Node ≥ 22 runs explicitly-passed paths as FILES and only recurses into
directories for its implicit cwd scan, so `node --test tests/js/` does not work.

`node:test` + `node:assert` only: zero dependencies, no package.json, no npm —
the pack ships to users with an empty `requirements.txt` and no JS toolchain.
Covers `web/js/composer/util.js` (pure helpers) and `web/js/composer/api.js`
(fetch wrappers, library fingerprint cache, LLM caches — driven through the
injected transport, no network). The panel's DOM code is not unit-tested.
Both suites first import their module with `document`/`window`/`app`/`api`/
`fetch` booby-trapped, which is how the "zero top-level side effects" rule for
`web/js/composer/*` is enforced rather than merely documented.
