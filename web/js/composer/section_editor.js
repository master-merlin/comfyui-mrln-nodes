// MRLN Prompt Composer — the section editor: the item table (child slots via
// '{', LoRA blocks with their trigger/AIR lookup and missing-file healer), the
// factory extend/replace save modes, and the combine builder that generates a
// delegating section and hands it to the same form.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js).
import {
  cleanTriggerWords,
  combineItem,
  dropTriggerWord,
  isCombineItem,
  itemRowEdited,
  loraProgressText,
  muteTriggerWord,
  soloTriggerWord,
  triggerSelection,
  triggerSoloed,
  uniqueName,
} from "./util.js";
import { armDestructive, braceAssist, busy, el, field, mount, smallBtn, tierChip, validateRefs } from "./dom.js";

export function createSectionEditor(hub) {
  const { ctx, state } = hub;
  // late-bound cross-module calls (see composer/state.js for the why)
  const applyItemRenames = (...a) => hub.applyItemRenames(...a);
  const askString = (...a) => hub.askString(...a);
  const deleteEntry = (...a) => hub.deleteEntry(...a);
  const editorCloseBtn = (...a) => hub.editorCloseBtn(...a);
  const installedLoras = (...a) => hub.installedLoras(...a);
  const invalidateInstalledLoras = (...a) => hub.invalidateInstalledLoras(...a);
  const loadLibrary = (...a) => hub.loadLibrary(...a);
  const loraPicker = (...a) => hub.loraPicker(...a);
  const pollLoraDownload = (...a) => hub.pollLoraDownload(...a);
  const refreshDetail = (...a) => hub.refreshDetail(...a);
  const refreshLoraBanner = (...a) => hub.refreshLoraBanner(...a);
  const setEditor = (...a) => hub.setEditor(...a);
  const thumbControls = (...a) => hub.thumbControls(...a);

  // ---- combine sections ----------------------------------------------------
  // A "combine" is an ordinary section whose every item just delegates to
  // ANOTHER section through a child slot. Drawing it picks which source to
  // draw from (weights included), which is how you group several sections
  // into one pool. The engine has always supported this; only building one
  // by hand was tedious, so this generates the structure and hands it to the
  // normal section editor — no new schema, no new save path.

  function newCombineSection(existing = null) {
    // existing: {slug, body} when re-opening a section that IS a combine
    const chosen = new Map(); // section slug -> weight
    if (existing) {
      for (const item of existing.body.items ?? []) {
        if (isCombineItem(item)) chosen.set(item.slots[0].ref, Number(item.weight) || 1);
      }
    }
    const labelInput = el("input", {
      type: "text",
      value: existing?.body.label ?? "",
      placeholder: "e.g. Anywhere — urban, nature or studio",
    });
    const filterInput = el("input", {
      type: "text",
      placeholder: "Filter sections…",
      oninput: () => renderPicks(),
    });
    const pickList = el("div", { class: "mrln-combine-picks" });
    const chosenList = el("div", { class: "mrln-combine-chosen" });
    const errorLine = el("div", { class: "mrln-error" });

    function renderChosen() {
      const rows = [...chosen.entries()].map(([slug, weight]) =>
        el(
          "div",
          { class: "mrln-inline" },
          el("span", {}, slug),
          el("input", {
            type: "number",
            class: "mrln-narrow",
            min: "0.1",
            step: "0.1",
            value: String(weight),
            title: "Draw weight — 2 means twice as likely as a 1",
            oninput: (e) => chosen.set(slug, Math.max(0.1, Number(e.target.value) || 1)),
          }),
          smallBtn("Remove from the combine", "✕", () => {
            chosen.delete(slug);
            renderChosen();
            renderPicks();
          })
        )
      );
      mount(chosenList, 
        el("div", { class: "mrln-field-name" }, `Combining ${chosen.size} section(s)`),
        ...(rows.length ? rows : [el("div", { class: "mrln-note" }, "nothing picked yet")])
      );
    }

    function renderPicks() {
      const filter = filterInput.value.trim().toLowerCase();
      const rows = (state.library?.sections ?? [])
        .filter((s) => !chosen.has(s.slug))
        .filter((s) => !filter || s.slug.toLowerCase().includes(filter))
        .slice(0, 40)
        .map((s) =>
          el(
            "div",
            {
              class: "mrln-combine-pick",
              onclick: () => {
                chosen.set(s.slug, 1);
                renderChosen();
                renderPicks();
              },
            },
            `+ ${s.slug}`,
            el("span", { class: "mrln-slug" }, ` ${s.item_count ?? "?"} items`)
          )
        );
      mount(pickList, 
        ...(rows.length ? rows : [el("div", { class: "mrln-note" }, "no matches")])
      );
    }

    function build() {
      if (!chosen.size) {
        errorLine.textContent = "pick at least one section to combine";
        return;
      }
      const items = [...chosen.entries()].map(([slug, weight]) => combineItem(slug, weight));
      const names = new Set();
      for (const item of items) {
        item.name = uniqueName(item.name, names);
        names.add(item.name);
      }
      // hand off to the ordinary editor: from here it is just a section
      openSectionForm(existing?.slug ?? null, {
        label: labelInput.value.trim(),
        description: existing?.body.description ?? "",
        negative: existing?.body.negative ?? "",
        items,
        raw: existing?.body.raw ?? { items: [] },
        factory_raw: existing?.body.factory_raw ?? null,
        merged: false,
        replaces: Boolean(existing?.body.replaces),
        tier: existing?.body.tier ?? "",
      });
    }

    renderChosen();
    renderPicks();
    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        existing ? `Combine: ${existing.slug}` : "New combine section",
        editorCloseBtn()
      ),
      el(
        "div",
        { class: "mrln-note" },
        "Each picked section becomes one entry; drawing this section picks an "
          + "entry (by weight) and then draws from that section. Build it, then "
          + "the normal section editor opens so you can save it."
      ),
      field("Label", labelInput),
      chosenList,
      field("Add a section", filterInput),
      pickList,
      errorLine,
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn mrln-primary", onclick: build }, "Build →")
      )
    );
  }

  function newSection() {
    openSectionForm(null, {
      label: "",
      description: "",
      negative: "",
      items: [{ name: "", text: "" }],
      raw: { items: [] },
      factory_raw: null,
      merged: false,
      replaces: false,
      tier: "",
    });
  }

  async function openSectionEditor(slug) {
    let body;
    try {
      body = await ctx.apiJson(`/mrln/prompt/section?slug=${encodeURIComponent(slug)}`);
    } catch (err) {
      setEditor(el("div", { class: "mrln-error" }, err.message));
      return;
    }
    const items = body.items ?? [];
    if (items.length && items.every(isCombineItem)) {
      // a section that is nothing but delegations edits far better as the
      // pick-and-weight list it came from than as a table of "{pick}" rows
      newCombineSection({ slug, body });
      return;
    }
    openSectionForm(slug, body);
  }

  function openSectionForm(slug, body) {
    // Factory sections COMPOUND: the default save writes only your changes
    // (edited/new items, tombstones for hidden ones) as a thin extend file
    // that survives factory updates. 'Replace' opts into a full frozen copy.
    const factoryBaseline = body.factory_raw ?? (body.tier === "factory" ? body.raw : null);
    const hasFactory = Boolean(factoryBaseline);
    let saveMode = hasFactory ? (body.replaces ? "replace" : "extend") : "standalone";

    const slugInput = el("input", { type: "text", value: slug ?? "", placeholder: "folder/name" });
    const labelInput = el("input", { type: "text", value: body.label ?? "" });
    const descInput = el("input", { type: "text", value: body.description ?? "" });
    const negInput = el("input", { type: "text", value: body.negative ?? "" });
    const suitsInput = el("input", {
      type: "text",
      value: (body.raw?.suits ?? body.factory_raw?.suits ?? []).join(", "),
      placeholder: "e.g. object, car — empty = universal (offered to every template)",
      title: "Which template types this section serves; typed templates filter "
        + "their pickers and random draws by this. Explicit picks are never restricted.",
    });
    const itemRows = [];
    const table = el("table", { class: "mrln-items-table" });
    table.append(
      el(
        "tr",
        {},
        el("td", { class: "mrln-w-origin" }),
        el("td", { class: "mrln-w-name mrln-note" }, "name"),
        el("td", { class: "mrln-note" }, "text"),
        el("td", { class: "mrln-w-weight mrln-note" }, "wt"),
        el("td", { class: "mrln-w-act" })
      )
    );

    function addItemRow(item = { name: "", text: "" }) {
      const row = {
        orig: item,
        hidden: Boolean(item.hidden),
        slots: (item.slots ?? []).map((s) => ({ ...s })), // child refs, grown via '{'
        name: el("input", { type: "text", value: item.name ?? "" }),
        text: el("input", { type: "text", value: item.text ?? "", title: item.text ?? "" }),
        weight: el("input", { type: "text", value: item.weight ?? "" }),
      };
      // '{' assist: pick a declared child, the trigger, or any section —
      // picking a section auto-declares it as a child slot of this item,
      // so its draw weaves into the text bare (no lead-in)
      const itemRefOptions = () => {
        const options = [];
        const used = new Set(["trigger"]);
        for (const s of row.slots) {
          options.push({ name: s.id, hint: `child → ${s.ref}` });
          used.add(s.id);
        }
        options.push({ name: "trigger", hint: "node trigger widget" });
        for (const sec of state.library?.sections ?? []) {
          if (row.slots.some((s) => s.ref === sec.slug)) continue;
          const base = sec.slug.split("/").pop();
          let id = base;
          let n = 2;
          while (used.has(id)) id = `${base}-${n++}`;
          used.add(id);
          options.push({ name: id, hint: sec.slug, create: { id, ref: sec.slug } });
        }
        return options;
      };
      const itemKnown = () => new Set(["trigger", ...row.slots.map((s) => s.id)]);
      row.text.dataset.mrlnBaseTitle = item.text ?? "";
      const textAssist = braceAssist(row.text, itemRefOptions, (option) => {
        if (option.create) row.slots.push({ ...option.create });
      });
      row.text.addEventListener("input", () => validateRefs(row.text, itemKnown));
      validateRefs(row.text, itemKnown);
      const fromFactory = item.origin === "factory";
      const originChip = fromFactory
        ? el("span", { class: "mrln-chip mrln-factory", title: "Lives in the factory tier" }, "F")
        : item.origin === "user"
          ? el("span", { class: "mrln-chip mrln-user", title: "Lives in your user tier" }, "U")
          : null;
      const actionButton = el(
        "button",
        {
          class: "mrln-btn",
          title: fromFactory
            ? "Hide this factory item from your pools (a tombstone in your user file — restorable)"
            : "Remove item",
          onclick: () => {
            if (fromFactory) {
              row.hidden = !row.hidden;
              tr.classList.toggle("mrln-hidden-item", row.hidden);
              actionButton.textContent = row.hidden ? "↩" : "🚫";
            } else {
              itemRows.splice(itemRows.indexOf(row), 1);
              tr.remove();
            }
          },
        },
        fromFactory ? (row.hidden ? "↩" : "🚫") : "✕"
      );
      const tr = el(
        "tr",
        { class: row.hidden ? "mrln-hidden-item" : null },
        el("td", { class: "mrln-w-origin" }, originChip),
        el("td", { class: "mrln-w-name" }, row.name),
        el("td", {}, textAssist),
        el("td", { class: "mrln-w-weight" }, row.weight),
        el("td", { class: "mrln-w-act" }, actionButton)
      );
      itemRows.push(row);
      table.append(tr);
      if (item.data?.lora !== undefined) {
        // LoRA block: an extra editor line for the loader metadata — the
        // text above stays the catchword that lands in the prompt.
        const picker = loraPicker(item.data.lora ?? "");
        row.lora = picker.file;
        row.loraFilter = picker.filter;
        row.loraControl = picker.control;
        row.loraSet = picker.set;
        row.sm = el("input", {
          type: "text",
          inputmode: "decimal",
          value: item.data.strength_model ?? 1.0,
          title: "strength_model",
        });
        row.sc = el("input", {
          type: "text",
          inputmode: "decimal",
          value: item.data.strength_clip ?? item.data.strength_model ?? 1.0,
          title: "strength_clip",
        });
        row.comment = el("input", {
          type: "text",
          value: item.data.comment ?? "",
          placeholder: "comment / AIR",
          title: "Free comment stored with this LoRA block — the Civitai lookup "
            + "fills in the model's AIR tag here",
        });
        // the base family lets LoRA Apply warn when this LoRA meets a model
        // of another architecture; an AIR already encodes it, so this only
        // has to be typed for files that carry none
        row.base = el("input", {
          type: "text",
          class: "mrln-narrow",
          value: item.data.base ?? "",
          placeholder: "base",
          title: "Base-model family this LoRA was trained for (flux1, sdxl, sd1, "
            + "qwen, pony…). LoRA Apply warns when it does not match the connected "
            + "model. Left empty, it is read from the AIR's ecosystem segment.",
        });
        // ---- trigger words: the LoRA's own pool, with the composer's M/S ----
        // data.lora_info.trained_words is PROVENANCE — what the source said,
        // never edited here. The text field above is the CATCHWORD: truth, the
        // words that actually render. So mute = absent from the catchword,
        // solo = the only one present, and the chips are a pure derivation
        // over two fields already on disk (see util.js triggerSelection, whose
        // server twin is mrln/promptapi/lora.py). Nothing is widget-only:
        // reopening this editor re-derives every chip from the FILE.
        row.loraInfo = { ...(item.data.lora_info ?? {}) };
        const triggerBox = el("div", { class: "mrln-triggers" });
        const trainedWords = () => cleanTriggerWords(row.loraInfo?.trained_words);

        function setCatchword(text) {
          // the input stays the truth: chips write THROUGH it, so both halves
          // can never disagree (and validateRefs/brace assist still run)
          row.text.value = text;
          row.text.dispatchEvent(new Event("input", { bubbles: false }));
          renderTriggers();
        }

        function msButton(label, on, title, onclick) {
          return el(
            "button",
            {
              class: `mrln-btn mrln-mini${on ? (label === "M" ? " mrln-m-on" : " mrln-s-on") : ""}`,
              title,
              onclick,
            },
            label
          );
        }

        function provenanceChip(word, selection) {
          const muted = selection.muted.includes(word);
          const soloed = triggerSoloed(selection, word);
          return el(
            "span",
            {
              class: `mrln-chip mrln-trigger${muted ? " mrln-trigger-muted" : ""}`,
              title: `trained word from ${row.loraInfo?.model_name ?? "this LoRA"} — `
                + `${muted ? "muted: kept as provenance, dropped from the catchword" : "renders"}`,
            },
            el(
              "span",
              { class: "mrln-ms" },
              msButton(
                "M",
                muted,
                "Mute — the word stays in the LoRA's trained words but drops out of "
                  + "the catchword, so it stops rendering. Click again to bring it back.",
                () => setCatchword(muteTriggerWord(selection.words, row.text.value, word))
              ),
              msButton(
                "S",
                soloed,
                "Solo — render only this trained word (every other one mutes; click "
                  + "again to un-mute them all). Words you typed yourself are left alone.",
                () => setCatchword(soloTriggerWord(selection.words, row.text.value, word))
              )
            ),
            el("span", { class: "mrln-trigger-word" }, word)
          );
        }

        function extraChip(word) {
          // A word with no provenance entry: there is nothing to un-mute from,
          // so its M can only REMOVE it — armed, never one click.
          const button = el(
            "button",
            {
              class: "mrln-btn mrln-mini",
              title: "You typed this word — it is NOT one of this LoRA's trained words, "
                + "so there is nothing to un-mute from: muting REMOVES it. Click twice.",
              onclick: () =>
                armDestructive(button, "Remove?", () =>
                  setCatchword(dropTriggerWord(row.text.value, word))
                ),
            },
            "M"
          );
          return el(
            "span",
            { class: "mrln-chip mrln-trigger mrln-trigger-extra", title: "user-added word" },
            el("span", { class: "mrln-ms" }, button),
            el("span", { class: "mrln-trigger-word" }, word)
          );
        }

        const wordsButton = el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            title: "Re-read this LoRA's trained words from Civitai (by file hash). "
              + "Provenance only — your catchword is never overwritten.",
            onclick: (e) => busy(e.currentTarget, refreshTriggerWords),
          },
          "⟳ words"
        );

        function renderTriggers() {
          const words = trainedWords();
          const selection = triggerSelection(words, row.text.value);
          const nodes = [];
          if (!words.length) {
            nodes.push(
              el(
                "span",
                { class: "mrln-note" },
                "no trained words recorded for this file — ⟳ asks Civitai for them"
              )
            );
          } else {
            for (const word of words) nodes.push(provenanceChip(word, selection));
            for (const word of selection.extra) nodes.push(extraChip(word));
            if (!selection.catchword) {
              // All-muted derives fine (nothing renders) but does NOT save: a
              // section item needs non-empty text (promptlib/schema.py
              // _parse_item), so say that here instead of letting the save fail.
              nodes.push(
                el(
                  "span",
                  { class: "mrln-error" },
                  "every word muted — nothing would render, and an item cannot be "
                    + "saved with empty text: keep one word or type your own"
                )
              );
            }
          }
          nodes.push(wordsButton);
          mount(triggerBox, ...nodes);
        }

        async function refreshTriggerWords() {
          const file = row.lora.value.trim();
          if (!file) {
            ctx.toast("error", "No LoRA file", "pick a file first — provenance follows the file");
            return;
          }
          let civ;
          try {
            civ = await ctx.apiJson(`/mrln/prompt/lora-civitai?name=${encodeURIComponent(file)}`);
          } catch (err) {
            ctx.toast("error", "Civitai lookup failed", err.message);
            return;
          }
          // merge, exactly like the server-side healer: the catchword is NOT
          // touched — overwriting a selection the user curated is the whole
          // thing the provenance/truth split exists to prevent
          row.loraInfo = { ...(row.loraInfo ?? {}), ...(civ.lora_info ?? {}) };
          renderTriggers();
          const found = trainedWords().length;
          ctx.toast(
            found ? "success" : "error",
            found ? "Trained words refreshed" : "Civitai lists no trained words",
            found ? `${found} word(s) — your catchword is unchanged` : "type the catchword yourself"
          );
        }

        // typing in the catchword feeds the chips: a word typed by hand that
        // IS a trained word un-mutes its chip, with no separate state to sync
        row.text.addEventListener("input", renderTriggers);
        renderTriggers();

        let loraMetaReq = 0;
        row.lora.addEventListener("change", async () => {
          if (!row.name.value.trim()) {
            const stem = row.lora.value.split(/[\\/]/).pop().replace(/\.\w+$/, "");
            row.name.value = stem.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40);
          }
          // switching files switches the trigger: file metadata first, then
          // Civitai by file hash (trigger fallback + AIR tag for the comment)
          const file = row.lora.value;
          row.text.classList.remove("mrln-input-error");
          // provenance describes a FILE — never let the old file's words
          // linger as chips over a new file's catchword
          row.loraInfo = {};
          renderTriggers();
          if (!file) return;
          const req = ++loraMetaReq;
          let found = false;
          try {
            const meta = await ctx.apiJson(
              `/mrln/prompt/lora-meta?name=${encodeURIComponent(file)}`
            );
            if (req !== loraMetaReq) return; // superseded by a newer switch
            row.text.value = meta.trigger;
            row.text.title = `trigger word from LoRA metadata (${meta.source})`;
            // safetensors metadata yields exactly ONE trigger, so the
            // provenance list derived from it has one entry — enough for a
            // chip, and the Civitai answer below replaces it when it has more
            row.loraInfo = { trained_words: [meta.trigger], file: meta.name ?? file };
            found = true;
          } catch {
            if (req !== loraMetaReq) return;
          }
          // auto-filled AIR follows the file on every switch; only a comment
          // the user typed themselves is preserved
          const commentIsAuto = () => {
            const current = row.comment.value.trim();
            return !current || current === row.autoAir || current.startsWith("urn:air:");
          };
          try {
            const civ = await ctx.apiJson(
              `/mrln/prompt/lora-civitai?name=${encodeURIComponent(file)}`
            );
            if (req !== loraMetaReq) return;
            // provenance from the same answer that names the trigger, so the
            // chips are populated without a second round trip
            row.loraInfo = { ...(row.loraInfo ?? {}), ...(civ.lora_info ?? {}) };
            if (!found && (civ.catchword || civ.trigger)) {
              // the server's DEFAULT selection (first trained word) — taking
              // its text rather than re-deriving one keeps a new item
              // byte-identical to what the lookup has always written
              row.text.value = civ.catchword || civ.trigger;
              row.text.title = `trigger word from Civitai (${civ.model_name ?? "model"}`
                + `${civ.trained_words?.length > 1 ? "; the rest are muted chips below" : ""})`;
              found = true;
            }
            if (commentIsAuto()) {
              row.comment.value = civ.air ?? "";
              row.autoAir = civ.air ?? "";
            }
          } catch {
            if (req !== loraMetaReq) return;
            if (commentIsAuto()) {
              // the previous file's AIR must not stick to a file Civitai
              // doesn't know
              row.comment.value = "";
              row.autoAir = "";
            }
          }
          if (!found) {
            row.text.value = "";
            row.text.classList.add("mrln-input-error");
            row.text.title = "No trigger word found in file metadata or on Civitai — "
              + "type the trigger word / catchword yourself";
          }
          renderTriggers(); // chips follow the new file's provenance + catchword
        });
        // Missing-file healer: a template shared across machines carries the
        // file name + AIR urn — when the file is absent here, offer the
        // Civitai download (background, SHA256-verified) and re-point the
        // block if the chosen folder differs.
        row.missingBox = el("div", { class: "mrln-lora-missing", style: "display:none" });
        const norm = (n) => n.replaceAll("\\", "/").toLowerCase();
        const downloadMissing = async (file, air) => {
          const parts = file.replaceAll("\\", "/").split("/");
          const filename = parts.pop();
          const folder = await askString(
            "Download LoRA from Civitai",
            "Subfolder under your loras directory (empty = root):",
            parts.join("/")
          );
          if (folder == null) return; // cancelled
          try {
            await ctx.apiJson("/mrln/prompt/lora-download", {
              method: "POST",
              body: {
                air,
                start: true,
                folder,
                filename,
                section: slug ?? "",
                item: row.name.value.trim(),
                stored: file,
              },
            });
          } catch (err) {
            ctx.toast("error", "Download failed to start", err.message);
            return;
          }
          const progress = el("span", { class: "mrln-note" }, "starting download…");
          mount(row.missingBox, progress);
          const body = await pollLoraDownload(air, (tick) => {
            progress.textContent = `downloading… ${loraProgressText(tick)}`;
          });
          if (body.status !== "done") {
            ctx.toast("error", "LoRA download failed", body.detail ?? "");
            checkMissing();
            return;
          }
          invalidateInstalledLoras(); // pickers must list the new file
          ctx.toast(
            "success",
            "LoRA downloaded",
            body.healed ? `${body.name} — LoRA block re-pointed` : body.name
          );
          if (body.healed) {
            row.loraSet(body.healed); // picker + trigger/AIR follow the heal
            await refreshDetail(); // compose tab pools reflect the new path
          } else {
            checkMissing();
            refreshLoraBanner(state.slug); // one entry fewer on the compose banner
          }
        };
        const checkMissing = async () => {
          const file = row.lora.value;
          if (!file) {
            row.missingBox.style.display = "none";
            return;
          }
          const names = await installedLoras();
          if (!names.length || names.some((n) => norm(n) === norm(file))) {
            row.missingBox.style.display = "none";
            return;
          }
          const air = (row.comment.value || "").trim();
          row.missingBox.style.display = "";
          mount(row.missingBox, 
            el("span", { class: "mrln-error" }, "file missing on this machine"),
            air.toLowerCase().startsWith("urn:air:")
              ? el(
                  "button",
                  {
                    class: "mrln-btn",
                    title: `Download ${air} from Civitai into your loras folder `
                      + "(background, SHA256-verified) and re-point this block "
                      + "if the path changes",
                    onclick: () => downloadMissing(file, air),
                  },
                  "⬇ Get from Civitai"
                )
              : el(
                  "span",
                  { class: "mrln-note" },
                  "no AIR in the comment — pick a local file instead"
                )
          );
        };
        row.lora.addEventListener("change", checkMissing);
        checkMissing();
        // The row's head cell — nothing reads its children, so a LoRA preview
        // tile mounts by prepending here (or by inserting a cell into
        // row.loraRow). Kept as a named handle for exactly that.
        row.loraTileMount = el(
          "td",
          { class: "mrln-w-origin" },
          el("span", { class: "mrln-chip mrln-user" }, "LoRA")
        );
        row.loraRow = el(
          "tr",
          { class: "mrln-lora-row" },
          row.loraTileMount,
          el("td", { colspan: 2 }, el("div", { class: "mrln-inline" }, row.loraFilter, row.loraControl)),
          el("td", { class: "mrln-w-weight" }, el("div", { class: "mrln-inline" }, row.sm, row.sc)),
          el("td", { class: "mrln-w-act" })
        );
        // The preview tile + its set/reset/refresh controls. Keyed on the
        // FILE, not the item: a Civitai preview is stored per LoRA, so one
        // download serves every item referencing the same weights — which
        // means the tile has to follow a file change, not an item rename.
        // `hasThumb` is passed ONLY while the file still matches the row the
        // server annotated; after an edit the flag describes a different file,
        // and omitting it makes the tile ask instead of assume.
        const thumbBox = el("span", {});
        const paintThumb = () => {
          const file = row.lora.value.trim();
          const known = file && file === (item.data?.lora ?? "");
          mount(thumbBox, 
            file
              ? thumbControls("loras", file, {
                  section: slug ?? "",
                  item: row.name.value.trim(),
                  domain: slug ?? "",
                  ...(known ? { hasThumb: Boolean(item.has_thumb) } : {}),
                })
              : el("span", { class: "mrln-note" }, "pick a LoRA file to give it a preview")
          );
        };
        row.lora.addEventListener("change", paintThumb);
        paintThumb();
        table.append(
          row.loraRow,
          el(
            "tr",
            { class: "mrln-lora-row" },
            el("td", { class: "mrln-w-origin" }),
            el("td", { colspan: 3 }, triggerBox),
            el("td", { class: "mrln-w-act" })
          ),
          el(
            "tr",
            { class: "mrln-lora-row mrln-lora-end" },
            el("td", { class: "mrln-w-origin" }),
            el(
              "td",
              { colspan: 3 },
              el("div", { class: "mrln-inline" }, row.comment, row.base),
              thumbBox,
              row.missingBox
            ),
            el("td", { class: "mrln-w-act" })
          )
        );
      }
    }
    for (const item of body.items ?? []) addItemRow(item);

    function cleanedItem(row) {
      const item = { ...row.orig, name: row.name.value.trim(), text: row.text.value };
      if (row.slots.length) item.slots = row.slots.map((s) => ({ ...s }));
      delete item.origin; // runtime provenance, never persisted
      delete item.hidden;
      if (!item.name) delete item.name;
      const weight = parseFloat(row.weight.value);
      if (!Number.isNaN(weight) && weight !== 1) item.weight = weight;
      else delete item.weight;
      for (const key of ["negative", "text_short"]) if (!item[key]) delete item[key];
      for (const key of ["tags", "excludes", "requires", "slots"]) {
        if (Array.isArray(item[key]) && !item[key].length) delete item[key];
      }
      if (row.lora) {
        const name = row.lora.value.trim();
        if (name) {
          const sm = parseFloat(row.sm.value);
          const sc = parseFloat(row.sc.value);
          item.data = {
            ...(item.data ?? {}),
            lora: name,
            strength_model: Number.isNaN(sm) ? 1.0 : sm,
            strength_clip: Number.isNaN(sc) ? (Number.isNaN(sm) ? 1.0 : sm) : sc,
          };
          const comment = row.comment?.value.trim();
          if (comment) item.data.comment = comment;
          else delete item.data.comment;
          const base = row.base?.value.trim().toLowerCase();
          if (base) item.data.base = base;
          else delete item.data.base;
          // provenance for the trigger chips: what the SOURCE said, never what
          // the user selected (the selection is the text). Only written when
          // there is something to say, so no pointless key lands in a file.
          const info = row.loraInfo ?? {};
          if (Object.keys(info).length) item.data.lora_info = info;
          else delete item.data.lora_info;
        } else if (item.data) {
          delete item.data.lora;
          delete item.data.strength_model;
          delete item.data.strength_clip;
          delete item.data.base;
          delete item.data.lora_info; // provenance about a file no longer referenced
        }
      }
      if (item.data == null || (typeof item.data === "object" && !Object.keys(item.data).length)) {
        delete item.data;
      }
      if (row.hidden) item.hidden = true;
      return item;
    }

    function rowEdited(row) {
      // Gate for the thin extend diff: a factory-origin row is stored ONLY
      // when this is true, so it has to see every field cleanedItem persists
      // — the LoRA block (file/strengths/comment/base) and the child slots
      // included, not just name/text/weight.
      return itemRowEdited(
        {
          name: row.name.value,
          text: row.text.value,
          weight: row.weight.value,
          slots: row.slots,
          lora: row.lora ? row.lora.value : undefined,
          sm: row.sm?.value,
          sc: row.sc?.value,
          comment: row.comment?.value,
          base: row.base?.value,
          loraInfo: row.loraInfo, // trigger-word provenance, see itemRowEdited
        },
        row.orig
      );
    }

    function fieldValue(input, factoryValue) {
      // extend mode: equal-to-factory (or empty) inherits — omit from the file
      const value = input.value.trim();
      if (saveMode === "extend" && (!value || value === (factoryValue ?? ""))) return null;
      return value || null;
    }

    async function save() {
      const targetSlug = slugInput.value.trim();
      // An all-muted LoRA renders nothing, which is a legal SELECTION but not
      // a legal FILE: promptlib/schema.py rejects an item without non-empty
      // text. Catch it here so the user gets the fix instead of "items[3] is
      // missing a non-empty 'text'" after the round trip.
      const empty = itemRows.find((row) => row.lora?.value.trim() && !row.text.value.trim() && !row.hidden);
      if (empty) {
        ctx.toast(
          "error",
          "A LoRA block has no catchword",
          `'${empty.name.value.trim() || empty.lora.value}': every trigger word is muted. `
            + "Un-mute one, or type your own catchword — an item cannot be saved "
            + "with empty text."
        );
        empty.text.classList.add("mrln-input-error");
        return;
      }
      const extending = saveMode === "extend" && factoryBaseline;
      const data = extending ? { version: 1 } : { ...body.raw, version: 1 };
      delete data.replaces;
      const fields = [
        ["label", labelInput, factoryBaseline?.label],
        ["description", descInput, factoryBaseline?.description],
        ["negative", negInput, factoryBaseline?.negative],
      ];
      for (const [key, input, factoryValue] of fields) {
        const value = extending ? fieldValue(input, factoryValue) : input.value.trim() || null;
        if (value) data[key] = value;
        else delete data[key];
      }
      const suits = suitsInput.value.split(",").map((v) => v.trim()).filter(Boolean);
      const factorySuits = factoryBaseline?.suits ?? [];
      if (extending && JSON.stringify(suits) === JSON.stringify(factorySuits)) delete data.suits;
      else if (suits.length) data.suits = suits;
      else delete data.suits;
      if (extending) {
        // thin diff: edited/new/user items + bare tombstones for hidden ones
        data.items = [];
        for (const row of itemRows) {
          if (row.orig.origin === "factory") {
            if (row.hidden) data.items.push({ name: row.orig.name, hidden: true });
            else if (rowEdited(row)) data.items.push(cleanedItem(row));
          } else {
            data.items.push(cleanedItem(row));
          }
        }
      } else {
        // replace: hidden factory items simply stay out of the copy;
        // a user's own hidden item persists WITH its flag (soft-disabled)
        data.items = itemRows
          .filter((row) => !(row.hidden && row.orig.origin === "factory"))
          .map(cleanedItem);
        if (saveMode === "replace") data.replaces = true;
      }
      // renames of YOUR items re-point every user-tier template default
      // server-side; factory-origin rows are copies, not renames
      const renames = {};
      for (const row of itemRows) {
        const oldName = (row.orig.name ?? "").trim();
        const newName = row.name.value.trim();
        if (oldName && newName && oldName !== newName && row.orig.origin !== "factory") {
          renames[oldName] = newName;
        }
      }
      let saved;
      try {
        saved = await ctx.apiJson("/mrln/prompt/save-section", {
          method: "POST",
          body: { slug: targetSlug, data, renames },
        });
      } catch (err) {
        ctx.toast("error", "Save failed", err.message);
        return;
      }
      const how = extending
        ? "extends factory — only your changes stored"
        : saveMode === "replace"
          ? "replaces factory entirely"
          : "user library";
      const repointed = saved?.templates_rewritten
        ? ` — ${saved.templates_rewritten} template file(s) re-pointed to renamed items`
        : "";
      ctx.toast("success", "Section saved", `${targetSlug} (${how})${repointed}`);
      state.libGroups.add("sections:@block"); // reveal where it landed
      state.libGroups.add(`sections:${targetSlug.split("/")[0]}`);
      ctx.refreshCombos();
      await loadLibrary();
      openSectionEditor(targetSlug);
      if (state.slug) {
        applyItemRenames(targetSlug, renames);
        await refreshDetail(); // the loaded template may draw this section
      }
    }

    const modeSelect = el("select", {
      title: "How your user file compounds with the factory section",
      onchange: (e) => {
        saveMode = e.target.value;
      },
    });
    if (hasFactory) {
      modeSelect.append(
        el(
          "option",
          { value: "extend" },
          "extend factory — save only my changes (survives factory updates)"
        ),
        el("option", { value: "replace" }, "replace factory — full frozen copy")
      );
      modeSelect.value = saveMode;
    }

    const actions = [
      el(
        "button",
        { class: "mrln-btn mrln-primary", onclick: (e) => busy(e.currentTarget, save) },
        "Save to user library"
      ),
    ];
    if (body.tier === "user") {
      actions.push(
        el(
          "button",
          {
            class: "mrln-btn",
            title: hasFactory
              ? "Delete your user file — the slug reverts to pure factory content"
              : "Delete your user file",
            onclick: (e) => busy(e.currentTarget, () => deleteEntry("sections", slug)),
          },
          "Delete user file"
        )
      );
    }

    setEditor(
      el(
        "div",
        { class: "mrln-tree-head" },
        slug ? `Section: ${slug}` : "New section",
        body.merged
          ? el("span", { class: "mrln-chip mrln-merged" }, "factory+user")
          : tierChip(body.tier),
        editorCloseBtn()
      ),
      body.merged
        ? el(
            "div",
            { class: "mrln-note" },
            "Combined view — F/U marks where each item lives. Saving stores only your changes."
          )
        : body.tier === "factory"
          ? el(
              "div",
              { class: "mrln-note" },
              "Factory file — saving creates a user-tier file that extends (or replaces) it."
            )
          : null,
      // The section's face in the browse grid. Only for a section that exists
      // on disk: a thumbnail is stored under a slug, and a section still being
      // typed has none to store it under (the field above is editable, so a
      // guessed slug could write a file for a section that never gets saved).
      slug ? thumbControls("sections", slug, {}) : null,
      field("Slug", slugInput),
      field("Label", labelInput),
      field("Description", descInput),
      field("Negative", negInput),
      field("Suits (template types)", suitsInput),
      hasFactory ? field("Save mode", modeSelect) : null,
      el("span", { class: "mrln-field-name" }, "Items"),
      table,
      el(
        "div",
        { class: "mrln-actions" },
        el("button", { class: "mrln-btn", onclick: () => addItemRow() }, "+ item"),
        el(
          "button",
          {
            class: "mrln-btn",
            title: "Add a LoRA block: catchword text for the prompt plus loader "
              + "metadata (file + strengths) signalled to the LoRA Apply (MRLN) "
              + "node via the template node's loras output",
            onclick: () =>
              addItemRow({
                name: "",
                text: "",
                data: { lora: "", strength_model: 1.0, strength_clip: 1.0 },
              }),
          },
          "+ LoRA block"
        ),
        ...actions
      )
    );
  }

  return { newCombineSection, newSection, openSectionEditor };
}
