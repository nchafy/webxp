/* City councils — the 51 Council districts, and who represents each.
 *
 * Ported from the `/district/` index and the 51 district pages. The list and the facts were
 * two routes; here they are two panes of one window, which is what makes the 51 pages a
 * program rather than 51 programs.
 *
 * Two artifacts, because the facts come at two depths. `preview.json` is the side-panel
 * summary the build already writes for the map, and it carries the committees, the
 * participatory-budgeting cycle and the covering community boards. `detail.json` is the
 * uncapped record: the member's email, their offices, their term, and a per-field note of
 * which source answered for each fact.
 *
 * What the old pages had and this does not: the testimony guidance and the citywide meeting
 * shortlist. Both are prose and a citywide artifact rather than per-district facts, and
 * neither is in either file — so they are named here as missing rather than quietly dropped.
 */

import { group, table } from '../../src/widgets.mjs';
import { districts, load, usableTable, PREVIEW, DETAIL } from '../../shared/showup/data.mjs';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* One row of the list. A button, not a link: there is no URL to go to, and a link whose href
 * goes nowhere is a lie to anything that reads the page. */
function row(entry, onPick) {
  const item = el('li', 'pick-item');
  const button = el('button', 'pick');
  button.type = 'button';
  button.dataset.district = String(entry.n);
  button.append(
    el('span', 'pick-name', `District ${entry.n}`),
    el('span', 'pick-hoods', entry.hoods || ''),
  );
  button.addEventListener('click', () => onPick(entry));
  item.append(button);
  return item;
}

/** `seat_status` -> `Seat status`, for a label built from a field name. */
function fieldLabel(name) {
  const words = String(name).replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* The term as one value, or null when the sources gave neither end.
 *
 * Null rather than a half-filled range: "2026-01-01 to" beside "Term" reads as a seat with
 * no end date, which is a different claim from an end date nobody published. District 3 is
 * the live case — both ends are absent there and all fifty others carry both. */
function termText(member) {
  if (member.term_start && member.term_end) return `${member.term_start} to ${member.term_end}`;
  if (member.term_start) return `From ${member.term_start}`;
  if (member.term_end) return `Until ${member.term_end}`;
  return null;
}

/** One office as its own record: its label, and only the lines the sources answered. */
function officeGroup(office) {
  const lines = [];
  if (office.address) lines.push(['Address', office.address]);
  if (office.phone) lines.push(['Phone', office.phone]);
  if (office.fax) lines.push(['Fax', office.fax]);
  if (office.hours) lines.push(['Hours', office.hours]);
  if (!lines.length) return null;
  return group(office.label || 'Office', { columns: ['Field', 'Value'], rows: lines });
}

/* Which source answered for each fact, folded away.
 *
 * A `details` rather than a group box. It answers "how do you know", which a reader wants
 * occasionally and never first, and three lines of dataset ids above the member's phone
 * number would make the pane mostly provenance.
 *
 * `missing` sits in the same disclosure because it is the same question from the other side:
 * a district with no committees table and no note reads as a member on no committees, which
 * is a different claim from "the source did not say". */
function provenanceDisclosure(full) {
  const sources = Object.entries(full.provenance || {});
  const missing = full.missing || [];
  if (!sources.length && !missing.length) return null;

  const box = el('details', 'provenance');
  box.append(el('summary', null, 'Where these facts came from'));
  if (sources.length) {
    box.append(table({
      columns: ['Fact', 'Source'],
      rows: sources.map(([field, source]) => [fieldLabel(field), source]),
    }));
  }
  if (missing.length) {
    const named = missing.map((name) => String(name).replace(/_/g, ' ')).join(', ');
    box.append(el('p', 'provenance-missing', `The sources did not answer for: ${named}.`));
  }
  return box;
}

export default {
  id: 'showup-councils',
  name: 'City councils',
  icon: 'folder',
  singleton: true,
  width: 720,
  height: 520,
  status: true,
  styles: [
    '../../shared/showup/list-detail.css',
    '../../shared/showup/links.css',
    'app.css',
  ],

  mount({ win }) {
    const root = el('div', 'list-detail');
    const list = el('ul', 'pick-list');
    list.setAttribute('aria-label', 'Council districts');
    const detail = el('div', 'detail-pane');
    detail.append(el('p', 'detail-empty', 'Pick a district to see who represents it.'));
    root.append(list, detail);

    win.app = { list, detail };
    win.setStatus('Loading the districts…');
    return root;
  },

  async ready({ win, desktop }) {
    const { list, detail } = win.app;

    let rows;
    let preview;
    let record;
    // Named, not swallowed, and not fatal. The uncapped record failing must not cost the
    // list and the summary — those are what a reader most often came for — but it must not
    // pass unmentioned either: "this member has no email" and "this window could not fetch
    // the email" are different claims, and a blank row makes the first one.
    let recordError = null;
    try {
      [rows, preview, record] = await Promise.all([
        districts(),
        load(PREVIEW),
        load(DETAIL).catch((error) => { recordError = error.message; return null; }),
      ]);
    } catch (error) {
      // Named, not swallowed: a reader who sees an empty list should be told whose fault it is.
      win.setStatus(`The districts could not be loaded (${error.message}).`);
      list.replaceChildren(el('li', 'pick-error', `Could not load: ${error.message}`));
      return;
    }

    const show = (entry) => {
      const key = String(entry.n);
      const facts = (preview.districts || {})[key] || {};
      const full = (record && record.districts ? record.districts[key] : null) || {};
      // The uncapped record wins where it is there. `preview.json` carries the name and the
      // seat status as well, and one fact with two renderers is one fact that can disagree;
      // the summary is the fallback for a session whose `detail.json` fetch failed.
      const member = full.member || facts.member;
      const nodes = [];

      const lines = [];
      if (member && member.name) lines.push(['Council Member', member.name]);
      // Only when it is not "filled": a status beside every name would read as a warning
      // about fifty filled seats.
      if (member && member.seat_status && member.seat_status !== 'filled') {
        lines.push(['Seat', member.seat_status]);
      }
      if (member && member.email) {
        lines.push(['Email', { text: member.email, href: `mailto:${member.email}` }]);
      }
      const term = member ? termText(member) : null;
      if (term) lines.push(['Term', term]);
      if (entry.hoods) lines.push(['Neighbourhoods', entry.hoods]);
      nodes.push(
        lines.length
          ? group(`Council District ${entry.n}`, { columns: ['Field', 'Value'], rows: lines })
          : group(`Council District ${entry.n}`, ['No member is recorded for this district.']),
      );

      for (const office of (member && member.offices) || []) {
        const card = officeGroup(office);
        if (card) nodes.push(card);
      }

      for (const spec of facts.tables || []) {
        nodes.push(group(spec.legend, usableTable(spec, desktop)));
      }

      if (recordError) {
        nodes.push(group('Where these facts came from', [
          `${DETAIL} could not be loaded (${recordError}). The email, the offices, the term `
          + 'and the per-field sources are absent from this pane for that reason, not '
          + 'because the City publishes none of them.',
        ]));
      } else {
        const provenance = provenanceDisclosure(full);
        if (provenance) nodes.push(provenance);
      }

      detail.replaceChildren(...nodes);
      // Scoped to this app's own list, not `document`: a query from the document would find
      // another window's rows the moment a second one opened.
      for (const button of list.querySelectorAll('.pick')) {
        button.setAttribute('aria-current', button.dataset.district === String(entry.n)
          ? 'true'
          : 'false');
      }
      win.setStatus(`Council District ${entry.n}${member && member.name ? ` — ${member.name}` : ''}`);
    };

    list.replaceChildren(...rows.map((entry) => row(entry, show)));
    win.setStatus(`${rows.length} council districts. Pick one.`);
  },
};
