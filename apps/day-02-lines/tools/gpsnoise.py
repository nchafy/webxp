"""How much of a recorded GPS trace is noise.

Two lengths, neither of them the truth:

- The **recorded** length, summed straight over the trackpoints. Receiver jitter is
  indistinguishable from movement, so every wobble is counted as distance run.
- The **matched** length, after snapping the trace to the OSM network. Map matching
  follows way centrelines, so it cuts corners and never sees you weave.

The first overestimates and the second underestimates. The gap between them is not an
error bar on a known value; it is the width of the question. `richardson` measures the
whole curve rather than the two endpoints, by simplifying the trace at a ladder of
tolerances and recording the length at each -- the Richardson method for a coastline
(Richardson 1961, via Mandelbrot, https://doi.org/10.1126/science.156.3775.636).

Everything here works in a local tangent plane, not on lat/lon. `project` builds it once
per trace from the WGS84 radii of curvature at the trace's mean latitude, after which
length, perpendicular distance and simplification are all plane geometry and agree with
each other by construction. Over the few km of one run the planar error is of order
(L/R)^2, about 3 mm over 5 km -- four orders of magnitude below the effect being
measured. Doing this on a sphere of mean radius instead would introduce a systematic
0.3% scale error, which is the same size as the noise on a clean trace.
"""

from __future__ import annotations

import gzip
import json
import math
import pathlib
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np

# WGS84, the datum GPS reports in. https://nsgreg.nga.mil/doc/view?i=4085 (NGA.STND.0036)
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)

# The ladder of simplification tolerances, in metres. Dense at the bottom because that
# is where receiver noise lives: a tolerance below the noise amplitude keeps the jitter,
# one above it removes the jitter and keeps the corners.
TOLERANCES_M = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 35.0, 60.0, 100.0)


@dataclass
class Track:
    """One recorded activity, as points in the order they were sampled."""

    name: str
    source: pathlib.Path
    lat: np.ndarray
    lon: np.ndarray
    # Seconds since the first point. NaN where the file carried no timestamps.
    t: np.ndarray
    # Metres. NaN where the file carried no elevation.
    ele: np.ndarray
    sport: str = ""

    def __len__(self) -> int:
        return int(self.lat.size)


@dataclass
class Measured:
    """What one track measures, by each method that can be applied to it."""

    track: Track
    recorded_m: float
    ladder: list[tuple[float, float, int]] = field(default_factory=list)
    matched_m: float | None = None
    matched_lat: np.ndarray | None = None
    matched_lon: np.ndarray | None = None
    match_note: str = ""
    # Median spacing between consecutive points, in metres. The single number that most
    # predicts how badly noise inflates a trace: see `expected_inflation`.
    step_m: float = float("nan")
    duration_s: float = float("nan")


# ---------------------------------------------------------------------------
# The local plane
# ---------------------------------------------------------------------------


def frame_origin(values: np.ndarray) -> float:
    """The tangent plane's origin on one axis: the bounding-box midpoint, to 6 dp.

    Both halves of this are load-bearing, and neither is obvious.

    **The midpoint, not the mean.** The scale factors are evaluated once at the origin,
    so the origin wants to be in the middle of the data: over a ride spanning a degree of
    latitude the meridional radius varies by about 0.17%, and taking the origin at an
    edge instead of the centre would leave a systematic error of order 0.08% -- the same
    size as the effect being measured on a clean trace.

    **Rounded, and by an explicitly written rule.** `geo.mjs` recomputes all of this in
    the browser, and the two have to agree exactly, not nearly. A mean would not: numpy
    sums pairwise and a JavaScript loop sums sequentially, so the two origins differ in
    the last bits. That is harmless for a length -- a shifted origin cancels -- but
    Douglas-Peucker compares each point's distance against a threshold, and a trace has
    points sitting arbitrarily close to it. One flipped decision changed a written ladder
    by 3e-5, which is what `scripts/check.py` caught.

    So: min and max are exact, the midpoint is one IEEE operation, and the rounding is
    written as floor(x * 1e6 + 0.5) in both languages rather than left to `round`, whose
    Python and JavaScript versions disagree on a half-way tie.
    """
    mid = (float(values.min()) + float(values.max())) / 2.0
    return math.floor(mid * 1e6 + 0.5) / 1e6


def project(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    """Project lat/lon degrees to metres east/north in a local tangent plane.

    The scale factors are the WGS84 radii of curvature at the origin latitude: M, the
    meridional radius, for north, and N, the prime-vertical radius, times cos(lat) for
    east. Both are standard (Torge & Mueller, *Geodesy*, eq. 4.18-4.19); using a single
    mean radius for both axes is the error this function exists to avoid, since at 40
    degrees north they differ by about 0.7%.
    """
    lat0 = frame_origin(lat)
    lon0 = frame_origin(lon)
    phi = math.radians(lat0)
    w = 1.0 - WGS84_E2 * math.sin(phi) ** 2
    m_rad = WGS84_A * (1.0 - WGS84_E2) / w**1.5
    n_rad = WGS84_A / math.sqrt(w)

    # The scale factors are collapsed into one multiplier per axis *before* touching the
    # coordinates, and `geo.mjs` does the identical two multiplies in the identical order.
    # This is not pedantry: `(d * k) * M` and `d * (k * M)` differ in the last bits, which
    # was measured at 2.3e-13 m on a 1362-point trace. That is harmless for a length, and
    # not harmless for Douglas-Peucker, which compares each point's distance against a
    # threshold -- the same reason `frame_origin` exists. Writing it one way here and the
    # other way there would have voided that.
    # Spelled `math.pi / 180.0` rather than `math.radians(1.0)` so that it is visibly the
    # same expression as the JavaScript's `Math.PI / 180`, with no trust placed in the two
    # standard libraries agreeing on the constant.
    ky = (math.pi / 180.0) * m_rad
    kx = (math.pi / 180.0) * n_rad * math.cos(phi)
    y = (lat - lat0) * ky
    x = (lon - lon0) * kx
    return x, y, {"lat0": lat0, "lon0": lon0, "m_rad": m_rad, "n_rad": n_rad, "kx": kx, "ky": ky}


def unproject(x: np.ndarray, y: np.ndarray, frame: dict) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of `project`, for writing a derived geometry back out as GeoJSON.

    Divides by the same two multipliers `project` built, rather than rebuilding them from
    the radii -- which is what makes the round trip exact rather than merely close.
    """
    lat = frame["lat0"] + y / frame["ky"]
    lon = frame["lon0"] + x / frame["kx"]
    return lat, lon


def plane_length(x: np.ndarray, y: np.ndarray) -> float:
    """Total length of the polyline through (x, y), in the units of x and y."""
    if x.size < 2:
        return 0.0
    return float(np.hypot(np.diff(x), np.diff(y)).sum())


def segment_lengths(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    if x.size < 2:
        return np.zeros(0)
    return np.hypot(np.diff(x), np.diff(y))


def track_length_m(lat: np.ndarray, lon: np.ndarray) -> float:
    """Recorded length of a lat/lon trace, in metres."""
    x, y, _ = project(lat, lon)
    return plane_length(x, y)


# ---------------------------------------------------------------------------
# Simplification
# ---------------------------------------------------------------------------


def simplify_mask(x: np.ndarray, y: np.ndarray, tol: float) -> np.ndarray:
    """Ramer-Douglas-Peucker. Returns a boolean mask of the points that survive.

    Iterative rather than recursive: a 1 Hz trace of a long run is tens of thousands of
    points and a pathological one recurses that deep.
    """
    n = x.size
    keep = np.zeros(n, dtype=bool)
    if n == 0:
        return keep
    keep[0] = keep[n - 1] = True
    if n < 3 or tol <= 0.0:
        keep[:] = True
        return keep

    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        # Perpendicular distance from each interior point to the chord (i0, i1). For a
        # degenerate chord -- the two ends coincide, which a doubled-back trace does
        # produce -- fall back to radial distance from the shared end.
        cx, cy = x[i1] - x[i0], y[i1] - y[i0]
        chord = math.hypot(cx, cy)
        px = x[i0 + 1 : i1] - x[i0]
        py = y[i0 + 1 : i1] - y[i0]
        if chord == 0.0:
            dist = np.hypot(px, py)
        else:
            dist = np.abs(cx * py - cy * px) / chord
        k = int(np.argmax(dist))
        if dist[k] > tol:
            split = i0 + 1 + k
            keep[split] = True
            stack.append((i0, split))
            stack.append((split, i1))
    return keep


def richardson(
    lat: np.ndarray, lon: np.ndarray, tolerances: tuple[float, ...] = TOLERANCES_M
) -> list[tuple[float, float, int]]:
    """Length of the trace after simplifying at each tolerance.

    Returns (tolerance_m, length_m, surviving_points) per rung. Length falls as tolerance
    rises, always: simplification can only shorten a polyline, since it replaces a path
    between two kept points with the straight line between them. A rung that rises is a
    bug, which is why `test_gpsnoise.py` asserts monotonicity.
    """
    x, y, _ = project(lat, lon)
    out = []
    for tol in tolerances:
        mask = simplify_mask(x, y, tol)
        out.append((float(tol), plane_length(x[mask], y[mask]), int(mask.sum())))
    return out


def expected_inflation(step_m: float, sigma_m: float) -> float:
    """Factor by which independent per-point noise inflates a straight path's length.

    Closed form, so that a measurement can be checked against theory rather than against
    another measurement. A true step of `step_m` between samples, each sample displaced
    by independent isotropic Gaussian noise of per-axis sigma `sigma_m`, gives a measured
    step whose length is Rice-distributed: the along-track component is
    N(step, 2*sigma^2) and the across-track component N(0, 2*sigma^2). The factor of two
    is there because the step is a *difference* of two independently noisy points, and a
    difference has twice the variance of one.

    The Rice mean is sigma_d * sqrt(pi/2) * L_{1/2}(-step^2 / (2 sigma_d^2)) with L the
    Laguerre function (Rice 1945, https://doi.org/10.1002/j.1538-7305.1945.tb00453.x).
    Substituting a = step^2 / (4 sigma_d^2) and folding the exponential into the scaled
    Bessel functions leaves

        E[|step|] = sigma_d sqrt(pi/2) [ (1 + 2a) I0e(a) + 2a I1e(a) ]

    where I0e(a) = exp(-a) I0(a). Writing it this way keeps it stable at large a, where
    the unscaled I0 overflows long before the result stops being near 1.

    **This models white noise, which real GPS error is not.** Receiver error is dominated
    by satellite geometry and atmospheric delay, which drift over tens of seconds, so
    consecutive points share most of their error and their difference carries far less
    than 2*sigma^2. That is why a real trace is inflated by a few percent while this
    function, fed a realistic per-point sigma, predicts tens of percent. It is the right
    reference for checking the measurement code against a known answer, and the wrong
    model for predicting a real activity.

    The part that does carry over: inflation depends on the *ratio* of noise to step, so
    a slow activity sampled often is inflated much more than a fast one. A runner at
    3 m/s logging at 1 Hz steps 3 m between points, below the error of a consumer
    receiver under tree cover.
    """
    if step_m <= 0.0:
        return float("nan")
    sd = sigma_m * math.sqrt(2.0)
    if sd == 0.0:
        return 1.0
    a = step_m**2 / (4.0 * sd**2)
    lag = (1.0 + 2.0 * a) * _bessel_ie(a, 0) + 2.0 * a * _bessel_ie(a, 1)
    return sd * math.sqrt(math.pi / 2.0) * lag / step_m


def speed_residuals(
    speed: np.ndarray, values: np.ndarray, min_per_bin: int = 400
) -> tuple[np.ndarray, int]:
    """Take the speed trend out of a set of noise measurements.

    Returns `(residual, n_bins)`, where residual is `values` minus the median of other
    measurements at the same speed.

    Why this exists at all: standing still is indistinguishable from bad reception, so a
    noise measurement binned by place will light up every junction in a city unless speed
    is controlled. A speed *band* narrows that and does not remove it -- inside a 2 to
    5 m/s band, speed still explained 12.6% of the variance in one real export.

    Non-parametric on purpose. Equal-count bins and a median, rather than a fitted line:
    the relationship is visibly curved -- about +4.5% at 2.0 m/s falling to +1.7% at
    4.5 -- and a straight line through that leaves structure in the residual at both ends,
    which is exactly where the slow, built-up measurements are.

    Shared by the grid and the street-segment pipelines, because two copies of a control
    this load-bearing is how they come to disagree about what they corrected for.
    """
    n = values.size
    if n < min_per_bin * 2:
        # Too few to bin meaningfully; subtract one median and say so with n_bins = 1.
        return values - float(np.median(values)), 1
    n_bins = max(8, min(40, n // min_per_bin))
    order = np.argsort(speed)
    bin_of = np.empty(n, dtype=int)
    bin_of[order] = np.minimum((np.arange(n) * n_bins) // n, n_bins - 1)
    expected = np.array([np.median(values[bin_of == b]) for b in range(n_bins)])
    return values - expected[bin_of], n_bins


def _bessel_ie(a: float, order: int) -> float:
    """exp(-a) * I_order(a), order 0 or 1.

    Hand-rolled so that `scipy` is not a dependency of a package that otherwise needs
    only numpy.

    Ascending series below a = 50, asymptotic expansion above. The switch sits at 50
    rather than somewhere more obvious because the asymptotic series is only truncated:
    the five terms carried here leave an error of order (8a)^-5. The two branches were
    measured against each other either side of the switch (`test_gpsnoise.py`, "bessel
    branches agree at the switch"): 2.6e-6 relative at a = 15, which is why the switch
    is not there, and 4.9e-11 at a = 50.

    The ascending series is happy either side -- every term is positive, so there is no
    cancellation, and although I0(50) is about 3e20 that is nowhere near overflowing a
    float64.
    """
    if a < 50.0:
        # Ascending series, which converges fast in this range.
        total = 0.0
        term_k = 0
        while True:
            # (a/2)^(2k+order) / (k! (k+order)!)
            num = (a / 2.0) ** (2 * term_k + order)
            den = math.factorial(term_k) * math.factorial(term_k + order)
            add = num / den
            total += add
            if add < 1e-18 * max(total, 1e-300) and term_k > 3:
                break
            term_k += 1
            if term_k > 200:
                break
        return math.exp(-a) * total
    # Asymptotic expansion of exp(-a) I_n(a) for large a, Abramowitz & Stegun 9.7.1.
    # Terms are built as a running product so the fifth one is not a line of literals.
    mu = 4.0 * order**2
    s = 1.0
    term = 1.0
    for k in range(1, 6):
        term *= -(mu - (2.0 * k - 1.0) ** 2) / (k * 8.0 * a)
        s += term
    return s / math.sqrt(2.0 * math.pi * a)


# ---------------------------------------------------------------------------
# Reading activity files
# ---------------------------------------------------------------------------

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1", "g0": "http://www.topografix.com/GPX/1/0"}


def _open_maybe_gz(path: pathlib.Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rb")
    return open(path, "rb")


def read_gpx(path: pathlib.Path) -> Track:
    """Parse a GPX file's trackpoints, in file order.

    Every `trkpt` in every `trkseg` of every `trk` is taken, concatenated. Strava writes
    one track and one segment, but a device that paused and resumed can write several,
    and the straight line across a pause is real distance the reader covered.
    """
    with _open_maybe_gz(path) as fh:
        tree = ET.parse(fh)
    root = tree.getroot()
    # Accept GPX 1.0 and 1.1, and a file with no namespace at all, by matching local names.
    pts = [el for el in root.iter() if _local(el.tag) == "trkpt"]
    if not pts:
        raise ValueError(f"{path.name}: no trkpt elements")

    lat = np.array([float(p.get("lat")) for p in pts])
    lon = np.array([float(p.get("lon")) for p in pts])
    ele = np.full(lat.size, np.nan)
    tsec = np.full(lat.size, np.nan)
    t0 = None
    for i, p in enumerate(pts):
        for child in p:
            tag = _local(child.tag)
            if tag == "ele" and child.text:
                ele[i] = float(child.text)
            elif tag == "time" and child.text:
                stamp = _parse_time(child.text.strip())
                if stamp is not None:
                    if t0 is None:
                        t0 = stamp
                    tsec[i] = stamp - t0

    name_el = next((el for el in root.iter() if _local(el.tag) == "name"), None)
    name = (name_el.text or path.stem).strip() if name_el is not None else path.stem
    sport_el = next((el for el in root.iter() if _local(el.tag) == "type"), None)
    sport = (sport_el.text or "").strip() if sport_el is not None else ""
    return Track(name=name, source=path, lat=lat, lon=lon, t=tsec, ele=ele, sport=sport)


def read_fit(path: pathlib.Path) -> Track:
    """Parse a FIT file's record messages. Needs `fitdecode`.

    Strava's bulk export hands back the file the device uploaded, which for most watches
    is FIT rather than GPX -- so this, not `read_gpx`, is the path most of an export
    takes. Positions are semicircles: degrees = semicircles * 180 / 2^31.

    A `.fit.gz` is decompressed to a temporary file first. `fitdecode` seeks, so a
    `gzip` stream will not do.
    """
    import fitdecode

    if path.name.endswith(".gz"):
        import shutil
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
            with gzip.open(path, "rb") as src:
                shutil.copyfileobj(src, tmp)
            tmp_path = pathlib.Path(tmp.name)
        try:
            track = read_fit(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return Track(
            name=path.name.removesuffix(".gz").removesuffix(".fit"),
            source=path,
            lat=track.lat,
            lon=track.lon,
            t=track.t,
            ele=track.ele,
            sport=track.sport,
        )

    lat_l: list[float] = []
    lon_l: list[float] = []
    ele_l: list[float] = []
    t_l: list[float] = []
    sport = ""
    t0 = None

    with fitdecode.FitReader(str(path)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "sport":
                val = frame.get_value("sport", fallback=None)
                if val:
                    sport = str(val)
                continue
            if frame.name != "record":
                continue
            la = frame.get_value("position_lat", fallback=None)
            lo = frame.get_value("position_long", fallback=None)
            if la is None or lo is None:
                # A record with no fix -- indoors, or before lock. Dropping it is right:
                # interpolating a position that was never measured invents distance.
                continue
            lat_l.append(la * (180.0 / 2**31))
            lon_l.append(lo * (180.0 / 2**31))
            alt = frame.get_value("enhanced_altitude", fallback=None)
            if alt is None:
                alt = frame.get_value("altitude", fallback=None)
            ele_l.append(float(alt) if alt is not None else float("nan"))
            stamp = frame.get_value("timestamp", fallback=None)
            if stamp is None:
                t_l.append(float("nan"))
            else:
                secs = stamp.timestamp()
                if t0 is None:
                    t0 = secs
                t_l.append(secs - t0)

    if not lat_l:
        raise ValueError(f"{path.name}: no record messages carried a position")
    return Track(
        name=path.stem,
        source=path,
        lat=np.array(lat_l),
        lon=np.array(lon_l),
        t=np.array(t_l),
        ele=np.array(ele_l),
        sport=sport,
    )


def read_tcx(path: pathlib.Path) -> Track:
    """Parse a TCX file's trackpoints."""
    with _open_maybe_gz(path) as fh:
        tree = ET.parse(fh)
    root = tree.getroot()
    pts = [el for el in root.iter() if _local(el.tag) == "Trackpoint"]
    lat_l, lon_l, ele_l, t_l = [], [], [], []
    t0 = None
    for p in pts:
        lat_v = lon_v = None
        ele_v = float("nan")
        t_v = float("nan")
        for el in p.iter():
            tag = _local(el.tag)
            if tag == "LatitudeDegrees" and el.text:
                lat_v = float(el.text)
            elif tag == "LongitudeDegrees" and el.text:
                lon_v = float(el.text)
            elif tag == "AltitudeMeters" and el.text:
                ele_v = float(el.text)
            elif tag == "Time" and el.text:
                stamp = _parse_time(el.text.strip())
                if stamp is not None:
                    if t0 is None:
                        t0 = stamp
                    t_v = stamp - t0
        if lat_v is None or lon_v is None:
            continue
        lat_l.append(lat_v)
        lon_l.append(lon_v)
        ele_l.append(ele_v)
        t_l.append(t_v)
    if not lat_l:
        raise ValueError(f"{path.name}: no Trackpoint carried a position")
    return Track(
        name=path.stem,
        source=path,
        lat=np.array(lat_l),
        lon=np.array(lon_l),
        t=np.array(t_l),
        ele=np.array(ele_l),
    )


def read_activity(path: pathlib.Path) -> Track:
    """Dispatch on extension, ignoring a trailing `.gz`."""
    stem = path.name[:-3] if path.name.endswith(".gz") else path.name
    low = stem.lower()
    if low.endswith(".gpx"):
        return read_gpx(path)
    if low.endswith(".fit"):
        return read_fit(path)
    if low.endswith(".tcx"):
        return read_tcx(path)
    raise ValueError(f"{path.name}: unknown activity format")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_time(text: str) -> float | None:
    """Seconds since the epoch from an ISO 8601 stamp, or None.

    Hand-rolled rather than `datetime.fromisoformat`, which before 3.11 rejects the
    trailing `Z` that every GPS file in the world writes.
    """
    import datetime as _dt

    t = text.replace("Z", "+00:00")
    # Trim fractional seconds to microseconds, which is all fromisoformat accepts.
    if "." in t:
        head, _, tail = t.partition(".")
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                tail = tail[len(digits) :]
                break
        else:
            tail = ""
        t = f"{head}.{digits[:6]}{tail}"
    try:
        return _dt.datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Map matching
# ---------------------------------------------------------------------------

VALHALLA_URL = "https://valhalla1.openstreetmap.de"


def decode_polyline(encoded: str, precision: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """Decode an encoded polyline to lat/lon arrays.

    Valhalla encodes at precision 6, not the 5 that Google's original format and most
    copies of this function use. Decoding a Valhalla shape at precision 5 yields a
    perfectly plausible path across the Atlantic.
    """
    factor = float(10**precision)
    lat = lon = 0
    out_lat: list[float] = []
    out_lon: list[float] = []
    i = 0
    n = len(encoded)
    while i < n:
        for axis in (0, 1):
            shift = result = 0
            while True:
                if i >= n:
                    raise ValueError("truncated polyline")
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if axis == 0:
                lat += delta
            else:
                lon += delta
        out_lat.append(lat / factor)
        out_lon.append(lon / factor)
    return np.array(out_lat), np.array(out_lon)


def _post_json(action: str, body: dict, timeout: float = 120.0) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{VALHALLA_URL}/{action}",
        data=data,
        headers={
            "Content-Type": "application/json",
            # A courtesy to the FOSSGIS volunteers running the public instance, who ask
            # that a client identify itself. https://github.com/valhalla/valhalla
            "User-Agent": "30daymapchallenge-gps-noise (one-off, non-commercial)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def match_chunk(
    lat: np.ndarray, lon: np.ndarray, costing: str = "pedestrian", search_radius: float = 50.0
) -> tuple[np.ndarray, np.ndarray]:
    """Snap one chunk of a trace to the OSM network. Returns the matched lat/lon path.

    The matched *shape* is summed rather than `edge.length`, deliberately. An edge's
    length is the length of the whole way segment, but a trace can enter and leave one
    part-way through, and a trace that doubles back traverses the same edge twice; both
    make a sum over edges wrong in a way that is invisible in the output. The returned
    shape is the path the matcher actually fitted.
    """
    body = {
        "shape": [{"lat": float(a), "lon": float(o)} for a, o in zip(lat, lon)],
        "costing": costing,
        "shape_match": "map_snap",
        "trace_options": {"search_radius": search_radius},
        "filters": {"attributes": ["shape", "matched.point", "matched.type"], "action": "include"},
    }
    res = _post_json("trace_attributes", body)
    shape = res.get("shape")
    if not shape:
        raise ValueError("matcher returned no shape")
    return decode_polyline(shape, precision=6)


#: Points per matching request.
#:
#: Measured against the public instance by `probe_limits.py`: 16,000 is accepted and
#: 20,000 returns HTTP 413, so the ceiling is somewhere in between. 15,000 leaves margin
#: for a differently configured instance.
#:
#: The size matters for politeness, not for speed or accuracy. A 16,000-point request
#: takes 1.3 s against the public instance -- barely longer than a 1,500-point one -- and
#: returns a matched length within 0.01 km of it. But chunking a 1.6M-point history at
#: 1,500 is over 1,200 requests where 15,000 is about 410. Same answer, a third of the
#: load on a volunteer-run service.
MATCH_CHUNK = 15000


def match_trace(
    lat: np.ndarray,
    lon: np.ndarray,
    costing: str = "pedestrian",
    chunk: int = MATCH_CHUNK,
    pause_s: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, str]:
    """Snap a whole trace, in chunks, with a pause between calls.

    Chunks overlap by one point -- the last point of a chunk is the first of the next --
    so the snapped pieces join instead of leaving a gap the length of one sample.

    `costing` must suit the activity. Snapping a run with `auto` routes it down the road
    network only, which on a park path or a towpath produces a detour that is longer
    than the recorded trace and inverts the whole result.
    """
    if lat.size < 2:
        return np.zeros(0), np.zeros(0), "too few points to match"

    pieces: list[tuple[np.ndarray, np.ndarray]] = []
    notes: list[str] = []
    start = 0
    while start < lat.size - 1:
        stop = min(start + chunk, lat.size)
        try:
            mlat, mlon = match_chunk(lat[start:stop], lon[start:stop], costing=costing)
            pieces.append((mlat, mlon))
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
            detail = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = json.loads(exc.read().decode()).get("error", "")
                except Exception:
                    detail = ""
            notes.append(f"points {start}-{stop}: {detail or exc}")
        start = stop - 1 if stop < lat.size else stop
        if start < lat.size - 1:
            time.sleep(pause_s)

    if not pieces:
        return np.zeros(0), np.zeros(0), "; ".join(notes) or "no chunk matched"
    mlat = np.concatenate([p[0] for p in pieces])
    mlon = np.concatenate([p[1] for p in pieces])
    note = ""
    if notes:
        note = f"{len(notes)} of {len(notes) + len(pieces)} chunks unmatched: " + "; ".join(notes)
    return mlat, mlon, note


COSTING_FOR_SPORT = {
    "run": "pedestrian",
    "running": "pedestrian",
    "trailrun": "pedestrian",
    "walk": "pedestrian",
    "walking": "pedestrian",
    "hike": "pedestrian",
    "hiking": "pedestrian",
    "ride": "bicycle",
    "cycling": "bicycle",
    "biking": "bicycle",
    "virtualride": "bicycle",
    "gravelride": "bicycle",
    "mountainbikeride": "bicycle",
}


def costing_for(sport: str) -> str:
    """Pick a Valhalla costing model from a sport name, defaulting to pedestrian."""
    key = sport.lower().replace(" ", "").replace("_", "")
    return COSTING_FOR_SPORT.get(key, "pedestrian")


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


def measure(track: Track, do_match: bool = True, costing: str | None = None) -> Measured:
    """Measure one track: recorded length, the simplification ladder, and the snapped path."""
    recorded = track_length_m(track.lat, track.lon)
    x, y, _ = project(track.lat, track.lon)
    steps = segment_lengths(x, y)
    out = Measured(
        track=track,
        recorded_m=recorded,
        ladder=richardson(track.lat, track.lon),
        step_m=float(np.median(steps)) if steps.size else float("nan"),
        duration_s=float(np.nanmax(track.t)) if np.isfinite(track.t).any() else float("nan"),
    )
    if do_match:
        mlat, mlon, note = match_trace(track.lat, track.lon, costing=costing or costing_for(track.sport))
        out.match_note = note
        if mlat.size >= 2:
            out.matched_lat, out.matched_lon = mlat, mlon
            out.matched_m = track_length_m(mlat, mlon)
    return out
