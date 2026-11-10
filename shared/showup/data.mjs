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

/* Where an internal href goes now that the pages are programs.
 *
 * `preview.json` is built for a site that had a page per district and per board, so its
 * table cells carry hrefs like `/district/35/` and `/board/302/`. Those routes are gone.
 * They were flattened to plain text for a while, which threw away the one thing the old
 * pages were best at: a district listed its community boards, and you could click into one.
 *
 * So an internal href resolves to a surface instead — a program, plus the row to select in
 * it. `table` from webxp's widgets renders an href as an anchor and has no hook for a
 * launch, but `fillCell` accepts a `Node`, so the cell becomes a button and the button is
 * the hook.
 */
export const SURFACES = {
  district: { program: 'showup-councils', attribute: 'district' },
  board: { program: 'showup-boards', attribute: 'board' },
};

//: `/district/35/` and `/board/302/`, with or without the trailing slash.
const INTERNAL = /^\/(district|board)\/([^/]+?)\/?$/;

export function internalTarget(href) {
  const match = typeof href === 'string' ? INTERNAL.exec(href) : null;
  return match ? { ...SURFACES[match[1]], id: match[2] } : null;
}

/* A cell's href when it is genuinely external — the City's own pages, a PDF agenda.
 *
 * Internal ones are `internalTarget`'s job. An anchor to a route that 404s is worse than
 * text, which is why this ever existed.
 */
export function externalHrefOnly(href) {
  if (typeof href !== 'string' || !href) return null;
  return /^https?:/.test(href) ? href : null;
}

/* Wait for a program's list to hold the row we are about to select.
 *
 * `launch` resolves after the program's own `ready` and `load()` caches its promise, so the
 * rows are usually there already. Usually is not always: the first cross-link into a
 * program is also that program's first fetch.
 */
async function pickRow(target, attempts = 40, pause = 50) {
  const selector =
    `.app-${target.program} .pick[data-${target.attribute}="${CSS.escape(target.id)}"]`;
  for (let i = 0; i < attempts; i += 1) {
    const row = document.querySelector(selector);
    if (row) return row;
    await new Promise((resolve) => { setTimeout(resolve, pause); });
  }
  return null;
}

/**
 * Open the program a cross-link points at, and select the row it names.
 *
 * Both failure paths announce rather than doing nothing, because a click that appears to do
 * nothing is the worst outcome and both causes are real: a program missing from
 * `apps/manifest.mjs`, and a row `preview.json` names that `lookup.json` does not carry.
 *
 * @param {object} desktop
 * @param {{program: string, attribute: string, id: string}} target
 * @returns {Promise<boolean>} whether the row was reached
 */
export async function openSurface(desktop, target) {
  const opened = await desktop.launch(target.program);
  if (!opened) {
    desktop.announce(`${target.program} could not be opened. Its program is not registered.`);
    return false;
  }
  const row = await pickRow(target);
  if (!row) {
    desktop.announce(`That ${target.attribute} is not in the list.`);
    return false;
  }
  row.click();
  row.scrollIntoView({ block: 'nearest' });
  row.focus();
  return true;
}

/* The cell itself: a button that reads as a link.
 *
 * A button and not an anchor because there is no URL to put in an href — the destination is
 * a window, not a document. The `aria-label` says where it goes, because the text alone does
 * not tell a screen-reader user that activating it moves them into another program.
 */
function crossLink(text, desktop, target) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'cross-link';
  button.textContent = text ?? '';
  const where = target.program.replace('showup-', '');
  button.setAttribute('aria-label', `${text} — opens in ${where}`);
  button.addEventListener('click', () => { void openSurface(desktop, target); });
  return button;
}

/**
 * One of `preview.json`'s table specs, ready for webxp's `table`.
 *
 * @param {object} spec        a table spec from `preview.json`
 * @param {object} [desktop]   omit for the text-only behaviour; pass it for cross-links
 */
export function usableTable(spec, desktop) {
  const rows = (spec.rows || []).map((row) =>
    row.map((cell) => {
      if (!cell || typeof cell !== 'object') return cell;

      const external = externalHrefOnly(cell.href);
      if (external) return { text: cell.text, href: external };

      const target = desktop ? internalTarget(cell.href) : null;
      if (target) return crossLink(cell.text, desktop, target);

      return cell.text ?? '';
    }),
  );
  return { caption: spec.caption || undefined, columns: spec.columns || [], rows };
}

/**
 * Several cross-links on one line, for a facts card that has no room for a table.
 *
 * The map's card is `[label, value]` rows, not a table, so the boards covering a district
 * cannot be a column — they become one cell holding a button each. `fillCell` takes a Node,
 * which is what makes that possible.
 *
 * Returns null when the named table is absent or carries no internal link, so the caller's
 * row drops out rather than rendering a label with nothing beside it: a blank next to
 * "Community boards" reads as "this district has none".
 *
 * @param {object} facts    one entry of `preview.json`'s `districts` or `boards`
 * @param {string} legend   the table to read, e.g. 'Community boards'
 * @param {object} desktop
 * @returns {HTMLElement|null}
 */
export function crossLinkRow(facts, legend, desktop) {
  if (!desktop) return null;
  const spec = (facts.tables || []).find((entry) => entry.legend === legend);
  if (!spec) return null;

  const wrap = document.createElement('span');
  wrap.className = 'cross-link-row';
  for (const row of spec.rows || []) {
    for (const cell of row) {
      const target = cell && typeof cell === 'object' ? internalTarget(cell.href) : null;
      if (target) wrap.append(crossLink(cell.text, desktop, target));
    }
  }
  return wrap.childElementCount ? wrap : null;
}
