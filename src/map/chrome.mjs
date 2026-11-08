/* The standard map furniture, built once and used by every map app.
 *
 * `createBaseMap` wires controls it finds by class inside the root it is handed. Which
 * means every app that wants a map has to build that same subtree: a container, a basemap
 * switcher, a Tilt button, a status line, a zoom pair, a compass, a credit. That is about
 * eighty lines, and it was about to be copied thirty-one times.
 *
 * So it lives here, beside the engine that reads it. An app calls `buildMapChrome()`, gets
 * the root to return from `mount`, and appends its own cards to `chrome.cards`.
 *
 *     const chrome = buildMapChrome();
 *     chrome.cards.append(myCard);
 *     return chrome.root;          // from mount
 *     // then in ready:
 *     const base = await createBaseMap({ root: chrome.root, center, zoom, draw });
 *
 * The class names are the engine's contract, not decoration. `basemap.mjs` looks for
 * `.map`, `.map-status`, `.map-attrib`, `.map-compass`, `.map-compass-rose`,
 * `.map-zoom-in`, `.map-zoom-out`, `.map-tilt` and `[data-basemap]`. Renaming one here
 * silently unwires that control: the map still draws, the button just stops doing anything.
 */

/* This module imports nothing, and that is deliberate.
 *
 * It began by importing `BASEMAPS` from `basemap.mjs` so the switcher could not disagree
 * with the presets. That cost the whole desktop: `apps/basemap/app.mjs` imported this at
 * module scope, every `app.mjs` is imported at startup so the Start menu can be built, and
 * so the map engine became startup work for a desktop where nobody had opened a map.
 *
 * It surfaced as webxp's own `scaffold a new app` check failing — a freshly scaffolded app
 * missing from the loaded list — which is a long way from the cause and exactly the failure
 * `docs/apps.md` § Keeping an app cheap describes.
 *
 * So the presets arrive as an argument and the caller imports them when a window opens. */

const SVG = 'http://www.w3.org/2000/svg';

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

  const face = svg('svg', {
    viewBox: '0 0 24 24',
    width: 34,
    height: 34,
    'aria-hidden': 'true',
    focusable: 'false',
  });
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

/**
 * Build the subtree a map app returns from `mount`.
 *
 * @param {object} [opts]
 * @param {object} [opts.basemaps]    The `BASEMAPS` object, for the switcher's buttons. Pass
 *        it so this module needs no import; omit it and the switcher is left empty, which is
 *        what a caller that has not loaded the engine yet should get rather than a guess.
 * @param {boolean} [opts.tilt=true]  A day that must stay flat and north-up passes `false`,
 *        which removes the Tilt button and the compass rather than leaving them inert. A
 *        compass that indicates something you cannot change is furniture.
 * @param {string} [opts.hint]        A line of copy under the switcher, for a day that wants
 *        to say what to do first.
 * @returns {{root: HTMLElement, cards: HTMLElement, status: HTMLElement, card: HTMLElement}}
 */
export function buildMapChrome(opts = {}) {
  const tilt = opts.tilt !== false;

  const root = el('div', 'map-root');
  root.append(el('div', 'map'));

  const overlays = el('div', 'overlays');

  const card = el('div', 'card');
  card.append(el('h2', null, 'Basemap'));

  const layers = el('div', 'layer-switch');
  layers.setAttribute('role', 'group');
  layers.setAttribute('aria-label', 'Which basemap to show');
  // From `BASEMAPS` rather than a list written again here: the presets, their labels and
  // their credits have one definition, and a fourth one should appear in the switcher by
  // being added there.
  for (const [value, preset] of Object.entries(opts.basemaps || {})) {
    const button = el('button', null, preset.label);
    button.type = 'button';
    button.dataset.basemap = value;
    button.setAttribute('aria-pressed', 'false');
    layers.append(button);
  }
  card.append(layers);

  if (opts.hint) card.append(el('p', 'hint', opts.hint));

  if (tilt) {
    // The tilt gesture is ctrl-drag on a desktop, which nobody finds, so there is a button
    // for it. It only moves the camera: the 3D buildings follow the angle, not the button.
    const tiltRow = el('div', 'tilt-row');
    const tiltButton = el('button', 'map-tilt', 'Tilt');
    tiltButton.type = 'button';
    tiltButton.setAttribute('aria-pressed', 'false');
    tiltRow.append(
      tiltButton,
      el('span', 'tilt-note', '3D buildings show from an angle, past zoom 14.')
    );
    card.append(tiltRow);
  }

  overlays.append(card);

  // Where an app appends its own cards: after the basemap card, before the status line, so
  // the live region can never end up on top of a control whose size changed.
  const cards = el('div', 'overlay-cards');
  overlays.append(cards);

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
  zoom.append(zoomIn, zoomOut);
  if (tilt) zoom.append(compass());
  root.append(zoom);

  // Every basemap source requires attribution, so this is a licence term and not a nicety:
  // nothing is allowed to cover it. Filled by `basemap.mjs`, which owns the per-source
  // credit. It is the last child of the root and outside `.overlays` on purpose — the
  // overlay column is `inset: 0`, so a credit inside it would be coverable.
  root.append(el('p', 'map-attrib'));

  return { root, overlays, cards, card, status };
}
