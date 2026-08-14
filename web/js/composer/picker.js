// The value picker: the popover that replaces a native <select> on the row's
// DRAWN VALUE field.
//
// Why it exists (design handoff, phase 3): the native list mixes two MODES
// (random, off) with 200+ content items in one flat list, offers no search over
// them, covers the row it belongs to, and is painted by the browser — so it is
// the one control in the panel that cannot follow the panel's own look.
//
// The <select> stays in the DOM as the source of truth. Committing a pick sets
// select.value and dispatches `change`, so every existing handler — row state,
// preview scheduling, seed retirement — runs exactly as it did before. This
// module owns presentation and keyboard, never the data.
//
// No top-level side effects: nothing here runs until openPicker() is called.

import { el } from "./dom.js";
import { wordPrefixMatch } from "./util.js";

const MODE_HINT = { random: "redraw each run", off: "skip" };
const MODE_GLYPH = { random: "🎲", off: "⊘" };

// The open picker lives in the DOM, not in a module variable: modules here are
// forbidden mutable top-level state (composer_modules.test.mjs enforces it),
// and the document already knows whether a picker is on screen.
export function closePicker(restoreFocus = false) {
  const node = document.querySelector(".pc-pick");
  if (!node) return;
  node.pcCleanup?.();
  node.remove();
  if (restoreFocus) node.pcAnchor?.focus?.();
}

export function pickerIsOpen() {
  return Boolean(document.querySelector(".pc-pick"));
}

/**
 * @param {object} options
 * @param {HTMLSelectElement} options.select source of truth: options and value
 * @param {Array} options.pool library items, for per-item draw weights
 * @param {HTMLElement} options.anchor the trigger to position against
 * @param {Function} [options.onEditSection] opens this section in the Library
 * @param {string} [options.sectionRef] shown in the footer action
 */
export function openPicker({ select, pool, anchor, onEditSection, sectionRef }) {
  closePicker();

  const weights = new Map();
  for (const item of pool ?? []) weights.set(item.name, Number(item.weight ?? 1));

  const all = [...select.options].map((option) => ({
    value: option.value,
    label: option.textContent,
    title: option.title,
    mode: option.value === "random" || option.value === "off",
  }));
  const modes = all.filter((entry) => entry.mode);
  const items = all.filter((entry) => !entry.mode);

  const search = el("input", {
    type: "search",
    class: "pc-pick-search",
    placeholder: "filter…",
    "aria-label": "Filter values",
  });
  const count = el("span", { class: "pc-pick-count" });
  const list = el("div", { class: "pc-pick-list", role: "listbox", tabindex: "-1" });
  const footer = el("div", { class: "pc-pick-foot" });

  const node = el(
    "div",
    { class: "pc-panel pc-portal pc-pick", role: "dialog", "aria-label": "Choose a value" },
    el(
      "div",
      { class: "pc-pick-head" },
      el("span", { class: "pc-pick-icon" }, "⌕"),
      search,
      count
    ),
    list,
    footer
  );

  let rows = [];
  let active = -1;

  function paintActive(scroll = true) {
    rows.forEach((row, i) => {
      row.setAttribute("aria-selected", i === active ? "true" : "false");
    });
    if (scroll) rows[active]?.scrollIntoView({ block: "nearest" });
  }

  function commit(value) {
    if (value !== select.value) {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    closePicker(true);
  }

  function entryRow(entry) {
    const chosen = entry.value === select.value;
    const weight = weights.get(entry.value);
    const right = entry.mode
      ? MODE_HINT[entry.value] ?? ""
      : weight === undefined
        ? ""
        : `×${weight}`;
    const row = el(
      "div",
      {
        class: `pc-pick-row${entry.mode ? " pc-pick-mode" : ""}`,
        role: "option",
        "aria-selected": "false",
        // the real option value: a row's text carries marks (•, LoRA, user)
        // that the value does not
        "data-value": entry.value,
        title: entry.title || "",
        onmousedown: (e) => {
          e.preventDefault(); // keep focus in the search field
          commit(entry.value);
        },
      },
      el(
        "span",
        { class: "pc-pick-mark" },
        chosen ? "✓" : entry.mode ? MODE_GLYPH[entry.value] ?? "" : ""
      ),
      el("span", { class: "pc-pick-name" }, entry.mode ? entry.value : entry.label),
      el("span", { class: "pc-pick-side" }, right)
    );
    if (chosen) row.classList.add("pc-pick-current");
    return row;
  }

  function render() {
    const query = search.value.trim();
    // The modes are never filtered: they are what the row DOES, not what it
    // contains, and hiding them behind a query would strip the two states a
    // composer reaches for most.
    const shown = query
      ? items.filter(
          (entry) => wordPrefixMatch(entry.label, query) || entry.value === select.value
        )
      : items;
    rows = [];
    const children = [];
    for (const entry of modes) {
      const row = entryRow(entry);
      rows.push(row);
      children.push(row);
    }
    children.push(
      el(
        "div",
        { class: "pc-pick-sep" },
        el("span", {}, "Items"),
        el("span", {}, "weight")
      )
    );
    if (!shown.length) {
      children.push(el("div", { class: "pc-pick-empty" }, `nothing matches “${query}”`));
    }
    for (const entry of shown) {
      const row = entryRow(entry);
      rows.push(row);
      children.push(row);
    }
    list.replaceChildren(...children);
    count.textContent = query ? `${shown.length}/${items.length}` : `${items.length}`;
    active = rows.findIndex((row) => row.classList.contains("pc-pick-current"));
    if (active < 0) active = 0;
    paintActive();
  }

  footer.replaceChildren(
    el("span", { class: "pc-pick-foot-note" }, sectionRef ?? ""),
    onEditSection
      ? el(
          "button",
          {
            class: "mrln-btn mrln-mini",
            onmousedown: (e) => {
              e.preventDefault();
              closePicker();
              onEditSection();
            },
          },
          "Edit section ↗"
        )
      : null
  );

  // The mouse moves the same cursor the keys do — otherwise hovering one row
  // while ⏎ commits another is a trap. No scrollIntoView on hover: the pointer
  // is already where the user is looking, and scrolling under it fights them.
  list.addEventListener("mouseover", (e) => {
    const row = e.target.closest?.(".pc-pick-row");
    if (!row) return;
    const index = rows.indexOf(row);
    if (index < 0 || index === active) return;
    active = index;
    paintActive(false);
  });

  search.addEventListener("input", render);
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!rows.length) return;
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
      paintActive();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const row = rows[active];
      if (row) commit(row.dataset.value);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closePicker(true);
    } else if (e.key === "Tab") {
      closePicker(true);
    }
  });

  render();
  document.body.appendChild(node);
  place(node, anchor);

  const onOutside = (e) => {
    if (!node.contains(e.target) && e.target !== anchor) closePicker();
  };
  const onScroll = (e) => {
    if (node.contains(e.target)) return;
    closePicker();
  };
  document.addEventListener("mousedown", onOutside, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", closePicker);

  node.pcAnchor = anchor;
  node.pcCleanup = () => {
    document.removeEventListener("mousedown", onOutside, true);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", closePicker);
  };
  search.focus();
  return node;
}

/** Anchor below the field, flip above when the room is not there, dock as a
 *  bottom sheet on a panel too narrow for a floating list. */
function place(node, anchor) {
  const rect = anchor.getBoundingClientRect();
  const panel = anchor.closest(".pc-panel");
  const panelWidth = panel ? panel.getBoundingClientRect().width : window.innerWidth;
  if (panelWidth < 460) {
    node.classList.add("pc-pick-sheet");
    return;
  }
  const width = Math.min(Math.max(rect.width, 300), window.innerWidth - 16);
  node.style.width = `${width}px`;
  const height = node.getBoundingClientRect().height;
  const below = window.innerHeight - rect.bottom;
  const flip = below < height + 12 && rect.top > below;
  node.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
  if (flip) node.style.top = `${Math.max(8, rect.top - height - 4)}px`;
  else node.style.top = `${rect.bottom + 4}px`;
}
