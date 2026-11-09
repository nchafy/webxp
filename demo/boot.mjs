/* The host page.
 *
 * It owns the desktop's name and its wallpaper. It owns no programs: every app is a folder
 * under `apps/`, listed in the generated `apps/manifest.mjs`, and the loader registers them.
 * Adding an app to this desktop means adding a folder, never editing this file.
 */

import { Desktop } from '../src/webxp.mjs';
import { loadApps } from '../src/apps.mjs';
import { APPS } from '../apps/manifest.mjs';
import { mountClippy } from './clippy.mjs';

const desktop = new Desktop({
  root: document.getElementById('webxp'),
  name: 'webxp',
  tagline: 'a Windows XP window manager',
  footer: 'Programs are folders under apps/.',
});

desktop.surface.classList.add('wall-bliss');

// For the devtools console and for `scripts/check.py`, which reads geometry rather than
// trusting a screenshot. Published before the apps load, so a desktop whose apps failed is
// still inspectable.
window.webxp = desktop;

const { loaded, broken } = await loadApps(desktop, APPS);
// What loaded and what did not, for the console and for the checks. The loader already
// reports a broken app to the console and registers an entry that explains itself.
window.webxpApps = { loaded, broken };

await desktop.autostart();

// Clippy, in place of the Read me opening itself. Offering help and taking no for an answer
// is the same thought as an autostarting window, minus the part where it takes the desktop
// before you have asked for anything.
mountClippy({
  root: desktop.surface,
  onAccept: () => desktop.launch('readme'),
});
