// MRLN Prompt Composer — extension entry. Registers the sidebar tab on
// frontends that support it and no-ops (with a console note) everywhere
// else; the nodes themselves never depend on this file.
//
// This is the ONLY file in the composer allowed top-level side effects (CSS
// link, registerExtension, api instance). Everything it hands to the panel
// travels in the `ctx` object at the bottom — that object is a contract:
// additive changes only, never a rename or a removal.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createComposerPanel } from "./prompt_composer_panel.js";
import {
  CUSTOM_ENTRY,
  LIBRARY_ROUTE,
  PULL_PREFIX,
  buildModelValues,
  createApi,
  isNoteEntry,
  isSentinelEntry,
} from "./composer/api.js";

const cssUrl = new URL("./prompt_composer.css", import.meta.url).href;
if (!document.querySelector(`link[href="${cssUrl}"]`)) {
  document.head.appendChild(
    Object.assign(document.createElement("link"), { rel: "stylesheet", href: cssUrl })
  );
}

// The fetch layer: JSON wrapper, library fingerprint cache, LLM model/key
// caches with in-flight dedup, one query encoder. Injected transport keeps
// composer/api.js free of ComfyUI imports (and unit-testable under node).
const mrln = createApi({ fetchApi: (route, init) => api.fetchApi(route, init) });
const { apiJson } = mrln;

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

function enhanceNodes() {
  const all = app.graph?._nodes ?? app.graph?.nodes ?? [];
  return all.filter((n) => (n.comfyClass ?? n.type) === "MRLN_PromptEnhance");
}

// ---- Prompt Enhance: model dropdown (progressive enhancement) --------------
// Server-side the model widget stays a plain STRING, so the node works
// headless and via the API. In the browser it becomes a dropdown listing
// the backend's installed models plus curated "⬇ pull" suggestions that
// Ollama downloads in the background when picked.
//
// Model lists, curated suggestions and key flags all come from the server
// through composer/api.js: llm-validate answers cloud providers offline with
// its own curated `suggested` list (llm.py CLOUD_MODEL_SUGGESTIONS, "edit
// freely"), so there is deliberately NO copy of that list here any more — the
// documented customization point is the server constant.

function backendValue(node) {
  return (node.widgets ?? []).find((w) => w.name === "backend")?.value ?? "ollama";
}

function enhanceProvider(node) {
  const backend = backendValue(node);
  if (backend === "lm studio") return "lmstudio";
  if (backend === "ollama") return "ollama";
  return backend; // cloud backends carry their own name
}

// model name -> being watched. ONE watcher per model for the life of the
// page: the server answers an already-running pull with 200 "already
// running", so without this every re-pick added another 4 s poller and
// another completion toast.
const activePulls = new Set();

function watchPull(provider, model) {
  if (activePulls.has(model)) return;
  activePulls.add(model);
  const started = Date.now();
  let unknown = 0;
  const stop = (severity, summary, detail = "") => {
    activePulls.delete(model);
    toast(severity, summary, detail);
  };
  const poll = async () => {
    if (Date.now() - started > 45 * 60 * 1000) {
      stop(
        "warn",
        `Stopped watching ${model}`,
        "45 minutes without a result — check `ollama list`, the download may still be running"
      );
      return;
    }
    let body = null;
    try {
      body = await mrln.pullStatus(model);
    } catch {
      /* transient — keep polling */
    }
    if (body?.status === "done") {
      stop("success", "Model pulled", `${model} is installed — the next Enhance run uses it`);
      mrln.refreshLlmModels(provider);
      return;
    }
    if (body?.status === "error") {
      stop("error", `Pull failed: ${model}`, body.detail ?? "");
      return;
    }
    // "unknown" = the server holds no state for this model. The POST claims
    // the status before it answers, so this cannot race a fresh pull — it
    // means the state was lost (ComfyUI restarted, or the bounded status map
    // evicted it). Three consecutive polls guard against a transient blip.
    unknown = body?.status === "unknown" ? unknown + 1 : 0;
    if (unknown >= 3) {
      stop(
        "warn",
        "Pull state lost",
        `${model} is no longer tracked (did ComfyUI restart?) — pick it again to resume`
      );
      return;
    }
    setTimeout(poll, 4000);
  };
  setTimeout(poll, 4000);
}

function installBackendCombo(node) {
  // Only keyed cloud backends are offered (locals always); the node's current
  // value stays listed so foreign workflows load intact. The cloud list itself
  // is the server's — the keys of llm_keys_set — not a hardcoded copy.
  //
  // Re-assertable ON PURPOSE: app.refreshComboInNodes() re-applies option
  // values from the fetched node definition, and on some frontend versions
  // that overwrites this values FUNCTION with the definition's static array
  // (unkeyed backends would reappear). Whether a given frontend does that is
  // only decidable live, so this is called from three places — after the
  // enhancement, after every refreshCombos, and on every model-menu open —
  // and is idempotent so it is correct under either behavior.
  const backendWidget = (node.widgets ?? []).find((w) => w.name === "backend");
  if (!backendWidget) return;
  backendWidget.options = backendWidget.options ?? {};
  backendWidget.options.values = () => {
    mrln.llmKeys(); // TTL + in-flight dedup live in the api layer
    const values = ["ollama", "lm studio", ...mrln.keyedCloudBackends()];
    const current = String(backendWidget.value ?? "");
    if (current && !values.includes(current)) values.push(current);
    return values;
  };
  if (!backendWidget.__mrlnPatched) {
    // wrap once: a re-assert must not stack N callbacks on one widget
    const inner = backendWidget.callback;
    backendWidget.callback = function (...args) {
      const result = inner?.apply(this, args);
      mrln.refreshLlmModels(enhanceProvider(node));
      return result;
    };
    backendWidget.__mrlnPatched = true;
  }
}

/**
 * Make `template_names` actually change the template combo.
 *
 * The combo's list is built SERVER-side in INPUT_TYPES from the library's
 * slugs. INPUT_TYPES is a classmethod — it knows nothing about one node's
 * widget values — so no reload, and not even 'Refresh node definitions', can
 * make the server hand out labels for one node and slugs for another. Swapping
 * the list is therefore a client job, and this is the progressive enhancement
 * over a server side that already accepts BOTH forms: without this file the
 * node still renders, it just always lists slugs.
 *
 * The slug list captured here is the identity list and is never thrown away —
 * labels are a display over it, and a label shared by two templates keeps its
 * slug, because a name that identifies two things identifies neither (the
 * server refuses such a label by name for the same reason).
 */
function enhanceTemplateNames(node) {
  const tpl = (node.widgets ?? []).find((w) => w.name === "template");
  const mode = (node.widgets ?? []).find((w) => w.name === "template_names");
  if (!tpl || !mode || mode.__mrlnWired) return;
  mode.__mrlnWired = true;
  const slugs = [...(tpl.options?.values ?? [])];
  const bySlug = new Map(); // slug -> what the combo shows
  const byShown = new Map(); // what the combo shows -> slug

  async function loadLabels() {
    if (bySlug.size) return;
    let body;
    try {
      body = await mrln.apiJson(LIBRARY_ROUTE);
    } catch {
      return; // no labels available: the list stays slugs, which always works
    }
    const labelOf = new Map();
    const uses = new Map();
    for (const entry of body.templates ?? []) {
      const label = String(entry.label ?? "").trim();
      if (!label) continue;
      labelOf.set(entry.slug, label);
      uses.set(label, (uses.get(label) ?? 0) + 1);
    }
    for (const slug of slugs) {
      const label = labelOf.get(slug);
      const shown = label && uses.get(label) === 1 ? label : slug;
      bySlug.set(slug, shown);
      byShown.set(shown, slug);
    }
  }

  async function apply() {
    const wantLabels = mode.value === "label";
    if (wantLabels) await loadLabels();
    const asSlug = byShown.get(tpl.value) ?? tpl.value;
    if (!tpl.options) tpl.options = {};
    tpl.options.values = wantLabels ? slugs.map((s) => bySlug.get(s) ?? s) : [...slugs];
    tpl.value = wantLabels ? (bySlug.get(asSlug) ?? asSlug) : asSlug;
    app.graph?.setDirtyCanvas(true, true);
  }

  const inner = mode.callback;
  mode.callback = function (...args) {
    const result = inner?.apply(this, args);
    apply();
    return result;
  };
  apply();
  // A workflow load assigns widget values AFTER the node is built and does not
  // fire callbacks, so a graph saved in label mode would come back with a label
  // in a list of slugs. Re-apply once the load has settled.
  setTimeout(apply, 0);
}

function enhanceModelDropdown(node) {
  // Mutating a text widget's `type` does NOT change its behavior — the
  // click handler is bound to the widget instance (it kept opening the
  // frontend's Value editor). Replace it with a REAL combo widget at the
  // exact same index: widgets_values are positional and workflow loads
  // assign them by index.
  const old = (node.widgets ?? []).find((w) => w.name === "model");
  if (!old || old.__mrlnCombo) {
    if (old?.__mrlnCombo) installBackendCombo(node); // already enhanced: re-assert only
    return;
  }
  const index = node.widgets.indexOf(old);
  let combo;
  let lastReal = String(old.value ?? "");
  const callback = (value) => {
    if (typeof value !== "string") return;
    if (value.startsWith(PULL_PREFIX)) {
      const model = value.slice(PULL_PREFIX.length);
      combo.value = model; // widget is set now; the pull lands in the background
      lastReal = model;
      const provider = enhanceProvider(node);
      mrln
        .startPull(model)
        .then((body) => {
          toast(
            "info",
            body?.detail === "already running" ? "Already downloading" : "Pulling model",
            `${model} — Ollama downloads it in the background`
          );
          watchPull(provider, model);
        })
        .catch((err) => toast("error", "Pull failed to start", err.message));
      return;
    }
    if (isNoteEntry(value)) {
      // the ⚠ line explaining an empty list is informational — picking it
      // must never become the model name
      combo.value = lastReal;
      return;
    }
    if (value === CUSTOM_ENTRY) {
      // The frontend assigns the picked entry to the widget BEFORE this
      // callback runs — restore a real value right away so the sentinel can
      // never be serialized (the dialog below resolves asynchronously, and
      // window.prompt throws on Electron/ComfyUI Desktop).
      combo.value = lastReal;
      const applyTyped = (typed) => {
        if (typeof typed !== "string" || !typed.trim()) return;
        combo.value = typed.trim();
        lastReal = combo.value;
        app.graph?.change?.();
        app.graph?.setDirtyCanvas(true, true);
      };
      const nativePrompt = () => {
        try {
          applyTyped(window.prompt("Model name", lastReal));
        } catch {
          toast(
            "warn",
            "Custom model entry unavailable",
            "This frontend cannot open a text prompt — pick one of the listed models instead."
          );
        }
      };
      const dialog = app.extensionManager?.dialog;
      if (typeof dialog?.prompt === "function") {
        Promise.resolve(
          dialog.prompt({ title: "Custom model", message: "Model name", defaultValue: lastReal })
        )
          .then(applyTyped)
          .catch(nativePrompt);
      } else {
        nativePrompt();
      }
      return;
    }
    lastReal = value;
  };
  combo = node.addWidget("combo", "model", lastReal, callback, {
    values: () => {
      installBackendCombo(node); // cheap re-assert (see the note there)
      const provider = enhanceProvider(node);
      const current = String(combo.value ?? "").trim();
      if (current && !isSentinelEntry(current)) lastReal = current;
      // sync return from cache; the api layer kicks an async refresh when
      // stale (deduped while one is in flight) so the NEXT open is current
      mrln.llmModels(provider);
      return buildModelValues({
        provider,
        current: lastReal,
        entry: mrln.llmModelsCached(provider),
      });
    },
  });
  combo.__mrlnCombo = true;
  if (old.tooltip) combo.tooltip = old.tooltip;
  node.widgets.splice(node.widgets.indexOf(combo), 1);
  node.widgets.splice(index, 1, combo);
  installBackendCombo(node);
  mrln.refreshLlmKeys();
  mrln.refreshLlmModels(enhanceProvider(node));
}

app.registerExtension({
  name: "mrln.promptComposer",
  nodeCreated(node) {
    // the extension-level hook fires for every constructed node with its
    // widgets already built — prototype onNodeCreated is NOT reliably
    // invoked across frontend versions
    const kind = node.comfyClass ?? node.type;
    if (kind === "MRLN_PromptEnhance") {
      enhanceModelDropdown(node);
    }
    if (kind === "MRLN_PromptTemplate") {
      enhanceTemplateNames(node);
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

    // The sidebar is a percentage of the window, which lands near 360px on a
    // common screen — and a six-column table with an editor disclosure open is
    // dense there. So the panel asks the host for a wider FLOOR while it is
    // the tab on screen, and gives it straight back when the sidebar shows
    // something else: the host is shared with Queue, Node library and friends,
    // and leaving a minimum behind on their behalf would be rude. Capped at a
    // third of the window so a small screen is never taken over.
    function claimPanelWidth(container, tries = 6) {
      // render() can run BEFORE the frontend attaches its container, so the
      // host is not reachable yet on the first frame — look again next frame
      // rather than giving up silently.
      const host = container.closest?.(".side-bar-panel");
      if (!host) {
        if (tries > 0) requestAnimationFrame(() => claimPanelWidth(container, tries - 1));
        return;
      }
      if (host.dataset.mrlnWidened === "1") return;
      const want = Math.round(Math.min(480, window.innerWidth * 0.34));
      if (want <= host.getBoundingClientRect().width) return;
      const previous = host.style.minWidth;
      host.style.minWidth = `${want}px`;
      host.dataset.mrlnWidened = "1";
      const observer = new MutationObserver(() => {
        if (host.isConnected && host.contains(panelRoot)) return;
        host.style.minWidth = previous;
        delete host.dataset.mrlnWidened;
        observer.disconnect();
      });
      observer.observe(host, { childList: true, subtree: true });
    }

    app.extensionManager.registerSidebarTab({
      id: "mrln-prompt-composer",
      icon: "pi pi-book",
      title: "Prompt Composer",
      tooltip: "MRLN Prompt Composer — browse, compose, preview and edit prompt libraries",
      type: "custom",
      render: (el) => {
        if (panelRoot) {
          if (!el.contains(panelRoot)) el.appendChild(panelRoot);
          claimPanelWidth(el);
          return;
        }
        panelRoot = document.createElement("div");
        panelRoot.style.height = "100%";
        el.appendChild(panelRoot);
        claimPanelWidth(el);
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
          refreshCombos: async () => {
            await app.refreshComboInNodes?.();
            // a definition refresh can drop our enhancements (see
            // installBackendCombo) — re-assert them on every Enhance node
            for (const node of enhanceNodes()) enhanceModelDropdown(node);
          },
          dialog: app.extensionManager?.dialog,
          // ADDITIVE (the ctx contract is append-only): the shared fetch
          // layer, so the panel's own llm-validate / llm-pull calls can use
          // the same cache and the same single URL encoding instead of a
          // second implementation.
          api: mrln,
          // ADDITIVE: route -> the URL the BROWSER must use. Everything else
          // in the panel goes through api.fetchApi, which applies ComfyUI's
          // api_base itself; a thumbnail is fetched by an <img src>, which
          // cannot. Without this a root-relative /mrln/prompt/thumb 404s on
          // every install served under a sub-path (a reverse proxy, or
          // ComfyUI's own --base-directory style deployments) while the JSON
          // routes keep working — a failure that looks like "thumbnails are
          // broken" rather than "the base path is missing".
          apiUrl: (route) => (typeof api.apiURL === "function" ? api.apiURL(route) : route),
        });
      },
    });
  },
});
