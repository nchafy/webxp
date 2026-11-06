/* The five programs the demo desktop ships with.
 *
 * Each one exists to exercise a part of the window contract that is otherwise invisible:
 *
 *   Read me          container queries — the table restacks when the window gets narrow
 *   Notepad          content that must reflow on every resize frame
 *   Display          a program that reaches back out and changes the shell
 *   Window list      the geometry, read live, so a drag and a resize are observable
 *   About            a window that is neither resizable nor maximisable
 */

import { group, sheet, segment } from '../src/widgets.mjs';

/* ---------------------------------------------------------------------------
   Read me.
   --------------------------------------------------------------------------- */

const readme = {
  id: 'readme',
  name: 'Read me',
  icon: 'info',
  singleton: true,
  onDesktop: true,
  autostart: true,
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
    ]);
  },
};

/* ---------------------------------------------------------------------------
   Notepad.
   --------------------------------------------------------------------------- */

const notepad = {
  id: 'notepad',
  name: 'Untitled - Notepad',
  icon: 'notepad',
  onDesktop: true,
  width: 440,
  height: 300,
  status: true,
  mount({ win }) {
    const area = document.createElement('textarea');
    area.className = 'notepad-area';
    area.setAttribute('aria-label', 'Document text');
    area.spellcheck = false;
    area.value = 'This window is not a singleton. Open a second Notepad from the Start '
      + 'menu and drag it off this one.\n\nThe text reflows on every resize frame, which is '
      + 'the point: the window is not a screenshot of a window.\n';

    const count = () => win.setStatus(`${area.value.length} characters`);
    area.addEventListener('input', () => {
      count();
      // The title tracks the document, and the taskbar button tracks the title. XP put the
      // asterisk nowhere, but it is the one thing the title bar is for.
      win.setTitle(area.value ? 'Document - Notepad' : 'Untitled - Notepad');
    });
    count();

    return area;
  },
};

/* ---------------------------------------------------------------------------
   Display properties: a program that changes the shell it runs in.
   --------------------------------------------------------------------------- */

//: The three desktop backgrounds, as classes rather than inline styles so a strict
//: `style-src` needs no exception. `demo.css` draws them.
const WALLPAPERS = [
  { value: 'bliss', text: 'Bliss' },
  { value: 'flat', text: 'Flat blue' },
  { value: 'night', text: 'Night' },
];

const display = {
  id: 'display',
  name: 'Display Properties',
  icon: 'display',
  singleton: true,
  onDesktop: true,
  width: 380,
  height: 260,
  mount({ desktop }) {
    const current = WALLPAPERS.find((w) => desktop.surface.classList.contains(`wall-${w.value}`));

    const chooser = segment({
      label: 'Desktop background',
      options: WALLPAPERS,
      value: current ? current.value : 'bliss',
      onChange: (value) => {
        for (const wall of WALLPAPERS) {
          desktop.surface.classList.toggle(`wall-${wall.value}`, wall.value === value);
        }
        desktop.announce(`Desktop background: ${value}`);
      },
    });

    return sheet([
      group('Background', [chooser]),
      group('Window animation', [
        'The zoom on open, minimise and restore comes from --zoom-ms in theme.css. Under '
        + 'prefers-reduced-motion it is skipped rather than shortened.',
      ]),
    ]);
  },
};

/* ---------------------------------------------------------------------------
   Window list: the geometry, read live.
   --------------------------------------------------------------------------- */

//: Fast enough to look live under a drag, slow enough not to compete with the drag for
//: frames. The list is a diagnostic, not an animation.
const POLL_MS = 180;

const windowList = {
  id: 'windows',
  name: 'Window list',
  icon: 'folder',
  singleton: true,
  onDesktop: true,
  width: 420,
  height: 260,
  status: true,
  mount({ win, desktop }) {
    const table = document.createElement('table');
    table.className = 'group-table win-list';
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const name of ['Window', 'State', 'x, y', 'w × h']) {
      const th = document.createElement('th');
      th.setAttribute('scope', 'col');
      th.textContent = name;
      headRow.append(th);
    }
    head.append(headRow);
    const body = document.createElement('tbody');
    table.append(head, body);

    const STATES = { normal: 'normal', min: 'minimised', max: 'maximised' };

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
          else cell.dataset.label = ['Window', 'State', 'x, y', 'w × h'][index];
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
    // forever. A program that starts a timer owns stopping it.
    win.onClose = () => clearInterval(timer);

    return sheet([table]);
  },
};

/* ---------------------------------------------------------------------------
   About: fixed size, no maximise.
   --------------------------------------------------------------------------- */

const about = {
  id: 'about',
  name: 'About webxp',
  icon: 'check',
  singleton: true,
  width: 320,
  height: 180,
  resizable: false,
  maximizable: false,
  mount() {
    return sheet([
      group('webxp', [
        'A Windows XP window manager, extracted from the 30 Day Map Challenge base map and '
        + 'given the behaviour the original only drew.',
        'This window is neither resizable nor maximisable, so its grips and its maximise '
        + 'box are gone rather than inert.',
      ]),
    ]);
  },
};

export const PROGRAMS = [readme, notepad, display, windowList, about];
