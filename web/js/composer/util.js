// MRLN Prompt Composer — pure helpers.
//
// HARD RULE for this file: ZERO top-level side effects. ComfyUI auto-imports
// every .js file under WEB_DIRECTORY, so this module is loaded once on its
// own AND once as an import of the panel; the browser module cache makes that
// harmless only because evaluating it does nothing but declare functions.
// Therefore: no app/api imports, no DOM access, no listeners, no network, no
// module-level mutable state, no top-level statements other than declarations
// and exports. Every function here must be a pure function of its arguments
// (structuredClone is used where a caller expects a detached copy).
//
// The state-shaped helpers take a plain object with the fields they read —
// the panel passes its own state, tests pass a literal:
//   {rawData, baseRaw, rows, variant, muted, soloed, orderIds, profile, modified}

// ---- token / kv parsing ----------------------------------------------------

export function parseToken(token) {
  const match = /^(?:🎲 )?random(?:@(\d+))?$/.exec((token ?? "").trim());
  if (match) return { random: true, seed: match[1] ?? "", item: "" };
  return { random: false, seed: "", item: (token ?? "").trim() };
}

export function parseKvLines(text) {
  const map = {};
  for (const raw of (text ?? "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    map[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return map;
}

// ---- template raw-data shape ----------------------------------------------

export function allSlots(raw) {
  if (!raw) return [];
  return [...(raw.slots ?? []), ...(raw.variants ?? []).flatMap((v) => v.slots ?? [])];
}

export function activeSlots(raw, variantName) {
  if (!raw) return [];
  const active = [...(raw.slots ?? [])];
  if (variantName && variantName !== "random") {
    const variant = (raw.variants ?? []).find((v) => v.name === variantName);
    if (variant) active.push(...(variant.slots ?? []));
  }
  return active;
}

export function variantSlotIds(raw) {
  return new Set((raw.variants ?? []).flatMap((v) => (v.slots ?? []).map((s) => s.id)));
}

export function syncOrderIds(raw) {
  const shared = (raw.slots ?? []).map((s) => s.id);
  const hasVariants = (raw.variants ?? []).length > 0;
  let order = Array.isArray(raw.order) ? [...raw.order] : null;
  if (!order) return hasVariants ? [...shared, "@variant"] : shared;
  order = order.filter((id) => id === "@variant" || shared.includes(id));
  for (const id of shared) if (!order.includes(id)) order.push(id);
  if (hasVariants && !order.includes("@variant")) order.push("@variant");
  if (!hasVariants) order = order.filter((id) => id !== "@variant");
  return order;
}

// ---- mute / solo audition (preview-only, DAW-style) ------------------------
// Seeding is per-slot, so the surviving sections draw exactly the same
// items — muting isolates the pure textual impact of a section.

export function auditionActive(muted, soloed) {
  return muted.size > 0 || soloed.size > 0;
}

export function slotAudible(muted, soloed, id, isVariantSlot) {
  if (soloed.size) {
    return soloed.has(id) || (isVariantSlot && soloed.has("@variant"));
  }
  if (muted.has(id)) return false;
  if (isVariantSlot && muted.has("@variant")) return false;
  return true;
}

export function variantBlockAudible(raw, muted, soloed) {
  if (!(raw.variants ?? []).length) return false;
  if (soloed.size) {
    if (soloed.has("@variant")) return true;
    const vids = variantSlotIds(raw);
    return [...soloed].some((id) => vids.has(id));
  }
  return !muted.has("@variant");
}

// ---- selection lines (the node persistence format) -------------------------

export function rowToken(rows, slot) {
  const row = rows.get(slot.id) ?? parseToken(slot.default ?? "random");
  if (row.random) return row.seed ? `random@${row.seed}` : "random";
  return row.item || "random";
}

export function buildSelectionLines(state) {
  // Mute/solo serializes as 'off' lines — the SAME selection goes to the
  // preview and to the node, so what you audition is what the node runs.
  const audition = auditionActive(state.muted, state.soloed);
  const vids = variantSlotIds(state.rawData);
  const lines = [];
  const variants = state.rawData.variants ?? [];
  const blockOff =
    audition && variants.length > 0 && !variantBlockAudible(state.rawData, state.muted, state.soloed);
  if (variants.length) {
    const fileDefault = state.rawData.variant_default ?? "";
    if (blockOff) {
      if (fileDefault !== "off") lines.push("variant=off"); // baked default needs no line
    } else {
      const fallback = fileDefault && fileDefault !== "off" ? fileDefault : variants[0].name;
      if (state.variant !== fallback || fileDefault === "off") {
        lines.push(`variant=${state.variant}`); // an explicit pick un-mutes an off default
      }
    }
  }
  for (const slot of activeSlots(state.rawData, state.variant)) {
    const isVar = vids.has(slot.id);
    if (isVar && blockOff) continue; // variant=off already silences the block
    if (audition && !slotAudible(state.muted, state.soloed, slot.id, isVar)) {
      if ((slot.default ?? "random") !== "off") lines.push(`${slot.id}=off`);
      continue; // a baked off-default reproduces without a line
    }
    const token = rowToken(state.rows, slot);
    if (token !== (slot.default ?? "random")) lines.push(`${slot.id}=${token}`);
  }
  for (const [key, row] of state.rows) {
    if (!key.includes(".") || !row.touched) continue; // nested rows, user-set only
    const token = row.random ? (row.seed ? `random@${row.seed}` : "random") : row.item || "random";
    lines.push(`${key}=${token}`);
  }
  return lines.join("\n");
}

// ---- draft / save payloads -------------------------------------------------

export function buildDraftData(state) {
  // Structure as edited, defaults untouched — current picks travel as
  // selection lines, exactly like the node executes them.
  const draft = structuredClone(state.rawData);
  if (state.orderIds.length) draft.order = [...state.orderIds];
  // Under a profile the working copy ALREADY embodies its overrides —
  // strip them so the preview doesn't apply the stored diff twice.
  const profile = state.profile ?? "standard";
  if (profile !== "standard" && draft.profiles?.[profile]?.overrides) {
    delete draft.profiles[profile].overrides;
  }
  return draft;
}

export function buildSaveData(state) {
  const draft = structuredClone(state.rawData);
  const audition = auditionActive(state.muted, state.soloed);
  const hasVariants = (draft.variants ?? []).length > 0;
  const blockOff =
    audition && hasVariants && !variantBlockAudible(state.rawData, state.muted, state.soloed);
  const bake = (slots, isVariant) => {
    for (const slot of slots ?? []) {
      if (!state.rows.has(slot.id)) continue;
      // Apply persists what you SEE: a muted slot bakes as default "off"
      // (a muted variant BLOCK rides variant_default instead)
      if (
        audition &&
        !slotAudible(state.muted, state.soloed, slot.id, isVariant) &&
        !(isVariant && blockOff)
      ) {
        slot.default = "off";
      } else {
        const token = rowToken(state.rows, slot);
        if (token === "random") delete slot.default;
        else slot.default = token;
      }
      if (!slot.label) delete slot.label;
    }
  };
  bake(draft.slots, false);
  for (const variant of draft.variants ?? []) bake(variant.slots, true);
  if (hasVariants && (blockOff || state.variant)) {
    draft.variant_default = blockOff ? "off" : state.variant;
  }
  const sharedIds = (draft.slots ?? []).map((s) => s.id);
  const synthesized = (draft.variants ?? []).length ? [...sharedIds, "@variant"] : sharedIds;
  if (JSON.stringify(state.orderIds) === JSON.stringify(synthesized)) delete draft.order;
  else draft.order = [...state.orderIds];
  for (const key of ["prefix", "suffix", "negative", "description"]) {
    if (!draft[key]) delete draft[key];
  }
  draft.version = 1;
  return draft;
}

export function appliedStateDiffers(state) {
  // Apply's contract: the FILE carries what you see. Anything the bake
  // would change (structure, picks, mutes, variant) forces a save, so the
  // node reproduces the applied state from the library even when the
  // workflow's widget values are lost (autosave off, stale workflow file,
  // frontend serialization hiccups).
  if (state.modified) return true;
  const audition = auditionActive(state.muted, state.soloed);
  const variants = state.rawData.variants ?? [];
  const vids = variantSlotIds(state.rawData);
  const blockOff =
    audition && variants.length > 0 && !variantBlockAudible(state.rawData, state.muted, state.soloed);
  if (variants.length) {
    const fileVariant = state.rawData.variant_default || variants[0].name;
    const nowVariant = blockOff ? "off" : state.variant;
    if (nowVariant !== fileVariant) return true;
  }
  for (const slot of allSlots(state.rawData)) {
    if (!state.rows.has(slot.id)) continue;
    const isVar = vids.has(slot.id);
    const nowToken =
      audition && !slotAudible(state.muted, state.soloed, slot.id, isVar) && !(isVar && blockOff)
        ? "off"
        : rowToken(state.rows, slot);
    if (nowToken !== (slot.default ?? "random")) return true;
  }
  return false;
}

// ---- per-profile template variants -----------------------------------------
// A template can carry profiles.<name>.overrides: a sparse diff vs the
// standard render (prefix/suffix/negative/variant_default + slot default/
// emphasis). Editing with a Target profile selected edits THAT variant;
// Save stores only the diff, the base file stays untouched — 'standard'
// is always the way back. Mirrors the trainer's family/definitions split.

export function overridesFor(profileName, raw) {
  if (!profileName || profileName === "standard") return null;
  return raw?.profiles?.[profileName]?.overrides ?? null;
}

export function overrideTweakCount(ov) {
  if (!ov) return 0;
  const scalars = ["prefix", "suffix", "negative", "variant_default"];
  return Object.keys(ov.slots ?? {}).length + scalars.filter((key) => key in ov).length;
}

export function effectiveRaw(profileName, base) {
  const data = structuredClone(base);
  const ov = overridesFor(profileName, base);
  if (!ov) return data;
  for (const key of ["prefix", "suffix", "negative", "variant_default"]) {
    if (ov[key] !== undefined) data[key] = ov[key];
  }
  const bySlot = ov.slots ?? {};
  const fix = (slot) => {
    const so = bySlot[slot.id];
    if (!so) return;
    if (so.default !== undefined) {
      if (so.default === "random") delete slot.default;
      else slot.default = so.default;
    }
    if (so.emphasis !== undefined) {
      if (so.emphasis === null) delete slot.emphasis;
      else slot.emphasis = so.emphasis;
    }
  };
  (data.slots ?? []).forEach(fix);
  (data.variants ?? []).forEach((v) => (v.slots ?? []).forEach(fix));
  return data;
}

export function diffProfileOverrides(effective, base) {
  const ov = {};
  for (const key of ["prefix", "suffix", "negative", "variant_default"]) {
    if ((effective[key] ?? "") !== (base[key] ?? "")) ov[key] = effective[key] ?? "";
  }
  const slotMap = (raw) => {
    const map = new Map();
    for (const s of raw.slots ?? []) map.set(s.id, s);
    for (const v of raw.variants ?? []) for (const s of v.slots ?? []) map.set(s.id, s);
    return map;
  };
  const baseSlots = slotMap(base);
  const slots = {};
  for (const [id, slot] of slotMap(effective)) {
    const baseSlot = baseSlots.get(id);
    if (!baseSlot) continue; // structural adds belong to the base template
    const so = {};
    if ((slot.default ?? "random") !== (baseSlot.default ?? "random")) {
      so.default = slot.default ?? "random";
    }
    if ((slot.emphasis ?? null) !== (baseSlot.emphasis ?? null)) {
      so.emphasis = slot.emphasis ?? null;
    }
    if (Object.keys(so).length) slots[id] = so;
  }
  if (Object.keys(slots).length) ov.slots = slots;
  return Object.keys(ov).length ? ov : null;
}

export function structuralDrift(effective, base) {
  // Everything the sparse profile diff CANNOT carry: slot adds/removes,
  // refs, lead-in labels, variant structure, order, template label/type.
  // default/emphasis and the prose scalars stay out — those diff fine.
  const sig = (raw) => {
    const slotSig = (s) => [s.id, s.ref, s.label ?? ""];
    const sharedIds = (raw.slots ?? []).map((s) => s.id);
    const synthesized = (raw.variants ?? []).length ? [...sharedIds, "@variant"] : sharedIds;
    return JSON.stringify({
      label: raw.label ?? "",
      type: raw.type ?? [],
      slots: (raw.slots ?? []).map(slotSig),
      variants: (raw.variants ?? []).map((v) => [v.name, (v.slots ?? []).map(slotSig)]),
      order: Array.isArray(raw.order) ? raw.order : synthesized,
    });
  };
  return sig(effective) !== sig(base);
}

// ---- missing-LoRA banner ---------------------------------------------------

export const loraKey = (file) => String(file ?? "").replaceAll("\\", "/").toLowerCase();

export function missingLoraRows(status) {
  // One row per FILE — the same .safetensors can back several items, and a
  // single download repairs all of them.
  const byFile = new Map();
  for (const entry of status?.loras ?? []) {
    if (entry.present) continue;
    const seen = byFile.get(loraKey(entry.file));
    if (seen) seen.uses.push(entry);
    else byFile.set(loraKey(entry.file), { ...entry, uses: [entry] });
  }
  return [...byFile.values()];
}

export function downloadableAir(row) {
  const air = String(row.air ?? "").trim();
  return air.toLowerCase().startsWith("urn:air:") ? air : "";
}

export function loraProgressText(body) {
  const mb = (n) => (Number(n ?? 0) / (1 << 20)).toFixed(0);
  return body.total ? `${mb(body.loaded)} / ${mb(body.total)} MB` : `${mb(body.loaded)} MB`;
}

// ---- de-compose ------------------------------------------------------------

export function jsSlugify(text, maxLen = 40) {
  const slug = (text ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, maxLen)
    .replace(/-+$/g, "");
  return slug || "item";
}

export function defaultPlan(fragment, index, fragments) {
  if (fragment.match) return { action: "slot", include: true };
  const firstMatch = fragments.findIndex((f) => f.match);
  const lastMatch = fragments.length - 1 - [...fragments].reverse().findIndex((f) => f.match);
  if ((fragment.suggestion?.score ?? 0) >= 0.3) {
    return { action: "new-item", section: fragment.suggestion.section, include: true };
  }
  if (firstMatch === -1 || index < firstMatch) return { action: "prefix", include: true };
  if (index > lastMatch) return { action: "suffix", include: true };
  return { action: "skip", include: false };
}

// ---- combine sections ------------------------------------------------------
// A "combine" is an ordinary section whose every item just delegates to
// ANOTHER section through a child slot.

export function combineItem(slug, weight) {
  const item = {
    name: jsSlugify(slug.split("/").slice(-2).join("-")),
    text: "{pick}",
    slots: [{ id: "pick", ref: slug }],
  };
  if (weight && weight !== 1) item.weight = weight;
  return item;
}

export function isCombineItem(item) {
  return (
    (item?.text ?? "").trim() === "{pick}" &&
    (item?.slots ?? []).length === 1 &&
    item.slots[0].id === "pick"
  );
}

// ---- misc ------------------------------------------------------------------

export function moveInArray(arr, from, to) {
  const [moved] = arr.splice(from, 1);
  arr.splice(to, 0, moved);
}

export function bundleFilename(slug) {
  return `${String(slug || "bundle").replace(/\//g, "--")}.mrln.json`;
}
