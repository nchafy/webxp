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
| `filetype` | string | — | What the app opens. Picks the icon. See below. |
| `icon` | string | from `filetype` | One of the names in `src/icons.mjs`. Overrides the filetype. An unknown name is an error, not a fallback. |
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

### `filetype` — one group, one icon

Declare what the app opens, not which picture to draw:

```js
filetype: 'map',      // yes — every map app now looks like a map
icon: 'map',          // works, but says it once per app instead of once
```

Thirty map apps with `filetype: 'map'` share one answer to "which icon", and changing that
answer is one edit in `src/icons.mjs` instead of thirty. The registry is the extension point:

| `filetype` | Icon | For |
| --- | --- | --- |
| `map` | folded map | an app that draws a map |
| `document` | page | text, prose, a report |
| `notes` | page and pencil | something editable |
| `folder` | folder | an app that holds other apps |
| `settings` | monitor | a control panel |
| `info` | info bubble | a read-me, an about box |
| `web` | globe | a hosted page — `frame: { src }` |
| `tool` | tick | a diagnostic |

**Adding a filetype is one entry in `FILETYPES`**, and a new icon is one entry in `SHAPES`
beside it. Nothing else changes: the loader asks `iconFor(spec)` and the Start menu, the
taskbar and the desktop all follow.

Resolution order is `icon`, then `filetype`, then a generic window. So an app that is an
exception to its own group sets `icon` and keeps its `filetype` honest — `apps/about/` does
exactly that. An unknown `filetype` is an error for the same reason an unknown `icon` is: a
typo would otherwise be a silently generic window, and the point of a group is that
everything in it looks the same.

### `mount`

```js
mount({ win, desktop, args }) { return node; }
```

Return a node, an array of nodes, or a promise of either. It runs once per window, while the
frame is still showing its loading marquee.

- `win` — the `XPWindow`. `setStatus`, `setTitle`, `onClose`, `box`, `state`.
- `desktop` — the `Desktop`. `windows`, `announce`, `launch`, `surface`.
- `args` — what `launch` was given, or `null`. See below.

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

### Opening an app on something

`desktop.launch(id, args)` hands `args` to that program's `mount` and `ready`. It is how one
app opens another on a thing without either of them knowing the other exists:

```js
await desktop.launch('notepad', { path: '/home/user/readme.txt' });   // the terminal's `open`
```

Two rules:

- **The sender resolves what it sends.** The other window has its own state — a different
  working directory, a different selection — so a relative path or an index means nothing
  there. The terminal calls `realpath` before launching.
- **A singleton cannot take arguments.** It is mounted once, so a second launch has no
  `mount` to hand them to. The desktop warns to the console rather than dropping them
  silently: a launch whose arguments vanished looks like a program that ignored them.

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

## File associations — which program opens a `.txt`

`filetype` and the association table answer two different questions, and the names are close
enough to swap by accident:

| | Question | Lives in | Keyed by |
| --- | --- | --- | --- |
| `filetype` | which icon does this *program* draw? | `FILETYPES` in `src/icons.mjs` | a group name — `map`, `notes` |
| `FILE_PROGRAMS` | which *program* opens this file? | `src/apps.mjs` | an extension — `.txt`, `.gz` |

The table is keyed by extension, lower-cased, leading dot included. Its value is program ids,
most-preferred first: the first is what a double-click uses, and the whole list is what an
"Open With" menu offers.

```js
import { programsFor, noProgramFor } from '../../src/apps.mjs';

const [program] = programsFor(path);
if (!program) { win.setStatus(noProgramFor(path)); return; }
await desktop.launch(program, { path });
```

Three functions, one definition each:

| | |
| --- | --- |
| `extensionOf(path)` | `'.txt'`, lower-cased, `''` when there is none. The last extension only, so `a.gpx.gz` is a `.gz`. A leading dot is a name, so `.bashrc` has none. |
| `programsFor(path)` | the ids, most-preferred first. Frozen — copy before sorting. Empty means nothing is registered. |
| `noProgramFor(path)` | the line to show when it came back empty. |

`''` is a real key, not a placeholder: the mounted source tree has exactly two names with no
extension, `LICENSE` and `.gitignore`, and both are text.

### An unregistered type is refused, not guessed

A type with no entry resolves to nothing, and the caller says so. The alternative — fall back
to Notepad for anything — was rejected: every file in this filesystem is text, so that
fallback would resolve *everything* and the table would stop being able to answer the picker's
question. It would also hide what is missing. There is no image viewer, so `.png` has to come
back empty for the gap to be visible at all.

The cost is real and is one line of data to pay off: `echo hi > ~/notes.yml` and
double-clicking it is a refusal until `.yml` is added. Add it, rather than widening the lookup.

### Two ways an association dangles

**An id no program registered.** `Desktop.launch` returns `null` for a program it does not
have, so `.png → ['paint']` with no Paint on the disk is a double-click that does nothing at
all — not an error, nothing. `loadApps` compares the table against the registry it just filled
and writes a console line naming every dangling pair, which the suite's end-of-run console
sweep turns into a failure.

**An id that cannot be handed a path.** This one has no detector that catches every case and
it looks exactly like a working association. `.html` goes to Notepad rather than to `webview`
even though `webview` exists, because `webview` is a singleton `frame` app: it hosts its own
page, and a second launch warns to the console and discards the arguments. **Only name a
program that reads `args.path` as a file.** Today that is `notepad`; `explorer` and `terminal`
take a path too, but a directory.

So the table deliberately resolves almost everything to Notepad. That is not the table being
pointless — it is the table being honest. There is no image viewer, so `.png` resolves to
nothing and says so, where before it was handed to a text editor.

### Adding one

One line of data:

```js
'.png': ['image-viewer', 'notepad'],
```

Nothing else changes. Adding a program that opens an existing type is an edit to the same line,
and its position in the list is its preference.

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
    fs/
      vfs.mjs           the filesystem
      fs.mjs            the one mount, and its persistence
  apps/
    basemap/app.mjs     -> ../../shared/map/
    day-02-lines/app.mjs -> ../../shared/map/
    terminal/app.mjs    -> ../../shared/fs/
    notepad/app.mjs     -> ../../shared/fs/
```

`shared/fs/` is the case where sharing is the feature rather than an economy: the terminal
and Notepad must get the *same* filesystem, or a save in one lands in a tree the other cannot
see. The module-level mount in `fs.mjs` is what guarantees it.

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
   classes**. The count is the invariant; the class names are not. This has bitten eight
   times, usually on an edit that looked purely mechanical.

   The third column is "what decided it", not "what it lost to", because a tie has two
   outcomes and only one of them is a loss. **A tie you win by accident is the same defect as
   a tie you lose:** both put the result in the order of the `<link>` tags, and the one that
   currently looks right is the one nobody will check before reordering them.

   | Rule | Became | What decided it | What the reader saw |
   | --- | --- | --- | --- |
   | `.app-basemap .basemap-root > .map` (3) | `.map-root > .map` (2) | tied `.map-root .maplibregl-map` (2); the vendored block sits last | The map container collapsed to 0 px high and the window drew only the holding colour, while every MapLibre flag read healthy. |
   | `.app-basemap .map` (2) | — | tied `.app-basemap .maplibregl-map` (2); source order | The same collapse, in the form it took before the move to `shared/`. |
   | `.map-root .map-compass` (2) | — | lost to `.map-root .map-zoom button` (2 classes and a type) | A 34 px compass rose inside a 28 px button, overhanging three pixels a side and sitting four pixels down and right of centre. |
   | `.task-button:hover` (2) | a third rule, `.task-button[aria-pressed="true"]:hover` (3) | tied `.task-button[aria-pressed="true"]` (2); source order | The taskbar button for the *active* window — the one a pointer is over most of the time — answered a hover with nothing, while every other button on the bar lit up. |
   | `.clippy-says` (1) | `.clippy-bubble .clippy-says` (2) | lost to `.card p` (1 and a type) | Clippy's line took `.25rem 0` instead of `0 0 .5rem`: the text sat four pixels low and the buttons crowded the question. |
   | `.clippy-bubble` (1) | `.clippy .clippy-bubble` (2) | tied `.card` (1) on four declarations; won only because `demo.css` is the last `<link>` | Nothing yet. Reorder the four `<link>` tags in `index.html` and the tooltip turns bisque with a grey bevel. |
   | `.window-body.is-loading` (2) | `.window > .window-body.is-loading` (3) | tied `.window-body.app-terminal` (2); `app.css` is linked later, so black won | A loading Terminal showed the marquee bar sliding in a void: the caption under it measured 1.72:1 on `#0c0c0c` and was not there. |
   | `.picker-row[aria-selected="true"]` (2) | `.picker-list .picker-row[aria-selected="true"]` (3) | tied `.picker-row:hover` (2) on `background`; source order | Nothing yet. Reorder the two blocks in `src/picker.css` and a selected row under the pointer keeps `color: #fff` over hover's 16% wash of `--title-b` on `--bg` — rgb(223 232 241), 1.24:1, against the 5.31:1 the selection is chosen for. |
   | `.app-explorer .exp-row[aria-selected="true"]` (3) | `.app-explorer .exp-list .exp-row[aria-selected="true"]` (4) | tied `.app-explorer .exp-row:hover` (3) on `background`; source order | Nothing yet. The same 1.24:1, in the same shape, one sheet over. Nothing pinned it at all until this row. |
   | `.folder-item[aria-selected='true']` (2) | `.folder-items .folder-item[aria-selected='true']` (3) | tied `.folder-item:hover` (2) on `background` *and* `border-color`; source order | Nothing yet. Reorder them and a selected tile under the pointer is white on 20% of `--title-b` over `--bg` — rgb(216 226 237), 1.31:1, against 14.25:1 on `--title-a`. |

   One more shape, which the table above cannot express, because the rule *won*:

   | Rule | Became | What it outranked, and deleted |
   | --- | --- | --- |
   | `.app-day-02-lines .pass-row` (2) | `.app-day-02-lines .pass-row:not(:focus-visible)` (3) | `button:focus-visible` (1 and a type). A flat `box-shadow: none` on the pass-row deleted the theme's focus halo, which is a `box-shadow`. The indicator survived only because `:focus-visible` also paints a background — it rested on a colour rule rather than on the mechanism `theme.css` documents. |

   Three further pairs tie and happen to resolve the way they should —
   `.icon-tile:active` against `.desktop-icon:hover`, `.start-item:active` against
   `.start-item:hover`, and `.window.is-max` against `.window:not(.is-active)`. None has a
   visible symptom, so none gets a row; the first two were rewritten in longhands so the two
   sides share no property and the tie decides nothing, and the third says itself at three
   classes.

   The compass was reported from a screenshot, because no check looked inside a control;
   `basemap app` now measures the rose against its button. `shared/map/map.css` carries the
   live selectors and a note to count before touching them, and `scripts/css_ties.py` pins
   the live decision behind every row above — the first two rows are two spellings of one
   decision, so they share a pin. Read its docstring first: it is a regression guard over this
   list, not a detector, so it will not find the ninth. It needs no browser.
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
| `app filetypes` | A `filetype` picks the icon, an explicit `icon` overrides it, an unknown one is rejected — and two apps of one filetype resolve to the *same* icon, which is the property the key exists for. |
| `autostart` | `Desktop.autostart()` opens the programs that ask for it and leaves the others shut, exercised with programs registered by the check rather than with whichever app happens to carry the key. |
| `app styles` | One link per app, it applies, and it is scoped to that app's windows. |
| `broken app` | Each way a spec can be wrong is rejected — and a valid one is accepted. |
| `iframe app` | A `frame` app hosts its page, and the shim raises its window. |
| `basemap app` | The ported project fills its window **and painted** — canvas measured against the window, plus a screenshot. |
| `30DaysOfMaps folder` | A folder app derives its contents from `desktop.programs` and launches them; the shared `iconTile` reads on both the wallpaper and a window's sheet. |
| `day 02 lines` | The `mount`/`ready` split on a real map app: data loaded, its own layers present by id on whatever basemap is remembered, and the status bar uncovered with the drill-down panel open at three window sizes. |
| `scaffold a new app` | `--new`, reload, it is in the menu and it opens. |
| `mount failure` | A throwing `mount` reports instead of hanging. |
| `launch arguments` | `launch(id, args)` reaches `mount`, and a singleton that cannot take them says so. |
| `open across apps` | The terminal's `open` hands Notepad a path, Notepad saves, the terminal reads it back: two apps, one shared module, asserted rather than assumed. |

`scaffold a new app` is the one that matters most: it is the only check that walks the path a
person actually takes.

The rest of the suite divides the same way the code does. The window manager's own
behaviour — opening, focus, dragging, the eight resize edges, the clamps, minimise and
maximise — is in `docs/windows.md`. The filesystem's is in `shared/fs/README.md`:
`filesystem` for the model, `terminal` for the shell over it, `mount` and `mount index is
current` for the repository grafted at `/usr/src/webxp`, `explorer` for the folder window,
and `filesystem persists` for a reload.

No count here, deliberately. Three prose counts in this file and `docs/review.md` have
already gone stale — each one the same way, a check added without the sentence above it
being read — and `CHECKS` in `scripts/check.py` is the list that cannot.
