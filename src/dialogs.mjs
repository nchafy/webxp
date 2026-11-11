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
