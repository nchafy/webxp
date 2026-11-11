/* The app loader: turns a folder under `apps/` into a program on the desktop.
 *
 * The contract is in `docs/apps.md`. In one sentence: `apps/<id>/app.mjs` default-exports a
 * spec whose `id` matches its folder, and this module validates it, resolves its paths
 * against its own folder, and registers it.
 *
 * Nothing here knows what an app does. It knows where an app lives, what shape its spec has
 * to be, and how to report an app that got that wrong — which is the whole job, because the
 * failure this is built to avoid is an app that is silently absent.
 *
 * It also carries the one exception to that: `FILE_PROGRAMS`, which says *which* program opens
 * a given kind of file. It lives here because this is the module that knows every program
 * there is, so it is the only place an association naming a program that does not exist can be
 * caught — see `danglingAssociations`.
 */

import { PROGRAM_KEYS } from './desktop.mjs';
import { WINDOW_SPEC_KEYS } from './window.mjs';
import { ICON_NAMES, FILETYPE_NAMES, iconFor } from './icons.mjs';

//: Where the folders live, relative to the document. Overridable so a host page can keep its
//: apps somewhere else, and so the checks can point at a fixture directory.
export const DEFAULT_BASE = '/apps';

//: The keys this module consumes itself rather than forwarding: the folder's identity, the
//: two ways of filling a window, and the three the loader resolves into something else.
const LOADER_OWNED = new Set(['id', 'icon', 'filetype', 'mount', 'styles', 'frame']);

//: Every key an app spec may carry — the shell's, the window's, and the loader's own.
//: Anything else is a mistake worth reporting: a misspelled `singelton` is
//: indistinguishable from a missing feature at runtime.
const KNOWN_KEYS = new Set([...PROGRAM_KEYS, ...WINDOW_SPEC_KEYS, ...LOADER_OWNED]);

//: Spec keys passed straight through to `Desktop.register`: everything known that the
//: loader does not consume. Derived rather than listed, so an option added to the shell or
//: to the window is forwarded without an edit here — a key that validates and is then
//: dropped on the way through is the failure this list exists to prevent.
const PASS_THROUGH = [...KNOWN_KEYS].filter((key) => !LOADER_OWNED.has(key));

//: `ready` is passed through to the desktop but is not required, and an app may have it
//: without a `mount` only in the `frame` case — which has no use for it, since a hosted
//: document initialises itself.

/* ------------------------------ file associations ------------------------------ */

/**
 * Which program opens which kind of file.
 *
 * Keyed by extension, lower-cased, leading dot included. The value is program ids,
 * **most-preferred first**: the first is what a double-click uses, and the whole list is what
 * an "Open With" menu offers. Every list has one entry today and the shape is still a list,
 * because the list is the contract — the day an image viewer lands, `.svg` gains a preferred
 * opener ahead of Notepad by editing data rather than logic.
 *
 * `''` is the association for a name with no extension. The mounted source tree has exactly
 * two — `LICENSE` and `.gitignore` — and both are text, so this is a real key and not a
 * placeholder; it is also what keeps `noProgramFor` from ever having to describe the empty
 * extension.
 *
 * **Only ids in `apps/manifest.mjs` may appear here, and only apps that read `args.path` as a
 * file.** Both halves bite. `desktop.launch` returns `null` for a program it does not have, so
 * a dangling id is a double-click that does nothing at all; and an app that exists but cannot
 * be handed a path is just as dangling. That is why `.html` goes to Notepad and not to
 * `webview`: `webview` is a singleton `frame` app, so it hosts its own page and a second launch
 * discards the arguments it was given.
 *
 * The extensions listed are the ones a reader can actually reach — everything in the seeded
 * filesystem (`shared/fs/seed.mjs`) and in the repository mounted at `/usr/src/webxp`
 * (`shared/fs/mount.json`) — plus `.js`, which a reader writes by hand. Anything else resolves
 * to nothing and says so, which is the point: there is no image viewer, so `.png` is a gap the
 * desktop admits to rather than a file it hands to a text editor.
 *
 * Frozen twice over: shallow `freeze` leaves the arrays writable, and an "Open With" that
 * sorted its candidates in place would reorder the association for every later caller.
 */
export const FILE_PROGRAMS = Object.freeze(Object.fromEntries(
  Object.entries({
    '': ['notepad'],
    '.conf': ['notepad'],
    '.css': ['notepad'],
    '.csv': ['notepad'],
    '.html': ['notepad'],
    '.js': ['notepad'],
    '.json': ['notepad'],
    '.log': ['notepad'],
    '.md': ['notepad'],
    '.mjs': ['notepad'],
    '.py': ['notepad'],
    '.svg': ['notepad'],
    '.txt': ['notepad'],
  }).map(([extension, ids]) => [extension, Object.freeze(ids)]),
));

//: One shared empty result, so `programsFor` does not allocate per miss and cannot hand back
//: an array a caller could push into.
const NO_PROGRAMS = Object.freeze([]);

/**
 * The extension of a path: lower-cased, leading dot included, `''` when there is none.
 *
 * The one definition, because a type filter in a file picker has to split a name the same way
 * the association lookup does or it will offer a file that cannot then be opened.
 *
 * Only the last extension counts, so `activities.gpx.gz` is a `.gz` — which is correct here,
 * since what can read it is a decompressor and not a GPX reader. A leading dot is a name and
 * not a separator, so `.bashrc` has no extension; that is the POSIX reading, and this
 * filesystem is a POSIX one down to its mode bits.
 *
 * @param {string} path
 * @returns {string}
 */
export function extensionOf(path) {
  const name = String(path).slice(String(path).lastIndexOf('/') + 1);
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot).toLowerCase() : '';
}

/**
 * The programs registered to open `path`, most-preferred first.
 *
 * Three callers want exactly this list: a double-click takes `[0]`, a picker's type filter
 * asks whether its program is in it, and "Open With" offers all of it. Empty means nothing is
 * registered — ask `noProgramFor` for the sentence to show, because a double-click that
 * silently does nothing is indistinguishable from a desktop that is broken.
 *
 * A directory is not a kind of file and the caller settles that before asking: Explorer's
 * `open` navigates into one, and a path ending in `/` answers here as if it had no extension.
 *
 * @param {string} path
 * @returns {readonly string[]} Frozen. Copy it before sorting.
 */
export function programsFor(path) {
  return FILE_PROGRAMS[extensionOf(path)] || NO_PROGRAMS;
}

/**
 * Why `path` cannot be opened — the line a caller shows when `programsFor` came back empty.
 *
 * Path first, matching the filesystem's own errors, so a status bar reads the same whichever
 * way the open failed.
 *
 * The extension is non-empty wherever this is reachable: `''` is a key of `FILE_PROGRAMS`, so
 * an extensionless name never resolves to nothing.
 *
 * @param {string} path
 * @returns {string}
 */
export function noProgramFor(path) {
  return `${path}: no program is registered for ${extensionOf(path)} files`;
}

/**
 * Associations that name a program which is not registered.
 *
 * @param {Iterable<string>} ids  The registered program ids.
 * @returns {string[]} One `'.png -> paint'` per dangling pair. Empty means the table is live.
 */
export function danglingAssociations(ids) {
  const registered = new Set(ids);
  const dangling = [];
  for (const [extension, programs] of Object.entries(FILE_PROGRAMS)) {
    for (const id of programs) {
      if (!registered.has(id)) dangling.push(`${extension || '(no extension)'} -> ${id}`);
    }
  }
  return dangling;
}

/* ------------------------------ loading ------------------------------ */

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
      // A rejected promise left in the map poisons the app for the rest of the session: one
      // dropped request — a blip while another app saturates the connection pool — and every
      // later launch replays that same rejection and shows the same error window, with the
      // stylesheet answering 200 the whole time. Deleting the entry is what makes reopening
      // the app a retry.
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
 * Whether a spec asks for a hosted document rather than a mount.
 *
 * The one definition. `validate` and `loadApp` both ask, and two answers is how a spec that
 * validates loses its `mount`: anything truthy under `frame` reaches `frameMount`, which
 * reads `frame.src` and nothing else.
 */
function hostsFrame(spec) {
  return Boolean(spec.frame) && typeof spec.frame.src === 'string';
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
  if (!hasMount && !hostsFrame(spec)) {
    problems.push('needs either a mount() function or a frame: { src }');
  }
  if (hasMount && hostsFrame(spec)) {
    problems.push('has both mount() and frame — a window can only be filled once');
  }
  // A declared `frame` that is not usable is an error, not a fall-through to `mount`. The
  // two differ: `frame: { url: 'page.html' }` is a usable spec by every other test, and
  // without this the loader hosts `apps/<id>/undefined` and never calls the mount.
  if (spec.frame !== undefined && !hostsFrame(spec)) {
    problems.push('frame must be { src } with src a path string');
  }

  if (spec.icon !== undefined && !ICON_NAMES.includes(spec.icon)) {
    problems.push(`icon ${JSON.stringify(spec.icon)} is not one of: ${ICON_NAMES.join(', ')}`);
  }
  // An unknown filetype is an error rather than a fallback, for the same reason an unknown icon
  // is: a typo would otherwise be a silently generic window, and the whole point of a filetype
  // is that every app of one filetype looks the same.
  if (spec.filetype !== undefined && !FILETYPE_NAMES.includes(spec.filetype)) {
    problems.push(`filetype ${JSON.stringify(spec.filetype)} is not one of: ${FILETYPE_NAMES.join(', ')}`);
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
 * The focus shim is the reason `frame` is a first-class option rather than three lines in an
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
 * Wrap an app's mount with the work the loader owes it: its stylesheets, resolved against
 * its own folder and linked before the content, so the content is never laid out against
 * missing rules.
 *
 * The body class those rules are written under is set on the program, in `loadApp`.
 */
function wrapMount(spec, appBase, inner) {
  const styles = (spec.styles || []).map((path) => new URL(path, appBase).href);
  return async (ctx) => {
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

  // `app-<id>` is how two apps' stylesheets stay out of each other's way: app CSS is
  // written under it, which `docs/apps.md` requires and the scaffold's `app.css` shows.
  const program = { id, bodyClass: `app-${id}`, icon: iconFor(spec) };
  // Carried onto the program as well as resolved into an icon, because the filetype is what
  // groups programs: `app filetypes` in `scripts/check.py` reads it back off the registry to
  // assert that every app of one filetype resolved to one icon.
  if (spec.filetype) program.filetype = spec.filetype;
  for (const key of PASS_THROUGH) {
    if (spec[key] !== undefined) program[key] = spec[key];
  }
  const inner = hostsFrame(spec) ? frameMount(spec, appBase) : spec.mount;
  program.mount = wrapMount(spec, appBase, inner);
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

  // The only moment both halves of an association are known: the table is static, the
  // registry is not. An id in `FILE_PROGRAMS` that nothing registered is a file whose
  // double-click reaches `launch`, gets `null`, and does nothing — the silently-absent
  // program this module exists to make noisy.
  const dangling = danglingAssociations(desktop.programs.keys());
  if (dangling.length) {
    console.error(`webxp: file associations name programs that do not exist: ${dangling.join(', ')}`);
  }

  return { loaded, broken };
}
