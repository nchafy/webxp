/* Read me — the reference app.
 *
 * It exists to exercise the part of the window contract that is otherwise invisible:
 * container queries. Drag this window narrow and its table restacks, which no media query
 * could do.
 */

import { group, sheet } from '../../src/widgets.mjs';

export default {
  id: 'readme',
  name: 'Read me',
  filetype: 'info',
  singleton: true,
  onDesktop: true,
  width: 560,
  height: 420,
  status: true,

  mount({ win }) {
    win.setStatus('Drag the title bar or any edge.');

    const lede = document.createElement('p');
    lede.textContent =
      'Windows move, resize from eight edges, minimise to the taskbar and maximise.';

    return sheet([
      lede,
      group('By pointer', [
        'Drag the title bar to move. Drag an edge or a corner to resize.',
        'Double-click the title bar to maximise, and again to put it back.',
        'Click a taskbar button to raise its window, or to put the front one away.',
      ]),
      group('By keyboard', {
        caption: 'Focus a title bar first.',
        columns: ['Key', 'Does'],
        rows: [
          ['Arrow keys', 'Move the window ten pixels'],
          ['Shift and arrow keys', 'Resize the window ten pixels'],
          ['Enter', 'Maximise, or put it back'],
          ['Tab', 'Into the window’s own content'],
        ],
      }),
      group('Narrow windows', [
        'Drag this window’s left edge until it is narrow. The table collapses to stacked rows.',
      ]),
      group('Adding your own app', [
        'Run scripts/apps.py --new myapp, then edit apps/myapp/app.mjs.',
      ]),
    ]);
  },
};
