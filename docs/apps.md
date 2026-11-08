# Writing an app

An app is a folder under `apps/`. It owns its code, its styles and its assets, and it can be
moved or deleted as a unit.

## Add one in three steps

```sh
scripts/apps.py --new myapp     # creates apps/myapp/, regenerates the manifest
# edit apps/myapp/app.mjs
python3 -m http.server 8000      # it is in the Start menu and on the desktop
```

That is the whole loop. If you make the folder by hand instead, run `scripts/apps.py`
afterwards so the manifest picks it up.

## Why there is a manifest

A browser cannot list a directory over static HTTP. There is no request that answers "what is
in `apps/`", so something has to write the answer down: `apps/manifest.mjs` is a generated
list of folder names, and `scripts/apps.py` regenerates it from the folders on disk.

It is generated, never hand-edited. `scripts/apps.py --check` exits 1 if it disagrees with
the disk, and the check suite runs that — a stale manifest is the one failure mode of this
arrangement that looks like nothing at all, because the app is simply not there.

## The contract

`apps/<id>/app.mjs` default-exports one object.

### Required

| Key | Type | Notes |
| --- | --- | --- |
| `id` | string | **Must equal the folder name.** Lower-case, digits, dashes. |
| `name` | string | The window title and the menu label. |
| `mount` | function | Builds the content. See below. Omit only if you set `frame`. |

`id` is checked against the folder because the folder is the identity. A spec whose `id`
disagrees is almost always a copied app whose id was not changed, and the symptom without
the check is the original app vanishing from the registry, replaced by its copy.

### Optional

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `icon` | string | `window` | One of the names in `src/icons.mjs`. An unknown name is an error, not a fallback. |
| `ready` | function | — | Runs after your nodes are in the document and laid out. |
| `styles` | string[] | none | Paths relative to the folder, linked once before first mount. |
| `width`, `height` | number | 480 × 320 | |
| `x`, `y` | number | cascade | Omit to let the desktop place it. |
| `resizable` | boolean | `true` | `false` removes the grips rather than making them inert. |
| `minimizable`, `maximizable` | boolean | `true` | `false` hides that title box. |
| `maximized` | boolean | `false` | Open maximised. |
| `status` | boolean | `false` | Gives a status bar; `win.setStatus(…)` writes to it. |
| `singleton` | boolean | `false` | A second launch focuses the open window. |
| `onDesktop` | boolean | `false` | Also give it a desktop icon. |
| `autostart` | boolean | `false` | Open it when the desktop mounts. |
| `minLoadMs` | number | 120 | Override the loading floor for this app. |
| `frame` | object | — | `{ src, sandbox?, allow? }`. Hosts a page instead of mounting. |

Any other key is an error. A misspelled `singelton` is indistinguishable from a missing
feature at runtime, so the loader rejects what it does not recognise rather than ignoring it.

### `mount`

```js
mount({ win, desktop }) { return node; }
```

Return a node, an array of nodes, or a promise of either. It runs once per window, while the
frame is still showing its loading marquee.

- `win` — the `XPWindow`. `setStatus`, `setTitle`, `onClose`, `box`, `state`.
- `desktop` — the `Desktop`. `windows`, `announce`, `launch`, `surface`.

Two rules that matter:

1. **Scope every DOM lookup to your window.** `document.getElementById('map')` breaks the
   moment a second window of your app opens, and breaks silently — the second window drives
   the first one's elements. Build your nodes and keep references, or query inside the subtree
   you returned.
2. **If you start it, stop it.** A timer, an observer, a listener on `window`, a fetch you
   want cancelled: hang the cleanup on `win.onClose`. Otherwise it outlives the window and
   writes into a detached tree forever.

```js
const timer = setInterval(refresh, 180);
win.onClose = () => clearInterval(timer);
```

A `mount` that throws does not hang. The window shows the error message, the program id and a
pointer to the console, and the rest of the desktop is unaffected.

### `ready` — when you need your nodes measured

```js
mount({ win })  { const root = build(); win.app = { root }; return root; },
async ready({ win }) { const { root } = win.app; /* root has a box now */ },
```

`mount` cannot measure anything. What it returns is what gets inserted, so during `mount`
your nodes are not in the document and every rect is zero. Anything that sizes itself to its
container — a map, a chart, a canvas, a virtualised list — reads zero there.

The failure is nasty because it does not look like one. The Base map app built this way
reported `styleLoaded`, `tilesLoaded` and 113 layers, fetched every tile with a 200, and drew
a blank rectangle. Use `ready` for that work.

Stash what `mount` built on the window (`win.app = …`), not in a module variable: a module
variable is shared by every window of your app, so the second window would drive the first
one's content.

A throwing `ready` is reported exactly like a throwing `mount`.

## Styles

Declare them, and scope them:

```js
styles: ['app.css'],
```

The loader links each one once — N windows of one app share one stylesheet — and waits for it
to apply before your content is laid out, so nothing flashes unstyled. A declared stylesheet
that 404s becomes a window saying so, not a window that quietly looks wrong.

The loader puts `app-<id>` on your window's client area. **Scope every rule under it.**

```css
/* apps/myapp/app.css */
.app-myapp .toolbar { ... }    /* yes */
.toolbar { ... }               /* no — reaches into every other app's windows */
```

Use the theme's tokens and shapes rather than inventing your own: `var(--face)`,
`var(--ink)`, `.sheet`, `.group`, `.group-table`, and the builders in `src/widgets.mjs`
(`group`, `table`, `sheet`, `segment`). An app that uses them looks like part of the desktop
for free.

### Sizing to the window, not the screen

`.window-body` is a container named `window`, so an app can respond to its own window:

```css
@container window (max-width: 26rem) {
  .app-myapp .columns { grid-template-columns: 1fr; }
}
```

Use this instead of `@media`. A window 300 px wide on a 1600 px desktop has exactly the
problem a 300 px phone has, and a media query cannot see it.

## Assets

Resolve them against your own module, never against the page:

```js
const data = await fetch(new URL('./data.json', import.meta.url)).then((r) => r.json());
const { draw } = await import('./vendor/chart.mjs');
```

`import.meta.url` is what makes the folder portable — move it, rename the mount point, host
it from a subdirectory, and the paths still resolve.

## Keeping an app cheap

Keep `app.mjs` small and put heavy imports inside `mount`:

```js
export default {
  id: 'maps', name: 'Maps', icon: 'globe',
  async mount({ win }) {
    const maplibre = await import('./vendor/maplibre-gl.mjs');   // not at module scope
    // …
  },
};
```

Every app's `app.mjs` is imported at startup so the Start menu can be built, so module-scope
work is startup cost for an app nobody opened. A dynamic `import()` inside `mount` is paid
when the window opens.

## Sharing a dependency between apps

Vendor inside your folder while the dependency is yours alone. Move it to `shared/` when a
second app needs it.

```
webxp/
  src/                  the window manager: theme, shell, window, loader, widgets
  shared/
    map/
      basemap.mjs       the engine
      chrome.mjs        the controls, as a subtree an app drops in
      map.css           scoped on the class chrome.mjs sets
      vendor/maplibre-gl.mjs
  apps/
    basemap/app.mjs     -> ../../shared/map/
    day-02-lines/app.mjs -> ../../shared/map/
```

**`shared/` is a sibling of `src/`, not a directory inside it.** `src/` is the window
manager. A 1.6 MB map engine in there means webxp ships a map engine, which it does not:
`shared/` says the true thing — shared between apps, not part of the window manager. Nothing
technical turns on this. ES modules dedupe by resolved URL, so thirty apps importing one path
download it once and get one module instance wherever that path lives; the directory name is
for the reader.

A shared module gets no special treatment from the loader. It is a path, and `styles` entries
resolve relative to the app folder like any other:

```js
styles: ['../../shared/map/map.css', 'app.css'],
```

**A shared module documents its own shape.** `shared/map/README.md` carries the map subtree,
what `buildMapChrome` builds and why it is laid out that way; this file does not restate it,
because two renderings of one structure is how they drift apart. Read the shared module's own
README before building against it.

### Import a shared engine inside `mount`, never at module scope

This is the rule that costs something to get wrong, and it is a consequence of sharing rather
than of any one dependency.

```js
// no — the whole desktop now pays for this at startup
import { createBaseMap } from '../../shared/map/basemap.mjs';

// yes — paid when a window opens
async mount({ win }) {
  const { createBaseMap } = await import('../../shared/map/basemap.mjs');
}
```

Every `app.mjs` is imported at startup so the Start menu can be built. A module-scope import
of a shared engine therefore loads that engine for every reader, including the ones who never
open the app — and once thirty apps point at one engine, one of them importing it eagerly
spends the whole desktop's startup budget.

It is worse than it sounds, because a shared module can reach back and do this to you. A
`chrome.mjs` that imported `BASEMAPS` from `basemap.mjs` pulled the engine in at module scope
through what looked like a cheap import of a control builder. The symptom was a newly
scaffolded app missing from the loaded list — a long way from the cause. The fix was to make
the presets an argument, `buildMapChrome({ basemaps })`, so the shared chrome module imports
nothing at all.

**A shared module meant for `mount` should import nothing at module scope itself.** That is
the property that makes it safe for an app to import it eagerly by mistake.

## Hosting a project you already have

Set `frame` instead of `mount` and nothing gets rewritten:

```js
export default {
  id: 'webview', name: 'Webview', icon: 'globe', singleton: true,
  frame: { src: 'page.html', sandbox: 'allow-same-origin allow-scripts' },
};
```

`src` resolves against the app folder, so a relative path keeps the folder portable; an
absolute URL works the same way. `apps/webview/` is a worked example.

What you give up: the hosted page is a separate document, so it does not inherit the Luna
theme, cannot use `src/widgets.mjs`, and cannot talk to the desktop. Port it to a `mount`
when you want it to look like part of the system.

One behaviour to expect rather than debug: **the first click on hosted content only raises its
window.** A pointer event inside an iframe does not cross the document boundary, so the
window manager cannot see a click on the page. A transparent shim exists only while the window
is unfocused and catches that first click. This is what every window manager does.

## Porting an existing project: what actually bites

From porting the 30 Day Map Challenge base map into `apps/basemap/`:

1. **Global element ids.** The original looked up `#map`, `#map-status`, `#map-zoom-in` and
   so on from `document`. One window works; two windows fight. Every lookup had to be scoped
   to a root element the app builds and owns.
2. **The project's own chrome is now the desktop's.** The original shipped a title bar, a
   taskbar and a slide-out panel. All three were deleted — webxp already is the chrome, and
   keeping them would have been two of everything.
3. **Page-level CSS becomes window-level CSS.** Rules written against `body` and `100vh`
   became rules against the app's own class and `100%`.
4. **Vendored dependencies move into the folder** — until a second app wants them, at which
   point they move again, to `shared/`. See "Sharing a dependency between apps".
5. **Scoping flattens specificity, and that changes who wins.** This one cost the most. The
   original relied on `.window-body > .map` outranking MapLibre's own `.maplibregl-map` to
   keep `position: absolute` on the map container. Scoping both rules under `.app-basemap`
   made them *equal*, so source order decided it — and the vendored block sits last.
   MapLibre's `position: relative` won, the container collapsed to 0 px high, and the window
   drew nothing while every flag read healthy.

   When you scope a stylesheet that already fought a specificity battle, **count the
   classes**. The count is the invariant; the class names are not. The same rule has broken
   twice on rescopes that looked purely mechanical, each time landing on exactly MapLibre's
   specificity. `shared/map/map.css` carries the live selector and a note to count before
   touching it.
6. **Anything measuring its container goes in `ready`.** See above.

The lesson behind 5 and 6: **look at the window**. `tilesLoaded: true` and a blank rectangle
are the same assertion away from each other. The check for this app screenshots the canvas
and compares it against the window's own size, because nothing cheaper could tell them apart.

## Checks

`scripts/check.py` covers this contract:

| Check | Asserts |
| --- | --- |
| `manifest is current` | The manifest agrees with the folders on disk. |
| `app loading` | Every folder became a program, in manifest order. |
| `app styles` | One link per app, it applies, and it is scoped to that app's windows. |
| `broken app` | Each way a spec can be wrong is rejected — and a valid one is accepted. |
| `iframe app` | A `frame` app hosts its page, and the shim raises its window. |
| `basemap app` | The ported project fills its window **and painted** — canvas measured against the window, plus a screenshot. |
| `30DaysOfMaps folder` | A folder app derives its contents from `desktop.programs` and launches them; the shared `iconTile` reads on both the wallpaper and a window's sheet. |
| `day 02 lines` | The `mount`/`ready` split on a real map app: data loaded, its own layers present by id on whatever basemap is remembered, and the status bar uncovered with the drill-down panel open at three window sizes. |
| `scaffold a new app` | `--new`, reload, it is in the menu and it opens. |
| `mount failure` | A throwing `mount` reports instead of hanging. |

`scaffold a new app` is the one that matters most: it is the only check that walks the path a
person actually takes.

These ten are the contract's. The suite is thirty-three; the rest belong to the window
manager and are described in `docs/windows.md`.
