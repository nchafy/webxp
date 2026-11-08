"""Checks for `gpsnoise`, run before any of it is pointed at real data.

The one that matters is `test_synthetic_inflation_matches_theory`. A pipeline that
measures GPS noise can be wrong in a way that still produces a plausible number -- a
sphere instead of an ellipsoid, a factor of two in the noise variance, a projection whose
axes are scaled alike -- and every one of those errors reads as "about 2% inflation,
seems right". So the measurement is checked against a closed form instead: build a trace
whose true length is known exactly, displace every point by noise of known sigma, and
require the measured inflation to match the Rice mean to within sampling error.

    .venv/bin/python days/02-lines/test_gpsnoise.py

Prints one line per check and exits non-zero on the first failure. `--negative-control`
corrupts the metric and the noise model in turn and requires the suite to notice; a check
that has not been seen to fail has not been shown to check anything.
"""

from __future__ import annotations

import math
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "ok  " if ok else "FAIL"
    print(f"{mark} {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def close(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= rel * abs(b)


# ---------------------------------------------------------------------------


def test_degree_lengths() -> None:
    """One degree of latitude and of longitude at 40 N, against published values.

    A degree of latitude is 110.57 km at the equator and 111.69 km at the pole; a degree
    of longitude is 111.32 km at the equator and zero at the pole. At 40 N they are
    111.03 km and 85.39 km (NOAA, https://www.noaa.gov/jetstream/latlong). Getting both
    right is what separates a correctly scaled local plane from one using a single mean
    radius, which would put latitude and longitude out by opposite signs.
    """
    lat = np.array([39.5, 40.5])
    lon = np.array([-74.0, -74.0])
    check("one degree of latitude at 40N", close(gn.track_length_m(lat, lon), 111_030.0, 2e-4),
          f"{gn.track_length_m(lat, lon):,.0f} m, expected 111,030")

    lat = np.array([40.0, 40.0])
    lon = np.array([-74.5, -73.5])
    check("one degree of longitude at 40N", close(gn.track_length_m(lat, lon), 85_390.0, 2e-4),
          f"{gn.track_length_m(lat, lon):,.0f} m, expected 85,390")


def test_projection_round_trip() -> None:
    rng = np.random.default_rng(1)
    lat = 40.7 + rng.normal(0, 0.02, 500)
    lon = -73.95 + rng.normal(0, 0.02, 500)
    x, y, frame = gn.project(lat, lon)
    back_lat, back_lon = gn.unproject(x, y, frame)
    worst = max(np.abs(back_lat - lat).max(), np.abs(back_lon - lon).max())
    # 1e-9 degrees is about 0.1 mm. The round trip is exact arithmetic, not an estimate.
    check("projection round trip", worst < 1e-9, f"worst error {worst:.2e} deg")


def test_simplify_known_case() -> None:
    """A square wave on a straight baseline: a tolerance above the wave amplitude must
    remove it entirely and leave the two endpoints."""
    x = np.arange(0.0, 101.0, 1.0)
    y = np.where(np.arange(101) % 2 == 0, 0.0, 3.0)
    y[0] = y[-1] = 0.0

    keep_tight = gn.simplify_mask(x, y, 1.0)
    keep_loose = gn.simplify_mask(x, y, 5.0)
    check("simplify keeps detail below tolerance", keep_tight.sum() > 50,
          f"{keep_tight.sum()} of {x.size} points survive at tol 1 m")
    check("simplify removes detail above tolerance", int(keep_loose.sum()) == 2,
          f"{keep_loose.sum()} points survive at tol 5 m")

    # A collinear path must collapse to its endpoints at any positive tolerance.
    x2 = np.arange(0.0, 50.0)
    y2 = np.zeros(50)
    check("simplify collapses a straight line", int(gn.simplify_mask(x2, y2, 0.01).sum()) == 2)

    # A closed loop has a degenerate first chord: the fallback must not drop the loop.
    th = np.linspace(0, 2 * math.pi, 200)
    check("simplify handles a closed loop",
          gn.simplify_mask(100 * np.cos(th), 100 * np.sin(th), 1.0).sum() > 20)


def test_richardson_monotone() -> None:
    """Length must fall as tolerance rises, on a real-shaped noisy trace."""
    lat, lon, _ = synthetic_trace(seed=7, n=4000, step_m=3.0, sigma_m=4.0)
    ladder = gn.richardson(lat, lon)
    lengths = [row[1] for row in ladder]
    rises = [(ladder[i][0], lengths[i], lengths[i - 1]) for i in range(1, len(lengths))
             if lengths[i] > lengths[i - 1] + 1e-6]
    check("richardson ladder is monotone", not rises, f"{len(rises)} rung(s) rose" if rises else
          f"{lengths[0]:,.0f} m at tol 0 falling to {lengths[-1]:,.0f} m at tol 100")
    check("richardson ladder starts at the recorded length",
          close(lengths[0], gn.track_length_m(lat, lon), 1e-12))


def test_polyline_decode() -> None:
    """Against the published fixture for the encoded polyline format.

    `_p~iF~ps|U_ulLnnqC_mqNvxq`@` is the example from Google's own specification
    (https://developers.google.com/maps/documentation/utilities/polylinealgorithm) and
    decodes at precision 5 to three known points. Valhalla uses the same algorithm at
    precision 6, so this fixture exercises the varint and zigzag handling, which is
    where the bugs are; the precision is only a divisor.
    """
    lat, lon = gn.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    want_lat = np.array([38.5, 40.7, 43.252])
    want_lon = np.array([-120.2, -120.95, -126.453])
    ok = lat.size == 3 and np.allclose(lat, want_lat) and np.allclose(lon, want_lon)
    check("polyline decode against the published fixture", ok,
          f"got {list(zip(np.round(lat, 3), np.round(lon, 3)))}")

    # Precision 6 on the same bytes must give exactly a tenth of the degrees. A decoder
    # that hardcodes 1e5 passes the check above and silently mislocates every Valhalla
    # response, so the divisor is checked too.
    lat6, _ = gn.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=6)
    check("polyline precision is applied", np.allclose(lat6, want_lat / 10.0))


def test_rice_mean_against_monte_carlo() -> None:
    """`expected_inflation` is a Bessel series; check it against direct sampling.

    This is the reference the main test leans on, so it gets its own check rather than
    being trusted. Sampling the step length directly shares no code with the closed
    form.
    """
    # The Bessel helper switches from an ascending series to an asymptotic expansion at
    # a = 50. A discontinuity there would be invisible in the checks below, which
    # straddle it without landing on it.
    for order in (0, 1):
        lo = gn._bessel_ie(50.0 - 1e-9, order)
        hi = gn._bessel_ie(50.0, order)
        check(f"bessel branches agree at the switch, order {order}", close(lo, hi, 1e-9),
              f"series {lo:.12f} vs asymptotic {hi:.12f}")

    rng = np.random.default_rng(11)
    n = 4_000_000
    # The last pair is the regime a real trace sits in -- a few percent, not tens -- and
    # is the one worth getting right, since that is where the answer will land.
    for step, sigma in ((3.0, 4.0), (10.0, 2.0), (1.0, 5.0), (50.0, 3.0), (20.0, 1.5)):
        sd = sigma * math.sqrt(2.0)
        along = step + rng.normal(0, sd, n)
        across = rng.normal(0, sd, n)
        mc = float(np.hypot(along, across).mean()) / step
        theory = gn.expected_inflation(step, sigma)
        # 4e6 samples puts the standard error of the mean near 1e-4 relative.
        check(f"rice mean, step {step} m sigma {sigma} m", close(theory, mc, 2e-3),
              f"closed form {theory:.5f}, monte carlo {mc:.5f}")


def synthetic_trace(
    seed: int, n: int, step_m: float, sigma_m: float, leg_m: float = 400.0
) -> tuple[np.ndarray, np.ndarray, float]:
    """A trace with an exactly known true length, plus independent per-point noise.

    The true path is a rectilinear circuit of `leg_m` legs -- a city block pattern, so
    the path has corners that simplification should keep, not just a straight line that
    it should flatten. Points are placed exactly `step_m` apart along it, so the true
    length is `step_m * (n - 1)` by construction, with no accumulation of error.

    Returns noisy lat/lon and the true length in metres.
    """
    rng = np.random.default_rng(seed)
    # Walk the circuit in the local plane, turning left every `leg_m`.
    headings = np.array([0.0, math.pi / 2, math.pi, 3 * math.pi / 2])
    xs = np.empty(n)
    ys = np.empty(n)
    x = y = 0.0
    dist_on_leg = 0.0
    leg = 0
    for i in range(n):
        xs[i], ys[i] = x, y
        h = headings[leg % 4]
        x += step_m * math.cos(h)
        y += step_m * math.sin(h)
        dist_on_leg += step_m
        if dist_on_leg >= leg_m:
            dist_on_leg = 0.0
            leg += 1
    true_len = step_m * (n - 1)

    xs_noisy = xs + rng.normal(0, sigma_m, n)
    ys_noisy = ys + rng.normal(0, sigma_m, n)

    # Convert to lat/lon about a real location, so the projection is exercised rather
    # than bypassed.
    frame = {"lat0": 40.7128, "lon0": -74.0060}
    phi = math.radians(frame["lat0"])
    w = 1.0 - gn.WGS84_E2 * math.sin(phi) ** 2
    m_rad = gn.WGS84_A * (1.0 - gn.WGS84_E2) / w**1.5
    n_rad = gn.WGS84_A / math.sqrt(w)
    lat = frame["lat0"] + np.degrees(ys_noisy / m_rad)
    lon = frame["lon0"] + np.degrees(xs_noisy / (n_rad * math.cos(phi)))
    return lat, lon, true_len


def test_synthetic_inflation_matches_theory() -> None:
    """The headline check: measured inflation equals the Rice prediction.

    Over a long trace the corners of the circuit contribute a vanishing share of the
    length -- one turn per `leg_m / step_m` points -- so a straight-path prediction
    applies. The tolerance is set by sampling error, not by taste: the relative standard
    error of a mean of n segment lengths is about 0.8 / sqrt(n) here, so 0.4% at n =
    40,000 is roughly five sigma.
    """
    for step_m, sigma_m in ((3.0, 4.0), (10.0, 3.0), (2.0, 2.0)):
        lat, lon, true_len = synthetic_trace(seed=3, n=40_000, step_m=step_m, sigma_m=sigma_m)
        measured = gn.track_length_m(lat, lon)
        got = measured / true_len
        want = gn.expected_inflation(step_m, sigma_m)
        check(
            f"synthetic inflation, step {step_m} m sigma {sigma_m} m",
            close(got, want, 4e-3),
            f"measured {got:.4f}x, predicted {want:.4f}x "
            f"({measured / 1000:,.2f} km recorded for {true_len / 1000:,.2f} km run)",
        )


def test_simplification_recovers_truth() -> None:
    """Simplifying a noisy trace at a tolerance near the noise amplitude should land
    much closer to the true length than the raw trace does.

    This is the claim the whole map rests on -- that the Richardson ladder is reading
    noise rather than inventing a trend -- so it is checked against a known truth.
    """
    step_m, sigma_m = 3.0, 4.0
    lat, lon, true_len = synthetic_trace(seed=5, n=20_000, step_m=step_m, sigma_m=sigma_m)
    ladder = gn.richardson(lat, lon)
    raw_err = abs(ladder[0][1] - true_len) / true_len
    best_tol, best_err = None, 1e9
    for tol, length, _ in ladder:
        err = abs(length - true_len) / true_len
        if err < best_err:
            best_tol, best_err = tol, err
    check("simplification recovers the true length better than the raw trace",
          best_err < raw_err / 3.0,
          f"raw off by {raw_err:.1%}, best rung (tol {best_tol} m) off by {best_err:.1%}")


def test_distance_unit_resolution() -> None:
    """`prepare.resolve_distance_unit` must identify a unit, or refuse.

    The refusal cases carry the weight. Kilometres and miles are a factor of 1.609
    apart, so a reference distance that is itself uncertain by more than about 25%
    cannot tell them apart -- and quietly choosing one would scale every reference
    figure on the page by 1.609 while still looking like a column of plausible numbers.
    """
    import prepare

    # Clean references: 5,000 m and 10,000 m activities.
    refs = [5000.0, 10000.0, 7500.0, 3200.0]

    metres = [(r, r) for r in refs]
    scale, note = prepare.resolve_distance_unit(metres, "Distance", "matched")
    check("unit resolution finds metres", scale == 1.0, note)

    km = [(r / 1000.0, r) for r in refs]
    scale, note = prepare.resolve_distance_unit(km, "Distance", "matched")
    check("unit resolution finds kilometres", scale == 1000.0, note)

    miles = [(r / 1609.344, r) for r in refs]
    scale, note = prepare.resolve_distance_unit(miles, "Distance", "matched")
    check("unit resolution finds miles", scale == 1609.344, note)

    # A reference inflated by 45% makes a kilometres column look like 0.00069 rather
    # than 0.001. That is 31% away from kilometres and 11% from miles -- so with a
    # tolerant window it is read as *miles*, which is a wrong answer rather than a
    # missing one. This is the case that set the tolerance.
    inflated = [(r / 1000.0, r * 1.45) for r in refs]
    scale, note = prepare.resolve_distance_unit(inflated, "Distance", "recorded")
    check("unit resolution refuses an inflated reference", scale == 0.0, note)

    # The other edge: the window must not be so tight that real data is refused. A
    # reported distance a few percent off the matched length is normal, since matching
    # cuts corners that a smoothed distance keeps.
    realistic = [(r * 1.04 / 1000.0, r) for r in refs]
    scale, note = prepare.resolve_distance_unit(realistic, "Distance", "matched")
    check("unit resolution accepts a realistic few-percent gap", scale == 1000.0, note)

    # Nothing to go on.
    scale, note = prepare.resolve_distance_unit([], "Distance", "matched")
    check("unit resolution refuses with no data", scale == 0.0, note)

    # A column of something that is not a distance at all -- heart rate, say.
    scale, note = prepare.resolve_distance_unit([(142.0, r) for r in refs], "Distance", "matched")
    check("unit resolution refuses a non-distance column", scale == 0.0, note)

    # The windows for kilometres and miles must not overlap at this tolerance, or the
    # ambiguity branch can never fire and the two are being told apart by luck.
    gap = 1609.344 / 1000.0
    check("km and mile windows are disjoint at the chosen tolerance",
          (1 + prepare.UNIT_TOLERANCE) / (1 - prepare.UNIT_TOLERANCE) < gap,
          f"tolerance {prepare.UNIT_TOLERANCE:.0%} spans a factor of "
          f"{(1 + prepare.UNIT_TOLERANCE) / (1 - prepare.UNIT_TOLERANCE):.2f}, "
          f"units are {gap:.3f} apart")


def test_activity_id_collisions() -> None:
    """`prepare.activity_id` must not collapse two activities onto one id.

    The id names the file the page fetches, so a collision is not a cosmetic clash: the
    second document overwrites the first, `index.json` carries the id twice, and the page
    loads that one activity twice and adds its distance to the total twice.
    """
    import prepare

    taken: set[str] = set()
    ids = [
        prepare.activity_id(pathlib.Path(p), taken)
        for p in (
            "12345.gpx.gz",
            "2026-01-01.run.gpx",
            "2026-01-01.ride.gpx",
            "2026-01-01.ride.gpx",
            "67890.fit.gz",
            "67890.tcx",
        )
    ]
    check("activity ids are all distinct", len(set(ids)) == len(ids), str(ids))
    check("the plain Strava name is unchanged", ids[0] == "12345", ids[0])
    check(
        "a dotted stem keeps its whole stem",
        ids[1] == "2026-01-01.run" and ids[2] == "2026-01-01.ride",
        f"{ids[1]!r}, {ids[2]!r} -- splitting on the first dot would give '2026-01-01' twice",
    )
    check("a true duplicate is suffixed", ids[3] == "2026-01-01.ride-2", ids[3])
    # Same stem, different format: these are two files and must stay two activities.
    check("same stem with different suffixes stays distinct", ids[4] != ids[5],
          f"{ids[4]!r} vs {ids[5]!r}")


def test_file_parsers() -> None:
    """Round-trip a tiny GPX and TCX through the readers.

    Written to a temp file rather than fixtured, so the check carries its own input.
    """
    import tempfile

    gpx = """<?xml version="1.0"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
 <trk><name>Morning Run</name><type>run</type><trkseg>
  <trkpt lat="40.700000" lon="-73.950000"><ele>10.0</ele><time>2026-11-02T09:00:00Z</time></trkpt>
  <trkpt lat="40.700899" lon="-73.950000"><ele>11.5</ele><time>2026-11-02T09:00:30Z</time></trkpt>
  <trkpt lat="40.701798" lon="-73.950000"><ele>12.0</ele><time>2026-11-02T09:01:00.500Z</time></trkpt>
 </trkseg></trk></gpx>"""
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "a.gpx"
        p.write_text(gpx)
        tr = gpsnoise_read(p)
        ok = (
            len(tr) == 3
            and tr.name == "Morning Run"
            and tr.sport == "run"
            and abs(tr.t[1] - 30.0) < 1e-6
            and abs(tr.t[2] - 60.5) < 1e-6
            and abs(tr.ele[1] - 11.5) < 1e-9
        )
        check("gpx reader", ok, f"{len(tr)} pts, name {tr.name!r}, t={list(np.round(tr.t, 1))}")
        # Two steps of ~100 m each: 0.000899 deg of latitude at 40.7 N.
        check("gpx length is plausible", 190.0 < gn.track_length_m(tr.lat, tr.lon) < 210.0,
              f"{gn.track_length_m(tr.lat, tr.lon):.1f} m over two ~100 m steps")

    check("costing for a run is pedestrian", gn.costing_for("Run") == "pedestrian")
    check("costing for a ride is bicycle", gn.costing_for("Ride") == "bicycle")
    check("costing defaults to pedestrian", gn.costing_for("Kayaking") == "pedestrian")


def gpsnoise_read(p: pathlib.Path):
    return gn.read_activity(p)


# ---------------------------------------------------------------------------


def negative_control() -> int:
    """Break the thing each check is about, and require the suite to fail.

    Three sabotages, each the error most likely to happen for real:

    1. A single mean radius for both axes -- the sphere mistake. Must break the degree
       lengths, since latitude and longitude then scale alike.
    2. Dropping the factor of two in the noise variance -- forgetting that a *difference*
       of two noisy points has twice the variance of one. Must break the Rice check and
       the synthetic inflation.
    3. Decoding Valhalla polylines at precision 5. Must break the precision check.
    """
    print("\n-- negative control: each sabotage must produce a failure --")
    failures_seen = 0
    sabotages = [
        ("single mean radius for both axes", _sabotage_sphere, [test_degree_lengths]),
        ("no variance doubling in the noise model", _sabotage_variance,
         [test_rice_mean_against_monte_carlo]),
        ("polyline precision hardcoded to 5", _sabotage_precision, [test_polyline_decode]),
    ]
    for label, apply_sabotage, tests in sabotages:
        FAILURES.clear()
        restore = apply_sabotage()
        try:
            for t in tests:
                t()
        finally:
            restore()
        if FAILURES:
            print(f"ok   sabotage caught: {label} -> {len(FAILURES)} failure(s)")
            failures_seen += 1
        else:
            print(f"FAIL sabotage NOT caught: {label}. The check cannot fail, so it checks nothing.")
    FAILURES.clear()
    return 0 if failures_seen == len(sabotages) else 1


def _sabotage_sphere():
    original = gn.project

    def broken(lat, lon):
        r = 6_371_008.8
        lat0, lon0 = float(np.mean(lat)), float(np.mean(lon))
        y = np.radians(lat - lat0) * r
        x = np.radians(lon - lon0) * r * math.cos(math.radians(lat0))
        return x, y, {"lat0": lat0, "lon0": lon0, "m_rad": r, "n_rad": r}

    gn.project = broken
    return lambda: setattr(gn, "project", original)


def _sabotage_variance():
    original = gn.expected_inflation

    def broken(step_m, sigma_m):
        return original(step_m, sigma_m / math.sqrt(2.0))

    gn.expected_inflation = broken
    return lambda: setattr(gn, "expected_inflation", original)


def _sabotage_precision():
    original = gn.decode_polyline

    def broken(encoded, precision=6):
        return original(encoded, precision=5)

    gn.decode_polyline = broken
    return lambda: setattr(gn, "decode_polyline", original)


TESTS = [
    test_degree_lengths,
    test_projection_round_trip,
    test_simplify_known_case,
    test_richardson_monotone,
    test_polyline_decode,
    test_rice_mean_against_monte_carlo,
    test_synthetic_inflation_matches_theory,
    test_simplification_recovers_truth,
    test_distance_unit_resolution,
    test_activity_id_collisions,
    test_file_parsers,
]


def main() -> int:
    if "--negative-control" in sys.argv:
        return negative_control()
    for t in TESTS:
        t()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print(f"\nall {len(TESTS)} check groups passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
