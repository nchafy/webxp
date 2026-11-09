/* Day 02 — Lines.
 *
 * Every run is drawn twice: as the watch recorded it, and snapped to the OSM network. The
 * recording is longer, because receiver jitter is indistinguishable from movement and
 * gets counted as distance covered.
 *
 * The slider is the argument. It simplifies one recorded trace at a tolerance the reader
 * chooses, and the distance falls as the wobble comes out. There is no setting at which
 * the number is right — that is the point. A run has no single length, only a length at a
 * stated resolution, which is the coastline paradox
 * (https://doi.org/10.1126/science.156.3775.636) happening on a 10 km run.
 *
 * ## Why there are two resolutions
 *
 * A real history is 400-odd activities and over a million points. Handing a browser all
 * of it is 35 MB, and re-simplifying all of it per frame is seconds per frame. So:
 *
 * - `overview.json` holds every run simplified to a stated floor. It draws the map, and
 *   the floor is printed in the interface rather than hidden.
 * - A single run's full-resolution document is fetched when the reader selects it. That is
 *   the only geometry the slider ever touches, so the slider is exact and continuous.
 * - The aggregate distances come from `index.json`, which `prepare.py` computed at full
 *   resolution. The headline figures are never the simplified ones.
 *
 * `draw` is re-run against a fresh style on every basemap change, so it adds its sources
 * unconditionally. The tolerance and the selection live outside it and are read back in.
 */

import { createBaseMap } from '../../shared/map/basemap.mjs';
import { Panel } from '../../shared/map/panel.mjs';
import { group } from '../../src/widgets.mjs';
import { project, simplifyMask, simplifiedCoords, planeLength, ladder } from './geo.mjs';


//: Every run, simplified. The map's backdrop.
const ALL = 'all';
//: The selected run, at full resolution, re-simplified live by the slider.
const FOCUS = 'focus';
//: Every run snapped to the network.
const MATCHED = 'matched';
//: A wide transparent line carrying the clicks. The drawn trace is 2 px, which is right
//: for a jittery line and wrong as a target: MapLibre hit-tests the rendered geometry, so
//: a 2 px line asks for pixel-exact aim.
const HIT = 'all-hit';

//: A hot magenta and a cyan, chosen against the satnav palette rather than from it. The
//: base map's own tokens are olive, navy and yellow, and an overlay in any of them reads
//: as part of the road network. Both carry over imagery, where the era's navy vanishes.
const INK_RECORDED = '#ff2e88';
const INK_MATCHED = '#39d0ff';
const CASING = 'rgba(8, 14, 32, 0.75)';

//: Line widths, in one place because they only work as a set. The snapped line has to be
//: wider than the recorded line's casing, not merely under it: at 3 px beneath a 2 px
//: trace with a 5 px casing it was covered completely, and the key named a cyan line that
//: was nowhere on the map.
const WIDTH = {
  focusCasing: 4,
  focus: 2,
  hit: 16,
};

/* Widths that follow the zoom, for the layers that draw every run at once.
 *
 * A fixed 8 px snapped line is right when you are looking at one run and far too heavy
 * when four hundred of them overlap: at city zoom the whole history merges into one cyan
 * mass and the recorded traces on top of it vanish. Interpolating keeps the overview
 * readable without making a single selected run look like a hairline.
 *
 * The stops are zoom 9 (a metropolitan area, where runs overlap) to zoom 15 (a few
 * streets, where they do not). */
function byZoom(wide, narrow) {
  return ['interpolate', ['linear'], ['zoom'], 9, narrow, 15, wide];
}

const ZOOM_WIDTH = {
  matchedCasing: byZoom(11, 2.4),
  matched: byZoom(8, 1.2),
  allCasing: byZoom(3, 1.4),
  all: byZoom(1.4, 0.7),
};

//: Opacity of a run that is not the selected one. One constant, because `draw` and the
//: click handler both build the expression and two numbers meant that switching basemap
//: with a run selected un-dimmed the rest.
const DIM = 0.3;
const FULL = 1;

/* ---------------------------------------------------------------------------
   The noise-by-area choropleth.
   --------------------------------------------------------------------------- */

const NOISE = 'noise';

/* Two surfaces, at two scales, and the scrim is the one that needs placing carefully.
 *
 * Each coloured line gets its own casing, which gives that mark a known surface without
 * touching anything else's stacking. That is the local half and the rest of this file works
 * the same way.
 *
 * The scrim is the global half: it dims the whole basemap while the segments are up, so an
 * olive-and-yellow satnav palette stops competing with a diverging ramp. It has to go in
 * *above* every basemap layer, not at `ctx.beforeId`. `beforeId` is chosen so place labels
 * stay above the data, which is right for a line and wrong for a scrim — added there it
 * left fifty basemap layers on top of it, and bridges, one-way arrows and every label
 * painted at full brightness over a dimmed map. */
const NOISE_CASING_INK = '#14161a';
const NOISE_SCRIM = 'noise-scrim';

//: Dims the basemap while the segments are up. Moderate on purpose: heavy enough that an
//: olive-and-yellow satnav basemap stops competing with a diverging ramp, light enough
//: that street names read through it.
const SCRIM = 'rgba(20, 22, 26, 0.62)';

/* Diverging, blue to red, neutral grey midpoint. Signed data gets a diverging ramp, and a
 * diverging ramp gets a grey middle rather than a hue.
 *
 * Every value measured against the casing colour with the palette validator:
 *
 *   each arm as an ordinal ramp      monotone L, step gaps >= 0.06, PASS
 *   innermost steps vs the casing    4.10:1 blue, 4.12:1 red
 *   across the midpoint, CVD         worst pair dE 8.6 deutan, 10.5 tritan (>= 8 target)
 *   across the midpoint, normal      worst pair dE 17.4 (>= 15 floor)
 *   all three vs the casing          all clear 3:1
 *
 * The midpoint is #7f7f77 and not the palette's darker #383835, which is a deliberate
 * departure with a measurement behind it. A dark neutral is right for an area fill, where
 * "unremarkable" should recede toward the surface. On a thin line it recedes all the way to
 * invisible, and an invisible segment is indistinguishable from a segment with too few
 * passes to report -- a different fact. Lightening it costs separation from the red arm, so
 * it was walked up until the CVD gate passed again and no further: #5c5c56 and #6e6e67 both
 * fail at dE 5.8 and 5.7, and #7f7f77 clears at 8.6.
 *
 * That makes the midpoint about as light as the innermost steps, so colour alone no longer
 * says "near zero is unremarkable". `NOISE_WIDTHS` says it instead: thin at the middle,
 * thickening outward. Two channels for one quantity, which also means the map reads before
 * anyone finds the legend. */
const NOISE_RAMP = ['#9ec5f4', '#5598e7', '#2a78d6', '#7f7f77', '#d6453f', '#ef6a5e', '#f9a79c'];

//: Width per class, symmetric about the middle. The second channel: a near-zero segment is
//: a hairline and an extreme one is heavy, so salience tracks the measurement even in
//: greyscale or for a reader who cannot separate the two hues.
const NOISE_WIDTHS = [4.5, 3.5, 2.5, 1.3, 2.5, 3.5, 4.5];

const NOISE_BREAKS = [-1.0, -0.5, -0.2, 0.2, 0.5, 1.0];

const PROP = 'adjusted_pts';
//: The one crossing being shown, drawn over its own run.
const PASS = 'pass';

/* Every crossing of the open segment, overlaid. The worm.
 *
 * This is the point of the whole drill-down, and it needs no number to read. The same
 * street, run thirty times, traced thirty times: where the ghosts lie on top of each
 * other the receiver agreed with itself, and where they fan apart it did not. The width
 * of the bundle *is* the noise being measured everywhere else on this page as a
 * percentage.
 *
 * It is also the era's own failure mode. A Windows window dragged faster than the
 * compositor could repaint left a smear of copies behind it, and that is what a noisy
 * segment looks like here. The real artifact usually left one trailing shadow rather than
 * a dense bundle, which is the argument for `GHOST_CAP`: past about a dozen the overlay
 * stops being a smear and becomes a blob. */
const GHOSTS = 'ghosts';

//: How many crossings the worm draws. A cap, not a sample of convenience: the panel still
//: lists the extremes, and the ghosts are there to be *looked* at.
const GHOST_CAP = 12;

/**
 * `cap` items spread evenly across `items`, both ends included.
 *
 * The worm exists to show the *spread* of a street's crossings — the panel says so: "the
 * width of that bundle is the noise". `passes` arrives sorted noisiest-first
 * (`street_segments.py` writes `sorted(..., key=-adjusted_pts)`), so taking the first
 * `GHOST_CAP` drew the worst twelve and the bundle's width was the width of the tail.
 *
 * Measured on the 401-activity export: 193 of 395 streets carry more than twelve crossings,
 * and on the worst (197 crossings) the drawn twelve averaged +20.76 adjusted points against
 * +4.84 across all of them — the overlay overstated that street's noise by 4.3x, which is
 * the one number on the page a reader is invited to judge by eye.
 *
 * Evenly spaced over the sorted list is a quantile sample: it keeps both extremes and the
 * middle, so the bundle spans what the street actually did. Deterministic, so the same
 * segment draws the same worm twice.
 */
function evenSample(items, cap) {
  if (items.length <= cap) return items;
  const step = (items.length - 1) / (cap - 1);
  const out = [];
  for (let i = 0; i < cap; i += 1) out.push(items[Math.round(i * step)]);
  return out;
}

//: Low enough that one ghost is a hint and a dozen stacked is solid, so the overlay
//: encodes how many passes agreed as well as where they did.
const GHOST_INK = 'rgba(255, 255, 255, 0.3)';

/* The step expressions MapLibre wants, both built from the same breaks, so the colour, the
 * width and the legend cannot disagree about where a class starts.
 *
 * @param {any[]} values  One per class: `NOISE_RAMP` or `NOISE_WIDTHS`.
 */
function noiseStepExpression(values) {
  const stops = [];
  NOISE_BREAKS.forEach((edge, i) => stops.push(edge, values[i + 1]));
  return ['step', ['get', PROP], values[0], ...stops];
}

//: The casing is the line plus a ring either side, so every segment keeps its own surface
//: whatever the basemap is doing underneath. Two pixels, per the mark spec.
function noiseCasingExpression() {
  return noiseStepExpression(NOISE_WIDTHS.map((w) => w + 2));
}

/** Legend rows, in the same order the map paints them. */
function noiseLegendRows() {
  const fmt = (v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}`;
  return NOISE_RAMP.map((colour, i) => {
    const lo = NOISE_BREAKS[i - 1];
    const hi = NOISE_BREAKS[i];
    let label;
    if (lo === undefined) label = `below ${fmt(hi)}`;
    else if (hi === undefined) label = `above ${fmt(lo)}`;
    else label = `${fmt(lo)} to ${fmt(hi)}`;
    return { colour, width: NOISE_WIDTHS[i], label };
  });
}

//: Mirrors `gpsnoise.TOLERANCES_M`. Only used to cross-check this module's arithmetic
//: against the ladder `prepare.py` wrote; the slider is continuous and uses neither.
const LADDER_TOLERANCES = [0, 0.5, 1, 2, 3, 5, 8, 12, 20, 35, 60, 100];

/* Where the day opens. A stated choice, not a derived one.
 *
 * It used to fit the camera to `overview.home`, the densest cluster `build_overview.py`
 * computes — and that opened on empty hillside. The box spans a degree of longitude,
 * reaching from a city to mountains an hour east of it, so its midpoint is in neither: the
 * runs were two tight knots at opposite ends of a frame centred on the gap between them.
 *
 * A history with runs in several places has no single correct opening view, so deriving one
 * produces a confident wrong answer. New York is where this project's committed synthetic
 * data is, where its basemap and park checks point, and where the second-largest cluster of
 * real runs sits — so it is never an empty map. To open somewhere else, change this.
 */
const OPEN_AT = { center: [-73.95, 40.7], zoom: 11 };

//: Where the data might be, most private first. `private/` is what `tools/prepare.py` writes
//: by default and is gitignored; `data/` holds the committed synthetic fixture, so this day
//: renders for someone with no Strava export.
//:
//: Resolved against this module's own URL, not written as a path from the site root. The
//: whole desktop is served from `/` and an app's folder is not the document's directory, so
//: `./data` resolves against the page and 404s silently — which leaves the day reporting no
//: activities rather than reporting a broken path. `import.meta.url` is the only address
//: that stays correct if the app folder is ever renamed or the desktop is served from a
//: sub-path.
const DATA_DIRS = ['private', 'data'].map(
  (dir) => new URL(`${dir}/`, import.meta.url).href,
);


/* ---------------------------------------------------------------------------
   The day's own controls, built.
   --------------------------------------------------------------------------- */

/**
 * Build this day's three cards into the slot `lib/basemap.mjs` left for them.
 *
 * Built rather than written as markup for the same reason the rest of the shell is: a
 * window is created at runtime, so there is no page to put the markup in. It also stops
 * the ids being global — two map windows would have fought over `#tolerance`.
 *
 * @param {HTMLElement} slot    `chrome.cards`.
 * @param {number} instance     `chrome.instance`, for the one id `label[for]` needs.
 */
function buildCards(slot, instance) {
  const el = (tag, className, parent) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (parent) parent.append(node);
    return node;
  };
  const heading = (card, text) => {
    const h = el('h2', null, card);
    h.textContent = text;
    return h;
  };

  // Every run.
  const aggCard = el('div', 'card', slot);
  heading(aggCard, 'Every run');
  const aggregateEl = el('p', 'readout', aggCard);
  aggregateEl.dataset.role = 'aggregate';

  // Noise by place.
  const noiseCard = el('div', 'card', slot);
  heading(noiseCard, 'Noise by place');
  const notePara = el('p', 'readout', noiseCard);
  const noiseNote = el('span', 'sub', notePara);
  noiseNote.dataset.role = 'noise-note';
  const noiseRow = el('div', 'tilt-row', noiseCard);
  const noiseToggle = el('button', null, noiseRow);
  noiseToggle.type = 'button';
  noiseToggle.setAttribute('aria-pressed', 'false');
  noiseToggle.textContent = 'Show segments';
  noiseToggle.dataset.role = 'noise-toggle';
  const legendEl = el('div', 'key noise-legend', noiseCard);
  legendEl.hidden = true;
  legendEl.dataset.role = 'noise-legend';
  noiseToggle.setAttribute('aria-describedby', (legendEl.id = `noise-legend-${instance}`));

  // Measuring stick.
  const stickCard = el('div', 'card', slot);
  heading(stickCard, 'Measuring stick');
  const bar = el('div', 'trackbar', stickCard);
  const label = el('label', null, bar);
  label.textContent = 'Detail';
  const slider = el('input', null, bar);
  slider.type = 'range';
  slider.min = '0';
  slider.max = '100';
  slider.step = '0.5';
  slider.value = '0';
  slider.disabled = true;
  slider.dataset.role = 'tolerance';
  slider.id = `tolerance-${instance}`;
  label.setAttribute('for', slider.id);
  const sliderOut = el('output', null, bar);
  sliderOut.setAttribute('for', slider.id);
  sliderOut.dataset.role = 'tolerance-out';
  sliderOut.textContent = '0 m';
  const readoutEl = el('p', 'readout', stickCard);
  readoutEl.dataset.role = 'readout';
  // After the readout exists, not before: `const` is not hoisted, and describing the
  // slider by an element declared further down throws on the first mount.
  readoutEl.id = `readout-${instance}`;
  slider.setAttribute('aria-describedby', readoutEl.id);

  const key = el('div', 'key', stickCard);
  const keyRow = (colour, text) => {
    const row = el('div', null, key);
    const swatch = el('i', null, row);
    swatch.style.borderTopColor = colour;
    const span = el('span', null, row);
    span.textContent = text;
    return span;
  };
  const keyRecordedEl = keyRow(INK_RECORDED, 'as recorded');
  keyRecordedEl.dataset.role = 'key-recorded';
  keyRow(INK_MATCHED, 'snapped to the map');

  const buttons = el('div', 'tilt-row', stickCard);
  const matchedToggle = el('button', null, buttons);
  matchedToggle.type = 'button';
  matchedToggle.setAttribute('aria-pressed', 'true');
  matchedToggle.textContent = 'Snapped line';
  matchedToggle.dataset.role = 'toggle-matched';
  const clearButton = el('button', null, buttons);
  clearButton.type = 'button';
  clearButton.textContent = 'Deselect';
  clearButton.dataset.role = 'clear-selection';
  clearButton.hidden = true;

  return {
    aggregateEl,
    noiseNote,
    noiseToggle,
    legendEl,
    slider,
    sliderOut,
    readoutEl,
    keyRecordedEl,
    matchedToggle,
    clearButton,
  };
}

/**
 * Build the day, into a map chrome that is already in the page.
 *
 * Called from `ready`, not from `mount`. MapLibre measures its container on creation, and
 * during `mount` the nodes have no box yet — the map would size itself to 0x0, report its
 * style loaded, fetch every tile and draw nothing.
 *
 * @param {object} args
 * @param {object} args.win     The `XPWindow`.
 * @param {object} args.chrome  What `buildMapChrome` returned, already mounted.
 */
export async function startDay({ win, chrome }) {
  {
    //: The detail panel, built from the shell `createBaseMap` assembled. Per window.
    let panel = null;

    //: Handles to this day's own controls, assigned once `createBaseMap` has built the slot
    //: they go in. `let` rather than `const` because every function below closes over them
    //: and runs long after the cards exist.
    let statusEl = null;
    let slider = null;
    let sliderOut = null;
    let readoutEl = null;
    let matchedToggle = null;
    let keyRecordedEl = null;
    let aggregateEl = null;
    let clearButton = null;
    let noiseToggle = null;
    let legendEl = null;
    let noiseNote = null;

    //: State that has to survive a basemap switch.

    let tolerance = 0;
    let showMatched = true;

    let dataDir = null;
    let summary = null;
    let overview = null;
    //: The noise grid, or null when `noise_by_area.py` has not been run. Optional throughout:
    //: the page is useful without it and must not fail for its absence.
    let noise = null;
    let showNoise = false;
    //: The segment being drilled into, by cell id.
    let selectedCell = null;
    //: One crossing of one segment by one run: `{activity, i0, i1, ...}`. The drill-down's
    //: leaf, and what `PASS` draws.
    let passSpan = null;
    /** @type {{id: string, doc: object, projected: object}|null} */
    let focus = null;

    const docCache = new Map();

    //: `base.say` owns the status region -- it is what `createBaseMap` uses for its own
    //: messages, and writing `statusEl.textContent` beside it was a second renderer of one
    //: line.
    //:
    //: Writes straight to the element until the map exists. `base` is a `const` declared
    //: further down, so an `if (base)` here before that line throws `ReferenceError` on the
    //: temporal dead zone rather than falling through -- which made the fallback branch it
    //: was written for unreachable twice over, since `statusEl` was also still null.
    //: `chrome.status` is in scope from the first line and is the same element.
    function setStatus(text) {
      try {
        if (base) {
          base.say(text);
          return;
        }
      } catch {
        // Not yet initialised; fall through to the element.
      }
      if (chrome.status) chrome.status.textContent = text;
    }

    function fmtKm(metres) {
      return `${(metres / 1000).toFixed(2)} km`;
    }

    function fmtBigKm(metres) {
      return `${Math.round(metres / 1000).toLocaleString()} km`;
    }

    /* ---------------------------------------------------------------------------
       Loading.
       --------------------------------------------------------------------------- */

    async function fetchFirst(name) {
      const tried = [];
      // Each entry of `DATA_DIRS` already ends in a slash, so `new URL` treats it as a
      // directory. Without the slash the last segment is replaced instead of appended.
      for (const dir of dataDir ? [dataDir] : DATA_DIRS) {
        const url = new URL(name, dir).href;
        try {
          const response = await fetch(url);
          if (!response.ok) {
            tried.push(`${url}: HTTP ${response.status}`);
            continue;
          }
          dataDir = dir;
          return await response.json();
        } catch (err) {
          tried.push(`${url}: ${err.message}`);
        }
      }
      // Names every path tried. "No data" without saying where it looked costs an afternoon.
      throw new Error(tried.join('; '));
    }

    async function loadFocus(id) {
      if (docCache.has(id)) return docCache.get(id);
      const doc = await fetchFirst(`${id}.json`);
      if (!Array.isArray(doc.recorded) || doc.recorded.length < 2) {
        throw new Error(`${id}: no usable geometry`);
      }
      const entry = { id, doc, projected: project(doc.recorded) };
      docCache.set(id, entry);
      return entry;
    }

    /* ---------------------------------------------------------------------------
       Geometry handed to MapLibre.
       --------------------------------------------------------------------------- */

    const EMPTY = { type: 'FeatureCollection', features: [] };

    function allData() {
      return overview ? overview.recorded : EMPTY;
    }

    function matchedGeometry() {
      return overview ? overview.matched : EMPTY;
    }

    /* The one crossing being shown, as a line over its own run's full-resolution geometry.
     *
     * Needs the run loaded, which `showPass` guarantees before setting `passSpan`. Returns
     * empty rather than throwing when it is not, because `draw` runs on every basemap change
     * and must not care what order things happened in. */
    //: The crossings currently drawn as ghosts: `[{activity, i0, i1}]`, already loaded.
    let ghostSpans = [];

    /* The worm: one line per loaded crossing of the open segment. */
    function ghostData() {
      const features = [];
      for (const span of ghostSpans) {
        const entry = docCache.get(span.activity);
        if (!entry) continue;
        const coords = entry.doc.recorded.slice(span.i0, span.i1 + 1);
        if (coords.length < 2) continue;
        features.push({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: coords },
          properties: { activity: span.activity },
        });
      }
      return { type: 'FeatureCollection', features };
    }

    //: How far the traces recede when the choropleth is the subject.
    //:
    //: With the grid on, four hundred magenta and cyan lines sit on top of the cells they are
    //: supposed to be explaining, and the cells lose. Figure and ground swap when the layer
    //: does: the traces stay as context, because a cell with no road under it is a square in
    //: the dark, but they stop competing.
    const TRACE_UNDER_NOISE = 0.22;

    /** Set the whole-history traces' weight according to what the reader is looking at. */
    function applyTraceEmphasis(map) {
      const base = showNoise ? TRACE_UNDER_NOISE : FULL;
      for (const id of [ALL, `${ALL}-casing`]) {
        if (!map.getLayer(id)) continue;
        map.setPaintProperty(
          id,
          'line-opacity',
          focus ? ['case', ['==', ['get', 'id'], focus.id], 0, showNoise ? TRACE_UNDER_NOISE : DIM] : base
        );
      }
      for (const id of [MATCHED, `${MATCHED}-casing`]) {
        if (map.getLayer(id)) map.setPaintProperty(id, 'line-opacity', base);
      }
    }

    /* Put the camera where the cells are, clear of the controls.
     *
     * Turning the layer on used to leave it off screen: the grid only covers the places with
     * enough repeat passes to measure, which is a fraction of the extent the page opens at,
     * and the little that did show sat behind the control cards. A layer you switch on and
     * cannot see reads as a broken layer.
     *
     * The left padding is measured off the overlay column rather than guessed, so it stays
     * right at both viewports and after a card changes size. */
    function goToCells(map) {
      if (!noise || !noise.features.length) return;

      // The segments where the reader is already looking, not a fixed cluster.
      //
      // Fitting *all* of them spans every city the runner has ever visited — here 262 degrees
      // of longitude — so the camera goes continental and every segment is sub-pixel. The
      // previous rule was `overview.home`, which meant pressing this in New York flew to
      // Seattle. Asking the map what is on screen keeps the press local, and it needs no
      // stored idea of where home is.
      //
      // LineString, so `coordinates` is already the list of positions. It was `[[ring]]` when
      // these were squares, and the leftover `[0]` indexing fed bare numbers to a destructure
      // — "number is not iterable", thrown once per press.
      const view = map.getBounds();
      const inView = noise.features.filter((f) => {
        const [lon, lat] = f.geometry.coordinates[0];
        return view.contains([lon, lat]);
      });
      // Nothing here: show everything rather than nothing, so the reader learns the data is
      // somewhere else instead of watching the button do nothing.
      const chosen = inView.length ? inView : noise.features;

      const box = boundsOfCoords(chosen.flatMap((f) => f.geometry.coordinates));
      if (!box) return;
      // This window's own control column, not the first one in the document.
      //
      // It was `document.querySelector('.overlays > *')`, which is what `docs/apps.md`
      // forbids by name: with the Base map window also open it resolved inside *that*
      // window and returned its basemap card. Measured — open Base map, narrow it to
      // 260 px, then open this day: the gutter came out 250 where this window's own card
      // is 264, so the camera was padded by the wrong amount and the cells sat behind the
      // cards. It kept resolving into the other window even after that one was minimised.
      const column = chrome.overlays.firstElementChild;
      const gutter = column ? Math.ceil(column.getBoundingClientRect().width) + 24 : 24;
      const canvas = map.getCanvas();
      // On a narrow viewport the cards take most of the width, so a left gutter would leave
      // nothing to draw in; there, pad evenly and accept the overlap.
      const roomy = canvas.clientWidth > gutter * 2.2;
      map.fitBounds(box, {
        padding: roomy
          ? { left: gutter, right: 24, top: 24, bottom: 48 }
          : { left: 12, right: 12, top: 12, bottom: 48 },
        duration: 600,
        maxZoom: 15,
      });
    }

    /* Load the runs the worm needs and draw it.
     *
     * Geometry comes from each run's own full-resolution document rather than being copied
     * into the grid file. The grid would have had to carry a few thousand short line strings,
     * and they would be a second copy of coordinates that already exist -- fine until the grid
     * is rebuilt against changed data and the two disagree. `docCache` makes a revisit free. */
    async function drawGhosts(map, passes) {
      const wanted = evenSample(passes, GHOST_CAP);
      const results = await Promise.allSettled(wanted.map((p) => loadFocus(p.activity)));
      ghostSpans = wanted.filter((_p, i) => results[i].status === 'fulfilled');
      const source = map.getSource(GHOSTS);
      if (source) source.setData(ghostData());
      const failed = results.length - ghostSpans.length;
      if (failed) {
        // Named rather than swallowed: a worm quietly missing half its ghosts would read as a
        // quiet segment, which is the opposite of the truth.
        setStatus(`${failed} of ${results.length} crossings could not be loaded for the overlay.`);
      }
      return ghostSpans.length;
    }

    function passData() {
      if (!passSpan || !focus || focus.id !== passSpan.activity) return EMPTY;
      const coords = focus.doc.recorded.slice(passSpan.i0, passSpan.i1 + 1);
      if (coords.length < 2) return EMPTY;
      return {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            geometry: { type: 'LineString', coordinates: coords },
            properties: { activity: passSpan.activity },
          },
        ],
      };
    }

    /* The selected run at the current tolerance. One pass produces the geometry and the
     * length, because the simplification that makes the line is also what makes the number:
     * computing them separately is two chances to disagree. */
    function focusData() {
      if (!focus) return { fc: EMPTY, length: 0, kept: 0, total: 0 };
      const keep = simplifyMask(focus.projected, tolerance);
      const coords = simplifiedCoords(focus.doc.recorded, keep);
      let kept = 0;
      for (let i = 0; i < keep.length; i += 1) kept += keep[i];
      return {
        fc: coords
          ? {
              type: 'FeatureCollection',
              features: [
                {
                  type: 'Feature',
                  geometry: { type: 'LineString', coordinates: coords },
                  properties: { id: focus.id, name: focus.doc.name },
                },
              ],
            }
          : EMPTY,
        length: planeLength(focus.projected, keep),
        kept,
        total: keep.length,
      };
    }

    /* ---------------------------------------------------------------------------
       The two readouts.
       --------------------------------------------------------------------------- */

    function aggregate() {
      const rows = (summary && summary.activities) || [];
      let recorded = 0;
      let matched = 0;
      //: The recorded total over *only* the rows that also matched, which is the one the
      //: percentage is allowed to use. Dividing the all-rows total by the matched total
      //: compares different sets of activities: with 9 of 10 runs snapping, 9 km recorded
      //: against 8.5 km matched plus one unmatched 10 km run, the card read "+123.5%" where
      //: the truth was +5.9%. Both totals are still shown in full, because the headline is
      //: "how far did I record", not "how far of it could be matched".
      let recordedWhereMatched = 0;
      let strava = 0;
      let matchedCount = 0;
      let stravaCount = 0;
      for (const row of rows) {
        recorded += row.recorded_m || 0;
        if (row.matched_m) {
          matched += row.matched_m;
          recordedWhereMatched += row.recorded_m || 0;
          matchedCount += 1;
        }
        if (row.strava_m) {
          strava += row.strava_m;
          stravaCount += 1;
        }
      }
      return {
        count: rows.length,
        recorded,
        recordedWhereMatched,
        matched,
        strava,
        matchedCount,
        stravaCount,
      };
    }

    /* The whole history, from figures `prepare.py` computed at full resolution. Never from the
     * simplified overview the map is drawing. */
    function renderAggregate() {
      if (!aggregateEl) return;
      const a = aggregate();
      aggregateEl.replaceChildren();
      if (!a.count) return;

      const big = document.createElement('b');
      big.textContent = fmtBigKm(a.recorded);
      aggregateEl.append(big, ` recorded across ${a.count.toLocaleString()} activities`);

      const sub = document.createElement('span');
      sub.className = 'sub';
      const bits = [];
      if (a.matchedCount && a.matched > 0) {
        // Over the matched activities only, on both sides of the division.
        const gap = ((a.recordedWhereMatched / a.matched - 1) * 100).toFixed(1);
        const sign = gap >= 0 ? '+' : '';
        bits.push(`snapped to the map ${fmtBigKm(a.matched)} — recording is ${sign}${gap}%`);
      }
      if (a.stravaCount) {
        bits.push(`Strava says ${fmtBigKm(a.strava)}`);
      }
      if (a.matchedCount !== a.count) {
        bits.push(`${a.count - a.matchedCount} did not snap`);
      }
      sub.textContent = bits.join(' · ');
      aggregateEl.append(sub);
    }

    function renderReadout(measured) {
      if (!readoutEl) return;
      readoutEl.replaceChildren();

      if (!focus) {
        const hint = document.createElement('span');
        hint.className = 'sub';
        hint.textContent = overview
          ? `Pick a run to measure it. The map is drawn at a ${overview.floor_m} m floor; a selected run is full resolution.`
          : '';
        readoutEl.append(hint);
        if (keyRecordedEl) keyRecordedEl.textContent = 'as recorded';
        return;
      }

      const d = focus.doc;
      const big = document.createElement('b');
      big.textContent = fmtKm(measured.length);
      readoutEl.append(big, ' ');

      const drop = d.recorded_m - measured.length;
      const delta = document.createElement('span');
      delta.className = 'delta';
      delta.textContent =
        drop > 0.5
          ? `−${fmtKm(drop)} (−${((drop / d.recorded_m) * 100).toFixed(1)}%)`
          : 'full resolution';
      readoutEl.append(delta);

      const sub = document.createElement('span');
      sub.className = 'sub';
      const bits = [`${measured.kept.toLocaleString()} of ${measured.total.toLocaleString()} points`];
      if (d.matched_m) bits.push(`snapped ${fmtKm(d.matched_m)}`);
      if (d.strava_m) bits.push(`Strava ${fmtKm(d.strava_m)}`);
      sub.textContent = `${d.name} · ${bits.join(' · ')}`;
      readoutEl.append(sub);

      if (keyRecordedEl) {
        keyRecordedEl.textContent = tolerance > 0 ? `simplified to ${tolerance} m` : 'as recorded';
      }
    }

    /* ---------------------------------------------------------------------------
       Applying the slider.
       --------------------------------------------------------------------------- */

    //: Coalesce slider input into one update per frame. Dragging fires `input` far more often
    //: than the display can change.
    //: The handle is kept so `onClose` can cancel it. Closing the window within one frame of
    //: a slider drag otherwise ran `applyTolerance` against a map that `destroy()` had
    //: already removed.
    let pendingFrame = 0;
    function scheduleUpdate(map) {
      if (pendingFrame) return;
      pendingFrame = requestAnimationFrame(() => {
        pendingFrame = 0;
        applyTolerance(map);
      });
    }

    function applyTolerance(map) {
      const measured = focusData();
      const source = map && map.getSource(FOCUS);
      if (source) source.setData(measured.fc);
      if (sliderOut) sliderOut.textContent = `${tolerance} m`;
      renderReadout(measured);
    }

    function setSliderEnabled(on) {
      if (!slider) return;
      slider.disabled = !on;
      if (!on) {
        slider.value = '0';
        tolerance = 0;
        if (sliderOut) sliderOut.textContent = '0 m';
      }
    }

    /* ---------------------------------------------------------------------------
       Selection.
       --------------------------------------------------------------------------- */

    function boundsOfCoords(coords) {
      let west = 180;
      let south = 90;
      let east = -180;
      let north = -90;
      for (const [lon, lat] of coords) {
        if (lon < west) west = lon;
        if (lon > east) east = lon;
        if (lat < south) south = lat;
        if (lat > north) north = lat;
      }
      return coords.length ? [[west, south], [east, north]] : null;
    }

    async function selectActivity(map, id, { fly = true, openPanel: showPanel = true } = {}) {
      let entry;
      try {
        entry = await loadFocus(id);
      } catch (err) {
        setStatus(`Could not load ${id}: ${err.message}`);
        return null;
      }
      focus = entry;
      setSliderEnabled(true);

      applyTraceEmphasis(map);
      applyTolerance(map);
      if (fly) {
        const box = boundsOfCoords(entry.doc.recorded);
        if (box) map.fitBounds(box, { padding: 70, duration: 700, maxZoom: 16 });
      }
      if (showPanel) openPanel();
      if (clearButton) clearButton.hidden = false;
      return entry;
    }

    function clearSelection(map) {
      focus = null;
      passSpan = null;
      setSliderEnabled(false);
      if (map.getSource(FOCUS)) map.getSource(FOCUS).setData(EMPTY);
      if (map.getSource(PASS)) map.getSource(PASS).setData(EMPTY);
      ghostSpans = [];
      selectedCell = null;
      if (map.getSource(GHOSTS)) map.getSource(GHOSTS).setData(EMPTY);
      if (map.getLayer(`${NOISE}-selected`)) {
        map.setFilter(`${NOISE}-selected`, ['==', ['get', 'id'], '']);
      }
      applyTraceEmphasis(map);
      renderReadout(focusData());
      if (clearButton) clearButton.hidden = true;
      if (panel && panel.isOpen) panel.close();
    }

    /* ---------------------------------------------------------------------------
       The drill-down: segment -> its passes -> the run one of them came from.
       --------------------------------------------------------------------------- */

    //: Passes listed per segment. Thirty-seven buttons is a wall, and the interesting ones are
    //: at the ends of the distribution, so the list is the worst few and the best few.
    const PASSES_SHOWN = 6;

    function passButton(map, pass, cell) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'pass-row';
      const sign = pass.adjusted_pts > 0 ? '+' : '';
      button.textContent =
        `${pass.name} · ${pass.inflation_pct.toFixed(1)}% ` +
        `(${sign}${pass.adjusted_pts.toFixed(1)} adj, ${pass.speed_ms.toFixed(1)} m/s)`;
      button.addEventListener('click', () => showPass(map, pass, cell));
      return button;
    }

    /* A segment's own extent, found from the feature its properties came from.
     *
     * A click hands over properties; the geometry lives on the feature. Looked up by id
     * rather than threaded through, because every caller only ever holds the properties.
     *
     * A way has a real extent. The grid had only a centre and one cell size, which is why
     * the camera used to be given a box drawn around a point. */
    function segmentBounds(props) {
      if (!noise) return null;
      const feature = noise.features.find((f) => f.properties.id === props.id);
      if (!feature) return null;
      return boundsOfCoords(feature.geometry.coordinates);
    }

    /** Open a segment: what this stretch measures, and every run that crossed it. */
    async function openSegment(map, props) {
      if (!panel) return;
      selectedCell = props.id;
      passSpan = null;
      if (map.getSource(PASS)) map.getSource(PASS).setData(EMPTY);
      if (map.getLayer(`${NOISE}-selected`)) {
        map.setFilter(`${NOISE}-selected`, ['==', ['get', 'id'], selectedCell]);
      }

      const passes = Array.isArray(props.passes) ? props.passes : [];

      // Go there first. A 220 m cell at city zoom is a few pixels across, and a worm drawn
      // inside it would be one smudge: the overlay only says anything at a zoom where two
      // traces four metres apart are two traces.
      map.fitBounds(
        [
          ...(segmentBounds(props) || [
            [props.lon - 0.002, props.lat - 0.0014],
            [props.lon + 0.002, props.lat + 0.0014],
          ]),
        ],
        { padding: 40, duration: 600, maxZoom: 18 }
      );
      const drawn = passes.length ? await drawGhosts(map, passes) : 0;
      const sign = (v) => `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
      const stats = [
        ['Recorded over simplified', `${props.inflation_pct.toFixed(2)}%`],
        ['Speed-adjusted', `${sign(props.adjusted_pts)} points`],
        ['Worst tenth of passes', `${props.p90_pct.toFixed(2)}%`],
        ['Crossings measured', `${props.windows}`],
        ['Different runs', `${props.activities}`],
      ];

      const groups = [group('This stretch', { columns: ['Measure', 'Value'], rows: stats })];

      if (passes.length) {
        const worst = passes.slice(0, PASSES_SHOWN);
        const best = passes.slice(-PASSES_SHOWN).reverse();
        groups.push(
          group(
            `Noisiest crossings`,
            worst.map((p) => passButton(map, p, props))
          )
        );
        if (passes.length > PASSES_SHOWN * 2) {
          groups.push(
            group(
              `Quietest crossings`,
              best.map((p) => passButton(map, p, props))
            )
          );
        }
      }

      panel.open(
        `Segment at ${props.lat.toFixed(4)}, ${props.lon.toFixed(4)}`,
        groups,
        drawn
          ? `${drawn} of ${passes.length} crossings drawn on top of each other, spread evenly ` +
            `from the noisiest to the quietest. The width of that bundle is the noise — pick ` +
            `one to pull it to the front.`
          : 'No individual crossings were kept for this segment.'
      );
    }

    /* The leaf: load the run this crossing came from, draw just that stretch, and go there.
     *
     * The run is loaded at full resolution, so the slider works on it afterwards -- the point
     * of arriving here is to look at the wobble, and the wobble is what the overview's 5 m
     * floor removed. */
    async function showPass(map, pass, cell) {
      const entry = await selectActivity(map, pass.activity, { fly: false, openPanel: false });
      if (!entry) return;
      passSpan = pass;
      const source = map.getSource(PASS);
      if (source) source.setData(passData());

      const coords = entry.doc.recorded.slice(pass.i0, pass.i1 + 1);
      const box = boundsOfCoords(coords);
      if (box) map.fitBounds(box, { padding: 90, duration: 700, maxZoom: 18 });

      if (!panel) return;
      const sign = (v) => `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
      const back = document.createElement('button');
      back.type = 'button';
      back.className = 'pass-row';
      back.textContent = '← back to the whole segment';
      back.addEventListener('click', () => openSegment(map, cell));

      panel.open(pass.name || pass.activity, [
        group('This crossing', {
          columns: ['Measure', 'Value'],
          rows: [
            ['Recorded over simplified', `${pass.inflation_pct.toFixed(2)}%`],
            ['Speed-adjusted', `${sign(pass.adjusted_pts)} points`],
            ['Speed through here', `${pass.speed_ms.toFixed(2)} m/s`],
            ['Points in the stretch', `${pass.i1 - pass.i0 + 1}`],
          ],
        }),
        group('The run it came from', {
          columns: ['Field', 'Value'],
          rows: [
            ['Recorded', fmtKm(entry.doc.recorded_m)],
            ...(entry.doc.matched_m ? [['Snapped', fmtKm(entry.doc.matched_m)]] : []),
            ...(entry.doc.date ? [['Date', entry.doc.date]] : []),
          ],
        }),
        group('Elsewhere', [back]),
      ], 'The slider now measures this whole run, at full resolution.');
    }

    function openPanel() {
      if (!panel || !focus) return;
      const d = focus.doc;
      const keep = simplifyMask(focus.projected, tolerance);
      const atTolerance = planeLength(focus.projected, keep);

      const rows = [['As recorded', fmtKm(d.recorded_m)]];
      if (tolerance > 0) rows.push([`Simplified to ${tolerance} m`, fmtKm(atTolerance)]);
      if (d.matched_m) rows.push(['Snapped to the map', fmtKm(d.matched_m)]);
      if (d.strava_m) rows.push(['Strava’s own figure', fmtKm(d.strava_m)]);
      if (d.matched_m) {
        const gap = (d.recorded_m / d.matched_m - 1) * 100;
        rows.push(['Recorded over snapped', `${gap >= 0 ? '+' : ''}${gap.toFixed(1)}%`]);
      }

      const recording = [['Points', d.points.toLocaleString()]];
      if (d.step_m) recording.push(['Median step', `${d.step_m.toFixed(1)} m`]);
      if (d.duration_s) recording.push(['Elapsed', `${Math.round(d.duration_s / 60)} min`]);
      if (d.sport) recording.push(['Type', d.sport]);
      if (d.date) recording.push(['Date', d.date]);

      const groups = [
        group('Distance', { columns: ['Measure', 'Value'], rows }),
        group('Recording', { columns: ['Field', 'Value'], rows: recording }),
      ];
      if (d.match_note) groups.push(group('Matching', [d.match_note]));

      panel.open(
        d.name || d.id,
        groups,
        d.matched_m
          ? 'Neither figure is the truth. Jitter adds length to the recording; snapping to the map cuts corners off it.'
          : 'This trace did not snap to the map, so only the recorded length is known.'
      );
    }

    /* ---------------------------------------------------------------------------
       Boot.
       --------------------------------------------------------------------------- */

    //: Loaded before the map so the camera can open on the data rather than panning to it. A
    //: failure is reported and the base map still draws: a day whose data is missing should
    //: say so, not show a blank window.
    let loadError = null;
    try {
      summary = await fetchFirst('index.json');
      overview = await fetchFirst('overview.json');
    } catch (err) {
      loadError = err;
    }

    // Optional, and a separate try: a missing noise grid disables one control. Folding it in
    // above would turn "you have not run noise_by_area.py" into "the page has no data".
    try {
      // Streets first, then the grid.
      //
      // `street_segments.py` keys on the OSM way the matcher put you on, which is the unit
      // the question is actually about. `noise_by_area.py` keys on a 222 m square, which
      // was always an admission: a square is not a place and it cuts across junctions.
      // Both write the same property names, so the map, the legend and the drill-down do
      // not care which arrived — only the caption does.
      try {
        noise = await fetchFirst('street-noise.json');
      } catch {
        noise = await fetchFirst('area-noise.json');
      }
    } catch {
      noise = null;
    }

    const base = await createBaseMap({
      root: chrome.root,
      center: OPEN_AT.center,
      zoom: OPEN_AT.zoom,

      draw(map, ctx) {
        map.addSource(ALL, { type: 'geojson', data: allData() });
        map.addSource(FOCUS, { type: 'geojson', data: focusData().fc });
        map.addSource(MATCHED, { type: 'geojson', data: matchedGeometry() });

        const dimmed = focus
          ? ['case', ['==', ['get', 'id'], focus.id], 0, DIM]
          : FULL;

        //: Matched first, so the recording sits on top of it. The recording is the subject;
        //: the snapped path is what it is being measured against.
        map.addLayer(
          {
            id: `${MATCHED}-casing`,
            type: 'line',
            source: MATCHED,
            layout: { 'line-cap': 'round', 'line-join': 'round', visibility: showMatched ? 'visible' : 'none' },
            paint: { 'line-color': CASING, 'line-width': ZOOM_WIDTH.matchedCasing },
          },
          ctx.beforeId
        );
        map.addLayer(
          {
            id: MATCHED,
            type: 'line',
            source: MATCHED,
            layout: { 'line-cap': 'round', 'line-join': 'round', visibility: showMatched ? 'visible' : 'none' },
            paint: { 'line-color': INK_MATCHED, 'line-width': ZOOM_WIDTH.matched },
          },
          ctx.beforeId
        );
        map.addLayer(
          {
            id: `${ALL}-casing`,
            type: 'line',
            source: ALL,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': CASING, 'line-width': ZOOM_WIDTH.allCasing, 'line-opacity': dimmed },
          },
          ctx.beforeId
        );
        map.addLayer(
          {
            id: ALL,
            type: 'line',
            source: ALL,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': INK_RECORDED, 'line-width': ZOOM_WIDTH.all, 'line-opacity': dimmed },
          },
          ctx.beforeId
        );
        map.addLayer(
          {
            id: `${FOCUS}-casing`,
            type: 'line',
            source: FOCUS,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': CASING, 'line-width': WIDTH.focusCasing },
          },
          ctx.beforeId
        );
        map.addLayer(
          {
            id: FOCUS,
            type: 'line',
            source: FOCUS,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': INK_RECORDED, 'line-width': WIDTH.focus },
          },
          ctx.beforeId
        );
        //: Last, so it sits above the drawn lines and takes the pointer first.
        map.addLayer(
          {
            id: HIT,
            type: 'line',
            source: ALL,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': INK_RECORDED, 'line-opacity': 0, 'line-width': WIDTH.hit },
          },
          ctx.beforeId
        );

        /* Everything from here up sits above the whole basemap, `ctx.beforeId` included.
         *
         * That insertion point exists so place labels stay above a thin data overlay, and it
         * is right for the traces above. It is wrong for a scrim and for an area-scale
         * encoding: reading the live style showed fifty basemap layers -- bridges, one-way
         * arrows, every label -- painting above it, which is why the first version of this
         * looked like it was drawn in the wrong order. It was.
         */
        map.addLayer({
          id: NOISE_SCRIM,
          type: 'background',
          layout: { visibility: showNoise ? 'visible' : 'none' },
          // Quiets the basemap so the coloured segments carry the eye; it is not what the ramp
          // was validated against. Each segment's own opaque casing is that surface, which is
          // why this alpha is a legibility choice rather than a contrast gate -- street names
          // still have to be readable through it to say *which* street is noisy.
          paint: { 'background-color': SCRIM },
        });
        // The noise layer goes in first, so the traces draw on top of it. The coloured
        // segments are context; the lines a reader clicks are the subject.
        if (noise) {
          map.addSource(NOISE, { type: 'geojson', data: noise });
          map.addLayer(
            {
              id: `${NOISE}-casing`,
              type: 'line',
              source: NOISE,
              layout: {
                'line-cap': 'round',
                'line-join': 'round',
                visibility: showNoise ? 'visible' : 'none',
              },
              // Each segment's own surface, so the ramp is judged against a known colour
              // rather than against whatever basemap happens to be under that street. The
              // scrim handles the same problem globally; this one is per-mark and is what
              // makes the ramp's contrast ratios mean anything.
              paint: { 'line-color': NOISE_CASING_INK, 'line-width': noiseCasingExpression() },
            }
          );
          map.addLayer(
            {
              id: `${NOISE}-selected`,
              type: 'line',
              source: NOISE,
              layout: {
                'line-cap': 'round',
                'line-join': 'round',
                visibility: showNoise ? 'visible' : 'none',
              },
              // A white halo around the open segment, wider than its casing so it reads at any
              // class width.
              paint: {
                'line-color': '#ffffff',
                'line-width': noiseStepExpression(NOISE_WIDTHS.map((w) => w + 6)),
                'line-opacity': 0.75,
                'line-blur': 1,
              },
              filter: ['==', ['get', 'id'], selectedCell || ''],
            }
          );
          map.addLayer(
            {
              id: NOISE,
              type: 'line',
              source: NOISE,
              layout: {
                'line-cap': 'round',
                'line-join': 'round',
                visibility: showNoise ? 'visible' : 'none',
              },
              // Opaque, so the colours on screen are the ones that were validated. Painting a
              // checked ramp at partial alpha composites every step toward whatever is behind
              // it and puts the result outside what was measured.
              paint: {
                'line-color': noiseStepExpression(NOISE_RAMP),
                'line-width': noiseStepExpression(NOISE_WIDTHS),
              },
            }
          );
        }
        //: The worm, under the one solid crossing: every loaded pass over this segment, so
        //: the bundle's width shows the disagreement between them.
        map.addSource(GHOSTS, { type: 'geojson', data: ghostData() });
        map.addLayer(
          {
            id: GHOSTS,
            type: 'line',
            source: GHOSTS,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': GHOST_INK, 'line-width': 1.5 },
          }
        );

        //: One crossing of a segment, over its own run. Drawn above the focused trace so the
        //: stretch being discussed is the brightest thing on screen.
        map.addSource(PASS, { type: 'geojson', data: passData() });
        map.addLayer(
          {
            id: PASS,
            type: 'line',
            source: PASS,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': '#ffffff', 'line-width': 3.5 },
          }
        );

      },
    });

    if (!base) {
      // `createBaseMap` has already written the reason into the status line, which is in the
      // bar and already on screen, so there is nothing to insert — and nothing to return:
      // this runs from `ready`, whose return value the desktop discards. It used to
      // `return win.bodyEl.firstElementChild`, which was both dead and, by then, the
      // loading marquee rather than the map.
      return;
    }

    // A window closed during the await never gets `setContent`, so `onClose` has already
    // fired and would never reach the handler set below. Let go of the context here.
    if (!win.isOpen) {
      base.destroy();
      return null;
    }

    // The controls go in the slot the factory left, and the panel wraps the shell it built.
    ({
      aggregateEl,
      noiseNote,
      noiseToggle,
      legendEl,
      slider,
      sliderOut,
      readoutEl,
      keyRecordedEl,
      matchedToggle,
      clearButton,
    } = buildCards(chrome.cards, chrome.instance));
    statusEl = chrome.status;
    panel = new Panel(chrome.detail);

    // The program owns what it started: webxp destroys the window's DOM but knows nothing
    // about a WebGL context or a ResizeObserver.
    win.onClose = () => {
      // The frame first: a queued `applyTolerance` would otherwise run against a map that
      // `destroy()` has already removed.
      if (pendingFrame) {
        cancelAnimationFrame(pendingFrame);
        pendingFrame = 0;
      }
      base.destroy();
    };

    {
      const { map } = base;
      // On the window, not on `window`: two windows of one app would otherwise fight
      // over one global, and the second would drive the first one's map.
      win.base = base;

      //: What `scripts/check.py` drives. Nothing in the interface reads it.
      win.lines = {
        get tolerance() {
          return tolerance;
        },
        dataDir: () => dataDir,
        overviewFloor: () => (overview ? overview.floor_m : null),
        //: The stated opening view, so a check can assert the camera honours it without
        //: a second copy of the coordinates.
        openAt: OPEN_AT,
        //: The noise layer and its drill-down, for `scripts/check.py`.
        hasNoise: () => Boolean(noise),
        noiseCells: () => (noise ? noise.features.length : 0),
        //: Cell properties, read from the module's own copy rather than off the MapLibre
        //: source -- `source._data` is wrapped in MapLibre 6 and reaching for it returns
        //: undefined.
        cells: () => (noise ? noise.features.map((f) => f.properties) : []),
        noiseShown: () => showNoise,
        legendRows: () => noiseLegendRows(),
        selectedCell: () => selectedCell,
        passSpan: () => passSpan,
        ghostCount: () => ghostSpans.length,
        firstCellWithPasses: () => {
          if (!noise) return null;
          const f = noise.features.find((x) => (x.properties.passes || []).length > 2);
          return f ? f.properties : null;
        },
        openSegment: (props) => openSegment(map, props),
        showPass: (pass, cell) => showPass(map, pass, cell),
        activities: () => ((summary && summary.activities) || []).map((a) => a.id),
        aggregate,
        selectedId: () => (focus ? focus.id : null),
        select: (id) => selectActivity(map, id, { fly: false }),
        clear: () => clearSelection(map),
        setTolerance: (t) => {
          tolerance = t;
          applyTolerance(map);
        },
        //: The ladder for the selected run, computed here, against the one `prepare.py`
        //: wrote into that run's document. The guard on two implementations of one
        //: calculation.
        ladder: () => (focus ? ladder(focus.doc.recorded, LADDER_TOLERANCES) : null),
        writtenLadder: () => (focus ? focus.doc.ladder : null),
        focusLengthAt: (t) => {
          if (!focus) return null;
          const was = tolerance;
          tolerance = t;
          const out = focusData().length;
          tolerance = was;
          return out;
        },
      };

      setSliderEnabled(false);
      renderAggregate();
      applyTolerance(map);

      if (loadError) {
        setStatus(`No activity data: ${loadError.message}. Run prepare.py, then build_overview.py.`);
      } else {
        const a = aggregate();
        setStatus(
          `${a.count.toLocaleString()} activities, ${a.matchedCount.toLocaleString()} snapped to the map. ` +
            `Click a run to measure it.`
        );
      }

      if (slider) {
        slider.addEventListener('input', () => {
          tolerance = Number(slider.value);
          scheduleUpdate(map);
        });
      }

      if (matchedToggle) {
        matchedToggle.addEventListener('click', () => {
          showMatched = !showMatched;
          matchedToggle.setAttribute('aria-pressed', String(showMatched));
          for (const id of [MATCHED, `${MATCHED}-casing`]) {
            if (map.getLayer(id)) {
              map.setLayoutProperty(id, 'visibility', showMatched ? 'visible' : 'none');
            }
          }
        });
      }

      if (clearButton) {
        clearButton.hidden = true;
        clearButton.addEventListener('click', () => clearSelection(map));
      }

      // The noise layer: its toggle, its legend, and the drill-down it is the entry to.
      // handles come from buildCards
      
      

      if (noiseToggle) {
        if (!noise) {
          // Disabled rather than hidden: a control that is absent looks like a feature that
          // does not exist, and this one is one script away.
          noiseToggle.disabled = true;
          if (noiseNote) {
            noiseNote.textContent = 'Run noise_by_area.py to measure noise by place.';
          }
        } else {
          // Built from the same two constants the fill expression uses, so the legend cannot
          // disagree with the map about where a class starts.
          if (legendEl) {
            legendEl.replaceChildren(
              ...noiseLegendRows().map(({ colour, width, label }) => {
                const row = document.createElement('div');
                const swatch = document.createElement('i');
                // Colour and width together, because the map encodes the class in both.
                swatch.style.borderTopColor = colour;
                swatch.style.borderTopWidth = `${width}px`;
                const text = document.createElement('span');
                // Text in text tokens, never the series colour.
                text.textContent = label;
                row.append(swatch, text);
                return row;
              })
            );
          }
          if (noiseNote) {
            // The caption is the one thing that cares which file arrived. Degrees of
            // latitude to metres, not kilometres: the square version read "0.222 m across"
            // until a screenshot was looked at.
            const unit =
              noise.unit === 'osm_way'
                ? 'streets'
                : `${Math.round((noise.cell_deg || 0.002) * 111_000)} m squares`;
            noiseNote.textContent =
              `${noise.features.length} ${unit}, speed-adjusted.`;
          }
          noiseToggle.addEventListener('click', () => {
            showNoise = !showNoise;
            noiseToggle.setAttribute('aria-pressed', String(showNoise));
            if (legendEl) legendEl.hidden = !showNoise;
            for (const id of [NOISE_SCRIM, `${NOISE}-casing`, `${NOISE}-selected`, NOISE]) {
              if (map.getLayer(id)) {
                map.setLayoutProperty(id, 'visibility', showNoise ? 'visible' : 'none');
              }
            }
            applyTraceEmphasis(map);
            if (showNoise) goToCells(map);
          });
          if (legendEl) legendEl.hidden = true;

          map.on('click', NOISE, (event) => {
            // Only when the layer is the thing the reader is looking at. A click that lands
            // on a trace should still select the trace.
            if (!showNoise) return;
            openSegment(map, event.features[0].properties);
          });
          map.on('mouseenter', NOISE, () => {
            if (showNoise) map.getCanvas().style.cursor = 'pointer';
          });
          map.on('mouseleave', NOISE, () => {
            map.getCanvas().style.cursor = '';
          });
        }
      }

      map.on('click', HIT, (event) => {
        // With the choropleth up, the segment is the subject and a stray click on a trace
        // should not replace the panel the reader is reading.
        if (showNoise) return;
        selectActivity(map, event.features[0].properties.id);
      });

      map.on('mouseenter', HIT, () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', HIT, () => {
        map.getCanvas().style.cursor = '';
      });

      if (panel) {
        // Unconditionally, not `if (focus)`.
        //
        // A segment can be opened with no run selected, which sets `selectedCell` and draws
        // the ghosts but leaves `focus` null. Closing the panel then left the white halo on
        // the segment and the worm on the map with nothing on screen explaining either, and
        // only a later `clearSelection` removed them. `clearSelection` already handles both
        // halves and is safe when there is nothing to clear.
        panel.onClose = () => clearSelection(map);
      }
    }

  }
}
