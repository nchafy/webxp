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

// A link, if the address bar carries one.
//
// After the apps are registered, because a route names a program and the registry is what
// says whether that program exists. After `autostart` too: a link is a request for one
// particular window, so it opens last and ends up on top.
//
// The host page does this rather than `Desktop`, for the same reason it will read
// `~/Startup`: deciding what opens is the environment's business and opening it is the
// window manager's. `src/` stays testable without a URL or a filesystem.
const routes = await import('../shared/routes/routes.mjs');
// Published for the devtools console and for `scripts/check.py`, the same way `window.webxp`
// and `window.webxpApps` are. A check that imported its own copy would be asserting about a
// second module instance rather than the one the desktop is using — and an `import()` from a
// page script turned out to be flaky against a single-threaded `http.server`, which is a
// reason to use the page's own object rather than a reason to retry.
window.webxpRoutes = routes;
window.webxpRoute = await routes.restore(desktop);

// Clippy, in place of the Read me opening itself. Offering help and taking no for an answer
// is the same thought as an autostarting window, minus the part where it takes the desktop
// before you have asked for anything.
mountClippy({
  root: desktop.surface,
  onAccept: () => desktop.launch('readme'),
});
