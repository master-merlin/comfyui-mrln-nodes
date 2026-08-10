// MRLN Show Text — progressive enhancement for MRLN_ShowText only: render
// the executed text in a read-only widget on the node. The node itself is
// fully functional without this file (OUTPUT_NODE "ui" channel +
// passthrough output); everything here is wrapped so a frontend change
// degrades to a no-op instead of an error.
import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

app.registerExtension({
  name: "mrln.showText",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "MRLN_ShowText") return;

    function showText(node, message) {
      const text = Array.isArray(message?.text) ? message.text.join("") : "";
      try {
        let widget = (node.widgets ?? []).find((w) => w.name === "display");
        if (!widget) {
          widget = ComfyWidgets.STRING(node, "display", ["STRING", { multiline: true }], app)
            .widget;
          if (widget.inputEl) {
            widget.inputEl.readOnly = true;
            widget.inputEl.style.opacity = 0.8;
          }
          widget.serialize = false; // display only — never persist into the workflow
        }
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

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      showText(this, message);
    };
  },
});
