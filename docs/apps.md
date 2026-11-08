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

Shared dependencies are free to share: ES modules dedupe by resolved URL, so five apps
importing `/vendor/maplibre-gl.mjs` download it once and get one module instance. Vendor
inside your folder when the dep is yours alone; point at a shared path when it is not.

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
4. **Vendored dependencies move into the folder.** They are the app's, so they live with it.

## Checks

`scripts/check.py` covers this contract:

| Check | Asserts |
| --- | --- |
| `manifest is current` | The manifest agrees with the folders on disk. |
| `app loading` | Every folder became a program, in manifest order. |
| `app styles` | One link per app, it applies, and it is scoped to that app's windows. |
| `broken app` | Each way a spec can be wrong is rejected — and a valid one is accepted. |
| `iframe app` | A `frame` app hosts its page, and the shim raises its window. |
| `scaffold a new app` | `--new`, reload, it is in the menu and it opens. |
| `mount failure` | A throwing `mount` reports instead of hanging. |

`scaffold a new app` is the one that matters most: it is the only check that walks the path a
person actually takes.
