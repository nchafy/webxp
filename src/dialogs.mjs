/* The two things a window can ask: a question, and a file.
 *
 * Every other piece of state in this shell goes to a status line or to `console.error`,
 * neither of which can take an answer. A program that needs one — replace this file?, the
 * buffer is dirty, are you sure — needs a question the visitor can answer, and that is a
 * modal dialog parented to the window that asked.
 *
 * `XPWindow.showModal` is the host and owns modality: the scrim, `inert` on the client area,
 * the focus trap, and what Escape means. This file owns the occupants of that layer, and the
 * division is the point: `messageBox` and the file picker share the host, the frame skin, the
 * question body, the button row and the answer-once latch below.
 *
 * The picker is a desktop service and not a dialog each program rebuilds. win32.run proves
 * the factoring by getting it wrong — `src/routes/xp/programs/paint.svelte` and
 * `notepad.svelte` carry a near-identical `pick_file_dialog` each, and a third copy renders
 * the list a third way.
 *
 * Nothing here reaches the page as markup: every node is built with DOM calls and
 * `textContent`.
 */

import { icon as makeIcon, ICON_NAMES } from './icons.mjs';
import { extensionOf } from './apps.mjs';

/**
 * What a dismissed box resolves to when no button is marked `cancel`.
 *
 * A promise that can be left unresolved is a leak, so every box has an answer for "the
 * visitor dismissed it without choosing". This is an id no button may carry, so a caller
 * switching on its own ids falls through to doing nothing rather than into a destructive arm.
 */
export const DISMISSED = 'dismissed';

//: XP drew a message box's icon at 32px, twice the title bar's. It is the only thing in the
//: box that is not text, and at 16px it reads as a bullet.
const ICON_SIZE = 32;

//: The keys a box spec may carry, and the keys one button may carry. Unknown keys are
//: rejected rather than ignored, for the reason `apps.mjs` rejects them in an app spec: the
//: real mistake is `{ id, label }` for `{ id, text }`, and ignoring it renders a box of blank
//: buttons that still resolve.
const SPEC_KEYS = new Set(['title', 'text', 'icon', 'buttons']);
const BUTTON_KEYS = new Set(['id', 'text', 'cancel', 'default']);

let serial = 0;

/**
 * Check a box spec before it reaches a window.
 *
 * @returns {string[]} Reasons it is unusable. Empty means usable.
 */
function validate(spec) {
  if (!spec || typeof spec !== 'object') return [`the spec is ${String(spec)}`];
  const problems = [];

  if (typeof spec.title !== 'string' || !spec.title.trim()) {
    problems.push('title must be a non-empty string — it names the dialog for a screen reader');
  }
  if (typeof spec.text !== 'string' || !spec.text.trim()) {
    problems.push('text must be a non-empty string — it is the question');
  }
  if (spec.icon !== undefined && !ICON_NAMES.includes(spec.icon)) {
    problems.push(`icon ${JSON.stringify(spec.icon)} is not one of: ${ICON_NAMES.join(', ')}`);
  }
  for (const key of Object.keys(spec)) {
    if (!SPEC_KEYS.has(key)) problems.push(`unknown key ${JSON.stringify(key)}`);
  }

  if (!Array.isArray(spec.buttons) || !spec.buttons.length) {
    // A box with no buttons has no answer but Escape, and a question with one answer is not a
    // question. The picker's "replace it?" is the case this exists for.
    problems.push('buttons must be a non-empty array — nothing else can answer the box');
    return problems;
  }

  const seen = new Set();
  spec.buttons.forEach((button, index) => {
    const where = `buttons[${index}]`;
    if (!button || typeof button !== 'object') {
      problems.push(`${where} is ${String(button)}, not a button`);
      return;
    }
    if (typeof button.id !== 'string' || !button.id.trim()) {
      problems.push(`${where} needs an id — it is what the box resolves to`);
    } else if (seen.has(button.id)) {
      problems.push(`${where} repeats the id ${JSON.stringify(button.id)}, so the answer would not say which was pressed`);
    } else if (button.id === DISMISSED) {
      problems.push(`${where} uses ${JSON.stringify(DISMISSED)}, which is what a dismissal resolves to — pressing it and walking away would be the same answer`);
    } else {
      seen.add(button.id);
    }
    if (typeof button.text !== 'string' || !button.text.trim()) {
      problems.push(`${where} needs text — it is the label`);
    }
    for (const key of Object.keys(button)) {
      if (!BUTTON_KEYS.has(key)) problems.push(`${where} has unknown key ${JSON.stringify(key)}`);
    }
  });

  const marked = (flag) => spec.buttons.filter((button) => button && button[flag]).length;
  if (marked('cancel') > 1) {
    problems.push('more than one button marked cancel — a dismissal has one answer');
  }
  if (marked('default') > 1) {
    problems.push('more than one button marked default — one of them takes the focus');
  }
  return problems;
}

/**
 * What Escape and a press on the scrim resolve to.
 *
 * The caller names it by marking a button `cancel`. A box with no such button resolves
 * `DISMISSED`: XP would have refused the dismissal and beeped, and a question with no way out
 * is not something this shell offers from the keyboard.
 */
function dismissalOf(buttons) {
  const cancel = buttons.find((button) => button.cancel);
  return cancel ? cancel.id : DISMISSED;
}

/* ---------------------------------------------------------------------------
   The parts both occupants are made of.
   --------------------------------------------------------------------------- */

/**
 * The dialog frame: the skin, the accessible name, and the bar that carries it.
 *
 * The window frame's own title bar, by class: one declaration of the Luna gradient, the
 * 28px height and the title's ellipsis. It keeps the lit gradient even when the window
 * underneath is asleep, because `window.css` washes a bar that is a *direct child* of an
 * inactive window and this one is not — which is the right answer anyway. A window with a
 * question open is the thing wanting attention.
 *
 * No close box. The buttons are the answers and Escape is the way out; a third route
 * resolving to the same id as Escape is a third route to keep in step, and XP greyed that
 * box out on exactly the boxes where it had nothing to mean.
 *
 * @param {string} id     Unique per dialog: it is the stem of every `aria-` reference in it.
 * @param {string} title
 * @returns {{root: HTMLElement, bar: HTMLElement}}
 */
function dialogFrame(id, title) {
  const root = document.createElement('div');
  root.className = 'dialog';
  root.setAttribute('role', 'dialog');
  // `aria-modal` is the half of modality the markup carries and `inert` on the window's body
  // is the half the browser enforces. Both: a screen reader in its own browse mode walks the
  // tree rather than following the focus rules.
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-labelledby', `${id}-title`);

  const bar = document.createElement('div');
  bar.className = 'window-title';
  const heading = document.createElement('h2');
  heading.id = `${id}-title`;
  heading.textContent = title;
  bar.append(heading);
  root.append(bar);
  return { root, bar };
}

/**
 * A question: an icon at 32px, and the text that is the question.
 *
 * The text node carries the id, so the dialog it goes into can point `aria-describedby` at
 * it. Both occupants want that — the picker's "replace it?" is the same question in the same
 * shape as a message box's, and rendering it twice is how the two would drift apart.
 */
function questionBody(id, { text, icon }) {
  const body = document.createElement('div');
  body.className = 'dialog-body';
  if (icon) body.append(makeIcon(icon, ICON_SIZE));
  const prose = document.createElement('p');
  prose.className = 'dialog-text';
  prose.id = `${id}-text`;
  prose.textContent = text;
  body.append(prose);
  return body;
}

/**
 * The row of answers, left to right, with `autofocus` on the one Enter would take.
 *
 * `autofocus` is how `showModal` is told which stop is the default, and it is also what
 * marks it on screen: the theme's focus ring is the only "Enter goes here" a box draws.
 *
 * @param {{id: string, text: string, default?: boolean}[]} buttons
 * @param {(id: string) => void} press
 */
function buttonRow(buttons, press) {
  const row = document.createElement('div');
  row.className = 'dialog-buttons';
  const focused = buttons.find((button) => button.default) || buttons[0];
  for (const button of buttons) {
    const el = document.createElement('button');
    el.type = 'button';
    el.textContent = button.text;
    // The answer, on the element that produces it: it is how a check — or a caller driving
    // its own UI test — addresses one button of the row.
    el.dataset.id = button.id;
    if (button === focused) el.autofocus = true;
    el.addEventListener('click', () => press(button.id));
    row.append(el);
  }
  return row;
}

/**
 * One answer, once, from an occupant of a window's modal layer.
 *
 * The latch is the whole mechanism. `closeModal` calls `onDismiss` on every route the layer
 * comes down by, including the one a pressed button took through here — so without it a
 * press would be overwritten by the dismissal that follows it, and the caller would be told
 * the visitor walked away from a button they had just pressed. Closing first and settling
 * second is what lets an awaiting handler touch a window that is no longer waiting.
 *
 * It lives here, shared, because the host offers no way to tell "I closed it" from "the
 * visitor dismissed it": every occupant of the layer needs this guard, and two copies of it
 * is two places for the same off-by-one to come back.
 */
function oneAnswer(win) {
  let settle;
  const answer = new Promise((resolve) => { settle = resolve; });
  let answered = false;
  const finish = (value) => {
    if (answered) return;
    answered = true;
    win.closeModal();
    settle(value);
  };
  return { answer, finish, answered: () => answered };
}

/**
 * Ask a question in the window that is asking it.
 *
 * @param {object} win                  The `XPWindow` the dialog belongs to.
 * @param {object} spec
 * @param {string} spec.title           The dialog's title bar, and its accessible name.
 * @param {string} spec.text            The question.
 * @param {string} [spec.icon]          A name `icons.mjs` knows, drawn at 32px.
 * @param {{id: string, text: string, cancel?: boolean, default?: boolean}[]} spec.buttons
 *        In the order they are drawn, left to right. `default` takes the focus, and the first
 *        button does if none is marked. `cancel` is what a dismissal resolves to.
 * @returns {Promise<string>}           The id of the button pressed, or the dismissal's id.
 * @throws {Error}                      On a spec that cannot be answered, and — out of
 *                                      `showModal` — on a window that is closed or already
 *                                      has a modal up. A malformed question is the caller's
 *                                      defect: rendering it would put a box on screen whose
 *                                      answer the caller cannot handle.
 */
export function messageBox(win, spec) {
  if (!win || typeof win.showModal !== 'function') {
    throw new Error('messageBox: needs the XPWindow the question belongs to');
  }
  const problems = validate(spec);
  if (problems.length) throw new Error(`messageBox: ${problems.join('; ')}`);

  const id = `dialog-${++serial}`;
  const { answer, finish } = oneAnswer(win);

  const { root } = dialogFrame(id, spec.title);
  root.setAttribute('aria-describedby', `${id}-text`);
  root.append(questionBody(id, spec), buttonRow(spec.buttons, finish));

  win.showModal(root, { onDismiss: () => finish(dismissalOf(spec.buttons)) });
  return answer;
}

/* ---------------------------------------------------------------------------
   The file picker.

   One definition, two entry points: `pickFile` and `pickSaveAs` differ in their footer and
   in what the primary button does, and share the places column, the list, the typed-path
   rules and the keyboard.

   What it resolves is an **absolute path**, and `null` on cancel — never a rejection, because
   a cancel is not an error and `try { await pickFile() } catch` is what every caller gets
   wrong. A path rather than contents or a handle: this filesystem is shared and live — a
   folder window watches it (`apps/explorer/app.mjs:340-354`) and a terminal writes it — so a
   handle goes stale between the press and the read, and contents are a copy of a file that
   may have moved on.
   --------------------------------------------------------------------------- */

//: Where the picker's rules live, resolved against this module so the folder moves intact.
//: Linked on first use, as `widgets.menuBar` links `menu.css`: `index.html` links the window
//: manager's sheets, and a program that never asks for a file never needs this one.
const PICKER_STYLESHEET = new URL('./picker.css', import.meta.url).href;

//: The option keys each entry point accepts. Rejected rather than ignored, for the reason
//: `messageBox` rejects them: the real mistake is `{ filter: ['.txt'] }` for `{ extensions:
//: ['.txt'] }`, and ignoring it renders a picker that offers every file and looks deliberate.
const OPEN_KEYS = new Set(['title', 'dir', 'extensions']);
const SAVE_KEYS = new Set(['title', 'dir', 'name', 'types']);

//: What the two dialogs are called when the caller names nothing. XP's own titles.
const OPEN_TITLE = 'Open';
const SAVE_TITLE = 'Save As';

//: The label of the type row when nothing is filtered. XP's own wording, and the entry a
//: `types` list gets for free when it carries no "everything" row of its own.
const ALL_FILES = 'All Files';

let pickerSerial = 0;

/** Link the picker's stylesheet, once, and resolve when it applies. */
function linkPickerStyles() {
  const already = [...document.querySelectorAll('link[rel="stylesheet"]')]
    .find((link) => link.href === PICKER_STYLESHEET);
  // A sheet the host page linked itself is not linked twice, and a second picker in the same
  // session waits on nothing.
  if (already) return Promise.resolve();
  return new Promise((resolve) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = PICKER_STYLESHEET;
    // Awaited, not fired and forgotten: the dialog is measured by the layout the moment
    // `showModal` puts it on screen, and an unstyled picker is a 400px column of plain
    // buttons that reads as a bad design rather than as a missing file. A failure resolves
    // anyway — a picker with no CSS is worse than a picker with no CSS *and* no answer.
    link.addEventListener('load', () => resolve());
    link.addEventListener('error', () => {
      console.error(`webxp: the picker stylesheet failed to load: ${PICKER_STYLESHEET}`);
      resolve();
    });
    document.head.append(link);
  });
}

/**
 * The extensions a filter is made of: lower case, leading dot, in the caller's order.
 *
 * `extensionOf` in `apps.mjs` decides what a path's extension *is*, and this is the only
 * other place that word is used — so the filter and the file-association table agree about
 * `.gpx.gz` and about `.bashrc` by construction rather than by coincidence.
 */
function extensionList(value, where) {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new Error(`${where} must be an array of extensions`);
  return value.map((entry, index) => {
    if (typeof entry !== 'string' || !entry.trim()) {
      throw new Error(`${where}[${index}] is ${String(entry)}, not an extension`);
    }
    const ext = entry.trim().toLowerCase();
    if (!ext.startsWith('.') || ext.length < 2) {
      // `'txt'` for `'.txt'` would filter every file out and look like an empty folder.
      throw new Error(`${where}[${index}] is ${JSON.stringify(entry)} — an extension starts with a dot`);
    }
    return ext;
  });
}

/** The type rows of a Save As dialog: a label, and the extensions it is. */
function typeList(value) {
  if (value === undefined) return [{ label: ALL_FILES, extensions: [] }];
  if (!Array.isArray(value) || !value.length) {
    throw new Error('pickSaveAs: types must be a non-empty array of { label, extensions }');
  }
  return value.map((entry, index) => {
    const where = `pickSaveAs: types[${index}]`;
    if (!entry || typeof entry !== 'object') throw new Error(`${where} is ${String(entry)}`);
    if (typeof entry.label !== 'string' || !entry.label.trim()) {
      throw new Error(`${where} needs a label — it is what the dropdown reads`);
    }
    for (const key of Object.keys(entry)) {
      if (key !== 'label' && key !== 'extensions') {
        throw new Error(`${where} has unknown key ${JSON.stringify(key)}`);
      }
    }
    return { label: entry.label, extensions: extensionList(entry.extensions, `${where}.extensions`) };
  });
}

/**
 * Check a picker's options before a dialog is built from them.
 *
 * Synchronous, and so is the throw: a caller defect must land on the line that made it
 * rather than arriving as a rejection one microtask later, where `catch` is already busy
 * being wrong about cancels.
 */
function pickerSpec(win, mode, options) {
  const where = mode === 'save' ? 'pickSaveAs' : 'pickFile';
  if (!win || typeof win.showModal !== 'function') {
    throw new Error(`${where}: needs the XPWindow the picker belongs to`);
  }
  if (!options || typeof options !== 'object') throw new Error(`${where}: options is ${String(options)}`);
  const allowed = mode === 'save' ? SAVE_KEYS : OPEN_KEYS;
  for (const key of Object.keys(options)) {
    if (!allowed.has(key)) throw new Error(`${where}: unknown option ${JSON.stringify(key)}`);
  }
  if (options.title !== undefined && (typeof options.title !== 'string' || !options.title.trim())) {
    throw new Error(`${where}: title must be a non-empty string`);
  }
  if (options.dir !== undefined && (typeof options.dir !== 'string' || !options.dir.trim())) {
    throw new Error(`${where}: dir must be a non-empty path`);
  }
  if (mode === 'save' && options.name !== undefined
      && (typeof options.name !== 'string' || options.name.includes('/'))) {
    throw new Error('pickSaveAs: name must be a file name, not a path');
  }
  return {
    mode,
    title: options.title || (mode === 'save' ? SAVE_TITLE : OPEN_TITLE),
    dir: options.dir || null,
    name: mode === 'save' ? (options.name || '') : '',
    types: mode === 'save' ? typeList(options.types) : null,
    extensions: mode === 'save' ? [] : extensionList(options.extensions, 'pickFile: extensions'),
  };
}

/** `.txt or .md files`, for the sentence that refuses a file. */
function extensionPhrase(extensions) {
  if (extensions.length === 1) return `a ${extensions[0]} file`;
  return `${extensions.slice(0, -1).join(', ')} or ${extensions[extensions.length - 1]} files`;
}

/** Whether a name is one of the kinds on offer. No extensions means every kind. */
function kindMatches(name, extensions) {
  return !extensions.length || extensions.includes(extensionOf(name));
}

/**
 * XP's Save As appended the type's own extension to a name typed without one, which is why
 * `notes` saved as `notes.txt`. Only when the name carries none: `notes.md` under a `.txt`
 * type is a deliberate choice, and renaming it would be the dialog overruling the reader.
 */
function withExtension(name, extensions) {
  if (!extensions.length || extensionOf(name)) return name;
  return `${name}${extensions[0]}`;
}

/**
 * Where the mounted parts of the tree start.
 *
 * Derived, never listed: win32.run hardcodes four filesystem ids and repeats the list in both
 * of its modals. A mount is grafted by `VFS.mountIndex` or `VFS.mountText`, both of which
 * flag every directory they create — so the topmost flagged directory on each branch *is* the
 * mount, whatever a page chose to graft and wherever it put it.
 *
 * The chain of single-child directories a graft had to invent on the way down is collapsed:
 * `/usr/src` exists only because `/usr/src/webxp` does, and `/usr/src` is not the place a
 * reader means. The walk stops at a mount rather than descending into it, so the repository's
 * thousand files are never listed to find a place to start from.
 */
function mountRoots(fs) {
  const roots = [];
  const descend = (dir) => {
    let entries;
    try {
      entries = fs.list(dir);
    } catch {
      // An unreadable directory has no mounts to report. `/etc/secret.conf`'s folder is the
      // seeded case, and a refusal here is not the picker's to print.
      return;
    }
    for (const entry of entries) {
      if (entry.type !== 'dir') continue;
      const path = dir === '/' ? `/${entry.name}` : `${dir}/${entry.name}`;
      if (entry.mounted) roots.push(collapse(fs, path));
      else descend(path);
    }
  };
  descend('/');
  return roots;
}

/** Follow a mount down while it holds exactly one directory and nothing else. */
function collapse(fs, path) {
  let at = path;
  for (;;) {
    let entries;
    try {
      entries = fs.list(at);
    } catch {
      return at;
    }
    if (entries.length !== 1 || entries[0].type !== 'dir') return at;
    at = `${at}/${entries[0].name}`;
  }
}

/**
 * The places column, derived from the filesystem that is in front of us.
 *
 * `HOME` comes from the seed, `/` is the filesystem, and the rest are wherever this page
 * grafted something. A hardcoded list goes stale the first time a graft moves; this one
 * cannot.
 */
function placesOf(fs, home) {
  const places = [
    { label: 'Home', path: home },
    { label: 'Computer', path: '/' },
  ];
  for (const root of mountRoots(fs)) {
    // Labelled by its last name and titled by its path: `webxp` is what a reader recognises
    // and `/usr/src/webxp` is what it means.
    places.push({ label: root.slice(root.lastIndexOf('/') + 1) || root, path: root });
  }
  return places;
}

//: A row's glyph, at the size every other list in this shell draws one.
const ROW_ICON_SIZE = 16;

/**
 * Build the picker, put it up, and resolve what it answers.
 *
 * @param {object} win
 * @param {object} spec  From `pickerSpec`: already checked, already defaulted.
 * @returns {Promise<string|null>}
 */
async function runPicker(win, spec) {
  // Dynamic, so a desktop whose reader never opens a file never loads the filesystem — the
  // same reason Notepad defers it.
  const [{ openFileSystem, hydrate }, { HOME }, { PERM, FsError, basename, dirname, joinPath }] =
    await Promise.all([
      import('../shared/fs/fs.mjs'),
      import('../shared/fs/seed.mjs'),
      import('../shared/fs/vfs.mjs'),
    ]);
  const fs = await openFileSystem();
  await linkPickerStyles();

  const id = `picker-${++pickerSerial}`;
  const saving = spec.mode === 'save';
  const verb = saving ? 'save' : 'open';

  /* ------------------------------ the frame ------------------------------ */

  const { root, bar } = dialogFrame(id, spec.title);
  root.classList.add('picker');

  const head = document.createElement('div');
  head.className = 'picker-head';
  const whereLabel = document.createElement('span');
  whereLabel.className = 'picker-label';
  whereLabel.textContent = saving ? 'Save in:' : 'Look in:';
  const where = document.createElement('span');
  where.className = 'picker-where';
  const up = document.createElement('button');
  up.type = 'button';
  up.className = 'picker-up';
  up.textContent = 'Up';
  up.title = 'Go to the parent folder';
  head.append(whereLabel, where, up);

  const main = document.createElement('div');
  main.className = 'picker-main';
  const places = document.createElement('nav');
  places.className = 'picker-places';
  places.setAttribute('aria-label', 'Places');
  const list = document.createElement('div');
  list.className = 'picker-list';
  list.setAttribute('role', 'listbox');
  // Focusable but not a tab stop, which is what `-1` means to `window.mjs`'s `FOCUSABLE`.
  // It is where the keyboard goes when the folder it just walked into has no rows to hold it:
  // without it, opening an empty folder dropped focus on BODY, outside the dialog — measured,
  // and Escape then did nothing either, because the host scopes Escape to the layer.
  list.tabIndex = -1;
  main.append(places, list);

  // The picker's own status line, and the only thing in it that is announced. XP's dialogs
  // said nothing, which is how a refusal — the folder is read-only, that file is the wrong
  // kind — became a press that appeared to do nothing at all.
  const note = document.createElement('p');
  note.className = 'picker-note';
  note.id = `${id}-note`;
  note.setAttribute('role', 'status');

  /* ------------------------------ the footer ------------------------------ */

  // Two rows, labels in a gutter, the buttons stacked on the right: `picker.css` fixes the
  // row height and the gutter width, which is the shape win32.run's `Viewer3.svelte` takes
  // from XP (lines 196-228) and the one piece of its layout worth copying exactly.
  const foot = document.createElement('form');
  foot.className = 'picker-foot';

  const nameLabel = document.createElement('label');
  nameLabel.className = 'picker-label';
  nameLabel.textContent = 'File name:';
  nameLabel.htmlFor = `${id}-name`;
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'picker-name';
  nameInput.id = `${id}-name`;
  nameInput.spellcheck = false;
  nameInput.autocomplete = 'off';
  nameInput.value = spec.name;
  // The box, not the list: a reader who knows the name types it, and a reader who does not
  // Tabs once. `showModal` honours `autofocus` and nothing else marks the default stop.
  nameInput.autofocus = true;
  const primary = document.createElement('button');
  primary.type = 'submit';
  primary.className = 'picker-go';
  primary.textContent = saving ? 'Save' : 'Open';

  const typeLabel = document.createElement(saving && spec.types.length > 1 ? 'label' : 'span');
  typeLabel.className = 'picker-label';
  typeLabel.textContent = 'Files of type:';
  let typeSelect = null;
  let typeText = null;
  if (saving && spec.types.length > 1) {
    typeSelect = document.createElement('select');
    typeSelect.className = 'picker-type';
    typeSelect.id = `${id}-type`;
    typeLabel.htmlFor = typeSelect.id;
    spec.types.forEach((type, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.textContent = type.label;
      typeSelect.append(option);
    });
  } else {
    // One type is not a choice. win32.run renders the dropdown anyway and never applies it
    // (`Viewer3.svelte:36` is `function is_desired(item){ return true; }`), which is a
    // control that teaches the reader their press did nothing.
    typeText = document.createElement('span');
    typeText.className = 'picker-kinds';
    typeText.textContent = saving
      ? spec.types[0].label
      : (spec.extensions.length ? `${spec.extensions.join(', ')} files` : ALL_FILES);
  }
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'picker-cancel';
  cancel.textContent = 'Cancel';

  foot.append(nameLabel, nameInput, primary, typeLabel, typeSelect || typeText, cancel);
  root.append(head, main, note, foot);

  /* ------------------------------ state ------------------------------ */

  //: The extensions in force. For Save As that is the selected type's, which is why the
  //: dropdown is a control and not decoration.
  const filter = () => (saving ? spec.types[Number(typeSelect ? typeSelect.value : 0)].extensions
    : spec.extensions);

  const say = (text) => { note.textContent = text; };

  const writable = (dir) => {
    try {
      fs.access(dir, PERM.W);
      return true;
    } catch (error) {
      // Only a refusal means read-only. A bare `catch` would turn a renamed `PERM` into a
      // picker that refuses every folder, with nothing in the console to say why.
      if (!(error instanceof FsError)) throw error;
      return false;
    }
  };

  /** What activating a row will do: a symlink answers for what it points at. */
  const kindOf = (entry, path) => {
    if (entry.type !== 'link') return entry.type;
    try {
      return fs.stat(path).type;
    } catch {
      // A broken shortcut points at nothing, so there is nothing to take a kind from.
      return 'file';
    }
  };

  let here = '/';
  let chosen = null;

  const rows = () => [...list.querySelectorAll('.picker-row')];

  const refuse = (name) => say(
    `${name} is not ${extensionPhrase(filter())} — type the name to ${verb} it anyway.`,
  );

  const select = (row) => {
    if (row.getAttribute('aria-disabled') === 'true') {
      // Refused selection, not hidden. A reader can see the file is there and is told why it
      // cannot be picked, which is the one thing a filter that hides files cannot do.
      refuse(row.dataset.name);
      return;
    }
    chosen = row.dataset.name;
    for (const other of rows()) other.setAttribute('aria-selected', String(other === row));
    nameInput.value = chosen;
    refreshPrimary();
  };

  const focusRow = (index) => {
    const all = rows();
    if (!all.length) return;
    const at = Math.max(0, Math.min(index, all.length - 1));
    all.forEach((row, i) => { row.tabIndex = i === at ? 0 : -1; });
    all[at].focus();
    // Selection follows focus in a single-select listbox, so arrowing through a folder fills
    // the name box the way XP's did. Explorer's list does not move at all from the keyboard;
    // this one is the whole route for a reader with no mouse.
    select(all[at]);
  };

  /* ------------------------------ the list ------------------------------ */

  function render() {
    where.textContent = here;
    where.title = here;
    up.disabled = here === '/';
    list.setAttribute('aria-label', `Contents of ${here}`);

    let entries;
    try {
      entries = fs.list(here);
    } catch (error) {
      // An unreadable folder is a fact to print, not an empty list: a picker that looks
      // finished and is empty is the failure this project keeps finding.
      list.replaceChildren(emptyNote(error.message));
      say(error.message);
      refreshPrimary();
      return;
    }

    const held = document.activeElement;
    const refocus = held && held.classList && held.classList.contains('picker-row')
      ? held.dataset.name
      : null;

    // Folders first, then by name — Explorer's order, and the one that makes a deep tree
    // navigable without reading every row.
    entries.sort((a, b) => {
      const kinds = (a.type === 'dir' ? 0 : 1) - (b.type === 'dir' ? 0 : 1);
      return kinds || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
    });

    //: The one tab stop. The selected row, so Tab into the list lands where the selection
    //: is rather than at the top of a folder the reader has already moved through.
    const stopAt = Math.max(0, entries.findIndex((entry) => entry.name === chosen));

    const built = entries.map((entry, index) => {
      const path = joinPath(here, entry.name);
      const kind = kindOf(entry, path);
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'picker-row';
      row.setAttribute('role', 'option');
      row.dataset.name = entry.name;
      row.dataset.kind = kind;
      row.setAttribute('aria-selected', String(entry.name === chosen));
      // One tab stop for the whole list, not one per file: a folder of 41 files would
      // otherwise put 41 stops between the places column and the name box.
      row.tabIndex = index === stopAt ? 0 : -1;
      if (kind !== 'dir' && !kindMatches(entry.name, filter())) {
        // Greyed and still a focus stop, as a disabled menu row is: a reader can arrive at
        // it, hear why, and move on.
        row.setAttribute('aria-disabled', 'true');
      }
      row.append(makeIcon(kind === 'dir' ? 'folder' : 'document', ROW_ICON_SIZE));
      const label = document.createElement('span');
      label.className = 'picker-row-label';
      label.textContent = entry.name;
      row.append(label);

      row.addEventListener('click', () => select(row));
      row.addEventListener('dblclick', () => activate(row));
      return row;
    });

    list.replaceChildren(...(built.length ? built : [emptyNote('This folder is empty.')]));

    // `replaceChildren` destroyed the focused row. Without this a write from a terminal left
    // a keyboard reader on BODY, outside the dialog, with the question still up.
    if (refocus) {
      const again = built.find((row) => row.dataset.name === refocus) || built[0];
      if (again) {
        again.tabIndex = 0;
        again.focus();
      } else {
        // An empty folder, or one whose rows all went away. The list itself takes the
        // keyboard so Backspace still walks back out of it.
        list.focus();
      }
    }
    refreshPrimary();
  }

  function emptyNote(text) {
    const line = document.createElement('p');
    line.className = 'picker-empty';
    line.textContent = text;
    return line;
  }

  function go(path) {
    here = path;
    chosen = null;
    render();
    const count = rows().length;
    const shut = saving && !writable(here) ? ' · read-only' : '';
    say(`${here} · ${count} item${count === 1 ? '' : 's'}${shut}`);
  }

  /**
   * The list's keyboard, on the list and not on each row.
   *
   * One listener for every row and for the empty list both: a row's press bubbles here, and
   * when the folder has no rows the list itself holds the focus, so Backspace still works
   * where a per-row handler would have had nothing to attach to.
   *
   * Bound on the list rather than on the dialog, so Backspace in the name box still deletes
   * a character.
   */
  function onListKey(event) {
    const row = event.target.closest ? event.target.closest('.picker-row') : null;
    const all = rows();
    //: Where the keyboard is. `-1` is the list itself, which is a real place for it to be.
    const index = row ? all.indexOf(row) : -1;
    const keys = {
      ArrowDown: () => focusRow(index + 1),
      ArrowUp: () => focusRow(index < 0 ? all.length - 1 : index - 1),
      Home: () => focusRow(0),
      End: () => focusRow(all.length - 1),
      Enter: () => { if (row) activate(row); },
      // XP's file list went up a folder on Backspace.
      Backspace: () => goUp(),
    };
    const run = keys[event.key];
    if (!run) return;
    event.preventDefault();
    run();
  }

  function goUp() {
    if (here === '/') return;
    go(dirname(here));
  }

  /* ------------------------------ answering ------------------------------ */

  const { answer, finish: settle } = oneAnswer(win);
  const unwatch = fs.watch(() => {
    // The confirm pane takes the list out of the tree; nothing to re-list while it is up.
    if (!list.isConnected) return;
    try {
      fs.stat(here);
    } catch (error) {
      if (error.code === 'ENOENT' || error.code === 'ENOTDIR') here = HOME;
    }
    render();
  });
  const finish = (value) => {
    unwatch();
    settle(value);
  };

  /**
   * Ask inside the picker, because a window has one modal slot and `showModal` refuses a
   * second one (`src/window.mjs:659-665`). The question is built from the same parts a
   * message box is, so the two cannot drift apart.
   *
   * The picker's panes come out of the tree rather than being hidden: the host's Tab trap
   * cycles every focus stop in the layer and cannot see `hidden` or `inert`, so a hidden
   * pane leaves Tab stopping on controls that cannot take the focus.
   */
  function askInside(text) {
    return new Promise((resolve) => {
      const askId = `${id}-ask`;
      const pane = document.createElement('div');
      pane.className = 'picker-ask';
      const done = (value) => {
        root.removeAttribute('aria-describedby');
        root.replaceChildren(bar, head, main, note, foot);
        render();
        nameInput.focus();
        resolve(value);
      };
      pane.append(
        questionBody(askId, { text, icon: 'document' }),
        buttonRow([
          { id: 'yes', text: 'Yes', default: true },
          { id: 'no', text: 'No', cancel: true },
        ], done),
      );
      pane.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        // Escape backs out of the question, not out of the picker: the layer's own Escape
        // handler would take the whole dialog down, and XP's Confirm Save As went back to
        // the file list.
        event.preventDefault();
        event.stopPropagation();
        done('no');
      });
      root.setAttribute('aria-describedby', `${askId}-text`);
      root.replaceChildren(bar, pane);
      const first = pane.querySelector('[autofocus]') || pane.querySelector('button');
      if (first) first.focus();
    });
  }

  /** The folder a typed name would land in, or null when there is no such folder. */
  function typedDir(typed) {
    const folder = typed ? dirname(typed) : '.';
    try {
      const real = fs.realpath(folder, { cwd: here });
      return fs.stat(real).type === 'dir' ? real : null;
    } catch {
      return null;
    }
  }

  //: Whether the last `refreshPrimary` left the button dead, so a reason is announced when
  //: the state changes rather than on every keystroke.
  let wasDead = false;

  /**
   * Save is dead in a folder nothing can be written to, before a name is typed.
   *
   * The repository mounts read-only, so this is not a hypothetical: the Save As dialog opens
   * in `/usr/src/webxp` whenever that is where the file came from. The button follows the
   * folder the *box* names, not the folder on screen — typing an absolute path into an
   * unwritable folder is a legitimate way to save somewhere else, and a button wired to the
   * list would refuse it.
   */
  function refreshPrimary() {
    if (!saving) return;
    const dir = typedDir(nameInput.value.trim());
    const dead = dir === null || !writable(dir);
    primary.disabled = dead;
    if (dead && !wasDead) {
      say(dir === null
        ? `There is no folder named ${dirname(nameInput.value.trim()) || '.'} here.`
        : `${dir} is read-only — nothing can be saved there.`);
    }
    wasDead = dead;
  }

  /** Open: the read the caller is about to do, done here first. */
  async function finishOpen(path) {
    try {
      // A file under a mount has a name and a size before it has contents, and reading it
      // unhydrated fails with `EAGAIN`. Resolving a path the caller cannot read would make
      // the picker's success the caller's error, one window away from the press that caused
      // it — `apps/explorer/app.mjs:165-171` is the precedent and the reason.
      await hydrate(fs, [path]);
      fs.readFile(path);
    } catch (error) {
      say(error.message);
      return;
    }
    finish(path);
  }

  async function finishSave(typed) {
    const dir = typedDir(typed);
    if (dir === null) {
      say(`There is no folder named ${dirname(typed)} here.`);
      return;
    }
    if (!writable(dir)) {
      say(`${dir} is read-only — nothing can be saved there.`);
      return;
    }
    const name = withExtension(basename(typed), filter());
    const path = joinPath(dir, name);
    let exists = false;
    try {
      exists = fs.exists(path);
    } catch (error) {
      say(error.message);
      return;
    }
    if (exists) {
      if (fs.lstat(path).type === 'dir') {
        say(`${name} is a folder.`);
        return;
      }
      if (await askInside(`${name} already exists. Replace it?`) !== 'yes') {
        say(`${name} was not replaced.`);
        return;
      }
    }
    finish(path);
  }

  /** The primary action, from the button, from Enter in the name box, or from a row. */
  async function commit() {
    const typed = nameInput.value.trim();
    if (!typed) {
      say(saving ? 'Type a name for the file.' : 'Choose a file, or type its name.');
      return;
    }
    // A typed path may be absolute, relative to the folder on screen, or a folder — the
    // three things a reader types into this box. Explorer settles the same question the same
    // way at `apps/explorer/app.mjs:323-324`: resolve it, then refuse what is not a folder.
    let real = null;
    //: Why it did not resolve. Kept, because "it is not there" is only one of the answers:
    //: a folder with no `x` bit, a symlink loop and an unreadable parent all land here, and
    //: a picker that reports every one of them as "does not exist" sends the reader looking
    //: for a file that is in front of them.
    let refusal = null;
    try {
      real = fs.realpath(typed, { cwd: here });
    } catch (error) {
      refusal = error;
    }
    if (real !== null) {
      let kind = null;
      try {
        kind = fs.stat(real).type;
      } catch (error) {
        say(error.message);
        return;
      }
      if (kind === 'dir') {
        // A folder typed into the name box is navigation, which is what both XP dialogs did
        // with it.
        nameInput.value = '';
        go(real);
        return;
      }
    }
    if (saving) {
      await finishSave(typed);
      return;
    }
    if (real === null) {
      say(refusal && refusal.code !== 'ENOENT' ? refusal.message : `${typed} does not exist here.`);
      return;
    }
    // A typed name is taken at its word even when the filter would grey it in the list: the
    // filter says what the program offers, and a reader naming a file has already answered.
    await finishOpen(real);
  }

  function activate(row) {
    const path = joinPath(here, row.dataset.name);
    if (row.dataset.kind === 'dir') {
      nameInput.value = '';
      try {
        // Resolved again rather than trusted: the row was drawn from a listing, and this
        // filesystem is shared — the folder can have gone between the render and the press.
        go(fs.realpath(path, { cwd: here }));
      } catch (error) {
        say(error.message);
      }
      return;
    }
    if (row.getAttribute('aria-disabled') === 'true') {
      refuse(row.dataset.name);
      return;
    }
    nameInput.value = row.dataset.name;
    commit();
  }

  /* ------------------------------ wiring ------------------------------ */

  for (const place of placesOf(fs, HOME)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'picker-place';
    button.textContent = place.label;
    button.title = place.path;
    button.dataset.path = place.path;
    button.addEventListener('click', () => go(place.path));
    places.append(button);
  }

  list.addEventListener('keydown', onListKey);
  up.addEventListener('click', goUp);
  cancel.addEventListener('click', () => finish(null));
  nameInput.addEventListener('input', refreshPrimary);
  if (typeSelect) {
    typeSelect.addEventListener('change', () => {
      // The type is applied, which is the whole difference from win32.run's dropdown.
      render();
      say(`Showing ${spec.types[Number(typeSelect.value)].label}`);
    });
  }
  foot.addEventListener('submit', (event) => {
    event.preventDefault();
    commit();
  });

  /* ------------------------------ up ------------------------------ */

  const start = (() => {
    const asked = spec.dir || HOME;
    try {
      const real = fs.realpath(asked);
      if (fs.stat(real).type === 'dir') return { path: real, problem: null };
      return { path: HOME, problem: `${asked} is not a folder` };
    } catch (error) {
      return { path: HOME, problem: error.message };
    }
  })();
  go(start.path);
  // A start folder that has gone — deleted, or never there — is said rather than silently
  // swapped for home, because the reader is looking at a different folder from the one the
  // program asked for.
  if (start.problem) say(`${start.problem} — showing ${start.path}.`);

  // A dismissal is a cancel: Escape, and a press on the scrim's own edge, both land here.
  //
  // The watcher is registered above, before the dialog reaches the screen, because `go` needs
  // the folder listed to size the dialog. `showModal` is documented to refuse a closed window
  // and a window whose one modal slot is taken, and on that route `finish` never runs — so the
  // watcher is released here rather than only by the latch. Measured without this: a picker
  // refused for a taken slot left one listener per refusal on the shared VFS for the session,
  // each retaining the detached dialog, the window and the filesystem (1 at rest -> 2 with a
  // picker up -> 3 after one refusal -> 2 once the live one was cancelled).
  try {
    win.showModal(root, { onDismiss: () => finish(null) });
  } catch (error) {
    unwatch();
    throw error;
  }
  return answer;
}

/**
 * Choose a file to read. Resolves its absolute path, or `null` if the visitor cancelled.
 *
 * The path is hydrated and read once before it resolves, so the caller is handed a path it
 * can actually read rather than one that throws `EAGAIN` in its own `mount`.
 *
 * @param {object} win                    The window the dialog belongs to.
 * @param {object} [options]
 * @param {string} [options.title]        The dialog's title. `Open` by default.
 * @param {string} [options.dir]          Where to start. `HOME` by default; a folder that is
 *                                        not there falls back to `HOME` and says so.
 * @param {string[]} [options.extensions] `['.txt']`. Anything else is greyed and refused,
 *                                        never hidden. Omitted means every file.
 * @returns {Promise<string|null>}
 * @throws {Error} On options this cannot answer — synchronously, on the caller's own line.
 */
export function pickFile(win, options = {}) {
  return runPicker(win, pickerSpec(win, 'open', options));
}

/**
 * Choose where to write. Resolves an absolute path, or `null` if the visitor cancelled.
 *
 * The path may not exist yet; its folder does, and is writable, and replacing a file that is
 * already there has been agreed to. Nothing is written here — the caller still owns the
 * write, and its own conflict rules with it.
 *
 * @param {object} win
 * @param {object} [options]
 * @param {string} [options.title]  `Save As` by default.
 * @param {string} [options.dir]    Where to start.
 * @param {string} [options.name]   The name to offer, e.g. the document's current one.
 * @param {{label: string, extensions: string[]}[]} [options.types]
 *        The type list, drawn as a dropdown when there is more than one and *applied*: it
 *        filters the list and supplies the extension a name typed without one gets.
 * @returns {Promise<string|null>}
 * @throws {Error} On options this cannot answer.
 */
export function pickSaveAs(win, options = {}) {
  return runPicker(win, pickerSpec(win, 'save', options));
}
