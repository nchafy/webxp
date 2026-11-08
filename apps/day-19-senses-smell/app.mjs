/* Day 19 — Senses: Smell.
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
  id: 'day-19-senses-smell',
  name: 'Day 19 — Senses: Smell',
  icon: 'document',
  singleton: true,
  // No desktop icon: these live in the 30DaysOfMaps folder, which is the one icon on the
  // desktop. `apps/map-challenge/` draws them.
  onDesktop: false,
  width: 420,
  height: 240,

  mount() {
    return sheet([
      group('Senses: Smell', [
        'Day 19 of the 30 Day Map Challenge, November 2026.',
        'Not built yet.',
      ]),
    ]);
  },
};
