// MRLN Show Text — progressive enhancement for MRLN_ShowText only: render
// the executed text in a read-only widget on the node. The node itself is
// fully functional without this file (OUTPUT_NODE "ui" channel +
// passthrough output); everything here is wrapped so a frontend change
// degrades to a no-op instead of an error.
//
// Written against the CURRENT public API (ComfyUI >= 0.32), because the two
// things this file used to do are both deprecated now and both said so out
// loud in the console:
//
//   ComfyWidgets from "scripts/widgets.js"  — an INTERNAL module; ComfyUI
//     logs "not part of the public API. Future updates may break this
//     import." Replaced by node.addDOMWidget(), which the custom-node docs
//     list as the public way to add a DOM-backed widget.
//   nodeType.prototype.onExecuted = …       — prototype hijacking, which the
//     docs call deprecated in favour of official hooks, and which this pack's
//     own CONVENTIONS ban. Replaced by the api "executed" event, addressed to
//     our node by id.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "MRLN_ShowText";
const WIDGET_NAME = "display";

function displayWidget(node) {
  // Reuse the widget across executions — one per node, not one per run.
  const existing = (node.widgets ?? []).find((w) => w.name === WIDGET_NAME);
  if (existing) return existing;
  if (typeof node.addDOMWidget !== "function") return null;
  const area = document.createElement("textarea");
  area.className = "comfy-multiline-input";
  area.readOnly = true;
  area.style.opacity = "0.8";
  area.style.width = "100%";
  const widget = node.addDOMWidget(WIDGET_NAME, "customtext", area, {
    // display only — this must never persist into the saved workflow
    serialize: false,
    getValue: () => area.value,
    setValue: (value) => {
      area.value = value ?? "";
    },
  });
  widget.inputEl = area; // what the old ComfyWidgets widget exposed
  return widget;
}

function showText(node, output) {
  // one entry per execution; several only when ComfyUI list-maps the node
  // over a list input — a blank line keeps those values apart
  const text = Array.isArray(output?.text) ? output.text.join("\n\n") : "";
  try {
    const widget = displayWidget(node);
    if (!widget) return;
    widget.value = text;
    requestAnimationFrame(() => {
      const size = node.computeSize?.();
      if (size) {
        node.setSize?.([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
      }
      app.graph?.setDirtyCanvas(true, false);
    });
  } catch (err) {
    console.log("[MRLN] Show Text widget enhancement unavailable:", err);
  }
}

app.registerExtension({
  name: "mrln.showText",
  setup() {
    // The official channel for execution results. `detail.node` is the node
    // ID as a string; a graph that has moved on (node deleted mid-run) simply
    // yields nothing to update.
    api.addEventListener("executed", (event) => {
      try {
        const detail = event?.detail;
        if (!detail) return;
        const node = app.graph?.getNodeById?.(Number(detail.node));
        if (!node || (node.comfyClass ?? node.type) !== NODE_CLASS) return;
        showText(node, detail.output);
      } catch (err) {
        console.log("[MRLN] Show Text update skipped:", err);
      }
    });
  },
});
