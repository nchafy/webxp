/* Day 02 — Lines: how much of a recorded run is receiver noise.
 *
 * Every run is drawn twice, as the watch recorded it and snapped to the OSM network. The
 * recording is longer, because jitter is indistinguishable from movement and gets counted
 * as distance covered. The slider is the argument: it simplifies one trace at a tolerance
 * the reader chooses, and the distance falls as the wobble comes out. There is no setting
 * at which the number is right — which is the point.
 *
 * `lines.mjs` is the day. This file is the spec and the mount/ready split, because that
 * split is the one thing about being an app rather than a page that this day has to know.
 *
 * ## Why nothing heavy is imported here
 *
 * Every `app.mjs` is read at startup so the Start menu can be built. A module-scope import
 * of the map layer would make MapLibre's 1.1 MB, this day's 1400 lines and the whole shared
 * map stack startup cost for a desktop where nobody opened a map. `docs/apps.md` § Keeping
 * an app cheap has the symptom it produced when it was got wrong: a newly scaffolded app
 * missing from the loaded list, which is a long way from the cause.
 */

export default {
  id: 'day-02-lines',
  name: 'Day 02 — Lines',
  filetype: 'map',
  singleton: true,
  // No desktop icon: the thirty days live in the 30DaysOfMaps folder, which is the one icon
  // on the desktop. `apps/map-challenge/` draws them.
  onDesktop: false,
  width: 1040,
  height: 680,
  // No window status bar. The map chrome has its own live region and `base.say()` writes to
  // it, so a second bar is a second renderer of one fact — and it showed: the window bar sat
  // on "Loading the runs…" for the session while the map's line said 401 activities had
  // loaded. One status, the one beside the thing it describes.
  status: false,
  // Two stylesheets: the map's furniture, shared with every other map app, then this day's
  // own controls. The loader keys its link cache on the resolved href, so the shared one is
  // linked once however many map apps are open.
  styles: ['../../shared/map/map.css', 'app.css'],

  /**
   * The chrome, and nothing that measures it.
   *
   * What `mount` returns is what gets inserted, so during `mount` none of these nodes has a
   * box. The map therefore cannot be built here — see `ready`.
   */
  async mount({ win }) {
    const [{ buildMapChrome }, { BASEMAPS }] = await Promise.all([
      import('../../shared/map/chrome.mjs'),
      import('../../shared/map/basemap.mjs'),
    ]);
    const chrome = buildMapChrome({
      basemaps: BASEMAPS,
      hint: 'Click a run to measure it. The slider simplifies the one you pick.',
    });
    // The map's own status line carries progress from here on; `lines.mjs` writes the first
    // word into it before the first fetch.
    //
    // On the window rather than in a module variable: two windows of one app would share a
    // module variable and the second would drive the first one's map.
    win.app = { chrome };
    return chrome.root;
  },

  /** The map and the whole day, once the subtree is laid out. */
  async ready({ win }) {
    const { startDay } = await import('./lines.mjs');
    await startDay({ win, chrome: win.app.chrome });
  },
};
