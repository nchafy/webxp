/* Content builders, for a window that wants the era's shapes rather than its own.
 *
 * These are optional: a program can return any node from its `mount`. They exist so that
 * two programs showing a record show it the same way.
 */

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
  if (!href) {
    target.textContent = text == null ? '' : String(text);
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
