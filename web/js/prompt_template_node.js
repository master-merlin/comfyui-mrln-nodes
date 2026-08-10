// MRLN Prompt Template node helper — progressive enhancement on our own
// node type only: when the template combo changes, selection lines that
// reference slots the new template doesn't have are dropped (with a toast)
// instead of failing the queue. Headless/API behavior is covered by the
// node's VALIDATE_INPUTS instead; this file just smooths the click path.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const RANDOM_RE = /^(?:🎲 )?random(?:@\d+)?$/;
const OFF_RE = /^(?:🔇 )?off$/;

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

      async function adjustSelection(slug) {
        const text = selectionWidget.value ?? "";
        if (!text.trim()) return;
        let detail;
        try {
          const resp = await api.fetchApi(
            `/mrln/prompt/template?slug=${encodeURIComponent(slug)}`
          );
          if (!resp.ok) return; // unknown template: VALIDATE_INPUTS reports it
          detail = await resp.json();
        } catch {
          return;
        }
        const tpl = detail.template;
        const slotIds = new Set([
          ...tpl.slots.map((s) => s.id),
          ...tpl.variants.flatMap((v) => v.slots.map((s) => s.id)),
        ]);
        const variantNames = new Set(tpl.variants.map((v) => v.name));
        const kept = [];
        let dropped = 0;
        for (const raw of text.split("\n")) {
          const line = raw.trim();
          if (!line || line.startsWith("#") || !line.includes("=")) {
            kept.push(raw);
            continue;
          }
          const idx = line.indexOf("=");
          const key = line.slice(0, idx).trim();
          const value = line.slice(idx + 1).trim();
          const ok =
            key === "variant"
              ? variantNames.has(value) || RANDOM_RE.test(value) || OFF_RE.test(value)
              : slotIds.has(key);
          if (ok) kept.push(raw);
          else dropped += 1;
        }
        if (!dropped) return;
        selectionWidget.value = kept.join("\n");
        selectionWidget.callback?.(selectionWidget.value);
        app.graph?.setDirtyCanvas(true, true);
        app.extensionManager?.toast?.add?.({
          severity: "info",
          summary: "Selection adjusted",
          detail: `${dropped} line(s) didn't match template '${slug}' and were removed`,
          life: 4000,
        });
      }

      const original = templateWidget.callback;
      templateWidget.callback = function (value, ...rest) {
        const result = original?.call(this, value, ...rest);
        adjustSelection(value);
        return result;
      };
    };
  },
});
