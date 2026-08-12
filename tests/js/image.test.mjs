// web/js/composer/image.js — the upload strategies the server's 700 KiB
// payload cap forces on the browser.
//
// Only the pure half is exercised here: sniffing, the PNG chunk walk, the
// rebuilt metadata file, the per-format decision and the fit maths. The
// DOM half (canvas downscale, drop/paste wiring) needs a browser and is
// deliberately left to the panel smoke-drive.
//
// The rebuilt PNG has a SECOND test on the Python side —
// tests/test_prompt_image_intake.py parses the three hex constants out of the
// module and feeds the result to Pillow. That is the test that would catch a
// scaffold which is well-formed by these rules and still unreadable by the
// server; this file catches everything before that point.
//
// Run: node --test "tests/js/*.test.mjs"

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import zlib from "node:zlib";

import {
  JPEG_HEAD_BYTES,
  MAX_UPLOAD_BYTES,
  PNG_SIGNATURE_HEX,
  concatBytes,
  fitWithin,
  hexToBytes,
  imageFileFrom,
  metadataPayload,
  mimeFor,
  pngMetadataFile,
  pngTextChunks,
  sniffContainer,
  urlFrom,
} from "../../web/js/composer/image.js";

// ---- fixtures: real containers, built here (no binaries in the repo) --------

function crc32(bytes) {
  // only the fixtures need this — image.js deliberately ships no CRC
  // implementation, which is the whole point of its frozen hex scaffold
  let table = crc32.table;
  if (!table) {
    table = crc32.table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[i] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (const byte of bytes) crc = table[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function be32(value) {
  return new Uint8Array([value >>> 24, (value >>> 16) & 255, (value >>> 8) & 255, value & 255]);
}

function chunk(type, payload = new Uint8Array()) {
  const tag = new Uint8Array([...type].map((c) => c.charCodeAt(0)));
  const body = concatBytes([tag, payload]);
  return concatBytes([be32(payload.length), body, be32(crc32(body))]);
}

function textChunk(keyword, text) {
  const bytes = new Uint8Array([
    ...[...keyword].map((c) => c.charCodeAt(0)),
    0,
    ...[...text].map((c) => c.charCodeAt(0)),
  ]);
  return chunk("tEXt", bytes);
}

function png({ text = [], pixels = 4096 } = {}) {
  return concatBytes([
    hexToBytes(PNG_SIGNATURE_HEX),
    chunk("IHDR", new Uint8Array(13)),
    ...text,
    chunk("IDAT", new Uint8Array(pixels)),
    chunk("IEND"),
  ]);
}

function jpeg(size) {
  const bytes = new Uint8Array(size);
  bytes[0] = 0xff;
  bytes[1] = 0xd8;
  return bytes;
}

function webp(size) {
  const bytes = new Uint8Array(Math.max(size, 12));
  bytes.set([..."RIFF"].map((c) => c.charCodeAt(0)), 0);
  bytes.set([..."WEBP"].map((c) => c.charCodeAt(0)), 8);
  return bytes;
}

// ---- sniffing --------------------------------------------------------------

describe("sniffContainer", () => {
  test("reads the magic bytes, never a file name", () => {
    assert.equal(sniffContainer(png()), "png");
    assert.equal(sniffContainer(jpeg(64)), "jpeg");
    assert.equal(sniffContainer(webp(64)), "webp");
  });

  test("refuses anything else, including a truncated header", () => {
    assert.equal(sniffContainer(new Uint8Array([0x47, 0x49, 0x46])), "");
    assert.equal(sniffContainer(new Uint8Array()), "");
    assert.equal(sniffContainer(hexToBytes(PNG_SIGNATURE_HEX).subarray(0, 4)), "");
    // 'RIFF' alone is not WebP — a .wav would otherwise pass
    const wav = new Uint8Array(16);
    wav.set([..."RIFF"].map((c) => c.charCodeAt(0)), 0);
    wav.set([..."WAVE"].map((c) => c.charCodeAt(0)), 8);
    assert.equal(sniffContainer(wav), "");
  });

  test("mimeFor spells jpeg correctly", () => {
    // "image/jpg" is not a MIME type; a data: URI carrying it is a coin flip
    assert.equal(mimeFor("jpeg"), "image/jpeg");
    assert.equal(mimeFor("png"), "image/png");
    assert.equal(mimeFor("webp"), "image/webp");
  });
});

// ---- the chunk walk --------------------------------------------------------

describe("pngTextChunks", () => {
  test("collects every text dialect before IDAT, verbatim", () => {
    const a = textChunk("parameters", "a red car");
    const b = textChunk("prompt", '{"1": {}}');
    const chunks = pngTextChunks(png({ text: [a, b] }));
    assert.equal(chunks.length, 2);
    assert.deepEqual([...chunks[0]], [...a]);
    assert.deepEqual([...chunks[1]], [...b]);
  });

  test("stops at IDAT — a trailing text chunk is pixels away and not worth the bytes", () => {
    const before = textChunk("parameters", "kept");
    const after = textChunk("comment", "dropped");
    const bytes = concatBytes([
      hexToBytes(PNG_SIGNATURE_HEX),
      chunk("IHDR", new Uint8Array(13)),
      before,
      chunk("IDAT", new Uint8Array(32)),
      after,
      chunk("IEND"),
    ]);
    assert.equal(pngTextChunks(bytes).length, 1);
  });

  test("a chunk running past the buffer ends the walk instead of throwing", () => {
    // exactly what a head-limited read hands over: the buffer stops INSIDE a
    // chunk. Everything already collected is complete and stays; the cut one
    // is dropped rather than half-copied.
    const kept = textChunk("parameters", "kept");
    const cut = textChunk("workflow", "w".repeat(400));
    const full = png({ text: [kept, cut] });
    const half = full.subarray(0, 8 + 25 + kept.length + 12 + 200);
    assert.doesNotThrow(() => pngTextChunks(half));
    const chunks = pngTextChunks(half);
    assert.equal(chunks.length, 1);
    assert.deepEqual([...chunks[0]], [...kept]);
  });

  test("null for anything that is not a PNG", () => {
    assert.equal(pngTextChunks(jpeg(64)), null);
    assert.equal(pngTextChunks(new Uint8Array()), null);
  });

  test("reads through a byte offset — a subarray does not start at index 0", () => {
    // the DataView is built on (buffer, byteOffset, byteLength); getting that
    // wrong reads another file's bytes, and readForMetadata hands over slices
    const padded = concatBytes([new Uint8Array(7), png({ text: [textChunk("k", "v")] })]);
    assert.equal(pngTextChunks(padded.subarray(7)).length, 1);
  });
});

describe("pngMetadataFile", () => {
  test("is tiny, keeps the text, and drops every pixel", () => {
    const source = png({ text: [textChunk("parameters", "a red car")], pixels: 500_000 });
    const rebuilt = pngMetadataFile(source);
    assert.ok(rebuilt.length < 200, `expected a small file, got ${rebuilt.length}`);
    assert.equal(sniffContainer(rebuilt), "png");
    // the text chunk survived byte for byte
    assert.deepEqual(pngTextChunks(rebuilt), pngTextChunks(source));
  });

  test("a PNG with no metadata still rebuilds into a valid PNG", () => {
    // a screenshot: the answer is an empty extraction, not a parse error
    const rebuilt = pngMetadataFile(png());
    assert.equal(sniffContainer(rebuilt), "png");
    assert.deepEqual(pngTextChunks(rebuilt), []);
  });

  test("the frozen scaffold really is a 1x1 greyscale IHDR", () => {
    // the size is what keeps the rebuild ~150 bytes; a scaffold that grew to
    // carry real pixels would put the payload cap back in play
    const rebuilt = pngMetadataFile(png());
    const view = new DataView(rebuilt.buffer, rebuilt.byteOffset, rebuilt.byteLength);
    assert.equal(String.fromCharCode(...rebuilt.subarray(12, 16)), "IHDR");
    assert.equal(view.getUint32(16), 1); // width
    assert.equal(view.getUint32(20), 1); // height
    assert.equal(rebuilt[24], 8); // bit depth
    assert.equal(rebuilt[25], 0); // colour type 0 = greyscale
  });

  test("the scaffold's IDAT is real deflate data, not a zero-length stub", () => {
    // Pillow's PngImageFile.text is lazy and calls load(); an empty IDAT
    // raises "image file is truncated" there, which is the exact bug the
    // frozen scaffold exists to avoid
    const rebuilt = pngMetadataFile(png());
    const view = new DataView(rebuilt.buffer, rebuilt.byteOffset, rebuilt.byteLength);
    let pos = 8;
    let idat = null;
    while (pos + 8 <= rebuilt.length) {
      const length = view.getUint32(pos);
      const type = String.fromCharCode(...rebuilt.subarray(pos + 4, pos + 8));
      if (type === "IDAT") idat = rebuilt.subarray(pos + 8, pos + 8 + length);
      pos += 12 + length;
    }
    assert.ok(idat && idat.length > 0, "the scaffold has no IDAT payload");
    assert.deepEqual([...zlib.inflateSync(Buffer.from(idat))], [0, 0]);
  });
});

// ---- the per-format decision ----------------------------------------------

describe("metadataPayload", () => {
  test("PNG: sends the rebuild, however big the original was", () => {
    const source = png({ text: [textChunk("parameters", "x")], pixels: 3_000_000 });
    const out = metadataPayload(source);
    assert.equal(out.strategy, "png-text-chunks");
    assert.equal(out.original, source.length);
    assert.ok(out.bytes.length < MAX_UPLOAD_BYTES);
    assert.ok(!out.error);
  });

  test("PNG: refuses when the METADATA itself is over the cap", () => {
    // a huge embedded ComfyUI workflow is the real case
    const huge = textChunk("workflow", "w".repeat(MAX_UPLOAD_BYTES + 1000));
    const out = metadataPayload(png({ text: [huge] }));
    assert.ok(out.error && /over the/.test(out.error));
    assert.ok(out.remediation);
    assert.equal(out.bytes, undefined); // nothing is sent
  });

  test("JPEG: head slice, capped, and never longer than the file", () => {
    const big = metadataPayload(jpeg(4_000_000));
    assert.equal(big.strategy, "jpeg-head");
    assert.equal(big.bytes.length, JPEG_HEAD_BYTES);
    const small = metadataPayload(jpeg(900));
    assert.equal(small.bytes.length, 900);
  });

  test("WebP: whole file under the cap, a refusal over it", () => {
    // the EXIF chunk trails the pixel data, so a head slice carries nothing —
    // sending half a WebP would be a silent no-metadata result, not an error
    const ok = metadataPayload(webp(1000));
    assert.equal(ok.strategy, "whole");
    assert.equal(ok.bytes.length, 1000);
    const over = metadataPayload(webp(MAX_UPLOAD_BYTES + 1));
    assert.ok(over.error && /whole/.test(over.error));
    assert.ok(/civitai\.com URL/.test(over.remediation));
  });

  test("an unknown container is refused before anything is sent", () => {
    const out = metadataPayload(new Uint8Array([1, 2, 3, 4]));
    assert.ok(/not a PNG, JPEG or WebP/.test(out.error));
    assert.equal(out.bytes, undefined);
  });

  test("never throws — every failure is a returned {error, remediation}", () => {
    for (const input of [new Uint8Array(), jpeg(2), webp(12)]) {
      const out = metadataPayload(input);
      assert.ok(out.bytes || (out.error && out.remediation));
    }
  });
});

// ---- fit maths -------------------------------------------------------------

describe("fitWithin", () => {
  test("caps the longest side and keeps the aspect", () => {
    assert.deepEqual(fitWithin(2048, 1024, 768), { width: 768, height: 384 });
    assert.deepEqual(fitWithin(1024, 2048, 768), { width: 384, height: 768 });
  });

  test("never upscales", () => {
    assert.deepEqual(fitWithin(100, 50, 768), { width: 100, height: 50 });
  });

  test("degenerate sizes still produce a drawable canvas", () => {
    // a 0-height canvas throws in drawImage; a 1px one is merely useless
    assert.deepEqual(fitWithin(0, 0, 768), { width: 1, height: 1 });
    assert.deepEqual(fitWithin(4000, 1, 256), { width: 256, height: 1 });
  });
});

// ---- drop / paste extraction ----------------------------------------------

describe("imageFileFrom / urlFrom", () => {
  const drop = (transfer) => ({ dataTransfer: transfer });

  test("takes the first image file out of a drop", () => {
    const file = { type: "image/png", name: "a.png" };
    assert.equal(imageFileFrom(drop({ files: [file] })), file);
  });

  test("ignores non-image files rather than sending a .txt to the decoder", () => {
    assert.equal(imageFileFrom(drop({ files: [{ type: "text/plain" }] })), null);
  });

  test("falls back to items[] — a paste carries files there, not in files[]", () => {
    const file = { type: "image/jpeg" };
    const transfer = { items: [{ kind: "file", type: "image/jpeg", getAsFile: () => file }] };
    assert.equal(imageFileFrom({ clipboardData: transfer }), file);
  });

  test("an item that cannot produce a file is skipped, not returned as null-ish", () => {
    const transfer = { items: [{ kind: "file", type: "image/png", getAsFile: () => null }] };
    assert.equal(imageFileFrom(drop(transfer)), null);
  });

  test("urlFrom accepts only http(s), so a dropped file path is not fetched", () => {
    const withText = (text) => drop({ getData: () => text });
    assert.equal(urlFrom(withText("https://civitai.com/images/42")), "https://civitai.com/images/42");
    assert.equal(urlFrom(withText("  http://x/y  ")), "http://x/y");
    assert.equal(urlFrom(withText("file:///C:/secret.png")), "");
    assert.equal(urlFrom(withText("javascript:alert(1)")), "");
    assert.equal(urlFrom(withText("just some prompt text")), "");
  });

  test("no transfer at all is not a crash", () => {
    assert.equal(imageFileFrom({}), null);
    assert.equal(urlFrom({}), "");
  });
});
