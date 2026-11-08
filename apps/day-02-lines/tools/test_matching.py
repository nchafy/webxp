"""Integration check for map matching. Hits the network, so it is separate from
`test_gpsnoise.py`, which is pure and fast.

    .venv/bin/python days/02-lines/test_matching.py

The trick is to manufacture a trace whose correct answer is already known. Ask Valhalla
to route between two points, and it returns both the path along the OSM network and the
length of that path. Resample the path, displace every point by noise, and feed it back
in as if it were a recording: matching should return close to the length Valhalla itself
reported, and the raw trace should come back visibly longer.

That checks the whole chain at once -- request body, polyline precision, chunk stitching,
costing model, and the claim the map actually rests on, which is that snapping a noisy
trace recovers the network distance. A unit test on the decoder cannot reach any of it.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
import urllib.request

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402

FAILURES: list[str] = []

# Two legs in Manhattan and one in Brooklyn: a street grid, a park path, and a stretch
# of waterfront. Different enough that a matcher working on only one of them shows up.
LEGS = [
    ("Upper West Side grid", (40.7681, -73.9819), (40.7731, -73.9712)),
    ("Central Park paths", (40.7686, -73.9742), (40.7812, -73.9665)),
    ("Brooklyn, Prospect Park", (40.6602, -73.9690), (40.6680, -73.9730)),
]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def fetch_route(a: tuple[float, float], b: tuple[float, float], costing: str = "pedestrian") -> dict:
    body = {
        "locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
        "costing": costing,
        "units": "kilometers",
        "directions_type": "none",
    }
    req = urllib.request.Request(
        f"{gn.VALHALLA_URL}/route",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "30daymapchallenge-gps-noise (one-off, non-commercial)",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def resample(lat: np.ndarray, lon: np.ndarray, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Place points every `spacing_m` along the polyline, by arc length.

    Local to this check. A real recording is already sampled by the device, so the
    pipeline never needs this -- only the synthetic trace does.
    """
    x, y, frame = gn.project(lat, lon)
    seg = gn.segment_lengths(x, y)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]
    want = np.arange(0.0, total, spacing_m)
    nx = np.interp(want, s, x)
    ny = np.interp(want, s, y)
    return gn.unproject(nx, ny, frame)


def main() -> int:
    rng = np.random.default_rng(42)
    sigma_m = 3.0
    spacing_m = 3.0

    for label, a, b in LEGS:
        print(f"\n-- {label} --")
        try:
            route = fetch_route(a, b)
        except Exception as exc:
            check(f"{label}: route request", False, str(exc))
            continue

        truth_km = route["trip"]["summary"]["length"]
        truth_m = truth_km * 1000.0
        shape = route["trip"]["legs"][0]["shape"]
        rlat, rlon = gn.decode_polyline(shape, precision=6)

        # Cross-check: my length metric against Valhalla's own figure for the same path.
        # These are computed by different code on different machines from the same
        # geometry, so agreeing to a fraction of a percent says both are right. A
        # precision-5 decode would be out by a factor of ten and a sphere by 0.3%.
        mine_m = gn.track_length_m(rlat, rlon)
        rel = abs(mine_m - truth_m) / truth_m
        check(
            f"{label}: my length agrees with Valhalla's",
            rel < 0.005,
            f"mine {mine_m:,.0f} m vs reported {truth_m:,.0f} m ({rel:.2%})",
        )

        # Manufacture the recording.
        slat, slon = resample(rlat, rlon, spacing_m)
        if slat.size < 20:
            check(f"{label}: route long enough to test", False, f"{slat.size} points")
            continue
        x, y, frame = gn.project(slat, slon)
        nlat, nlon = gn.unproject(
            x + rng.normal(0, sigma_m, x.size), y + rng.normal(0, sigma_m, y.size), frame
        )

        recorded_m = gn.track_length_m(nlat, nlon)
        predicted = gn.expected_inflation(spacing_m, sigma_m)
        check(
            f"{label}: noisy trace is inflated as predicted",
            abs(recorded_m / truth_m - predicted) < 0.08 * predicted,
            f"recorded {recorded_m:,.0f} m is {recorded_m / truth_m:.2f}x truth, "
            f"predicted {predicted:.2f}x",
        )

        # The real subject: does snapping it back recover the network length?
        mlat, mlon, note = gn.match_trace(nlat, nlon, costing="pedestrian")
        if mlat.size < 2:
            check(f"{label}: matcher returned a path", False, note)
            continue
        matched_m = gn.track_length_m(mlat, mlon)
        err = abs(matched_m - truth_m) / truth_m
        check(
            f"{label}: matching recovers the network length",
            err < 0.10,
            f"matched {matched_m:,.0f} m vs truth {truth_m:,.0f} m ({err:+.1%}), "
            f"from a recording of {recorded_m:,.0f} m",
        )
        if note:
            print(f"     note: {note}")

        # And the ordering the whole map depends on.
        check(
            f"{label}: matched < recorded",
            matched_m < recorded_m,
            f"{matched_m:,.0f} m < {recorded_m:,.0f} m",
        )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all matching checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
