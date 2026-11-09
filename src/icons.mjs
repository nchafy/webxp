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

/** Luna's icon palette, reduced to the few fills these shapes need. */
const C = {
  paper: '#fcfcfa',
  ink: '#3a3a3a',
  line: '#7a8a9a',
  blue: '#3a6ea5',
  deep: '#0a246a',
  pale: '#dfe8f5',
  green: '#51a800',
  amber: '#e8a317',
  shade: '#919b9c',
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

  /** A folder, for a list of other things. */
  folder: [
    ['path', { d: 'M1.5 4.5h5l1.5 2h6.5v8h-13z', fill: C.amber, stroke: '#9a6a00' }],
    ['path', { d: 'M1.5 6.5h13', stroke: '#fff', opacity: 0.6 }],
  ],

  /** The information bubble, XP's own "read this first". */
  info: [
    ['circle', { cx: 8, cy: 8, r: 6.5, fill: C.paper, stroke: C.blue }],
    ['circle', { cx: 8, cy: 4.8, r: 1.1, fill: C.blue }],
    ['path', { d: 'M8 7v5', stroke: C.blue, 'stroke-width': 2 }],
  ],

  /** A console: a dark screen with a prompt on it. */
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
 * Adding a filetype is one entry. The `label` is not rendered anywhere yet; it exists so a filetype
 * can be named in a UI — a grouped Start menu, a folder of everything of one filetype — without
 * that UI inventing its own vocabulary.
 */
export const FILETYPES = Object.freeze({
  map: { icon: 'map', label: 'Map' },
  document: { icon: 'document', label: 'Document' },
  notes: { icon: 'notepad', label: 'Notes' },
  folder: { icon: 'folder', label: 'Folder' },
  settings: { icon: 'display', label: 'Settings' },
  info: { icon: 'info', label: 'Information' },
  web: { icon: 'globe', label: 'Web page' },
  tool: { icon: 'check', label: 'Tool' },
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
 * Build one icon.
 *
 * @param {string} name   A key of `SHAPES`. An unknown name falls back to `window` rather
 *                        than returning nothing, so a typo in a program spec shows up as
 *                        the wrong picture instead of a hole in the title bar.
 * @param {number} [size] Rendered size in CSS pixels.
 * @returns {SVGSVGElement}
 */
export function icon(name, size = 16) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  // Decoration: the thing the icon sits next to always carries the name.
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('xp-icon');

  for (const [tag, attrs] of SHAPES[name] || SHAPES.window) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    // A stroked shape with no explicit fill paints black by default, which is never what
    // these outlines want.
    if (attrs.stroke && !('fill' in attrs)) node.setAttribute('fill', 'none');
    if (attrs.stroke && !('stroke-width' in attrs)) node.setAttribute('stroke-width', '1');
    svg.append(node);
  }
  return svg;
}
