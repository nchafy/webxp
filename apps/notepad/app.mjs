/* Notepad — content that has to reflow on every resize frame, and the filesystem's editor.
 *
 * Not a singleton, on purpose: a second Notepad proves windows of one app are independent,
 * and `open a.txt` followed by `open b.txt` has to give two documents rather than one window
 * that changed its mind.
 *
 * With no `args.path` it is a scratch pad and nothing is saved. Launched with one — which is
 * what the terminal's `open` does — it reads that file out of the shared filesystem and
 * Ctrl+S writes it back. The filesystem is the same instance the terminal holds, so a save
 * here is a `cat` there with no refresh and no message passing between the two windows.
 */

const SCRATCH = 'Type here. Open a second Notepad from the Start menu and drag it off '
  + 'this one.\n\nThe text reflows on every resize frame.\n\nThere is a filesystem behind '
  + 'the Terminal. Type "open readme.txt" there and this window gets a real file.\n';

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
    const path = (args && args.path) || null;

    const area = document.createElement('textarea');
    area.className = 'notepad-area';
    area.setAttribute('aria-label', path ? `Contents of ${path}` : 'Document text');
    area.spellcheck = false;

    let fs = null;
    if (path) {
      // Dynamic, so a desktop whose reader never opens a file never loads the filesystem.
      const { openFileSystem } = await import('../../shared/fs/fs.mjs');
      fs = await openFileSystem();
      // A read that fails here throws, and the loader turns that into a window naming the
      // program and the reason. That is the honest outcome: an editor that opened an empty
      // buffer for an unreadable file would offer to save over it.
      area.value = fs.readFile(path);
      win.setTitle(`${path.split('/').pop()} - Notepad`);
    } else {
      area.value = SCRATCH;
    }

    //: When this window last agreed with the file. The filesystem is shared, so the terminal
    //: can write the same path while the buffer sits here; saving over that silently would
    //: lose a write this window never saw.
    let readAt = path ? fs.lstat(path).mtime : 0;
    let overwrite = false;
    let dirty = false;
    const report = () => {
      if (!path) {
        win.setStatus(`${area.value.length} characters`);
        return;
      }
      win.setStatus(`${path} · ${area.value.length} characters · ${dirty ? 'Ctrl+S to save' : 'saved'}`);
    };

    const save = () => {
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
        win.setTitle(`${path.split('/').pop()} - Notepad`);
        report();
      } catch (error) {
        // A full or unwritable filesystem is a thing to say, in the one place the window has
        // to say things. Swallowing it would leave the title's asterisk as the only hint.
        win.setStatus(`${path} · not saved — ${error.message}`);
        console.error('notepad: save failed', error);
      }
    };

    area.addEventListener('input', () => {
      dirty = true;
      report();
      // The title tracks the document, and the taskbar button tracks the title. XP put the
      // asterisk nowhere, but it is the one thing the title bar is for.
      if (path) win.setTitle(`*${path.split('/').pop()} - Notepad`);
      else win.setTitle(area.value ? 'Document - Notepad' : 'Untitled - Notepad');
    });

    area.addEventListener('keydown', (event) => {
      if (!path) return;
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
      // The browser's own Save dialog is not what Ctrl+S means inside an editor.
      event.preventDefault();
      save();
    });

    report();
    win.app = { area, save, path };
    return area;
  },
};
