/* The public surface. One import for a page that wants a desktop.
 *
 *     import { Desktop } from './src/webxp.mjs';
 *
 *     new Desktop({ root: document.getElementById('webxp'), programs: [...] }).autostart();
 *
 * The three stylesheets are separate because they have separate subjects: `theme.css` is
 * the palette and the controls, `shell.css` is the desktop and the taskbar, `window.css` is
 * the frame. A page that only wants the Luna controls links the first and nothing else.
 */

export { Desktop } from './desktop.mjs';
export { XPWindow, MIN_W, MIN_H, zoom } from './window.mjs';
export { icon, ICON_NAMES } from './icons.mjs';
export { group, table, sheet, segment } from './widgets.mjs';
