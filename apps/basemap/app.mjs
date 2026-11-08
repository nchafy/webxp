/* Base map — the 30 Day Map Challenge's base map, ported into a window.
 *
 * This app exists as the worked example of integrating a project that already exists: it
 * brings its own vendored MapLibre, its own stylesheet and its own module, and the folder is
 * the unit. `docs/apps.md` lists what had to change, which was less about the map and more
 * about the page it used to assume.
 *
 * MapLibre is ~1.1 MB and is imported by `basemap.mjs` only once a window opens, so an
 * unopened Base map costs the desktop one small module.
 */

import { group } from '../../src/widgets.mjs';

const SVG = 'http://www.w3.org/2000/svg';

//: Where the map opens. New York at z10 because all three basemaps have good coverage there,
//: so switching between them on load shows three real maps rather than three grey squares.
const CENTER = [-73.95, 40.7];
const ZOOM = 10;

const CLICKED = 'clicked-point';

const BASEMAP_BUTTONS = [
  ['simple', 'Simple'],
  ['satellite', 'Satellite'],
  ['geography', 'Geography'],
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

/**
 * The compass: indicator and control in one, as a compass has always been.
 *
 * The rose turns with the map; pressing it faces north again. The `g` is rotated through its
 * SVG `transform` attribute rather than a style, so the needle keeps working under a strict
 * `style-src`.
 */
function compass() {
  const button = el('button', 'map-compass');
  button.type = 'button';
  button.setAttribute('aria-label', 'Facing north. Press to reset, arrow keys to turn');

  const face = svg('svg', { viewBox: '0 0 24 24', width: 34, height: 34, 'aria-hidden': 'true', focusable: 'false' });
  face.append(svg('circle', { cx: 12, cy: 12, r: 10.5, fill: 'none', stroke: '#8a8a6a', 'stroke-width': 1 }));

  const rose = svg('g', { class: 'map-compass-rose', transform: 'rotate(0 12 12)' });
  // The red half is north, which needs no letter: it is the one convention every compass
  // rose has kept.
  rose.append(svg('polygon', { points: '12,2.5 15.2,12 12,9.8 8.8,12', fill: '#c8341a' }));
  rose.append(svg('polygon', { points: '12,21.5 8.8,12 12,14.2 15.2,12', fill: '#6b6b55' }));
  face.append(rose);
  face.append(svg('circle', { cx: 12, cy: 12, r: 1.1, fill: '#3a3a3a' }));

  button.append(face);
  return button;
}

/** The whole subtree this app owns. Every control `basemap.mjs` looks for is in here. */
function build() {
  const root = el('div', 'basemap-root');
  root.append(el('div', 'map'));

  const overlays = el('div', 'overlays');

  const card = el('div', 'card');
  card.append(el('h2', null, 'Basemap'));

  const layers = el('div', 'layer-switch');
  layers.setAttribute('role', 'group');
  layers.setAttribute('aria-label', 'Which basemap to show');
  for (const [value, label] of BASEMAP_BUTTONS) {
    const button = el('button', null, label);
    button.type = 'button';
    button.dataset.basemap = value;
    button.setAttribute('aria-pressed', String(value === 'simple'));
    layers.append(button);
  }
  card.append(layers);
  card.append(el('p', 'hint', 'Click the map to read a coordinate.'));

  // The tilt gesture is ctrl-drag on a desktop, which nobody finds, so there is a button for
  // it. It only moves the camera: the 3D buildings follow the angle, not the button.
  const tiltRow = el('div', 'tilt-row');
  const tilt = el('button', 'map-tilt', 'Tilt');
  tilt.type = 'button';
  tilt.setAttribute('aria-pressed', 'false');
  tiltRow.append(tilt, el('span', 'tilt-note', '3D buildings show from an angle, past zoom 14.'));
  card.append(tiltRow);

  overlays.append(card);

  // The position card, filled on click. Hidden until there is something to say.
  const position = el('div', 'card position-card');
  position.hidden = true;
  overlays.append(position);

  // Under the control card, in the same flex column, so it can never land on top of a
  // control whose size changed.
  const status = el('p', 'map-status');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  overlays.append(status);

  root.append(overlays);

  // A map control belongs on the map, not in a card. The visible glyph is a symbol, so each
  // button carries an aria-label: "+" is not a name. The minus is U+2212, not a hyphen, so
  // it matches the plus optically.
  const zoom = el('div', 'map-zoom');
  const zoomIn = el('button', 'map-zoom-in', '+');
  zoomIn.type = 'button';
  zoomIn.setAttribute('aria-label', 'Zoom in');
  const zoomOut = el('button', 'map-zoom-out', '−');
  zoomOut.type = 'button';
  zoomOut.setAttribute('aria-label', 'Zoom out');
  zoom.append(zoomIn, zoomOut, compass());
  root.append(zoom);

  // Every basemap source requires attribution, so this is a licence term and not a nicety:
  // nothing is allowed to cover it. Filled by basemap.mjs, which owns the per-source credit.
  root.append(el('p', 'map-attrib'));

  return { root, position };
}

export default {
  id: 'basemap',
  name: 'Base map',
  icon: 'globe',
  singleton: true,
  onDesktop: true,
  width: 760,
  height: 520,
  status: true,
  styles: ['app.css'],

  mount({ win }) {
    const { root, position } = build();
    win.setStatus('Click the map to read a coordinate.');
    // Stashed on the window, not in a module variable: two windows of one app would share a
    // module variable and the second would drive the first one's map.
    win.app = { root, position };
    return root;
  },

  /**
   * The map, built once the subtree is laid out.
   *
   * This is in `ready` rather than `mount` because MapLibre measures its container on
   * creation. During `mount` the nodes have no box yet — what `mount` returns is what gets
   * inserted — so the map would size itself to 0x0, report its style loaded, fetch every
   * tile and draw nothing.
   */
  async ready({ win }) {
    const { root, position } = win.app;

    // Imported here rather than at module scope, so the 1.1 MB of MapLibre behind it is paid
    // when a window opens and not when the desktop starts.
    const { createBaseMap } = await import('./basemap.mjs');

    //: The last clicked position, kept outside `draw` because `draw` is re-run from scratch
    //: on every basemap change and must be able to put the point back.
    let clicked = null;
    const clickedData = () => ({
      type: 'FeatureCollection',
      features: clicked
        ? [{ type: 'Feature', geometry: { type: 'Point', coordinates: clicked }, properties: {} }]
        : [],
    });

    const base = await createBaseMap({
      root,
      center: CENTER,
      zoom: ZOOM,

      draw(map, ctx) {
        map.addSource(CLICKED, { type: 'geojson', data: clickedData() });
        map.addLayer({
          id: `${CLICKED}-halo`,
          type: 'circle',
          source: CLICKED,
          paint: {
            // Two circles rather than one with a stroke: a stroke scales with the circle and
            // this ring has to stay readable over imagery, where a thin line disappears.
            'circle-radius': 11,
            'circle-color': 'rgba(232, 67, 31, 0.25)',
            'circle-stroke-width': 2,
            // White over satellite, where the era's navy would vanish into the water; the
            // dark outline reads on the two pale basemaps.
            'circle-stroke-color': ctx.basemap === 'satellite' ? '#ffffff' : '#13264a',
          },
        }, ctx.beforeId);
        map.addLayer({
          id: CLICKED,
          type: 'circle',
          source: CLICKED,
          paint: { 'circle-radius': 4, 'circle-color': '#e8431f' },
        }, ctx.beforeId);
      },
    });

    if (!base) {
      // `createBaseMap` already wrote the reason into the status line inside the window.
      win.setStatus('The map could not start.');
      return;
    }

    const { map } = base;
    // On the window rather than on `window`: two windows of one app would otherwise fight
    // over one global. This is the handle for the devtools console and for the checks.
    win.base = base;

    const showCoordinate = (lngLat) => {
      position.hidden = false;
      position.replaceChildren(
        group('Coordinate', {
          columns: ['Field', 'Value'],
          rows: [
            // Six decimal places is about 0.1 m at this latitude: past that the figure claims
            // a precision no basemap here has.
            ['Latitude', lngLat.lat.toFixed(6)],
            ['Longitude', lngLat.lng.toFixed(6)],
            ['Zoom', map.getZoom().toFixed(2)],
            ['Basemap', base.basemap],
          ],
        }),
      );
      win.setStatus(`${lngLat.lat.toFixed(4)}, ${lngLat.lng.toFixed(4)} — read off the camera, not a geocoder.`);
    };

    map.on('click', (event) => {
      clicked = [event.lngLat.lng, event.lngLat.lat];
      const source = map.getSource(CLICKED);
      if (source) source.setData(clickedData());
      showCoordinate(event.lngLat);
    });

    // MapLibre holds a WebGL context and several workers. A window closed without this leaks
    // both, and a few open-and-close cycles is enough to lose the context on some drivers.
    win.onClose = () => map.remove();
  },
};
