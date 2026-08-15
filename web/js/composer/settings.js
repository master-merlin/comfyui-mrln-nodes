// MRLN Prompt Composer — the Settings tab: the Civitai key, the two local LLM
// backend URLs, the remote-backend gate, the cloud API keys and how much render
// history is kept. Every secret is stored SERVER-side in the user tier and never
// echoed back — this tab only ever shows whether one exists, and never puts a
// key into a node widget (widget values persist into workflow PNGs).
//
// TWO GUARDS RUN THROUGH THE WHOLE FILE:
//  1. `settingsLoaded`. A failed GET /settings leaves every control at its
//     empty/default rendering for a reason that has NOTHING to do with what is
//     stored, so no control may save from that state: an empty URL reverts the
//     stored one to the packaged default, an unchecked box would turn recording
//     off, and a "loopback only" chip would clamp a gate that is actually open.
//     The controls disable themselves AND the pure payload builders below
//     refuse — belt and braces, because only the builders are testable and
//     `busy()` re-enables a button in its finally.
//  2. The remote-backend gate widens what the SERVER may fetch (the URL is
//     user-supplied and ComfyUI is the one that makes the request), so turning
//     it ON is armed like a destructive action. window.confirm throws on the
//     Electron frontend — dom.js `armDestructive` is this panel's confirm.
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js).
import { armDestructive, busy, el, mount } from "./dom.js";

// ---- pure logic (exported for tests) ---------------------------------------

/** Why a control refuses to save when GET /settings never landed. */
export const SETTINGS_NOT_LOADED =
  "The stored settings never loaded, so this control does not know what it "
  + "would be overwriting and will not save. Reopen the tab once the server answers.";

/**
 * Verbatim from `handle_save_settings` in mrln/promptapi/settings.py, so a
 * value the client refuses and a value the server refuses read identically.
 * A bool is refused ON PURPOSE: bool is an int in Python and `true` would
 * silently mean "1 month" — hence `typeof raw === "boolean"` first below,
 * before any numeric coercion (Number(true) === 1).
 */
export const HISTORY_MONTHS_ERROR =
  "'history_months' must be a whole number of months >= 0 (0 keeps everything)";

/** Verbatim from the same handler. */
export const HISTORY_ENABLED_ERROR =
  "'history_enabled' must be a JSON boolean (true or false)";

/** Verbatim from the same handler. */
export const ALLOW_REMOTE_ERROR = "'llm.allow_remote' must be true or false";

/**
 * The hint appended to a backend failure that the loopback gate caused. The
 * server sends BACKEND_REMOTE_REMEDIATION with every such refusal, but the
 * cached probe path (api.js keeps only `err.message` in a cache entry) drops
 * it, and that is precisely the path the green/red marks use — so the message
 * that names a control gets to name a control that now exists.
 */
export const REMOTE_GATE_HINT =
  "'Allow remote backends' below is the switch that permits a backend on another "
  + "machine, and only belongs on a network you trust";

/**
 * A months value as the number input hands it over (string), or already a
 * number. Returns {ok: true, value} | {ok: false, error}. Mirrors the server's
 * rule exactly: a whole number >= 0, never a bool.
 */
export function parseHistoryMonths(raw) {
  if (typeof raw === "boolean") return { ok: false, error: HISTORY_MONTHS_ERROR };
  if (typeof raw === "number") {
    // Number.isSafeInteger also rejects NaN, Infinity and 1e21 — a value JSON
    // would hand the server as something it never typed
    if (!Number.isSafeInteger(raw) || raw < 0) return { ok: false, error: HISTORY_MONTHS_ERROR };
    return { ok: true, value: raw };
  }
  if (typeof raw !== "string") return { ok: false, error: HISTORY_MONTHS_ERROR };
  // digits only: "" (an emptied input), "-1", "1.5", "1e3" and " 12 months"
  // all mean the user is mid-edit or wrong, never "keep everything"
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return { ok: false, error: HISTORY_MONTHS_ERROR };
  const value = Number(text);
  if (!Number.isSafeInteger(value)) return { ok: false, error: HISTORY_MONTHS_ERROR };
  return { ok: true, value };
}

/**
 * The POST /save-settings body for the retention row, or the reason there
 * isn't one. `enabled` is the checkbox's `.checked`, `months` the number
 * input's `.value`.
 */
export function historySavePayload({ settingsLoaded, enabled, months, thumbs } = {}) {
  if (!settingsLoaded) return { ok: false, error: SETTINGS_NOT_LOADED };
  if (typeof enabled !== "boolean") return { ok: false, error: HISTORY_ENABLED_ERROR };
  const parsed = parseHistoryMonths(months);
  if (!parsed.ok) return parsed;
  const body = { history_enabled: enabled, history_months: parsed.value };
  // Omitted rather than defaulted when the caller does not track it: sending
  // a guessed `true` would turn the tiles back on for someone who opted out.
  if (typeof thumbs === "boolean") body.history_thumbs = thumbs;
  return { ok: true, body };
}

/**
 * The POST /save-settings body for the remote gate, or the reason there isn't
 * one. Refuses in BOTH directions when the settings never loaded: the tab is
 * then showing a default, not a stored value, so "turn it off" would be the
 * same blind overwrite as "turn it on" — and the user cannot see what they are
 * changing either way.
 */
export function allowRemotePayload({ settingsLoaded, next } = {}) {
  if (typeof next !== "boolean") return { ok: false, error: ALLOW_REMOTE_ERROR };
  if (!settingsLoaded) return { ok: false, error: SETTINGS_NOT_LOADED };
  return { ok: true, body: { llm: { allow_remote: next } } };
}

/** Suffix for a red backend status line: the server's remediation if it
 *  survived the trip, else the gate hint when the gate is what refused. */
export function backendFailureHint(message, remediation, allowRemote) {
  const fromServer = String(remediation ?? "").trim();
  if (fromServer) return ` — ${fromServer}`;
  const text = String(message ?? "");
  if (!allowRemote && /loopback|not this machine/i.test(text)) return ` — ${REMOTE_GATE_HINT}`;
  return "";
}

/** One line of English for the retention state, for the toast and the row. */
export function describeHistory(enabled, months) {
  const kept = months === 0 ? "every month is kept" : `${months} month file(s) kept`;
  return `${enabled ? "recording on" : "recording off"} · ${kept}`;
}

// ---- the tab ---------------------------------------------------------------

export function createSettings(hub) {
  const { ctx, state, settingsTab } = hub;

  function renderSettingsTab() {
    // The key is stored SERVER-side in your user tier (settings.json) and
    // never echoed back — it must never live in a node widget, because
    // widget values persist into workflow PNGs.
    const keyInput = el("input", {
      type: "password",
      placeholder: state.civitaiKeySet
        ? "•••••••• (key stored — enter a new one to replace, empty to keep)"
        : "Civitai API key (optional — unlocks restricted models)",
      autocomplete: "off",
    });
    const status = el("span", { class: "mrln-note" });
    // The URL inputs are prefilled from GET /settings. If that GET failed they
    // are empty for a reason that has nothing to do with what is stored, so
    // Validate must NOT persist them — an empty string reverts the stored URL
    // to the default server-side, silently discarding a custom endpoint.
    let settingsLoaded = false;
    // Mirror of llm.allow_remote. Read by the backend rows (a red line caused
    // by the gate has to say so) and written only by a successful save.
    let allowRemote = false;
    // State rides a CLASS now, not an inline colour: the status line in the
    // new layout carries a dot whose colour IS the state, and an inline
    // colour on the text would leave the dot saying something else.
    const paintStatus = (node, text, tone) => {
      node.textContent = text;
      node.style.color = "";
      node.classList.toggle("pc-bad", tone === "bad");
      node.classList.toggle("pc-ok", tone === "ok");
    };
    const bad = (node, text) => paintStatus(node, text, "bad");
    const good = (node, text) => paintStatus(node, text, "ok");
    const plain = (node, text) => paintStatus(node, text, "");
    const backendRow = (label, key, provider) => {
      const urlInput = el("input", {
        type: "text",
        placeholder: `${label} URL`,
        title: `${label} endpoint used by the Prompt Enhance (MRLN) node`,
      });
      const rowStatus = el("span", { class: "mrln-note pc-set-status" }, "checking…");
      // Off means OFF: not probed on open, not probed by Validate, and refused
      // by the node with a message naming this switch. A backend that still
      // answers when it is switched off would make the switch a lie.
      const enabled = el("input", {
        type: "checkbox",
        checked: "",
        title: `Use ${label}. Off: never contacted — no availability check, and `
          + "the Enhance node refuses it instead of waiting for a timeout.",
      });
      // `force` re-probes without saving: flipping the remote gate changes the
      // ANSWER for an unchanged URL, and the cached probe (30 s TTL) would
      // otherwise keep showing the refusal the user just fixed.
      const check = async (persist = false, force = persist) => {
        if (persist && !settingsLoaded) {
          bad(rowStatus, "stored settings could not be read — not saving");
          ctx.toast(
            "error",
            "Settings unavailable",
            "The stored settings never loaded, so Validate will not overwrite them "
              + "with an empty URL. Reopen this tab once the server answers."
          );
          return;
        }
        if (!enabled.checked) {
          plain(rowStatus, "switched off — not contacted");
          rowStatus.title = "";
          return;
        }
        plain(rowStatus, "…");
        try {
          if (persist) {
            await ctx.apiJson("/mrln/prompt/save-settings", {
              method: "POST",
              body: { llm: { [key]: urlInput.value } },
            });
          }
          // cached probe on tab entry (this used to re-ping on every open,
          // each one blocking a server executor thread up to 5 s when the
          // backend is down); an explicit Validate ignores the TTL
          const entry = force
            ? await ctx.api.refreshLlmModels(provider)
            : await ctx.api.llmModels(provider);
          if (entry.error) throw new Error(entry.error);
          good(
            rowStatus,
            `${entry.models.length} models · ${entry.models
              .slice(0, 3)
              .join(", ")}${entry.models.length > 3 ? ", …" : ""}`
          );
          rowStatus.title = entry.models.join("\n");
        } catch (err) {
          // the gate's refusal is the one error that names a control — keep
          // the remediation instead of dropping it on the floor
          const hint = backendFailureHint(err.message, err.remediation, allowRemote);
          bad(rowStatus, `${err.message}${hint}`);
          rowStatus.title = "";
        }
      };
      const validateBtn = el("button", {
        class: "mrln-btn",
        onclick: (e) => busy(e.currentTarget, () => check(true)),
      });
      validateBtn.textContent = "Validate";
      const paintEnabled = () => {
        validateBtn.disabled = !enabled.checked;
        urlInput.disabled = !enabled.checked;
        row.classList.toggle("mrln-off", !enabled.checked);
      };
      enabled.addEventListener("change", async () => {
        if (!settingsLoaded) {
          enabled.checked = !enabled.checked; // never save over settings we never read
          ctx.toast(
            "error",
            "Settings unavailable",
            "The stored settings never loaded, so this switch will not overwrite them."
          );
          return;
        }
        paintEnabled();
        try {
          await ctx.apiJson("/mrln/prompt/save-settings", {
            method: "POST",
            body: { llm: { [`${provider}_enabled`]: enabled.checked } },
          });
        } catch (err) {
          enabled.checked = !enabled.checked;
          paintEnabled();
          ctx.toast("error", "Settings save failed", err.message);
          return;
        }
        // force: the ANSWER changed for an unchanged URL, exactly like the
        // remote gate — the cached probe would keep showing the old verdict
        check(false, true);
      });
      // The row has to SAY which backend it is. It used to rely on the URL's
      // port and on a placeholder you only see while the field is empty —
      // which, once there were two rows and a checkbox each, told you nothing
      // about what the checkbox switches off.
      // The mock draws this as url + Validate and nothing else. The name and
      // the use-switch stay: the row telling you WHICH backend it is was a
      // deliberate fix (the port was the only clue, and it told you nothing),
      // and the switch is a real server flag — a backend you do not run must
      // be contactable-never, not just wrong.
      const row = el(
        "label",
        { class: "pc-set-backend" },
        enabled,
        el("span", { class: "mrln-backend-name" }, label),
        urlInput,
        validateBtn
      );
      return { row, rowStatus, urlInput, enabled, paintEnabled, check };
    };
    const ollama = backendRow("Ollama", "ollama_url", "ollama");
    const lmstudio = backendRow("LM Studio", "lmstudio_url", "lmstudio");
    // ---- the remote-backend gate (llm.allow_remote) ------------------------
    // Off by default: the SERVER fetches these URLs, so an address anywhere but
    // this machine turns "my endpoint" into "probe that host for me". Both
    // gates live in one server helper used at save time AND at every fetch
    // site, which is why a URL stored before the gate existed is still echoed
    // back here (the user has to SEE it to fix it) while being refused in use.
    const remoteChip = el("span", { class: "mrln-chip" }, "checking…");
    const remoteBtn = el("button", { class: "mrln-btn mrln-mini", disabled: "" }, "Allow…");
    const setAllowRemote = async (next) => {
      const payload = allowRemotePayload({ settingsLoaded, next });
      if (!payload.ok) {
        ctx.toast("error", "Remote backends unchanged", payload.error);
        return;
      }
      try {
        await ctx.apiJson("/mrln/prompt/save-settings", { method: "POST", body: payload.body });
      } catch (err) {
        ctx.toast("error", "Settings save failed", err.message);
        return;
      }
      allowRemote = next;
      renderRemote();
      ctx.toast(
        next ? "warn" : "success",
        next ? "Remote LLM backends allowed" : "LLM backends restricted to this machine",
        next
          ? "ComfyUI may now fetch the Ollama / LM Studio URL you store, wherever it "
            + "points — click Validate to save and test one. Turn this back off when "
            + "you no longer need it."
          : "A stored URL on another machine is kept so you can see and fix it, but "
            + "is refused every time it is used."
      );
      // the gate is enforced at every fetch site, so both rows can change
      // verdict without either URL changing
      ollama.check(false, true);
      lmstudio.check(false, true);
    };
    const renderRemote = () => {
      remoteBtn.disabled = !settingsLoaded;
      if (!settingsLoaded) {
        remoteChip.className = "mrln-chip";
        remoteChip.textContent = "unknown";
        remoteBtn.textContent = "Allow…";
        remoteBtn.title = "the stored settings could not be read — nothing can be changed here";
        return;
      }
      remoteChip.className = `mrln-chip ${allowRemote ? "mrln-gate-open" : "mrln-gate-closed"}`;
      remoteChip.textContent = allowRemote ? "remote allowed" : "loopback only";
      remoteBtn.textContent = allowRemote ? "Restrict…" : "Allow…";
      remoteBtn.title = allowRemote
        ? "go back to the default: only localhost / 127.0.0.1 / ::1 may be fetched"
        : "allow LLM backends on other machines — only enable on trusted networks";
    };
    remoteBtn.addEventListener("click", (e) => {
      const button = e.currentTarget;
      // Turning it OFF only ever narrows what the server may reach, so it is a
      // single click. Turning it ON widens it — that one arms first (the panel
      // never calls window.confirm: it throws on the Electron frontend).
      if (allowRemote) return busy(button, () => setAllowRemote(false));
      return armDestructive(button, "Really allow remote?", () =>
        busy(button, () => setAllowRemote(true))
      );
    });
    // ---- render history retention -----------------------------------------
    const historyEnabled = el("input", {
      type: "checkbox",
      title: "whether NEW renders are appended to the history — it never deletes anything",
    });
    const monthsInput = el("input", {
      type: "number",
      min: "0",
      step: "1",
      class: "mrln-months",
      title: "how many month files to keep; 0 keeps everything. Applied when ComfyUI starts.",
    });
    const historyThumbs = el("input", {
      type: "checkbox",
      title:
        "show each row's render as a mini thumbnail. The image is found "
        + "automatically — ComfyUI writes the template and seed into every PNG "
        + "it saves, which is the same pair the history line records, so "
        + "nothing needs wiring. Rows whose image is gone simply show none.",
    });
    const historyStatus = el("span", { class: "mrln-note pc-set-status" }, "checking…");
    const historySave = el("button", { class: "mrln-btn", disabled: "" }, "Save");
    const applyHistory = (body) => {
      // the server echoes both values back on GET and on save — render what it
      // stored, never what we hoped it stored
      historyEnabled.checked = body.history_enabled !== false;
      historyThumbs.checked = body.history_thumbs !== false;
      monthsInput.value = String(body.history_months ?? "");
      plain(
        historyStatus,
        describeHistory(historyEnabled.checked, Number(monthsInput.value))
      );
    };
    const saveHistory = async () => {
      const payload = historySavePayload({
        settingsLoaded,
        enabled: historyEnabled.checked,
        months: monthsInput.value,
        thumbs: historyThumbs.checked,
      });
      if (!payload.ok) {
        bad(historyStatus, payload.error);
        ctx.toast("error", "History settings not saved", payload.error);
        return;
      }
      try {
        const body = await ctx.apiJson("/mrln/prompt/save-settings", {
          method: "POST",
          body: payload.body,
        });
        applyHistory(body);
        ctx.toast(
          "success",
          "History settings saved",
          describeHistory(body.history_enabled !== false, Number(body.history_months ?? 0))
        );
      } catch (err) {
        bad(historyStatus, err.message);
        ctx.toast("error", "History settings not saved", err.message);
      }
    };
    historySave.addEventListener("click", (e) => busy(e.currentTarget, saveHistory));
    // Cloud keys: stored server-side (user tier settings.json), NEVER echoed
    // back — the response only says whether one exists (green check).
    const cloudRow = (label, provider) => {
      const input = el("input", {
        type: "password",
        autocomplete: "off",
        placeholder: `${label} API key`,
      });
      // A chip, not a sentence: four of these stack, and "whether a key is
      // stored" is a state with two values — the same vocabulary the rest of
      // the panel uses for a state.
      const chip = el("span", { class: "mrln-chip" }, "no key");
      const clearBtn = el(
        "button",
        {
          // one click away from Save, and a cleared key can only be recovered
          // from the provider's dashboard — arm it like every other
          // irreversible action in the panel
          class: "mrln-btn mrln-mini",
          onclick: (e) => armDestructive(e.currentTarget, "Really clear?", () => push("")),
        },
        "Clear"
      );
      const setMark = (isSet) => {
        chip.textContent = isSet ? "saved" : "no key";
        chip.classList.toggle("mrln-user", !!isSet); // the panel's "yours" green
        // Clear only exists when there IS something to clear. It used to sit
        // beside Save on every row, one slip away from a trip to a provider
        // dashboard, including on the rows that held nothing at all.
        clearBtn.style.display = isSet ? "" : "none";
      };
      setMark(false);
      const push = async (value) => {
        try {
          const body = await ctx.apiJson("/mrln/prompt/save-settings", {
            method: "POST",
            body: { llm_api_keys: { [provider]: value } },
          });
          input.value = "";
          setMark(body.llm_keys_set?.[provider]);
          ctx.api.refreshLlmKeys(); // key_set flipped — the node's dropdown must follow
          ctx.api.refreshLlmModels(provider); // and so must the De-compose model note
          ctx.toast("success", "Settings saved", `${label} key ${value ? "stored" : "cleared"}`);
        } catch (err) {
          ctx.toast("error", "Settings save failed", err.message);
        }
      };
      const row = el(
        "div",
        { class: "pc-set-key" },
        el("span", { class: "mrln-cloud-label" }, label),
        input,
        chip,
        el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            onclick: (e) =>
              busy(e.currentTarget, async () => {
                if (input.value.trim()) await push(input.value.trim());
              }),
          },
          "Save"
        ),
        clearBtn
      );
      return { row, setMark };
    };
    const clouds = [
      ["anthropic", cloudRow("Anthropic", "anthropic")],
      ["openai", cloudRow("OpenAI", "openai")],
      ["gemini", cloudRow("Gemini", "gemini")],
      ["openrouter", cloudRow("OpenRouter", "openrouter")],
    ];
    const refresh = async () => {
      try {
        const body = await ctx.apiJson("/mrln/prompt/settings");
        state.civitaiKeySet = body.civitai_key_set;
        status.textContent = body.civitai_key_set
          ? "key stored (server-side, user tier)"
          : "no key stored — public models still resolve by hash";
        ollama.urlInput.value = body.llm?.ollama_url ?? "";
        lmstudio.urlInput.value = body.llm?.lmstudio_url ?? "";
        ollama.enabled.checked = body.llm?.ollama_enabled !== false;
        lmstudio.enabled.checked = body.llm?.lmstudio_enabled !== false;
        ollama.paintEnabled();
        lmstudio.paintEnabled();
        allowRemote = body.llm?.allow_remote === true;
        for (const [provider, cloud] of clouds) cloud.setMark(body.llm_keys_set?.[provider]);
        settingsLoaded = true;
        applyHistory(body);
      } catch (err) {
        settingsLoaded = false;
        status.textContent = "settings unavailable — nothing shown here is what is stored";
        bad(historyStatus, "unavailable");
        ctx.toast("error", "Cannot read Composer settings", err.message);
      }
      // the two controls that must never save a value they did not read
      historySave.disabled = !settingsLoaded;
      renderRemote();
      // auto-check the local backends — green marks without a click
      ollama.check();
      lmstudio.check();
    };
    refresh();
    const save = async (clear) => {
      try {
        const body = await ctx.apiJson("/mrln/prompt/save-settings", {
          method: "POST",
          body: { civitai_api_key: clear ? "" : keyInput.value },
        });
        state.civitaiKeySet = body.civitai_key_set;
        keyInput.value = "";
        ctx.toast(
          "success",
          "Composer settings saved",
          body.civitai_key_set ? "Civitai key stored" : "Civitai key cleared"
        );
        refresh();
      } catch (err) {
        ctx.toast("error", "Settings save failed", err.message);
      }
    };
    const civitaiSave = el(
      "button",
      {
        class: "mrln-btn mrln-mini",
        onclick: (e) =>
          busy(e.currentTarget, async () => {
            if (keyInput.value.trim()) await save(false);
          }),
      },
      "Save key"
    );
    const civitaiClear = el(
      "button",
      {
        // never echoed back, so a misclick next to 'Save key' costs a trip to
        // the Civitai dashboard — armed like every other irreversible action
        class: "mrln-btn mrln-mini",
        onclick: (e) => armDestructive(e.currentTarget, "Really clear?", () => save(true)),
      },
      "Clear"
    );

    // ---- the layout --------------------------------------------------------
    // One row per setting: what it is on the left, the controls on the right,
    // and the long "why" behind a toggle instead of between the inputs.
    const setRow = (title, blurb, controls, prose) => {
      const body = el("div", { class: "pc-set-row" });
      const label = el("div", { class: "pc-set-label" }, el("b", {}, title));
      const note = el("div", { class: "mrln-note" }, blurb ? `${blurb} ` : "");
      let open = false;
      if (prose) {
        const panel = el("div", { class: "mrln-note pc-set-prose" }, prose);
        panel.style.display = "none";
        const toggle = el(
          "button",
          {
            class: "pc-set-more",
            onclick: () => {
              open = !open;
              panel.style.display = open ? "" : "none";
              toggle.textContent = open ? "Less" : "What this does";
            },
          },
          "What this does"
        );
        note.append(toggle);
        label.append(note);
        body.append(label, controls, panel);
      } else {
        if (blurb) label.append(note);
        body.append(label, controls);
      }
      return body;
    };
    const box = (...rows) => el("div", { class: "pc-set-box" }, ...rows.flat().filter(Boolean));

    // No group strip. It was drawn in the mock beside a page that showed every
    // group anyway, so once all three stayed on one page it had nothing left
    // to do: as a filter it hid two thirds of a short tab, and as a jump it
    // scrolled to something already on screen. A control that cannot change
    // what you see is worse than no control.

    const groups = {
      llm: () => [
        setRow(
          "Local LLM backends",
          "Checked on open.",
          box(
            ollama.row,
            ollama.rowStatus,
            lmstudio.row,
            lmstudio.rowStatus,
            el("div", { class: "pc-set-gate" }, remoteChip, el("span", { class: "mrln-note" },
              "safe default — remote URLs stay off until you allow them"), remoteBtn)
          ),
          "Used by the Prompt Enhance (MRLN) node — checked automatically on open; "
            + "Validate saves an edited URL and re-checks. The model list feeds the "
            + "node's dropdown. Clear the checkbox for a backend you do not run: it is "
            + "then never contacted — no check on open, no wait for a timeout — and the "
            + "node refuses it by name instead.\n\n"
            + "Remote backends are off by default, and the default is the safe one: "
            + "ComfyUI itself makes the request to the URL above, so only this machine "
            + "(localhost, 127.0.0.1, ::1) is fetched — a URL pointing anywhere else "
            + "would turn this box into a probe for whatever address is in that field. "
            + "Turn it on only for an Ollama / LM Studio you run yourself on a network "
            + "you trust; it covers both URLs above, stays on until you turn it off, and "
            + "is re-checked every single time a backend is used. Turning it back off "
            + "leaves a stored remote URL visible above — on purpose, so you can see and "
            + "fix it — and refuses it from then on."
        ),
      ],
      keys: () => [
        setRow(
          "Civitai",
          "Trigger words and AIR tags by file hash.",
          box(el("div", { class: "pc-set-key" }, keyInput, civitaiSave, civitaiClear), status),
          "Used by LoRA blocks to look up trigger words + AIR tags by file hash. The key "
            + "is stored server-side in your user tier and never echoed back."
        ),
        setRow(
          "Cloud API keys",
          "Stored server-side, never echoed back.",
          box(clouds.map(([, cloud]) => cloud.row)),
          "Unlock the cloud backends of Prompt Enhance and the LLM de-composer. Keys are "
            + "stored server-side in your user tier, never echoed back and never in a node "
            + "widget (widget values persist into workflow PNGs)."
        ),
      ],
      history: () => [
        setRow(
          "Render history",
          "Recording governs what is written, not what is kept.",
          box(
            el(
              "div",
              { class: "pc-set-history" },
              el("label", { class: "mrln-check" }, historyEnabled, el("span", {}, "Record renders")),
              el(
                "label",
                { class: "mrln-check" },
                el("span", {}, "keep"),
                monthsInput,
                el("span", {}, "months")
              ),
              el("label", { class: "mrln-check" }, historyThumbs, el("span", {}, "Show thumbnails")),
              historySave,
              historyStatus
            )
          ),
          "The Prompt Template node appends one line per render to a month file in your "
            + "user library; the History tab reads them back.\n\n"
            + "Recording governs what is WRITTEN, not what is kept: switching it off stops "
            + "new lines and deletes nothing — the old records stay until they age out, or "
            + "until you use Clear history in the History tab. Retention is applied when "
            + "ComfyUI starts, keeps the newest N month files regardless of the switch "
            + "above, and 0 keeps everything forever.\n\n"
            + "Show thumbnails puts each row's render beside it. The image is found "
            + "automatically — ComfyUI writes the template and seed into every PNG it "
            + "saves, the same pair the history line records — so nothing needs wiring."
        ),
      ],
    };

    const paint = () => {
      mount(
        settingsTab,
        el(
          "div",
          { class: "pc-set-head" },
          el("span", { class: "pc-set-title" }, "Settings")
        ),
        ...groups.llm(),
        ...groups.keys(),
        ...groups.history()
      );
    };
    paint();
  }

  return { renderSettingsTab };
}
