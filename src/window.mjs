/* One window: its frame, its state, and the two gestures that move it.
 *
 * A window knows nothing about the taskbar, the Start menu, or the other windows. It is
 * handed a `host` that answers the four questions it cannot answer alone — where the
 * desktop is, who to tell when it wants focus, where its taskbar button is, and how to say
 * something out loud. `desktop.mjs` is the only implementation of that interface, and the
 * split is what lets a window be tested without a shell around it.
 *
 * Nothing here reaches the page as markup: every node is built with DOM calls and
 * `textContent`.
 */

import { icon as makeIcon } from './icons.mjs';

/** The smallest a window may be dragged to. Below this the title bar loses its buttons. */
export const MIN_W = 220;
export const MIN_H = 120;

//: How much of a window must stay on the desktop. XP clamped nothing but the taskbar, so a
//: window could be dragged until only a corner showed and then not be draggable back. The
//: title bar is the only way to move a window, so it is the thing kept reachable.
const MIN_VISIBLE = 72;

//: One arrow press, moving or resizing from the keyboard. 10px is coarse enough to cross a
//: desktop in a few seconds and fine enough to place a window against an edge.
const KEY_STEP = 10;

//: The cascade. Each new window steps `CASCADE_STEP` down and right of the one before,
//: starting at `CASCADE_TOP`; `docs/windows.md` states the step. `CASCADE_FOOT` is the room
//: left below the last lane, so a cascaded window's foot is not under the taskbar, and
//: `CASCADE_EDGE` is how near the desktop's left or right edge a new window may start.
const CASCADE_STEP = 26;
const CASCADE_TOP = 24;
const CASCADE_FOOT = 40;
const CASCADE_EDGE = 10;

//: The directions a resize handle can carry. `window.css` insets each edge grip by the
//: corner size, so edges and corners meet without overlapping and the order of this list
//: decides nothing.
const GRIPS = ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'];

let serial = 0;

/** Honoured at call time, not at import time, so switching the OS setting takes effect. */
function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

//: A theme token's value. Tokens carry no JS fallback: a fallback is a second copy of the
//: token that can disagree with it, and `theme.css` and `window.css` are linked separately,
//: so the disagreement is reachable.
function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function tokenNumber(name) {
  return parseFloat(token(name));
}

/**
 * Zoom an element between its own box and some other rect, XP's open/minimise animation.
 *
 * The element is already at its final geometry; only `transform` moves, so this never
 * fights the layout and never needs a second reflow. `transform` is reserved for this —
 * window geometry travels as custom properties for exactly that reason.
 *
 * @param {HTMLElement} el
 * @param {DOMRect|null} other   The small end: a taskbar button, or the pre-maximise box.
 * @param {'in'|'out'} direction `in` ends at the element's box, `out` starts there.
 * @returns {Promise<void>}      Resolves when the animation finishes, or at once if it
 *                               was skipped.
 */
export function zoom(el, other, direction) {
  const self = el.getBoundingClientRect();
  if (!other || !self.width || !self.height || reducedMotion()) return Promise.resolve();

  // A taskbar button is ~22px tall against a 320px window; scaling all the way down reads
  // as a vanish rather than a move, so the small end is floored.
  const sx = Math.max(other.width / self.width, 0.08);
  const sy = Math.max(other.height / self.height, 0.08);
  const dx = other.left + other.width / 2 - (self.left + self.width / 2);
  const dy = other.top + other.height / 2 - (self.top + self.height / 2);

  const small = { transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})`, opacity: 0.4 };
  const full = { transform: 'translate(0px, 0px) scale(1, 1)', opacity: 1 };
  const frames = direction === 'in' ? [small, full] : [full, small];

  const ms = tokenNumber('--zoom-ms');
  const easing = token('--zoom-ease');
  // With no `theme.css` there are no tokens to read, and an animation takes neither a NaN
  // duration nor an empty easing. Skipped rather than defaulted, for the reason on `token`.
  if (!Number.isFinite(ms) || !easing) return Promise.resolve();

  const animation = el.animate(frames, { duration: ms, easing, fill: 'none' });
  return animation.finished.catch(() => {});
}

/**
 * Hold a box inside the desktop.
 *
 * Only the minimum size and the position are clamped. `MIN_VISIBLE` of the window stays
 * horizontally on the desktop and the title bar stays between the top edge and the
 * taskbar — a departure from XP, which let a window be dragged until its own handle was
 * unreachable.
 *
 * The size is deliberately *not* capped to the desktop. A cap here cannot know which edge
 * the pointer is holding, so it shrinks the window while leaving `x` and `y` alone and the
 * anchored edge absorbs the difference, which is a resize quietly becoming a move. A window
 * larger than the desktop is legitimate — XP allowed it — and the position clamp still keeps
 * the title bar reachable. `resize past the desktop` in `scripts/check.py` holds the
 * north and the west case.
 *
 * Pure arithmetic. The caller measures, so the result depends on nothing but the arguments
 * and the same box can be clamped against a surface the window is not currently on.
 *
 * @param {{x: number, y: number, w: number, h: number}} box
 * @param {{w: number, h: number}} surface  The desktop's size.
 * @param {number} barHeight                The title bar's height, which is what must stay
 *                                          above the taskbar.
 */
function clampBox(box, surface, barHeight) {
  const w = Math.max(box.w, MIN_W);
  const h = Math.max(box.h, MIN_H);
  const x = Math.min(Math.max(box.x, MIN_VISIBLE - w), Math.max(surface.w - MIN_VISIBLE, 0));
  const y = Math.min(Math.max(box.y, 0), Math.max(surface.h - barHeight, 0));
  return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
}

export class XPWindow {
  /**
   * @param {object} spec
   * @param {string} spec.title
   * @param {string} [spec.id]             Defaults to a generated one.
   * @param {string} [spec.iconName]       A name `icons.mjs` knows, drawn in the title bar.
   * @param {number} [spec.width]
   * @param {number} [spec.height]
   * @param {number} [spec.x]              Left, in desktop pixels. Omitted means cascade.
   * @param {number} [spec.y]
   * @param {boolean} [spec.resizable]     Default true.
   * @param {boolean} [spec.minimizable]   Default true.
   * @param {boolean} [spec.maximizable]   Default true.
   * @param {boolean} [spec.maximized]     Open maximised.
   * @param {boolean} [spec.status]        Give it a status bar at the foot.
   * @param {string} [spec.bodyClass]      Added to the client area, for an app's own CSS.
   * @param {object} host                  See the note at the top of this file.
   */
  constructor(spec, host) {
    this.host = host;
    this.id = spec.id || `win-${++serial}`;
    this.titleText = spec.title;
    this.iconName = spec.iconName || 'window';
    this.resizable = spec.resizable !== false;
    this.minimizable = spec.minimizable !== false;
    this.maximizable = spec.maximizable !== false;

    /** `normal`, `min` or `max`. The one place a window's state is recorded. */
    this.state = 'normal';
    /**
     * Where the window goes back to. Kept through a maximise, which is the whole point.
     *
     * The single source of truth, and the geometry of the last gesture: `apply()` never
     * writes it, so a desktop that briefly shrinks and grows back returns the window to
     * where it was put rather than leaving it where it was squeezed.
     *
     * A gesture does write it, from `placed` plus its own delta — so a drag or a resize
     * performed while the window is clamped adopts the clamped coordinate as the new intent.
     * That is the gesture speaking, not the clamp: seeding a gesture from an off-screen
     * `box` instead would make the window jump out from under the pointer.
     */
    this.box = {
      x: Number.isFinite(spec.x) ? spec.x : 0,
      y: Number.isFinite(spec.y) ? spec.y : 0,
      w: Math.max(spec.width || 480, MIN_W),
      h: Math.max(spec.height || 320, MIN_H),
    };
    /**
     * `box` held inside the desktop: the geometry on the element, and where a gesture or an
     * arrow key starts from. Recomputed by `apply()`; never written by a caller.
     */
    this.placed = { ...this.box };
    this.drag = null;
    this.closed = false;

    this.build(spec);
    if (!Number.isFinite(spec.x) || !Number.isFinite(spec.y)) this.cascade();
    this.apply();
    if (spec.maximized && this.maximizable) {
      this.state = 'max';
      this.el.classList.add('is-max');
      this.syncTitleButtons();
    }
  }

  /* -------------------------------------------------------------------------
     Markup.
     ------------------------------------------------------------------------- */

  build(spec) {
    const el = document.createElement('section');
    el.className = 'window';
    el.id = this.id;
    // A labelled `section` is a region, which is the closest honest match: these are not
    // dialogs — nothing is modal and nothing is waiting on an answer — and ARIA has no
    // role for "an application window". The name comes from the title, as XP's did.
    el.setAttribute('aria-labelledby', `${this.id}-title`);
    if (!this.resizable) el.classList.add('is-fixed-size');

    const bar = document.createElement('div');
    bar.className = 'window-title';
    // The bar is the move handle, so it has to be reachable without a pointer. Focusing it
    // and pressing an arrow is the keyboard equivalent of the drag (SC 2.1.1).
    bar.tabIndex = 0;
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', this.barLabel());

    bar.append(makeIcon(this.iconName, 16));

    const heading = document.createElement('h2');
    heading.id = `${this.id}-title`;
    heading.textContent = spec.title;
    bar.append(heading);

    const boxes = document.createElement('div');
    boxes.className = 'title-buttons';
    this.minBtn = this.titleBox('_', 'Minimize', () => this.minimize());
    // The maximise box is the one title box whose glyph and name follow the state, so
    // `syncTitleButtons` writes both and nothing else does.
    this.maxBtn = this.titleBox('', '', () => this.toggleMaximize());
    this.syncTitleButtons();
    this.closeBtn = this.titleBox('✕', 'Close', () => this.close());
    this.closeBtn.classList.add('close');
    this.minBtn.hidden = !this.minimizable;
    this.maxBtn.hidden = !this.maximizable;
    boxes.append(this.minBtn, this.maxBtn, this.closeBtn);
    bar.append(boxes);

    const body = document.createElement('div');
    body.className = 'window-body is-loading';
    // The hook an app's own stylesheet hangs off, so two apps' rules cannot reach each
    // other's content. `src/apps.mjs` sets it to `app-<id>` for every loaded app.
    if (spec.bodyClass) body.classList.add(spec.bodyClass);
    body.append(loadingBlock());

    el.append(bar, body);

    if (spec.status) {
      const status = document.createElement('p');
      status.className = 'window-status';
      el.append(status);
      this.statusEl = status;
    } else {
      this.statusEl = null;
    }

    if (this.resizable) {
      for (const dir of GRIPS) {
        const grip = document.createElement('div');
        grip.className = 'window-grip';
        grip.dataset.dir = dir;
        grip.addEventListener('pointerdown', (event) => this.startResize(event, dir));
        el.append(grip);
      }
    }

    this.el = el;
    this.bar = bar;
    this.bodyEl = body;
    this.headingEl = heading;

    bar.addEventListener('pointerdown', (event) => this.startMove(event));
    bar.addEventListener('dblclick', (event) => {
      if (event.target.closest('.title-buttons')) return;
      this.toggleMaximize();
    });
    bar.addEventListener('keydown', (event) => this.onBarKey(event));
    // Capture, so a click on anything inside raises the window before that thing reacts.
    el.addEventListener('pointerdown', () => this.host.focus(this), true);
  }

  titleBox(glyph, label, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'title-box';
    button.textContent = glyph;
    button.setAttribute('aria-label', label);
    button.addEventListener('click', onClick);
    return button;
  }

  /**
   * The title bar's accessible name: the window, then the keyboard equivalent of the drag
   * (SC 2.1.1). Written at build and again at every rename, so it is one sentence and a
   * renamed window cannot offer different instructions from a fresh one.
   */
  barLabel() {
    return `${this.titleText} window: arrow keys move, shift and arrow keys resize`;
  }

  /* -------------------------------------------------------------------------
     Geometry.
     ------------------------------------------------------------------------- */

  /** The desktop's size, which every clamp is measured against. Size only: a window's
      geometry is relative to the surface, so the surface's own offset answers nothing. */
  surface() {
    const rect = this.host.surface.getBoundingClientRect();
    return { w: rect.width, h: rect.height };
  }

  /**
   * Derive `placed` from `box` and write it to the element.
   *
   * `box` is left alone. The clamp belongs to the surface the window happens to be on, not
   * to the window: baking it into `box` destroys the position the user chose, and a desktop
   * that grows back cannot return it. Shrinking the browser and widening it again is the
   * route to that, and there is no undo.
   *
   * Through `style.setProperty`, not a `style` attribute: CSP's `style-src` blocks a parsed
   * `style` attribute and does not hook the CSSOM, so a window can move under a policy with
   * no `unsafe-inline`.
   */
  apply() {
    // `--titlebar-h` is what `window.css` sizes the bar with, and unlike `offsetHeight` it
    // is readable while the window is minimised and has no box to measure — which is exactly
    // when `watchViewport` re-clamps it. Without `theme.css` there is no token: the y clamp
    // then has no bar to hold above the taskbar and falls back to the desktop's own edge,
    // which keeps the geometry a number instead of writing `NaNpx` and dropping every window
    // to its static position.
    const barHeight = tokenNumber('--titlebar-h') || 0;
    this.placed = clampBox(this.box, this.surface(), barHeight);
    const { style } = this.el;
    style.setProperty('--win-x', `${this.placed.x}px`);
    style.setProperty('--win-y', `${this.placed.y}px`);
    style.setProperty('--win-w', `${this.placed.w}px`);
    style.setProperty('--win-h', `${this.placed.h}px`);
  }

  /** Step each new window down and right, so a second window is not hidden by the first. */
  cascade() {
    const { w: sw, h: sh } = this.surface();
    const index = this.host.cascadeIndex();
    // Wrap before the window would be pushed off the bottom, rather than letting the
    // cascade walk off the desktop and get clamped into a pile in the corner.
    const lanes = Math.max(Math.floor((sh - this.box.h - CASCADE_FOOT) / CASCADE_STEP), 1);
    /**
     * Which lane this window took. The host reads it back off its open windows to hand the
     * next one a lane nobody is in, so it is state and not a local. A window given an
     * explicit `x` and `y` never cascades and never has one.
     */
    this.cascadeSlot = index % lanes;
    // Clear of the icon column. XP cascaded from the top-left corner and buried its own
    // desktop icons under the first window; starting to the right of them costs nothing
    // and leaves both reachable.
    const left = this.host.reservedLeft();
    this.box.x = Math.min(
      left + this.cascadeSlot * CASCADE_STEP,
      Math.max(sw - this.box.w - CASCADE_EDGE, CASCADE_EDGE),
    );
    this.box.y = CASCADE_TOP + this.cascadeSlot * CASCADE_STEP;
  }

  moveTo(x, y) {
    this.box = { ...this.box, x, y };
    this.apply();
  }

  sizeTo(w, h) {
    this.box = { ...this.box, w, h };
    this.apply();
  }

  /* -------------------------------------------------------------------------
     Move and resize.
     ------------------------------------------------------------------------- */

  startMove(event) {
    if (event.button !== 0) return;
    if (event.target.closest('.title-buttons')) return;
    // XP did not let a maximised window be dragged, and neither does this.
    if (this.state === 'max') return;
    this.beginGesture(event, { kind: 'move', dir: null });
  }

  startResize(event, dir) {
    if (event.button !== 0 || this.state === 'max') return;
    // Resizing from an edge is a window gesture, not a content one; without this the grip's
    // pointerdown also starts a text selection in the body underneath.
    event.preventDefault();
    this.host.focus(this);
    this.beginGesture(event, { kind: 'resize', dir });
  }

  beginGesture(event, { kind, dir }) {
    // A second pointer — two fingers, or a mouse and a touch — re-enters here while a
    // gesture is live. The one in flight is ended first: its listeners only answer to its
    // own pointer id, so leaving them attached leaks three per re-entry for the window's
    // lifetime.
    if (this.drag) this.drag.end();

    const target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    // From `placed`, not `box`: the gesture starts from where the window is, so a drag that
    // begins on a clamped window does not jump to the unclamped position it remembers.
    const gesture = {
      kind,
      dir,
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      box: { ...this.placed },
      target,
    };
    this.drag = gesture;
    this.el.classList.add('is-moving');

    const onMove = (moveEvent) => {
      if (this.drag !== gesture || moveEvent.pointerId !== gesture.id) return;
      this.applyGesture(moveEvent.clientX - gesture.startX, moveEvent.clientY - gesture.startY);
    };
    const onEnd = (endEvent) => {
      if (endEvent.pointerId !== gesture.id) return;
      gesture.end();
    };
    gesture.end = () => {
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', onEnd);
      target.removeEventListener('pointercancel', onEnd);
      if (this.drag !== gesture) return;
      this.el.classList.remove('is-moving');
      this.drag = null;
    };
    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', onEnd);
    // `pointercancel` is not belt and braces: a touch drag that the browser turns into a
    // scroll fires it instead of `pointerup`, and without this the window stays stuck to
    // the finger.
    target.addEventListener('pointercancel', onEnd);
  }

  applyGesture(dx, dy) {
    const start = this.drag.box;
    if (this.drag.kind === 'move') {
      this.box = { ...start, x: start.x + dx, y: start.y + dy };
      this.apply();
      return;
    }

    // A resize moves edges, so it is computed as edges. Deriving a position and a size
    // separately is what makes the opposite edge drift: clamping `y` at the desktop top
    // while the height keeps growing slides the bottom edge down, which is a resize from
    // the north edge quietly becoming a move.
    const dir = this.drag.dir;
    let left = dir.includes('w') ? start.x + dx : start.x;
    let right = dir.includes('e') ? start.x + start.w + dx : start.x + start.w;
    let top = dir.includes('n') ? start.y + dy : start.y;
    let bottom = dir.includes('s') ? start.y + start.h + dy : start.y + start.h;

    // The title bar must not go above the desktop, and the edge being dragged is the one
    // that stops. The edge opposite it never moves during a resize.
    if (top < 0) top = 0;
    // The minimum is enforced on the edge under the pointer, so that edge stops rather than
    // the window walking in the other direction.
    if (right - left < MIN_W) {
      if (dir.includes('w')) left = right - MIN_W;
      else right = left + MIN_W;
    }
    if (bottom - top < MIN_H) {
      if (dir.includes('n')) top = bottom - MIN_H;
      else bottom = top + MIN_H;
    }
    // Enforcing the minimum height can push the top back above the desktop, so the clamp is
    // re-applied and the bottom follows it. Order matters: the minimum wins over the edge.
    if (top < 0) {
      bottom += -top;
      top = 0;
    }

    this.box = { x: left, y: top, w: right - left, h: bottom - top };
    this.apply();
  }

  onBarKey(event) {
    if (event.target.closest('.title-buttons')) return;
    if (event.key === 'Enter' || event.key === ' ') {
      if (!this.maximizable) return;
      event.preventDefault();
      this.toggleMaximize();
      return;
    }
    const deltas = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
    };
    const delta = deltas[event.key];
    if (!delta || this.state === 'max') return;
    event.preventDefault();
    const [kx, ky] = delta;
    // From `placed` for the same reason a drag is: an arrow moves the window from where it
    // is. Stepping from `box` would walk it off the desktop invisibly and make the way back
    // the same number of presses out.
    if (event.shiftKey) {
      if (!this.resizable) return;
      this.sizeTo(this.placed.w + kx * KEY_STEP, this.placed.h + ky * KEY_STEP);
    } else {
      this.moveTo(this.placed.x + kx * KEY_STEP, this.placed.y + ky * KEY_STEP);
    }
  }

  /* -------------------------------------------------------------------------
     State.
     ------------------------------------------------------------------------- */

  syncTitleButtons() {
    const max = this.state === 'max';
    this.maxBtn.textContent = max ? '❐' : '□';
    this.maxBtn.setAttribute('aria-label', max ? 'Restore' : 'Maximize');
  }

  async minimize() {
    if (!this.minimizable || this.state === 'min' || this.closed) return;
    this.restoreState = this.state;
    this.state = 'min';
    this.host.stateChanged(this);
    await zoom(this.el, this.host.anchorRect(this), 'out');
    // Marked `hidden` only once the zoom has finished, so the window is never seen
    // vanishing mid-slide, and it leaves the tab order for as long as it is minimised.
    if (this.state === 'min') this.el.hidden = true;
    this.host.announce(`${this.titleText} minimized`);
  }

  async restore() {
    if (this.state !== 'min' || this.closed) return;
    this.state = this.restoreState === 'max' ? 'max' : 'normal';
    this.el.hidden = false;
    this.host.stateChanged(this);
    await zoom(this.el, this.host.anchorRect(this), 'in');
  }

  maximize() {
    if (!this.maximizable || this.state === 'max') return;
    const from = this.el.getBoundingClientRect();
    this.state = 'max';
    this.el.classList.add('is-max');
    this.syncTitleButtons();
    this.host.stateChanged(this);
    zoom(this.el, from, 'in');
  }

  unmaximize() {
    if (this.state !== 'max') return;
    const from = this.el.getBoundingClientRect();
    this.state = 'normal';
    this.el.classList.remove('is-max');
    this.syncTitleButtons();
    // The box was never written while maximised, so this is the geometry the window had
    // before — which is the only reason a restore puts it back where it was.
    this.apply();
    this.host.stateChanged(this);
    zoom(this.el, from, 'in');
  }

  toggleMaximize() {
    if (this.state === 'max') this.unmaximize();
    else this.maximize();
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    if (this.onClose) this.onClose();
    if (!this.el.hidden) await zoom(this.el, this.host.anchorRect(this), 'out');
    this.el.remove();
    this.host.removed(this);
  }

  setActive(active) {
    this.el.classList.toggle('is-active', active);
  }

  /** Move keyboard focus into the window: the content if it has any, else the title bar. */
  takeFocus() {
    if (this.el.contains(document.activeElement)) return;
    const first = this.bodyEl.querySelector(
      'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    (first || this.bar).focus();
  }

  /* -------------------------------------------------------------------------
     Content.
     ------------------------------------------------------------------------- */

  setTitle(text) {
    this.titleText = text;
    this.headingEl.textContent = text;
    this.bar.setAttribute('aria-label', this.barLabel());
    this.host.titleChanged(this);
  }

  setStatus(text) {
    if (!this.statusEl) return;
    this.statusEl.textContent = text || '';
    this.statusEl.hidden = !text;
  }

  /** Swap the loading block for the real content. */
  setContent(nodes) {
    this.bodyEl.classList.remove('is-loading');
    this.bodyEl.replaceChildren(...[].concat(nodes || []));
  }

  get isOpen() {
    return !this.closed;
  }
}

/** The marquee that fills a window between the frame arriving and the content arriving. */
function loadingBlock() {
  const wrap = document.createElement('div');
  wrap.className = 'window-loading';
  const bar = document.createElement('div');
  bar.className = 'xp-progress';
  const blocks = document.createElement('div');
  blocks.className = 'xp-progress-blocks';
  bar.append(blocks);
  const label = document.createElement('p');
  // A `role="status"` would announce this on every window open, which is noise: the window
  // is already announced by its own region label.
  label.textContent = 'Loading…';
  wrap.append(bar, label);
  return wrap;
}
