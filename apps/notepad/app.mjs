/* Notepad — content that has to reflow on every resize frame, and the filesystem's editor.
 *
 * Not a singleton, on purpose: a second Notepad proves windows of one app are independent,
 * and `open a.txt` followed by `open b.txt` has to give two documents rather than one window
 * that changed its mind.
 *
 * With no `args.path` it is a scratch pad. Launched with one — which is what the terminal's
 * `open` does — it reads that file out of the shared filesystem and Ctrl+S writes it back.
 * The filesystem is the same instance the terminal holds, so a save here is a `cat` there
 * with no refresh and no message passing between the two windows.
 *
 * File > Open and File > Save As are the desktop's picker (`src/dialogs.mjs`), not a dialog
 * of this program's own: win32.run's Notepad and Paint carry a copy each of the same picker
 * and the two have already drifted apart.
 */

import { menuBar } from '../../src/widgets.mjs';
import { messageBox, pickFile, pickSaveAs } from '../../src/dialogs.mjs';

const SCRATCH = 'Type here. Open a second Notepad from the Start menu and drag it off '
  + 'this one.\n\nThe text reflows on every resize frame.\n\nThere is a filesystem behind '
  + 'the Terminal. Type "open readme.txt" there and this window gets a real file.\n';

//: The name offered for a document that has never been saved. XP's own.
const UNTITLED = 'Untitled';

//: Save As's type list, XP Notepad's two. Both are applied: the first filters the list to
//: `.txt` and supplies the extension a name typed without one gets, and the second is how a
//: reader saves `notes.md` anyway.
const SAVE_TYPES = [
  { label: 'Text Documents (*.txt)', extensions: ['.txt'] },
  { label: 'All Files', extensions: [] },
];

/** The title bar's name for a path, and the name Save As offers back. */
function nameOf(path) {
  return path ? path.slice(path.lastIndexOf('/') + 1) : UNTITLED;
}

function folderOf(path) {
  const cut = path.lastIndexOf('/');
  return cut > 0 ? path.slice(0, cut) : '/';
}

export default {
  id: 'notepad',
  name: 'Untitled - Notepad',
  filetype: 'notes',
  onDesktop: true,
  width: 440,
  height: 300,
  status: true,
  styles: ['app.css'],

  async mount({ win, args }) {
    // Absolute whoever sent it; a link carries it with the leading slash implied.
    const routes = await import('../../shared/routes/routes.mjs');

    const area = document.createElement('textarea');
    area.className = 'notepad-area';
    area.spellcheck = false;

    //: The document. All five move together — a Save As is a new file, a new agreement about
    //: its mtime, and a clean buffer — so they are set in one place and nowhere else.
    let path = null;
    let writable = true;
    //: When this window last agreed with the file. The filesystem is shared, so the terminal
    //: can write the same path while the buffer sits here; saving over that silently would
    //: lose a write this window never saw.
    let readAt = 0;
    let overwrite = false;
    let dirty = false;

    //: The filesystem, loaded the first time this window needs one — so a desktop whose
    //: reader opens nothing never pays for it — and then held, because the picker, the save
    //: and the conflict check are all the same instance.
    let api = null;
    const filesystem = async () => {
      if (!api) {
        const [{ openFileSystem, hydrate }, { PERM, FsError }] = await Promise.all([
          import('../../shared/fs/fs.mjs'),
          import('../../shared/fs/vfs.mjs'),
        ]);
        api = { fs: await openFileSystem(), hydrate, PERM, FsError };
      }
      return api;
    };

    /**
     * Whether this filesystem will let this window write that path.
     *
     * Only a refusal means read-only. A bare `catch` here would turn any other failure — a
     * renamed `PERM`, a typo in the call — into a read-only editor for every file, with
     * nothing in the console to say why.
     */
    const canWrite = ({ fs, PERM, FsError }, target) => {
      try {
        fs.access(target, PERM.W);
        return true;
      } catch (error) {
        if (!(error instanceof FsError)) throw error;
        return false;
      }
    };

    const report = () => {
      if (!path) {
        win.setStatus(`${area.value.length} characters`);
        return;
      }
      const state = !writable
        ? 'read-only'
        : dirty ? 'Ctrl+S to save' : 'saved';
      win.setStatus(`${path} · ${area.value.length} characters · ${state}`);
    };

    /**
     * Become this file: read it, then adopt it.
     *
     * Every read happens before any state is touched, so a file that cannot be read leaves
     * the buffer exactly as it was. The throw is the caller's to interpret: at mount it has
     * to become a window that says what failed, and from File > Open it is a status line
     * over a document still on screen.
     */
    const openPath = async (next) => {
      const held = await filesystem();
      const { fs, hydrate } = held;
      // A file under `/usr/src/webxp` is mounted: it has a name and a size before it has
      // contents, and reading it without fetching first fails with `EAGAIN`.
      await hydrate(fs, [next]);
      const text = fs.readFile(next);
      // The repository is mounted read-only, so a file can be perfectly readable and
      // unsaveable. Said at the top rather than discovered by a Ctrl+S that fails.
      const allowed = canWrite(held, next);

      path = next;
      writable = allowed;
      area.value = text;
      area.readOnly = !allowed;
      area.setAttribute('aria-label', `Contents of ${next}`);
      readAt = fs.lstat(next).mtime;
      overwrite = false;
      dirty = false;
      win.setTitle(`${nameOf(next)} - Notepad`);
      // A document window's state is the file it holds, so the address is a link to it.
      routes.publish('notepad', { path: next });
      report();
    };

    const save = () => {
      //: `path` is only ever set by a route that loaded the filesystem first, so this is held.
      const { fs } = api;
      try {
        // XP's Notepad would have overwritten it without a word. This asks once: the first
        // Ctrl+S after someone else's write refuses and says so, and the next one goes
        // through, because the reader may well mean to replace it.
        // A file that has been removed is not a conflict: the write puts it back. Only a
        // file that is still there and newer than this buffer is one.
        if (!overwrite && fs.exists(path) && fs.lstat(path).mtime > readAt) {
          overwrite = true;
          win.setStatus(`${path} · changed on disk since it was opened — Ctrl+S again to overwrite`);
          return;
        }
        fs.writeFile(path, area.value);
        readAt = fs.lstat(path).mtime;
        overwrite = false;
        dirty = false;
        win.setTitle(`${nameOf(path)} - Notepad`);
        report();
      } catch (error) {
        // A full or unwritable filesystem is a thing to say, in the one place the window has
        // to say things. Swallowing it would leave the title's asterisk as the only hint.
        win.setStatus(`${path} · not saved — ${error.message}`);
        console.error('notepad: save failed', error);
      }
    };

    /**
     * Save As: the desktop picks the path, this window owns the write.
     *
     * The picker resolves a path whose folder exists and is writable and whose name the
     * reader has agreed to replace — so the conflict guard is moved up to that agreement
     * rather than asking a second time about a file the visitor just approved.
     */
    const saveAs = async () => {
      const held = await filesystem();
      const { fs } = held;
      const target = await pickSaveAs(win, {
        dir: path ? folderOf(path) : undefined,
        name: nameOf(path),
        types: SAVE_TYPES,
      });
      // A cancel is not an error and never a rejection: nothing was asked for, so nothing
      // happens and the buffer is untouched.
      if (target === null) return;

      path = target;
      // An existing name in a writable folder can still be a read-only file. Derived from
      // the target rather than assumed from the folder the picker checked, so the status line
      // is true before the write rather than after it fails.
      writable = !fs.exists(target) || canWrite(held, target);
      area.readOnly = !writable;
      area.setAttribute('aria-label', `Contents of ${target}`);
      readAt = fs.exists(target) ? fs.lstat(target).mtime : 0;
      overwrite = false;
      save();
      // After the write, not before it: a route is a link, and a link to a file the write
      // never managed to create is a dead one.
      if (!dirty) routes.publish('notepad', { path: target });
    };

    //: One command behind two chords and one menu row: a document that has somewhere to go
    //: is written, and one that does not asks where first, as XP's Ctrl+S did.
    const saveOrSaveAs = () => (path ? Promise.resolve(save()) : saveAs());

    /**
     * Offer to keep the buffer before it is thrown away.
     *
     * @returns {Promise<boolean>} Whether to go on with what the reader asked for.
     */
    const agreedToLose = async () => {
      if (!dirty) return true;
      const answer = await messageBox(win, {
        title: 'Notepad',
        text: `Save changes to ${nameOf(path)}?`,
        icon: 'notepad',
        buttons: [
          { id: 'save', text: 'Save', default: true },
          { id: 'discard', text: "Don't Save" },
          { id: 'cancel', text: 'Cancel', cancel: true },
        ],
      });
      if (answer === 'cancel') return false;
      if (answer === 'discard') return true;
      await saveOrSaveAs();
      // Asked of the buffer and not of the answer: a save that hit the conflict guard, that
      // was refused by the filesystem, or whose Save As was cancelled leaves the document
      // dirty, and throwing it away then would lose exactly what the reader asked to keep.
      return !dirty;
    };

    const openFile = async () => {
      if (!(await agreedToLose())) return;
      // No `extensions`: this editor opens whatever this filesystem can read, and that is
      // the filesystem's `text` bit rather than an extension — `shared/fs/vfs.mjs:459`
      // refuses a `.gz` with its own sentence, and the picker reads the file before it
      // resolves, so that refusal lands in the picker where the press was.
      const target = await pickFile(win, { dir: path ? folderOf(path) : undefined });
      if (target === null) return;
      try {
        await openPath(target);
      } catch (error) {
        // The picker read the file before resolving it, so this is a file that changed in
        // between. Reported over the document that is still on screen, which is untouched.
        win.setStatus(`${target} · not opened — ${error.message}`);
        console.error('notepad: open failed', error);
      }
    };

    const exit = async () => {
      if (!(await agreedToLose())) return;
      win.close();
    };

    /**
     * Run a menu command, and say so when it fails.
     *
     * `menuBar` calls `onSelect` and does not await it, so an async handler that throws is an
     * unhandled rejection: no status line, no window, nothing on screen at all.
     */
    const guard = (what, job) => () => {
      Promise.resolve().then(job).catch((error) => {
        win.setStatus(`${what} failed — ${error.message}`);
        console.error(`notepad: ${what} failed`, error);
      });
    };

    const menu = menuBar({
      // The window's client area, so a chord works wherever focus is in this window and
      // nowhere else.
      scope: win.bodyEl,
      label: 'Notepad menu bar',
      menus: [{
        label: '&File',
        groups: [
          [{ label: '&Open…', accel: 'Ctrl+O', onSelect: guard('Open', openFile) }],
          [
            { label: '&Save', accel: 'Ctrl+S', onSelect: guard('Save', saveOrSaveAs) },
            { label: 'Save &As…', onSelect: guard('Save As', saveAs) },
          ],
          [{ label: 'E&xit', onSelect: guard('Exit', exit) }],
        ],
      }],
    });

    const frame = document.createElement('div');
    frame.className = 'notepad-frame';
    // Two rows: the bar's own height, and whatever is left for the document.
    //
    // `app.css` sizes the text area with `height: 100%` and `.window-body` is a block box, so
    // the bar and a full-height text area together overflow the client area by the height of
    // the bar — the last line of the document scrolled out of reach, under a scrollbar the
    // window never had before. The declaration belongs in `apps/notepad/app.css`, which is
    // outside this change; the line that replaces this is
    // `.app-notepad .notepad-frame { display: grid; grid-template-rows: auto minmax(0, 1fr); height: 100% }`.
    // Written through the CSSOM and not as a `style` attribute, for the reason `window.mjs`
    // writes geometry that way: a parsed `style` attribute is what CSP's `style-src` blocks.
    frame.style.setProperty('display', 'grid');
    frame.style.setProperty('grid-template-rows', 'auto minmax(0, 1fr)');
    frame.style.setProperty('height', '100%');
    frame.append(menu, area);

    if (args && args.path) {
      // A read that fails here throws out of `mount`, and the loader turns that into a window
      // naming the program and the reason. That is the honest outcome: an editor that opened
      // an empty buffer for an unreadable file would offer to save over it.
      await openPath(routes.asAbsolute(String(args.path)));
    } else {
      area.value = SCRATCH;
      area.setAttribute('aria-label', 'Document text');
      report();
    }

    area.addEventListener('input', () => {
      dirty = true;
      report();
      // The title tracks the document, and the taskbar button tracks the title. XP put the
      // asterisk nowhere, but it is the one thing the title bar is for.
      if (path) win.setTitle(`*${nameOf(path)} - Notepad`);
      else win.setTitle(area.value ? 'Document - Notepad' : 'Untitled - Notepad');
    });

    area.addEventListener('keydown', (event) => {
      // Command+S, the macOS spelling of the menu's Ctrl+S. `menuBar` binds the chord it
      // prints and never matches `metaKey` — XP had no Command key — so this is the other
      // half of one command rather than a second binding of one chord. Both would fire the
      // conflict guard twice and overwrite on the first press.
      if (!event.metaKey || event.ctrlKey || event.key.toLowerCase() !== 's') return;
      // The browser's own Save dialog is not what Cmd+S means inside an editor.
      event.preventDefault();
      saveOrSaveAs();
    });

    win.app = {
      area,
      save,
      saveAs,
      openFile,
      exit,
      /** What the document is, for a caller driving this window from outside. */
      info: () => ({ path, dirty, writable }),
    };
    return frame;
  },

  /**
   * The document takes the keyboard, not the File menu.
   *
   * `takeFocus` gives the window's first focus stop the keyboard, and with a menu bar in the
   * client area that is the File title. A new window is for typing in.
   */
  ready({ win }) {
    win.app.area.focus();
  },
};
