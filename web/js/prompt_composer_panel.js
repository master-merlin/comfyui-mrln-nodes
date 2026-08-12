// MRLN Prompt Composer — panel shell: the four tab bodies, the tab switch and
// the wiring that assembles the modules. ES module with NO top-level side
// effects (ComfyUI auto-loads every js file in WEB_DIRECTORY; the module cache
// makes that harmless). All computation happens server-side via
// /mrln/prompt/*; this file and its modules only move state between DOM,
// endpoints and node widgets.
//
// The modules under ./composer/ (each side-effect-free, each unit-tested for
// that property by tests/js/composer_modules.test.mjs):
//   util.js            pure data helpers — no DOM, no network
//   api.js             the fetch layer (instantiated by prompt_composer.js)
//   dom.js             DOM primitives: el, menus, small controls, button guards
//   image.js           image bytes: metadata upload, downscale, drop/paste
//   state.js           the ONE state object + everything that owns it
//   compose.js         the Compose tab + node interop
//   intake.js          image → template intake (mounted in the De-compose tab)
//   decompose.js       the De-compose tab
//   tree.js            the Library tab tree, editor mount, profiles, delete
//   thumbs.js          thumbnail tiles, set/reset controls, the URL rule
//   section_editor.js  the section editor + combine builder
//   template_editor.js the raw-JSON template editor
//   bundles.js         import/export of shareable bundles
//   loras.js           missing-LoRA banner, downloads, file picker
//   history.js         the History tab
//   settings.js        the Settings tab
//
// The 'hub' below is how they reach each other: ONE state object, the ctx
// injected by prompt_composer.js, the four tab bodies, and every module's
// exports assigned onto it as it is built. Modules call each other through
// thin hub forwarders — what function hoisting inside the old single closure
// did for free.
import { createState, createStore } from "./composer/state.js";
import { createBundles } from "./composer/bundles.js";
import { createCompose } from "./composer/compose.js";
import { createDecompose } from "./composer/decompose.js";
import { createHistory } from "./composer/history.js";
import { createIntake } from "./composer/intake.js";
import { createLoras } from "./composer/loras.js";
import { createSectionEditor } from "./composer/section_editor.js";
import { createSettings } from "./composer/settings.js";
import { createTemplateEditor } from "./composer/template_editor.js";
import { createThumbs } from "./composer/thumbs.js";
import { createTree } from "./composer/tree.js";
import { el } from "./composer/dom.js";

export function createComposerPanel(root, ctx) {
  root.classList.add("mrln-composer");

  const state = createState();

  // ---- skeleton ------------------------------------------------------------

  const composeTab = el("div", { class: "mrln-tab-body" });
  const decomposeTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  const libraryTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  const historyTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  const settingsTab = el("div", { class: "mrln-tab-body", style: "display:none" });
  // Order is the workflow order — compose, take one apart, browse what you
  // have, look at what you rendered, configure. Settings stays last.
  const tabNames = ["compose", "decompose", "library", "history", "settings"];
  const tabBodies = {
    compose: composeTab,
    decompose: decomposeTab,
    library: libraryTab,
    history: historyTab,
    settings: settingsTab,
  };
  const tabButtons = el(
    "div",
    { class: "mrln-tabs" },
    el("button", { class: "mrln-active", onclick: () => switchTab("compose") }, "Compose"),
    el("button", { onclick: () => switchTab("decompose") }, "De-compose"),
    el("button", { onclick: () => switchTab("library") }, "Library"),
    el("button", { onclick: () => switchTab("history") }, "History"),
    el("button", { onclick: () => switchTab("settings") }, "Settings")
  );
  root.replaceChildren(
    tabButtons,
    composeTab,
    decomposeTab,
    libraryTab,
    historyTab,
    settingsTab
  );

  function switchTab(name) {
    state.tab = name;
    for (const tab of tabNames) tabBodies[tab].style.display = tab === name ? "" : "none";
    tabButtons.querySelectorAll("button").forEach((button, i) => {
      button.classList.toggle("mrln-active", tabNames[i] === name);
    });
    if (name === "library") hub.renderLibraryTab();
    if (name === "decompose") hub.renderDecomposeTab();
    if (name === "history") hub.renderHistoryTab();
    if (name === "settings") hub.renderSettingsTab();
  }

  // ---- wiring --------------------------------------------------------------
  // Build order matters in exactly one way: a module that destructures another
  // module's PERSISTENT ELEMENT (compose.js takes loraBanner and editorBox)
  // must be built after it. Every cross-module CALL goes through a hub
  // forwarder, so those are order-independent.
  const hub = {
    ctx,
    state,
    root,
    composeTab,
    decomposeTab,
    libraryTab,
    historyTab,
    settingsTab,
    switchTab,
  };
  Object.assign(hub, createLoras(hub));
  Object.assign(hub, createThumbs(hub));
  Object.assign(hub, createTree(hub));
  Object.assign(hub, createBundles(hub));
  Object.assign(hub, createSectionEditor(hub));
  Object.assign(hub, createTemplateEditor(hub));
  Object.assign(hub, createCompose(hub));
  Object.assign(hub, createStore(hub));
  Object.assign(hub, createIntake(hub));
  Object.assign(hub, createDecompose(hub));
  Object.assign(hub, createHistory(hub));
  Object.assign(hub, createSettings(hub));

  // ---- boot ----------------------------------------------------------------
  // No disposer: the panel is a deliberate never-unmounted singleton (see
  // prompt_composer.js), so returning a cleanup only advertised a lifecycle
  // that never runs — its sole caller discarded it.

  hub.loadLibrary(false);
}
