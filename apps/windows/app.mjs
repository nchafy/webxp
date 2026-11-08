/* Window list — the geometry of every open window, read live.
 *
 * A diagnostic as much as a demo: it makes a drag and a resize observable as numbers, which
 * is what the check harness reads too.
 */

import { sheet } from '../../src/widgets.mjs';

//: Fast enough to look live under a drag, slow enough not to compete with the drag for
//: frames. The list is a diagnostic, not an animation.
const POLL_MS = 180;

const COLUMNS = ['Window', 'State', 'x, y', 'w × h'];
const STATES = { normal: 'normal', min: 'minimised', max: 'maximised' };

export default {
  id: 'windows',
  name: 'Window list',
  icon: 'folder',
  singleton: true,
  onDesktop: true,
  width: 420,
  height: 260,
  status: true,
  styles: ['app.css'],

  mount({ win, desktop }) {
    const table = document.createElement('table');
    table.className = 'group-table win-list';

    const headRow = document.createElement('tr');
    for (const name of COLUMNS) {
      const th = document.createElement('th');
      th.setAttribute('scope', 'col');
      th.textContent = name;
      headRow.append(th);
    }
    const head = document.createElement('thead');
    head.append(headRow);
    const body = document.createElement('tbody');
    table.append(head, body);

    const refresh = () => {
      const rows = desktop.windows.map((other) => {
        const tr = document.createElement('tr');
        const cells = [
          other.titleText,
          STATES[other.state],
          `${other.box.x}, ${other.box.y}`,
          `${other.box.w} × ${other.box.h}`,
        ];
        cells.forEach((text, index) => {
          const cell = document.createElement(index === 0 ? 'th' : 'td');
          if (index === 0) cell.setAttribute('scope', 'row');
          else cell.dataset.label = COLUMNS[index];
          cell.textContent = text;
          tr.append(cell);
        });
        return tr;
      });
      body.replaceChildren(...rows);
      win.setStatus(`${desktop.windows.length} open`);
    };

    refresh();
    const timer = setInterval(refresh, POLL_MS);
    // Without this the interval outlives the window and writes into a detached table
    // forever. An app that starts a timer owns stopping it.
    win.onClose = () => clearInterval(timer);

    return sheet([table]);
  },
};
