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
// and `window.webxpApps` are.
//
// Not because an import would give a check its own copy — it would not. ES modules dedupe by
// resolved URL within a realm, so `await import('/shared/routes/routes.mjs')` from a page
// script hands back this very object; measured, including a mutation made through one import
// being visible through the other. The reasons are narrower and both practical: an `import()`
// from a page script proved flaky against a single-threaded `http.server`, and a check that
// re-spells the specifier can resolve a *different* URL, which is the one way to genuinely
// end up somewhere else. Reading what the page published removes the spelling from the check.
window.webxpRoutes = routes;

// `/pages` — what is routable, as a directory you can `ls`.
//
// A view of the route table, not the table itself: the filesystem shows what routes and the
// router decides what routing means. That split is why `cat /pages/terminal` can print
// something true. A tree that *was* the router would have to answer what `chmod 000
// /pages/day-02-lines` does — break the site, or be decoration — and this project spent
// today removing four permissions that were decoration.
//
// Read-only, `444`, grafted by the same mechanism as the repository at `/usr/src/webxp`.
// Registered, not applied: the filesystem is mounted the first time a window needs one, and
// a reader who opens nothing should not pay for a directory they never look at.
const { onMount } = await import('../shared/fs/fs.mjs');
onMount((fs) => {
  window.webxpPages = fs.mountText(
    '/pages',
    [...desktop.programs.values()].map((program) => [program.id, routes.describe(program)]),
  );
});

window.webxpRoute = await routes.restore(desktop);

// Clippy, in place of the Read me opening itself. Offering help and taking no for an answer
// is the same thought as an autostarting window, minus the part where it takes the desktop
// before you have asked for anything.
mountClippy({
  root: desktop.surface,
  onAccept: () => desktop.launch('readme'),
});
