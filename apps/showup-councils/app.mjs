/* City councils — the 51 Council districts, and who represents each.
 *
 * Ported from the `/district/` index and the 51 district pages. The list and the facts were
 * two routes; here they are two panes of one window, which is what makes the 51 pages a
 * program rather than 51 programs.
 *
 * The facts come from `preview.json`, which the build already writes for the map's panel.
 * It carries the member, the committees, the participatory-budgeting cycle and the covering
 * community boards. What the old pages had and this does not yet: the testimony guidance and
 * the citywide meeting shortlist. Both are prose and a citywide artifact rather than
 * per-district facts, and neither is in that file — so they are named here as missing rather
 * than quietly dropped.
 */

import { group } from '../../src/widgets.mjs';
import { districts, load, usableTable, PREVIEW } from '../../shared/showup/data.mjs';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* One row of the list. A button, not a link: there is no URL to go to, and a link whose href
 * goes nowhere is a lie to anything that reads the page. */
function row(entry, onPick) {
  const item = el('li', 'pick-item');
  const button = el('button', 'pick');
  button.type = 'button';
  button.dataset.district = String(entry.n);
  button.append(
    el('span', 'pick-name', `District ${entry.n}`),
    el('span', 'pick-hoods', entry.hoods || ''),
  );
  button.addEventListener('click', () => onPick(entry));
  item.append(button);
  return item;
}

export default {
  id: 'showup-councils',
  name: 'City councils',
  icon: 'folder',
  singleton: true,
  width: 720,
  height: 520,
  status: true,
  styles: ['app.css'],

  mount({ win }) {
    const root = el('div', 'list-detail');
    const list = el('ul', 'pick-list');
    list.setAttribute('aria-label', 'Council districts');
    const detail = el('div', 'detail-pane');
    detail.append(el('p', 'detail-empty', 'Pick a district to see who represents it.'));
    root.append(list, detail);

    win.app = { list, detail };
    win.setStatus('Loading the districts…');
    return root;
  },

  async ready({ win }) {
    const { list, detail } = win.app;

    let rows;
    let preview;
    try {
      [rows, preview] = await Promise.all([districts(), load(PREVIEW)]);
    } catch (error) {
      // Named, not swallowed: a reader who sees an empty list should be told whose fault it is.
      win.setStatus(`The districts could not be loaded (${error.message}).`);
      list.replaceChildren(el('li', 'pick-error', `Could not load: ${error.message}`));
      return;
    }

    const show = (entry) => {
      const facts = (preview.districts || {})[String(entry.n)] || {};
      const member = facts.member;
      const nodes = [];

      const lines = [];
      if (member && member.name) lines.push(['Council Member', member.name]);
      // Only when it is not "filled": a status beside every name would read as a warning
      // about fifty filled seats.
      if (member && member.seat_status && member.seat_status !== 'filled') {
        lines.push(['Seat', member.seat_status]);
      }
      if (entry.hoods) lines.push(['Neighbourhoods', entry.hoods]);
      nodes.push(
        lines.length
          ? group(`Council District ${entry.n}`, { columns: ['Field', 'Value'], rows: lines })
          : group(`Council District ${entry.n}`, ['No member is recorded for this district.']),
      );

      for (const spec of facts.tables || []) {
        nodes.push(group(spec.legend, usableTable(spec)));
      }

      detail.replaceChildren(...nodes);
      // Scoped to this app's own list, not `document`: a query from the document would find
      // another window's rows the moment a second one opened.
      for (const button of list.querySelectorAll('.pick')) {
        button.setAttribute('aria-current', button.dataset.district === String(entry.n)
          ? 'true'
          : 'false');
      }
      win.setStatus(`Council District ${entry.n}${member && member.name ? ` — ${member.name}` : ''}`);
    };

    list.replaceChildren(...rows.map((entry) => row(entry, show)));
    win.setStatus(`${rows.length} council districts. Pick one.`);
  },
};
