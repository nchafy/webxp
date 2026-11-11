/* Context menus: the popup a right-click raises, and the keyboard route to it.
 *
 * Before this, a right-click anywhere in webxp reached Chrome's own menu. That is the first
 * thing anyone tries on a desktop claiming to be an OS, and it shows the browser through the
 * shell.
 *
 * The architecture is win32.run's, which is right: one host owns the layer, the open state
 * and the placement, and each surface contributes a `make()` the host calls at open time — so
 * the rows describe the state the surface is in now rather than the state it was in when the
 * menu was defined. `src/desktop.mjs` holds the shell's two definitions.
 *
 * The clamping is not win32.run's. Its `context_menu/CMFSItem.js:13-14` declares
 * `required_width: 180 + 20` and `required_height: 27 * 11 + 20` — hand-counted row heights
 * that are wrong the moment a label wraps or Tahoma falls back to Verdana. `placePopup`
 * renders the menu, measures it, and then places it.
 *
 * Keyboard access is why this is written rather than copied. Every menu row in win32.run is a
 * `div on:click`, reachable by pointer only. Here the popup is a `role="menu"` of real
 * buttons; it opens on the context-menu key and on Shift+F10 as well as on a right-click, and
 * the arrows, Home, End, Enter, a bare mnemonic and Escape all work.
 *
 * WHAT THIS SHARES WITH `widgets.menuBar`, AND WHERE IT DIVERGES
 * -------------------------------------------------------------
 * It shares the item vocabulary and the whole DOM contract, so `src/menu.css` styles both and
 * Luna's one popup look keeps one definition: `.menu-popup` holding `.menu-group` holding
 * `button.menu-item` with its `.menu-check`, `.menu-label` and `.menu-arrow`. A context menu
 * and a pull-down were the same popup in XP, and a second copy of that skin is the defect
 * `menu.css` already carries against `theme.css`'s `.card`.
 *
 * The parts of that contract that are code — the sheet's address, the depth cap, the mnemonic
 * parse, the underlined label, the measure-then-flip placement, and linking a sheet once — are
 * imported from `src/widgets.mjs` rather than restated here. They were a second copy for one
 * change, and the copies had already disagreed: the submenu-hover rule below was fixed here and
 * left wrong there.
 *
 * Three divergences, each because a context menu is not a pull-down:
 *
 *   no `accel`   A pull-down binds the accelerator it prints. Nothing on these surfaces has a
 *                chord this could bind — F5 and Alt+F4 belong to the browser — and a printed
 *                chord nothing answers is a lie, so an item declaring one throws.
 *   `selected`   One-of-N, as `role="menuitemradio"`. `menuBar` offers only `checked`, which
 *                toggles each row on its own, so Arrange Icons By built with it would report
 *                two rows checked at once.
 *   no state     The caller owns which row is on. This menu is built at every open and thrown
 *                away at every close, so a host-owned flip would be a second copy of the
 *                truth, discarded a moment later and announced by nobody.
 *
 * `buildRow` and `buildPopup` below are this module's own, because those three divergences all
 * live in them: which keys an item may carry, the one-of-N arithmetic that is checked per group,
 * and the absence of any state for the host to hold. One builder serving both shapes would need
 * a flag to tell its callers apart, which is the split `code-design.md` asks for instead.
 */

import {
  MENU_STYLESHEET, SUBMENU_DEPTH, labelNode, linkOnce, parseMnemonic, placePopup,
} from './widgets.mjs';

//: What a context menu needs that a pull-down does not. Two rules, both counted against
//: `menu.css` where they are declared.
const CONTEXT_STYLESHEET = new URL('./contextmenu.css', import.meta.url).href;

//: Every key an item may carry. An unknown one throws rather than being ignored, which is how
//: `accel` — accepted by `menuBar`, meaningless here — is refused with its name in the
//: message instead of silently doing nothing.
const ITEM_KEYS = Object.freeze(['label', 'selected', 'disabled', 'onSelect', 'groups']);

let serial = 0;

/**
 * The pointer, as the zero-size rect a menu hangs off.
 *
 * A pull-down hangs off its title's box; a context menu hangs off a point. `placePopup` takes
 * either, which is why there is one placement function — see its `anchor` parameter.
 */
function pointRect(x, y) {
  return { left: x, right: x, top: y, bottom: y };
}

/**
 * One row.
 *
 * @returns {{button: HTMLButtonElement, mnemonic: string|null, disabled: boolean,
 *            submenu: object|null, path: string, activate: () => void}}
 */
function buildRow(item, menuPath, depth) {
  if (!item || typeof item.label !== 'string' || !item.label.trim()) {
    throw new Error(`contextMenu: an item in ${menuPath} has no label`);
  }
  const parsed = parseMnemonic(item.label);
  const path = `${menuPath} > ${parsed.plain}`;
  for (const key of Object.keys(item)) {
    if (!ITEM_KEYS.includes(key)) {
      throw new Error(
        `contextMenu: ${path} carries an unknown key ${JSON.stringify(key)}; an item is `
        + `{ ${ITEM_KEYS.join(', ')} }`,
      );
    }
  }
  const hasSubmenu = Array.isArray(item.groups);
  const radio = item.selected !== undefined;
  const disabled = item.disabled === true;

  if (hasSubmenu && depth >= SUBMENU_DEPTH) {
    throw new Error(
      `contextMenu: ${path} is a submenu ${depth + 1} deep; one level is the most`,
    );
  }
  if (hasSubmenu && radio) {
    throw new Error(`contextMenu: ${path} is both a submenu and a one-of-N row`);
  }
  if (item.onSelect !== undefined && typeof item.onSelect !== 'function') {
    throw new Error(`contextMenu: ${path} has an onSelect that is not a function`);
  }
  // An absent feature does not leave its UI behind, so a row with nothing to do and nothing to
  // open is the one thing this refuses to render — unless it is disabled, which is a row
  // saying out loud that it is unavailable in the state the surface is in.
  if (!hasSubmenu && !item.onSelect && !disabled) {
    throw new Error(
      `contextMenu: ${path} has no onSelect and no submenu, so it would do nothing`,
    );
  }

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'menu-item';
  button.setAttribute('role', radio ? 'menuitemradio' : 'menuitem');
  button.tabIndex = -1;
  // `aria-disabled`, not the `disabled` attribute: a disabled button cannot be focused at all,
  // so the arrows would step over it and a reader would never be told the row is there.
  // Activation is refused in `select` instead.
  if (disabled) button.setAttribute('aria-disabled', 'true');
  if (radio) button.setAttribute('aria-checked', String(item.selected === true));

  // The gutter and the arrow are drawn, not spoken: `aria-checked` and `aria-expanded` carry
  // both already, and a glyph inside the name reads as "bullet Name".
  const gutter = document.createElement('span');
  gutter.className = 'menu-check';
  gutter.setAttribute('aria-hidden', 'true');
  button.append(gutter, labelNode(parsed));

  const row = {
    button, mnemonic: parsed.key, disabled, submenu: null, path,
  };

  if (hasSubmenu) {
    button.setAttribute('aria-haspopup', 'true');
    button.setAttribute('aria-expanded', 'false');
    const arrow = document.createElement('span');
    arrow.className = 'menu-arrow';
    arrow.setAttribute('aria-hidden', 'true');
    button.append(arrow);
    row.submenu = buildPopup({ label: parsed.plain, groups: item.groups }, path, depth + 1);
    button.setAttribute('aria-controls', row.submenu.popup.id);
  }

  // No `disabled` test here: `select` is the one gate every activation path goes through, and
  // a second copy of the refusal is one more thing to keep in step.
  row.activate = () => item.onSelect();
  return row;
}

/**
 * One popup, as `role="menu"` holding a `role="group"` per group.
 *
 * The grouping is the separator. A list of groups cannot carry a stray rule at its top or two
 * in a row, which a hand-placed separator can, and `role="group"` says to a reader what the
 * rule says to the eye.
 *
 * @returns {{popup: HTMLElement, rows: object[]}} `rows` in the order they are drawn.
 */
function buildPopup(menu, path, depth) {
  if (!Array.isArray(menu.groups) || !menu.groups.length) {
    throw new Error(`contextMenu: ${path} has no groups`);
  }
  const popup = document.createElement('div');
  // `.context-menu` on the outermost popup only. It carries one declaration — the z-index that
  // clears the taskbar — and a submenu is a child of that popup, so it is already inside the
  // stacking context the class created and needs `menu.css`'s own z-index relative to its
  // parent's rows instead. It also keeps "how many context menus are up" countable.
  popup.className = depth ? 'menu-popup' : 'menu-popup context-menu';
  popup.id = `context-menu-${++serial}`;
  popup.setAttribute('role', 'menu');
  // Named by its own label at every depth. A pull-down's top-level popup borrows the name of
  // the title button it hangs under; a context menu has no title, so the name has to be here
  // or the menu is announced as an unnamed list of items.
  popup.setAttribute('aria-label', menu.label);
  popup.hidden = true;

  const rows = [];
  for (const items of menu.groups) {
    if (!Array.isArray(items) || !items.length) {
      throw new Error(`contextMenu: ${path} has an empty group`);
    }
    // One-of-N is a property of the group, not of a row: the separators above and below the
    // group are what say "these five are the one choice". So the group is where the arithmetic
    // is checked, and a caller holding its own state cannot ship two bullets or none.
    const chosen = items.filter((item) => item && item.selected !== undefined);
    if (chosen.length && chosen.length !== items.length) {
      throw new Error(
        `contextMenu: a group in ${path} mixes one-of-N rows with plain ones; a radio group `
        + 'is the whole group, which is what the separators around it say',
      );
    }
    const on = chosen.filter((item) => item.selected === true);
    if (chosen.length && on.length !== 1) {
      throw new Error(
        `contextMenu: a one-of-N group in ${path} has ${on.length} rows marked selected; `
        + 'exactly one is on',
      );
    }

    const box = document.createElement('div');
    box.className = 'menu-group';
    box.setAttribute('role', 'group');
    for (const item of items) {
      const row = buildRow(item, path, depth);
      rows.push(row);
      if (!row.submenu) {
        box.append(row.button);
        continue;
      }
      // A submenu popup cannot sit inside its own button, so the pair share a wrapper.
      // `role="none"` is what keeps the rows children of the menu in the ARIA tree.
      const slot = document.createElement('div');
      slot.className = 'menu-slot';
      slot.setAttribute('role', 'none');
      slot.append(row.button, row.submenu.popup);
      box.append(slot);
    }
    popup.append(box);
  }

  // Two rows in one list cannot answer the same letter. The typeahead takes the first match,
  // so the second row draws an underline that does nothing — a dead affordance, which is the
  // same defect as a dead row and harder to see. Checked per popup and not per menu: a
  // submenu is its own list, so `&Properties` beside `&Program order` is only a collision if
  // they are siblings.
  //
  // Measured, which is why this exists: the desktop menu shipped `&Refresh` and
  // `P&roperties` for one pass, and pressing `r` on Properties ran Refresh.
  const letters = new Map();
  for (const row of rows) {
    if (!row.mnemonic) continue;
    if (letters.has(row.mnemonic)) {
      throw new Error(
        `contextMenu: ${row.path} and ${letters.get(row.mnemonic)} both answer the mnemonic `
        + `"${row.mnemonic}"; the first wins and the other underlines a letter that does `
        + 'nothing',
      );
    }
    letters.set(row.mnemonic, row.path);
  }
  return { popup, rows };
}

const step = (index, by, length) => (index + by + length) % length;

/**
 * The context menu host.
 *
 * One per desktop. It owns the open menu, where it is placed, and the two listeners an open
 * menu needs outside its own subtree; the surfaces own what their rows mean.
 *
 * There is no standing layer element. The popup is appended on open and removed on close, so
 * a closed host leaves nothing in the document and nothing on `document` or `window` — which
 * is also what makes "how many menus are up" a count rather than a flag.
 *
 * ```js
 * const menus = contextMenus({ root });
 * menus.bind(surface, ({ target }) => target === surface ? desktopMenu() : null);
 * ```
 *
 * `make` returns `{ label, groups }`, or `null` to decline — which leaves the browser's own
 * menu alone. Declining is how one binding on the desktop surface can answer for the surface
 * and its icons while a right-click inside a window keeps the menu the browser offers for a
 * text field, since webxp has no clipboard to put in its place.
 *
 * @param {{root: Element}} options  `root` is where the popup is appended: outside the
 *        desktop surface, which is `overflow: hidden`, and outside any window, so a menu
 *        belongs to no frame and outlives none.
 * @returns {{bind: Function, close: Function, destroy: Function, isOpen: boolean}}
 */
export function contextMenus(options) {
  if (!options || !(options.root instanceof Element)) {
    throw new Error('contextMenus: needs { root } — the element the popup is appended to');
  }
  // Not lazy, where `menuBar`'s is: a desktop builds its host here, in its constructor,
  // because a right-click can be the first thing a visitor does. So `menu.css` is fetched on
  // every load rather than only for a page whose program asks for a menu bar. Measured: one
  // `<link>` for it in the head, before `contextmenu.css`, with or without a menu bar up.
  linkOnce(MENU_STYLESHEET);
  linkOnce(CONTEXT_STYLESHEET);
  const { root } = options;

  //: The open menu, or null. One field rather than a stack: one menu is up at a time, and its
  //: submenu is tracked inside it because the depth is capped at one.
  let current = null;

  //: Set by `destroy`, and checked on every route that could put a menu up.
  //:
  //: The bindings live on the surfaces, and a title bar, a taskbar button and the desktop
  //: surface all outlive `destroy` — the root element is whoever built it to dispose of.
  //: Without this flag a replaced desktop still answered a right-click with a menu whose
  //: Close and Restore rows called back into it. Measured, before the flag existed: with
  //: `webxp.destroy()` called, a right-click on the surface put a full menu on screen.
  let dead = false;

  /**
   * Close on a press anywhere else, and on a resize.
   *
   * `pointerdown` in capture, as `desktop.mjs` does for the Start menu and for the reason
   * stated there: the menu has to be gone before the press underneath is read as a window
   * focus. `resize` closes rather than repositioning — the menu was placed against a point
   * measured when it opened, and a viewport that changed size under it has moved that point.
   *
   * The connection test covers the one case neither can unregister itself from: a root torn
   * out of the document with a menu still down.
   */
  const whileOpen = (event) => {
    if (!current || !current.popup.isConnected) {
      detach();
      current = null;
      return;
    }
    if (event.type === 'pointerdown' && current.popup.contains(event.target)) return;
    close();
  };

  function detach() {
    document.removeEventListener('pointerdown', whileOpen, true);
    window.removeEventListener('resize', whileOpen);
  }

  const closeSubmenu = () => {
    if (!current || !current.sub) return;
    current.sub.button.setAttribute('aria-expanded', 'false');
    current.sub.submenu.popup.hidden = true;
    current.sub = null;
  };

  /** Take the menu down, and give the keyboard back what it was standing on. */
  function close() {
    if (!current) return;
    // Read before anything is removed. Removing the element that holds focus hands focus to
    // the document body, so asking afterwards always answers "not in the menu" and the
    // keyboard is left on nothing.
    const held = current.popup.contains(document.activeElement);
    const { popup, opener } = current;
    // Cleared before the removal, not after: removing a focused element fires `focusout`
    // synchronously, and that handler calls back in here. With `current` already null the
    // re-entry returns at the guard above instead of reading a half-torn-down menu.
    current = null;
    detach();
    popup.remove();
    // Only if the keyboard was in the menu. Pulling focus back from wherever it has gone is
    // how a menu closing on a press elsewhere steals focus from the thing that took it.
    //
    // An opener that cannot hold focus is a `focus()` that does nothing, and the removal has
    // already handed focus to `document.body`. That is the right landing and not a dropped
    // one: the only surface here that is not focusable is the bare wallpaper, where a press
    // clears the shell's focus anyway — `Desktop.blurAll`, pinned by `desktop blur`. Every
    // keyboard route into this menu starts from something focusable and gets it back.
    if (held && opener && opener.isConnected) opener.focus();
  }

  /** The rows a keypress navigates: the open submenu's, or the menu's own. */
  const rowsAround = (row) => {
    const sub = current.sub;
    return sub && sub.submenu.rows.includes(row) ? sub.submenu.rows : current.rows;
  };

  const openSubmenuOf = (row, land) => {
    if (current.sub && current.sub.button === row.button) {
      if (land === 'first') row.submenu.rows[0].button.focus();
      return;
    }
    closeSubmenu();
    row.button.setAttribute('aria-expanded', 'true');
    placePopup(row.submenu.popup, row.button.getBoundingClientRect(), { beside: true });
    current.sub = { button: row.button, submenu: row.submenu };
    if (land === 'first') row.submenu.rows[0].button.focus();
  };

  /** Back out to the row the submenu hangs off. ArrowLeft and Escape both do this. */
  const leaveSubmenu = () => {
    const parent = current.rows.find((row) => row.button === current.sub.button);
    closeSubmenu();
    parent.button.focus();
  };

  /** The one gate: a click, an Enter, a Space and a bare mnemonic all arrive here. */
  const select = (row) => {
    if (row.disabled) return;
    if (row.submenu) {
      openSubmenuOf(row, 'first');
      return;
    }
    // Closed first, so whatever the row does to focus is the last move and is not undone by
    // the menu handing focus back to the surface it opened from.
    const run = row.activate;
    close();
    run();
  };

  const onKey = (event) => {
    if (!current) return;
    const row = current.byButton.get(event.target);
    // Tab has no next stop to go to: the popup is appended to the root, so letting the
    // default run would land focus on whatever happens to follow it in the document. It
    // closes instead, which is also what XP did.
    if (event.key === 'Tab' || event.key === 'Escape') {
      event.preventDefault();
      if (event.key === 'Escape' && current.sub && row && rowsAround(row) !== current.rows) {
        leaveSubmenu();
        return;
      }
      close();
      return;
    }
    if (!row) return;

    const rows = rowsAround(row);
    const at = rows.indexOf(row);
    const inSub = rows !== current.rows;
    switch (event.key) {
      case 'ArrowDown': rows[step(at, 1, rows.length)].button.focus(); break;
      case 'ArrowUp': rows[step(at, -1, rows.length)].button.focus(); break;
      case 'Home': rows[0].button.focus(); break;
      case 'End': rows[rows.length - 1].button.focus(); break;
      case 'ArrowRight':
        // Nothing to the right of a row with no submenu. A menu bar would step to the next
        // menu along the bar; a context menu has no bar and no next menu.
        if (row.submenu) openSubmenuOf(row, 'first');
        break;
      case 'ArrowLeft':
        if (inSub) leaveSubmenu();
        break;
      default: {
        // XP activated a row on its bare mnemonic, with no Enter. Enter and Space need no case
        // of their own: a `button` fires `click` on both.
        const hit = rows.find((other) => other.mnemonic
          && other.mnemonic === event.key.toLowerCase() && !other.disabled);
        if (!hit) return;
        select(hit);
        break;
      }
    }
    event.preventDefault();
  };

  /**
   * Put a menu up.
   *
   * Focus lands on the first row by both routes, which is WAI-ARIA's menu pattern
   * (https://www.w3.org/WAI/ARIA/apg/patterns/menu/) and the only arrangement that gives
   * Escape and the arrows somewhere to start from. XP highlighted no row until the pointer
   * picked one, and that was tried here: it needs the popup itself to be a `tabindex="-1"`
   * focus target, and a focused `[tabindex]` matches `theme.css`'s `:focus-visible` rule,
   * whose `box-shadow` then replaces the whole Luna bevel. Measured — the popup drew a navy
   * halo and lost its inset edge and its drop shadow. One focus policy also means one close
   * path rather than two.
   *
   * @param {{label: string, groups: object[][]}} menu
   * @param {{left: number, right: number, top: number, bottom: number}} anchor
   * @param {Element} opener  Where the keyboard goes back to on close.
   */
  function openAt(menu, anchor, opener) {
    close();
    if (!menu || typeof menu.label !== 'string' || !menu.label.trim()) {
      throw new Error('contextMenu: a menu needs a label, which names it for a screen reader');
    }
    const built = buildPopup(menu, menu.label, 0);
    const byButton = new Map();
    const walk = (rows) => rows.forEach((row) => {
      byButton.set(row.button, row);
      if (row.submenu) walk(row.submenu.rows);
    });
    walk(built.rows);

    root.append(built.popup);
    placePopup(built.popup, anchor, { beside: false });
    current = { ...built, byButton, opener, sub: null };

    built.popup.addEventListener('keydown', onKey);
    // A right-click inside our own menu must not reach the browser's.
    built.popup.addEventListener('contextmenu', (event) => event.preventDefault());
    built.popup.addEventListener('click', (event) => {
      const row = byButton.get(event.target.closest('.menu-item'));
      if (row) select(row);
    });
    // The pointer moves the highlight, which in a menu is the focus.
    //
    // `pointerover` and not `pointerenter`, because this is one delegated listener for the
    // whole tree and `pointerenter` does not bubble.
    //
    // Only a row of the *parent* list closes an open submenu. "Any row that is not the one
    // the submenu hangs off" is the obvious test and it is wrong: every row inside the
    // submenu passes it, so the submenu closes under the pointer that has just travelled
    // into it, and the press that follows lands on the surface behind. Measured, before this
    // read `current.rows.includes(row)`: hovering `Arrange Icons By` then pressing `Name`
    // left the icons in their old order and the live region empty.
    built.popup.addEventListener('pointerover', (event) => {
      const row = byButton.get(event.target.closest('.menu-item'));
      if (!row) return;
      row.button.focus();
      if (row.submenu) {
        openSubmenuOf(row, 'none');
        return;
      }
      if (current.sub && current.rows.includes(row)) closeSubmenu();
    });
    // Focus leaving the menu by any route other than a key this handles — a script moving it,
    // a window the shell raised — takes the menu with it. Belt to `whileOpen`'s braces, which
    // sees only a pointerdown and a resize. `focusout` fires only in a document that has
    // focus, which a headless browser needs focus emulation for; the suite turns it on for
    // the whole run and `menu keyboard` asserts both edges on the menu bar's identical
    // handler. It also fires synchronously when `close` removes the focused row, which is
    // what the re-entrancy guard there is for.
    built.popup.addEventListener('focusout', (event) => {
      if (built.popup.contains(event.relatedTarget)) return;
      close();
    });

    document.addEventListener('pointerdown', whileOpen, true);
    window.addEventListener('resize', whileOpen);
    built.rows[0].button.focus();
  }

  return {
    /**
     * Give a surface a context menu.
     *
     * Three routes, because a right-click is not the only one. The context-menu key and
     * Shift+F10 are how Windows itself opens this menu without a mouse, and they are the
     * reason this control exists rather than win32.run's: every row there is a `div on:click`.
     *
     * @param {Element} element
     * @param {(ctx: {target: Element}) => ({label: string, groups: object[][]}|null)} make
     *        Called at every open, so the rows describe the state the surface is in now.
     *        `target` is what the event landed on, which is how one binding tells a desktop
     *        icon from the wallpaper. Return `null` to decline and leave the browser's menu.
     */
    bind(element, make) {
      if (!(element instanceof Element)) {
        throw new Error('contextMenus.bind: needs an element to bind to');
      }
      if (typeof make !== 'function') {
        throw new Error(`contextMenus.bind: ${element.className || element.tagName} was given `
          + 'no make() to build its menu');
      }
      if (dead) {
        throw new Error('contextMenus.bind: this host is destroyed, so the surface would get '
          + 'a menu nothing can take down');
      }
      /** The one entry point both routes go through, so the refusals are stated once. */
      const raise = (event, anchor) => {
        if (dead) return;
        const menu = make({ target: event.target });
        // A declined surface keeps the browser's own menu, which is the point of `null`.
        // `preventDefault` comes after the decision for exactly that reason.
        if (!menu) return;
        event.preventDefault();
        openAt(menu, anchor(), event.target);
      };
      element.addEventListener('contextmenu', (event) => {
        raise(event, () => pointRect(event.clientX, event.clientY));
      });
      element.addEventListener('keydown', (event) => {
        if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
        // Anchored at the box the keyboard is standing on, not at the pointer, which is
        // wherever it was last left. A target with no box of its own falls back to the bound
        // element's.
        raise(event, () => {
          const box = event.target.getBoundingClientRect();
          return box.width ? box : element.getBoundingClientRect();
        });
      });
    },
    close,
    /** Take any open menu down, leave nothing on `document` or `window`, and go inert. */
    destroy() {
      close();
      detach();
      dead = true;
    },
    get isOpen() {
      return current !== null;
    },
  };
}
