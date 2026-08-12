// MRLN Prompt Composer — the Settings tab: the Civitai key, the two local LLM
// backend URLs and the cloud API keys. Every secret is stored SERVER-side in
// the user tier and never echoed back — this tab only ever shows whether one
// exists, and never puts a key into a node widget (widget values persist into
// workflow PNGs).
//
// HARD RULE for this file: ZERO top-level side effects (ComfyUI auto-imports
// every .js under WEB_DIRECTORY — see composer/util.js).
import { armDestructive, busy, el } from "./dom.js";

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
    const backendRow = (label, key, provider) => {
      const urlInput = el("input", {
        type: "text",
        placeholder: `${label} URL`,
        title: `${label} endpoint used by the Prompt Enhance (MRLN) node`,
      });
      const rowStatus = el("span", { class: "mrln-note" }, "checking…");
      const check = async (persist = false) => {
        if (persist && !settingsLoaded) {
          rowStatus.textContent = "✗ stored settings could not be read — not saving";
          rowStatus.style.color = "#e88";
          ctx.toast(
            "error",
            "Settings unavailable",
            "The stored settings never loaded, so Validate will not overwrite them "
              + "with an empty URL. Reopen this tab once the server answers."
          );
          return;
        }
        rowStatus.textContent = "…";
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
          const entry = persist
            ? await ctx.api.refreshLlmModels(provider)
            : await ctx.api.llmModels(provider);
          if (entry.error) throw new Error(entry.error);
          rowStatus.textContent = `✓ ${entry.models.length} model(s): ${entry.models
            .slice(0, 3)
            .join(", ")}${entry.models.length > 3 ? ", …" : ""}`;
          rowStatus.style.color = "#6ca";
          rowStatus.title = entry.models.join("\n");
        } catch (err) {
          rowStatus.textContent = `✗ ${err.message}`;
          rowStatus.style.color = "#e88";
          rowStatus.title = "";
        }
      };
      const row = el(
        "div",
        { class: "mrln-inline" },
        urlInput,
        el(
          "button",
          { class: "mrln-btn", onclick: (e) => busy(e.currentTarget, () => check(true)) },
          "Validate"
        )
      );
      return { row, rowStatus, urlInput, check };
    };
    const ollama = backendRow("Ollama", "ollama_url", "ollama");
    const lmstudio = backendRow("LM Studio", "lmstudio_url", "lmstudio");
    // Cloud keys: stored server-side (user tier settings.json), NEVER echoed
    // back — the response only says whether one exists (green check).
    const cloudRow = (label, provider) => {
      const input = el("input", {
        type: "password",
        autocomplete: "off",
        placeholder: `${label} API key`,
      });
      const mark = el("span", { class: "mrln-note" }, "");
      const setMark = (isSet) => {
        mark.textContent = isSet ? "✓ key stored" : "no key";
        mark.style.color = isSet ? "#6ca" : "";
      };
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
        { class: "mrln-inline" },
        el("span", { class: "mrln-cloud-label" }, label),
        input,
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: (e) =>
              busy(e.currentTarget, async () => {
                if (input.value.trim()) await push(input.value.trim());
              }),
          },
          "Save"
        ),
        el(
          "button",
          {
            // one click away from Save, and a cleared key can only be recovered
            // from the provider's dashboard — arm it like every other
            // irreversible action in the panel
            class: "mrln-btn",
            onclick: (e) => armDestructive(e.currentTarget, "Really clear?", () => push("")),
          },
          "Clear"
        ),
        mark
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
        for (const [provider, cloud] of clouds) cloud.setMark(body.llm_keys_set?.[provider]);
        settingsLoaded = true;
      } catch (err) {
        settingsLoaded = false;
        status.textContent = "settings unavailable — nothing shown here is what is stored";
        ctx.toast("error", "Cannot read Composer settings", err.message);
      }
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
    settingsTab.replaceChildren(
      el("div", { class: "mrln-tree-head" }, "Civitai"),
      el(
        "div",
        { class: "mrln-note" },
        "Used by LoRA blocks to look up trigger words + AIR tags by file hash. "
          + "The key is stored server-side in your user tier and never echoed back."
      ),
      el(
        "div",
        { class: "mrln-inline" },
        keyInput,
        el(
          "button",
          {
            class: "mrln-btn",
            onclick: (e) =>
              busy(e.currentTarget, async () => {
                if (keyInput.value.trim()) await save(false);
              }),
          },
          "Save key"
        ),
        el(
          "button",
          {
            // same reasoning as the cloud keys: never echoed back, so a
            // misclick next to 'Save key' costs a trip to the dashboard
            class: "mrln-btn",
            onclick: (e) => armDestructive(e.currentTarget, "Really clear?", () => save(true)),
          },
          "Clear"
        )
      ),
      status,
      el("hr", { class: "mrln-sep" }),
      el("div", { class: "mrln-tree-head" }, "Local LLM backends"),
      el(
        "div",
        { class: "mrln-note" },
        "Used by the Prompt Enhance (MRLN) node — checked automatically on "
          + "open; Validate saves an edited URL and re-checks. The model list "
          + "feeds the node's dropdown."
      ),
      ollama.row,
      ollama.rowStatus,
      lmstudio.row,
      lmstudio.rowStatus,
      el("hr", { class: "mrln-sep" }),
      el("div", { class: "mrln-tree-head" }, "Cloud LLM API keys"),
      el(
        "div",
        { class: "mrln-note" },
        "Unlock the cloud backends of Prompt Enhance and the LLM de-composer. "
          + "Keys are stored server-side in your user tier, never echoed back "
          + "and never in a node widget (widgets persist into workflow PNGs)."
      ),
      ...clouds.flatMap(([, cloud]) => [cloud.row])
    );
  }

  return { renderSettingsTab };
}
