# webxp

Windows XP's Luna chrome, with the behaviour behind it. Windows move, resize from any of
eight edges, minimise into their own taskbar button and come back out of it, maximise to the
desktop, and raise on a click. No dependencies, no build step, no framework.

The theme is extracted from the 30 Day Map Challenge base map, where the window was one
maximised frame and the minimise and maximise boxes were decorative `<span>` elements. This
repository is that theme with the window manager the original only drew.

## Run it

```sh
python3 -m http.server 8000
```

Then open <http://127.0.0.1:8000/>. Loopback only — nothing here is meant to be reachable
from another machine.

## Check it

```sh
python3 -m venv .venv
.venv/bin/pip install websocket-client
.venv/bin/python scripts/check.py
```

Twenty checks drive real input through the Chrome DevTools Protocol and read the geometry
back. A window manager cannot be checked by screenshot: an inert frame and a working one are
the same picture, and what separates them is whether the numbers changed after a drag.

Two things keep the harness honest:

- `scripts/check.py --prove-failure` inverts the drag assertion, so the run must report a
  failure. A harness nobody has seen fail is a harness nobody has checked.
- `drag is title-bar only` is a negative control: the identical drag, started inside the
  window body, must move nothing.

`scripts/inspect_titlebars.py` prints the resolved focused and unfocused title-bar gradients,
because "they differ" does not say which is which.

## Use it

```html
<link rel="stylesheet" href="/src/theme.css">
<link rel="stylesheet" href="/src/shell.css">
<link rel="stylesheet" href="/src/window.css">
<div id="webxp"></div>
<script type="module" src="/boot.mjs"></script>
```

```js
import { Desktop, group, sheet } from './src/webxp.mjs';

const desktop = new Desktop({
  root: document.getElementById('webxp'),
  name: 'My desktop',
  programs: [{
    id: 'hello',
    name: 'Hello',
    icon: 'info',          // a name from ICON_NAMES
    singleton: true,       // a second launch focuses the open window
    onDesktop: true,       // also give it a desktop icon
    autostart: true,       // open it on mount
    width: 420, height: 280,
    status: true,          // give it a status bar at the foot
    mount({ win, desktop }) {
      win.setStatus('Ready');
      return sheet([group('Hello', ['Content goes here.'])]);
    },
  }],
});

desktop.autostart();
```

`mount` may be async, and may return any node. A program that starts a timer owns stopping
it: set `win.onClose`.

The shell is built in JavaScript rather than written as markup, because windows are created
and destroyed at runtime — a desktop spelled out in HTML would be a picture of one, and the
two copies would drift.

## Layout

| Path                         | Subject |
| ---------------------------- | ------- |
| `src/theme.css`              | The Luna palette, type, controls, group boxes, tables |
| `src/shell.css`              | The desktop, its icons, the taskbar, the Start menu |
| `src/window.css`             | The frame, title bar, resize grips, states, the loading marquee |
| `src/window.mjs`             | `XPWindow`: one window, its state, drag and resize |
| `src/desktop.mjs`            | `Desktop`: programs, z-order, focus, taskbar, Start menu |
| `src/icons.mjs`              | Eight drawn 16 × 16 icons |
| `src/widgets.mjs`            | `group`, `table`, `sheet`, `segment` |
| `src/webxp.mjs`              | The public surface |
| `demo/`                      | Five programs, and the three wallpapers one of them switches |
| `docs/windows.md`            | The window model, the clamps, and every departure from XP |
| `scripts/check.py`           | The checks |
| `scripts/cdp.py`             | DevTools Protocol client, copied verbatim from the map project |

A window knows nothing about the taskbar or the other windows. It is handed a `host` that
answers the four questions it cannot answer alone — where the desktop is, who to tell when
it wants focus, where its taskbar button is, and how to say something out loud. `Desktop` is
the only implementation. That split is what lets a window be tested without a shell.

## Read next

`docs/review.md` has the findings raised against this extraction, each with its failure
scenario and the check that now covers it — including the one that matters most, where
satisfying the contrast gate defeated the thing the colour was carrying.

`docs/windows.md` has the window model, the keyboard equivalents, the clamps, the open
sequence, and the table of deliberate departures from XP — including the contrast
corrections, with the measured ratios, and the one failing surface that is kept on purpose
and labelled rather than quietly passed.
