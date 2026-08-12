// MRLN Prompt Template node helper — progressive enhancement on our own
// node type only: switching the template combo is an explicit decision to
// load that template, so the previous template's selection lines are
// cleared silently and the new template starts from its own per-slot
// defaults (an empty selection is always a fully-configured state).
// Trigger/seed/mode are left untouched — they carry across templates.
// Headless/API safety is covered by the node's VALIDATE_INPUTS.
import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "mrln.promptTemplateNode",
  nodeCreated(node) {
    // the extension-level hook fires for every constructed node with its
    // widgets already built — prototype onNodeCreated is NOT reliably
    // invoked across frontend versions (where it does not fire, this whole
    // enhancement would silently not exist)
    if ((node.comfyClass ?? node.type) !== "MRLN_PromptTemplate") return;
    try {
      const widgets = node.widgets ?? [];
      const templateWidget = widgets.find((w) => w.name === "template");
      const selectionWidget = widgets.find((w) => w.name === "selection");
      if (!templateWidget || !selectionWidget) return;

      // Legacy combo menus fire the callback even when the user re-picks
      // the entry that is already active — that is not a switch, so the
      // selection must survive. Track the previous value and only clear on
      // an actual change.
      let lastTemplate = templateWidget.value;
      // Workflow loads assign widget values without firing callbacks on
      // legacy frontends — resync so a post-load same-value re-pick is not
      // mistaken for a switch. Per-instance patch only, never the prototype.
      const onConfigure = node.onConfigure;
      node.onConfigure = function (...args) {
        const result = onConfigure?.apply(this, args);
        lastTemplate = templateWidget.value;
        return result;
      };

      const original = templateWidget.callback;
      templateWidget.callback = function (value, ...rest) {
        const result = original?.call(this, value, ...rest);
        const changed = value !== lastTemplate;
        lastTemplate = value;
        if (changed && (selectionWidget.value ?? "") !== "") {
          selectionWidget.value = "";
          selectionWidget.callback?.("");
          // change() notifies the frontend's change tracker — setDirtyCanvas
          // alone is only a redraw flag, so without it a programmatic widget
          // write can miss the serialized workflow
          app.graph?.change?.();
          app.graph?.setDirtyCanvas(true, true);
        }
        return result;
      };
    } catch (err) {
      console.log("[MRLN] Prompt Template selection-clear enhancement unavailable:", err);
    }
  },
});
