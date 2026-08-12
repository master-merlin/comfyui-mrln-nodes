# ComfyUI-MRLN-Nodes

A multi-domain collection of custom nodes for [ComfyUI](https://github.com/comfyanonymous/ComfyUI).

> **Status: first release.** The prompt domain ships complete — engine, factory
> library, Composer sidebar, LoRA integration and LLM tooling; further node
> domains are added incrementally.

## Install

Until the pack is on the [Comfy Registry](https://registry.comfy.org), install manually:

```bash
cd ComfyUI/custom_nodes
git clone <repo-url> ComfyUI-MRLN-Nodes
```

Restart ComfyUI afterwards. No extra Python dependencies are required: the
prompt engine and the nodes are pure standard library, and the two features
that read image files (dropping a generated image on the Composer, and
thumbnails) use Pillow and PyYAML — which ComfyUI itself ships — through soft
imports that disable just those features if the libraries are ever absent.

## Nodes

Nodes appear in the Add-Node menu under **MRLN/**, grouped by domain, and every
display name carries an `(MRLN)` marker so they are easy to find in search.

| Domain | Nodes |
| ------ | ----- |
| `MRLN/prompt` | **Prompt Template** — template-driven prompt composition from a persistent JSON library (per-slot fixed/random with deterministic seeds, variants, negatives, 4 output formats incl. JSON, target-model `profile` selector that can also swap in a per-profile tuned variant of the template *and* reorder the rendered blocks into the reading order that model rewards; six outputs — `prompt`, `negative`, `choices`, `loras`, `llm`, `gen_info`). `batch_count` renders a whole batch from one queue: `increment seed` draws item *i* at seed + *i*, so four images are four different draws instead of four copies of one; `combinatorial` instead enumerates every combination of the slots left on random, capped at 512. Every output is a list, and a length-1 list is indistinguishable from a single value downstream, so existing workflows are untouched. `gen_info` is an A1111 `parameters` string a metadata-capable save node can embed — carrying only what this node actually knows (the prompts, the seed it drew with, and the Civitai ids of the LoRAs it selected), never a guessed Steps/Sampler/CFG/Model; **Prompt Section** — a single library section as a standalone node for graph-native wiring; **LoRA Apply** — loads the LoRA blocks a template drew onto MODEL/CLIP at their authored strengths (wire the `loras` output; trigger words stay in the prompt, loading stays out of it); **Prompt Enhance** — rewrites the prompt with a local (Ollama / LM Studio) or cloud (Anthropic / OpenAI / Gemini / OpenRouter) LLM under the selected profile's per-model system prompt: ONE wire (the Template node's `llm` output carries prompt + system + protected LoRA trigger words, which are enforced verbatim and re-injected if the LLM rewrites them), a model dropdown listing installed models plus pull suggestions Ollama downloads on pick, deterministic per seed, VRAM freed/kept per choice, pass-through on backend failure. Best for thin hand-typed prompts, tag→prose conversion and de-compose assistance — the curated library prompts usually render better un-rewritten |
| `MRLN/text` | **Show Text** — display any input as text inside the node (strings as-is, other types stringified, dicts/lists as pretty JSON) with a STRING passthrough output |

Prompt libraries are plain JSON files: a multiverse of factory content ships
with the pack, your personal library lives in `<ComfyUI>/user/mrln/prompt/`
and survives pack updates. Same-name SECTIONS compound: your file *extends*
the factory one by default (same item name wins, new items append,
`"hidden": true` tombstones a factory item, `"replaces": true` opts into a
full replacement). Same-name templates replace the factory file entirely.

### Factory library

208 sections / ~2900 curated items across dimension folders every template
shares (`location` incl. anime/sci-fi/fantasy/historical/underwater/coastal
places, `lighting` organic/dramatic/studio, `atmosphere` weather/season/mood/
particles/reentry, `viewpoint`, `camera` with film stocks and real lens/
format/technique pools plus per-genre glass, `style` with genre/movement/
anime/photography/palette/quality-boost bundles, `composition`) and subject
domains (`vehicle` — car, motorcycle, aircraft, ship, train, sci-fi craft;
`human` + `wardrobe` + `pose`; `animal` + `nature` + `creature`;
`architecture`, `food`, `product`, `battle`, `treasure`). Each item pairs a
detailed long text with a compact `text_short`, so tag-based targets get tag
flow and prose models get prose from the same library. Genre coupling runs on
tags (`anime`, `scifi`, `fantasy`, `historical`, `night`…) that templates
select with per-slot `tags_any` / `tags_none` filters.

Flagship templates — every one permutation-tested seed by seed so any draw
combination reads like a specialist wrote the brief:

| Template | What it composes |
| --- | --- |
| `overdrive/full-shot`, `overdrive/night-pursuit`, `overdrive/design-studio` | the OverDrive concept-car showcase (day/night variants), neon-noir pursuit, design-studio reveal |
| `vehicle/apex-attack`, `vehicle/night-ride`, `vehicle/heritage-classic` | motorsport at the limit; neon-noir motion (parked/rolling); classic-car portraiture with your own LoRA trigger |
| `vehicle/aviation-shot`, `vehicle/maritime-shot`, `vehicle/blueprint-sheet`, `vehicle/rider-lifestyle` | aircraft/vessel × state × weather × long glass; any machine as an annotated technical sheet; motorcycle culture with an optional nested rider |
| `scifi/fleet-arrival`, `scifi/vista`, `scifi/mecha-clash`, `scifi/android-portrait` | capital fleets making atmospheric entry (day-raid/night-siege), genre vistas, mech combat, synthetic portraiture |
| `fantasy/epic-encounter`, `fantasy/battlefield-charge`, `fantasy/dragon-hoard`, `fantasy/realm-vista`, `fantasy/enchanted-portrait` | archetype vs mythical creature at romanticist scale; massed armies (charge/last-stand/aftermath); treasure-light interiors; realm panoramas |
| `anime/cinema-still`, `anime/keyvisual`, `anime/slice-of-life`, `anime/character` | theatrical anime master shots, key visuals, quiet everyday scenes, character sheets |
| `portrait/studio`, `portrait/character-concept`, `portrait/noir-night`, `portrait/street-candid` | studio portraits, character-design sheets, one hard light on pushed Delta 3200, decisive-moment street |
| `boudoir/session`, `boudoir/vanity-portrait`, `boudoir/window-silhouette` | solo / duo / couple via nested model profiles (adults only, tasteful glamour) |
| `animal/documentary`, `animal/small-world`, `animal/storybook` | subject × behavior × habitat; macro textures as landscapes; pet-in-handmade-media |
| `landscape/grand`, `landscape/astro-nightscape`, `landscape/moody-intimate` | landform hero shots; celestial heroes over dark landforms; intimate weather |
| `architecture/study`, `architecture/blue-hour-icon`, `architecture/golden-interior`, `architecture/cozy-moment` | architectural formulas from icon to interior to hygge |
| `food/editorial`, `food/dark-mood`, `food/pour-shot`, `product/hero`, `product/lifestyle-scene`, `product/teardown-sheet` | commercial photography formulas |
| `design/travel-poster`, `design/movie-poster`, `design/album-cover` | any place as a vintage poster; one-sheet and sleeve design |
| `showcase/krea2-art-direction`, `showcase/flux1-reportage`, `showcase/flux2-storefront`, `showcase/qwen-typographic`, `showcase/zimage-bilingual-counter` | one per target model, each built to what that family actually rewards — KREA 2's art-direction layering, FLUX.1's early-token weighting, FLUX.2's structured scene plus lettering spec, Qwen-Image's layout control and in-image type, Z-Image's all-positive phrasing with bilingual text. Each pins its own profile, so picking the template already targets the model |
| `showcase/ideogram4-type-poster` | Ideogram 4.0's typography and flat graphic design, composed straight into that model's structured JSON caption: the `ideogram4` profile carries the caption scaffold and the template extends it with a second element that holds the literal headline and its bounding box. The headline is a slot rather than a template variable, because the JSON filler sees slot texts. Supplying a JSON caption contractually disables Ideogram's magic-prompt rewriting, so this is the one target that renders the composed prompt verbatim — in stock ComfyUI, wire it to `IdeogramPImage` with `prompt_upsampling` OFF (the stock `IdeogramV4` node only accepts a plain text prompt, which re-enables the rewrite) |

The human-domain content is strictly adults-only and kept at a tasteful
glamour level (lint-enforced); templates carry matching safety negatives by
default.

### LoRA Lab

`loralab/*` sections curate well-known community LoRAs as ordinary library
items — detail boosters, film looks, portrait realism, sci-fi and anime style
movers, and vehicle icons — each with its trained trigger woven into the item
text, its authored strengths, and its Civitai AIR urn. `showcase/detail-portrait`,
`showcase/night-machine` and `showcase/style-lab` put them to work end to end: compose, and the drawn
LoRAs leave through the `loras` output into **LoRA Apply** while their trigger
words ride the prompt; a file you don't have yet is offered for download by
its AIR, verified by SHA256, and the item is healed to point at it. Every
LoRA-bearing slot pins exactly one base-model family by tag, so a random draw
can never stack a Flux LoRA onto an SDXL render.

A LoRA item may carry `data.lora_info` — `{name, creator, base, url, about}`
— a render-inert provenance card naming the model, its creator, its base
family, its Civitai page and what it does. It never reaches the prompt or any
output wire; it exists so the Composer can tell you where a file came from
and why it is on the list.

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
replace available per save) — and templates as validated raw JSON;
**New section…** and **New template…** start net-new compositions from a
blank slate (the green ＋ next to the template picker does the same).
**New combine…** groups several sections into one draw pool: pick the
sections, give each a weight, and every entry delegates to its source —
one slot then draws "urban *or* nature *or* studio". Re-opening such a
section returns to that pick-and-weight view rather than a table of
delegations.

A template that draws LoRA items warns you *before* you render: the
Compose tab lists any `.safetensors` this machine is missing and offers
a one-click fetch for the ones carrying a Civitai AIR. The same audit
runs at server start (it logs what is missing), and **LoRA Apply** has an
`on_missing` choice — stop with a named file, skip it and render without,
or download it by its AIR — so a shared workflow can heal itself without
the Composer ever being opened.

LoRA blocks also declare the base model they were trained for (`data.base`,
or the ecosystem segment of their AIR). LoRA Apply reads the architecture of
the connected model and, via `on_mismatch`, warns / skips / stops when they
disagree — a FLUX LoRA on an SDXL checkpoint loads without any error from
ComfyUI and simply degrades the image, which is otherwise invisible. The
De-compose tab works the other way around: paste a finished prompt and it
is decomposed against your library — matched fragments become slots pinned
to their items, the residue becomes new items, new sections, or
prefix/suffix prose, and one click stores the whole mapping as a template.
Three engines: `programmatic` (offline token matcher), `llm` (a configured
local or cloud backend splits and maps against the library catalog) and
`hybrid` (the programmatic result rides in the LLM system prompt as
suggestions to verify or correct); every LLM assignment is validated
against the real library, and a failing backend falls back to the
programmatic result instead of erroring.

**Start from an image you like.** Drop a generated PNG/JPEG/WebP on the
De-compose tab (or paste its civitai.com URL) and the metadata it carries is
read server-side — A1111 / Forge / Civitai `parameters`, an embedded ComfyUI
graph, or EXIF `UserComment`. You then pick one of two things to do with it,
and the panel never decides for you: **Use as-is** builds a template that
reproduces that prompt byte for byte (no LLM on this path, so it works with
every backend unset), or **Decompose** hands the extracted text to the
de-composer above. Inline `<lora:…>` tags are lifted out and resolved to
local files or Civitai AIRs. When an embedded graph is genuinely ambiguous
about which string is the positive, you get a picker rather than a guess.

**Optimize for a model without duplicating the template.** Reading order
changes results, and one template cannot store a copy per target — so order
is a render-time function of the profile. The Compose tab's *Optimize for…*
select renders the current and the target profile side by side, lists the
reading order with the blocks that moved, and separates "the order changed"
from the things that are not order (format, negative policy, a different
draw). If you want the new order made permanent, one explicit button writes
it into a copy — never automatically.

**Trigger words get mute/solo.** A LoRA's full trained-word list is
provenance and is never edited; the words that actually render are the
truth. Muting one drops it, soloing collapses to the rest muted, and the
state round-trips through the file itself, so re-opening the editor
re-derives it with nothing to lose. A LoRA whose trigger is baked in (or
unwanted) can mute all of them and contribute no text at all while still
loading its weights.

**Thumbnails.** Sections, templates and LoRAs can carry a 256 px webp tile,
and the Library tab switches between rows and a card grid. Drop or paste an
image to set one; *reset to factory* removes yours and the shipped one
reappears. A LoRA downloaded from Civitai brings its own preview along
(PG/PG-13 only, and nothing at all rather than something you did not ask
for). Your thumbnails live in the user tier, so a pack update can neither
overwrite nor delete them.

**History.** Every render the Prompt Template node makes is recorded as one
line — newest first, with restore (template, profile, seed, mode, selection,
variables, format, length and conflict policy, all nine, so it reproduces
rather than approximates) and copy-prompt. A batch collapses to one row.
Recording and retention are settings, clearing is a two-step confirm, and a
failed history write can never break a render that already succeeded.

**Coming from A1111 or a wildcard collection?** Two importers read wildcards
— a folder of `.txt`/`.yaml` files, or the `.zip` a published pack actually
ships as — or an A1111 `styles.csv`, and dry-run the result through the same
plan preview the bundle importer uses, so you see exactly what would be
written before anything is. Weighted lines (`3::rare option`) are honored,
and the plan says plainly which third-party syntax survives the trip and
which does not.

The Settings tab holds the local backend URLs (Ollama / LM Studio,
auto-validated with installed-model lists), the cloud API keys — stored
server-side in your user tier, never echoed back and never written into
workflows — the history retention controls, and the switch that allows an
LLM backend on another machine (off by default: ComfyUI itself makes that
request, so a non-loopback URL turns the box into a probe for whatever
address is in the field). On frontends without the API the panel simply
doesn't appear — the nodes work identically without it.

Templates and sections travel: every template/section row offers **⤓
Export**, which bundles the template together with all your user-tier
sections it draws from (factory content resolves on the other install)
plus the Civitai AIR links of any LoRAs involved. **Import…** dry-runs the
bundle first — you see exactly what will be written, colliding files are
kept unless you opt into overwrite — and opening the imported template
offers to auto-download missing LoRA files. Share the bundle next to your
workflow and the recipient rebuilds your renders end to end.

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
  when the library is missing instead of breaking the pack. Anything ComfyUI
  already guarantees (Pillow, PyYAML) is used through a soft import and never
  appears in `requirements.txt`, so installing this pack can never change the
  versions your other nodes depend on.
- **Stable node IDs** — node IDs (`MRLN_*`) are part of your saved workflows
  and are never renamed once released.

## Repository layout

```
__init__.py          # ComfyUI entry point (thin re-export shim)
mrln/
  pack.py            # pack identity: ID prefix, display marker, category root
  registry.py        # domain activation + fault-tolerant aggregation
  promptapi/         # /mrln/prompt/* endpoints (soft-fails outside ComfyUI)
  promptlib/         # prompt engine (pure Python, zero dependencies)
  nodes/             # one module per domain (prompt.py, text.py, ...)
  data/prompt/       # factory prompt library (sections + templates + thumbs)
web/js/              # Prompt Composer sidebar panel (progressive enhancement)
  composer/          # its modules — one per tab/concern, no side effects
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
