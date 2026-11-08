/* Length and simplification, in the browser.
 *
 * This is a second implementation of what `gpsnoise.py` already does, which is a thing
 * worth justifying rather than doing quietly. The page's whole point is a slider the
 * reader drags, and a continuous slider means re-simplifying on every frame; shipping a
 * pre-computed geometry per rung would make the control stepped and the payload several
 * times larger. So the arithmetic has to be here.
 *
 * Two implementations of one fact is exactly the thing that drifts. The guard is that
 * `prepare.py` writes its own ladder into each activity document, and `scripts/check.py`
 * drives this module over the same geometry and requires the two to agree. If they ever
 * disagree the check fails rather than the page quietly disagreeing with its own data.
 *
 * The constants and the frame are deliberately identical to the Python: same WGS84 radii
 * of curvature, same origin at the trace's mean latitude and longitude. A different
 * origin would be a correct projection and a different answer.
 */

const WGS84_A = 6378137.0;
const WGS84_F = 1.0 / 298.257223563;
const WGS84_E2 = WGS84_F * (2.0 - WGS84_F);

/* Project [lon, lat] pairs to metres east/north about their own mean.
 *
 * Returns flat Float64Arrays rather than an array of pairs: a 3,000-point trace
 * re-simplified on every pointer move allocates either way, and two typed arrays cost
 * one allocation instead of 3,000 objects. */
/* The tangent plane's origin on one axis: the bounding-box midpoint, to 6 dp.
 *
 * Must stay identical to `gpsnoise.frame_origin`, down to the last bit. The long version
 * of why is in that function's docstring; the short version is that a mean would not be
 * identical -- numpy sums pairwise and this would sum sequentially -- and Douglas-Peucker
 * compares distances against a threshold, so one differing bit flips a decision and
 * changes the answer by a visible amount. min and max are exact, the midpoint is one
 * operation, and the rounding is spelled out rather than left to each language's `round`.
 */
function frameOrigin(min, max) {
  return Math.floor(((min + max) / 2) * 1e6 + 0.5) / 1e6;
}

export function project(coords) {
  const n = coords.length;
  let lonMin = Infinity;
  let lonMax = -Infinity;
  let latMin = Infinity;
  let latMax = -Infinity;
  for (let i = 0; i < n; i += 1) {
    const lon = coords[i][0];
    const lat = coords[i][1];
    if (lon < lonMin) lonMin = lon;
    if (lon > lonMax) lonMax = lon;
    if (lat < latMin) latMin = lat;
    if (lat > latMax) latMax = lat;
  }
  const lat0 = frameOrigin(latMin, latMax);
  const lon0 = frameOrigin(lonMin, lonMax);
  const phi = (lat0 * Math.PI) / 180;
  const w = 1 - WGS84_E2 * Math.sin(phi) ** 2;
  const mRad = (WGS84_A * (1 - WGS84_E2)) / w ** 1.5;
  const nRad = WGS84_A / Math.sqrt(w);
  const kx = ((Math.PI / 180) * nRad * Math.cos(phi));
  const ky = (Math.PI / 180) * mRad;

  const x = new Float64Array(n);
  const y = new Float64Array(n);
  for (let i = 0; i < n; i += 1) {
    x[i] = (coords[i][0] - lon0) * kx;
    y[i] = (coords[i][1] - lat0) * ky;
  }
  return { x, y, lat0, lon0, kx, ky };
}

/* Length in metres of the polyline through the kept points of a projected trace.
 *
 * `keep` is optional; without it every point counts. Summing only the kept points is
 * what makes the slider cheap: no new array is built per frame. */
export function planeLength({ x, y }, keep) {
  const n = x.length;
  let total = 0;
  let prev = -1;
  for (let i = 0; i < n; i += 1) {
    if (keep && !keep[i]) continue;
    if (prev >= 0) {
      const dx = x[i] - x[prev];
      const dy = y[i] - y[prev];
      total += Math.sqrt(dx * dx + dy * dy);
    }
    prev = i;
  }
  return total;
}

/* Ramer-Douglas-Peucker over a projected trace. Returns a Uint8Array mask.
 *
 * Iterative, with an explicit stack: a dense trace recurses as deep as it is long, and
 * a long run at 1 Hz is tens of thousands of points. */
export function simplifyMask({ x, y }, tolerance) {
  const n = x.length;
  const keep = new Uint8Array(n);
  if (n === 0) return keep;
  keep[0] = 1;
  keep[n - 1] = 1;
  if (n < 3 || tolerance <= 0) {
    keep.fill(1);
    return keep;
  }

  const stack = [0, n - 1];
  while (stack.length) {
    const i1 = stack.pop();
    const i0 = stack.pop();
    if (i1 <= i0 + 1) continue;
    const cx = x[i1] - x[i0];
    const cy = y[i1] - y[i0];
    const chord = Math.sqrt(cx * cx + cy * cy);
    let best = -1;
    let bestDist = -1;
    for (let i = i0 + 1; i < i1; i += 1) {
      const px = x[i] - x[i0];
      const py = y[i] - y[i0];
      // A zero-length chord happens when a trace returns to where it started, which a
      // loop does. Fall back to radial distance from the shared end.
      const dist = chord === 0
        ? Math.sqrt(px * px + py * py)
        : Math.abs(cx * py - cy * px) / chord;
      if (dist > bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    if (bestDist > tolerance) {
      keep[best] = 1;
      stack.push(i0, best, best, i1);
    }
  }
  return keep;
}

/* Simplify to a [lon, lat] ring, for handing to MapLibre. */
export function simplifiedCoords(coords, keep) {
  const out = [];
  for (let i = 0; i < coords.length; i += 1) {
    if (keep[i]) out.push(coords[i]);
  }
  // A LineString needs two positions. One surviving point cannot happen -- both ends are
  // always kept -- but a one-point input would produce it, and MapLibre throws on it.
  return out.length >= 2 ? out : null;
}

/* Length and surviving-point count at one tolerance. */
export function measureAt(projected, tolerance) {
  const keep = simplifyMask(projected, tolerance);
  let kept = 0;
  for (let i = 0; i < keep.length; i += 1) kept += keep[i];
  return { keep, length: planeLength(projected, keep), kept };
}

/* The same ladder `prepare.py` writes, computed here. Used only by `scripts/check.py`,
 * to hold the two implementations against each other. */
export function ladder(coords, tolerances) {
  const projected = project(coords);
  return tolerances.map((t) => {
    const { length, kept } = measureAt(projected, t);
    return [t, length, kept];
  });
}
