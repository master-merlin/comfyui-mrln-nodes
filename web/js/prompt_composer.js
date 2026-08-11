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

app.registerExtension({
  name: "mrln.promptComposer",
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
