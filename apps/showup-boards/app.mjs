/* Community boards — the 59 boards, how to reach one, and when it meets.
 *
 * Ported from the `/board/` index and the 59 board pages, the same way the councils program
 * was: the list and the facts were two routes and are now two panes.
 *
 * Three artifacts, because a board's answer comes from three places. `lookup.json` has the
 * names and what each board covers. `preview.json` has the office, the phone, the cadence in
 * the board's own words, the covering council districts and the budget requests.
 * `board-meetings.json` has each board's own calendar, with when it was fetched and how
 * stale it is — which matters more here than anywhere else on the site, because the City runs
 * no board meeting calendar and these are the boards' own feeds.
 *
 * What the old pages had and this does not yet: the board's email and website, its public
 * members, and the prose on what a board can and cannot do. The first two are in the build's
 * board records but not in `preview.json`; naming that is better than letting it look
 * complete.
 */

import { group } from '../../src/widgets.mjs';
import { boards, load, usableTable, PREVIEW, BOARD_MEETINGS } from '../../shared/showup/data.mjs';

//: How many of a board's upcoming entries to list. Its own feed carries office closures and
//: screenings alongside meetings, so a long list is mostly not meetings.
const UPCOMING = 6;

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
    try {
      [rows, preview, calendars] = await Promise.all([
        boards(),
        load(PREVIEW),
        // The calendars are a nicety: a failure here must not cost the contact details,
        // which are the thing a reader most often came for.
        load(BOARD_MEETINGS).catch(() => null),
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
        nodes.push(group(spec.legend, usableTable(spec, desktop)));
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
