# Security policy

This pack runs inside ComfyUI, on your machine, with your files and your API
keys. This document says exactly what it does with them.

## Reporting a vulnerability

Open a [GitHub Security Advisory](https://github.com/master-merlin/comfyui-mrln-nodes/security/advisories/new)
(private), or a normal issue if the problem is not sensitive. Please include
the ComfyUI version, the pack version, and the smallest workflow that shows it.
There is no bounty; there is a fast fix and credit in the release notes.

## What this pack does not do

- **No `eval`, no `exec`** anywhere — Python or JavaScript.
- **No `subprocess`, no `os.system`, no runtime package installation.** The
  pack never installs, downloads, or executes code.
- **No obfuscation.** Every line ships readable.
- **No telemetry, analytics, or usage reporting.** Nothing is sent anywhere
  unless you asked for the feature that sends it.
- **No runtime dependencies.** `requirements.txt` is empty on purpose; the
  engine is standard library only. Pillow and PyYAML are used through soft
  imports where present (ComfyUI ships both) and their absence disables just
  the feature that needs them.

## Every outbound network call

The pack makes network requests **only** from the features listed here, and
every one is started by an explicit action of yours. Nothing is contacted on
import, on ComfyUI start, or on a schedule.

| Host | Which feature | What is sent | Auth |
| --- | --- | --- | --- |
| `civitai.com` | LoRA lookup by file **hash** (trigger words, AIR, base model), preview images, downloading a LoRA you chose, importing a Wildcards pack, de-composing a `civitai.com/images/…` link you pasted | the SHA256 of a local LoRA file, or the id you supplied | optional Civitai key, sent as an `Authorization: Bearer` **header** |
| `127.0.0.1:11434` / `127.0.0.1:1234` | Prompt Enhance and the LLM de-composer, when you pick the Ollama / LM Studio backend | the prompt you are enhancing | none (local) |
| `api.anthropic.com`, `api.openai.com`, `generativelanguage.googleapis.com`, `openrouter.ai` | the same two features, when you pick a cloud backend **and** have stored that provider's key | the prompt you are enhancing | your stored key |

A LoRA hash lookup sends a hash, never the file. The image intake reads
**metadata only** — never the pixels.

## The remote-backend gate (SSRF)

ComfyUI's process — not your browser — makes the LLM backend request, so an
arbitrary URL in that field would turn this pack into a probe for whatever
address you typed. Therefore:

- LLM backend URLs are **loopback-only by default** (`localhost`, `127.0.0.1`,
  `::1`). A URL anywhere else is refused at save time *and* at every use.
- Allowing a remote backend is an explicit, armed opt-in
  (`llm.allow_remote`), it covers both URLs, it stays on until you turn it
  off, and it is re-checked on every single use — turning it back off keeps a
  stored remote URL visible so you can see and fix it, while refusing it.
- Credentials may never be embedded in a backend URL (`user:pass@host` is
  rejected), nor may a query string or fragment.

Pinned by `tests/test_security_ssrf.py` — non-HTTP schemes, LAN addresses,
IPv6 loopback without brackets, stale stored LAN URLs at validate/pull/chat
time, and the rule that a failed request never echoes the response body back.

## Secrets

- API keys are stored **server-side only**, in your user tier
  (`settings.json`), never in a node widget — widget values persist into
  saved workflow PNGs and would travel with any image you share.
- No endpoint ever echoes a key. `GET /mrln/prompt/settings` returns
  `*_set` booleans and nothing else.
- The Civitai key travels as a request **header**, not in the URL, so it
  cannot land in a server log or a redirect. A presigned-URL fallback exists
  for downloads and is only used after a 403.
- Every error message, progress detail and log line is **scrubbed** before it
  is shown, including the URL-encoded form of a key.

Pinned by `tests/test_security_secrets.py`.

## Files

- Two tiers: the read-only factory library shipped in the pack, and your user
  tier under `<ComfyUI>/user/mrln/`. **A pack update never writes your tier**,
  and no code path can write the factory tier at runtime.
- Writes are atomic (temp file + `os.replace`), so a crash cannot truncate a
  library file.
- **Imported bundles are untrusted input.** Slugs are validated and every
  destination is containment-checked, so a bundle cannot write outside your
  user library; a downloaded LoRA is forced to a single directory and a
  `.safetensors` name; downloads are gated on a real Civitai AIR so a bundle
  cannot aim a fetch at an arbitrary host. Pinned by
  `tests/test_prompt_bundle_hardening.py` (44 cases, including path
  traversal, NUL truncation, Windows reserved names and fullwidth-dot
  variants).
- Request bodies are capped (1 MB) and oversized bodies are refused without
  being buffered.
- `MRLN_PROMPT_DIR` is **read** (never set) as an optional override for the
  user-tier location, used by the test suite for isolation.

## Note on the Comfy Registry security scan

Version 0.1.1 is marked *flagged* by the registry's automated YARA scan. All
six findings are `severity: info`, and are pattern matches on standard-library
calls rather than behaviour:

- 5 × `python_network_operations` — the literal `urllib.request` calls behind
  the Civitai, LLM-backend and preview-image features documented above.
- 1 × `python_environment_manipulation` — `os.environ.get("MRLN_PROMPT_DIR")`,
  a read of one optional variable.

These calls are the features, not a side effect of them, so they cannot be
removed without removing the features — and hiding them from the scanner would
be obfuscation, which the registry rightly prohibits and which this project
will not do. The behaviour behind each match is documented above and pinned by
the test suites named above.
