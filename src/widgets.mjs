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
 * two of this project's apps are folders and the other forty are not.
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
  if (selects) items.setAttribute('aria-multiselectable', 'false');

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
