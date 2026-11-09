/* What the Show Up programs all need from the build's own JSON.
 *
 * Not an app: `scripts/apps.py` only registers a folder that carries an `app.mjs`, so this
 * directory is a library the programs import and never a program itself.
 *
 * It exists because the councils and boards programs want the same three artifacts and the
 * same two transformations. Two copies of that would be two things to keep in step, and the
 * second one would be the one that drifted.
 */

//: Shared by resolved URL. ES modules dedupe that way, so three programs importing this get
//: one module and one copy of whatever it has already fetched.
const cache = new Map();

function fetchJson(url) {
  return fetch(url).then((response) => {
    if (!response.ok) throw new Error(`${url} returned ${response.status}`);
    return response.json();
  });
}

/**
 * Fetch a path once per page, however many programs ask for it.
 *
 * The promise is cached rather than the value, so two windows opening at the same moment
 * share one request instead of racing to make two.
 */
export function load(url) {
  if (!cache.has(url)) {
    cache.set(
      url,
      fetchJson(url).catch((error) => {
        // Dropped from the cache so a later window can retry: a transient failure should not
        // poison the rest of the session.
        cache.delete(url);
        throw error;
      }),
    );
  }
  return cache.get(url);
}

export const LOOKUP = '/data/showup/lookup.json';
export const PREVIEW = '/data/showup/preview.json';
export const BOARD_MEETINGS = '/data/showup/board-meetings.json';

/** Districts as `{n, hoods}` rows, in number order. */
export async function districts() {
  const lookup = await load(LOOKUP);
  return [...(lookup.districts || [])].sort((a, b) => a.n - b.n);
}

/** Boards as `{code, label, hoods}` rows, in code order. */
export async function boards() {
  const lookup = await load(LOOKUP);
  return [...(lookup.boards || [])].sort((a, b) => String(a.code).localeCompare(String(b.code)));
}

/* A cell's href, or null when it points inside this site.
 *
 * `preview.json` is built for a site that had a page per district and per board, so its
 * table cells carry hrefs like `/district/1/`. Those routes are gone: the programs are the
 * pages now. An anchor to a 404 is worse than text, so an internal href becomes text and an
 * external one — the City's own pages, a PDF agenda — is kept.
 *
 * Opening the sibling program instead would be better than text and is not what this does;
 * `table` from webxp's widgets renders an href as an anchor and has no hook for a launch.
 */
export function externalHrefOnly(href) {
  if (typeof href !== 'string' || !href) return null;
  return /^https?:/.test(href) ? href : null;
}

/**
 * One of `preview.json`'s table specs, with its internal links flattened to text.
 *
 * Returns a spec `table` from webxp's widgets accepts: `{caption, columns, rows}`.
 */
export function usableTable(spec) {
  const rows = (spec.rows || []).map((row) =>
    row.map((cell) => {
      if (cell && typeof cell === 'object') {
        const href = externalHrefOnly(cell.href);
        return href ? { text: cell.text, href } : (cell.text ?? '');
      }
      return cell;
    }),
  );
  return { caption: spec.caption || undefined, columns: spec.columns || [], rows };
}
