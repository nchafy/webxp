/* The demo desktop. Everything specific to it is here or in `programs.mjs`. */

import { Desktop } from '../src/webxp.mjs';
import { PROGRAMS } from './programs.mjs';

const desktop = new Desktop({
  root: document.getElementById('webxp'),
  name: 'webxp',
  tagline: 'a Windows XP window manager',
  footer: 'Programs are registered, not hard-coded. See demo/programs.mjs.',
  programs: PROGRAMS,
});

desktop.surface.classList.add('wall-bliss');
desktop.autostart();

// For the devtools console and for `scripts/check.py`, which reads the geometry rather than
// trusting a screenshot. Not a test hook: the check could read the same numbers off the
// custom properties, and this is the handle a person would want anyway.
window.webxp = desktop;
