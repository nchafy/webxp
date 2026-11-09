/* Map — the 51 council districts and the 59 community boards, drawn.
 *
 * Ported from this site's own `/map/` route. What changed is the page it assumed, which is
 * the lesson `webxp`'s `docs/apps.md` records from porting the base map: the route shipped a
 * title bar, a taskbar, a status bar and a sliding detail panel, and the desktop is all four
 * of those now. Selecting a district used to open a panel and offer a link to
 * `/district/35/`; it fills a card in this window instead, because that route is going away
 * and a program is not a page.
 *
 * MapLibre and the base map are NOT vendored in this folder. They are already shipped at
 * `/vendor/`, fetched and verified against a pinned sha256 by `scripts/fetch_maplibre.py`
 * and `scripts/fetch_basemap.py`. `docs/apps.md` says to vendor a dependency that is the
 * app's alone and to point at a shared path when it is not: a second copy here would be a
 * second thing to pin and the checksums would have to agree by hand.
 *
 * `singleton: true` is load-bearing rather than a preference. The vendored `basemap.mjs`
 * finds its own controls with `document.getElementById` — `map-status`, `map-zoom-in`,
 * `map-zoom-out`, `map-attrib`, and every `[data-basemap]` button — and it is pinned, so it
 * cannot be scoped from here. One window means those ids are unique in the document and the
 * lookups resolve to this app's elements. A second window would drive the first one's map.
 * Everything this file looks up itself is scoped to the subtree it built.
 */

import { group, table } from '../../src/widgets.mjs';

//: Whatever zoom fits the whole city is as far out as the map goes. The ceiling is fixed:
//: past this the basemap has no more detail and the boundary, simplified to 2.2 m, starts to
//: look like a decision rather than a coastline.
const MAX_ZOOM = 15;

const FILL = '#1b3f7a';
const SELECTED = '#e8431f';
const HOVERED = '#ff8c42';
const LINE = '#13264a';

//: The boundary's colour and weight per basemap. A boundary is read against whatever the
//: basemap draws under it: liberty's land is near-white, Esri's imagery is dark and mottled,
//: and OpenTopoMap is busy at every value. The navy hairline that reads on the first
//: disappears into the other two, which left the districts with no visible edge at all.
const BOUNDARY = {
  simple: { color: LINE, width: 1.1 },
  satellite: { color: '#ffffff', width: 1.9 },
  geography: { color: '#111111', width: 1.9 },
};

//: Where the city sits, and the zoom the whole of it fits at on a window-sized canvas.
const CENTER = [-73.95, 40.7];
const ZOOM = 9;

/** Neighbourhood names and the City's board labels; headline facts per feature. */
let names = { district: new Map(), board: new Map() };
let preview = null;

const LAYERS = {
  council: {
    url: '/data/showup/districts.geo.json',
    key: 'coundist',
    source: 'council',
    noun: 'council district',
    label: (id) => `Council District ${id}`,
    sections: (id) => {
      const hoods = (names.district.get(String(id)) || {}).hoods;
      const facts = (preview && preview.districts && preview.districts[String(id)]) || {};
      const member = facts.member;
      return [
        ['Neighbourhoods', hoods || null],
        ['Council Member', member ? member.name : null],
        // Only when it is not "filled": a status beside every name would read as a warning
        // about fifty filled seats.
        ['Seat', member && member.seat_status !== 'filled' ? member.seat_status : null],
      ];
    },
  },
  board: {
    url: '/data/showup/boards.geo.json',
    key: 'boro_cd',
    source: 'board',
    noun: 'community board',
    label: (id) => {
      const facts = (preview && preview.boards && preview.boards[String(id)]) || {};
      return facts.label || (names.board.get(String(id)) || {}).label
        || `Community Board ${id}`;
    },
    sections: (id) => {
      const hoods = (names.board.get(String(id)) || {}).hoods;
      const facts = (preview && preview.boards && preview.boards[String(id)]) || {};
      return [
        ['Covers', hoods || null],
        ['Office', facts.address || null],
        ['Phone', facts.phone || null],
        // The board's own words for when it meets. Not expanded into dates: "Fourth Tuesday"
        // is not a date until somebody publishes one.
        ['Meets', facts.cadence || null],
      ];
    },
  },
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fetchJson(url) {
  return fetch(url).then((response) => {
    if (!response.ok) throw new Error(`${url} returned ${response.status}`);
    return response.json();
  });
}

/* liberty compares `ref_length` without checking the feature carries it, so MapLibre disables
 * both highway-shield layers and no shield ever draws. Repaired on the loaded style rather
 * than before it, because the base map owns style loading. */
function repairShieldFilters(map) {
  for (const layer of map.getStyle().layers || []) {
    const filter = layer.filter;
    if (!Array.isArray(filter)) continue;
    const repaired = JSON.parse(
      JSON.stringify(filter).replaceAll(
        '["get","ref_length"]',
        '["coalesce",["get","ref_length"],0]',
      ),
    );
    if (JSON.stringify(repaired) !== JSON.stringify(filter)) map.setFilter(layer.id, repaired);
  }
}

/** A copy of the basemap's own water fill, to clip the district tint to the coastline. */
function waterMaskLayer(style) {
  const water = (style.layers || []).find(
    (layer) => layer['source-layer'] === 'water' && layer.type === 'fill',
  );
  if (!water) return null;
  return { ...water, id: 'water-mask', filter: water.filter || ['all'] };
}

function addFillLayer(map, layer, data, beforeId) {
  // `promoteId` is what makes `setFeatureState` addressable: these features carry no id of
  // their own, only the one key property.
  map.addSource(layer.source, { type: 'geojson', data, promoteId: layer.key });
  map.addLayer(
    {
      id: `${layer.source}-fill`,
      type: 'fill',
      source: layer.source,
      layout: { visibility: 'none' },
      paint: {
        'fill-color': [
          'case',
          ['boolean', ['feature-state', 'selected'], false],
          SELECTED,
          ['boolean', ['feature-state', 'hovered'], false],
          HOVERED,
          FILL,
        ],
        // Almost transparent at rest: a district is read by its boundary, not its tint. All
        // three states fade as the reader zooms in. `['zoom']` MUST stay the top level of the
        // expression — nested, MapLibre discards the whole style silently.
        'fill-opacity': [
          'interpolate',
          ['linear'],
          ['zoom'],
          12,
          [
            'case',
            ['boolean', ['feature-state', 'selected'], false],
            0.72,
            ['boolean', ['feature-state', 'hovered'], false],
            0.45,
            0.16,
          ],
          MAX_ZOOM,
          [
            'case',
            ['boolean', ['feature-state', 'selected'], false],
            0.22,
            ['boolean', ['feature-state', 'hovered'], false],
            0.16,
            0.05,
          ],
        ],
      },
    },
    beforeId,
  );
}

/* The boundary, above everything the basemap draws. Separate from the fill because the two
 * want opposite ends of the stack: the fill sits under the water mask, and under the water
 * mask the boundary was also under the roads, which made it read as broken. */
function addLineLayer(map, layer, beforeId, basemap) {
  const edge = BOUNDARY[basemap] || BOUNDARY.simple;
  map.addLayer(
    {
      id: `${layer.source}-line`,
      type: 'line',
      source: layer.source,
      layout: { visibility: 'none' },
      paint: { 'line-color': edge.color, 'line-width': edge.width },
    },
    beforeId,
  );
}

/* The subtree this app owns.
 *
 * The ids are the vendored base map's contract, not names chosen here: it finds the status
 * line, the zoom pair, the credit and the basemap switcher by id and by `[data-basemap]`.
 */
function build() {
  const root = el('div', 'map-root');

  // `map` as well as `map-canvas`: the base map takes the container by class from the
  // root it is handed, which is what lets two windows of a map app coexist.
  const canvas = el('div', 'map-canvas map');
  canvas.id = 'map';

  const cards = el('div', 'map-cards');

  const boundaries = el('div', 'card');
  boundaries.append(el('h2', null, 'Boundaries'));
  const switcher = el('div', 'layer-switch');
  switcher.setAttribute('role', 'group');
  switcher.setAttribute('aria-label', 'Which boundaries to show');
  for (const [name, layer] of Object.entries(LAYERS)) {
    const button = el('button', null, name === 'council' ? 'Council districts' : 'Community boards');
    button.type = 'button';
    button.dataset.layer = name;
    button.setAttribute('aria-pressed', String(name === 'council'));
    switcher.append(button);
    layer.button = button;
  }
  boundaries.append(switcher);

  const basemaps = el('div', 'card');
  basemaps.append(el('h2', null, 'Basemap'));
  const basemapSwitch = el('div', 'layer-switch');
  basemapSwitch.setAttribute('role', 'group');
  basemapSwitch.setAttribute('aria-label', 'Which basemap to show');
  for (const [value, label] of [
    ['simple', 'Simple'],
    ['satellite', 'Satellite'],
    ['geography', 'Geography'],
  ]) {
    const button = el('button', null, label);
    button.type = 'button';
    button.dataset.basemap = value;
    basemapSwitch.append(button);
  }
  basemaps.append(basemapSwitch);

  // Filled on selection. Hidden until there is something to say, so the map is never covered
  // by an empty box.
  const facts = el('div', 'card facts-card');
  facts.hidden = true;

  const status = el('p', 'map-status');
  status.id = 'map-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');

  cards.append(boundaries, basemaps, facts, status);

  const zoom = el('div', 'map-zoom');
  zoom.id = 'map-zoom';
  const zoomIn = el('button', null, '+');
  zoomIn.type = 'button';
  zoomIn.id = 'map-zoom-in';
  zoomIn.classList.add('map-zoom-in');
  zoomIn.setAttribute('aria-label', 'Zoom in');
  const zoomOut = el('button', null, '−');
  zoomOut.type = 'button';
  zoomOut.id = 'map-zoom-out';
  zoomOut.classList.add('map-zoom-out');
  zoomOut.setAttribute('aria-label', 'Zoom out');
  zoom.append(zoomIn, zoomOut);

  // Every basemap source requires attribution, so this is a licence term and not a nicety.
  // Filled by the base map, which owns the credit for whichever basemap is showing.
  const attrib = el('p', 'map-attrib');
  attrib.id = 'map-attrib';

  root.append(canvas, cards, zoom, attrib);
  return { root, canvas, facts, status };
}

export default {
  id: 'showup-map',
  name: 'Map',
  icon: 'globe',
  singleton: true,
  width: 820,
  height: 560,
  status: true,
  styles: ['app.css'],

  mount({ win }) {
    const parts = build();
    // Stashed on the window rather than in a module variable: a module variable is shared by
    // every window of an app, and the second window would drive the first one's map.
    win.app = parts;
    win.setStatus('Loading the map…');
    return parts.root;
  },

  /* The map, built once the subtree is laid out.
   *
   * In `ready` and not `mount` because MapLibre measures its container on creation. During
   * `mount` the nodes are not in the document and every rect is zero, so the map would size
   * itself to 0x0, report its style loaded, fetch every tile and draw nothing.
   */
  async ready({ win }) {
    const { root, canvas, facts, status } = win.app;

    let geometry;
    try {
      const [council, board, lookup, headlines] = await Promise.all([
        fetchJson(LAYERS.council.url),
        fetchJson(LAYERS.board.url),
        // Names and headline facts are both niceties: a failure in either must not cost the
        // map, which still draws and is still hit-testable.
        fetchJson('/data/showup/lookup.json').catch(() => null),
        fetchJson('/data/showup/preview.json').catch(() => null),
      ]);
      geometry = { council, board };
      if (lookup) names = indexNames(lookup);
      preview = headlines;
    } catch (error) {
      // Named, not swallowed: a reader who sees nothing should be told whose fault it is.
      win.setStatus(`The map could not load (${error.message}).`);
      status.textContent = `The map could not load (${error.message}).`;
      return;
    }

    const { createBaseMap } = await import('../../shared/map/basemap.mjs');

    //: Assigned by the first `draw` and read back by every later one. `draw` re-runs from
    //: scratch on each basemap change, and what the reader selected is the one thing that
    //: must survive it.
    let view = null;

    const base = await createBaseMap({
      // `root` is the contract now: the base map finds its status line, zoom pair, credit
      // and basemap switcher inside it, by class. The version this app was written against
      // looked each one up from `document` by id, which is why `singleton` was load-bearing
      // here — two windows would both have driven the first one's camera.
      root,
      container: canvas,
      center: CENTER,
      zoom: ZOOM,
      // The boundaries are the content; tilting or spinning them helps nobody read one.
      tilt: false,

      /* Everything that touches the style happens here, not after `createBaseMap` resolves.
       *
       * `draw` is called with the style loaded; the promise resolving does not mean it is.
       * `setMaxZoom`, `setLayoutProperty` and `setFeatureState` all throw "Style is not done
       * loading" off it, which is exactly what happened when this was written the other way
       * — webxp caught it and drew an error window instead of a map.
       */
      draw(map, ctx) {
        for (const name of Object.keys(LAYERS)) {
          addFillLayer(map, LAYERS[name], geometry[name], ctx.beforeId);
        }
        for (const name of Object.keys(LAYERS)) {
          addLineLayer(map, LAYERS[name], ctx.beforeId, ctx.basemap);
        }
        // Clips the district tint to the coastline so a district does not wash over water.
        // Absent on a raster basemap, which has no water layer to clone.
        const mask = waterMaskLayer(map.getStyle());
        if (mask) map.addLayer(mask, ctx.beforeId);
        repairShieldFilters(map);

        if (!view) {
          // The one-time setup, invoked from the first `draw` rather than beside it: the View
          // and the hit-testing both need the layers to exist, and `draw` is where they come
          // into being. The layers are rebuilt on every basemap change; these bindings are
          // properties of the window.
          view = new View({ map, win, facts });
          view.show('council');
          for (const name of Object.keys(LAYERS)) {
            LAYERS[name].button.addEventListener('click', () => view.show(name));
          }
          bindSelection(map, view);
        } else {
          view.repaint();
        }
      },
    });

    if (!base) {
      win.setStatus('The map could not start.');
      return;
    }

    const { map } = base;
    // The handle for the devtools console and for the browser gates, which read geometry
    // rather than trusting a screenshot.
    win.base = base;

    // MapLibre holds a WebGL context and several workers. A window closed without this leaks
    // both, and a few open-and-close cycles is enough to lose the context on some drivers.
    win.onClose = () => map.remove();
  },
};

/** Names keyed the way the geometry is: districts by number, boards by `boro_cd`. */
function indexNames(lookup) {
  const district = new Map();
  const board = new Map();
  // The shapes are `{n, hoods}` and `{code, label, hoods}`, read off the built file rather
  // than remembered: `n` is not `number`, which is the kind of guess that produces a card
  // full of blanks and no error.
  for (const entry of lookup.districts || []) {
    district.set(String(entry.n), { hoods: entry.hoods || null });
  }
  for (const entry of lookup.boards || []) {
    board.set(String(entry.code), { label: entry.label || null, hoods: entry.hoods || null });
  }
  return { district, board };
}

/** Which layer is showing, and which feature in it is selected. */
class View {
  constructor({ map, win, facts }) {
    this.map = map;
    this.win = win;
    this.facts = facts;
    this.name = 'council';
    this.selected = null;
  }

  get layer() {
    return LAYERS[this.name];
  }

  show(name) {
    if (!LAYERS[name]) return;
    this.clear();
    this.name = name;
    for (const other of Object.keys(LAYERS)) {
      LAYERS[other].button.setAttribute('aria-pressed', String(other === name));
    }
    this.repaint();
    this.win.setStatus(`Showing ${this.layer.noun}s. Select one for its details.`);
  }

  /** Put back what lives on the style rather than on this object.
   *
   * Layer visibility and feature state are both properties of a loaded style, so a basemap
   * change takes them with it. The zoom ceiling too: `setBasemap` installs the preset's own,
   * which is 17 on geography and 19 on satellite.
   */
  repaint() {
    this.map.setMaxZoom(MAX_ZOOM);
    for (const other of Object.keys(LAYERS)) {
      const visibility = other === this.name ? 'visible' : 'none';
      this.map.setLayoutProperty(`${LAYERS[other].source}-fill`, 'visibility', visibility);
      this.map.setLayoutProperty(`${LAYERS[other].source}-line`, 'visibility', visibility);
    }
    if (this.selected !== null) {
      this.map.setFeatureState(
        { source: this.layer.source, id: this.selected },
        { selected: true },
      );
    }
  }

  clear() {
    if (this.selected !== null) {
      this.map.setFeatureState(
        { source: this.layer.source, id: this.selected },
        { selected: false },
      );
    }
    this.selected = null;
    this.facts.hidden = true;
    this.facts.replaceChildren();
  }

  select(id) {
    this.clear();
    this.selected = id;
    this.map.setFeatureState({ source: this.layer.source, id }, { selected: true });

    const rows = this.layer
      .sections(id)
      // A fact the City does not publish is left out rather than shown as an empty row: a
      // blank beside a label reads as "nobody holds this seat".
      .filter(([, value]) => value)
      .map(([field, value]) => [field, value]);

    this.facts.hidden = false;
    this.facts.replaceChildren(
      rows.length
        ? group(this.layer.label(id), { columns: ['Field', 'Value'], rows })
        : group(this.layer.label(id), [
            `No headline facts are published for this ${this.layer.noun}.`,
          ]),
    );
    this.win.setStatus(`${this.layer.label(id)} selected.`);
  }
}

/* Hover and click, hit-tested against the visible fill only.
 *
 * Both layers are in the style at once and only one is visible, so the query names the fill
 * of the layer that is showing rather than whatever is under the pointer.
 */
function bindSelection(map, view) {
  let hovered = null;

  const setHover = (id) => {
    if (hovered !== null && hovered !== id) {
      map.setFeatureState({ source: view.layer.source, id: hovered }, { hovered: false });
    }
    hovered = id;
    if (id !== null) {
      map.setFeatureState({ source: view.layer.source, id }, { hovered: true });
    }
  };

  map.on('mousemove', (event) => {
    const hit = map.queryRenderedFeatures(event.point, {
      layers: [`${view.layer.source}-fill`],
    })[0];
    map.getCanvas().style.cursor = hit ? 'pointer' : '';
    setHover(hit ? String(hit.id ?? hit.properties[view.layer.key]) : null);
  });

  map.on('mouseout', () => setHover(null));

  map.on('click', (event) => {
    const hit = map.queryRenderedFeatures(event.point, {
      layers: [`${view.layer.source}-fill`],
    })[0];
    if (!hit) {
      view.clear();
      return;
    }
    const id = hit.id ?? hit.properties[view.layer.key];
    if (id !== undefined && id !== null) view.select(String(id));
  });
}

/* Why nothing here moves the camera on selection.
 *
 * `/map/` had a `panIntoView` that fitted the selection into the part of the map its sliding
 * detail panel did not cover, with three regression pins on it — no `cameraForBounds`, a
 * non-zero camera padding, and panning the centroid rather than the bounding box. That page,
 * the panel and the three pins were all deleted on 2026-11-08, and this program never
 * carried the behaviour.
 *
 * That is deliberate, and it was checked rather than assumed. The pan existed because the
 * panel slid in *after* the click and could land on top of the place the reader had just
 * pressed. The facts card cannot do that: it is already on screen in a fixed place before
 * any selection — measured at canvas x 8–392, y 157–291 of an 818x507 map — and it takes the
 * pointer (`.map-cards > * { pointer-events: auto }`), so a click inside its box never
 * reaches the map in the first place. The point a reader presses is therefore always one
 * they can still see, which is the whole of what the pan was for.
 *
 * A pan written for the old reason was tried here first and measured as unreachable: its
 * trigger is a click behind the card, and the card consumes those clicks.
 *
 * What is *not* preserved: part of a selected district's highlight can sit behind the card.
 * That is a different complaint from the one the pins were about, and the fill is not what
 * the reader is reading — the card over it carries the facts.
 */

