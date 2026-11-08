/* Content builders, for a window that wants the era's shapes rather than its own.
 *
 * These are optional: a program can return any node from its `mount`. They exist so that
 * two programs showing a record show it the same way.
 */

import { icon } from './icons.mjs';

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
