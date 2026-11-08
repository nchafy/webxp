/* The app loader: turns a folder under `apps/` into a program on the desktop.
 *
 * The contract is in `docs/apps.md`. In one sentence: `apps/<id>/app.mjs` default-exports a
 * spec whose `id` matches its folder, and this module validates it, resolves its paths
 * against its own folder, and registers it.
 *
 * Nothing here knows what an app does. It knows where an app lives, what shape its spec has
 * to be, and how to report an app that got that wrong — which is the whole job, because the
 * failure this is built to avoid is an app that is silently absent.
 */

import { ICON_NAMES } from './icons.mjs';

//: Where the folders live, relative to the document. Overridable so a host page can keep its
//: apps somewhere else, and so the checks can point at a fixture directory.
export const DEFAULT_BASE = '/apps';

//: Spec keys passed straight through to `Desktop.register`. Listed rather than spread, so a
//: typo in an app's spec is caught by `validate` instead of being silently ignored.
const PASS_THROUGH = [
  'name', 'icon', 'ready', 'width', 'height', 'x', 'y',
  'resizable', 'minimizable', 'maximizable', 'maximized',
  'status', 'singleton', 'onDesktop', 'autostart', 'minLoadMs',
];

//: Every key an app spec may carry. Anything else is a mistake worth reporting: a
//: misspelled `singelton` is indistinguishable from a missing feature at runtime.
const KNOWN_KEYS = new Set([...PASS_THROUGH, 'id', 'mount', 'styles', 'frame']);

//: `ready` is passed through to the desktop but is not required, and an app may have it
//: without a `mount` only in the `frame` case — which has no use for it, since a hosted
//: document initialises itself.

/** Stylesheets already in the document, so N windows of one app link its CSS once. */
const linked = new Map();

/**
 * Add a stylesheet and wait for it to apply.
 *
 * Awaited rather than fired and forgotten: the window would otherwise show its content
 * unstyled for a frame or two. A stylesheet that 404s rejects, which turns a declared but
 * missing `app.css` into a window that says so instead of a window that quietly looks wrong.
 */
function addStylesheet(href) {
  if (linked.has(href)) return linked.get(href);
  const ready = new Promise((resolve, reject) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.addEventListener('load', () => resolve());
    link.addEventListener('error', () => {
      // The failure is not cached, and the dead link is removed.
      //
      // A rejected promise left in the map poisons the app for the rest of the session: a
      // single dropped request — one blip while another app saturates the connection pool —
      // and every later launch replays that same rejection and shows the same error window,
      // with the stylesheet sitting there answering 200 the whole time. Observed exactly
      // once, as five identical mount failures from one real failure. Reopening the app now
      // retries.
      linked.delete(href);
      link.remove();
      reject(new Error(`stylesheet failed to load: ${href}`));
    });
    document.head.append(link);
  });
  linked.set(href, ready);
  return ready;
}

/**
 * Check a spec before it reaches the desktop.
 *
 * @returns {string[]} Reasons it is unusable. Empty means usable.
 */
export function validate(spec, id) {
  const problems = [];
  if (!spec || typeof spec !== 'object') return [`apps/${id}/app.mjs has no default export`];

  if (spec.id !== id) {
    // The folder is the identity. A spec whose id disagrees is almost always a copied app
    // whose id was not changed, and the symptom without this check is the original app
    // being replaced in the registry by its copy.
    problems.push(`id is ${JSON.stringify(spec.id)} but the folder is ${JSON.stringify(id)}`);
  }
  if (typeof spec.name !== 'string' || !spec.name.trim()) {
    problems.push('name must be a non-empty string');
  }

  const hasMount = typeof spec.mount === 'function';
  const hasFrame = spec.frame && typeof spec.frame.src === 'string';
  if (!hasMount && !hasFrame) {
    problems.push('needs either a mount() function or a frame: { src }');
  }
  if (hasMount && hasFrame) {
    problems.push('has both mount() and frame — a window can only be filled once');
  }

  if (spec.icon !== undefined && !ICON_NAMES.includes(spec.icon)) {
    problems.push(`icon ${JSON.stringify(spec.icon)} is not one of: ${ICON_NAMES.join(', ')}`);
  }
  if (spec.styles !== undefined && !Array.isArray(spec.styles)) {
    problems.push('styles must be an array of paths relative to the app folder');
  }
  if (spec.ready !== undefined && typeof spec.ready !== 'function') {
    problems.push('ready must be a function');
  }
  for (const key of Object.keys(spec)) {
    if (!KNOWN_KEYS.has(key)) problems.push(`unknown key ${JSON.stringify(key)}`);
  }
  return problems;
}

/**
 * Build the mount for an iframe app.
 *
 * The focus shim is the reason this is a first-class kind rather than three lines in an
 * app's own mount. A pointer event inside an iframe does not cross the document boundary, so
 * the window's own focus handler never sees a click on hosted content and the window cannot
 * be raised by clicking it. The shim is a transparent layer that exists only while the
 * window is inactive — `window.css` does that with `:not(.is-active)`, so there is no state
 * to keep in step. Clicking it raises the window, the shim stops existing, and the next
 * click reaches the page. The first click only focusing is not a compromise: it is what every
 * window manager does.
 */
function frameMount(spec, appBase) {
  return ({ win }) => {
    const wrap = document.createElement('div');
    wrap.className = 'app-frame-wrap';

    const frame = document.createElement('iframe');
    frame.className = 'app-frame';
    frame.src = new URL(spec.frame.src, appBase).href;
    // A frame is a document, and a document in the tab order needs a name.
    frame.title = spec.name;
    if (spec.frame.allow) frame.setAttribute('allow', spec.frame.allow);
    // Declared explicitly rather than left to the default, so a host reading this file can
    // see what hosted content is permitted to do.
    if (spec.frame.sandbox !== undefined) frame.setAttribute('sandbox', spec.frame.sandbox);

    const shim = document.createElement('div');
    shim.className = 'app-frame-shim';
    shim.setAttribute('aria-hidden', 'true');

    wrap.append(frame, shim);
    win.setStatus(frame.src);
    return wrap;
  };
}

/**
 * Wrap an app's mount with the work the loader owes it: its body class and its stylesheets.
 *
 * The class is how two apps' stylesheets stay out of each other's way — app CSS is written
 * under `.app-<id>`, which `docs/apps.md` requires and the scaffold's `app.css` demonstrates.
 */
function wrapMount(spec, id, appBase, inner) {
  const styles = (spec.styles || []).map((path) => new URL(path, appBase).href);
  return async (ctx) => {
    // Before the content, so the content is never laid out against missing rules.
    await Promise.all(styles.map(addStylesheet));
    return inner(ctx);
  };
}

/**
 * Load one app folder.
 *
 * @returns {{id: string, program: object, problems: string[]}}
 */
export async function loadApp(id, base = DEFAULT_BASE) {
  const appBase = new URL(`${base}/${id}/`, document.baseURI).href;
  let spec;
  try {
    const module = await import(`${appBase}app.mjs`);
    spec = module.default;
  } catch (error) {
    return { id, program: brokenProgram(id, [error.message]), problems: [error.message] };
  }

  const problems = validate(spec, id);
  if (problems.length) {
    return { id, program: brokenProgram(id, problems, spec), problems };
  }

  const program = { id, bodyClass: `app-${id}` };
  for (const key of PASS_THROUGH) {
    if (spec[key] !== undefined) program[key] = spec[key];
  }
  const inner = spec.frame ? frameMount(spec, appBase) : spec.mount;
  program.mount = wrapMount(spec, id, appBase, inner);
  return { id, program, problems: [] };
}

/**
 * A program standing in for an app that could not be loaded.
 *
 * Registered rather than skipped, on purpose. An app missing from the Start menu is a
 * question — did I forget to add it, is the manifest stale, did I misname the folder — and
 * the answer is in a console line nobody scrolled back to. An entry that opens a window
 * listing exactly what is wrong with `apps/<id>/app.mjs` answers it where it is asked.
 */
function brokenProgram(id, problems, spec) {
  return {
    id,
    name: (spec && typeof spec.name === 'string' && spec.name) || id,
    icon: 'info',
    singleton: true,
    width: 460,
    height: 260,
    status: true,
    mount() {
      throw new Error(`apps/${id}/app.mjs is not a usable app: ${problems.join('; ')}`);
    },
  };
}

/**
 * Load every app named in a manifest and register it on a desktop.
 *
 * Apps are imported in parallel and registered in manifest order, so the Start menu does not
 * reorder itself by whichever module happened to resolve first.
 *
 * @param {import('./desktop.mjs').Desktop} desktop
 * @param {string[]} ids    Usually `APPS` from the generated `apps/manifest.mjs`.
 * @param {{base?: string}} [options]
 * @returns {Promise<{loaded: string[], broken: Record<string, string[]>}>}
 */
export async function loadApps(desktop, ids, options = {}) {
  const base = options.base || DEFAULT_BASE;
  const results = await Promise.all(ids.map((id) => loadApp(id, base)));

  const loaded = [];
  const broken = {};
  for (const result of results) {
    desktop.register(result.program);
    if (result.problems.length) {
      broken[result.id] = result.problems;
      // Reported here as well as in the window, because a broken app at load time is
      // something the person editing the folder wants to see without opening anything.
      console.error(`webxp: apps/${result.id} did not load: ${result.problems.join('; ')}`);
    } else {
      loaded.push(result.id);
    }
  }
  return { loaded, broken };
}
