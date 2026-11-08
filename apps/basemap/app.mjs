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

//: Where the map opens. New York at z10 because all three basemaps have good coverage there,
//: so switching between them on load shows three real maps rather than three grey squares.
const CENTER = [-73.95, 40.7];
const ZOOM = 10;

const CLICKED = 'clicked-point';


function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}



/* The subtree this app owns: the shared map chrome, plus the one card it adds.
 *
 * Everything that was here — the switcher, the Tilt row, the status line, the zoom pair,
 * the compass, the credit — is now `buildMapChrome` in `src/map/chrome.mjs`, because every
 * map app needs exactly that subtree and `createBaseMap` finds its controls by class
 * inside it. Thirty-one copies of eighty lines was the alternative.
 */
async function build() {
  // Imported here rather than at module scope. Every `app.mjs` is read at startup to build
  // the Start menu, so a module-scope import of the map layer is startup cost for a desktop
  // where no map has been opened.
  const [{ buildMapChrome }, { BASEMAPS }] = await Promise.all([
    import('../../src/map/chrome.mjs'),
    import('../../src/map/basemap.mjs'),
  ]);
  const chrome = buildMapChrome({
    basemaps: BASEMAPS,
    hint: 'Click the map to read a coordinate.',
  });
  // The position card, filled on click. Hidden until there is something to say.
  const position = el('div', 'card position-card');
  position.hidden = true;
  chrome.cards.append(position);
  return { root: chrome.root, position };
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
  styles: ['../../src/map/map.css', 'app.css'],

  async mount({ win }) {
    const { root, position } = await build();
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
    const { createBaseMap } = await import('../../src/map/basemap.mjs');

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
