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
 * Two blocks, in the order the old landing page had them: the address box, then the next
 * citywide Council meeting.
 *
 * The address box is `apps/showup/address.mjs`, which carries the whole reason only one of
 * its four kinds of input ever leaves the machine. It is in this position because that is
 * where the landing page put it — the first thing on the page was "which district am I in".
 *
 * The meeting is the committee, the day and time, how to get into the building, and how long
 * is left to file written testimony. It is citywide, so it belongs to no district and had
 * nowhere to go when the pages were deleted — which is why it vanished. `preview.json` has
 * carried it the whole time and nothing rendered it.
 *
 * Both are read in `ready` rather than `mount`, because both are fetches: the window should
 * be on screen before anything waits on the network.
 */

import { folderView, group } from '../../src/widgets.mjs';
import { load, PREVIEW } from '../../shared/showup/data.mjs';
import { addressBox } from './address.mjs';

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
  // Not `autostart`. It was, while this was the only icon on its own desktop; here it is
  // one icon among forty-odd, and a window this project opened uninvited sat over the
  // click targets of nine checks in `scripts/check.py` — including `boot`, which says
  // outright that a fresh desktop wants no windows open. A guest does not open its own
  // window on someone else's desktop.
  width: 560,
  // Tall enough for the icons, the address box and the meeting at once. The body scrolls, but
  // an address box below the fold is an address box nobody finds.
  height: 600,
  status: true,
  // `links.css` carries `.cross-link`, which is what the address box's choices render as —
  // the same affordance the councils and boards programs use for the same kind of jump.
  styles: ['../../src/folder.css', '../../shared/showup/links.css', 'app.css'],

  mount({ win, desktop }) {
    const sheet = el('div', 'sheet folder-sheet');

    const view = folderView({
      items: CONTENTS.map((entry) => ({
        name: entry.name,
        icon: entry.icon,
        onOpen: async () => {
          // Awaited, and the await is the point: `launch` is async and resolves to the
          // window, or to null for a program that is not registered. Testing the promise
          // would make this guard unreachable, because a promise is always truthy.
          const opened = await desktop.launch(entry.id);
          if (!opened) {
            desktop.announce(`${entry.name} could not be opened. Its program is not registered.`);
          }
        },
      })),
      places: [
        { name: '30DaysOfMaps', icon: 'folder', onOpen: () => desktop.launch('map-challenge') },
      ],
      details: [
        ['Show Up NYC', `${CONTENTS.length} programs`],
        ['Type', 'Folder of programs'],
        ['Built from', "the City's published data"],
      ],
    });

    // Ships with its form hidden and reveals it in `ready`, once it has the index it answers
    // a ZIP, a neighbourhood and a district number from. A box that cannot answer those would
    // have only the geocoder left, which is the one input a reader did not ask to send.
    const address = addressBox(desktop);
    sheet.append(view);

    const lookup = el('div', 'folder-lookup');
    lookup.append(address.node);
    sheet.append(lookup);

    // Filled by `ready`. A placeholder rather than an absent box, so the window does not
    // change height once the fetch lands.
    const overview = el('div', 'folder-overview');
    overview.append(group(HEADING, ['Reading the calendar…']));
    sheet.append(overview);

    win.setStatus(`${CONTENTS.length} programs. Each one opens in its own window.`);
    // A fetch in flight and a pair of timers, so they are stopped with the window.
    win.onClose = () => address.dispose();
    win.app = { overview, address };
    return sheet;
  },

  async ready({ win }) {
    const { overview, address } = win.app;
    // Not awaited together with the calendar: either one failing should leave the other
    // rendered, and `Promise.all` would make the first failure hide the second block. The box
    // reports the failure it expects in its own status line, so anything arriving here is the
    // unexpected kind and an unhandled rejection would be its only trace.
    address.ready().catch((error) => {
      console.error(`showup: the address box could not start: ${error.message}`);
    });
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
