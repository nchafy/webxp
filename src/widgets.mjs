/* Content builders, for a window that wants the era's shapes rather than its own.
 *
 * These are optional: a program can return any node from its `mount`. They exist so that
 * two programs showing a record show it the same way.
 */

import { icon } from './icons.mjs';

//: Where the popup rules live, resolved against this module so the folder moves intact.
//:
//: Linked by the control that needs it, not by the host page. `index.html` links the window
//: manager's sheets, and this one belongs to popups most windows never build: `menuBar` links
//: it on first use, and a context menu host links it in its constructor, because a right-click
//: can be the first thing a visitor does. Either way the document gets exactly one `<link>`,
//: because `linkOnce` leaves a sheet another control already linked alone.
export const MENU_STYLESHEET = new URL('./menu.css', import.meta.url).href;

//: How far a submenu overlaps its parent popup. The pointer travelling from the parent row to
//: the submenu must never cross a gap: a gap is a `pointerleave` and the submenu closes under
//: the pointer on its way there.
const SUBMENU_OVERLAP = 3;

//: One level of submenu. Deeper nesting is refused rather than rendered: XP's shell menus go
//: one deep, a second level needs its own open-on-hover timing to be usable, and a popup
//: three deep does not fit beside a 220px window.
export const SUBMENU_DEPTH = 1;

//: An accelerator's key, where XP's spelling is not the `KeyboardEvent.key` value. A single
//: character and `F1`–`F12` need no entry. Lower case throughout: the match folds case, so
//: `Ctrl+Shift+S` can only arrive as `S`.
const NAMED_ACCEL_KEYS = Object.freeze({
  del: 'delete', delete: 'delete', ins: 'insert', insert: 'insert',
  esc: 'escape', escape: 'escape', enter: 'enter', tab: 'tab', space: ' ',
  home: 'home', end: 'end', pgup: 'pageup', pgdn: 'pagedown',
});

let menuSerial = 0;

//: The schemes a cell may link to. `table` and `group` are the shared renderer for "a
//: record", so the moment a cell's value comes from anywhere but a literal in the program
//: source — a fetch, a query string, a file a user opened — `javascript:` in an href is a
//: script that runs on activation. An allowlist is the only form of this check that stays
//: correct as schemes are added.
const SAFE_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

/**
 * Whether a cell may be rendered as a link to this href.
 *
 * A relative href has no scheme of its own and inherits the page's, so it is resolved
 * against the document to find out what it would actually navigate to. `new URL` throws on
 * input that is not a URL at all, which is also a refusal.
 */
function safeHref(href) {
  try {
    return SAFE_SCHEMES.has(new URL(href, document.baseURI).protocol);
  } catch {
    return false;
  }
}

/** A cell's content: a string, `[text, href]`, `{text, href}`, or a Node. */
function fillCell(target, value) {
  if (value instanceof Node) {
    target.append(value);
    return;
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    fillCell(target, value.href ? [value.text, value.href] : value.text);
    return;
  }
  const [text, href] = Array.isArray(value) ? value : [value, null];
  if (!href || !safeHref(href)) {
    // A refused href degrades to its text rather than to nothing, so the cell still carries
    // its value and the caller can see what happened.
    target.textContent = text == null ? '' : String(text);
    if (href) console.error(`webxp: refused a cell link to an unsupported scheme: ${href}`);
    return;
  }
  const a = document.createElement('a');
  a.textContent = text;
  a.href = href;
  // An off-site link carries no referrer.
  if (/^https?:/.test(href)) a.rel = 'noopener noreferrer';
  target.append(a);
}

/**
 * A real table, for content that has columns.
 *
 * `th` elements carry `scope`, so a screen reader announces the column with every cell and
 * the relationship is in the markup rather than in an em dash between a label and its
 * value.
 *
 * @param {{caption?: string, columns: string[], rows: any[][]}} spec
 * @returns {HTMLTableElement}
 */
export function table(spec) {
  const element = document.createElement('table');
  element.className = 'group-table';

  if (spec.caption) {
    const caption = document.createElement('caption');
    caption.textContent = spec.caption;
    element.append(caption);
  }

  const headRow = document.createElement('tr');
  for (const name of spec.columns) {
    const th = document.createElement('th');
    th.setAttribute('scope', 'col');
    th.textContent = name;
    headRow.append(th);
  }
  const head = document.createElement('thead');
  head.append(headRow);

  const body = document.createElement('tbody');
  for (const row of spec.rows) {
    const tr = document.createElement('tr');
    row.forEach((value, index) => {
      // The first column names its row, so it is a row header and not a plain cell.
      const cell = document.createElement(index === 0 ? 'th' : 'td');
      if (index === 0) {
        cell.setAttribute('scope', 'row');
      } else {
        // A narrow window hides the header row and collapses each row to a block, so the
        // column name has to travel with the cell for the stylesheet to print it back.
        cell.dataset.label = spec.columns[index];
      }
      fillCell(cell, value);
      tr.append(cell);
    });
    body.append(tr);
  }

  element.append(head, body);
  return element;
}

/**
 * One labelled group box: the era's way of titling a block, and a fieldset is what it was.
 *
 * @param {string} heading
 * @param {any[]|{columns: string[], rows: any[][]}} content
 *        A list of lines, or a table spec.
 * @returns {HTMLFieldSetElement}
 */
export function group(heading, content) {
  const box = document.createElement('fieldset');
  box.className = 'group';
  const legend = document.createElement('legend');
  legend.textContent = heading;
  box.append(legend);

  if (content && !Array.isArray(content) && Array.isArray(content.columns)) {
    box.append(table(content));
    return box;
  }

  for (const line of content || []) {
    const p = document.createElement('p');
    p.className = 'group-line';
    fillCell(p, line);
    box.append(p);
  }
  return box;
}

/**
 * A document page inside a window: the white sheet a dialog's prose sits on.
 *
 * @param {Node|Node[]} children
 * @returns {HTMLDivElement}
 */
export function sheet(children) {
  const element = document.createElement('div');
  element.className = 'sheet';
  element.append(...[].concat(children || []));
  return element;
}

/**
 * One icon tile: a picture above a name, activating to open something.
 *
 * The desktop's icons and a folder window's icons are the same tile on two surfaces, so the
 * shape is here and only the surface is styled per caller. `.icon-tile` carries the box —
 * the width a label wraps at, the gap, the absence of button chrome — and the caller's own
 * class carries what the surface changes, which is the label colour: white with a shadow
 * over a wallpaper, dark over a window's sheet.
 *
 * It is a button, not a link. Opening a program is a call, not a navigation, and a link with
 * a dead href promises the keyboard and the context menu something that is not there.
 *
 * @param {object} spec
 * @param {string} spec.name              The label. Also the accessible name.
 * @param {string} [spec.icon]            A key of `ICON_NAMES`.
 * @param {string} [spec.className]       Surface class, added beside `icon-tile`.
 * @param {number} [spec.size]            Icon size in CSS pixels.
 * @param {() => void} spec.onOpen
 * @returns {HTMLButtonElement}
 */
export function iconTile(spec) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = spec.className ? `icon-tile ${spec.className}` : 'icon-tile';
  button.append(icon(spec.icon, spec.size || 32));
  const name = document.createElement('span');
  name.textContent = spec.name;
  button.append(name);
  button.addEventListener('click', spec.onOpen);
  return button;
}

/**
 * A segmented control: one of N, with `aria-pressed` carrying which.
 *
 * @param {object} spec
 * @param {string} spec.label                 Names the group for a screen reader.
 * @param {{value: string, text: string}[]} spec.options
 * @param {string} spec.value                 The one that starts pressed.
 * @param {(value: string) => void} spec.onChange
 * @returns {HTMLDivElement}
 */
export function segment(spec) {
  const wrap = document.createElement('div');
  wrap.className = 'segment';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', spec.label);

  const buttons = spec.options.map((option) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = option.text;
    button.dataset.value = option.value;
    button.setAttribute('aria-pressed', String(option.value === spec.value));
    button.addEventListener('click', () => {
      for (const other of buttons) {
        other.setAttribute('aria-pressed', String(other === button));
      }
      spec.onChange(option.value);
    });
    return button;
  });

  wrap.append(...buttons);
  return wrap;
}

/* ---------------------------------------------------------------------------
   The menu bar: the row of menus under a window's title bar, and the popups it opens.

   A builder a program calls, not a `win.setMenu`. Three reasons, in order of weight:

   1. The menu belongs to the program, not to the frame. `setTitle` and `setStatus` write
      into elements `window.mjs` builds — a title bar and a status strip exist whether or not
      an app uses them. A menu bar does not: it is content at the top of the client area,
      which the program already owns through what it returns from `mount`.
   2. `window.mjs` knows nothing about what an app does, and says so in its own first
      paragraph. `setMenu` would put File, Open and Save As behind the frame's API and give
      the frame a second content tree to re-render and tear down — state in the frame that the
      program has to keep in step with its own.
   3. A builder composes. Notepad's menu bar sits above its textarea, Explorer's above its
      list; both are one array in the node the program already returns.

   Keyboard access is the whole point of building this rather than copying one. Every row is a
   real `button` with a menu role, the bar is one tab stop with a roving `tabindex`, arrows
   move, Escape closes, mnemonics work under Alt, and an accelerator printed in the right-hand
   column is bound by the bar that printed it.

   What a popup is made of is shared with `src/contextmenu.mjs`, which renders the same
   `.menu-popup` that `src/menu.css` styles: `MENU_STYLESHEET`, `SUBMENU_DEPTH`, `linkOnce`,
   `parseMnemonic`, `labelNode` and `placePopup` are exported and that module imports them.
   What is not shared is the item vocabulary and the row builder — a pull-down has `accel` and
   `checked`, a context menu has `selected` and no chord to bind — so `buildRow` and
   `buildPopup` stay one per shape rather than one with a flag.
   --------------------------------------------------------------------------- */

/**
 * Link a stylesheet, once per document.
 *
 * A sheet another control already linked is left be, so two controls sharing a sheet produce
 * one `<link>` whichever is built first, and a host page that links it itself is recognised.
 *
 * @param {string} href  Resolved, because the comparison is against `link.href`, which is.
 */
export function linkOnce(href) {
  const already = [...document.querySelectorAll('link[rel="stylesheet"]')]
    .some((link) => link.href === href);
  if (already) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  // A missing sheet would otherwise be a column of plain buttons, which reads as a layout bug
  // rather than as a failed fetch.
  link.addEventListener('error', () => {
    console.error(`webxp: a stylesheet failed to load: ${href}`);
  });
  document.head.append(link);
}

/**
 * Split a label at its mnemonic: `&File` is File with F underlined, and `&&` is a literal
 * ampersand so a label can contain one.
 *
 * XP marked the Alt key's target by underlining it, and the marker has to travel with the
 * label rather than being a second field, or the underline and the key can disagree.
 *
 * @returns {{plain: string, key: string|null, at: number}} `at` is -1 when there is none.
 */
export function parseMnemonic(label) {
  const text = String(label);
  let plain = '';
  let key = null;
  let at = -1;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === '&' && text[i + 1] === '&') {
      plain += '&';
      i += 1;
    } else if (text[i] === '&' && i + 1 < text.length && key === null) {
      key = text[i + 1].toLowerCase();
      at = plain.length;
    } else {
      plain += text[i];
    }
  }
  return { plain, key, at };
}

/** The label, with its mnemonic underlined. */
export function labelNode(parsed) {
  const span = document.createElement('span');
  span.className = 'menu-label';
  if (parsed.at < 0) {
    span.textContent = parsed.plain;
    return span;
  }
  const mark = document.createElement('u');
  mark.textContent = parsed.plain[parsed.at];
  span.append(parsed.plain.slice(0, parsed.at), mark, parsed.plain.slice(parsed.at + 1));
  return span;
}

/**
 * An accelerator as XP wrote it — `Ctrl+O`, `Ctrl+Shift+S`, `F5`, `Del` — as the fields a
 * `keydown` carries.
 *
 * Parsed rather than taken on trust, so the row that prints a chord is the row that binds it
 * and a chord nothing could match is a build error instead of a key that does nothing.
 *
 * @param {string} accel
 * @param {string} path  The item, for the message.
 */
function parseAccel(accel, path) {
  const chord = { ctrl: false, shift: false, alt: false, key: '' };
  for (const part of String(accel).split('+').map((piece) => piece.trim())) {
    const lower = part.toLowerCase();
    if (lower === 'ctrl' || lower === 'control') chord.ctrl = true;
    else if (lower === 'shift') chord.shift = true;
    else if (lower === 'alt') chord.alt = true;
    else if (chord.key) {
      throw new Error(`menuBar: ${path} has two keys in its accelerator: ${accel}`);
    } else {
      const named = /^f\d{1,2}$/.test(lower) ? lower : NAMED_ACCEL_KEYS[lower];
      if (!named && part.length !== 1) {
        throw new Error(`menuBar: ${path} has an accelerator this cannot bind: ${accel}`);
      }
      chord.key = named || lower;
    }
  }
  if (!chord.key) throw new Error(`menuBar: ${path} has an accelerator with no key: ${accel}`);
  return chord;
}

/**
 * The letter a keypress names, for matching against a mnemonic.
 *
 * `key` for a bare press, because it follows the keyboard layout and the reader is pressing
 * the letter they can see. `code` while Alt is held, because macOS composes with Alt and
 * reports `ƒ` for Alt+F — matched on `key`, every underlined letter in the bar would open
 * nothing on the platform this is developed on.
 */
function pressedLetter(event) {
  if (event.altKey) {
    const physical = /^Key([A-Z])$/.exec(event.code || '');
    if (physical) return physical[1].toLowerCase();
  }
  return event.key.toLowerCase();
}

/** Whether a `keydown` is this chord. `metaKey` is never part of one: XP had no Command. */
function matchesAccel(chord, event) {
  return event.key.toLowerCase() === chord.key
    && event.ctrlKey === chord.ctrl
    && event.shiftKey === chord.shift
    && event.altKey === chord.alt
    && !event.metaKey;
}

/**
 * How much of the viewport bottom the taskbar owns.
 *
 * A popup flips up rather than opening into it: the taskbar paints above every window, so a
 * row placed under it is a row nobody can click. The number stays in `theme.css` and is read
 * here; with no theme there is nothing to read and the viewport's own edge is the bound,
 * which keeps the arithmetic a number rather than a NaN.
 */
function reservedBottom() {
  const value = getComputedStyle(document.documentElement).getPropertyValue('--taskbar-h');
  return parseFloat(value) || 0;
}

/**
 * Put a popup where it fits, measured rather than assumed.
 *
 * The popup is `position: fixed`, which is what lets it leave the client area: `.window-body`
 * is `overflow: auto`, and an absolutely positioned popup is clipped by it and adds its own
 * height to what the body can scroll — a 900px probe popup took the body's `scrollHeight`
 * from 483 to 900 and was not hit-testable outside the window. A fixed popup resolves against
 * the viewport even though the body carries `container-type: inline-size`, so it is neither
 * clipped nor scrolled. `menu geometry` in `scripts/check.py` holds both halves. The same
 * positioning is what lets a context menu leave the desktop surface, which is
 * `overflow: hidden`, and paint over the taskbar.
 *
 * Measured before it is placed, because a flip needs the height, and the height depends on
 * `menu.css`'s cap and on how the labels wrapped. Both writes happen in one task, so nothing
 * is painted mid-move.
 *
 * @param {HTMLElement} popup
 * @param {{left: number, right: number, top: number, bottom: number}} anchor
 *        What the popup hangs off: a title's box, a row's box, or a pointer as a zero-size
 *        rect. A point is a box whose left is its right, so the edge flip below puts the menu
 *        to the left of the pointer — which is what makes one function serve both routes.
 * @param {{beside: boolean}} how   `beside` is a submenu: to the right of its row, not under.
 */
export function placePopup(popup, anchor, { beside }) {
  popup.hidden = false;
  popup.style.setProperty('left', '0px');
  popup.style.setProperty('top', '0px');
  const size = popup.getBoundingClientRect();
  const right = document.documentElement.clientWidth;
  const bottom = document.documentElement.clientHeight - reservedBottom();

  let x = beside ? anchor.right - SUBMENU_OVERLAP : anchor.left;
  let y = beside ? anchor.top : anchor.bottom;
  // Past the right edge, the popup goes to the other side of what it hangs off — XP's
  // behaviour for a menu opened near the edge of the screen.
  if (x + size.width > right) {
    x = beside ? anchor.left - size.width + SUBMENU_OVERLAP : anchor.right - size.width;
  }
  if (y + size.height > bottom) {
    // A submenu has no room above its own row, so it rides up the screen instead.
    y = beside ? bottom - size.height : anchor.top - size.height;
  }
  popup.style.setProperty('left', `${Math.round(Math.max(x, 0))}px`);
  popup.style.setProperty('top', `${Math.round(Math.max(y, 0))}px`);
}

/**
 * One row.
 *
 * @returns {{button: HTMLButtonElement, mnemonic: string|null, disabled: boolean,
 *            accel: object|null, submenu: object|null, activate: () => void}}
 */
function buildRow(item, menuPath, depth) {
  if (!item || typeof item.label !== 'string' || !item.label.trim()) {
    throw new Error(`menuBar: an item in ${menuPath} has no label`);
  }
  const parsed = parseMnemonic(item.label);
  const path = `${menuPath} > ${parsed.plain}`;
  const hasSubmenu = Array.isArray(item.groups);
  const checkable = item.checked !== undefined;
  const disabled = item.disabled === true;

  if (hasSubmenu && depth >= SUBMENU_DEPTH) {
    throw new Error(`menuBar: ${path} is a submenu ${depth + 1} deep; one level is the most`);
  }
  if (hasSubmenu && checkable) {
    throw new Error(`menuBar: ${path} is both a submenu and a check item`);
  }
  if (hasSubmenu && item.accel) {
    throw new Error(`menuBar: ${path} is a submenu, which cannot have an accelerator`);
  }
  if (item.onSelect !== undefined && typeof item.onSelect !== 'function') {
    throw new Error(`menuBar: ${path} has an onSelect that is not a function`);
  }
  // An absent feature does not leave its UI behind. A row with nothing to do and nothing to
  // open is the one thing this must not render — unless it is disabled, which is a row saying
  // out loud that it is unavailable.
  if (!hasSubmenu && !item.onSelect && !disabled) {
    throw new Error(`menuBar: ${path} has no onSelect and no submenu, so it would do nothing`);
  }

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'menu-item';
  button.setAttribute('role', checkable ? 'menuitemcheckbox' : 'menuitem');
  button.tabIndex = -1;
  // `aria-disabled`, not the `disabled` attribute: a disabled button cannot be focused at
  // all, so arrow keys would step straight over it and a screen reader would never be told
  // the row exists. Activation is refused in `activate` instead.
  if (disabled) button.setAttribute('aria-disabled', 'true');
  if (checkable) button.setAttribute('aria-checked', String(item.checked === true));

  // The check gutter and the submenu arrow are drawn, not spoken: `aria-checked` and
  // `aria-expanded` already carry both, and a glyph in the name reads as "tick Word wrap".
  const gutter = document.createElement('span');
  gutter.className = 'menu-check';
  gutter.setAttribute('aria-hidden', 'true');
  button.append(gutter, labelNode(parsed));

  if (item.accel) {
    const accel = document.createElement('span');
    accel.className = 'menu-accel';
    accel.textContent = item.accel;
    // Left in the accessible name rather than hidden and re-declared as `aria-keyshortcuts`:
    // the chord is announced once, in the spelling the reader sees, and `aria-keyshortcuts`
    // has a syntax of its own that `Ctrl+O` is not written in.
    button.append(accel);
  }

  const row = {
    button,
    mnemonic: parsed.key,
    disabled,
    accel: item.accel ? parseAccel(item.accel, path) : null,
    submenu: null,
    path,
  };

  if (hasSubmenu) {
    button.setAttribute('aria-haspopup', 'true');
    button.setAttribute('aria-expanded', 'false');
    const arrow = document.createElement('span');
    arrow.className = 'menu-arrow';
    arrow.setAttribute('aria-hidden', 'true');
    button.append(arrow);
    row.submenu = buildPopup({ label: item.label, groups: item.groups }, path, depth + 1);
    button.setAttribute('aria-controls', row.submenu.popup.id);
  }

  // No `disabled` test here. `menuBar`'s `select` is the one gate every activation path goes
  // through, and a second copy of the refusal is a second thing to keep in step — and one
  // that makes neither copy observable on its own.
  row.activate = () => {
    if (!checkable) {
      item.onSelect();
      return;
    }
    // The bar owns a check item's state. The alternative is the program re-rendering its menu
    // to show what it just did, which is a second copy of "is word wrap on".
    const next = button.getAttribute('aria-checked') !== 'true';
    button.setAttribute('aria-checked', String(next));
    item.onSelect(next);
  };

  return row;
}

/**
 * One popup, as `role="menu"` holding a `role="group"` per group.
 *
 * The grouping is the separator. A list of groups cannot have a stray rule at its top or two
 * in a row, which a hand-inserted separator can — and `role="group"` says the same thing to a
 * screen reader that the rule says to the eye.
 *
 * @returns {{popup: HTMLElement, rows: object[]}} `rows` in the order they are drawn.
 */
function buildPopup(menu, path, depth) {
  if (!Array.isArray(menu.groups) || !menu.groups.length) {
    throw new Error(`menuBar: ${path} has no groups`);
  }
  const popup = document.createElement('div');
  popup.className = 'menu-popup';
  popup.id = `menu-popup-${++menuSerial}`;
  popup.setAttribute('role', 'menu');
  // A submenu is named by its own label; a top-level menu is named by its title button, which
  // `menuBar` points at once the button exists.
  if (depth) popup.setAttribute('aria-label', parseMnemonic(menu.label).plain);
  popup.hidden = true;

  const rows = [];
  for (const items of menu.groups) {
    if (!Array.isArray(items) || !items.length) {
      throw new Error(`menuBar: ${path} has an empty group`);
    }
    const box = document.createElement('div');
    box.className = 'menu-group';
    box.setAttribute('role', 'group');
    for (const item of items) {
      const row = buildRow(item, path, depth);
      rows.push(row);
      if (!row.submenu) {
        box.append(row.button);
        continue;
      }
      // A submenu popup cannot sit inside its own button, so the pair share a wrapper.
      // `role="none"` is what keeps the rows children of the menu in the ARIA tree.
      const slot = document.createElement('div');
      slot.className = 'menu-slot';
      slot.setAttribute('role', 'none');
      slot.append(row.button, row.submenu.popup);
      box.append(slot);
    }
    popup.append(box);
  }
  return { popup, rows };
}

/**
 * A menu bar.
 *
 * ```js
 * menuBar({
 *   scope: win.bodyEl,
 *   menus: [{ label: '&File', groups: [
 *     [{ label: '&Open…', accel: 'Ctrl+O', onSelect: open }],
 *     [{ label: 'E&xit', onSelect: () => win.close() }],
 *   ] }],
 * })
 * ```
 *
 * A menu holds **groups** of items, so a separator falls out of the grouping and cannot be
 * misplaced. An item is `{label, accel?, checked?, disabled?, onSelect, groups?}`; `groups`
 * makes it a submenu, one level deep. `&` before a character is its mnemonic.
 *
 * Every refusal is a throw naming the item, because each one is a program that would
 * otherwise ship a row that does nothing: an item with no `onSelect`, a submenu two deep, an
 * accelerator on a submenu, an empty group, an `accel` with no `scope` to bind it in.
 *
 * @param {object} spec
 * @param {{label: string, groups: object[][]}[]} spec.menus
 * @param {Element} [spec.scope]  Where accelerators and Alt mnemonics are listened for —
 *        normally `win.bodyEl`, so a chord works wherever focus is in that window and nowhere
 *        else. Required if any item declares an `accel`. The listener is on the element, so
 *        it goes when the window does.
 * @param {string} [spec.label]   Names the bar for a screen reader.
 * @returns {HTMLDivElement}
 */
export function menuBar(spec) {
  if (!spec || !Array.isArray(spec.menus) || !spec.menus.length) {
    throw new Error('menuBar: needs { menus: [...] } with at least one menu');
  }
  linkOnce(MENU_STYLESHEET);

  const bar = document.createElement('div');
  bar.className = 'menu-bar';
  bar.setAttribute('role', 'menubar');
  bar.setAttribute('aria-label', spec.label || 'Menu bar');

  /** One per menu: its title button, its popup, its rows, and its mnemonic. */
  const menus = spec.menus.map((menu, index) => {
    if (!menu || typeof menu.label !== 'string' || !menu.label.trim()) {
      throw new Error(`menuBar: menu ${index} has no label`);
    }
    const parsed = parseMnemonic(menu.label);
    const built = buildPopup(menu, parsed.plain, 0);
    const title = document.createElement('button');
    title.type = 'button';
    title.className = 'menu-title';
    title.id = `${built.popup.id}-title`;
    title.setAttribute('role', 'menuitem');
    title.setAttribute('aria-haspopup', 'true');
    title.setAttribute('aria-expanded', 'false');
    title.setAttribute('aria-controls', built.popup.id);
    // One tab stop for the whole bar, as a menubar is: Tab moves past it, arrows move inside
    // it. The first title is the stop until an arrow moves it.
    title.tabIndex = index === 0 ? 0 : -1;
    title.append(labelNode(parsed));
    built.popup.setAttribute('aria-labelledby', title.id);

    const slot = document.createElement('div');
    slot.className = 'menu-slot';
    slot.setAttribute('role', 'none');
    slot.append(title, built.popup);
    bar.append(slot);
    return { ...built, title, mnemonic: parsed.key };
  });

  //: Which menu is open, and which submenu inside it. Two fields rather than a stack: the
  //: depth is capped at one, so a stack would be a list that is never longer than two.
  let open = -1;
  let openSub = null;
  /**
   * Whether the open menu was opened by a click on its own title.
   *
   * A click on an open menu's title closes it, as XP's did. Without this flag that rule eats
   * the most ordinary gesture there is: the pointer crossing the second title opens that menu
   * on the way, so by the time the click lands the menu is already open and the click closes
   * it — click File, click Edit, and the bar shuts instead of showing Edit.
   */
  let openedByClick = false;

  /**
   * The two listeners an open menu needs outside its own subtree, attached while it is open
   * and removed when it closes — so a bar sitting closed leaves nothing on `document` or
   * `window`, and `menuBar` needs no teardown hook from its caller.
   *
   * `pointerdown` closes on a click anywhere else, as `desktop.mjs` does for the Start menu,
   * and for the reason stated there: the menu has to be gone before the click underneath is
   * read as a window focus. In capture, so a handler on the way down cannot swallow it.
   *
   * `resize` closes rather than repositioning. A popup is placed from a rect measured when it
   * opened, so a viewport that changes size under it leaves it detached from its own title;
   * repositioning would also have to re-decide every flip.
   *
   * The connection test covers the one case neither can unregister itself from: a window
   * destroyed with its menu still down.
   */
  const whileOpen = (event) => {
    if (!bar.isConnected) {
      detachWhileOpen();
      return;
    }
    if (event.type === 'pointerdown' && bar.contains(event.target)) return;
    closeAll();
  };

  const detachWhileOpen = () => {
    document.removeEventListener('pointerdown', whileOpen, true);
    window.removeEventListener('resize', whileOpen);
  };

  const closeSubmenu = () => {
    if (!openSub) return;
    openSub.button.setAttribute('aria-expanded', 'false');
    openSub.submenu.popup.hidden = true;
    openSub = null;
  };

  const closeAll = () => {
    if (open < 0) {
      closeSubmenu();
      return;
    }
    // Read before anything is hidden. Hiding the element that holds focus hands focus to the
    // document body, so asking afterwards always answers "not in the bar" and the keyboard is
    // left on nothing.
    const held = bar.contains(document.activeElement);
    closeSubmenu();
    const was = menus[open];
    was.title.setAttribute('aria-expanded', 'false');
    was.popup.hidden = true;
    open = -1;
    openedByClick = false;
    detachWhileOpen();
    // Focus goes back to the title it came from — but only if it was in the bar. Pulling it
    // back from wherever it has gone is how a menu closing on a click elsewhere steals focus
    // from the thing that took it.
    if (held) was.title.focus();
  };

  const rove = (index) => {
    menus.forEach((menu, at) => { menu.title.tabIndex = at === index ? 0 : -1; });
  };

  /**
   * @param {'title'|'first'|'last'} land  Where the keyboard ends up.
   * @param {boolean} [byClick]            Whether a click on this title did it, which is what
   *        decides whether the next click on it closes it again.
   */
  const openMenu = (index, land = 'title', byClick = false) => {
    if (index !== open) closeAll();
    const menu = menus[index];
    open = index;
    openedByClick = byClick;
    menu.title.setAttribute('aria-expanded', 'true');
    placePopup(menu.popup, menu.title.getBoundingClientRect(), { beside: false });
    document.addEventListener('pointerdown', whileOpen, true);
    window.addEventListener('resize', whileOpen);
    rove(index);
    if (land === 'first') menu.rows[0].button.focus();
    else if (land === 'last') menu.rows[menu.rows.length - 1].button.focus();
    else menu.title.focus();
  };

  const openSubmenuOf = (row, land = 'none') => {
    if (openSub && openSub.button === row.button) {
      if (land === 'first') row.submenu.rows[0].button.focus();
      return;
    }
    closeSubmenu();
    row.button.setAttribute('aria-expanded', 'true');
    placePopup(row.submenu.popup, row.button.getBoundingClientRect(), { beside: true });
    openSub = { button: row.button, submenu: row.submenu };
    if (land === 'first') row.submenu.rows[0].button.focus();
  };

  /** The one gate: a click, an Enter, a mnemonic and an accelerator all arrive here. */
  const select = (row) => {
    if (row.disabled) return;
    if (row.submenu) {
      openSubmenuOf(row, 'first');
      return;
    }
    // Closed first, so the program's own focus move is the last one and not undone by the
    // bar putting focus back on the title.
    closeAll();
    row.activate();
  };

  const step = (index, by, length) => (index + by + length) % length;

  /** The rows a keypress navigates: the open submenu's, or the open menu's. */
  const rowsAround = (row) => {
    const sub = openSub && openSub.submenu.rows.includes(row) ? openSub.submenu.rows : null;
    return sub || menus[open].rows;
  };

  const mnemonicIndex = (event) => menus.findIndex((menu) => menu.mnemonic
    && menu.mnemonic === pressedLetter(event));

  const onTitleKey = (event, index) => {
    // An arrow along the bar carries the open state with it, as XP's did: once a menu is
    // down, the next one is one key away rather than one key and a reopen.
    const move = (to) => {
      if (open >= 0) {
        openMenu(to);
        return;
      }
      rove(to);
      menus[to].title.focus();
    };
    const keys = {
      ArrowRight: () => move(step(index, 1, menus.length)),
      ArrowLeft: () => move(step(index, -1, menus.length)),
      Home: () => move(0),
      End: () => move(menus.length - 1),
      ArrowDown: () => openMenu(index, 'first'),
      ArrowUp: () => openMenu(index, 'last'),
      Escape: () => closeAll(),
    };
    // Tab leaves the bar, so the menu goes with it. Not in `keys`, because this is the one
    // key whose default has to happen: closing moves focus to the title, and Tab then steps
    // from the title to whatever follows the bar.
    if (event.key === 'Tab') {
      closeAll();
      return;
    }
    const handler = keys[event.key];
    if (handler) {
      event.preventDefault();
      handler();
      return;
    }
    const letter = mnemonicIndex(event);
    if (letter >= 0) {
      event.preventDefault();
      openMenu(letter, 'first');
    }
  };

  const onItemKey = (event, row) => {
    // See `onTitleKey`: Tab's default is what takes focus out of the bar.
    if (event.key === 'Tab') {
      closeAll();
      return;
    }
    const rows = rowsAround(row);
    const at = rows.indexOf(row);
    const inSub = openSub ? openSub.submenu.rows === rows : false;
    /** Back out to the row the submenu hangs off. Both ArrowLeft and Escape do this. */
    const leaveSubmenu = () => {
      const parent = menus[open].rows.find((other) => other.button === openSub.button);
      closeSubmenu();
      parent.button.focus();
    };

    switch (event.key) {
      case 'ArrowDown':
        rows[step(at, 1, rows.length)].button.focus();
        break;
      case 'ArrowUp':
        rows[step(at, -1, rows.length)].button.focus();
        break;
      case 'Home':
        rows[0].button.focus();
        break;
      case 'End':
        rows[rows.length - 1].button.focus();
        break;
      case 'ArrowRight':
        if (row.submenu) openSubmenuOf(row, 'first');
        else openMenu(step(open, 1, menus.length), 'first');
        break;
      case 'ArrowLeft':
        if (inSub) leaveSubmenu();
        else openMenu(step(open, -1, menus.length), 'first');
        break;
      case 'Escape':
        if (inSub) leaveSubmenu();
        else closeAll();
        break;
      default: {
        // XP activated an item on its bare mnemonic, with no Enter. Enter and Space need no
        // case of their own: a `button` fires `click` on both.
        const hit = rows.find((other) => other.mnemonic
          && other.mnemonic === pressedLetter(event) && !other.disabled);
        if (!hit) return;
        select(hit);
        break;
      }
    }
    event.preventDefault();
  };

  menus.forEach((menu, index) => {
    menu.title.addEventListener('click', () => {
      // Only a menu this title's own click opened closes again. See `openedByClick`.
      if (open === index && openedByClick) {
        closeAll();
        return;
      }
      openMenu(index, 'title', true);
    });
    // Once one menu is down, sliding along the bar opens each in turn without a second click.
    menu.title.addEventListener('pointerenter', () => {
      if (open >= 0 && open !== index) openMenu(index);
    });
    menu.title.addEventListener('keydown', (event) => onTitleKey(event, index));

    const wire = (row) => {
      row.button.addEventListener('click', () => select(row));
      row.button.addEventListener('keydown', (event) => onItemKey(event, row));
      // The pointer moves the highlight, which in a menu is the focus.
      //
      // Only a row of the *parent* list closes an open submenu, so two submenus are never
      // down at once. "Any row that is not the one the submenu hangs off" is the obvious test
      // and it is wrong: every row inside the submenu passes it, so the submenu closes under
      // the pointer that has just travelled into it and the click that follows lands on
      // whatever is behind. `contextmenu.mjs`'s `pointerover` handler states the same rule
      // against `current.rows`, and records where that was measured.
      row.button.addEventListener('pointerenter', () => {
        row.button.focus();
        if (row.submenu) openSubmenuOf(row);
        else if (openSub && menu.rows.includes(row)) closeSubmenu();
      });
      if (row.submenu) row.submenu.rows.forEach(wire);
    };
    menu.rows.forEach(wire);
  });

  // Focus leaving the bar by any route other than Tab — a script moving it, a window the
  // shell raised — closes the menu too. Belt to `whileOpen`'s braces, which sees only a
  // pointerdown and a resize. The `relatedTarget` test is load-bearing: the arrows walk the
  // rows by moving focus, so closing on a move that stays inside the bar would take the menu
  // down as it is being used. `focusout` fires only in a document that has focus, which a
  // headless browser needs focus emulation for; the suite turns it on for the whole run and
  // `menu keyboard` asserts both edges.
  bar.addEventListener('focusout', (event) => {
    if (bar.contains(event.relatedTarget)) return;
    closeAll();
  });

  const accelRows = [];
  const collect = (rows) => rows.forEach((row) => {
    if (row.accel && !row.disabled) accelRows.push(row);
    if (row.submenu) collect(row.submenu.rows);
  });
  menus.forEach((menu) => collect(menu.rows));
  if (accelRows.length && !spec.scope) {
    throw new Error(
      `menuBar: ${accelRows[0].path} prints the accelerator ${accelRows[0].accel.key} with no `
      + 'scope to bind it in — pass scope: win.bodyEl',
    );
  }

  const scope = spec.scope || bar;
  scope.addEventListener('keydown', (event) => {
    if (event.key === 'F10' && !event.ctrlKey && !event.shiftKey && !event.metaKey) {
      // Windows' own way into a menu bar from the keyboard.
      event.preventDefault();
      menus[0].title.focus();
      return;
    }
    if (event.altKey && !event.ctrlKey && !event.metaKey) {
      const index = mnemonicIndex(event);
      if (index >= 0) {
        event.preventDefault();
        openMenu(index, 'first');
      }
      return;
    }
    const row = accelRows.find((candidate) => matchesAccel(candidate.accel, event));
    if (!row) return;
    // Taken off the browser only for a chord this bar will act on. A disabled row is not in
    // `accelRows` at all, so Ctrl+S under a greyed Save still reaches whatever the browser
    // binds it to rather than being swallowed by a row that does nothing.
    event.preventDefault();
    select(row);
  });

  return bar;
}

/**
 * An Explorer folder window: the task pane on the left, the icons on the right.
 *
 * Every folder in this project drew its own grid before this existed. `iconTile` gave them
 * the tile and nothing gave them the window, so two folders meant two `repeat(auto-fill,
 * 5.5rem)` rules, two sheet overrides and two hover washes in two app stylesheets, kept in
 * step by hand. A third folder would have been a third copy.
 *
 * What XP put in that left pane is the part worth having, and not decoration: it is where a
 * folder says what it is and where else you can go. A folder window without it is a grid in
 * a frame.
 *
 * Icons view only. XP also had Tiles, List, Details and Thumbnails, and a switch for them
 * belongs here when something can switch — a mode nobody can reach is dead code, and
 * `segment` above is the control it would use.
 *
 * Selection is the one thing parameterised ahead of its caller, and deliberately: a file
 * picker is a folder listing plus a footer, so without a seam here the picker gets a second
 * lister. win32.run is the worked example of not having one — three copies of its listing,
 * two of them a 200-line copy-paste, and the save side's type filter left as
 * `is_desired(){ return true }` because the fix went into the other copy.
 *
 * `select: 'one'` therefore exists before the picker does. `'many'` does not, because
 * nothing needs it and a mode with no caller is the thing this comment warns about.
 *
 * An item may be `disabled` with a `hint` saying why. XP greyed a file it would not let you
 * pick rather than hiding it, which is the better behaviour: a visitor can see the file is
 * there and learn why it is refused. Extension filtering is the caller's to compute — this
 * widget does not know what a file is.
 *
 * The caller owns `status: true` on its own spec: the object count goes in the window's
 * status bar, which is where XP put it, and this returns no chrome it does not own.
 *
 * Load `/src/folder.css` from the calling app's `styles`. It is not linked globally because
 * almost none of this project's apps are folders.
 *
 * @param {object} spec
 * @param {{name: string, icon?: string, onOpen: () => void}[]} spec.items
 * @param {{name: string, icon?: string, onOpen: () => void}[]} [spec.tasks]
 *        "File and Folder Tasks". Omit it rather than passing an empty list.
 * @param {{name: string, icon?: string, onOpen: () => void}[]} [spec.places]
 *        "Other Places" — where a reader goes instead of here.
 * @param {[string, string][]} [spec.details]  "Details": label and value rows.
 * @param {string} [spec.itemClass]  Surface class for each tile, as `iconTile` takes.
 * @param {'none'|'one'} [spec.select]  `'one'` makes a click select and a double click open.
 * @param {(item: object, index: number) => void} [spec.onSelect]  Fired on selection change.
 * @returns {HTMLDivElement}
 */
export function folderView(spec) {
  const view = document.createElement('div');
  view.className = 'folder-view';

  const pane = document.createElement('div');
  pane.className = 'folder-pane';
  for (const block of [
    ['File and Folder Tasks', spec.tasks],
    ['Other Places', spec.places],
  ]) {
    const [heading, entries] = block;
    if (!entries || !entries.length) continue;
    pane.append(taskBlock(heading, entries.map(paneLink)));
  }
  if (spec.details && spec.details.length) {
    const rows = document.createElement('dl');
    rows.className = 'folder-details';
    for (const [label, value] of spec.details) {
      const term = document.createElement('dt');
      term.textContent = label;
      const detail = document.createElement('dd');
      detail.textContent = value;
      rows.append(term, detail);
    }
    pane.append(taskBlock('Details', [rows]));
  }
  if (pane.childElementCount) view.append(pane);

  const items = document.createElement('ul');
  items.className = 'folder-items';
  const selects = spec.select === 'one';
  if (selects) {
    // The roles are set in selecting mode and nowhere else. A folder of launchers is a list
    // of buttons — `apps/map-challenge/app.mjs` uses this as exactly that — and calling that
    // a listbox promises a selection the reader cannot make. `aria-multiselectable` was here
    // before the role was, on a `<ul>` whose implicit `list` role ignored it.
    items.setAttribute('role', 'listbox');
    items.setAttribute('aria-multiselectable', 'false');
  }

  const tiles = [];
  (spec.items || []).forEach((item, index) => {
    const cell = document.createElement('li');
    const tile = iconTile({
      name: item.name,
      icon: item.icon,
      className: spec.itemClass ? `folder-item ${spec.itemClass}` : 'folder-item',
      // `iconTile` binds this to click. In selecting mode a click selects and a double
      // click opens, as Explorer does, so the open is moved to `dblclick` below and this
      // becomes the selection.
      onOpen: () => {
        if (!selects) {
          item.onOpen();
          return;
        }
        for (const other of tiles) other.setAttribute('aria-selected', 'false');
        tile.setAttribute('aria-selected', 'true');
        if (spec.onSelect) spec.onSelect(item, index);
      },
    });

    if (item.disabled) {
      // Shown and refused, not hidden: a reader can see the thing is there and read why.
      tile.disabled = true;
      tile.classList.add('is-refused');
      if (item.hint) tile.title = item.hint;
    }
    if (selects) {
      tile.setAttribute('role', 'option');
      tile.setAttribute('aria-selected', 'false');
      tile.addEventListener('dblclick', () => {
        if (!item.disabled) item.onOpen();
      });
    }

    tiles.push(tile);
    cell.append(tile);
    items.append(cell);
  });
  view.append(items);
  return view;
}

/* One collapsible block of the task pane.
 *
 * The chevron is a real button carrying `aria-expanded`, not a styled div: collapsing a
 * section is something a keyboard reaches in XP, and the heading is what names the region
 * it collapses.
 */
function taskBlock(heading, children) {
  const block = document.createElement('section');
  block.className = 'task-block';

  const bar = document.createElement('h2');
  bar.className = 'task-head';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'task-toggle';
  toggle.textContent = heading;
  toggle.setAttribute('aria-expanded', 'true');
  bar.append(toggle);

  const body = document.createElement('div');
  body.className = 'task-body';
  body.append(...children);

  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') === 'true';
    toggle.setAttribute('aria-expanded', String(!open));
    body.hidden = open;
  });

  block.append(bar, body);
  return block;
}

/* A line in the task pane: a small icon and a label, activating to go somewhere. */
function paneLink(entry) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'pane-link';
  if (entry.icon) button.append(icon(entry.icon, 16));
  const label = document.createElement('span');
  label.textContent = entry.name;
  button.append(label);
  button.addEventListener('click', entry.onOpen);
  return button;
}
