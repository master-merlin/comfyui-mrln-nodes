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

Prompt libraries are plain JSON files: factory content ships with the pack,
your personal library lives in `<ComfyUI>/user/mrln/prompt/` and survives
pack updates. A user file with the same name overrides the factory file.

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
  nodes/             # one module per domain (image.py, mask.py, ...)
web/js/              # frontend extensions (kept empty unless truly needed)
```

## License

[MIT](LICENSE)
