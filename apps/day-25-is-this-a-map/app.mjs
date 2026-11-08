/* Day 25 — Is this a map?.
 *
 * Not built yet. The window says so rather than the icon being missing: thirty missing
 * icons look like a broken desktop, and one window that explains itself is a smaller lie
 * than an empty folder.
 *
 * To build it, replace `mount` with a map. `apps/day-02-lines/app.mjs` is the worked
 * example, and `docs/apps.md` has the contract — the two that bite are `ready` for anything
 * that measures its container, and scoping every lookup to the subtree you returned.
 */

import { group, sheet } from '../../src/widgets.mjs';

export default {
  id: 'day-25-is-this-a-map',
  name: 'Day 25 — Is this a map?',
  icon: 'document',
  singleton: true,
  // No desktop icon: these live in the 30DaysOfMaps folder, which is the one icon on the
  // desktop. `apps/map-challenge/` draws them.
  onDesktop: false,
  width: 420,
  height: 240,

  mount() {
    return sheet([
      group('Is this a map?', [
        'Day 25 of the 30 Day Map Challenge, November 2026.',
        'Not built yet.',
      ]),
    ]);
  },
};
