/* About — a window that is neither resizable nor maximisable, so its grips and its maximise
 * box are gone rather than inert. */

import { group, sheet } from '../../src/widgets.mjs';

export default {
  id: 'about',
  name: 'About webxp',
  filetype: 'info',
  icon: 'check',
  singleton: true,
  width: 320,
  height: 180,
  resizable: false,
  maximizable: false,

  mount() {
    return sheet([
      group('webxp', [
        'A Windows XP window manager.',
        'Every program on this desktop is a folder under apps/. The contract is in '
        + 'docs/apps.md.',
      ]),
    ]);
  },
};
