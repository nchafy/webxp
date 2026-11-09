/* Display Properties — an app that reaches back out and changes the shell it runs in.
 *
 * The `wall-*` classes it toggles ship with webxp in `src/shell.css`, not with this folder,
 * so this app depends on the window manager and not on the page hosting it.
 */

import { group, sheet, segment } from '../../src/widgets.mjs';

//: The desktop backgrounds, as classes rather than inline styles so a strict `style-src`
//: needs no exception for a background change.
const WALLPAPERS = [
  { value: 'bliss', text: 'Bliss' },
  { value: 'flat', text: 'Flat blue' },
  { value: 'night', text: 'Night' },
];

export default {
  id: 'display',
  name: 'Display Properties',
  filetype: 'settings',
  singleton: true,
  onDesktop: true,
  width: 380,
  height: 260,

  mount({ desktop }) {
    const current = WALLPAPERS.find((w) => desktop.surface.classList.contains(`wall-${w.value}`));

    const chooser = segment({
      label: 'Desktop background',
      options: WALLPAPERS,
      value: current ? current.value : 'bliss',
      onChange: (value) => {
        for (const wall of WALLPAPERS) {
          desktop.surface.classList.toggle(`wall-${wall.value}`, wall.value === value);
        }
        desktop.announce(`Desktop background: ${value}`);
      },
    });

    return sheet([
      group('Background', [chooser]),
      group('Window animation', [
        '--zoom-ms in theme.css. Skipped under prefers-reduced-motion.',
      ]),
    ]);
  },
};
