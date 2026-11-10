/* Show Up NYC — the folder, and the overview.
 *
 * Two jobs, because the old site had two things here and the port kept neither properly.
 *
 * ## The folder
 *
 * Icons in a grid, same as `apps/map-challenge/`. It was four rows of a name and a sentence
 * of description, which is a list of links rather than a folder, and it matched nothing else
 * on this desktop. `iconTile` from `src/widgets.mjs` is what both folders use now, so the
 * two cannot drift apart.
 *
 * Unlike that folder, this one cannot read its membership off `desktop.programs`: the day
 * apps share a `day-` prefix, and these four share `showup-` with nothing separating them
 * from each other. The order also matters — the map, then the two lists, then the read-me.
 * So `CONTENTS` is a list, and `launch` is guarded, because a typo in it is a dead icon
 * rather than a broken import.
 *
 * ## The overview
 *
 * The next citywide Council meeting, which is what the old landing page led with: the
 * committee, the day and time, how to get into the building, and how long is left to file
 * written testimony. It is citywide, so it belongs to no district and had nowhere to go when
 * the pages were deleted — which is why it vanished. `preview.json` has carried it the whole
 * time and nothing rendered it.
 *
 * Read in `ready` rather than `mount`, because it is a fetch: the window should be on screen
 * before anything waits on the network.
 */

import { group, iconTile } from '../../src/widgets.mjs';
import { load, PREVIEW } from '../../shared/showup/data.mjs';

//: What the folder contains, in the order it shows them.
const CONTENTS = [
  { id: 'showup-map', name: 'Map', icon: 'globe' },
  { id: 'showup-councils', name: 'City councils', icon: 'folder' },
  { id: 'showup-boards', name: 'Community boards', icon: 'folder' },
  { id: 'showup-readme', name: 'Readme', icon: 'document' },
];

const HEADING = 'Next City Council meeting — citywide';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* The citywide meeting, as the lines the old landing page showed.
 *
 * A line is dropped when its fact is absent rather than rendered empty: a blank beside
 * "Written testimony" would read as "the window has closed", which is the opposite of what
 * a missing stamp means.
 */
function meetingLines(meeting) {
  const when = [meeting.day, meeting.start_time, meeting.mode, meeting.time_state_label]
    .filter(Boolean)
    .join(' · ');

  const lines = [];
  if (meeting.committee) lines.push(el('strong', null, meeting.committee));
  if (when) lines.push(when);
  if (meeting.topic) lines.push(meeting.topic);
  if (meeting.venue_name) lines.push(meeting.venue_name);
  if (meeting.venue_address) lines.push(meeting.venue_address);
  // Photo ID, security, metal detectors. A reader who turns up without knowing this is the
  // person this site exists for.
  if (meeting.entry) lines.push(meeting.entry);

  const written = meeting.written;
  if (written && written.stamp) {
    lines.push(`${written.label || 'Written testimony'}: ${written.stamp}`);
  }
  // Only while it is still reachable. A passed deadline stated as a deadline reads as an
  // instruction a reader can still follow.
  const accommodation = meeting.accommodation;
  if (accommodation && accommodation.stamp && !accommodation.passed) {
    lines.push(`Accommodation requests by ${accommodation.stamp}`);
  }

  for (const link of meeting.links || []) {
    const [href, label] = link;
    if (!href || !/^https?:/.test(href)) continue;
    const anchor = el('a', null, label || href);
    anchor.href = href;
    anchor.rel = 'noopener noreferrer';
    lines.push(anchor);
  }
  return lines;
}

export default {
  id: 'showup',
  name: 'Show Up NYC',
  icon: 'folder',
  singleton: true,
  onDesktop: true,
  autostart: true,
  width: 560,
  height: 470,
  status: true,
  styles: ['app.css'],

  mount({ win, desktop }) {
    const sheet = el('div', 'sheet folder-sheet');

    const grid = el('ul', 'folder-grid');
    for (const entry of CONTENTS) {
      const item = document.createElement('li');
      item.append(iconTile({
        name: entry.name,
        icon: entry.icon,
        className: 'folder-icon',
        onOpen: async () => {
          // Awaited, and the await is the point: `launch` is async and resolves to the
          // window, or to null for a program that is not registered. Testing the promise
          // would make this guard unreachable, because a promise is always truthy.
          const opened = await desktop.launch(entry.id);
          if (!opened) {
            desktop.announce(`${entry.name} could not be opened. Its program is not registered.`);
          }
        },
      }));
      grid.append(item);
    }
    sheet.append(grid);

    // Filled by `ready`. A placeholder rather than an absent box, so the window does not
    // change height once the fetch lands.
    const overview = el('div', 'folder-overview');
    overview.append(group(HEADING, ['Reading the calendar…']));
    sheet.append(overview);

    win.setStatus(`${CONTENTS.length} programs. Each one opens in its own window.`);
    win.app = { overview };
    return sheet;
  },

  async ready({ win }) {
    const { overview } = win.app;
    try {
      const preview = await load(PREVIEW);
      const meeting = preview.next_meeting;
      overview.replaceChildren(group(
        HEADING,
        meeting
          ? meetingLines(meeting)
          // Not an error: the published calendar legitimately runs out, and saying so is the
          // honest answer. The old page omitted the whole block, which left a reader unable
          // to tell "nothing scheduled" from "this failed".
          : ['No Council or committee meeting is in the published calendar.'],
      ));
    } catch (error) {
      // Named, not swallowed. A reader who sees no meeting should be told whose fault it is.
      overview.replaceChildren(group(HEADING, [
        `The calendar could not be read (${error.message}).`,
      ]));
    }
  },
};
