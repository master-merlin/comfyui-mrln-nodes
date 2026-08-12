// MRLN Prompt Composer — thumbnails: the tile every card, editor and LoRA pill
// draws, the set/reset/refresh controls, and the ONE cache-busting URL rule.
// The browse grid in tree.js and the editors all mount what this exports, so
// the resolution order lives here and nowhere else.
//
// RESOLUTION ORDER (SPEC §6.1: user thumb → LoRA preview → factory thumb →
// glyph). The user/factory step is NOT a client decision: GET /mrln/prompt/thumb
// already answers the user tier and falls through to the factory one — that is
// store.py's shadow model, and it is why nothing here ever names a tier. What
// is left for the client is the candidate CHAIN: the row's own (kind, slug),
// then the LoRA preview when the row bears a LoRA, then an optional fallback
// (an item's containing section), then the glyph. A 404 is a NORMAL outcome at
// every step — it means "no tile here", not an error, so it is never logged and
// never toasted; the chain just moves on.
//
// THE CACHE BUSTER. The server sends Last-Modified + Cache-Control: no-cache,
// so a replaced thumbnail at an unchanged URL can still come back from the
// browser cache as the OLD image. Every URL therefore carries `v=state.thumbEpoch`
// and every successful set/reset/refresh bumps that counter and re-loads the
// live tiles that name the changed (kind, slug).
//
// LORA IDENTITY IS NEVER SLUGIFIED HERE. `?kind=loras&slug=<the row's own
// `lora` value>` — the raw file name or AIR. thumbs.py::lora_slug reduces that
// identity to the storage key server-side so the rule lives in exactly one
// place; duplicating it in JS is how the two would drift.
//
// MOUNTING CONTRACT — every call returns ONE element, ready to append:
//   section editor   hub.thumbControls("sections", slug, {onChange})
//   template editor  hub.thumbControls("templates", slug, {onChange})
//   LoRA item row    hub.thumbControls("loras", item.data.lora,
//                      {section: sectionSlug, item: item.name, hasThumb: item.has_thumb,
//                       domain: sectionSlug, onChange})
//   LoRA pill (read-only tile, no controls)
//                    hub.thumbTile("loras", entry.lora,
//                      {hasThumb: entry.has_thumb, size: "sm", domain: entry.section_slug,
//                       fallback: {kind: "sections", slug: entry.section_slug}})
//   browse card      hub.thumbTile(kind, entry.slug, {hasThumb: entry.has_thumb, size: "lg"})
// The full opts, all optional:
//   hasThumb   the row's `has_thumb` flag (annotate_entries / annotate_items).
//              `false` means "the server already told us there is none" and the
//              tile draws its glyph WITHOUT a request — that is what keeps a
//              268-row listing at zero requests. OMIT IT when unknown; only an
//              explicit `false` suppresses.
//   lora       a LoRA identity to try after the row's own thumb
//   fallback   {kind, slug, hasThumb} tried last (an item → its section)
//   domain     the slug the GLYPH is derived from (an item → its section slug;
//              a LoRA file name is not a domain)
//   size       "sm" (pill) | "md" (default) | "lg" (card / editor)
//   title,alt,glyph  overrides
//   onChange   fires after a successful set/reset/refresh
//   reload     set false to skip the library reload afterChange does
//   canReset   set false to leave the "Reset to factory" button out
//   air|section+item|file   identity for the Civitai refresh; the button only
//              appears when `air` or `section`+`item` is present, because the
//              endpoint needs an AIR and answers 400 without one
//   hint       replaces the note under the buttons
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). Everything below is a
// declaration; all mutable state lives inside createThumbs().
import { routeWithQuery } from "./api.js";
import { armDestructive, busy, el, mount } from "./dom.js";
import { downscaleToDataUrl, wireDropZone } from "./image.js";

// ---- the server contract (mrln/promptapi/routes.py) ------------------------

export const THUMB_ROUTE = "/mrln/prompt/thumb";
export const THUMB_DELETE_ROUTE = "/mrln/prompt/thumb-delete";
export const LORA_PREVIEW_ROUTE = "/mrln/prompt/lora-preview";

/** The kinds the endpoint accepts (promptapi/thumbs.py KINDS). */
export const THUMB_KINDS = ["sections", "templates", "loras"];

/** Longest side an upload is downscaled to before it is sent (image.js caps). */
export const UPLOAD_MAX_SIDE = 768;

// ---- glyph fallback --------------------------------------------------------
// The tile a row without a thumbnail draws, derived from the FIRST segment of
// its slug — the domain. Unknown domains (every user-authored folder) fall back
// to a stable neutral shape rather than one shared box, so two user domains
// still look different and the same domain always looks the same.

export const LORA_GLYPH = "🎚";
export const UNKNOWN_GLYPH = "▦";
export const NEUTRAL_GLYPHS = ["◆", "●", "▲", "■", "★", "✦", "⬢", "❖"];

export const DOMAIN_GLYPHS = {
  animal: "🐾",
  anime: "🎌",
  architecture: "🏛",
  astro: "🌌",
  atmosphere: "🌫",
  battle: "⚔",
  boudoir: "🌹",
  camera: "📷",
  character: "🧍",
  composition: "🔲",
  creature: "🐉",
  design: "🎨",
  fantasy: "🧙",
  food: "🍜",
  human: "🧑",
  landscape: "🏞",
  lighting: "💡",
  location: "📍",
  loralab: "🧪",
  macro: "🔬",
  moment: "⏱",
  nature: "🌿",
  noir: "🕵",
  overdrive: "🚀",
  pose: "🕺",
  portrait: "👤",
  poster: "📰",
  product: "📦",
  scifi: "🛸",
  showcase: "✨",
  street: "🏙",
  style: "🖌",
  treasure: "💎",
  vehicle: "🚗",
  viewpoint: "👁",
  wardrobe: "👗",
  whimsy: "🎠",
  wildcards: "🃏",
  wildlife: "🦁",
}

/** The domain a slug belongs to: its first path segment, lowercased. */
export function domainOf(slug) {
  return String(slug ?? "")
    .trim()
    .replace(/\\/g, "/")
    .split("/")[0]
    .toLowerCase();
}

/**
 * The glyph for a slug's domain. Own-property lookup only — a user section
 * named 'constructor/…' or '__proto__/…' would otherwise pull a function off
 * Object.prototype and render '[object Function]' into the tile.
 */
export function glyphFor(slug) {
  const domain = domainOf(slug);
  if (!domain) return UNKNOWN_GLYPH;
  if (Object.hasOwn(DOMAIN_GLYPHS, domain)) return DOMAIN_GLYPHS[domain];
  let hash = 0;
  for (let i = 0; i < domain.length; i++) hash = (hash * 31 + domain.charCodeAt(i)) >>> 0;
  return NEUTRAL_GLYPHS[hash % NEUTRAL_GLYPHS.length];
}

/**
 * The glyph a tile draws. A LoRA identity is a FILE NAME, not a slug — its
 * first segment is a weights folder and says nothing about the domain — so a
 * LoRA tile without an explicit `domain` (its item's section) gets the LoRA
 * glyph instead of a hash of 'kits'.
 */
export function glyphForTile(kind, slug, domain = "") {
  if (domain) return glyphFor(domain);
  if (kind === "loras") return LORA_GLYPH;
  return glyphFor(slug);
}

// ---- the URL rule ----------------------------------------------------------

export function isThumbKind(kind) {
  return THUMB_KINDS.includes(String(kind ?? ""));
}

/** The identity of one candidate — dedupe key AND the "did this change?" key. */
export function candidateKey(kind, slug) {
  return `${kind}::${slug}`;
}

/**
 * The image URL for one (kind, slug) at one cache epoch. routeWithQuery is the
 * single encoding path (api.js), so a slug with '/', '&' or '+' in it cannot
 * corrupt the query. This is the ONE non-JSON route in the panel: the bytes are
 * fetched by the browser through an <img src>, never through apiJson.
 */
export function thumbRoute(kind, slug, epoch = 0) {
  return routeWithQuery(THUMB_ROUTE, { kind, slug, v: epoch });
}

/**
 * The ordered (kind, slug) pairs a tile tries before it gives up on the glyph.
 *
 * @param {string} kind    the row's own kind
 * @param {string} slug    the row's own slug — for `loras`, the RAW identity
 * @param {object} opts    {hasThumb, lora, loraHasThumb, fallback:{kind,slug,hasThumb},
 *                          trust}
 * `hasThumb === false` drops that candidate, which is what keeps a 268-row
 * listing from firing 268 guaranteed 404s. `trust: false` ignores every such
 * hint — used after a mutation, when the cached flags are known to be stale.
 */
export function thumbCandidates(kind, slug, opts = {}) {
  const trust = opts.trust !== false;
  const seen = new Set();
  const out = [];
  const add = (candidateKind, candidateSlug, known) => {
    if (!isThumbKind(candidateKind)) return;
    const id = String(candidateSlug ?? "").trim();
    if (!id) return;
    if (trust && known === false) return;
    const key = candidateKey(candidateKind, id);
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ kind: String(candidateKind), slug: id });
  };
  add(kind, slug, opts.hasThumb);
  add("loras", opts.lora, opts.loraHasThumb);
  if (opts.fallback) add(opts.fallback.kind, opts.fallback.slug, opts.fallback.hasThumb);
  return out;
}

/** thumbCandidates as ready-to-use <img> URLs, cache-buster included. */
export function thumbUrls(kind, slug, opts = {}, epoch = 0) {
  return thumbCandidates(kind, slug, opts).map((c) => thumbRoute(c.kind, c.slug, epoch));
}

/** Does this row get an <img> at all, or straight to the glyph? */
export function wantsTile(kind, slug, opts = {}) {
  return thumbCandidates(kind, slug, opts).length > 0;
}

/** Whether a "refresh Civitai preview" action can even be offered. */
export function canRefreshPreview(opts = {}) {
  return Boolean(opts.air || (opts.section && opts.item));
}

// ---- the factory -----------------------------------------------------------

export function createThumbs(hub) {
  const { ctx, state } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const loadLibrary = (...a) => hub.loadLibrary?.(...a);

  function failDetail(err) {
    return [err?.message, err?.remediation].filter(Boolean).join(" — ");
  }

  // An <img src> does NOT go through api.fetchApi, so it misses the api_base
  // prefix that a sub-path reverse proxy needs (/comfy/mrln/prompt/thumb…).
  // ctx.apiUrl is ComfyUI's own api.apiURL, injected by prompt_composer.js
  // precisely because this module may not import ComfyUI; the fallback keeps
  // the root-relative behavior for any ctx that predates the key.
  const absolute = (route) => (ctx.apiUrl ? ctx.apiUrl(route) : route);

  /** One <img> URL at the CURRENT epoch, for a caller that builds its own. */
  function thumbUrl(kind, slug) {
    return absolute(thumbRoute(kind, slug, state.thumbEpoch));
  }

  /**
   * Bump the cache epoch and repaint. `changed` is the (kind, slug) that was
   * just written: tiles naming it re-load with their `has_thumb` hints
   * DISTRUSTED (the row said "no tile" a moment ago and that is exactly what
   * just changed); every other tile is left alone, so one edit does not fire a
   * request per visible card.
   *
   * Live tiles are found by querying the panel root rather than kept in a
   * registry: a registry of every tile ever built would pin detached <img>
   * elements for the life of the session, and the DOM already knows which
   * tiles exist. The reloader rides the element the same way dom.js parks
   * `mrlnArmed` on an armed button — an own property, not a prototype patch.
   *
   * Matching is on the RAW identity, so a second tile spelling the same LoRA
   * differently ('kits/hy.safetensors' vs 'hy.safetensors' — one storage key
   * server-side) is not repainted here. It is repainted by the library reload
   * afterChange() runs, and slugifying in JS to close that gap is exactly the
   * duplication thumbs.py::lora_slug exists to prevent.
   */
  function bumpEpoch(changed = null) {
    state.thumbEpoch = (state.thumbEpoch ?? 0) + 1;
    for (const node of hub.root?.querySelectorAll?.(".mrln-thumb") ?? []) {
      if (typeof node.mrlnReloadThumb !== "function") continue;
      if (!changed) node.mrlnReloadThumb();
      else if (node.mrlnThumbKeys?.includes(candidateKey(changed.kind, changed.slug))) {
        node.mrlnReloadThumb({ trust: false });
      }
    }
  }

  /**
   * A thumbnail tile: the image when one resolves, the domain glyph otherwise.
   * Returns ONE element — mount it anywhere. See the module header for opts.
   */
  function thumbTile(kind, slug, opts = {}) {
    const glyph = opts.glyph ?? glyphForTile(kind, slug, opts.domain);
    const tile = el("span", {
      class: `mrln-thumb mrln-thumb-${opts.size ?? "md"}`,
      title: opts.title ?? null,
    });
    const glyphNode = () => el("span", { class: "mrln-thumb-glyph" }, glyph);

    function load({ trust = true } = {}) {
      const urls = thumbUrls(kind, slug, { ...opts, trust }, state.thumbEpoch).map(absolute);
      mount(tile, glyphNode());
      tile.classList.toggle("mrln-thumb-empty", urls.length === 0);
      if (!urls.length) return;
      attempt(0, urls);
    }

    function attempt(index, urls) {
      if (index >= urls.length) {
        // every candidate 404'd: a missing thumbnail is NORMAL, so the glyph
        // stays and nothing is logged
        mount(tile, glyphNode());
        tile.classList.add("mrln-thumb-empty");
        return;
      }
      const img = el("img", {
        class: "mrln-thumb-img",
        alt: opts.alt ?? "",
        loading: "lazy",
        decoding: "async",
        onload: () => {
          mount(tile, img);
          tile.classList.remove("mrln-thumb-empty");
        },
        onerror: () => attempt(index + 1, urls),
      });
      img.src = urls[index]; // last: the handlers must be attached first
    }

    // what this tile would ask for if no has_thumb hint were trusted — the key
    // set bumpEpoch matches a mutation against
    const keys = thumbCandidates(kind, slug, { ...opts, trust: false }).map((c) =>
      candidateKey(c.kind, c.slug)
    );
    tile.mrlnThumbKeys = keys;
    tile.mrlnReloadThumb = load;
    load();
    return tile;
  }

  // ---- mutations -----------------------------------------------------------

  async function afterChange(kind, slug, opts) {
    bumpEpoch({ kind, slug });
    // the server called lib.invalidate(), so the cached has_thumb flags in
    // state.library are stale — reload before anyone re-renders from them
    if (opts?.reload !== false) await loadLibrary();
    opts?.onChange?.({ kind, slug });
  }

  /**
   * Set (or replace) the USER-tier thumbnail from a dropped/pasted/picked file.
   * The raw file is never posted: image.js downscales it on a canvas first,
   * because the route's 1 MiB body cap turns a 4 MB PNG into a 413.
   */
  async function setThumbFromFile(kind, slug, file, opts = {}) {
    let image;
    try {
      image = await downscaleToDataUrl(file, { maxSide: UPLOAD_MAX_SIDE });
    } catch (err) {
      ctx.toast("error", "Could not read that image", failDetail(err));
      return false;
    }
    let body;
    try {
      body = await ctx.apiJson(THUMB_ROUTE, { method: "POST", body: { kind, slug, image } });
    } catch (err) {
      ctx.toast("error", "Thumbnail not saved", failDetail(err));
      return false;
    }
    ctx.toast(
      "success",
      "Thumbnail set",
      `${slug} — ${body.width}×${body.height}, ${Math.max(1, Math.round(body.bytes / 1024))} KB`
        + (body.overrides_factory ? " (shadows the shipped one — Reset brings it back)" : "")
    );
    await afterChange(kind, slug, opts);
    return true;
  }

  /**
   * Remove the USER thumbnail so the shipped one reappears. Destructive: the
   * user's own image is deleted, so the caller arms this.
   */
  async function resetThumb(kind, slug, opts = {}) {
    let body;
    try {
      body = await ctx.apiJson(THUMB_DELETE_ROUTE, { method: "POST", body: { kind, slug } });
    } catch (err) {
      ctx.toast("error", "Reset failed", failDetail(err));
      return false;
    }
    if (!body.removed) {
      ctx.toast("warn", "Nothing to reset", `${slug} has no thumbnail of yours to remove`);
    } else {
      ctx.toast(
        "success",
        body.reverted_to_factory ? "Reverted to the shipped thumbnail" : "Thumbnail removed",
        body.reverted_to_factory ? slug : `${slug} — the domain glyph shows again`
      );
    }
    await afterChange(kind, slug, opts);
    return true;
  }

  /**
   * The DELIBERATE Civitai preview refresh. Automatic capture never overwrites
   * an existing tile, so replacing one has to be asked for — `force: true`.
   * A 200 can still carry `ok: false` (every preview of that version is a video
   * or rated above PG-13): that is an answer, not a failure.
   */
  async function refreshLoraPreview(slug, opts = {}) {
    let body;
    try {
      body = await ctx.apiJson(LORA_PREVIEW_ROUTE, {
        method: "POST",
        body: {
          air: opts.air || undefined,
          section: opts.section || undefined,
          item: opts.item || undefined,
          file: opts.file || slug || undefined,
          force: true,
        },
      });
    } catch (err) {
      ctx.toast("error", "Preview refresh failed", failDetail(err));
      return false;
    }
    if (!body.ok) {
      ctx.toast("warn", "No preview taken", body.reason ?? "Civitai offered nothing usable");
      return false;
    }
    ctx.toast("success", "Preview updated", `${body.slug} — fetched from Civitai`);
    await afterChange("loras", slug || body.slug, opts);
    return true;
  }

  /**
   * The set/reset/refresh strip, with the tile itself as the drop target.
   * Returns ONE element — see the module header for the mount one-liners.
   */
  function thumbControls(kind, slug, opts = {}) {
    const tile = thumbTile(kind, slug, { ...opts, size: opts.size ?? "lg" });
    const drop = el(
      "div",
      {
        class: "mrln-thumb-drop",
        tabindex: "0", // paste is listened for ON the zone — unfocusable = never pasteable
        title: "Drop or paste an image here to use it as the thumbnail",
      },
      tile
    );
    const picker = el("input", {
      type: "file",
      accept: "image/*",
      class: "mrln-thumb-file",
      style: "display:none",
    });

    const chooseBtn = el(
      "button",
      {
        class: "mrln-btn mrln-mini",
        title: "Pick an image file — it is downscaled in the browser and stored as a "
          + "256 px webp in your user library",
        onclick: () => picker.click(),
      },
      "Set image…"
    );
    const apply = (file) => busy(chooseBtn, () => setThumbFromFile(kind, slug, file, opts));
    picker.addEventListener("change", () => {
      const file = picker.files?.[0];
      picker.value = ""; // so re-picking the same file fires 'change' again
      if (file) apply(file);
    });
    wireDropZone(drop, {
      onImage: apply,
      // A LINK is not an image: the browser cannot read another origin's
      // pixels (CORS) and this endpoint takes bytes, not URLs. Saying so beats
      // a drop that silently does nothing.
      onUrl: () =>
        ctx.toast(
          "warn",
          "Drop the image, not the link",
          "A URL cannot be read by the browser — save the picture and drop the file "
            + "(or use the image intake in the De-compose tab for a Civitai link)."
        ),
    });

    const actions = [chooseBtn];
    if (opts.canReset !== false) {
      actions.push(
        el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            title: kind === "loras"
              ? "Delete this preview — the LoRA falls back to the domain glyph until "
                + "a new Civitai preview is captured"
              : "Delete YOUR thumbnail — the shipped one (if any) shows through again. "
                + "Factory thumbnails are never deletable.",
            // two-step arm: this discards a user thumbnail irrecoverably, and
            // window.confirm throws on the Electron frontend
            onclick: (e) =>
              armDestructive(e.currentTarget, "Really reset?", () =>
                busy(e.currentTarget, () => resetThumb(kind, slug, opts))
              ),
          },
          "Reset to factory"
        )
      );
    }
    if (kind === "loras" && canRefreshPreview(opts)) {
      actions.push(
        el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            title: "Fetch this LoRA's Civitai preview again and REPLACE the current tile "
              + "(automatic capture never overwrites one, so this is the way to redo it)",
            onclick: (e) =>
              busy(e.currentTarget, () => refreshLoraPreview(slug, opts)),
          },
          "Refresh preview"
        )
      );
    }

    return el(
      "div",
      { class: "mrln-thumb-controls mrln-inline" },
      drop,
      el(
        "div",
        { class: "mrln-thumb-side" },
        el("div", { class: "mrln-actions" }, ...actions),
        el(
          "div",
          { class: "mrln-note mrln-thumb-hint" },
          opts.hint
            ?? "Drop or paste an image on the tile. It is stored in YOUR library as a "
              + "256 px webp; the shipped thumbnail is only shadowed, never overwritten."
        ),
        picker
      )
    );
  }

  return {
    bumpThumbEpoch: bumpEpoch,
    refreshLoraPreview,
    resetThumb,
    setThumbFromFile,
    thumbControls,
    thumbTile,
    thumbUrl,
  };
}
