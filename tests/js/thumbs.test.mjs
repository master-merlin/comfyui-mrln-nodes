// web/js/composer/thumbs.js — the PURE half: the URL rule (including the cache
// buster), the resolution order, the glyph derivation and the has_thumb → tile
// decision. The DOM half (thumbTile/thumbControls) is not unit-tested for the
// same reason the rest of the panel is not: no jsdom, no npm (see
// tests/js/README.md). Everything asserted here is what a wrong tile, a stale
// tile or a storm of guaranteed 404s would come from.
//
// The server contract these encode lives in mrln/promptapi/thumbs.py and
// mrln/promptapi/routes.py; the last describe() block reads the route table
// itself so a renamed endpoint fails HERE instead of silently at runtime.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  DOMAIN_GLYPHS,
  LORA_GLYPH,
  LORA_PREVIEW_ROUTE,
  NEUTRAL_GLYPHS,
  THUMB_DELETE_ROUTE,
  THUMB_KINDS,
  THUMB_ROUTE,
  UNKNOWN_GLYPH,
  candidateKey,
  canRefreshPreview,
  createThumbs,
  domainOf,
  glyphFor,
  glyphForTile,
  isThumbKind,
  thumbCandidates,
  thumbRoute,
  thumbUrls,
  wantsTile,
} from "../../web/js/composer/thumbs.js";

const SRC = fileURLToPath(new URL("../../web/js/composer/thumbs.js", import.meta.url));
const TREE_SRC = fileURLToPath(new URL("../../web/js/composer/tree.js", import.meta.url));
const ROUTES_PY = fileURLToPath(new URL("../../mrln/promptapi/routes.py", import.meta.url));

// ---------------------------------------------------------------------------
// the URL rule
// ---------------------------------------------------------------------------

describe("thumbRoute", () => {
  test("names the kind, the slug and the cache epoch", () => {
    assert.equal(
      thumbRoute("sections", "vehicle/muscle-car", 0),
      "/mrln/prompt/thumb?kind=sections&slug=vehicle%2Fmuscle-car&v=0"
    );
  });

  test("the epoch is ALWAYS in the URL — that is the whole cache buster", () => {
    // Last-Modified + Cache-Control: no-cache means an unchanged URL can come
    // back from the browser cache as the OLD image after a replacement. If
    // this ever stops differing, a replaced thumbnail stays invisible.
    const before = thumbRoute("templates", "noir/rain", 3);
    const after = thumbRoute("templates", "noir/rain", 4);
    assert.notEqual(before, after);
    assert.match(before, /[?&]v=3$/);
    assert.match(after, /[?&]v=4$/);
  });

  test("v=0 survives the query encoder's empty-value drop", () => {
    // api.js::queryString drops '' / null / undefined — 0 is none of those,
    // and a first-load URL without the parameter would not match the ones
    // built after a bump (two cache entries for the same tile).
    assert.ok(thumbRoute("sections", "human/pose", 0).includes("v=0"));
  });

  test("everything is encoded exactly once", () => {
    const url = thumbRoute("loras", "kits/hy cade&x+1.safetensors", 2);
    assert.equal(
      url,
      "/mrln/prompt/thumb?kind=loras&slug=kits%2Fhy%20cade%26x%2B1.safetensors&v=2"
    );
    assert.equal(url.split("?")[1].split("&").length, 3); // kind, slug, v — no injected pair
  });

  test("a LoRA identity is passed RAW, never slugified in JS", () => {
    // thumbs.py::lora_slug owns that reduction ('kits\\hycade.safetensors' and
    // 'hycade.safetensors' must land on the same key) — doing it here too is
    // how the two implementations would drift.
    const identity = "kits\\Hycade_v3.safetensors";
    const url = thumbRoute("loras", identity, 0);
    assert.equal(decodeURIComponent(url.split("slug=")[1].split("&")[0]), identity);
    const air = "urn:air:sdxl:lora:civitai:12345@67890";
    assert.ok(thumbRoute("loras", air, 0).includes(encodeURIComponent(air)));
  });
});

describe("isThumbKind", () => {
  test("exactly the three kinds the server accepts", () => {
    assert.deepEqual(THUMB_KINDS, ["sections", "templates", "loras"]);
    for (const kind of THUMB_KINDS) assert.equal(isThumbKind(kind), true);
    for (const kind of ["items", "profiles", "", null, undefined, "Sections"]) {
      assert.equal(isThumbKind(kind), false);
    }
  });
});

// ---------------------------------------------------------------------------
// the resolution order
// ---------------------------------------------------------------------------

describe("thumbCandidates — the resolution order", () => {
  test("a plain section is one candidate: the server resolves user→factory", () => {
    assert.deepEqual(thumbCandidates("sections", "vehicle/muscle-car"), [
      { kind: "sections", slug: "vehicle/muscle-car" },
    ]);
  });

  test("LoRA preview, then the containing section, then the glyph", () => {
    // SPEC §6.1: user thumb → LoRA preview → factory thumb → glyph. The
    // user/factory step is the SERVER's (one URL), so what is left is this
    // chain; the empty tail is the glyph.
    assert.deepEqual(
      thumbCandidates("loras", "hycade.safetensors", {
        fallback: { kind: "sections", slug: "loralab/hycade" },
      }),
      [
        { kind: "loras", slug: "hycade.safetensors" },
        { kind: "sections", slug: "loralab/hycade" },
      ]
    );
  });

  test("an explicit `lora` rides between the row's own thumb and the fallback", () => {
    assert.deepEqual(
      thumbCandidates("sections", "loralab/hycade", {
        lora: "hycade.safetensors",
        fallback: { kind: "templates", slug: "showcase/hycade" },
      }),
      [
        { kind: "sections", slug: "loralab/hycade" },
        { kind: "loras", slug: "hycade.safetensors" },
        { kind: "templates", slug: "showcase/hycade" },
      ]
    );
  });

  test("has_thumb === false drops that candidate — no guaranteed 404", () => {
    // 268 shipped rows: a listing with no thumbnails must cost ZERO requests.
    assert.deepEqual(thumbCandidates("sections", "human/pose", { hasThumb: false }), []);
    assert.deepEqual(
      thumbCandidates("loras", "hycade.safetensors", {
        hasThumb: false,
        fallback: { kind: "sections", slug: "loralab/hycade", hasThumb: true },
      }),
      [{ kind: "sections", slug: "loralab/hycade" }]
    );
  });

  test("has_thumb undefined means UNKNOWN, so the candidate is tried", () => {
    // annotate_items only sets the flag on LoRA-bearing rows; absent must
    // never read as 'false'.
    assert.equal(thumbCandidates("sections", "human/pose", {}).length, 1);
    assert.equal(
      thumbCandidates("sections", "human/pose", { hasThumb: undefined }).length,
      1
    );
  });

  test("trust:false ignores every hint — the post-mutation re-check", () => {
    // The row said 'no thumbnail' a moment ago and that is exactly what just
    // changed; trusting the stale flag would leave the tile a glyph.
    assert.deepEqual(
      thumbCandidates("sections", "human/pose", { hasThumb: false, trust: false }),
      [{ kind: "sections", slug: "human/pose" }]
    );
  });

  test("empty slugs, unknown kinds and duplicates are dropped", () => {
    assert.deepEqual(thumbCandidates("sections", ""), []);
    assert.deepEqual(thumbCandidates("sections", "   "), []);
    assert.deepEqual(thumbCandidates("items", "human/pose"), []);
    assert.deepEqual(
      thumbCandidates("loras", "hycade.safetensors", {
        lora: "hycade.safetensors",
        fallback: { kind: "loras", slug: "hycade.safetensors" },
      }),
      [{ kind: "loras", slug: "hycade.safetensors" }]
    );
  });

  test("candidateKey separates the kind from the slug", () => {
    // The kind comes from a CLOSED set, so the separator only has to
    // disambiguate legal kinds — the same slug under two kinds is two keys,
    // and a slug containing '::' cannot collide with anything because no legal
    // kind is a prefix of another.
    for (const slug of ["x", "a::b", "vehicle/muscle", ""]) {
      const keys = THUMB_KINDS.map((kind) => candidateKey(kind, slug));
      assert.equal(new Set(keys).size, THUMB_KINDS.length, `collision for slug '${slug}'`);
    }
    assert.equal(candidateKey("loras", "hy.safetensors"), candidateKey("loras", "hy.safetensors"));
    assert.notEqual(candidateKey("sections", "a/b"), candidateKey("sections", "a/c"));
  });
});

describe("thumbUrls / wantsTile", () => {
  test("the candidate chain becomes <img> URLs at the current epoch", () => {
    assert.deepEqual(
      thumbUrls("loras", "hy.safetensors", { fallback: { kind: "sections", slug: "loralab/hy" } }, 7),
      [
        "/mrln/prompt/thumb?kind=loras&slug=hy.safetensors&v=7",
        "/mrln/prompt/thumb?kind=sections&slug=loralab%2Fhy&v=7",
      ]
    );
  });

  test("wantsTile is the has_thumb → tile decision", () => {
    assert.equal(wantsTile("sections", "human/pose"), true);
    assert.equal(wantsTile("sections", "human/pose", { hasThumb: false }), false);
    assert.equal(wantsTile("sections", ""), false);
    // …but a LoRA-bearing row with no section thumb still has its preview
    assert.equal(
      wantsTile("sections", "loralab/hy", { hasThumb: false, lora: "hy.safetensors" }),
      true
    );
  });
});

// ---------------------------------------------------------------------------
// the glyph fallback
// ---------------------------------------------------------------------------

describe("domainOf / glyphFor", () => {
  test("the domain is the slug's FIRST segment, lowercased", () => {
    assert.equal(domainOf("vehicle/muscle/car"), "vehicle");
    assert.equal(domainOf("Vehicle"), "vehicle");
    assert.equal(domainOf("  human/pose  "), "human");
    assert.equal(domainOf("kits\\hy.safetensors"), "kits"); // backslashes count too
    assert.equal(domainOf(""), "");
    assert.equal(domainOf(null), "");
    assert.equal(domainOf(undefined), "");
  });

  test("shipped domains all have a hand-picked glyph", () => {
    // every top-level folder under mrln/data/prompt/{sections,templates}
    for (const domain of [
      "animal", "anime", "architecture", "astro", "atmosphere", "battle", "boudoir",
      "camera", "character", "composition", "creature", "design", "fantasy", "food",
      "human", "landscape", "lighting", "location", "loralab", "macro", "moment",
      "nature", "noir", "overdrive", "pose", "portrait", "poster", "product", "scifi",
      "showcase", "street", "style", "treasure", "vehicle", "viewpoint", "wardrobe",
      "whimsy", "wildlife",
    ]) {
      assert.ok(Object.hasOwn(DOMAIN_GLYPHS, domain), `no glyph for the '${domain}' domain`);
      assert.equal(glyphFor(`${domain}/anything`), DOMAIN_GLYPHS[domain]);
    }
  });

  test("an unknown domain gets a STABLE neutral shape, not one shared box", () => {
    const mine = glyphFor("my-stuff/thing");
    assert.ok(NEUTRAL_GLYPHS.includes(mine));
    assert.equal(mine, glyphFor("my-stuff/other")); // same domain, same glyph
    // and different domains are allowed to differ (this pair does)
    assert.notEqual(glyphFor("alpha/x"), glyphFor("delta/x"));
  });

  test("an empty slug falls back to the neutral placeholder", () => {
    assert.equal(glyphFor(""), UNKNOWN_GLYPH);
    assert.equal(glyphFor(null), UNKNOWN_GLYPH);
  });

  test("a slug named after an Object.prototype key does not leak a function", () => {
    // 'constructor/…' or '__proto__/…' is a legal user folder; a plain
    // DOMAIN_GLYPHS[domain] lookup would render '[object Function]'.
    for (const evil of ["constructor", "__proto__", "toString", "hasOwnProperty"]) {
      const glyph = glyphFor(`${evil}/thing`);
      assert.equal(typeof glyph, "string");
      assert.ok(NEUTRAL_GLYPHS.includes(glyph), `${evil} leaked ${glyph}`);
    }
  });
});

describe("glyphForTile", () => {
  test("a LoRA identity is a FILE NAME — its first segment is not a domain", () => {
    // 'kits/hycade.safetensors' would otherwise hash the weights folder
    assert.equal(glyphForTile("loras", "kits/hycade.safetensors"), LORA_GLYPH);
    assert.equal(glyphForTile("loras", "hycade.safetensors"), LORA_GLYPH);
  });

  test("an explicit domain (the item's section) wins for every kind", () => {
    assert.equal(
      glyphForTile("loras", "hycade.safetensors", "vehicle/muscle"),
      DOMAIN_GLYPHS.vehicle
    );
    assert.equal(glyphForTile("sections", "human/pose", ""), DOMAIN_GLYPHS.human);
    assert.equal(glyphForTile("templates", "noir/rain"), DOMAIN_GLYPHS.noir);
  });
});

describe("canRefreshPreview", () => {
  test("only when the server can find an AIR: air, or section+item", () => {
    assert.equal(canRefreshPreview({ air: "urn:air:sdxl:lora:civitai:1@2" }), true);
    assert.equal(canRefreshPreview({ section: "loralab/hy", item: "hycade" }), true);
    assert.equal(canRefreshPreview({ section: "loralab/hy" }), false);
    assert.equal(canRefreshPreview({ item: "hycade" }), false);
    assert.equal(canRefreshPreview({ file: "hy.safetensors" }), false); // a file is not an AIR
    assert.equal(canRefreshPreview({}), false);
  });
});

// ---------------------------------------------------------------------------
// the module's own hygiene (composer_modules.test.mjs covers the generic part)
// ---------------------------------------------------------------------------

describe("thumbs.js / tree.js house rules", () => {
  const sources = [
    ["thumbs.js", readFileSync(SRC, "utf8")],
    ["tree.js", readFileSync(TREE_SRC, "utf8")],
  ];

  test("createThumbs is a factory and nothing is built at import time", () => {
    assert.equal(typeof createThumbs, "function");
    assert.equal(createThumbs.length, 1); // (hub)
  });

  for (const [name, src] of sources) {
    test(`${name} never calls window.fetch — every JSON call is ctx.apiJson`, () => {
      assert.ok(!/\bwindow\.fetch\b/.test(src), `${name} calls window.fetch`);
      assert.ok(!/(^|[^.\w])fetch\s*\(/m.test(src), `${name} calls a bare fetch()`);
    });

    test(`${name} builds DOM with el(), never innerHTML`, () => {
      assert.ok(!/innerHTML/.test(src), `${name} uses innerHTML`);
      assert.ok(!/\.prototype\./.test(src), `${name} patches a prototype`);
    });

    test(`${name} imports nothing from ComfyUI`, () => {
      assert.ok(!/from\s+["'][^"']*\/scripts\//.test(src), `${name} imports a ComfyUI script`);
    });
  }

  test("window.confirm is never used — it throws on the Electron frontend", () => {
    const src = readFileSync(SRC, "utf8");
    // the CALL, not the word: the comment above armDestructive names it
    assert.ok(!/window\.(confirm|prompt|alert)\s*\(/.test(src));
    assert.ok(src.includes("armDestructive"), "resetting to factory must be armed");
    assert.ok(src.includes("busy("), "async mutating buttons must run through busy()");
  });

  test("the upload goes through image.js, not the raw file", () => {
    // a 4 MB PNG posted straight at /thumb is a 413 (routes.py MAX_BODY_BYTES)
    const src = readFileSync(SRC, "utf8");
    assert.ok(src.includes("downscaleToDataUrl"), "pixels must be downscaled first");
    assert.ok(src.includes("wireDropZone"), "drop/paste must use image.js");
  });
});

// ---------------------------------------------------------------------------
// the cross-language contract: these routes must exist on the server
// ---------------------------------------------------------------------------

describe("the routes this module posts to are registered", () => {
  const routes = readFileSync(ROUTES_PY, "utf8");

  test("GET /thumb, POST /thumb, POST /thumb-delete, POST /lora-preview", () => {
    for (const [method, path] of [
      ["get", THUMB_ROUTE],
      ["post", THUMB_ROUTE],
      ["post", THUMB_DELETE_ROUTE],
      ["post", LORA_PREVIEW_ROUTE],
    ]) {
      assert.ok(
        routes.includes(`("${method}", "${path}"`),
        `routes.py has no ${method.toUpperCase()} ${path} — the panel would 404`
      );
    }
  });

  test("thumb-delete is a POST (the route lint freezes method to get/post)", () => {
    assert.ok(!/\("delete",/.test(routes), "a DELETE route appeared — SPEC §6.1's wording");
  });
});
