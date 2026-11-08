# The shared map layer

Three basemaps, one chrome subtree, and a `draw` callback. `shared/map/basemap.mjs` is the
engine, `shared/map/chrome.mjs` builds the furniture it wires by class, `shared/map/map.css`
styles it and `shared/map/panel.mjs` runs the drill-down panel. The era itself is
`src/theme.css` and nowhere else: a map colour MUST NOT appear in the window chrome, and a
Luna colour MUST NOT appear in the map.

Ported from the 30daymapchallenge project, where the map was a page. `shared/README.md`
carries why this is a sibling of `src/` rather than part of webxp.

## The three basemaps

| Name | Source | Max zoom | Licence |
| --- | --- | --- | --- |
| `simple` | [OpenFreeMap](https://openfreemap.org/) `liberty`, vector | 18 | Data ODbL, [OSM contributors](https://www.openstreetmap.org/copyright) |
| `satellite` | [Esri](https://www.esri.com/) ArcGIS Online `World_Imagery` + `World_Boundaries_and_Places`, raster | 19 | Esri terms — see Licensing |
| `geography` | [OpenTopoMap](https://opentopomap.org/), raster | 17 | Style [CC-BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/), data ODbL + SRTM |

Adding a fourth is one object in `BASEMAPS` plus one button in the page's
`.layer-switch`. The object needs a `label`, a `maxZoom`, an `attribution`, a
`style()` returning a style object or a promise of one, and an `overlayBefore(style)`
returning the layer id a day's overlay is inserted before.

The reader's choice is remembered in `localStorage` under `30dmc:basemap`, so it
survives moving between days. A page MAY force one with `basemap: 'satellite'`.

## The camera: tilt, 3D and the compass

The `simple` basemap is an early-2000s car satnav, and those had two things a web map
usually does not: a bird's-eye lean and a heading you could turn.

- **Tilt** is on by default. Drag with ctrl held (or the right button), two-finger
  drag on touch, or press **Tilt**, which eases between pitch 0 and 55. A day that must
  stay flat and north-up passes `tilt: false`, which also hides the Tilt button and the
  compass — a compass that indicates something you cannot change is furniture.
- **Rotate** is the same gesture set, plus the compass: pressing it faces north, and the
  arrow keys turn the view 15° while it has focus. That keyboard path is the only
  rotation a keyboard can reach, since every other route is a drag.
- **3D buildings appear only from an angle.** liberty draws `building-3d` as a
  `fill-extrusion` from zoom 14. Seen at pitch 0 you are looking straight down the top of
  each block: the walls collapse and it reads as a smeared grey shape offset from its own
  footprint. So the extrusion layers are gated on camera pitch — visible at 12° and above,
  hidden below 8°. Two thresholds and not one, because a single cutoff makes the buildings
  strobe while a drag-pitch sits on it.

The gate is driven by the camera, never by the button: pressing **Tilt** and dragging to
the same angle give the same result by construction. It is re-asserted after every style
load, because `setStyle` brings a fresh style's layers back visible.

The extrusion layers are found by `type === 'fill-extrusion'` rather than by name, so a
basemap shipping more than one is covered. The flat `building` fill underneath is left
alone — in 3D the extrusions cover their own footprints, and toggling both would flicker
two layers instead of one.

### Why `liberty` and not `positron`

`positron` was the first choice and was wrong. Measured with
`scripts/inspect_style.py positron liberty`:

- positron draws the whole `park` source layer as `rgb(230, 233, 229)` — a grey.
- positron ships no `landcover_grass`, no `landuse_pitch`, no `landuse_cemetery`
  and no `park_outline` layer at all.
- Its `landcover_wood` starts at `minzoom: 10` and fades in across zoom 8–12.

So Central Park, Prospect Park and Flushing Meadows were not green on it, and no
paint override can put back a layer that was never in the style. positron is a
deliberately monochrome canvas style; it is the right basemap for a choropleth and
the wrong one for a general-purpose map.

This was measured with `scripts/inspect_style.py`, which dumps a style's layer ids: compare
`positron` and `liberty` before doubting it. The 30daymapchallenge repo's harness asserted
the three parks render green; webxp's does not, so this one *can* regress quietly and is
listed under "Not checked" below.

### The satnav palette

`simple` is liberty with a paint-only override, `SATNAV` in `shared/map/basemap.mjs`: flat
olive land, deep navy water, arterials in a yellow that carries at arm's length. Every
key in it is a layer id measured out of liberty with `scripts/inspect_style.py` —
positron's ids are different, which is why a palette written against positron silently
did nothing here.

This took three passes, and the two failures are worth recording:

1. **Repainting positron, with its roads stripped.** Lost every park in the city, because
   positron has no park colour to override, and lost the look it was imitating, because a
   GPS screen was mostly roads.
2. **Muting the palette** to keep a data overlay readable. The verdict was that it had
   gone pale (2026-11-04). These screens were read at a glance in daylight through a
   windscreen; nothing on them was subtle.

So the palette is saturated, and no layer is removed. A day whose data needs a quiet
backdrop should pick `geography`, or empty `SATNAV`.

Navy water broke liberty's own water labels — `#495e91` on `#1b4b8f` measures about
1.5:1 — so `water_name_*` and `waterway_line_label` are re-coloured to light text on a
dark halo, about 12:1. A palette change that breaks a label is not finished.

## Licensing

Two of the three need reading before this goes anywhere public.

- **OpenFreeMap** publishes no SLA and asks for no key. It is free for any use with
  attribution. Because there is no SLA, `basemap.mjs` carries a `FALLBACK_STYLE`: when
  the style request fails the day's overlay still draws, on a flat background, and the
  status line says so.
- **Esri ArcGIS Online** is a commercial service. The keyless tile endpoints are widely
  used and ship as presets in Leaflet and MapLibre provider lists, but Esri's terms
  govern ArcGIS Online basemaps and generally contemplate an account for application
  use ([master agreement](https://www.esri.com/en-us/legal/terms/full-master-agreement)).
  **Nobody here has confirmed this use is licensed.** It is documented rather than
  assumed. Attribution to Esri is required and is set on the source and in the credit.
- **OpenTopoMap** is free for non-commercial use with attribution, under a tile usage
  policy that forbids bulk downloading
  ([usage](https://opentopomap.org/about#verwendung)). Panning a map is not bulk
  downloading; a scripted sweep of a city at zoom 17 would be.

Attribution is rendered by `basemap.mjs` into `.map-attrib`, not by MapLibre's own
control, and `.map-attrib` lives in the map's **status bar** — a row of `.map-root`'s
column and a sibling of `.map-area`, rather than a layer over it. `chrome.mjs` carries why,
and what happened the once it was a layer.

That placement is the whole point. It used to float over the canvas at `bottom: 0;
right: 0; z-index: 4`, which is exactly where the detail panel docks at `z-index: 5`:
the credit disappeared the moment anything was selected, on every viewport, and nothing
caught it for a day. A status bar cannot be covered at any size and has no stacking order
to get wrong — and an XP window had one anyway.

`scripts/check.py` asserts it with `elementFromPoint` at the midpoint of each pane: once in
`basemap app` with the panel shut, and in `day 02 lines` at three window sizes with the
panel open and carrying its longest footer. That question — "what would the reader's cursor
actually hit here?" — is the one a `visibility` or `z-index` comparison does not answer.

It is also asserted *geometrically*: the credit must start below `.map-area`. The positional
check alone is not enough, and the gap is instructive. Floating the credit back into the
corner passed `elementFromPoint`, because the base map app docks nothing over it — the bug
survived the check written for it. An assertion that only holds for the app that happens to
be open is not an assertion about the invariant.

## Writing a map app

The thirty day folders already exist, each with a placeholder `app.mjs`. To make one a map,
open `apps/day-NN-theme/app.mjs` and follow `apps/day-02-lines/` — the only one built out.
`docs/apps.md` is the app contract; two parts of it bite here.

**Build the chrome in `mount`, the map in `ready`.** MapLibre measures its container on
creation, and during `mount` nothing has a box yet — what `mount` returns is what gets
inserted. A map built there sizes itself to 0×0, reports its style loaded, fetches every tile
and draws nothing.

```js
styles: ['../../shared/map/map.css', 'app.css'],

async mount({ win }) {
  const [{ buildMapChrome }, { BASEMAPS }] = await Promise.all([
    import('../../shared/map/chrome.mjs'),
    import('../../shared/map/basemap.mjs'),
  ]);
  const chrome = buildMapChrome({ basemaps: BASEMAPS, hint: 'Click something.' });
  chrome.cards.append(myCard);
  win.app = { chrome };
  return chrome.root;
},

async ready({ win }) {
  const { createBaseMap } = await import('../../shared/map/basemap.mjs');
  const base = await createBaseMap({ root: win.app.chrome.root, center, zoom, draw });
  win.base = base;
  win.onClose = () => base.destroy();   // the WebGL context, or it leaks
},
```

**Import inside the hook, never at module scope.** Every `app.mjs` is read at startup so the
Start menu can be built, so a module-scope import of this directory makes MapLibre's 1.1 MB
startup cost for a desktop where nobody opened a map.

Pass `tilt: false` for a day that must stay flat and north-up: it removes the Tilt button and
the compass rather than leaving them inert. Pass `detail: false` if nothing is ever selected.
A day wanting a quiet backdrop uses `geography` — it does not mute the palette.

### The drill-down panel

`chrome.mjs` builds the element and `panel.mjs` drives it: `new Panel(chrome.detail)`, then
`panel.open(title, content, footer)` and `panel.close()`.

Set `panel.onClose` to undo whatever the panel was describing. **It may call `panel.close()`**
— the obvious implementation is the app's own clear-everything routine, and closing the panel
is part of clearing. `close()` guards against the re-entry rather than requiring every caller
to think about it. Without that guard the cycle threw `RangeError: Maximum call stack size
exceeded` out of MapLibre's `setPaintProperty`, which named neither the panel nor the
recursion; `hidden` cannot break the cycle because it is only set when the 220 ms slide ends.

Clear the map state unconditionally in `onClose`, not only when something is selected. A
panel can be opened for a feature without a selection, and `if (selected)` left a highlight
and an overlay painted with nothing on screen explaining them.

### `draw` is called once per basemap, not once per page

`map.setStyle()` discards every source and layer added to the previous style. So
`draw(map, ctx)` is called again after each basemap change, always against a fresh and
empty style — it may add its sources unconditionally and needs no "already added?"
guard.

Anything that has to survive a switch — a selection, a filter, a clicked point — lives
in a variable outside `draw` and is read back in. `apps/basemap/app.mjs` does exactly this
with its clicked point; `apps/day-02-lines/lines.mjs` does it with the selected run, the
tolerance and the segment overlay.

`ctx.beforeId` is the insertion point that keeps place labels above the overlay.
`ctx.basemap` is the active preset's name, for a day that wants to paint differently
over imagery — the base map's own click ring switches its outline to white on
`satellite`, where a dark line disappears.

### The one subtlety worth knowing

`styledata` is not a reliable "the new style is ready" signal. It fires repeatedly as a
style's sprites, glyphs and tiles arrive, and the *previous* style keeps firing it for a
while after `setStyle` has been called. A counter bumped just before `setStyle` gets
consumed by a late event belonging to the old style, and the real event for the new one
is then skipped — which left the satellite basemap with no overlay at all and no error
to show for it. `createBaseMap` therefore stamps every style it installs with a serial in
the style's `name` and compares, which identifies the style rather than the moment.

## Checking it

```
.venv/bin/python scripts/check.py
```

Drives the Chrome already on the machine over the DevTools Protocol, screenshots each check
into `screenshots/`, and fails on a console error. Two of its thirty-three checks are about
the map. `basemap app` asserts:

- the canvas fills `.map-area` — a collapsed container fetches every tile, reports the style
  loaded and draws nothing, which is what MapLibre's own `position: relative` did until this
  stylesheet started winning on specificity;
- the canvas has *painted*, by compressing a screenshot of it: every flag MapLibre exposes
  read healthy over a blank canvas;
- the satellite button really switches the basemap;
- the credit is visible, nothing is on top of it, and it starts below the map area — a
  credit over the canvas passes "nothing covers it" today and is covered by the first panel
  that docks there.

`day 02 lines` asserts the app on top of it: that it loaded data and drew its own layers *by
id* on whatever basemap was remembered, that simplifying a trace shortens it, and that both
panes of the status bar are below the map area with nothing over them.

Every assertion has been shown capable of failing by breaking the thing it names — the
credit floated back over the canvas, the container selector dropped to two classes, the
status line went back inside the scrolling column. `scripts/check.py --only <substring>` runs
one check, which is how that is done.

`scripts/inspect_style.py` dumps a basemap style's layer ids, which is how the palette's keys
were measured rather than guessed.

### Not checked

Written down because an unlisted gap reads as coverage, and "is that covered?" answered with
a confident yes is worse than answered with no.

- **The parks.** Nothing asserts Central Park, Prospect Park and Flushing Meadows render
  green space, so the `positron` finding above can regress quietly. Re-measure with
  `inspect_style.py`.
- **The 3D buildings and the compass.** The pitch gate (`building-3d` showing at ≥12° and
  hiding below 8°) and the rose's `-bearing` rotation are both load-bearing and both
  unasserted here. A needle rotated the wrong way looks plausible and points at the wrong
  quarter of the sky.
- **The JS/Python geometry pair.** `apps/day-02-lines/geo.mjs` and `tools/gpsnoise.py`
  implement the same Douglas-Peucker and the same local-plane length, and they are supposed
  to agree to the millimetre. That agreement has been measured by hand (≤0.5 mm across all
  twelve rungs, identical kept-point counts) but no check enforces it, so a divergence would
  be found by noticing a wrong number on screen.
- **A clean clone's segment feature.** `apps/day-02-lines/data/` carries no
  `street-noise.json`, so on a fresh checkout the choropleth, its legend, the drill-down and
  the ghost worm never execute. `day 02 lines` says so rather than passing silently, but the
  code path is unexercised in CI terms.
- **An app's own stylesheet.** `app styles` only tests Notepad, so a scoping mistake in any
  other app's CSS is not caught.

A harness that cannot reach what it was pointed at must fail rather than pass. Two ways it
has not: `http.server` exits immediately when its port is taken, the readiness probe then
succeeds against whatever *was* listening, and the run silently measures another checkout —
a server left over from a second worktree made a real fix look broken and a reintroduced bug
look caught, in one session. And `http.server` validates with `Last-Modified` at one-second
granularity, so a manifest rewritten inside the same second answers 304 and the page keeps
the previous module; `scripts/inspect_manifest_cache.py` forces that window and reports what
the page saw. The
probe binds with `SO_REUSEADDR`, which clears a harmless `TIME_WAIT` socket from the
previous run but still refuses a live listener.

A harness MUST fail when it cannot test the thing it was pointed at. Silence there costs
more than the bug it was hiding.

`scripts/cdp.py` is copied from the author's Show Up NYC project. It drives the system
Chrome over the DevTools Protocol, because a check harness that installs its own browser
is a bigger dependency than the site it checks. Its one requirement:

```
python3 -m venv .venv && .venv/bin/pip install websocket-client
```

Nothing the site itself serves needs Python, a venv, or a build step.
