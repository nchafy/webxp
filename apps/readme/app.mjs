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
    win.setStatus('Drag the title bar. Drag any edge or corner. Try the three boxes.');

    const lede = document.createElement('p');
    lede.textContent =
      'Every window on this desktop is a real window. It moves, it resizes from any of '
      + 'eight edges, it minimises into its own taskbar button and comes back out of it, '
      + 'and it maximises to the desktop above the taskbar.';

    return sheet([
      lede,
      group('By pointer', [
        'Drag the title bar to move. Drag an edge or a corner to resize.',
        'Double-click the title bar to maximise, and again to put it back.',
        'Click a taskbar button to raise its window, or to put the front one away.',
      ]),
      group('By keyboard', {
        caption: 'Focus a title bar first — it is in the tab order.',
        columns: ['Key', 'Does'],
        rows: [
          ['Arrow keys', 'Move the window ten pixels'],
          ['Shift and arrow keys', 'Resize the window ten pixels'],
          ['Enter', 'Maximise, or put it back'],
          ['Tab', 'Into the window’s own content'],
        ],
      }),
      group('Why the table above restacks', [
        'Drag this window’s left edge until it is narrow. The two-column table collapses '
        + 'to stacked rows with the column name in front of each cell.',
        'That rule is a container query, not a media query. A window 300 pixels wide on a '
        + '1600 pixel desktop has the problem a 300 pixel phone has, and a media query '
        + 'cannot see it.',
      ]),
      group('Adding your own app', [
        'Run scripts/apps.py --new myapp, then edit apps/myapp/app.mjs. The contract is '
        + 'in docs/apps.md.',
      ]),
    ]);
  },
};
