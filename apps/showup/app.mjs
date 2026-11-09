/* Show Up NYC — the folder.
 *
 * webxp draws a desktop and a taskbar but has no folder container: a program is an icon, and
 * an icon opens a window. So this is the folder, in the only way the shell affords one — a
 * window whose contents are the four programs, each of which opens in a window of its own.
 * That is what a folder on an XP desktop does, and `desktop.launch(id)` is the double click.
 *
 * It is the only thing on the desktop. The four programs it contains are deliberately NOT
 * `onDesktop`, so the desktop stays one icon rather than five and the folder means something.
 */

import { icon } from '../../src/icons.mjs';

//: What the folder contains, in the order it lists them. The ids are the app folders, and a
//: typo here is a dead row rather than a broken import, so `launch` is guarded below.
const CONTENTS = [
  {
    id: 'showup-map',
    name: 'Map',
    icon: 'globe',
    what: 'All 51 council districts and 59 community boards, drawn. Pick one to see who '
      + 'represents it and when it next meets.',
  },
  {
    id: 'showup-councils',
    name: 'City councils',
    icon: 'folder',
    what: 'The 51 City Council districts: the member, their committees, and the next '
      + 'meetings.',
  },
  {
    id: 'showup-boards',
    name: 'Community boards',
    icon: 'folder',
    what: 'The 59 community boards: how to reach one, when it meets, and what it asked the '
      + 'City to fund.',
  },
  {
    id: 'showup-readme',
    name: 'Readme',
    icon: 'document',
    what: 'What this is, where every figure comes from, and what it refuses to tell you.',
  },
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* One row: an icon, the name, and a line saying what opening it gets you.
 *
 * A button rather than a link. There is no URL to go to — `launch` opens a window in this
 * document — and a link whose href goes nowhere is a lie to anything that reads the page.
 * Activating on Enter and Space comes free with the element.
 */
function row(entry, desktop) {
  const item = el('li', 'folder-item');
  const button = el('button', 'folder-open');
  button.type = 'button';

  const art = icon(entry.icon, 32);
  art.classList.add('folder-art');

  const text = el('span', 'folder-text');
  text.append(el('span', 'folder-name', entry.name), el('span', 'folder-what', entry.what));

  button.append(art, text);
  button.addEventListener('click', async () => {
    // Awaited, and the await is the point: `launch` is async and resolves to the window or to
    // null for a program that is not registered. Testing the promise instead would make the
    // guard below unreachable, because a promise is always truthy.
    const opened = await desktop.launch(entry.id);
    if (!opened) {
      desktop.announce(`${entry.name} could not be opened. Its program is not registered.`);
    }
  });

  item.append(button);
  return item;
}

export default {
  id: 'showup',
  name: 'Show Up NYC',
  icon: 'folder',
  singleton: true,
  onDesktop: true,
  autostart: true,
  width: 520,
  height: 420,
  styles: ['app.css'],

  mount({ win, desktop }) {
    const root = el('div', 'folder-root');
    root.append(
      el('p', 'folder-lede', 'Who represents this address, and when can I turn up and say '
        + 'something. Four programs, one question each.'),
    );

    const list = el('ul', 'folder-list');
    list.setAttribute('aria-label', 'Show Up NYC programs');
    for (const entry of CONTENTS) list.append(row(entry, desktop));
    root.append(list);

    win.setStatus(`${CONTENTS.length} programs`);
    return root;
  },
};
