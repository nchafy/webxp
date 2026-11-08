/* Webview — a whole page hosted in a window, with no mount of its own.
 *
 * This is the route for integrating a project that already exists and already works: point
 * `frame.src` at it and it is on the desktop. Nothing is rewritten, and nothing about the
 * hosted page needs to know it is in a window.
 *
 * `src` is resolved against this folder, so a relative path keeps the app portable; an
 * absolute URL works the same way. The cost of this route is that the hosted page does not
 * inherit the Luna theme and cannot use `src/widgets.mjs` — it is a separate document. Port
 * to a `mount` when you want it to look like part of the desktop.
 */

export default {
  id: 'webview',
  name: 'Webview',
  icon: 'globe',
  singleton: true,
  onDesktop: true,
  width: 560,
  height: 380,
  status: true,

  frame: {
    src: 'page.html',
    // Spelled out rather than left to the default, so what hosted content may do is visible
    // in the app that hosts it. `allow-same-origin` is what lets the page load its own
    // relative assets; drop it for content you do not control.
    sandbox: 'allow-same-origin allow-scripts',
  },
};
