/* Explorer — a folder window. Lists a directory, opens what you double-click.
 *
 * The filesystem is the subject here, not a model of it: every row is an `lstat` of a real
 * entry, and a change made in a terminal appears without a refresh because this window
 * watches the filesystem it is listing. That is the whole reason the desktop is being built
 * on top of `shared/fs/` rather than beside it.
 *
 * ## Sizing
 *
 * No JavaScript sizes anything. What fills the window is two rules in `app.css`:
 * `.window-body.app-explorer` is a flex column with `min-height: 0`, on top of
 * `.window-body { flex: 1 1 auto; min-height: 0 }` in `src/window.css`, and `.exp-list` is
 * `flex: 1; min-height: 0; overflow-y: auto`. That survives a drag-resize, a maximise and a
 * restore with no listener and no second copy of the layout's answer in JS.
 *
 * (The container query at the foot of `app.css` is what `container: window / inline-size`
 * provides, and it drops columns in a narrow window. It has nothing to do with filling the
 * block axis — an earlier version of this comment credited it with both.)
 *
 * ## What a row knows
 *
 * `lstat`, never `stat`: a symlink lists as a symlink and says where it points, the same way
 * `ls -l` does. Opening one follows it, because that is what double-clicking a shortcut
 * means — the distinction is between describing a name and using it.
 */

import { icon } from '../../src/icons.mjs';

//: The columns, once. `key` is the cell's class suffix and `label` is its heading, so
//: renaming a heading cannot leave the header and the rows disagreeing about a class — which
//: would hide a row cell while leaving its heading, and wrap the header into the grid's next
//: line.
const COLUMNS = [
  { key: 'name', label: 'Name' },
  { key: 'size', label: 'Size' },
  { key: 'type', label: 'Type' },
  { key: 'modified', label: 'Modified' },
];

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** `Nov 9 14:01`. No padding: HTML collapses it, and `.exp-modified` is tabular instead. */
function stamp(ms) {
  const date = new Date(ms);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${MONTHS[date.getMonth()]} ${date.getDate()} ${hh}:${mm}`;
}

function size(entry) {
  if (entry.type === 'dir') return '';
  // Characters, not bytes: a file here holds a string, and the index counts the same way.
  if (entry.size < 1024) return `${entry.size} chars`;
  if (entry.size < 1024 * 1024) return `${(entry.size / 1024).toFixed(1)}K`;
  return `${(entry.size / (1024 * 1024)).toFixed(1)}M`;
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
    const [{ openFileSystem, hydrate }, { HOME }, { normalize, joinPath }] = await Promise.all([
      import('../../shared/fs/fs.mjs'),
      import('../../shared/fs/seed.mjs'),
      import('../../shared/fs/vfs.mjs'),
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
      cell.className = `exp-cell exp-${column.key}`;
      cell.textContent = column.label;
      head.append(cell);
    }

    root.append(bar, head, table);

    /* ------------------------------ navigation ------------------------------ */

    // A relative `args.path` would resolve against `/` inside `fs.list`, and a trailing
    // slash made the title the empty string.
    const asked = args && args.path ? String(args.path) : HOME;
    let here = normalize(asked.startsWith('/') ? asked : joinPath(HOME, asked));
    let selected = null;

    const title = (path) => (path === '/' ? '/' : path.slice(path.lastIndexOf('/') + 1));

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
        win.setStatus(error.message);
        return;
      }
      if (stat.type === 'dir') {
        show(real);
        return;
      }

      // The read Notepad is about to do, done here first.
      //
      // `launch` turns a failed mount into a window that says so and returns normally, so
      // awaiting it says nothing about whether the file opened — this window used to report
      // `Opened …` over an editor showing `Permission denied`. Attempting the same read is
      // also better placed: the refusal appears where the double-click happened.
      try {
        await hydrate(fs, [real]);
        fs.readFile(real);
      } catch (error) {
        win.setStatus(error.message);
        return;
      }
      await desktop.launch('notepad', { path: real });
      win.setStatus(`Opened ${real}`);
    };

    const select = (entry, row) => {
      selected = entry;
      for (const other of table.querySelectorAll('.exp-row')) {
        const isTarget = other === row;
        other.setAttribute('aria-selected', String(isTarget));
        // A roving tabindex: one stop for the whole listbox, not one per file. A folder of
        // 41 files put 41 tab stops between the address bar and the rest of the desktop.
        other.tabIndex = isTarget ? 0 : -1;
      }
      const detail = entry.type === 'link'
        ? `${entry.name} → ${entry.target}`
        : `${entry.name} · ${size(entry) || `${entry.size} items`} · ${entry.modeString}`;
      win.setStatus(detail);
    };

    function show(path) {
      // A half-typed path survives another window's write. The watcher re-lists on every
      // filesystem event, and in a shared filesystem that is whenever anything else writes —
      // which used to wipe the address box mid-edit.
      const editing = document.activeElement === address;
      const refocus = document.activeElement
        && document.activeElement.classList
        && document.activeElement.classList.contains('exp-row')
        ? document.activeElement.dataset.name
        : null;

      let entries;
      try {
        entries = fs.list(path);
      } catch (error) {
        // An unreadable directory is a fact to print, not an empty list. A folder window
        // that looks finished and is empty is the failure this project keeps finding.
        // `error.message` already starts with the path, so it is not prefixed again.
        table.replaceChildren(message(error.message));
        win.setStatus(error.message);
        here = path;
        if (!editing) address.value = path;
        win.setTitle(`${title(path)} - Explorer`);
        up.disabled = path === '/';
        return;
      }

      here = path;
      if (!editing) address.value = path;
      selected = null;
      win.setTitle(`${title(path)} - Explorer`);
      up.disabled = path === '/';

      // Folders first, then by name — Explorer's own order, and the one that makes a deep
      // tree navigable without reading every row.
      entries.sort((a, b) => {
        const kinds = (a.type === 'dir' ? 0 : 1) - (b.type === 'dir' ? 0 : 1);
        return kinds || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
      });

      const rows = entries.map((entry, index) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'exp-row';
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', 'false');
        row.dataset.name = entry.name;
        row.tabIndex = index === 0 ? 0 : -1;

        const path = here === '/' ? `/${entry.name}` : `${here}/${entry.name}`;

        const cells = COLUMNS.map((column) => {
          const cell = document.createElement('span');
          cell.className = `exp-cell exp-${column.key}`;
          if (column.key === 'name') {
            cell.append(icon(glyph(entry, path), 16));
            const text = document.createElement('span');
            text.className = 'exp-label';
            text.textContent = entry.name;
            cell.append(text);
          } else {
            cell.textContent = column.key === 'size' ? size(entry)
              : column.key === 'type' ? kind(entry)
                : stamp(entry.mtime);
          }
          return cell;
        });
        row.append(...cells);

        row.addEventListener('click', () => select(entry, row));
        row.addEventListener('dblclick', () => open(entry, path));
        row.addEventListener('keydown', (event) => {
          // Enter opens. Space is left alone on purpose: a button activates on Space, which
          // fires the click that selects — cancelling it meant a keyboard user could never
          // select a row at all, and `aria-selected` stayed false on every one of them.
          if (event.key !== 'Enter') return;
          event.preventDefault();
          open(entry, path);
        });
        return row;
      });

      table.replaceChildren(...(rows.length ? rows : [message('This folder is empty.')]));

      // `replaceChildren` destroyed the focused row, and nothing put focus back: a keyboard
      // user landed on BODY after every navigation and after every write another window made.
      if (refocus) {
        const again = [...table.querySelectorAll('.exp-row')]
          .find((row) => row.dataset.name === refocus) || table.querySelector('.exp-row');
        if (again) {
          again.tabIndex = 0;
          again.focus();
        }
      }

      const dirs = entries.filter((entry) => entry.type === 'dir').length;
      win.setStatus(`${entries.length} item${entries.length === 1 ? '' : 's'}, ${dirs} folder${dirs === 1 ? '' : 's'}`);
    }

    /** The icon for a row. A shortcut to a folder draws a folder. */
    function glyph(entry, path) {
      if (entry.type === 'dir') return 'folder';
      if (entry.type === 'link') {
        try {
          return fs.stat(path).type === 'dir' ? 'folder' : 'document';
        } catch {
          // A broken shortcut points at nothing, so there is nothing to take the icon from.
          return 'document';
        }
      }
      return 'document';
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
      // `fs.exists` rethrows EACCES rather than answering false, so asking it here threw out
      // of the listener and left the window showing a folder it could no longer read — the
      // exact "looks finished and is wrong" failure, reached through the watcher. Only
      // absence sends this window home; a refusal is something `show` prints.
      try {
        fs.stat(here);
      } catch (error) {
        if (error.code === 'ENOENT' || error.code === 'ENOTDIR') {
          show(fs.exists(HOME) ? HOME : '/');
          return;
        }
      }
      show(here);
    });
    win.onClose = unwatch;

    win.app = { show, table };
    show(here);
    return root;
  },

  /** The list takes the keyboard, so the first row is reachable without a click. */
  ready({ win }) {
    const first = win.app.table.querySelector('.exp-row');
    if (first) first.focus();
  },
};
