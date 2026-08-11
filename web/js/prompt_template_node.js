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
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "MRLN_PromptTemplate") return;
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      const widgets = this.widgets ?? [];
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
      // mistaken for a switch.
      const onConfigure = this.onConfigure;
      this.onConfigure = function (...args) {
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
          app.graph?.setDirtyCanvas(true, true);
        }
        return result;
      };
    };
  },
});
