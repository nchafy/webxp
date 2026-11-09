/* Explorer — a folder window. Lists a directory, opens what you double-click.
 *
 * The filesystem is the subject here, not a model of it: every row is an `lstat` of a real
 * entry, and a change made in a terminal appears without a refresh because this window
 * watches the filesystem it is listing. That is the whole reason the desktop is being built
 * on top of `shared/fs/` rather than beside it.
 *
 * ## Sizing
 *
 * No JavaScript sizes anything. `.window-body` declares `container: window / inline-size`,
 * so a `height: 100%` list with `overflow-y: auto` fills its window and scrolls, and that
 * survives a drag-resize, a maximise and a restore with no listener and nothing to keep in
 * step. Reading the window's own geometry would mean keeping a second copy of the layout's
 * answer on this side of the boundary.
 *
 * ## What a row knows
 *
 * `lstat`, never `stat`: a symlink lists as a symlink and says where it points, the same way
 * `ls -l` does. Opening one follows it, because that is what double-clicking a shortcut
 * means — the distinction is between describing a name and using it.
 */

import { icon } from '../../src/icons.mjs';

//: Columns, in order. The widths live in `app.css`; this is what each cell says.
const COLUMNS = ['Name', 'Size', 'Type', 'Modified'];

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function stamp(ms) {
  const date = new Date(ms);
  return `${MONTHS[date.getMonth()]} ${String(date.getDate()).padStart(2, ' ')} `
    + `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function size(entry) {
  if (entry.type === 'dir') return '';
  if (entry.size < 1024) return `${entry.size} B`;
  if (entry.size < 1024 * 1024) return `${(entry.size / 1024).toFixed(1)} KB`;
  return `${(entry.size / (1024 * 1024)).toFixed(1)} MB`;
}

function kind(entry) {
  if (entry.type === 'dir') return entry.mounted ? 'Folder, read-only' : 'Folder';
  if (entry.type === 'link') return `Shortcut to ${entry.target}`;
  if (entry.mounted) return 'File, read-only';
  return 'File';
}

export default {
  id: 'explorer',
  name: 'Explorer',
  icon: 'folder',
  onDesktop: true,
  width: 660,
  height: 420,
  status: true,
  styles: ['app.css'],

  async mount({ win, desktop, args }) {
    const [{ openFileSystem }, { HOME }] = await Promise.all([
      import('../../shared/fs/fs.mjs'),
      import('../../shared/fs/seed.mjs'),
    ]);
    const fs = await openFileSystem();

    const root = document.createElement('div');
    root.className = 'exp';

    /* ------------------------------ the toolbar ------------------------------ */

    const bar = document.createElement('form');
    bar.className = 'exp-bar';

    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'exp-up';
    up.textContent = 'Up';
    up.title = 'Go to the parent folder';

    const label = document.createElement('label');
    label.className = 'exp-address-label';
    label.textContent = 'Address';
    const address = document.createElement('input');
    address.type = 'text';
    address.className = 'exp-address';
    address.spellcheck = false;
    address.autocomplete = 'off';
    address.id = `${win.id}-address`;
    label.htmlFor = address.id;

    bar.append(up, label, address);

    /* ------------------------------ the list ------------------------------ */

    const table = document.createElement('div');
    table.className = 'exp-list';
    table.setAttribute('role', 'listbox');
    table.setAttribute('aria-label', 'Folder contents');

    const head = document.createElement('div');
    head.className = 'exp-head';
    for (const column of COLUMNS) {
      const cell = document.createElement('span');
      cell.className = `exp-cell exp-${column.toLowerCase()}`;
      cell.textContent = column;
      head.append(cell);
    }

    root.append(bar, head, table);

    /* ------------------------------ navigation ------------------------------ */

    let here = (args && args.path) || HOME;
    let selected = null;

    const open = async (entry, path) => {
      // A symlink is followed on open and described in the list: `realpath` is what the
      // other window needs, because it has its own working directory and a relative target
      // would mean something else there.
      let real;
      let stat;
      try {
        real = fs.realpath(path);
        stat = fs.stat(real);
      } catch (error) {
        win.setStatus(`${entry.name} — ${error.message}`);
        return;
      }
      if (stat.type === 'dir') {
        show(real);
        return;
      }
      // Notepad is the only thing that opens a file today, and it hydrates a mounted one
      // itself. When an app can declare what it opens, this becomes a lookup rather than a
      // name written here.
      await desktop.launch('notepad', { path: real });
      win.setStatus(`Opened ${real}`);
    };

    const select = (entry, row) => {
      selected = entry;
      for (const other of table.querySelectorAll('.exp-row')) {
        other.setAttribute('aria-selected', String(other === row));
      }
      const detail = entry.type === 'link'
        ? `${entry.name} → ${entry.target}`
        : `${entry.name} · ${size(entry) || `${entry.size} items`} · ${entry.modeString}`;
      win.setStatus(detail);
    };

    function show(path) {
      let entries;
      try {
        entries = fs.list(path);
      } catch (error) {
        // An unreadable directory is a fact to print, not an empty list. A folder window
        // that looks finished and is empty is the failure this project keeps finding.
        table.replaceChildren(message(`${path} — ${error.message}`));
        win.setStatus(error.message);
        here = path;
        address.value = path;
        win.setTitle(`${path} - Explorer`);
        up.disabled = path === '/';
        return;
      }

      here = path;
      address.value = path;
      selected = null;
      win.setTitle(`${path === '/' ? '/' : path.split('/').pop()} - Explorer`);
      up.disabled = path === '/';

      // Folders first, then by name — Explorer's own order, and the one that makes a deep
      // tree navigable without reading every row.
      entries.sort((a, b) => {
        const kinds = (a.type === 'dir' ? 0 : 1) - (b.type === 'dir' ? 0 : 1);
        return kinds || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
      });

      const rows = entries.map((entry) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'exp-row';
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', 'false');
        row.dataset.name = entry.name;

        const name = document.createElement('span');
        name.className = 'exp-cell exp-name';
        name.append(icon(entry.type === 'dir' ? 'folder' : 'document', 16));
        const text = document.createElement('span');
        text.textContent = entry.name;
        name.append(text);

        const cells = [name];
        for (const [column, value] of [['size', size(entry)], ['type', kind(entry)], ['modified', stamp(entry.mtime)]]) {
          const cell = document.createElement('span');
          cell.className = `exp-cell exp-${column}`;
          cell.textContent = value;
          cells.push(cell);
        }
        row.append(...cells);

        const path = here === '/' ? `/${entry.name}` : `${here}/${entry.name}`;
        row.addEventListener('click', () => select(entry, row));
        row.addEventListener('dblclick', () => open(entry, path));
        row.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          open(entry, path);
        });
        return row;
      });

      table.replaceChildren(...(rows.length ? rows : [message('This folder is empty.')]));
      const dirs = entries.filter((entry) => entry.type === 'dir').length;
      win.setStatus(`${entries.length} item${entries.length === 1 ? '' : 's'}, ${dirs} folder${dirs === 1 ? '' : 's'}`);
    }

    function message(text) {
      const note = document.createElement('p');
      note.className = 'exp-note';
      note.textContent = text;
      return note;
    }

    up.addEventListener('click', () => {
      if (here === '/') return;
      show(here.slice(0, here.lastIndexOf('/')) || '/');
    });

    bar.addEventListener('submit', (event) => {
      event.preventDefault();
      const typed = address.value.trim();
      if (!typed) return;
      try {
        const real = fs.realpath(typed, { cwd: here });
        if (fs.stat(real).type !== 'dir') {
          win.setStatus(`${typed} is not a folder`);
          address.value = here;
          return;
        }
        show(real);
      } catch (error) {
        // The typed path stays in the box so it can be corrected rather than retyped.
        win.setStatus(error.message);
      }
    });

    /* ------------------------------ live state ------------------------------ */

    // The filesystem is shared. A `touch` in a terminal, a save in Notepad, or a `format`
    // all change what this window is showing, and a folder that needs a refresh button is a
    // folder that is lying between clicks.
    const unwatch = fs.watch(() => {
      show(fs.exists(here) ? here : HOME);
    });
    win.onClose = unwatch;

    win.app = { show, table, address, up, get here() { return here; }, get selected() { return selected; } };
    show(here);
    return root;
  },

  /** The list takes the keyboard, so the first row is reachable without a click. */
  ready({ win }) {
    const first = win.app.table.querySelector('.exp-row');
    if (first) first.focus();
  },
};
