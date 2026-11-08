/* The shell: the desktop surface, the taskbar, the Start menu, and the z-order.
 *
 * `Desktop` is the only implementation of the `host` interface `window.mjs` expects, and it
 * is what a page talks to. A program is registered once and launched as often as it likes;
 * the shell owns which window is on top, which taskbar button is pressed, and what gets
 * announced.
 *
 * Every node here is built with DOM calls and `textContent`. The shell is not in the page's
 * markup, because windows are not: one builder keeps the two in step.
 */

import { XPWindow, zoom } from './window.mjs';
import { icon } from './icons.mjs';

//: The lowest z-index a window gets. Above the desktop icons, below the taskbar.
const Z_BASE = 10;

//: How long the frame shows its marquee before the content replaces it, when the content
//: was ready sooner.
//:
//: This is deliberate latency, which is normally a defect. It is the thing being imitated:
//: XP gave you the frame first and filled it afterwards, and a window whose content is
//: already there when the frame lands reads as a div appearing, not as a program starting.
//: Set `minLoadMs: 0` on the desktop or on a program to drop it.
const MIN_LOAD_MS = 120;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * What a window shows when its program's `mount` threw.
 *
 * It names the program and the error. "Something went wrong" in a window with a title bar
 * and a taskbar button is worse than a hang, because it looks like a finished state.
 */
function mountFailure(program, error) {
  const box = document.createElement('fieldset');
  box.className = 'group';
  const legend = document.createElement('legend');
  legend.textContent = `${program.name} could not start`;
  const what = document.createElement('p');
  what.className = 'group-line';
  what.textContent = error && error.message ? error.message : String(error);
  const where = document.createElement('p');
  where.className = 'group-line';
  where.textContent = `Thrown by the mount of program "${program.id}". The console carries the stack.`;
  box.append(legend, what, where);

  const sheet = document.createElement('div');
  sheet.className = 'sheet';
  sheet.append(box);
  return sheet;
}

export class Desktop {
  /**
   * @param {object} options
   * @param {HTMLElement} options.root       The mount point. Emptied.
   * @param {string} [options.name]          The Start menu's heading.
   * @param {string} [options.tagline]       A second line under it.
   * @param {string} [options.footer]        A line at the foot of the Start menu.
   * @param {object[]} [options.programs]    Registered up front; see `register`.
   * @param {number} [options.minLoadMs]
   */
  constructor(options) {
    this.programs = new Map();
    this.windows = [];
    this.active = null;
    this.z = Z_BASE;
    this.opened = 0;
    this.minLoadMs = Number.isFinite(options.minLoadMs) ? options.minLoadMs : MIN_LOAD_MS;

    this.ready = false;
    this.build(options);
    for (const program of options.programs || []) this.register(program);
    this.ready = true;
    this.renderStartMenu();
    this.renderDesktopIcons();
    this.watchViewport();
  }

  /**
   * Re-clamp every window when the desktop changes size.
   *
   * Without this a window is clamped only when it moves. Shrink the browser and a window at
   * x=1000 on a desktop that is now 520 wide stays at x=1000: it and its title bar are
   * entirely off the surface, and the title bar is the only way to move a window, so there
   * is no pointer or keyboard route back. That is the trap this project claims to have
   * removed from XP, reachable by resizing the browser.
   *
   * Coalesced to one frame, because a browser resize fires continuously while dragged and
   * every window re-clamps on each event.
   */
  watchViewport() {
    let queued = false;
    window.addEventListener('resize', () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        // A maximised window takes its size from CSS, so it needs no arithmetic. A
        // minimised one is re-clamped too, or it comes back out of the taskbar off-screen.
        for (const win of this.windows) {
          if (!win.closed && win.state !== 'max') win.apply();
        }
      });
    });
  }

  /* -------------------------------------------------------------------------
     The shell's own markup.
     ------------------------------------------------------------------------- */

  build(options) {
    const root = options.root;
    root.replaceChildren();

    const surface = document.createElement('div');
    surface.className = 'desktop';
    // The desktop is not a landmark and holds no content of its own; the windows inside it
    // carry their own names.
    surface.id = 'desktop';

    const icons = document.createElement('ul');
    icons.className = 'desktop-icons';
    icons.setAttribute('aria-label', 'Desktop');
    surface.append(icons);

    // Clicking bare desktop drops focus, which is how XP cleared the pressed taskbar
    // button. Only a click on the surface itself counts: a click inside a window is that
    // window's business.
    surface.addEventListener('pointerdown', (event) => {
      if (event.target === surface || event.target === icons) this.blurAll();
    });

    const taskbar = document.createElement('nav');
    taskbar.className = 'taskbar';
    taskbar.setAttribute('aria-label', 'Taskbar');

    const start = document.createElement('button');
    start.type = 'button';
    start.className = 'start';
    start.id = 'start';
    start.textContent = 'start';
    start.setAttribute('aria-expanded', 'false');
    start.setAttribute('aria-controls', 'start-menu');

    const items = document.createElement('ul');
    items.className = 'task-items';
    items.setAttribute('aria-label', 'Open windows');

    const tray = document.createElement('div');
    tray.className = 'tray';
    const clock = document.createElement('span');
    tray.append(clock);

    taskbar.append(start, items, tray);

    const menu = document.createElement('div');
    menu.className = 'start-menu';
    menu.id = 'start-menu';
    menu.hidden = true;
    const head = document.createElement('div');
    head.className = 'start-menu-head';
    head.textContent = options.name || 'webxp';
    if (options.tagline) {
      const small = document.createElement('small');
      small.textContent = options.tagline;
      head.append(small);
    }
    const list = document.createElement('ul');
    const foot = document.createElement('p');
    foot.className = 'start-menu-foot';
    foot.textContent = options.footer || '';
    foot.hidden = !options.footer;
    menu.append(head, list, foot);

    // The live region. State a sighted reader gets from the taskbar — a window minimised,
    // restored or closed — has to be said out loud for a reader who cannot see it.
    const live = document.createElement('p');
    live.className = 'visually-hidden';
    // Named, because an app may have a live region of its own — the Base map app does, for
    // its basemap and tile status — and "the first element with aria-live" then stops
    // meaning the shell's. Anything looking for the shell's announcements wants this one.
    live.id = 'webxp-live';
    live.setAttribute('role', 'status');
    live.setAttribute('aria-live', 'polite');

    root.append(surface, taskbar, menu, live);

    this.surface = surface;
    this.iconsEl = icons;
    this.taskItems = items;
    this.startBtn = start;
    this.menu = menu;
    this.menuList = list;
    this.live = live;

    this.wireStartMenu();
    this.tick(clock);
  }

  /* -------------------------------------------------------------------------
     The clock.
     ------------------------------------------------------------------------- */

  /** XP's tray clock: hours and minutes, no seconds. */
  tick(element) {
    const now = new Date();
    element.textContent = now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    // Scheduled to the next minute boundary rather than every second: a clock with no
    // seconds hand has nothing to say in between, and a 1 s interval wakes the page 60x for
    // nothing.
    const msToNextMinute = (60 - now.getSeconds()) * 1000 - now.getMilliseconds();
    setTimeout(() => this.tick(element), msToNextMinute + 50);
  }

  /* -------------------------------------------------------------------------
     Programs.
     ------------------------------------------------------------------------- */

  /**
   * Register a program.
   *
   * @param {object} program
   * @param {string} program.id
   * @param {string} program.name          The window title and the menu label.
   * @param {string} [program.icon]        A name `icons.mjs` knows.
   * @param {boolean} [program.singleton]  A second launch focuses the open window.
   * @param {boolean} [program.onDesktop]  Also give it a desktop icon.
   * @param {boolean} [program.autostart]  Open it when the desktop mounts.
   * @param {(ctx: {win: XPWindow, desktop: Desktop}) => Node|Node[]|Promise<Node|Node[]>}
   *        program.mount                  Builds the content. May be async.
   */
  register(program) {
    this.programs.set(program.id, program);
    // Re-rendered, so a program registered after construction is reachable from the Start
    // menu and the desktop rather than from code only. `ready` keeps the constructor's own
    // registration loop from rendering once per program.
    if (this.ready) {
      this.renderStartMenu();
      this.renderDesktopIcons();
    }
  }

  /**
   * Open every program marked `autostart`, in registration order.
   *
   * One program that fails to start must not stop the ones after it. `launch` already turns
   * a thrown `mount` into a window that says so, so this only has to survive a rejection
   * from anywhere else in the sequence.
   */
  async autostart() {
    for (const program of this.programs.values()) {
      if (!program.autostart) continue;
      try {
        await this.launch(program.id);
      } catch (error) {
        console.error(`webxp: autostart failed for program ${program.id}`, error);
      }
    }
  }

  /**
   * Open a program.
   *
   * The sequence is the point: the frame arrives, zooms up from its own taskbar button, and
   * only then fills. `MIN_LOAD_MS` explains why the fill is not immediate.
   *
   * @param {string} id
   * @returns {Promise<XPWindow|null>}
   */
  async launch(id) {
    const program = this.programs.get(id);
    if (!program) return null;

    if (program.singleton) {
      const open = this.windows.find((w) => w.programId === id);
      if (open) {
        if (open.state === 'min') await open.restore();
        this.focus(open, { moveFocus: true });
        return open;
      }
    }

    const win = new XPWindow({
      title: program.name,
      iconName: program.icon,
      width: program.width,
      height: program.height,
      x: program.x,
      y: program.y,
      resizable: program.resizable,
      minimizable: program.minimizable,
      maximizable: program.maximizable,
      maximized: program.maximized,
      status: program.status,
      bodyClass: program.bodyClass,
    }, this);
    win.programId = id;

    this.opened += 1;
    this.windows.push(win);
    this.surface.append(win.el);
    this.addTaskButton(win);
    this.focus(win);

    const started = performance.now();
    // The zoom and the mount run together: the content is usually ready before the frame
    // has finished arriving, which is what makes the marquee a glimpse rather than a wait.
    //
    // A `mount` that throws must not leave the window on its marquee forever. The frame is
    // already on screen and already has a taskbar button, so there is nothing to roll back
    // to — the honest outcome is a window that says what failed. Swallowing it silently
    // would be worse than the hang it replaces, so the error is re-reported to the console
    // with the program's id on it.
    let content;
    try {
      [content] = await Promise.all([
        Promise.resolve().then(() => program.mount({ win, desktop: this })),
        zoom(win.el, this.anchorRect(win), 'in'),
      ]);
    } catch (error) {
      if (win.closed) return win;
      win.setContent(mountFailure(program, error));
      win.setStatus(`${program.id} failed to start`);
      this.announce(`${program.name} failed to start`);
      console.error(`webxp: mount failed for program ${program.id}`, error);
      return win;
    }

    if (win.closed) return win;
    const floor = Number.isFinite(program.minLoadMs) ? program.minLoadMs : this.minLoadMs;
    const left = floor - (performance.now() - started);
    if (left > 0) await sleep(left);
    if (win.closed) return win;

    win.setContent(content);
    win.takeFocus();

    // `ready` runs once the content is in the document and laid out.
    //
    // It exists because `mount` cannot do this: what `mount` returns is what gets inserted,
    // so during `mount` the nodes have no box. Anything that measures its container — a map,
    // a chart, a canvas — reads zero there and draws nothing, which is invisible to an
    // assertion that the window filled. Two frames, so a layout has happened in between.
    if (typeof program.ready === 'function') {
      await new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      });
      if (win.closed) return win;
      try {
        await program.ready({ win, desktop: this });
      } catch (error) {
        // Same treatment as a failed mount: the window says what broke rather than looking
        // finished and being empty.
        win.setContent(mountFailure(program, error));
        win.setStatus(`${program.id} failed to start`);
        this.announce(`${program.name} failed to start`);
        console.error(`webxp: ready failed for program ${program.id}`, error);
      }
    }
    return win;
  }

  /* -------------------------------------------------------------------------
     Focus and z-order.
     ------------------------------------------------------------------------- */

  /**
   * Raise a window and make it the active one.
   *
   * @param {XPWindow} win
   * @param {{moveFocus?: boolean}} [options] `moveFocus` also moves the keyboard caret into
   *        the window, which is right for a launch or a taskbar click and wrong for a click
   *        that landed on a control inside it.
   */
  focus(win, options = {}) {
    if (win.closed) return;
    if (this.active !== win) {
      if (this.active) this.active.setActive(false);
      this.active = win;
      win.setActive(true);
      this.z += 1;
      win.el.style.setProperty('--win-z', String(this.z));
    }
    this.syncTaskButtons();
    if (options.moveFocus) win.takeFocus();
  }

  blurAll() {
    if (!this.active) return;
    this.active.setActive(false);
    this.active = null;
    this.syncTaskButtons();
  }

  /** The window a reader should land on when the active one goes away. */
  focusTopmost() {
    const candidates = this.windows.filter((w) => !w.closed && w.state !== 'min');
    if (!candidates.length) {
      this.active = null;
      this.syncTaskButtons();
      return;
    }
    const top = candidates.reduce((best, w) => {
      const z = Number(w.el.style.getPropertyValue('--win-z')) || 0;
      const bestZ = Number(best.el.style.getPropertyValue('--win-z')) || 0;
      return z > bestZ ? w : best;
    });
    this.focus(top);
  }

  /* -------------------------------------------------------------------------
     The taskbar.
     ------------------------------------------------------------------------- */

  addTaskButton(win) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'task-button';
    button.append(icon(win.iconName, 16));
    const label = document.createElement('span');
    label.textContent = win.titleText;
    button.append(label);
    button.setAttribute('aria-pressed', 'false');
    button.setAttribute('aria-controls', win.id);

    button.addEventListener('click', () => {
      // XP's rule: the button for the window you are looking at puts it away. Any other
      // button brings its window up. One control, both directions, no second button.
      if (win.state === 'min') {
        win.restore().then(() => this.focus(win, { moveFocus: true }));
        return;
      }
      if (this.active === win) {
        win.minimize();
        button.focus();
        return;
      }
      this.focus(win, { moveFocus: true });
    });

    item.append(button);
    this.taskItems.append(item);
    win.taskItem = item;
    win.taskButton = button;
    win.taskLabel = label;
  }

  syncTaskButtons() {
    for (const win of this.windows) {
      if (!win.taskButton) continue;
      const pressed = this.active === win && win.state !== 'min';
      win.taskButton.setAttribute('aria-pressed', String(pressed));
    }
  }

  /* -------------------------------------------------------------------------
     The Start menu and the desktop icons.
     ------------------------------------------------------------------------- */

  wireStartMenu() {
    const setOpen = (open) => {
      this.menu.hidden = !open;
      this.startBtn.setAttribute('aria-expanded', String(open));
      if (open) {
        const first = this.menu.querySelector('.start-item');
        if (first) first.focus();
      }
    };
    this.setStartOpen = setOpen;

    this.startBtn.addEventListener('click', () => setOpen(this.menu.hidden));

    // Escape closes and returns focus to the button that opened it, which is the only way
    // back for a keyboard once focus has moved into the menu.
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || this.menu.hidden) return;
      setOpen(false);
      this.startBtn.focus();
    });

    // A click anywhere else closes it. `pointerdown` rather than `click`, so the menu is
    // gone before the click underneath is interpreted as a window focus.
    document.addEventListener('pointerdown', (event) => {
      if (this.menu.hidden) return;
      if (this.menu.contains(event.target) || this.startBtn.contains(event.target)) return;
      setOpen(false);
    });
  }

  renderStartMenu() {
    const rows = [...this.programs.values()].map((program) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'start-item';
      button.append(icon(program.icon, 16));
      const name = document.createElement('span');
      name.textContent = program.name;
      button.append(name);
      if (program.singleton) {
        const hint = document.createElement('span');
        hint.className = 'hint';
        hint.textContent = 'one window';
        button.append(hint);
      }
      button.addEventListener('click', () => {
        this.setStartOpen(false);
        this.launch(program.id);
      });
      item.append(button);
      return item;
    });
    this.menuList.replaceChildren(...rows);
  }

  renderDesktopIcons() {
    const rows = [...this.programs.values()]
      .filter((program) => program.onDesktop)
      .map((program) => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'desktop-icon';
        button.append(icon(program.icon, 32));
        const name = document.createElement('span');
        name.textContent = program.name;
        button.append(name);
        // XP opened a desktop icon on a double click. A single activation is used here
        // because a double click has no keyboard equivalent and no touch equivalent; the
        // Start menu lists the same programs, so neither route is the only one.
        button.addEventListener('click', () => this.launch(program.id));
        item.append(button);
        return item;
      });
    this.iconsEl.replaceChildren(...rows);
  }

  /* -------------------------------------------------------------------------
     The host interface `window.mjs` calls back into.
     ------------------------------------------------------------------------- */

  /** Where a window's zoom starts and ends: its own taskbar button. */
  anchorRect(win) {
    if (!win.taskButton) return null;
    const rect = win.taskButton.getBoundingClientRect();
    return rect.width ? rect : null;
  }

  cascadeIndex() {
    return this.opened;
  }

  /** How far in from the left the cascade should start: past the icon column, if there is
      one. Measured rather than assumed, so a desktop with no icons starts at the edge. */
  reservedLeft() {
    if (!this.iconsEl.children.length) return 20;
    const rect = this.iconsEl.getBoundingClientRect();
    const surface = this.surface.getBoundingClientRect();
    return Math.round(rect.right - surface.left) + 12;
  }

  announce(message) {
    // Cleared first: two identical messages in a row are otherwise read once.
    this.live.textContent = '';
    this.live.textContent = message;
  }

  /** A window was minimised, restored, maximised or un-maximised. */
  stateChanged(win) {
    if (win.state === 'min') {
      if (this.active === win) {
        this.active.setActive(false);
        this.active = null;
        this.focusTopmost();
        return;
      }
      this.syncTaskButtons();
      return;
    }
    // A window coming back out of the taskbar comes back on top. Everything else —
    // maximise, restore from maximise — leaves the order alone.
    if (this.active !== win) this.focus(win);
    else this.syncTaskButtons();
  }

  /** A window renamed itself. Its taskbar button is the only thing that mirrors the title,
      and renaming must not move the window to the front. */
  titleChanged(win) {
    if (win.taskLabel) win.taskLabel.textContent = win.titleText;
  }

  removed(win) {
    this.windows = this.windows.filter((w) => w !== win);
    if (win.taskItem) win.taskItem.remove();
    if (this.active === win) this.active = null;
    this.announce(`${win.titleText} closed`);
    this.focusTopmost();
  }
}
