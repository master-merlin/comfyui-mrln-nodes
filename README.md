# ComfyUI-MRLN-Nodes

A multi-domain collection of custom nodes for [ComfyUI](https://github.com/comfyanonymous/ComfyUI).

> **Status: early scaffolding.** The pack architecture is in place; node domains are added incrementally.

## Install

Until the pack is on the [Comfy Registry](https://registry.comfy.org), install manually:

```bash
cd ComfyUI/custom_nodes
git clone <repo-url> ComfyUI-MRLN-Nodes
```

Restart ComfyUI afterwards. No extra Python dependencies are required.

## Nodes

Nodes appear in the Add-Node menu under **MRLN/**, grouped by domain, and every
display name carries an `(MRLN)` marker so they are easy to find in search.

| Domain | Nodes |
| ------ | ----- |
| `MRLN/prompt` | **Prompt Template** — template-driven prompt composition from a persistent JSON library (per-slot fixed/random with deterministic seeds, variants, negatives, 4 output formats incl. JSON); **Prompt Section** — a single library section as a standalone node for graph-native wiring |

Prompt libraries are plain JSON files: factory content ships with the pack
(including the OverDrive car-photography showcase: `overdrive/full-shot`,
plus staged `car-design` / `paintshop` / `scenery` / `action` templates for
image-edit pipelines), your personal library lives in
`<ComfyUI>/user/mrln/prompt/` and survives pack updates. A user file with
the same name overrides the factory file.

### Prompt Composer panel

On frontends with the sidebar-extension API, the pack adds a **Prompt
Composer** sidebar tab: browse the library, pick items per slot from
dropdowns, watch a live preview (prompt / negative / choices) as you click,
then *Apply to node* — it writes the plain selection lines into the selected
Prompt Template node, so workflows stay fully shareable and headless-safe.
The Library tab edits sections with a form (saves go to your user tier;
factory files get a copy-on-write override) and templates as validated raw
JSON. On frontends without the API the panel simply doesn't appear — the
nodes work identically without it.

The panel talks to the pack's own endpoints under `/mrln/prompt/*`
(registered only inside a running ComfyUI). The library is shared per
installation — in `--multi-user` setups all users see the same library.

## Design principles

- **Compatible by default** — nodes use the stable ComfyUI class API and
  server-side UI features (tooltips, descriptions, widget options), so they work
  identically in the legacy frontend and the new Nodes 2.0 UI, with no fragile
  frontend patching.
- **Zero-dependency core** — the pack installs nothing beyond what ComfyUI
  ships; domains that need optional libraries disable themselves gracefully
  when the library is missing instead of breaking the pack.
- **Stable node IDs** — node IDs (`MRLN_*`) are part of your saved workflows
  and are never renamed once released.

## Repository layout

```
__init__.py          # ComfyUI entry point (thin re-export shim)
mrln/
  pack.py            # pack identity: ID prefix, display marker, category root
  registry.py        # domain activation + fault-tolerant aggregation
  promptapi.py       # /mrln/prompt/* endpoints (soft-fails outside ComfyUI)
  promptlib/         # prompt engine (pure Python, zero dependencies)
  nodes/             # one module per domain (image.py, mask.py, ...)
  data/prompt/       # factory prompt library (sections + templates)
web/js/              # Prompt Composer sidebar panel (progressive enhancement)
```

## License

[MIT](LICENSE)
