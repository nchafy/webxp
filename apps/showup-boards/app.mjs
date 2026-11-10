/* Community boards — the 59 boards, how to reach one, and when it meets.
 *
 * Ported from the `/board/` index and the 59 board pages, the same way the councils program
 * was: the list and the facts were two routes and are now two panes.
 *
 * Four artifacts, because a board's answer comes from four places. `lookup.json` has the
 * names and what each board covers. `preview.json` has the office, the phone, the cadence in
 * the board's own words, the covering council districts and the first few budget requests.
 * `detail.json` has every budget request the board filed, with the board's own explanation
 * and the agency's reply. `board-meetings.json` has each board's own calendar, with when it
 * was fetched and how stale it is — which matters more here than anywhere else on the site,
 * because the City runs no board meeting calendar and these are the boards' own feeds.
 *
 * What the old pages had and this does not yet: the board's public members, and the prose on
 * what a board can and cannot do. Neither is in `preview.json` or `detail.json` — a board's
 * entry in the second carries nothing but its budget — so naming them is better than letting
 * the pane look complete.
 */

import { group } from '../../src/widgets.mjs';
import {
  boards, load, usableTable, PREVIEW, BOARD_MEETINGS, DETAIL,
} from '../../shared/showup/data.mjs';

//: How many of a board's upcoming entries to list. Its own feed carries office closures and
//: screenings alongside meetings, so a long list is mostly not meetings.
const UPCOMING = 6;

//: The one name for the requests, used twice on purpose: as this program's own legend, and
//: as the `preview.json` table it drops. That table caps the requests at a few rows of two
//: columns, and a handful of requests above all of them is one fact with two renderers that
//: a reader then has to choose between.
const BUDGET_LEGEND = 'Budget requests';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function row(entry, onPick) {
  const item = el('li', 'pick-item');
  const button = el('button', 'pick');
  button.type = 'button';
  button.dataset.board = String(entry.code);
  button.append(
    el('span', 'pick-name', entry.label || `Community Board ${entry.code}`),
    el('span', 'pick-hoods', entry.hoods || ''),
  );
  button.addEventListener('click', () => onPick(entry));
  item.append(button);
  return item;
}

/* One budget request as its own record.
 *
 * A record per request rather than one wide table. The explanation and the reply are
 * paragraphs — the longest in `detail.json` run to 2,849 and 5,206 characters — and the
 * detail pane measures 508 px at this window's default size, so six columns of those would
 * be columns an inch wide. The group box is the shape the rest of this window already uses
 * for "a thing with fields", so a reader meets nothing new.
 *
 * The reply is quoted as filed and never sorted into funded or not. The phrasing changes
 * between editions, and it records the agency's position rather than an outcome. */
function requestGroup(request, index) {
  const lines = [];
  if (request.priority) lines.push(['Priority', request.priority]);
  if (request.category) lines.push(['Category', request.category]);
  if (request.agency) lines.push(['Agency', request.agency]);
  if (request.tracking_code) lines.push(['Tracking code', request.tracking_code]);
  if (request.explanation) lines.push(['The board explained', request.explanation]);
  if (request.response) lines.push(['The agency replied', request.response]);

  // One of the 3,809 carries no request text, so the legend falls back to the one identifier
  // that is always there rather than to an empty legend.
  const legend = request.request || request.tracking_code || `Request ${index + 1}`;
  return lines.length
    ? group(legend, { columns: ['Field', 'Value'], rows: lines })
    : group(legend, ['Nothing else is recorded for this request.']);
}

/** Every request a board filed, under a header saying for which year and how many. */
function budgetSection(budget) {
  const requests = budget.requests || [];
  const wrap = el('div', 'budget');

  const summary = [];
  summary.push(requests.length
    ? `${requests.length} request${requests.length === 1 ? '' : 's'}`
      + `${budget.fiscal_year ? ` for fiscal ${budget.fiscal_year}` : ''}, every one below.`
    : 'The City publishes no budget requests for this board.');
  if (budget.publication) summary.push(`Published ${budget.publication}.`);
  wrap.append(group(BUDGET_LEGEND, summary));

  requests.forEach((request, index) => { wrap.append(requestGroup(request, index)); });
  return wrap;
}

export default {
  id: 'showup-boards',
  name: 'Community boards',
  icon: 'folder',
  singleton: true,
  width: 760,
  height: 540,
  status: true,
  styles: [
    '../../shared/showup/list-detail.css',
    '../../shared/showup/links.css',
    'app.css',
  ],

  mount({ win }) {
    const root = el('div', 'boards-root');

    // Stated before the list, because this is the page a reader lands on having confused the
    // two things, and the distinction is the first thing they need.
    const note = el('p', 'boards-note');
    note.append(
      el('strong', null, 'A community board is not a council district.'),
      document.createTextNode(' It is the most local unit of City government, advising on '
        + 'zoning, budget priorities and local services for one community district.'),
    );

    const panes = el('div', 'boards-panes list-detail');
    const list = el('ul', 'pick-list');
    list.setAttribute('aria-label', 'Community boards');
    const detail = el('div', 'detail-pane');
    detail.append(el('p', 'detail-empty', 'Pick a board to see how to reach it.'));
    panes.append(list, detail);
    root.append(note, panes);

    win.app = { list, detail };
    win.setStatus('Loading the boards…');
    return root;
  },

  async ready({ win, desktop }) {
    const { list, detail } = win.app;

    let rows;
    let preview;
    let calendars;
    let record;
    // Named, not swallowed, and not fatal. The uncapped requests failing must not cost the
    // contact details, which are what a reader most often came for — but a board whose
    // requests are missing because a fetch failed must not read as a board that filed none.
    let recordError = null;
    try {
      [rows, preview, calendars, record] = await Promise.all([
        boards(),
        load(PREVIEW),
        // The calendars are a nicety: a failure here must not cost the contact details,
        // which are the thing a reader most often came for.
        load(BOARD_MEETINGS).catch(() => null),
        load(DETAIL).catch((error) => { recordError = error.message; return null; }),
      ]);
    } catch (error) {
      win.setStatus(`The boards could not be loaded (${error.message}).`);
      list.replaceChildren(el('li', 'pick-error', `Could not load: ${error.message}`));
      return;
    }

    const show = (entry) => {
      const code = String(entry.code);
      const facts = (preview.boards || {})[code] || {};
      const calendar = calendars && calendars.boards ? calendars.boards[code] : null;
      const full = (record && record.boards ? record.boards[code] : null) || {};
      const budget = full.budget || null;
      const nodes = [];

      const lines = [];
      if (entry.hoods) lines.push(['Covers', entry.hoods]);
      if (facts.address) lines.push(['Office', facts.address]);
      if (facts.phone) lines.push(['Phone', facts.phone]);
      if (facts.email) {
        lines.push(['Email', { text: facts.email, href: `mailto:${facts.email}` }]);
      } else if (facts.email_suppressed) {
        // The reason, not a blank. The City withholds some addresses, and saying so is an
        // answer where an empty cell reads as a board nobody can write to.
        lines.push(['Email', facts.email_suppressed]);
      }
      // The board's own words. Not expanded into dates: "Fourth Tuesday" is not a date until
      // somebody publishes one, and inventing twelve would read as a schedule.
      if (facts.cadence) lines.push(['Meets', facts.cadence]);
      if (facts.website) {
        lines.push(['Website', { text: facts.website, href: facts.website }]);
      } else if (facts.website_host) {
        // A bare host the build would not vouch for as a URL. Carried as text for that
        // reason, so this must not make it a link.
        lines.push(['Website', `${facts.website_host} (the board's own domain, unverified)`]);
      } else if (calendar && calendar.site) {
        lines.push(['Website', { text: calendar.site, href: calendar.site }]);
      }

      nodes.push(
        lines.length
          ? group(facts.label || entry.label || `Community Board ${code}`, {
              columns: ['Field', 'Value'],
              rows: lines,
            })
          : group(entry.label || `Community Board ${code}`, [
              'The City publishes no contact details for this board.',
            ]),
      );

      if (calendar && Array.isArray(calendar.upcoming) && calendar.upcoming.length) {
        nodes.push(
          group('From the board\'s own calendar', {
            caption: calendars.caveat,
            columns: ['Date', 'Entry'],
            rows: calendar.upcoming.slice(0, UPCOMING).map((item) => [
              item.date || '',
              item.title || item.summary || '',
            ]),
          }),
        );
        // Provenance beside the data rather than at the foot of the window: these are the
        // boards' own feeds, and how old one is changes what the list means.
        const stale = calendar.source_stale_days;
        nodes.push(
          group('Where that came from', [
            `Fetched ${calendar.fetched_at || 'at an unrecorded time'}.`,
            typeof stale === 'number'
              ? `The feed's own latest entry is ${calendar.source_latest_entry || 'unknown'}, `
                + `${stale} day${stale === 1 ? '' : 's'} behind the fetch.`
              : 'The feed carries no latest entry.',
          ]),
        );
      } else if (calendar) {
        nodes.push(
          group('From the board\'s own calendar', [
            'Nothing upcoming is published. The City runs no board meeting calendar, so this '
            + 'is the board\'s own feed and it lists whatever the board puts in it.',
          ]),
        );
      }

      for (const spec of facts.tables || []) {
        if (budget && spec.legend === BUDGET_LEGEND) continue;
        nodes.push(group(spec.legend, usableTable(spec, desktop)));
      }

      if (budget) {
        nodes.push(budgetSection(budget));
      } else if (recordError) {
        // Not `BUDGET_LEGEND`: the capped table above kept its own legend when the uncapped
        // set did not arrive, and two boxes titled the same thing read as one of them being
        // a duplicate rather than as a note about the other.
        nodes.push(group('The rest of the budget requests', [
          `${DETAIL} could not be loaded (${recordError}), so the few requests above are all `
          + 'this pane can show. The board filed more; this is a failed fetch, not the whole '
          + 'filing.',
        ]));
      }

      detail.replaceChildren(...nodes);
      for (const button of list.querySelectorAll('.pick')) {
        button.setAttribute('aria-current', button.dataset.board === code ? 'true' : 'false');
      }
      win.setStatus(facts.label || entry.label || `Community Board ${code}`);
    };

    list.replaceChildren(...rows.map((entry) => row(entry, show)));
    win.setStatus(`${rows.length} community boards. Pick one.`);
  },
};
