# shared

Code shared between apps, and not part of webxp.

`src/` is the window manager: the theme, the shell, the window, the loader, the widgets.
Anything in there is something webxp *is*. A 1.6 MB map engine in `src/` would mean webxp
ships a map engine, which it does not — so the map stack sits here instead, as a sibling.

The distinction is not cosmetic. It answers "what do I get if I take webxp?" without a
reader having to open the folder.

## What is here

| Path | Shared by |
| --- | --- |
| `map/basemap.mjs` | `createBaseMap`, the three basemaps and the satnav palette |
| `map/chrome.mjs` | `buildMapChrome` — the control subtree `createBaseMap` wires by class |
| `map/map.css` | Map styling, scoped on `.map-root` |
| `map/vendor/` | MapLibre GL JS, BSD-3-Clause, licence beside it |

An app declares what it needs and the loader does the rest:

```js
styles: ['../../shared/map/map.css', 'app.css'],
```

ES modules dedupe by resolved URL and the style loader keys its cache on the resolved href,
so thirty apps asking for the same file download and link it once.

Import it inside `mount`, never at module scope: every `app.mjs` is read at startup to build
the Start menu, so a module-scope import here is startup cost for a desktop where nobody
opened that app. `shared/map/chrome.mjs` carries the note about what that cost looked like
when it was got wrong.
