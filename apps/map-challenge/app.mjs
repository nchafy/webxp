/* 30DaysOfMaps — a folder of the thirty challenge days.
 *
 * One icon on the desktop instead of thirty. Opening it shows the days as icons; opening one
 * of those launches that day as its own window, so several can be open at once and each is
 * a real, movable, resizable window rather than a pane inside this one.
 *
 * ## Why the folder owns no list of days
 *
 * It reads `desktop.programs` and takes everything whose id starts `day-`. The loader has
 * already imported every `app.mjs` to build the Start menu, so each day's own `name` and
 * `icon` are in memory before this window opens — there is nothing to fetch and nothing to
 * keep in step. A day added to `apps/` appears here by existing.
 *
 * The alternative was thirty `{ id, name }` rows in this file, which is the same thirty
 * strings written twice, and the copy that drifts is the one nobody opens.
 *
 * ## Why the name and the folder disagree
 *
 * The folder is `apps/map-challenge/` and the label is "30DaysOfMaps". `scripts/apps.py`
 * requires an id matching `^[a-z][a-z0-9-]*$`, and "30DaysOfMaps" fails twice: it opens with
 * a digit and it has capitals. So the id is a valid slug and the name is the name, which is
 * the only one a reader ever sees.
 */

import { folderView } from '../../src/widgets.mjs';

//: Every day app's id begins with this. It is the whole membership rule.
const DAY_PREFIX = 'day-';

export default {
  id: 'map-challenge',
  name: '30DaysOfMaps',
  icon: 'folder',
  singleton: true,
  onDesktop: true,
  width: 620,
  height: 440,
  status: true,
  styles: ['../../src/folder.css', 'app.css'],

  mount({ win, desktop }) {
    const days = [...desktop.programs.values()]
      .filter((program) => program.id.startsWith(DAY_PREFIX))
      // By id, which sorts by day number because the day numbers are zero-padded. Without
      // the padding `day-2-…` would sort after `day-10-…` and a string sort would be wrong.
      .sort((a, b) => a.id.localeCompare(b.id));

    win.setStatus(
      days.length
        ? `${days.length} days. Each one opens in its own window.`
        : 'No day apps found under apps/.',
    );

    return folderView({
      items: days.map((program) => ({
        name: program.name,
        icon: program.icon,
        // The desktop owns opening things, including focusing a singleton that is already
        // open. Nothing here needs to know which of those happened.
        onOpen: () => desktop.launch(program.id),
      })),
      places: [
        {
          name: 'Base map',
          icon: 'globe',
          onOpen: () => desktop.launch('basemap'),
        },
      ],
      details: [
        ['30DaysOfMaps', `${days.length} days`],
        ['Type', 'Folder of programs'],
      ],
    });
  },
};
