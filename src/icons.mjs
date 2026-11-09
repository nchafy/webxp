/* The icon set, drawn rather than fetched.
 *
 * Inline SVG built with DOM calls: no image requests, nothing for an `img-src` policy to
 * allow, and no `innerHTML`. Every icon is drawn in a 16x16 box and scaled by the `width`
 * and `height` attributes, so one definition serves the 16px title bar, the 16px taskbar
 * button and the 32px desktop icon.
 *
 * Shapes are deliberately flat. XP's own icons were painted 48x48 at three light angles;
 * imitating that at 16px produces mud, and a legible silhouette is what the size can hold.
 */

const NS = 'http://www.w3.org/2000/svg';

/**
 * Luna's icon palette, reduced to the few fills these shapes need.
 *
 * A presentation attribute is parsed as CSS, so `var()` resolves in it and a colour the
 * theme already names is taken from the theme rather than retyped. The three literals in
 * this table are shades `theme.css` has no token for: `line` is the mid grey these outlines
 * want, between `--rule` and `--sunken`; `pale` is the globe's meridians against
 * `--title-b`; `amber` is the one warm colour in the set and belongs to nothing else. Three
 * more are justified where the shapes below use them.
 */
const C = {
  paper: 'var(--panel)',
  ink: 'var(--muted)',
  line: '#7a8a9a',
  blue: 'var(--title-b)',
  deep: 'var(--title-a)',
  pale: '#dfe8f5',
  green: 'var(--start-a)',
  amber: '#e8a317',
  shade: 'var(--rule)',
};

//: Each entry is a list of `[tag, attributes]` pairs, painted in order.
const SHAPES = {
  /** The generic window: a frame with a blue title bar. The default. */
  window: [
    ['rect', { x: 1.5, y: 2.5, width: 13, height: 11, fill: C.paper, stroke: C.deep }],
    ['rect', { x: 2, y: 3, width: 12, height: 3, fill: C.blue }],
    ['path', { d: 'M3 8h8M3 10h8M3 12h5', stroke: C.line, 'stroke-width': 1 }],
  ],

  /** A text document: a page with a folded corner. */
  document: [
    ['path', { d: 'M3.5 1.5h6l3 3v10h-9z', fill: C.paper, stroke: C.line }],
    ['path', { d: 'M9.5 1.5v3h3', fill: 'none', stroke: C.line }],
    ['path', { d: 'M5 7h6M5 9h6M5 11h4', stroke: C.ink, 'stroke-width': 1 }],
  ],

  /** Notepad: the same page, with the pencil that says it is editable. */
  notepad: [
    ['path', { d: 'M3.5 1.5h8v13h-8z', fill: C.paper, stroke: C.line }],
    ['path', { d: 'M5 5h5M5 7h5M5 9h3', stroke: C.ink, 'stroke-width': 1 }],
    ['path', { d: 'M9 14.2l1-3 3.6-3.6 2 2L12 13z', fill: C.amber, stroke: C.ink }],
  ],

  /** Display properties: a CRT on its stand. */
  display: [
    ['rect', { x: 1.5, y: 2.5, width: 13, height: 9, rx: 1, fill: C.deep, stroke: C.ink }],
    ['rect', { x: 3, y: 4, width: 10, height: 6, fill: C.blue }],
    ['path', { d: 'M6.5 11.5v2h3v-2M4 13.5h8', stroke: C.shade, 'stroke-width': 1.4, fill: 'none' }],
  ],

  /** A folded paper map, for an app that draws one.
   *
   * Distinct from `globe` on purpose: a map app and a hosted web page are different things
   * and were both wearing the globe. The folds are what make this read as a map rather than
   * as a document at 16 px, so they survive the small size even though they carry no
   * information. */
  map: [
    ['path', { d: 'M1.5 4l4.5-2 4 2 4.5-2v10l-4.5 2-4-2-4.5 2z', fill: C.paper, stroke: C.line }],
    ['path', { d: 'M6 2.2v10M10 4v10', stroke: C.line }],
    // The route, which is the one coloured mark and the reason the shape reads as a map of
    // somewhere rather than a blank sheet.
    ['path', {
      d: 'M3 10c1.6-.6 2.2-2.6 4-2.2 1.8.4 2.4 2 4.5 1.2',
      fill: 'none',
      // The close box's red. The palette has no token for it, so this is one of four copies
      // — `window.css` has two and `shared/map/chrome.mjs` the compass needle — that have to
      // be edited together until one exists.
      stroke: '#c8341a',
      'stroke-width': 1.4,
    }],
  ],

  /** A globe, for anything that is about the world. */
  globe: [
    ['circle', { cx: 8, cy: 8, r: 6.5, fill: C.blue, stroke: C.deep }],
    ['path', {
      d: 'M1.5 8h13M8 1.5c2.3 1.9 2.3 11.1 0 13M8 1.5C5.7 3.4 5.7 12.6 8 14.5',
      fill: 'none',
      stroke: C.pale,
    }],
  ],

  /** A folder, for a list of other things. The outline is `amber` darkened to hold against
   *  its own fill, which no token covers; the fold is the lit edge of a bevel. */
  folder: [
    ['path', { d: 'M1.5 4.5h5l1.5 2h6.5v8h-13z', fill: C.amber, stroke: '#9a6a00' }],
    ['path', { d: 'M1.5 6.5h13', stroke: 'var(--raised)', opacity: 0.6 }],
  ],

  /** The information bubble, XP's own "read this first". */
  info: [
    ['circle', { cx: 8, cy: 8, r: 6.5, fill: C.paper, stroke: C.blue }],
    ['circle', { cx: 8, cy: 4.8, r: 1.1, fill: C.blue }],
    ['path', { d: 'M8 7v5', stroke: C.blue, 'stroke-width': 2 }],
  ],

  /** A console: a dark screen with a prompt on it. The screen is near-black, which the
   *  palette has no token for — `--ink` is the text colour and reads as a shape, not a
   *  surface. */
  terminal: [
    ['rect', { x: 1.5, y: 2.5, width: 13, height: 11, rx: 1, fill: '#0c0c0c', stroke: C.ink }],
    ['path', { d: 'M4 6.2l2 1.8-2 1.8', stroke: C.green, 'stroke-width': 1.4 }],
    ['path', { d: 'M7.5 10.5h4.5', stroke: C.green, 'stroke-width': 1.2 }],
  ],

  /** A check, for anything that reports a result. */
  check: [
    ['circle', { cx: 8, cy: 8, r: 6.5, fill: C.paper, stroke: C.green }],
    ['path', { d: 'M4.5 8.5l2.5 2.5 4.5-5.5', fill: 'none', stroke: C.green, 'stroke-width': 2 }],
  ],
};

export const ICON_NAMES = Object.freeze(Object.keys(SHAPES));

/**
 * App filetypes: what a program *is*, and the icon that follows from it.
 *
 * An app declares `filetype: 'map'` rather than `icon: 'map'`, so that thirty map apps share one
 * answer to "which icon" and changing it is one edit here instead of thirty. Explicit `icon`
 * still wins, for the app that is genuinely an exception.
 *
 * Adding a filetype is one entry.
 */
export const FILETYPES = Object.freeze({
  map: { icon: 'map' },
  document: { icon: 'document' },
  notes: { icon: 'notepad' },
  folder: { icon: 'folder' },
  settings: { icon: 'display' },
  info: { icon: 'info' },
  web: { icon: 'globe' },
  tool: { icon: 'check' },
});

export const FILETYPE_NAMES = Object.freeze(Object.keys(FILETYPES));

/**
 * The icon an app should use.
 *
 * Order is deliberate: an explicit `icon` is an override and wins, then the filetype, then the
 * generic window. A spec that sets neither is not an error — it gets a window, which is what
 * it is.
 *
 * @param {{icon?: string, filetype?: string}} spec
 * @returns {string} A key of `SHAPES`.
 */
export function iconFor(spec) {
  if (spec && spec.icon) return spec.icon;
  if (spec && spec.filetype && FILETYPES[spec.filetype]) return FILETYPES[spec.filetype].icon;
  return 'window';
}

/**
 * One SVG element, built with DOM calls and given its attributes.
 *
 * The primitive the icon set and `demo/clippy.mjs` both go through: a stroked shape with no
 * explicit fill paints black, which is never what an outline wants, so the default is
 * applied here rather than remembered at each call. `shared/map/chrome.mjs` still has a
 * builder of its own, without the default.
 *
 * @param {string} tag
 * @param {Record<string, string|number>} attrs
 * @returns {SVGElement}
 */
export function svgNode(tag, attrs) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (attrs.stroke && !('fill' in attrs)) node.setAttribute('fill', 'none');
  if (attrs.stroke && !('stroke-width' in attrs)) node.setAttribute('stroke-width', '1');
  return node;
}

/**
 * Build one icon.
 *
 * @param {string} name   A key of `SHAPES`. An unknown name falls back to `window` rather
 *                        than returning nothing, so a typo in a program spec shows up as
 *                        the wrong picture instead of a hole in the title bar.
 * @param {number} [size] Rendered size in CSS pixels.
 * @returns {SVGSVGElement}
 */
export function icon(name, size = 16) {
  const svg = svgNode('svg', {
    viewBox: '0 0 16 16',
    width: size,
    height: size,
    // Decoration: the thing the icon sits next to always carries the name.
    'aria-hidden': 'true',
    focusable: 'false',
  });
  svg.classList.add('xp-icon');

  for (const [tag, attrs] of SHAPES[name] || SHAPES.window) svg.append(svgNode(tag, attrs));
  return svg;
}
