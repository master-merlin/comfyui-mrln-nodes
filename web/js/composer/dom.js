// MRLN Prompt Composer — DOM primitives. Everything the panel builds its UI
// from that needs NOTHING but its arguments: the element factory, the two
// menu/reference helpers, the small control builders and the two button
// guards (in-flight disable, two-step arm).
//
// HARD RULE for this file: ZERO top-level side effects. ComfyUI auto-imports
// every .js file under WEB_DIRECTORY, so this module is evaluated standalone in
// the browser AND as an import of the panel; the module cache makes that
// harmless only because evaluating it does nothing but declare functions.
// Therefore: no app/api imports, no DOM access, no listeners, no network and no
// module-level mutable state at import time. document/window are touched only
// INSIDE these functions, i.e. when the panel calls them.
//
// util.js is the pure-data sibling (no DOM at all); this is the DOM sibling.

// ---- element factory -------------------------------------------------------

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (value !== undefined && value !== null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat(2)) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/**
 * Replace a node's children, dropping null/undefined the way el() does.
 *
 * `replaceChildren` is NOT el(): its arguments are (Node or DOMString), so a
 * null child is COERCED TO THE STRING "null" and rendered as visible text
 * (verified in Chromium: `box.replaceChildren(kid, null)` gives
 * textContent "realnull"). Every render function in this panel builds its
 * children as `condition ? el(…) : null` lists — the pattern el() was designed
 * around — so the one call that cannot filter them needs this.
 */
export function mount(node, ...children) {
  node.replaceChildren(...children.flat(2).filter((child) => child != null));
  return node;
}

const REF_RE = /(?<!\{)\{([A-Za-z_][A-Za-z0-9_-]*)\}(?!\})/g;

export function validateRefs(field, knownNames) {
  // red border when the text references a {name} that nothing provides
  const unknown = [...new Set([...field.value.matchAll(REF_RE)].map((m) => m[1]))].filter(
    (name) => !knownNames().has(name)
  );
  field.classList.toggle("mrln-input-error", unknown.length > 0);
  if (unknown.length) {
    field.title = `unknown reference {${unknown.join("}, {")}} — type '{' to pick from `
      + "what is available";
  } else if (field.dataset.mrlnBaseTitle !== undefined) {
    field.title = field.dataset.mrlnBaseTitle;
  }
  return unknown;
}

export function placeMenu(anchor, menu) {
  // FIXED positioning escapes the panel's overflow clipping (the menu was
  // being cut at the scroll container's edge, not the window's); flip
  // above the anchor when the window bottom is tight.
  const rect = anchor.getBoundingClientRect();
  const below = window.innerHeight - rect.bottom;
  const flipUp = below < 220 && rect.top > below;
  menu.style.position = "fixed";
  // content-sized: at least as wide as the anchor, growing up to 560px —
  // leftward when the panel hugs the window's right edge
  menu.style.width = "max-content";
  menu.style.minWidth = `${rect.width}px`;
  const spaceRight = window.innerWidth - rect.left - 12;
  if (spaceRight < 360) {
    menu.style.left = "auto";
    menu.style.right = `${window.innerWidth - rect.right}px`;
    menu.style.maxWidth = `${Math.max(rect.width, Math.min(560, rect.right - 12))}px`;
  } else {
    menu.style.right = "auto";
    menu.style.left = `${rect.left}px`;
    menu.style.maxWidth = `${Math.max(rect.width, Math.min(560, spaceRight))}px`;
  }
  if (flipUp) {
    menu.style.top = "auto";
    menu.style.bottom = `${window.innerHeight - rect.top}px`;
    menu.style.maxHeight = `${Math.max(120, rect.top - 16)}px`;
  } else {
    menu.style.bottom = "auto";
    menu.style.top = `${rect.bottom}px`;
    menu.style.maxHeight = `${Math.max(120, below - 16)}px`;
  }
  menu.classList.toggle("mrln-menu-up", flipUp);
}

export function braceAssist(field, getOptions, onPick) {
  // Typing '{' opens a picker over everything referencable at the caret;
  // choosing inserts the name and closes the brace. Returns a wrapper to
  // mount instead of the bare field (the menu anchors to it).
  const menu = el("div", { class: "mrln-brace-menu", style: "display:none" });
  const wrap = el("span", { class: "mrln-assist" }, field, menu);
  const onScroll = (e) => {
    if (!menu.contains(e.target)) hide(); // fixed menus must not desync
  };
  const hide = () => {
    if (menu.style.display !== "none") window.removeEventListener("scroll", onScroll, true);
    menu.style.display = "none";
  };
  const openBrace = () => {
    // last unclosed, unescaped '{' before the caret; returns {pos, partial}
    const caret = field.selectionStart ?? 0;
    const value = field.value;
    for (let i = caret - 1; i >= 0; i--) {
      const ch = value[i];
      if (ch === "}") return null;
      if (ch === "{") {
        if (value[i - 1] === "{") return null; // '{{' literal escape
        const partial = value.slice(i + 1, caret);
        return /^[A-Za-z0-9_-]*$/.test(partial) ? { pos: i, partial } : null;
      }
    }
    return null;
  };
  const refresh = () => {
    const at = openBrace();
    if (!at) {
      hide();
      return;
    }
    const lower = at.partial.toLowerCase();
    const options = getOptions().filter((o) => o.name.toLowerCase().startsWith(lower));
    if (!options.length) {
      hide();
      return;
    }
    menu.replaceChildren(
      ...options.slice(0, 14).map((option) =>
        el(
          "div",
          {
            class: "mrln-brace-item",
            onmousedown: (e) => {
              e.preventDefault(); // beat the blur
              field.setRangeText(`${option.name}}`, at.pos + 1, field.selectionStart, "end");
              hide();
              onPick?.(option);
              field.dispatchEvent(new Event("input", { bubbles: false }));
              field.focus();
            },
          },
          option.name,
          option.hint ? el("span", { class: "mrln-slug" }, ` ${option.hint}`) : null
        )
      )
    );
    if (menu.style.display === "none") window.addEventListener("scroll", onScroll, true);
    menu.style.display = "";
    placeMenu(field, menu);
  };
  field.addEventListener("input", refresh);
  field.addEventListener("click", refresh);
  field.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  });
  field.addEventListener("blur", () => setTimeout(hide, 150));
  return wrap;
}

export function tierChip(tier) {
  if (!tier) return null;
  return el("span", { class: `mrln-chip mrln-${tier}` }, tier);
}

// ---- small control builders ------------------------------------------------

export function field(name, control) {
  return el(
    "label",
    { class: "mrln-field" },
    el("span", { class: "mrln-field-name" }, name),
    control
  );
}

// Auto-growing textarea: height follows content, capped at ~35% viewport.
export function autoSize(area) {
  const cap = Math.floor(window.innerHeight * 0.35);
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight + 2, cap)}px`;
}

export function autoArea(attrs, text) {
  const area = el("textarea", attrs, text);
  area.classList.add("mrln-auto");
  area.addEventListener("input", () => autoSize(area));
  requestAnimationFrame(() => autoSize(area)); // after it is in the DOM
  return area;
}

export function loadingNote(message) {
  return el(
    "div",
    { class: "mrln-note mrln-loading" },
    el("span", { class: "mrln-spinner" }),
    ` ${message}`
  );
}

export function smallBtn(title, text, onclick, disabled = false) {
  // `disabled` is what the ↑/↓ move buttons use at the ends of a list —
  // moveOrder/the variant swaps no-op there, and a button that silently
  // does nothing reads as a bug.
  return el(
    "button",
    { class: "mrln-btn mrln-mini", title, onclick, disabled: disabled ? "" : null },
    text
  );
}

export function dragHandle() {
  return el("span", { class: "mrln-drag", title: "Drag to reorder" }, "⠿");
}

// ---- button guards ---------------------------------------------------------

export async function busy(button, fn) {
  // Every mutating async action runs through here: the button disables for
  // the life of the promise and re-enables in a finally. Double-clicking
  // Save/Apply used to interleave two POST → loadLibrary → selectTemplate
  // chains; a De-compose run takes up to two minutes and looked frozen.
  // (Inline style, not a class — the panel's stylesheet is shared and a
  // disabled button is otherwise indistinguishable in some themes.)
  if (button) {
    if (button.disabled) return undefined; // still running — swallow the click
    button.disabled = true;
    button.style.opacity = "0.55";
    button.style.cursor = "progress";
  }
  try {
    return await fn();
  } finally {
    if (button) {
      button.disabled = false;
      button.style.opacity = "";
      button.style.cursor = "";
    }
  }
}

export function armDestructive(button, reallyLabel, action) {
  // Button flavor: the first click relabels the button to the question and
  // arms it; the second click (any call while armed) runs the stored
  // action. Auto-disarms after ~4s. Returns the action's result so an
  // async armed action can be awaited (busy() keeps the button disabled).
  if (button.mrlnArmed) {
    clearTimeout(button.mrlnArmed.timer);
    button.textContent = button.mrlnArmed.label;
    button.classList.remove("mrln-armed");
    const run = button.mrlnArmed.action;
    button.mrlnArmed = null;
    return run();
  }
  button.mrlnArmed = {
    label: button.textContent,
    action,
    timer: setTimeout(() => {
      button.textContent = button.mrlnArmed.label;
      button.classList.remove("mrln-armed");
      button.mrlnArmed = null;
    }, 4000),
  };
  button.textContent = reallyLabel;
  button.classList.add("mrln-armed");
}
