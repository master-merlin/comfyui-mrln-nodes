// MRLN Prompt Composer — extension entry. Registers the sidebar tab on
// frontends that support it and no-ops (with a console note) everywhere
// else; the nodes themselves never depend on this file.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createComposerPanel } from "./prompt_composer_panel.js";

const cssUrl = new URL("./prompt_composer.css", import.meta.url).href;
if (!document.querySelector(`link[href="${cssUrl}"]`)) {
  document.head.appendChild(
    Object.assign(document.createElement("link"), { rel: "stylesheet", href: cssUrl })
  );
}

async function apiJson(route, options = {}) {
  const started = performance.now();
  const resp = await api.fetchApi(route, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const ms = performance.now() - started;
  if (ms > 400) {
    // surfaces WHERE first-open time goes (busy boot loop, AV first-touch…)
    console.debug(`[MRLN] slow request ${route.split("?")[0]} took ${Math.round(ms)}ms`);
  }
  let data = null;
  try {
    data = await resp.json();
  } catch {
    /* non-JSON error page */
  }
  if (!resp.ok) {
    const err = new Error(data?.error ?? `HTTP ${resp.status}`);
    err.remediation = data?.remediation;
    throw err;
  }
  return data;
}

function toast(severity, summary, detail = "") {
  const mgr = app.extensionManager;
  if (mgr?.toast?.add) mgr.toast.add({ severity, summary, detail, life: 4000 });
  else console.log(`[MRLN ${severity}] ${summary} ${detail}`);
}

function selectedTemplateNode() {
  const isTemplate = (n) => (n.comfyClass ?? n.type) === "MRLN_PromptTemplate";
  // Convenience rule: with exactly ONE Prompt Template node in the graph
  // there is nothing to choose — Apply/Load target it automatically. Only
  // with several nodes does an explicit selection become necessary.
  const all = (app.graph?._nodes ?? app.graph?.nodes ?? []).filter(isTemplate);
  if (all.length === 1) return all[0];
  return explicitlySelectedTemplateNode();
}

function explicitlySelectedTemplateNode() {
  const items = app.canvas?.selectedItems
    ? [...app.canvas.selectedItems] // new frontend: Set of nodes/groups
    : Object.values(app.canvas?.selected_nodes ?? {}); // legacy fallback
  return items.find((n) => (n.comfyClass ?? n.type) === "MRLN_PromptTemplate") ?? null;
}

function setWidget(node, name, value) {
  const widget = (node.widgets ?? []).find((w) => w.name === name);
  if (!widget) return false;
  widget.value = value;
  widget.callback?.(value);
  return true;
}

function getWidget(node, name) {
  return (node.widgets ?? []).find((w) => w.name === name)?.value;
}

// ---- Prompt Enhance: model dropdown (progressive enhancement) --------------
// Server-side the model widget stays a plain STRING, so the node works
// headless and via the API. In the browser it becomes a dropdown listing
// the backend's installed models plus curated "⬇ pull" suggestions that
// Ollama downloads in the background when picked.
const PULL_PREFIX = "⬇ pull ";
const CLOUD_BACKENDS = ["anthropic", "openai", "gemini", "openrouter"];
const llmModels = {}; // provider -> {models, suggested, fetchedAt, error}
let llmKeysSet = { fetchedAt: 0, keys: {} }; // which cloud backends hold a key

async function refreshLlmModels(provider) {
  try {
    const body = await apiJson(`/mrln/prompt/llm-validate?provider=${provider}`);
    llmModels[provider] = {
      models: body.models ?? [],
      suggested: body.suggested ?? [],
      fetchedAt: Date.now(),
      error: null,
    };
  } catch (err) {
    llmModels[provider] = {
      models: [],
      suggested: llmModels[provider]?.suggested ?? [],
      fetchedAt: Date.now(),
      error: err.message,
    };
  }
  return llmModels[provider];
}

async function refreshLlmKeys() {
  try {
    const body = await apiJson("/mrln/prompt/settings");
    llmKeysSet = { fetchedAt: Date.now(), keys: body.llm_keys_set ?? {} };
  } catch {
    llmKeysSet = { fetchedAt: Date.now(), keys: llmKeysSet.keys };
  }
  return llmKeysSet;
}

function backendValue(node) {
  return (node.widgets ?? []).find((w) => w.name === "backend")?.value ?? "ollama";
}

function enhanceProvider(node) {
  const backend = backendValue(node);
  if (backend === "lm studio") return "lmstudio";
  if (backend === "ollama") return "ollama";
  return backend; // cloud backends carry their own name
}

function watchPull(provider, model) {
  const started = Date.now();
  const poll = async () => {
    if (Date.now() - started > 45 * 60 * 1000) return; // stop polling silently
    let body = null;
    try {
      body = await apiJson(`/mrln/prompt/llm-pull?model=${encodeURIComponent(model)}`);
    } catch {
      /* transient — keep polling */
    }
    if (body?.status === "done") {
      toast("success", "Model pulled", `${model} is installed — the next Enhance run uses it`);
      refreshLlmModels(provider);
      return;
    }
    if (body?.status === "error") {
      toast("error", `Pull failed: ${model}`, body.detail ?? "");
      return;
    }
    setTimeout(poll, 4000);
  };
  setTimeout(poll, 4000);
}

function enhanceModelDropdown(node) {
  const widget = (node.widgets ?? []).find((w) => w.name === "model");
  if (!widget || widget.type === "combo") return;
  const isCloud = () => CLOUD_BACKENDS.includes(backendValue(node));
  const syncModelType = () => {
    // clouds have no listing endpoint here — keep the model free-typed;
    // locals get the installed-models dropdown (type is read at draw time)
    widget.type = isCloud() ? "text" : "combo";
  };
  widget.options = widget.options ?? {};
  widget.options.values = () => {
    const provider = enhanceProvider(node);
    const entry = llmModels[provider];
    // sync return from cache; kick an async refresh when stale so the NEXT
    // open is current (litegraph needs the list immediately)
    if (!entry || Date.now() - entry.fetchedAt > 30000) refreshLlmModels(provider);
    const values = [...(entry?.models ?? [])];
    const current = String(widget.value ?? "").trim();
    if (current && !values.includes(current)) values.unshift(current);
    if (provider === "ollama") {
      values.push(...(entry?.suggested ?? []).map((m) => `${PULL_PREFIX}${m}`));
    }
    return values.length ? values : [current || ""];
  };
  const prevCallback = widget.callback;
  widget.callback = function (value, ...rest) {
    if (typeof value === "string" && value.startsWith(PULL_PREFIX)) {
      const model = value.slice(PULL_PREFIX.length);
      widget.value = model; // widget is set now; the pull lands in the background
      const provider = enhanceProvider(node);
      apiJson("/mrln/prompt/llm-pull", { method: "POST", body: { model, start: true } })
        .then(() => {
          toast("info", "Pulling model", `${model} — Ollama downloads it in the background`);
          watchPull(provider, model);
        })
        .catch((err) => toast("error", "Pull failed to start", err.message));
      return prevCallback?.call(this, model, ...rest);
    }
    return prevCallback?.call(this, value, ...rest);
  };
  const backendWidget = (node.widgets ?? []).find((w) => w.name === "backend");
  if (backendWidget) {
    // only keyed cloud backends are offered (locals always); the node's
    // current value stays listed so foreign workflows load intact
    backendWidget.options = backendWidget.options ?? {};
    backendWidget.options.values = () => {
      if (Date.now() - llmKeysSet.fetchedAt > 30000) refreshLlmKeys();
      const values = ["ollama", "lm studio"];
      for (const cloud of CLOUD_BACKENDS) {
        if (llmKeysSet.keys[cloud]) values.push(cloud);
      }
      const current = String(backendWidget.value ?? "");
      if (current && !values.includes(current)) values.push(current);
      return values;
    };
    const backendCallback = backendWidget.callback;
    backendWidget.callback = function (...args) {
      const result = backendCallback?.apply(this, args);
      syncModelType();
      if (!isCloud()) refreshLlmModels(enhanceProvider(node));
      return result;
    };
  }
  syncModelType();
  refreshLlmKeys();
  if (!isCloud()) refreshLlmModels(enhanceProvider(node));
}

app.registerExtension({
  name: "mrln.promptComposer",
  nodeCreated(node) {
    // the extension-level hook fires for every constructed node with its
    // widgets already built — prototype onNodeCreated is NOT reliably
    // invoked across frontend versions
    if ((node.comfyClass ?? node.type) === "MRLN_PromptEnhance") {
      enhanceModelDropdown(node);
    }
  },
  setup() {
    if (!app.extensionManager?.registerSidebarTab) {
      console.log(
        "[MRLN] sidebar API unavailable — Prompt Composer disabled " +
          "(the MRLN nodes themselves remain fully functional)."
      );
      return;
    }
    // The panel is a SINGLETON detached from the sidebar's render cycle.
    // The frontend may re-invoke render() (tab switches, and on some
    // versions during workflow execution) — rebuilding the panel each time
    // closes every open dropdown, steals focus and discards edits. Instead
    // the panel DOM is built once and re-attached, keeping all state.
    let panelRoot = null;
    app.extensionManager.registerSidebarTab({
      id: "mrln-prompt-composer",
      icon: "pi pi-book",
      title: "Prompt Composer",
      tooltip: "MRLN Prompt Composer — browse, compose, preview and edit prompt libraries",
      type: "custom",
      render: (el) => {
        if (panelRoot) {
          if (!el.contains(panelRoot)) el.appendChild(panelRoot);
          return;
        }
        panelRoot = document.createElement("div");
        panelRoot.style.height = "100%";
        el.appendChild(panelRoot);
        createComposerPanel(panelRoot, {
          apiJson,
          toast,
          selectedTemplateNode,
          setWidget,
          getWidget,
          markDirty: () => {
            // change() notifies the frontend's change tracker — without it,
            // programmatic widget writes render live but never reach the
            // serialized workflow, so a reload reverts them (setDirtyCanvas
            // alone is only a redraw flag)
            app.graph?.change?.();
            app.graph?.setDirtyCanvas(true, true);
          },
          refreshCombos: () => app.refreshComboInNodes?.(),
          dialog: app.extensionManager?.dialog,
        });
      },
    });
  },
});
