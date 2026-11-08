/* The base map, ported from the 30 Day Map Challenge into a webxp app.
 *
 * Changed from the original in one way that matters: it takes a `root` element and finds
 * every control inside it, rather than reaching into `document` for ids. See the note on
 * `createBaseMap`. `docs/apps.md` records why, because it is the first thing that bites when
 * porting a page into a window.
 *
 * One factory, three basemaps, and a `draw` callback that is re-run every time the basemap
 * changes — because `setStyle` discards every source and layer that was added to the old
 * one, so a day's overlay has to be rebuildable rather than built once.
 *
 * MapLibre is ~1.1 MB unminified and is imported dynamically after first paint, so the
 * page's chrome is interactive before a byte of it arrives.
 *
 * Usage:
 *
 *   import { createBaseMap } from './basemap.mjs';
 *
 *   const base = await createBaseMap({
 *     root,
 *     center: [-73.95, 40.7],
 *     zoom: 10,
 *     draw(map, ctx) {
 *       map.addSource('thing', { type: 'geojson', data: new URL('./thing.geo.json', import.meta.url) });
 *       map.addLayer({ id: 'thing', type: 'circle', source: 'thing' }, ctx.beforeId);
 *     },
 *   });
 */

/* ---------------------------------------------------------------------------
   The basemaps.

   Each preset is: where the tiles come from, what the credit has to say, how far it zooms,
   and where a day's overlay is inserted into its layer stack.

   Adding a fourth is one object here plus one button in the page's `.layer-switch`.
   --------------------------------------------------------------------------- */

/* OpenFreeMap's `liberty` style, used as published.
 *
 * Not `positron`, and not a repaint of it. positron is a deliberately monochrome canvas
 * style: measured with `scripts/inspect_style.py`, it draws the whole `park` source layer
 * as `rgb(230,233,229)` — a grey — and ships no `landcover_grass`, no `landuse_pitch` and
 * no `landuse_cemetery` layer at all. So Central Park, Prospect Park and Flushing Meadows
 * are not green on it, and no paint override puts back a layer that was never in the style.
 * liberty draws `park` at `#d8e8c8` with a `park_outline`, plus grass, pitches, tracks and
 * cemeteries, which is what makes it read like a general-purpose map.
 *
 * liberty is also the style on openfreemap.org's own homepage, which is the reference the
 * owner asked for (2026-11-04).
 *
 * OpenFreeMap publishes no SLA, so `FALLBACK_STYLE` is not decoration: when this request
 * fails the day's overlay must still draw.
 */
const OPENFREEMAP = 'https://tiles.openfreemap.org/styles/liberty';

//: Esri's ArcGIS Online tiles. The keyless endpoints are widely used and ship as presets in
//: Leaflet and MapLibre provider lists, but Esri's terms govern ArcGIS Online basemaps and
//: generally contemplate an account for application use
//: (https://www.esri.com/en-us/legal/terms/full-master-agreement). Nobody here has confirmed
//: this use is licensed, so it is documented rather than assumed: docs/basemap.md §Licensing.
//: Attribution to Esri is required and is set on the source.
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';

//: OpenTopoMap, raster. Free for non-commercial use with attribution, under a tile usage
//: policy that forbids bulk downloading (https://opentopomap.org/about#verwendung). Style
//: CC-BY-SA 3.0; data OSM contributors (ODbL) plus SRTM elevation.
const OPENTOPO = ['a', 'b', 'c'].map((s) => `https://${s}.tile.opentopomap.org/{z}/{x}/{y}.png`);

/* The satnav palette, over liberty.
 *
 * Paint only. No layer is removed and no road is dropped — an early-2000s GPS screen was
 * mostly roads, and the first attempt at this look stripped them and lost the thing it was
 * imitating. Every key below is a layer id measured out of liberty with
 * `scripts/inspect_style.py`; positron's ids are different, which is why a palette written
 * against positron silently did nothing here.
 *
 * Saturated on purpose. The second attempt muted all of this to keep a data overlay
 * readable and the owner's verdict was that it had gone pale (2026-11-04). These screens
 * were read at a glance in daylight through a windscreen, so nothing on them was subtle:
 * flat olive land, deep navy water, and arterials in a yellow that carries at arm's length.
 * A day whose data needs a quiet backdrop should pick `geography`, or empty this object.
 *
 * Contrast is checked where it changed, not assumed. Navy water broke liberty's own water
 * labels — `#495e91` on `#1b4b8f` measures about 1.5:1 — so those are re-coloured at the
 * foot of this object rather than left unreadable.
 */
const SATNAV = {
  // Land, and the things growing on it.
  background: { 'background-color': '#c3d18a' },
  landuse_residential: { 'fill-color': '#cbd694', 'fill-opacity': 0.9 },
  park: { 'fill-color': '#86b35a', 'fill-outline-color': '#6f9a46', 'fill-opacity': 0.92 },
  park_outline: { 'line-color': '#6f9a46' },
  landcover_wood: { 'fill-color': '#6fa348', 'fill-opacity': 0.9 },
  landcover_grass: { 'fill-color': '#9ec46e', 'fill-opacity': 0.85 },
  landuse_pitch: { 'fill-color': '#8fbb63' },
  landuse_track: { 'fill-color': '#8fbb63' },
  landuse_cemetery: { 'fill-color': '#8aad66' },
  // The era's own colour coding: hospitals pink, schools sand.
  landuse_hospital: { 'fill-color': '#d9a9a0' },
  landuse_school: { 'fill-color': '#d8cf8a' },
  landcover_sand: { 'fill-color': '#e8d98a' },
  landcover_ice: { 'fill-color': '#dfeaf2' },

  // Water. The signature of the look, and the reason the land reads as land.
  water: { 'fill-color': '#1b4b8f' },
  waterway_river: { 'line-color': '#1b4b8f' },
  waterway_other: { 'line-color': '#2a5a9e' },
  waterway_tunnel: { 'line-color': '#2a5a9e' },

  aeroway_fill: { 'fill-color': '#b3b89a' },
  aeroway_runway: { 'line-color': '#d8dcc0' },
  aeroway_taxiway: { 'line-color': '#d8dcc0' },

  /* Roads, brightest class first. Each class is a casing and an inner line; the casing is
     the dark edge that makes a yellow road read against olive land, so the pair has to move
     together or the road loses its outline. liberty's `road_motorway` carries a zoom
     interpolation for its colour, which a flat value replaces — the era did not fade its
     motorways out. */
  road_motorway: { 'line-color': '#ffd21f' },
  road_motorway_casing: { 'line-color': '#7d5e08' },
  road_motorway_link: { 'line-color': '#ffd21f' },
  road_motorway_link_casing: { 'line-color': '#7d5e08' },
  road_trunk_primary: { 'line-color': '#ffdf5c' },
  road_trunk_primary_casing: { 'line-color': '#8c6d12' },
  road_secondary_tertiary: { 'line-color': '#ffeb99' },
  road_secondary_tertiary_casing: { 'line-color': '#9c8524' },
  road_link: { 'line-color': '#ffdf5c' },
  road_link_casing: { 'line-color': '#8c6d12' },
  road_minor: { 'line-color': '#ffffff' },
  road_minor_casing: { 'line-color': '#a8a87e' },
  road_service_track: { 'line-color': '#f4f2e2' },
  road_service_track_casing: { 'line-color': '#a8a87e' },
  road_path_pedestrian: { 'line-color': '#ffffff' },
  // A GPS unit drew roads, not timetables, so rail recedes rather than competing.
  road_major_rail: { 'line-color': '#7a7a5c' },
  road_major_rail_hatching: { 'line-color': '#7a7a5c' },
  road_transit_rail: { 'line-color': '#8a8a6a' },
  road_transit_rail_hatching: { 'line-color': '#8a8a6a' },

  // Tunnels mirror their surface class, one step down in brightness.
  tunnel_motorway: { 'line-color': '#e8be1c' },
  tunnel_motorway_casing: { 'line-color': '#7d5e08' },
  tunnel_motorway_link: { 'line-color': '#e8be1c' },
  tunnel_motorway_link_casing: { 'line-color': '#7d5e08' },
  tunnel_trunk_primary: { 'line-color': '#f0d276' },
  tunnel_trunk_primary_casing: { 'line-color': '#8c6d12' },
  tunnel_secondary_tertiary: { 'line-color': '#f2e0a4' },
  tunnel_secondary_tertiary_casing: { 'line-color': '#9c8524' },
  tunnel_link: { 'line-color': '#f0d276' },
  tunnel_link_casing: { 'line-color': '#8c6d12' },
  tunnel_minor: { 'line-color': '#f0eee0' },
  tunnel_street_casing: { 'line-color': '#a8a87e' },
  tunnel_service_track: { 'line-color': '#eae8d8' },
  tunnel_service_track_casing: { 'line-color': '#a8a87e' },
  tunnel_path_pedestrian: { 'line-color': '#f4f2e2' },
  tunnel_major_rail: { 'line-color': '#7a7a5c' },
  tunnel_major_rail_hatching: { 'line-color': '#7a7a5c' },
  tunnel_transit_rail: { 'line-color': '#8a8a6a' },
  tunnel_transit_rail_hatching: { 'line-color': '#8a8a6a' },

  // Buildings. Flat footprint and extruded block share a hue so tilting does not recolour
  // the city; the 3D one is lighter because it catches the light.
  building: { 'fill-color': '#a8a87e' },
  'building-3d': { 'fill-extrusion-color': '#bdbd92', 'fill-extrusion-opacity': 0.95 },

  boundary_3: { 'line-color': '#7a7a5c' },
  boundary_2: { 'line-color': '#5c5c42' },
  boundary_disputed: { 'line-color': '#5c5c42' },

  /* Water labels, re-coloured because the fill under them changed. liberty sets `#495e91`
     with a white halo, which was written for its pale blue water; on `#1b4b8f` that is
     about 1.5:1 and unreadable. Light text on a dark halo instead, which measures about
     12:1 against the navy. */
  water_name_point_label: { 'text-color': '#dce8ff', 'text-halo-color': 'rgba(8,28,64,0.9)' },
  water_name_line_label: { 'text-color': '#dce8ff', 'text-halo-color': 'rgba(8,28,64,0.9)' },
  waterway_line_label: { 'text-color': '#dce8ff', 'text-halo-color': 'rgba(8,28,64,0.9)' },
};

/** liberty with the satnav palette applied. Paint is merged, so unlisted keys survive. */
function satnavStyle(raw) {
  return {
    ...raw,
    layers: (raw.layers || []).map((layer) => {
      const paint = SATNAV[layer.id];
      return paint ? { ...layer, paint: { ...layer.paint, ...paint } } : layer;
    }),
  };
}

export const BASEMAPS = {
  simple: {
    label: 'Simple',
    //: liberty's tiles go to z14 and MapLibre overzooms cleanly a few levels past that.
    maxZoom: 18,
    attribution:
      'Basemap <a href="https://openfreemap.org/" rel="noopener noreferrer">OpenFreeMap</a>, ' +
      'data &copy; <a href="https://www.openstreetmap.org/copyright" rel="noopener noreferrer">OpenStreetMap contributors</a>',
    //: Fetched rather than handed to MapLibre as a URL, because the palette is applied to
    //: the object and because `createBaseMap` stamps every style it installs with a serial
    //: in `name` to tell a new style's `styledata` from the old one's. The browser caches
    //: the request, so the fetch costs one round trip once.
    style: async () => satnavStyle(await fetchJson(OPENFREEMAP)),
    //: Above the land and roads, below the place labels — so town names draw on top of an
    //: overlay instead of under it. With a vector style this is one argument; it is the trick
    //: Esri's canvas basemaps get by shipping their labels as a second tile layer.
    overlayBefore: (style) => firstSymbolLayerId(style),
  },

  satellite: {
    label: 'Satellite',
    maxZoom: 19,
    attribution:
      'Imagery &copy; <a href="https://www.esri.com/" rel="noopener noreferrer">Esri</a>, ' +
      'Maxar, Earthstar Geographics, and the GIS User Community',
    style() {
      return {
        version: 8,
        //: Needed even for a raster basemap: a symbol layer added by a day would otherwise
        //: have no font to render with, and MapLibre discards the whole style for it.
        glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
        sources: {
          imagery: {
            type: 'raster',
            tiles: [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`],
            tileSize: 256,
            maxzoom: 19,
            attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics',
          },
          places: {
            type: 'raster',
            tiles: [`${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`],
            tileSize: 256,
            maxzoom: 19,
          },
        },
        layers: [
          //: Imagery with no labels is hard to navigate — you can see everything and name
          //: nothing — so Esri's reference layer goes on top, and a day's overlay between.
          { id: 'imagery', type: 'raster', source: 'imagery' },
          { id: 'places', type: 'raster', source: 'places' },
        ],
      };
    },
    overlayBefore: () => 'places',
  },

  geography: {
    label: 'Geography',
    //: OpenTopoMap's own ceiling. Past it the contours are interpolated rather than surveyed.
    maxZoom: 17,
    attribution:
      'Map data &copy; <a href="https://www.openstreetmap.org/copyright" rel="noopener noreferrer">OpenStreetMap contributors</a>, ' +
      'SRTM &middot; style <a href="https://opentopomap.org/" rel="noopener noreferrer">OpenTopoMap</a> ' +
      '(<a href="https://creativecommons.org/licenses/by-sa/3.0/" rel="noopener noreferrer">CC-BY-SA</a>)',
    style() {
      return {
        version: 8,
        glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
        sources: {
          topo: {
            type: 'raster',
            tiles: OPENTOPO,
            tileSize: 256,
            maxzoom: 17,
            attribution: 'OpenTopoMap (CC-BY-SA), data OpenStreetMap contributors + SRTM',
          },
        },
        layers: [{ id: 'topo', type: 'raster', source: 'topo' }],
      };
    },
    //: One raster layer and nothing above it, so an overlay simply goes on top. The contours
    //: underneath stay legible through a semi-transparent fill, which is the reason to pick
    //: this basemap in the first place.
    overlayBefore: () => undefined,
  },
};

export const DEFAULT_BASEMAP = 'simple';

//: Drawn when a basemap's own style could not be fetched. Flat, and deliberately not one of
//: the three palettes: a reader looking at it should be able to tell that something failed.
const FALLBACK_STYLE = {
  version: 8,
  glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
  sources: {},
  layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#dcdcd2' } }],
};

//: Which basemap was last chosen, so the choice survives moving between days. A failure here
//: is swallowed: private browsing throws on `localStorage` and that must not cost the map.
const STORAGE_KEY = '30dmc:basemap';

function rememberedBasemap() {
  try {
    const name = window.localStorage.getItem(STORAGE_KEY);
    return BASEMAPS[name] ? name : null;
  } catch {
    return null;
  }
}

function rememberBasemap(name) {
  try {
    window.localStorage.setItem(STORAGE_KEY, name);
  } catch {
    /* no storage, no memory. The map is unaffected. */
  }
}

/* ---------------------------------------------------------------------------
   Helpers.
   --------------------------------------------------------------------------- */

export function fetchJson(url) {
  return fetch(url).then((response) => {
    if (!response.ok) throw new Error(`${url} returned ${response.status}`);
    return response.json();
  });
}

/** The id of a style's first symbol layer, or undefined if it has none. */
function firstSymbolLayerId(style) {
  const label = (style.layers || []).find((layer) => layer.type === 'symbol');
  return label && label.id;
}

/** WebGL2 before MapLibre: a 1.1 MB download that can only fail is worth skipping. */
function hasWebgl2() {
  try {
    return Boolean(document.createElement('canvas').getContext('webgl2'));
  } catch {
    return false;
  }
}

/** Resolve after first paint, so the map never delays the chrome above it. */
function afterFirstPaint() {
  return new Promise((resolve) => {
    if (document.readyState === 'complete') {
      requestAnimationFrame(() => resolve());
      return;
    }
    window.addEventListener('load', () => requestAnimationFrame(() => resolve()), { once: true });
  });
}

/* ---------------------------------------------------------------------------
   The factory.
   --------------------------------------------------------------------------- */

/**
 * Build the base map.
 *
 * @param {object} options
 * @param {string|HTMLElement} options.container  The map element, or its id.
 * @param {[number, number]} [options.center]     Starting centre, [lon, lat].
 * @param {number} [options.zoom]                 Starting zoom.
 * @param {[[number,number],[number,number]]} [options.bounds]
 *        Fit these bounds instead of using centre and zoom.
 * @param {string} [options.basemap]              Which preset to open with. Defaults to the
 *        reader's last choice, then to `simple`.
 * @param {boolean} [options.tilt=true]           Allow the camera to pitch and rotate. A
 *        thematic day that must stay flat and north-up passes `false`.
 * @param {(map: object, ctx: object) => void} [options.draw]
 *        Add the day's sources and layers. Called after every basemap load, so it MUST be
 *        idempotent-by-construction: it is always called against a fresh, empty style.
 *        `ctx.beforeId` is the insertion point that keeps place labels on top; `ctx.basemap`
 *        is the active preset name, for a day that wants to paint differently over imagery.
 * @returns {Promise<object|null>} The handle, or null if the map could not be built.
 */
export async function createBaseMap(options = {}) {
  /* The subtree this map owns.
   *
   * Every control below is found inside it, by class. The original looked each one up from
   * `document` by id, which is correct for a page holding one map and wrong here: two
   * windows of this app would both find the first window's buttons, and the symptom is a
   * second window whose zoom buttons drive the first one's camera. */
  const root = options.root;
  if (!root) return null;
  const container = options.container || root.querySelector('.map');

  const status = root.querySelector('.map-status');
  const say = (text) => {
    if (status) status.textContent = text;
  };

  const tilt = options.tilt !== false;

  if (!container) return null;
  if (!hasWebgl2()) {
    say('This browser has no WebGL 2, so the map cannot draw.');
    return null;
  }

  await afterFirstPaint();
  say('Loading the map…');

  let MapLibre;
  try {
    MapLibre = await import('./vendor/maplibre-gl.mjs');
  } catch (error) {
    say(`MapLibre could not load (${error.message}).`);
    return null;
  }

  let active = BASEMAPS[options.basemap] ? options.basemap : rememberedBasemap() || DEFAULT_BASEMAP;

  const loadStyle = async (name) => {
    try {
      return await BASEMAPS[name].style();
    } catch (error) {
      say(`The ${BASEMAPS[name].label.toLowerCase()} basemap did not load (${error.message}).`);
      return null;
    }
  };

  /* Which style is installed, stamped into the style's own `name`.
   *
   * `styledata` is not a reliable "the new style is ready" signal on its own: it fires
   * repeatedly as each of a style's sprites, glyphs and tiles arrive, and the previous
   * style keeps firing it for a while after `setStyle` has been called. A counter bumped
   * just before `setStyle` is therefore consumed by a late event belonging to the *old*
   * style, and the real event for the new one is then skipped — which left the satellite
   * basemap with no overlay at all and no error to show for it (measured 2026-11-04).
   *
   * Stamping the style and comparing identifies the style rather than the moment, so a
   * stale event cannot match. */
  let serial = 0;
  let wantedTag = null;
  let drawnTag = null;

  const stamp = (style) => {
    serial += 1;
    wantedTag = `30dmc-${serial}`;
    return { ...style, name: wantedTag };
  };

  const first = stamp(await loadStyle(active) || FALLBACK_STYLE);

  const map = new MapLibre.Map({
    container,
    style: first,
    center: options.center || [0, 20],
    zoom: options.zoom === undefined ? 2 : options.zoom,
    maxZoom: BASEMAPS[active].maxZoom,
    // Our own credit is rendered in `.map-attrib`, because MapLibre's control is styled by
    // the part of its stylesheet this project does not ship.
    attributionControl: false,
    pitchWithRotate: tilt,
    dragRotate: tilt,
    touchZoomRotate: tilt,
    // Two-finger vertical drag on a phone. Its own handler, and off by default, so without
    // this line the tilt is unreachable on touch no matter what `dragRotate` says.
    touchPitch: tilt,
    // 60 is as far as a car satnav ever leaned, and past about 65 MapLibre's horizon turns
    // into a wall of overzoomed tiles.
    maxPitch: 60,
  });

  if (options.bounds) map.fitBounds(options.bounds, { padding: 32, animate: false });

  /* The 3D buildings, shown only from an angle.
   *
   * liberty draws `building-3d` as a `fill-extrusion` from zoom 14, and at pitch 0 an
   * extrusion is seen straight down the top of itself: the walls collapse and it reads as a
   * smeared grey blob offset from its own footprint. So it is gated on the camera rather
   * than left on — owner's decision, 2026-11-04. Looking down gets the flat map; leaning
   * over gets the city.
   *
   * The layers are discovered by type, not named, so a basemap that ships more than one
   * extrusion layer is covered too. The flat `building` fill underneath is left alone: in
   * 3D the extrusions cover their own footprints, and toggling both would flicker two
   * layers instead of one. */
  const extrusionLayers = (style) =>
    (((style || {}).layers) || []).filter((layer) => layer.type === 'fill-extrusion');

  /* Thresholds, not one value. A single cutoff makes the buildings strobe while the camera
     sits right on it, which a drag-pitch does constantly. On at 12 degrees, off below 8. */
  const PITCH_ON = 12;
  const PITCH_OFF = 8;
  let showing3d = null;

  const applyPitchGate = (force = false) => {
    const pitch = map.getPitch();
    const next = showing3d === null ? pitch >= PITCH_ON : showing3d ? pitch >= PITCH_OFF : pitch >= PITCH_ON;
    if (!force && next === showing3d) return;
    showing3d = next;
    for (const layer of extrusionLayers(map.getStyle())) {
      // The layer may be mid-removal during a style swap; a missing one is not an error.
      if (!map.getLayer(layer.id)) continue;
      map.setLayoutProperty(layer.id, 'visibility', next ? 'visible' : 'none');
    }
  };

  // `pitch` rather than `pitchend`: the buildings should appear while the drag is happening,
  // not once it stops. Cheap, because the loop above only runs when the boolean flips.
  map.on('pitch', () => applyPitchGate());

  /* The compass: an orientation indicator and a control in one.
   *
   * Rotating already worked through MapLibre's own ctrl-drag, right-drag and two-finger
   * gestures, but nothing on screen said which way was up and none of those gestures is
   * discoverable or reachable from a keyboard. So the rose turns with the map, pressing it
   * faces north, and the arrow keys turn the view while it has focus.
   *
   * The needle is rotated by `-bearing`: MapLibre's bearing is the compass direction that
   * is up the screen, so at bearing 90 east is up and true north lies 90 degrees
   * anticlockwise of it.
   *
   * Written to the `g` element's SVG `transform` attribute rather than to `style`, the same
   * division the map pin uses: a geometry attribute survives a strict `style-src`, and
   * MapLibre is not involved here so nothing overwrites it. */
  const compass = root.querySelector('.map-compass');
  const rose = root.querySelector('.map-compass-rose');
  if (compass) {
    if (!tilt) {
      // `dragRotate` is off in this mode, so a compass would indicate something the reader
      // cannot change.
      compass.hidden = true;
    } else {
      //: How far one arrow-key press turns the view. 15 degrees is a sixth of a right angle:
      //: coarse enough to get somewhere in a few presses, fine enough to aim down a street.
      const ROTATE_STEP = 15;

      const syncCompass = () => {
        const bearing = map.getBearing();
        if (rose) rose.setAttribute('transform', `rotate(${-bearing} 12 12)`);
        // The name carries the live value, because the needle is `aria-hidden` and a
        // screen reader would otherwise be told only that there is a button.
        const facing = Math.round(((bearing % 360) + 360) % 360);
        compass.setAttribute(
          'aria-label',
          facing === 0
            ? 'Facing north. Press to reset, arrow keys to turn'
            : `Facing ${facing} degrees. Press to face north, arrow keys to turn`
        );
      };

      compass.addEventListener('click', () => map.easeTo({ bearing: 0, duration: 400 }));
      compass.addEventListener('keydown', (event) => {
        const step =
          event.key === 'ArrowLeft' ? -ROTATE_STEP : event.key === 'ArrowRight' ? ROTATE_STEP : 0;
        if (!step) return;
        // Claimed, or the arrow key also scrolls the page behind the map.
        event.preventDefault();
        map.easeTo({ bearing: map.getBearing() + step, duration: 180 });
      });
      map.on('rotate', syncCompass);
      syncCompass();
    }
  }

  /* A button, because on a desktop the tilt gesture is ctrl-drag or right-drag and nobody
     finds it. It sets the camera; the gate above reacts to the camera. Nothing here toggles
     the buildings directly, so dragging to an angle and pressing this agree by construction. */
  /* `setStyle` discards everything added to the previous style, so `draw` is bound to the
     style rather than to the map: once per installed style, against a fresh empty one. */
  const runDraw = () => {
    // The pitch gate below runs either way, so a page with no overlay still gets its
    // buildings gated.
    if (!options.draw) {
      applyPitchGate(true);
      return;
    }
    const style = map.getStyle() || { layers: [] };
    const ctx = {
      basemap: active,
      beforeId: BASEMAPS[active].overlayBefore(style),
      library: MapLibre,
    };
    try {
      options.draw(map, ctx);
    } catch (error) {
      // Named, not swallowed: a reader who sees an empty basemap should be told whose
      // fault it is, and the basemap itself still works.
      say(`The overlay failed to draw (${error.message}).`);
    }
    // A fresh style brings its extrusion layers back visible, so the gate is re-asserted
    // here rather than waiting for the next pitch event. `force`, because the remembered
    // boolean describes the style that was just thrown away.
    applyPitchGate(true);
  };

  map.on('styledata', () => {
    const current = map.getStyle();
    // A stale event from the style being replaced: its `name` is the previous stamp.
    if (!current || current.name !== wantedTag) return;
    if (drawnTag === wantedTag) return;
    drawnTag = wantedTag;
    runDraw();
  });

  // A style that 404s or times out surfaces here rather than at `load`, so the fallback is
  // armed before the first paint instead of after a reader has stared at nothing.
  let failed = false;
  map.on('error', (event) => {
    const message = (event && event.error && event.error.message) || 'unknown error';
    // Tile-level 404s are normal at the edge of a raster pyramid and must not trip the
    // fallback; only a style, sprite or glyph failure means the basemap itself is gone.
    if (!failed && /style|sprite|glyph/i.test(message)) {
      failed = true;
      say('The basemap did not load, so the data is drawn on its own.');
      map.setStyle(stamp(FALLBACK_STYLE));
      return;
    }
    if (!failed && !/tile|Failed to fetch|403|404/i.test(message)) {
      say(`The map reported an error (${message}).`);
    }
  });

  const attrib = root.querySelector('.map-attrib');
  const paintAttribution = () => {
    // innerHTML, and the only place in this project that uses it: the credit is a fixed
    // string in this file with no reader input anywhere near it, and the licences require
    // specific links. Nothing fetched is ever put through here.
    if (attrib) attrib.innerHTML = BASEMAPS[active].attribution;
  };
  paintAttribution();

  /** Switch basemaps, keeping the camera and rebuilding the day's overlay. */
  const setBasemap = async (name) => {
    if (!BASEMAPS[name] || name === active) return;
    const style = await loadStyle(name);
    if (!style) return;
    active = name;
    rememberBasemap(name);
    failed = false;
    map.setMaxZoom(BASEMAPS[name].maxZoom);
    map.setStyle(stamp(style));
    paintAttribution();
    syncSwitch();
    say(`${BASEMAPS[name].label} basemap.`);
  };

  /* The switcher. Every `[data-basemap]` button in the document drives it, so a page can put
     the control wherever it likes — or leave it out, and the map still works. */
  const buttons = [...root.querySelectorAll('[data-basemap]')];

  function syncSwitch() {
    for (const button of buttons) {
      button.setAttribute('aria-pressed', String(button.dataset.basemap === active));
    }
  }

  for (const button of buttons) {
    button.addEventListener('click', () => setBasemap(button.dataset.basemap));
  }
  syncSwitch();

  const zoomIn = root.querySelector('.map-zoom-in');
  const zoomOut = root.querySelector('.map-zoom-out');
  if (zoomIn) zoomIn.addEventListener('click', () => map.zoomIn());
  if (zoomOut) zoomOut.addEventListener('click', () => map.zoomOut());

  const tiltButton = root.querySelector('.map-tilt');
  if (tiltButton) {
    if (!tilt) {
      tiltButton.hidden = true;
    } else {
      const syncTilt = () => {
        const on = map.getPitch() >= PITCH_ON;
        tiltButton.setAttribute('aria-pressed', String(on));
      };
      tiltButton.addEventListener('click', () => {
        map.easeTo({ pitch: map.getPitch() >= PITCH_ON ? 0 : 55, duration: 400 });
      });
      map.on('pitch', syncTilt);
      syncTilt();
    }
  }

  map.once('idle', () => {
    // Cleared rather than left saying "Loading": an empty status line hides itself, and the
    // next real event is what should put text back in it.
    if (status && /^Loading/.test(status.textContent)) say('');
  });

  return {
    map,
    library: MapLibre,
    /** The active preset's name. */
    get basemap() {
      return active;
    },
    setBasemap,
    /** Put a line in the status region. Announced, because it is `aria-live="polite"`. */
    say,
    /** Rebuild the day's overlay against the current style, without changing basemaps. */
    redraw() {
      runDraw();
    },
  };
}
