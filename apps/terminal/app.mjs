/* Terminal — the pane, the prompt line, and the keys. The language is `shell.mjs`.
 *
 * The chrome is XP's and the shell is POSIX, which is a deliberate mismatch: the ask was
 * Linux commands, and calling the window "Command Prompt" while `ls` and `grep` work would
 * be the wrong label on the right program.
 *
 * ## What lives here and what does not
 *
 * This file owns the DOM, the scrollback, history keys, Tab completion and the status bar.
 * It knows nothing about any command. `shell.mjs` parses a line and runs it, writing through
 * three callbacks — `write`, `warn`, `control` — so a command cannot reach the pane except
 * through them, and the same shell can be driven by a check with no DOM at all.
 *
 * Two windows of this app are two sessions over one filesystem: each has its own working
 * directory, history and scrollback, and both see the same files. That is the point of the
 * single mount in `shared/fs/fs.mjs`, and it is what `open` depends on.
 */

//: The character width of the pane is read from a probe rather than assumed, because `ls`
//: lays its columns out to fit and a wrong width is a listing that wraps.
const PROBE_TEXT = '0123456789';

//: Scrollback ceiling, in blocks — one block is one `write` from one command. A session that
//: runs `find /` in a loop would otherwise grow the DOM until the tab slows down, which looks
//: like the shell getting slower rather than like a window that is keeping everything.
const MAX_BLOCKS = 4000;
const TRIM_BLOCKS = 1000;

export default {
  id: 'terminal',
  name: 'Terminal',
  icon: 'terminal',
  onDesktop: true,
  width: 620,
  height: 400,
  status: true,
  styles: ['app.css'],

  async mount({ win, desktop, args }) {
    // Dynamic, so the whole desktop does not pay for the filesystem at startup just because
    // every `app.mjs` is imported to build the Start menu. See `docs/apps.md`.
    const [{ openFileSystem, persistence }, { Shell }, { HOME }] = await Promise.all([
      import('../../shared/fs/fs.mjs'),
      import('./shell.mjs'),
      import('../../shared/fs/seed.mjs'),
    ]);
    const fs = await openFileSystem();

    const root = document.createElement('div');
    root.className = 'term';

    const log = document.createElement('div');
    log.className = 'term-log';
    log.setAttribute('role', 'log');
    // Output is announced as it arrives. `polite`, because a command that prints a hundred
    // lines must not interrupt the reader mid-sentence.
    log.setAttribute('aria-live', 'polite');
    log.setAttribute('aria-label', 'Terminal output');

    const form = document.createElement('form');
    form.className = 'term-line';
    const label = document.createElement('label');
    label.className = 'term-prompt';
    const input = document.createElement('input');
    input.className = 'term-input';
    input.type = 'text';
    input.autocomplete = 'off';
    input.autocapitalize = 'off';
    input.spellcheck = false;
    input.id = `${win.id}-input`;
    label.htmlFor = input.id;
    form.append(label, input);

    // Measured, never guessed. A span of known length inside the pane, with the pane's own
    // font, divided by its length.
    const probe = document.createElement('span');
    probe.className = 'term-probe';
    probe.textContent = PROBE_TEXT;
    probe.setAttribute('aria-hidden', 'true');

    root.append(log, form, probe);

    let columns = 80;
    const measure = () => {
      const charWidth = probe.getBoundingClientRect().width / PROBE_TEXT.length;
      const paneWidth = log.clientWidth;
      if (charWidth > 0 && paneWidth > 0) columns = Math.max(20, Math.floor(paneWidth / charWidth));
      return columns;
    };

    const shell = new Shell({
      fs,
      desktop,
      win,
      cwd: (args && args.cwd) || HOME,
      columns: () => columns,
    });

    /* ----------------------------- the pane ----------------------------- */

    const append = (text, className) => {
      if (!text) return;
      const block = document.createElement('span');
      if (className) block.className = className;
      // Text, never markup: the content is file contents and command output, and one
      // `innerHTML` here would make every file in the filesystem a script.
      block.textContent = text;
      log.append(block);
      // Trimmed in batches rather than one block per write, so appending stays O(1) in the
      // common case instead of removing a node on every line of a long listing.
      if (log.childElementCount > MAX_BLOCKS) {
        for (let i = 0; i < TRIM_BLOCKS && log.firstElementChild; i += 1) {
          log.firstElementChild.remove();
        }
      }
      log.scrollTop = log.scrollHeight;
    };

    const io = {
      write: (text) => append(text, null),
      warn: (text) => append(text, 'term-error'),
      control: (name) => {
        if (name === 'clear') log.replaceChildren();
        else if (name === 'exit') win.close();
      },
    };

    const refreshPrompt = () => {
      label.textContent = shell.prompt();
    };

    const refreshStatus = () => {
      const stats = fs.stats();
      const state = persistence.error
        ? `not saving — ${persistence.error}`
        : `saved ${persistence.saves}x`;
      win.setStatus(`${shell.cwd} · ${stats.files} files, ${stats.dirs} dirs, ${stats.links} links · ${state}`);
    };

    /* ----------------------------- running ----------------------------- */

    //: Where Up and Down are in `shell.history`, or null when the line is being typed fresh.
    let historyAt = null;
    let busy = false;
    const submit = async () => {
      const line = input.value;
      input.value = '';
      append(`${shell.prompt()}${line}\n`, 'term-echo');
      historyAt = null;
      if (!line.trim()) return;
      busy = true;
      try {
        await shell.run(line, io);
      } finally {
        busy = false;
        refreshPrompt();
        refreshStatus();
        input.focus();
      }
    };

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      // A second Enter while a command is still running would interleave two commands'
      // output in one pane. There is one session per window, so the line waits.
      if (!busy) submit();
    });

    /* ----------------------------- the keys ----------------------------- */

    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
        const history = shell.history;
        if (!history.length) return;
        event.preventDefault();
        if (historyAt === null) historyAt = history.length;
        historyAt += event.key === 'ArrowUp' ? -1 : 1;
        historyAt = Math.max(0, Math.min(history.length, historyAt));
        input.value = historyAt === history.length ? '' : history[historyAt];
        // The caret goes to the end, which is where a recalled line is edited from.
        input.setSelectionRange(input.value.length, input.value.length);
        return;
      }

      if (event.key === 'Tab') {
        event.preventDefault();
        const { matches, prefix } = shell.complete(input.value);
        if (!matches.length) return;
        const shared = commonPrefix(matches);
        if (shared.length > prefix.length || matches.length === 1) {
          const head = input.value.slice(0, input.value.length - prefix.length);
          input.value = head + shared + (matches.length === 1 && !shared.endsWith('/') ? ' ' : '');
        } else {
          // Two Tabs' worth of information in one: the candidates, printed the way a shell
          // prints them, with the prompt re-echoed so the line being edited is still visible.
          append(`${shell.prompt()}${input.value}\n`, 'term-echo');
          append(`${matches.join('  ')}\n`, null);
        }
        return;
      }

      if (event.ctrlKey && (event.key === 'l' || event.key === 'L')) {
        event.preventDefault();
        log.replaceChildren();
        return;
      }

      if (event.ctrlKey && (event.key === 'c' || event.key === 'C')) {
        // Only when there is a line to abandon; otherwise this is a copy, and stealing it
        // would break the one shortcut everyone has in their hands.
        if (!input.value) return;
        event.preventDefault();
        append(`${shell.prompt()}${input.value}^C\n`, 'term-echo');
        input.value = '';
        historyAt = null;
      }
    });

    /* ----------------------------- the banner ----------------------------- */

    try {
      append(fs.readFile('/etc/motd'), null);
    } catch (error) {
      // A filesystem with no readable motd is not a broken terminal, so the banner is
      // skipped. Any other reason is named: swallowed, a defect in reading a file would
      // look exactly like a tree that ships without one.
      if (!['ENOENT', 'ENOTDIR', 'EACCES', 'EISDIR'].includes(error.code)) {
        console.error('terminal: /etc/motd could not be read', error);
      }
    }
    append(`${persistence.source === 'restored' ? 'Filesystem restored from this browser.' : 'Fresh filesystem.'} `
      + "Type 'help'.\n\n", 'term-note');

    refreshPrompt();
    refreshStatus();

    /* ----------------------------- live state ----------------------------- */

    // The filesystem is shared, so this window's working directory can be removed by another
    // one — or by `format`. Watched rather than discovered on the next command, which would
    // report ENOENT for a directory the reader never left.
    const unwatch = fs.watch(() => {
      // The status bar first, because the probe below can refuse: `fs.exists` rethrows
      // EACCES, and a throw here left the bar and the prompt frozen on the values they had
      // when an ancestor of the working directory was last readable — for the rest of the
      // session, with nothing said in the pane.
      refreshStatus();
      let gone;
      try {
        gone = !fs.exists(shell.cwd);
      } catch (error) {
        // A directory inside something this session may not look into is not a removed
        // directory; it is an unavailable answer, and moving the reader on one would strand
        // them somewhere they never went. Anything else is rethrown, which `fs.watch`
        // reports to the console rather than letting it abort the write.
        if (error.code !== 'EACCES') throw error;
        return;
      }
      if (!gone) return;
      const next = fs.exists(HOME) ? HOME : '/';
      // The destination is read from the decision, not restated as `HOME`: when home is
      // gone too this moves to `/`, and a message that said otherwise would be a report of
      // something that did not happen.
      append(`terminal: ${shell.cwd} is gone; moved to ${next}\n`, 'term-error');
      shell.cwd = next;
      refreshPrompt();
      refreshStatus();
    });

    const observer = new ResizeObserver(() => measure());
    observer.observe(log);

    // If you start it, stop it: both of these outlive the window otherwise, and the watcher
    // would keep writing into a detached pane for the rest of the session.
    win.onClose = () => {
      unwatch();
      observer.disconnect();
    };

    win.app = { shell, input, log, measure };
    return root;
  },

  /** `mount` cannot measure anything, and the column count is a measurement. */
  ready({ win }) {
    win.app.measure();
    win.app.input.focus();
  },
};

/** The longest string every candidate starts with. */
function commonPrefix(values) {
  let prefix = values[0];
  for (const value of values.slice(1)) {
    while (prefix && !value.startsWith(prefix)) prefix = prefix.slice(0, -1);
  }
  return prefix;
}
