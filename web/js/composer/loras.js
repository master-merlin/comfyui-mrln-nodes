// MRLN Prompt Composer — LoRA surfaces: the missing-file banner on the compose
// tab, the bounded download poller, the installed-file list and the drill-down
// file picker the section editor mounts.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js). All mutable state
// (the installed-LoRA promise cache, the banner element) lives inside
// createLoras().
import { downloadableAir, loraKey, loraProgressText, missingLoraRows } from "./util.js";
import { el, mount, placeMenu } from "./dom.js";

export function createLoras(hub) {
  const { ctx, state } = hub;
  // late-bound cross-module call (see composer/state.js for the why)
  const refreshDetail = (...a) => hub.refreshDetail(...a);

  // ---- missing-LoRA banner -------------------------------------------------
  // A LoRA item references its .safetensors by NAME and carries the Civitai
  // AIR in its comment. A template that travelled between machines therefore
  // LOADS fine and only dies later, inside LoRA Apply, with a
  // FileNotFoundError. This states it up front and offers the same AIR
  // download the Library tab's item rows do — in bulk.

  const loraBanner = el("div", { class: "mrln-lora-banner", style: "display:none" });

  async function fetchLoraStatus(slug) {
    // slug omitted = the whole library. An older server (or a failing scan)
    // must not break the tab: no answer simply means no banner.
    const query = slug ? `?template=${encodeURIComponent(slug)}` : "";
    try {
      return await ctx.apiJson(`/mrln/prompt/lora-status${query}`);
    } catch {
      return null;
    }
  }

  function pollLoraDownload(air, onProgress) {
    // Resolves with the TERMINAL status body ({status: "done"|"error"|…}).
    // Bounded on both axes — a poll loop that cannot end outlives the panel
    // that started it: ~40 min of progress ticks, or 5 consecutive unreadable
    // polls, then it stops watching (the server-side download runs on).
    return new Promise((resolve) => {
      let ticks = 0;
      let failures = 0;
      const tick = async () => {
        let body;
        try {
          body = await ctx.apiJson(`/mrln/prompt/lora-download?air=${encodeURIComponent(air)}`);
          failures = 0;
        } catch (err) {
          if (++failures >= 5) {
            resolve({ status: "error", detail: `progress unreadable: ${err.message}` });
            return;
          }
          setTimeout(tick, 3000);
          return;
        }
        if (body.status !== "downloading") {
          resolve(body);
          return;
        }
        onProgress?.(body);
        if (++ticks > 1600) {
          resolve({
            status: "error",
            detail: "still running after ~40 min — the download continues in the "
              + "background, reopen the template to check on it",
          });
          return;
        }
        setTimeout(tick, 1500);
      };
      setTimeout(tick, 1500);
    });
  }

  async function getMissingLoras(rows, notes) {
    // Sequential on purpose: multi-GB pulls in parallel starve each other and
    // the progress status is keyed by AIR, so one at a time stays readable.
    let done = 0;
    let failed = 0;
    let healed = false;
    for (const row of rows) {
      const air = downloadableAir(row);
      const note = notes.get(row);
      // No subfolder question in bulk (askString per file would be a modal
      // storm): the stored path's own folder is where the author had it —
      // the same value the section editor pre-fills.
      const parts = String(row.file ?? "").replaceAll("\\", "/").split("/");
      const filename = parts.pop();
      note.textContent = " — starting…";
      try {
        await ctx.apiJson("/mrln/prompt/lora-download", {
          method: "POST",
          body: {
            air,
            start: true,
            folder: parts.join("/"),
            filename,
            section: row.section ?? "",
            item: row.item ?? "",
            stored: row.file ?? "",
          },
        });
      } catch (err) {
        failed++;
        note.textContent = ` — ${err.message}`;
        continue;
      }
      const body = await pollLoraDownload(air, (progress) => {
        note.textContent = ` — downloading… ${loraProgressText(progress)}`;
      });
      if (body.status === "done") {
        done++;
        loraListPromise = null; // pickers must list the new file
        if (body.healed) healed = true;
        note.textContent = body.healed ? ` — done → ${body.healed}` : " — done";
      } else {
        failed++;
        note.textContent = ` — failed: ${body.detail || body.status}`;
      }
    }
    return { done, failed, healed };
  }

  function renderLoraBanner(status) {
    const rows = missingLoraRows(status);
    if (!rows.length) {
      mount(loraBanner);
      loraBanner.style.display = "none";
      return;
    }
    const notes = new Map();
    const list = el(
      "ul",
      { class: "mrln-import-plan" },
      rows.map((row) => {
        const note = el(
          "span",
          { class: "mrln-note" },
          downloadableAir(row) ? "" : " — no AIR — pick a local file in the Library tab"
        );
        notes.set(row, note);
        return el(
          "li",
          {},
          el("b", {}, row.file || "(no file name)"),
          el(
            "span",
            { class: "mrln-slug" },
            ` ${row.uses.map((use) => `${use.section} · ${use.item}`).join(", ")}`
          ),
          note
        );
      })
    );
    const downloadable = rows.filter((row) => downloadableAir(row));
    let action = null;
    if (status?.can_download === false) {
      action = el(
        "div",
        { class: "mrln-note" },
        "Downloads need a running ComfyUI — copy these files into your loras folder by hand."
      );
    } else if (downloadable.length) {
      const button = el(
        "button",
        {
          class: "mrln-btn mrln-primary",
          title: "Download every missing file that carries an AIR from Civitai "
            + "(one after another, background, SHA256-verified) and re-point the "
            + "section items whose path changes",
          onclick: async () => {
            if (button.disabled) return;
            button.disabled = true;
            const slug = state.slug;
            const no = state.templateNo;
            try {
              const result = await getMissingLoras(downloadable, notes);
              if (result.healed && no === state.templateNo) await refreshDetail();
              ctx.toast(
                result.failed ? "warn" : "success",
                "LoRA downloads finished",
                `${result.done} downloaded`
                  + (result.failed ? `, ${result.failed} failed — see the list` : "")
              );
              await refreshLoraBanner(slug, no); // hides itself once nothing is missing
            } finally {
              button.disabled = false;
            }
          },
        },
        `⬇ Get all missing from Civitai (${downloadable.length})`
      );
      action = el("div", { class: "mrln-actions" }, button);
    }
    mount(loraBanner, 
      el(
        "div",
        { class: "mrln-error" },
        `⚠ ${rows.length} LoRA file(s) this template draws are missing on this machine — `
          + "queueing the graph fails in LoRA Apply until they are there."
      ),
      list,
      action
    );
    loraBanner.style.display = "";
  }

  async function refreshLoraBanner(slug, token = state.templateNo) {
    // Supersede guard, same token as selectTemplate: a fast template switch
    // must never paint the previous template's missing files over the new one.
    if (!slug) {
      renderLoraBanner(null); // template-scoped only — never library-wide here
      return null;
    }
    const status = await fetchLoraStatus(slug);
    if (token !== state.templateNo || slug !== state.slug) return status;
    renderLoraBanner(status);
    return status;
  }

  async function warnMissingLoras(plan) {
    // Section-only bundles never reach the compose tab, so the banner never
    // fires for them — same status source, scoped to the files this bundle
    // actually brought, delivered as a toast.
    const wanted = new Set((plan.loras ?? []).map((entry) => loraKey(entry.file)));
    if (!wanted.size) return;
    const missing = missingLoraRows(await fetchLoraStatus(null)).filter((row) =>
      wanted.has(loraKey(row.file))
    );
    if (!missing.length) return;
    ctx.toast(
      "warn",
      `${missing.length} referenced LoRA file(s) missing`,
      `${missing.map((row) => row.file).join(", ")} — open the section in the Library `
        + "tab to download them from Civitai"
    );
  }

  // ---- installed files + the drill-down picker -----------------------------

  let loraListPromise = null;
  function installedLoras() {
    // PROMISE cache, not a value cache: a section with N LoRA rows calls this
    // 2N times as it opens, and the old value cache only helped once the first
    // pair of requests had already resolved. A resolved EMPTY list is a valid
    // answer too (an empty loras folder used to refetch both endpoints
    // forever) — only an unreachable server stays uncached, so it can retry.
    if (!loraListPromise) {
      loraListPromise = (async () => {
        let names = [];
        let answered = false;
        // primary: the dedicated models endpoint (full list incl. subfolders —
        // modern frontends load combos lazily, so object_info may be incomplete)
        try {
          const viaModels = await ctx.apiJson("/models/loras");
          if (Array.isArray(viaModels)) {
            answered = true;
            names = viaModels
              .map((entry) => (typeof entry === "string" ? entry : entry?.name))
              .filter(Boolean);
          }
        } catch {
          /* older server without /models */
        }
        if (!names.length) {
          try {
            const info = await ctx.apiJson("/object_info/LoraLoader");
            const spec = info?.LoraLoader?.input?.required?.lora_name;
            if (Array.isArray(spec)) {
              answered = true;
              if (Array.isArray(spec[0])) names = spec[0];
              else if (Array.isArray(spec[1]?.options)) names = spec[1].options;
            }
          } catch {
            /* endpoint unavailable */
          }
        }
        if (!answered) loraListPromise = null; // never cache "the server said nothing"
        return [...new Set(names)].sort((a, b) => a.localeCompare(b));
      })();
    }
    return loraListPromise;
  }

  function invalidateInstalledLoras() {
    // A fresh download changes what the pickers must list. Exported because
    // the section editor's own download path has to drop the cache too (in the
    // single-closure panel it just assigned the shared 'let').
    loraListPromise = null;
  }

  function loraPicker(current) {
    // A drill-down browser in dropdown clothes: the list shows the current
    // folder's subfolders + files, clicking a folder descends IN PLACE
    // (native selects close on click, so this is a custom menu), '..' goes
    // up a level. The filter searches flat across all folders.
    const value = el("input", { type: "hidden", value: current ?? "" });
    const baseOf = (name) => name.slice(Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\")) + 1);
    const dirOf = (name) => {
      const cut = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
      return cut === -1 ? "" : name.slice(0, cut).replace(/\\/g, "/");
    };
    const control = el(
      "button",
      { type: "button", class: "mrln-btn mrln-lora-current", title: current || "" },
      current ? baseOf(current) : "— choose LoRA —"
    );
    const menu = el("div", { class: "mrln-brace-menu mrln-lora-menu", style: "display:none" });
    const wrap = el("span", { class: "mrln-assist" }, control, menu, value);
    const filter = el("input", {
      type: "text",
      class: "mrln-lora-filter",
      placeholder: "filter…",
      title: "Substring filter across all folders",
    });
    let names = current ? [current] : [];
    let cwd = current ? dirOf(current) : "";
    let open = false;
    const onScroll = (e) => {
      if (!menu.contains(e.target)) hide(); // fixed menus must not desync
    };
    const hide = () => {
      open = false;
      menu.style.display = "none";
      window.removeEventListener("scroll", onScroll, true);
    };
    const choose = (name) => {
      value.value = name;
      control.textContent = baseOf(name);
      control.title = name;
      hide();
      value.dispatchEvent(new Event("change"));
    };
    const entry = (cls, text, action) =>
      el(
        "div",
        {
          class: `mrln-brace-item${cls ? ` ${cls}` : ""}`,
          title: text, // ellipsised rows reveal their full name on hover
          onmousedown: (e) => {
            e.preventDefault(); // keep focus → no blur-close before the click
            action();
          },
        },
        text
      );
    const render = () => {
      const needle = filter.value.trim().toLowerCase();
      const out = [];
      if (needle) {
        const hits = names.filter((n) => n.toLowerCase().includes(needle));
        for (const name of hits.slice(0, 200)) {
          out.push(entry(name === value.value ? "mrln-lora-sel" : "", name.replace(/\\/g, "/"), () => choose(name)));
        }
        if (!out.length) out.push(el("div", { class: "mrln-note", style: "padding:3px 6px" }, "no matches"));
      } else {
        if (cwd) {
          out.push(el("div", { class: "mrln-note", style: "padding:2px 6px" }, `📁 ${cwd}/`));
          out.push(
            entry("mrln-lora-dir", "📁 ..", () => {
              cwd = cwd.includes("/") ? cwd.slice(0, cwd.lastIndexOf("/")) : "";
              render();
            })
          );
        }
        const prefix = cwd ? `${cwd}/` : "";
        const subdirs = new Set();
        const files = [];
        for (const name of names) {
          const norm = name.replace(/\\/g, "/");
          if (!norm.startsWith(prefix)) continue;
          const rest = norm.slice(prefix.length);
          const slash = rest.indexOf("/");
          if (slash === -1) files.push(name);
          else subdirs.add(rest.slice(0, slash));
        }
        for (const d of [...subdirs].sort((a, b) => a.localeCompare(b))) {
          out.push(
            entry("mrln-lora-dir", `📁 ${d}/`, () => {
              cwd = prefix + d;
              render();
            })
          );
        }
        for (const name of files.sort((a, b) => baseOf(a).localeCompare(baseOf(b)))) {
          out.push(entry(name === value.value ? "mrln-lora-sel" : "", baseOf(name), () => choose(name)));
        }
        if (!subdirs.size && !files.length) {
          // zero state: with no installed LoRAs (or both listing endpoints
          // down) this menu was an unexplained empty rectangle
          out.push(
            el(
              "div",
              { class: "mrln-note", style: "padding:3px 6px" },
              cwd
                ? "this folder is empty"
                : "no LoRAs found — check your ComfyUI models/loras folder"
            )
          );
        }
      }
      mount(menu, ...out);
      menu.style.display = "";
      placeMenu(control, menu);
      if (!open) window.addEventListener("scroll", onScroll, true);
      open = true;
    };
    control.addEventListener("click", () => {
      if (open) {
        hide();
        return;
      }
      cwd = value.value ? dirOf(value.value) : cwd;
      render();
    });
    control.addEventListener("blur", () => setTimeout(hide, 150));
    filter.addEventListener("input", render);
    filter.addEventListener("blur", () => setTimeout(hide, 150));
    // Escape closes it, like braceAssist — a menu that only a blur or a
    // re-click dismisses feels stuck
    for (const node of [control, filter]) {
      node.addEventListener("keydown", (e) => {
        if (e.key === "Escape") hide();
      });
    }
    installedLoras().then((list) => {
      if (list.length) names = list;
      if (value.value && !names.includes(value.value)) {
        control.textContent = `⚠ ${baseOf(value.value)}`;
        control.title = `${value.value} — not installed`;
      }
    });
    return { file: value, filter, control: wrap, set: choose };
  }

  return {
    installedLoras,
    invalidateInstalledLoras,
    loraBanner,
    loraPicker,
    pollLoraDownload,
    refreshLoraBanner,
    renderLoraBanner,
    warnMissingLoras,
  };
}
