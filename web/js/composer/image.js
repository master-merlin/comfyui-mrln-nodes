// MRLN Prompt Composer — image bytes: the two upload strategies the server's
// 700 KiB payload cap forces on the browser, plus the drop/paste plumbing the
// two consumers share.
//
// TWO CONSUMERS, TWO DIFFERENT NEEDS — confusing them is the bug this module
// exists to prevent:
//   composer/intake.js   wants the METADATA and nothing else, so it sends a
//                        REBUILT file carrying the text chunks and no pixels.
//   composer/thumbs.js   wants the PIXELS, so it sends a canvas-downscaled
//                        copy, which the server re-encodes to 256 px webp.
// A thumbnail sent through the metadata path is a 1×1 grey square; a metadata
// read done through the thumbnail path loses every text chunk (canvas
// re-encoding drops them). Neither is recoverable downstream, hence one module.
//
// WHY A REBUILT PNG AND NOT A HEAD SLICE. The obvious trick — "text chunks all
// precede IDAT, so send the head" — produces a file Pillow refuses:
// PngImageFile.text is lazy and calls load(), which raises "image file is
// truncated" on a headless PNG. Verified against Pillow 12. So the head is
// walked for its tEXt/iTXt/zTXt chunks and those are re-wrapped around a frozen
// 1×1 greyscale scaffold: a real, complete, ~150-byte PNG that opens cleanly
// and carries every dialect the server knows. This is still chunk-WALKING, not
// metadata parsing — the chunk payloads are copied verbatim and the server
// remains the only place that understands what is inside them.
//
// JPEG is different and needs no rebuild: EXIF sits in APP1 right behind SOI
// and Image.open never decodes pixels for it, so a head slice parses (verified
// at 4 KB). WebP is different again: its EXIF chunk trails the image data in
// the RIFF container, so head-slicing yields nothing and the file can only go
// whole or not at all.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). Everything below is a
// declaration; document/window/FileReader are touched only INSIDE functions.

// ---- caps ------------------------------------------------------------------

/**
 * Largest payload the server decodes (promptapi/intake.py MAX_IMAGE_BYTES).
 * Derived from the 1 MiB route body cap and base64's 4/3 inflation — raising
 * it here without raising it there only produces 413s.
 */
export const MAX_UPLOAD_BYTES = 700 * 1024;

/** EXIF lives in APP1 immediately after SOI; this is orders of magnitude. */
export const JPEG_HEAD_BYTES = 128 * 1024;

/** Longest side a thumbnail upload is downscaled to before sending. */
export const THUMB_UPLOAD_SIDE = 768;

// ---- the frozen PNG scaffold -----------------------------------------------
// Signature, IHDR (1×1, 8-bit greyscale) and IDAT+IEND for a single black
// pixel. Constant by construction, so they are stored as hex rather than
// computed — which also means no CRC32 implementation has to live in the
// browser. tests/test_prompt_image_intake.py parses these three literals OUT OF
// THIS FILE and feeds them to Pillow, so the scaffold cannot rot silently.

export const PNG_SIGNATURE_HEX = "89504e470d0a1a0a";
export const PNG_STUB_IHDR_HEX = "0000000d49484452000000010000000108000000003a7e9b55";
export const PNG_STUB_TAIL_HEX = "0000000a49444154789c636000000002000148afa4710000000049454e44ae426082";

/** PNG chunk types worth carrying: every text dialect, nothing else. */
export const PNG_TEXT_CHUNKS = ["tEXt", "iTXt", "zTXt"];

// ---- bytes -----------------------------------------------------------------

export function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export function concatBytes(parts) {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/**
 * base64 in 8 KB slices. `btoa(String.fromCharCode(...bytes))` blows the
 * argument limit somewhere around 100 KB and throws RangeError — on the exact
 * inputs this module is for.
 */
export function bytesToBase64(bytes) {
  const chunk = 8192;
  const parts = [];
  for (let i = 0; i < bytes.length; i += chunk) {
    parts.push(String.fromCharCode(...bytes.subarray(i, i + chunk)));
  }
  return btoa(parts.join(""));
}

export function dataUrl(bytes, mime) {
  return `data:${mime};base64,${bytesToBase64(bytes)}`;
}

// ---- container sniffing ----------------------------------------------------

function startsWith(bytes, signature) {
  if (bytes.length < signature.length) return false;
  for (let i = 0; i < signature.length; i++) if (bytes[i] !== signature[i]) return false;
  return true;
}

/** "png" | "jpeg" | "webp" | "" — from the magic bytes, never the filename. */
export function sniffContainer(bytes) {
  if (startsWith(bytes, hexToBytes(PNG_SIGNATURE_HEX))) return "png";
  if (bytes.length > 2 && bytes[0] === 0xff && bytes[1] === 0xd8) return "jpeg";
  if (
    bytes.length > 12 &&
    String.fromCharCode(...bytes.subarray(0, 4)) === "RIFF" &&
    String.fromCharCode(...bytes.subarray(8, 12)) === "WEBP"
  ) {
    return "webp";
  }
  return "";
}

export function mimeFor(container) {
  return container === "jpeg" ? "image/jpeg" : `image/${container || "png"}`;
}

// ---- PNG chunk walk --------------------------------------------------------

/**
 * Every text chunk before the first IDAT, as raw slices (length + type +
 * payload + CRC, exactly as they sit on disk). Returns [] for a PNG with no
 * metadata and null when the bytes are not a walkable PNG.
 */
export function pngTextChunks(bytes) {
  if (sniffContainer(bytes) !== "png") return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const kept = [];
  let pos = 8;
  while (pos + 8 <= bytes.length) {
    const length = view.getUint32(pos);
    const type = String.fromCharCode(...bytes.subarray(pos + 4, pos + 8));
    const end = pos + 12 + length;
    if (type === "IDAT" || type === "IEND") return kept;
    // A chunk running past the buffer means the slice we were handed ends
    // mid-chunk: everything already collected is still valid and complete.
    if (end > bytes.length) return kept;
    if (PNG_TEXT_CHUNKS.includes(type)) kept.push(bytes.subarray(pos, end));
    pos = end;
  }
  return kept;
}

/**
 * A complete, minimal PNG carrying only the source's text chunks. null when
 * the input is not a PNG.
 */
export function pngMetadataFile(bytes) {
  const chunks = pngTextChunks(bytes);
  if (chunks === null) return null;
  return concatBytes([
    hexToBytes(PNG_SIGNATURE_HEX),
    hexToBytes(PNG_STUB_IHDR_HEX),
    ...chunks,
    hexToBytes(PNG_STUB_TAIL_HEX),
  ]);
}

// ---- the metadata upload ---------------------------------------------------

/**
 * What to POST to /mrln/prompt/extract-image for these bytes.
 *
 * @returns {{bytes: Uint8Array, container: string, strategy: string,
 *            original: number}} on success, or {error, remediation} when the
 *          file cannot be sent at all. Never throws: callers surface `error`.
 */
export function metadataPayload(bytes, { maxBytes = MAX_UPLOAD_BYTES } = {}) {
  const container = sniffContainer(bytes);
  if (!container) {
    return {
      error: "that file is not a PNG, JPEG or WebP",
      remediation:
        "drop the image a generator wrote — a screenshot or a re-saved copy has "
        + "no generation metadata left in it",
    };
  }
  if (container === "png") {
    const rebuilt = pngMetadataFile(bytes);
    if (rebuilt.length <= maxBytes) {
      return { bytes: rebuilt, container, strategy: "png-text-chunks", original: bytes.length };
    }
    return {
      error: `this PNG's metadata alone is ${Math.round(rebuilt.length / 1024)} KB, over the `
        + `${Math.round(maxBytes / 1024)} KB the server accepts`,
      remediation:
        "an embedded ComfyUI workflow that large has to come from the API instead — "
        + "paste the prompt text into the box below",
    };
  }
  if (container === "jpeg") {
    const head = bytes.subarray(0, Math.min(bytes.length, JPEG_HEAD_BYTES, maxBytes));
    return { bytes: head, container, strategy: "jpeg-head", original: bytes.length };
  }
  // WebP: the EXIF chunk trails the pixel data, so a head slice carries
  // nothing. Whole file or nothing.
  if (bytes.length <= maxBytes) {
    return { bytes, container, strategy: "whole", original: bytes.length };
  }
  return {
    error: `this WebP is ${Math.round(bytes.length / 1024)} KB and WebP stores its metadata `
      + "AFTER the image data, so it can only be sent whole",
    remediation:
      "paste the image's civitai.com URL instead, or drop the PNG/JPEG the generator wrote",
  };
}

// ---- reading files ---------------------------------------------------------

/** The first `limit` bytes of a File/Blob, as a Uint8Array. */
export async function readBytes(file, limit = MAX_UPLOAD_BYTES) {
  const slice = file.size > limit ? file.slice(0, limit) : file;
  if (typeof slice.arrayBuffer === "function") return new Uint8Array(await slice.arrayBuffer());
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsArrayBuffer(slice);
  });
}

/**
 * A PNG needs its whole head walked, a JPEG only its first 128 KB, a WebP all
 * of it. Which one it is is only knowable after reading the magic bytes, so
 * read the JPEG budget first and go back for more only when it turns out to be
 * a PNG or WebP (the common drop — a 2 MB PNG — costs one extra read of a file
 * the browser already has in its page cache).
 */
export async function readForMetadata(file) {
  const head = await readBytes(file, JPEG_HEAD_BYTES);
  const container = sniffContainer(head);
  if (container === "jpeg" || file.size <= head.length) return head;
  // A PNG's text chunks can outrun 128 KB (embedded ComfyUI workflows do);
  // metadataPayload enforces the real cap on what comes back.
  return await readBytes(file, Math.max(MAX_UPLOAD_BYTES, JPEG_HEAD_BYTES) + 1);
}

// ---- pixels: the thumbnail upload ------------------------------------------

function drawToCanvas(source, width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.drawImage(source, 0, 0, width, height);
  return canvas;
}

function decodeImage(file) {
  // createImageBitmap is the fast path but is not universal (and refuses some
  // animated formats); the <img> + object URL path works everywhere.
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("the browser could not decode that image"));
    };
    img.src = url;
  });
}

/** The longest-side-capped size for `w`×`h`, never upscaling. */
export function fitWithin(width, height, maxSide) {
  const longest = Math.max(width, height);
  if (!longest || longest <= maxSide) return { width: width || 1, height: height || 1 };
  const scale = maxSide / longest;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

/**
 * A data URI of `file` downscaled to `maxSide`, small enough to POST. The
 * server re-encodes to 256 px webp regardless, so this only has to be small and
 * faithful — not final. Throws with a user-facing message.
 */
export async function downscaleToDataUrl(file, { maxSide = THUMB_UPLOAD_SIDE } = {}) {
  const img = await decodeImage(file);
  const size = fitWithin(img.naturalWidth || img.width, img.naturalHeight || img.height, maxSide);
  const canvas = drawToCanvas(img, size.width, size.height);
  // webp first (roughly half the bytes of jpeg at this size); a canvas that
  // does not encode webp silently answers a PNG data URI, which is why the
  // prefix is checked rather than trusted.
  const webp = canvas.toDataURL("image/webp", 0.9);
  if (webp.startsWith("data:image/webp")) return webp;
  return canvas.toDataURL("image/jpeg", 0.9);
}

// ---- drop / paste ----------------------------------------------------------

export function imageFileFrom(event) {
  const transfer = event.dataTransfer ?? event.clipboardData;
  if (!transfer) return null;
  for (const file of transfer.files ?? []) {
    if (String(file.type || "").startsWith("image/")) return file;
  }
  for (const item of transfer.items ?? []) {
    if (item.kind === "file" && String(item.type || "").startsWith("image/")) {
      const file = item.getAsFile();
      if (file) return file;
    }
  }
  return null;
}

export function urlFrom(event) {
  const transfer = event.dataTransfer ?? event.clipboardData;
  const text = transfer?.getData?.("text/uri-list") || transfer?.getData?.("text/plain") || "";
  return /^https?:\/\//i.test(text.trim()) ? text.trim() : "";
}

/**
 * Wire a node as a drop target: `onImage(file)` for dropped/pasted image
 * files, `onUrl(url)` for dropped links. The node gains `mrln-drop-hot` while
 * something hovers it. Paste is listened for on the node, so it fires when the
 * zone (or anything inside it) has focus — not globally, which would hijack
 * pasting a prompt into the textarea next to it.
 */
export function wireDropZone(node, { onImage, onUrl } = {}) {
  const hot = (on) => node.classList.toggle("mrln-drop-hot", on);
  node.addEventListener("dragover", (event) => {
    event.preventDefault();
    hot(true);
  });
  node.addEventListener("dragleave", () => hot(false));
  node.addEventListener("drop", (event) => {
    event.preventDefault();
    hot(false);
    const file = imageFileFrom(event);
    if (file) {
      onImage?.(file);
      return;
    }
    const url = urlFrom(event);
    if (url) onUrl?.(url);
  });
  node.addEventListener("paste", (event) => {
    const file = imageFileFrom(event);
    if (file) {
      event.preventDefault();
      onImage?.(file);
      return;
    }
    const url = urlFrom(event);
    if (url && onUrl) {
      event.preventDefault();
      onUrl(url);
    }
  });
  return node;
}
