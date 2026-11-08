/* The detail panel: a sliding record that docks over the map.
 *
 * Map furniture, not shell furniture. webxp supplies the window, the taskbar, the group
 * boxes and the tables; it has no opinion about a panel, because a panel is something a
 * *map* wants when a reader clicks a feature.
 *
 * `chrome.mjs` builds the element; this opens, fills and closes it.
 *
 * It stays a panel inside the map's own window rather than becoming a second window. A
 * second window is the more webxp-native answer and would be worse here: the panel always
 * describes the thing selected in one particular map, so it would have to be opened,
 * raised, closed and repositioned in step with its map, and the two would drift the first
 * time someone closed the wrong one.
 */

//: How long the panel takes to slide, matching the transition in `map.css`. The element
//: is marked `hidden` only once the slide has finished, so it is never seen vanishing
//: mid-slide and it stays out of the tab order the rest of the time.
const SLIDE_MS = 220;

export class Panel {
  constructor(root) {
    this.root = root;
    this.head = root.querySelector('.detail-head h2');
    this.body = root.querySelector('.detail-body');
    this.foot = root.querySelector('.detail-foot');
    this.timer = null;
    this.onClose = null;
    //: True while `onClose` is running, so a `close()` from inside it is a no-op. See
    //: `close`.
    this.closing = false;

    const close = root.querySelector('.detail-close');
    if (close) close.addEventListener('click', () => this.close());
  }

  /**
   * Show the panel.
   *
   * @param {string} title         The title-bar text.
   * @param {Node|Node[]} content  What goes in the body.
   * @param {Node|string} [foot]   An optional footer, for a link onward or a provenance line.
   */
  open(title, content, foot) {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.head) this.head.textContent = title;
    if (this.body) this.body.replaceChildren(...[].concat(content || []));
    if (this.foot) {
      if (foot instanceof Node) this.foot.replaceChildren(foot);
      else this.foot.textContent = foot || '';
    }

    // `hidden` comes off first and the class goes on in a later frame: setting both in one
    // go gives the browser no start state to animate from, so the panel snaps instead of
    // sliding. Two nested frames guarantee a layout in between.
    this.root.classList.remove('is-open');
    this.root.hidden = false;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => this.root.classList.add('is-open'));
    });
  }

  /**
   * Close the panel, and tell the owner once.
   *
   * Re-entrant by necessity, not by taste. `onClose` is where an app undoes whatever the
   * panel was describing, and the natural way to write that is to call the app's own
   * "clear everything" routine — which itself closes the panel, because closing it is part
   * of clearing. That is a cycle: `close` -> `onClose` -> clear -> `close`.
   *
   * `hidden` cannot break it, because it is only set when the slide finishes; for those
   * 220 ms the panel is still open by every test available. So a flag is set *before*
   * `onClose` runs and a second call during it returns immediately. Without this, clicking
   * the panel's close box threw `RangeError: Maximum call stack size exceeded` out of
   * MapLibre's `setPaintProperty`, which named neither the panel nor the cycle.
   */
  close() {
    if (this.root.hidden || this.closing) return;
    this.closing = true;
    this.root.classList.remove('is-open');
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.root.hidden = true;
      this.timer = null;
    }, SLIDE_MS);
    try {
      if (this.onClose) this.onClose();
    } finally {
      // Cleared in `finally` so a throwing `onClose` cannot wedge the panel shut.
      this.closing = false;
    }
  }

  get isOpen() {
    return !this.root.hidden;
  }
}
