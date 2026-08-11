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
| `MRLN/prompt` | **Prompt Template** — template-driven prompt composition from a persistent JSON library (per-slot fixed/random with deterministic seeds, variants, negatives, 4 output formats incl. JSON, `loras` output describing drawn LoRA blocks); **Prompt Section** — a single library section as a standalone node for graph-native wiring; **LoRA Apply** — loads the LoRA blocks a template drew onto MODEL/CLIP at their authored strengths (wire the `loras` output; trigger words stay in the prompt, loading stays out of it) |
| `MRLN/text` | **Show Text** — display any input as text inside the node (strings as-is, other types stringified, dicts/lists as pretty JSON) with a STRING passthrough output |

Prompt libraries are plain JSON files: a multiverse of factory content ships
with the pack, your personal library lives in `<ComfyUI>/user/mrln/prompt/`
and survives pack updates. Same-name SECTIONS compound: your file *extends*
the factory one by default (same item name wins, new items append,
`"hidden": true` tombstones a factory item, `"replaces": true` opts into a
full replacement). Same-name templates replace the factory file entirely.

### Factory library

135 sections / ~1600 curated items across dimension folders every template
shares (`location` incl. anime/sci-fi/fantasy/historical/underwater places,
`lighting` organic/dramatic/studio, `atmosphere` weather/season/mood/
particles, `viewpoint`, `camera` with 14 film stocks and real lens/format/
technique pools, `style` with genre/movement/anime/photography/palette/
quality-boost bundles, `composition`) and subject domains (`vehicle` — car,
motorcycle, aircraft, ship, train, sci-fi craft; `human` + `wardrobe` +
`pose`; `animal` + `nature`; `architecture`, `food`, `product`, `creature`).
Nearly every item carries a detailed long text plus a compact `text_short`
for tight tokenizers. Genre coupling runs on tags (`anime`, `scifi`,
`fantasy`, `historical`, `night`…) that templates select with per-slot
`tags_any` filters.

Showcase templates, one per domain:

| Template | What it composes |
| --- | --- |
| `overdrive/full-shot` | the OverDrive concept-car showcase (day/night variants) |
| `vehicle/aviation-shot`, `vehicle/maritime-shot` | aircraft/vessel × state × weather × long glass |
| `boudoir/session` | solo / duo / couple via nested model profiles (adults only, tasteful glamour) |
| `portrait/studio`, `character/concept` | studio portraits and character-design sheets |
| `anime/keyvisual`, `scifi/vista` | genre scenes over tagged locations |
| `wildlife/documentary`, `landscape/grand` | subject × behavior × habitat; landform hero shots |
| `food/editorial`, `product/hero`, `architecture/study` | commercial photography formulas |
| `vehicle/night-ride`, `vehicle/heritage-classic` | neon-noir motion (parked/rolling variants); classic-car portraiture with your own LoRA trigger |
| `vehicle/blueprint-sheet`, `vehicle/rider-lifestyle` | any machine as an annotated technical sheet; motorcycle culture with an optional nested rider |
| `poster/travel`, `whimsy/storybook` | any place as a vintage poster; the trending pet-in-handmade-media formula |
| `fantasy/epic-encounter`, `noir/night-scene` | archetype vs mythical creature at romanticist scale; one hard light on pushed Delta 3200 |
| `macro/small-world`, `astro/nightscape` | textures become landscapes; celestial heroes over dark landforms |
| `street/candid`, `moment/cozy` | decisive-moment street; hygge with the weather kept outside the window |

The human-domain content is strictly adults-only and kept at a tasteful
glamour level (lint-enforced); templates carry matching safety negatives by
default.

Factory updates never strand your saved templates: renamed section slugs
keep resolving through shipped aliases, and a template whose section
genuinely vanished still loads and runs — the dead slot is skipped with a
loud ⚠ in the choices output, and the Composer offers a one-click remap.

### Prompt Composer panel

On frontends with the sidebar-extension API, the pack adds a **Prompt
Composer** sidebar tab: browse the library, pick items per slot from
dropdowns, watch a live preview (prompt / negative / choices) as you click,
then *Apply to node* — it writes the plain selection lines into the selected
Prompt Template node, so workflows stay fully shareable and headless-safe.
The Library tab edits sections with a form — merged factory+user views mark
each item's tier (F/U), factory items can be hidden/restored, and saving
defaults to a thin "extend factory" diff that survives pack updates (full
replace available per save) — and templates as validated raw JSON. The
De-compose tab works the other way around: paste a finished prompt and it
is programmatically decomposed against your library — matched fragments
become slots pinned to their items, the residue becomes new items, new
sections, or prefix/suffix prose, and one click stores the whole mapping
as a template (the endpoint takes an `engine` parameter, so an Ollama/LLM
decomposer can plug in later). On frontends without the API the panel
simply doesn't appear — the nodes work identically without it.

The panel talks to the pack's own endpoints under `/mrln/prompt/*`
(registered only inside a running ComfyUI). The library is shared per
installation — in `--multi-user` setups all users see the same library.

### Example workflows

The pack ships ready-made graphs in `example_workflows/` — they appear in
ComfyUI's workflow template browser under this pack's name. Start with
**mrln-prompting**: a Prompt Template and a Prompt Section wired into Show
Text nodes, so you can explore the library, seeds and selection lines
before connecting anything to a sampler.

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

## Acknowledgements

Parts of the factory library's coverage were validated against, and a small
number of pool themes informed by, these openly licensed collections:

- [Awesome-AI-Image-Prompts](https://github.com/devanshug2307/Awesome-AI-Image-Prompts) (MIT)
- [nanobanana-trending-prompts](https://github.com/jau123/nanobanana-trending-prompts) (CC BY 4.0)
- [ComfyUI-Style-Prompts-Collection](https://github.com/vaulthunt3r/ComfyUI-Style-Prompts-Collection)
  served as a taxonomy reference (all texts in this pack are original)

All factory texts were authored for this pack; no collection content was
imported verbatim.

## License

[MIT](LICENSE)
