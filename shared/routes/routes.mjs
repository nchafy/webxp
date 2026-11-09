/* Links. One URL names one app and the state that app knows how to restore.
 *
 * Nothing in this desktop was linkable before: you could open day 02, drill into a street,
 * and there was no way to send anyone what you were looking at. `grep -rn 'location.hash'`
 * over `src/`, `apps/`, `demo/` and `shared/` returned nothing at all.
 *
 *     #/terminal                    the shell, at home
 *     #/terminal/usr/src/webxp      the shell, already there
 *     #/explorer/home/user/notes    a folder window on that folder
 *     #/notepad/etc/motd?ro=1       a file, and whatever else that app puts in its link
 *
 * ## The router knows no app
 *
 * It splits a hash into three things — an id, a path, and query parameters — and hands the
 * last two to whoever owns the id. `street/1234` means nothing here; it means something to
 * day 02. That is what keeps a per-app parser out of this module, and it is why the state is
 * a path rather than a scheme this file would have to understand.
 *
 * ## The id is the program id
 *
 * `#/day-02-lines`, not `#/day-02`. A prettier id would need a registry mapping one to the
 * other, which is a second name for every app and a thing to drift; when an app can declare
 * its own route name in its spec, this becomes that declaration instead of a table here.
 *
 * ## What a link does not carry
 *
 * One app and its state, not the desktop. No window geometry, no z-order, no second window.
 * Opening a link gives you a fresh desktop with that one window on it — which is the thing
 * a reader wants from a link they were sent, and it keeps the URL short enough to paste.
 *
 * Imports nothing.
 */

//: Everything after this is ours. `#/` rather than `#` so a bare `#` — which browsers also
//: produce for "top of page" — is not read as a route.
const PREFIX = '#/';

/**
 * Read a hash into a route.
 *
 * @param {string} [hash] Defaults to the document's own.
 * @returns {{id: string, path: string, params: URLSearchParams}|null} `null` when there is
 *          no route in it, which is the ordinary case for a desktop opened without a link.
 */
export function parse(hash = window.location.hash) {
  if (typeof hash !== 'string' || !hash.startsWith(PREFIX)) return null;
  const body = hash.slice(PREFIX.length);
  if (!body) return null;

  const [route, query = ''] = body.split('?');
  const parts = route.split('/').filter(Boolean);
  if (!parts.length) return null;

  const [id, ...rest] = parts;
  return {
    id: decodeURIComponent(id),
    // Each segment is decoded on its own, so a path component containing an encoded slash
    // survives as one component rather than splitting into two.
    path: rest.map(decodeURIComponent).join('/'),
    params: new URLSearchParams(query),
  };
}

/**
 * Build a hash from a route.
 *
 * @param {string} id
 * @param {{path?: string, params?: Record<string, string>}} [state]
 */
export function format(id, state = {}) {
  const path = (state.path || '')
    .split('/')
    .filter(Boolean)
    .map(encodeURIComponent)
    .join('/');
  const query = new URLSearchParams(state.params || {}).toString();
  return `${PREFIX}${encodeURIComponent(id)}${path ? `/${path}` : ''}${query ? `?${query}` : ''}`;
}

/**
 * Put a route in the address bar without adding a history entry.
 *
 * `replaceState`, not `location.hash =`: a window whose state changes as you use it — every
 * `cd` in a terminal, every folder in Explorer — would otherwise fill the back button with
 * steps nobody took deliberately, and the back button would stop meaning "the page I came
 * from". The address bar still updates, so the link is always there to copy.
 *
 * @returns {string} The hash that was published.
 */
export function publish(id, state = {}) {
  const hash = format(id, state);
  if (window.location.hash !== hash) {
    window.history.replaceState(null, '', hash);
  }
  return hash;
}

/**
 * A link's path as an absolute filesystem path.
 *
 * `#/explorer/home/user` carries `home/user`, because a leading slash in a hash reads as an
 * empty first segment and a URL with `//` in it invites a reader to delete one. The three
 * apps whose state *is* a filesystem path all need the same transform, so it is defined once
 * here rather than three times there.
 *
 * Not applied by `restore`: an app's state is not necessarily a path, and a router that
 * assumed it was would be a router that knows what its apps hold.
 */
export function asAbsolute(path) {
  if (!path) return '';
  return path.startsWith('/') ? path : `/${path}`;
}

/** Remove the route, for a desktop that no longer has the window it described. */
export function clear() {
  if (window.location.hash) window.history.replaceState(null, '', window.location.pathname);
}

/**
 * Open the app a link names, if the link names one.
 *
 * The desktop is handed `args` the same way the terminal's `open` hands them over — there is
 * no routing-specific path into an app, because a link and a command opening the same window
 * must not be able to disagree about how it starts.
 *
 * `path` and the query are passed through as given. An app that declares nothing still gets
 * opened bare, which is the honest reading of `#/<id>` with nothing after it.
 *
 * @param {object} desktop
 * @param {{id: string, path: string, params: URLSearchParams}|null} [route]
 * @returns {Promise<{id: string, opened: boolean, reason?: string}|null>}
 */
export async function restore(desktop, route = parse()) {
  if (!route) return null;
  if (!desktop.programs.has(route.id)) {
    // Said out loud rather than silently ignored: a link to an app that has been renamed or
    // removed is a dead link, and a desktop that opens nothing looks like a broken page.
    console.warn(`webxp: no program named ${route.id} — the link #/${route.id} opens nothing`);
    return { id: route.id, opened: false, reason: 'no such program' };
  }
  const args = { path: route.path || undefined };
  for (const [key, value] of route.params) args[key] = value;
  await desktop.launch(route.id, args);
  return { id: route.id, opened: true };
}
