"""Build a synthetic Strava export, for checking the pipeline and for the page to draw.

    .venv/bin/python days/02-lines/make_fixture.py --out days/02-lines/fixture

Real activities are private, so they are gitignored and never committed. The page still
has to render in `scripts/check.py`, and the pipeline still has to be checkable by
someone who does not have my Strava account. So this writes an export whose *true*
answer is known: each activity is a real Valhalla route along the OSM network, resampled
and then displaced by noise of a stated sigma.

That makes it a fixture and a check at once. `truth.json` records the route length
Valhalla reported for each activity, so `prepare.py`'s output can be held against a
number that came from somewhere else.

The noise is white, which real receiver error is not -- see `gpsnoise.expected_inflation`
-- so the inflation here is larger than a real trace shows. The fixture is for checking
that the arithmetic and the rendering work, not for claiming what a real run looks like.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
import pathlib
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402

# name, sport, (lat, lon) via-points, metres between samples, noise sigma in metres.
# The spread in sigma and spacing is the point: it should produce a spread in inflation,
# which is the thing the page is about.
#
# Every leg here was checked to route near its straight-line distance before being
# used. The first draft included a Brooklyn Bridge crossing which the pedestrian router
# sent 12.9 km between endpoints 600 m apart, because an endpoint snapped to the wrong
# bank of the East River -- producing a fixture whose "truth" was a detour.
PLAN = [
    ("Riverside morning", "Run", [(40.7988, -73.9723), (40.8105, -73.9641)], 2.5, 1.5),
    ("Central Park loop", "Run", [(40.7686, -73.9742), (40.7812, -73.9665), (40.7900, -73.9580)], 3.0, 2.5),
    ("Battery to Tribeca", "Walk", [(40.7030, -74.0170), (40.7190, -74.0110)], 1.5, 3.0),
    ("Prospect Park easy", "Run", [(40.6602, -73.9690), (40.6680, -73.9730)], 3.5, 2.0),
    ("Hudson River ride", "Ride", [(40.7390, -74.0100), (40.7700, -73.9940)], 6.0, 2.5),
    ("Flushing Meadows trees", "Run", [(40.7400, -73.8430), (40.7470, -73.8400)], 3.0, 4.0),
]


def fetch_route(points: list[tuple[float, float]], costing: str) -> tuple[np.ndarray, np.ndarray, float]:
    body = {
        "locations": [{"lat": p[0], "lon": p[1]} for p in points],
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
        res = json.loads(resp.read().decode())
    legs = res["trip"]["legs"]
    lat = np.concatenate([gn.decode_polyline(l["shape"], 6)[0] for l in legs])
    lon = np.concatenate([gn.decode_polyline(l["shape"], 6)[1] for l in legs])
    return lat, lon, res["trip"]["summary"]["length"] * 1000.0


def resample(lat: np.ndarray, lon: np.ndarray, spacing_m: float):
    x, y, frame = gn.project(lat, lon)
    s = np.concatenate([[0.0], np.cumsum(gn.segment_lengths(x, y))])
    want = np.arange(0.0, s[-1], spacing_m)
    return gn.unproject(np.interp(want, s, x), np.interp(want, s, y), frame)


def write_gpx(path: pathlib.Path, name: str, sport: str, lat, lon, start: dt.datetime, dt_s: float):
    rows = []
    for i, (a, o) in enumerate(zip(lat, lon)):
        stamp = (start + dt.timedelta(seconds=i * dt_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(
            f'   <trkpt lat="{a:.6f}" lon="{o:.6f}"><ele>{10 + 5 * math.sin(i / 200):.1f}</ele>'
            f"<time>{stamp}</time></trkpt>"
        )
    body = "\n".join(rows)
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="make_fixture.py" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        f" <trk>\n  <name>{name}</name>\n  <type>{sport.lower()}</type>\n  <trkseg>\n"
        f"{body}\n  </trkseg>\n </trk>\n</gpx>\n"
    )
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("days/02-lines/fixture"))
    args = ap.parse_args()

    acts = args.out / "activities"
    acts.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(2026)
    truth = {}
    csv_rows = []
    day = dt.datetime(2026, 10, 20, 7, 30)

    for i, (name, sport, vias, spacing, sigma) in enumerate(PLAN):
        costing = gn.costing_for(sport)
        print(f"routing {name} ({costing})...", flush=True)
        rlat, rlon, route_m = fetch_route(vias, costing)
        slat, slon = resample(rlat, rlon, spacing)
        x, y, frame = gn.project(slat, slon)
        nlat, nlon = gn.unproject(
            x + rng.normal(0, sigma, x.size), y + rng.normal(0, sigma, y.size), frame
        )

        aid = f"20261020{i:04d}"
        fname = f"{aid}.gpx.gz"
        # 1 Hz for the foot activities, which is what a watch writes; the ride samples
        # further apart because it moves faster at the same cadence.
        dt_s = 1.0
        start = day + dt.timedelta(days=i, hours=i % 3)
        write_gpx(acts / fname, name, sport, nlat, nlon, start, dt_s)

        recorded_m = gn.track_length_m(nlat, nlon)
        truth[fname] = {
            "name": name,
            "sport": sport,
            "route_m": round(route_m, 1),
            "recorded_m": round(recorded_m, 1),
            "spacing_m": spacing,
            "sigma_m": sigma,
            "predicted_inflation": round(gn.expected_inflation(spacing, sigma), 4),
            "points": int(nlat.size),
        }
        # Strava's own figure stands in for a smoothed distance: take the route length,
        # which is what a well-behaved algorithm would land near, not the raw sum.
        csv_rows.append(
            {
                "Activity ID": aid,
                "Activity Date": start.strftime("%b %d, %Y, %I:%M:%S %p"),
                "Activity Name": name,
                "Activity Type": sport,
                "Elapsed Time": int(nlat.size * dt_s),
                # Two differently-scaled distance columns, as real exports have carried:
                # the first in kilometres, the second in metres. `prepare.py` must work
                # out which is which by measurement rather than by trusting the header.
                "Distance": f"{route_m / 1000:.2f}",
                "Distance.1": f"{route_m:.1f}",
                "Filename": f"activities/{fname}",
            }
        )
        print(f"  route {route_m:,.0f} m, recorded {recorded_m:,.0f} m "
              f"({recorded_m / route_m:.2f}x), {nlat.size} pts")
        time.sleep(1.0)

    import csv as _csv

    with open(args.out / "activities.csv", "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
        w.writeheader()
        w.writerows(csv_rows)

    (args.out / "truth.json").write_text(json.dumps(truth, indent=1))
    print(f"\nwrote {len(PLAN)} activities to {args.out}")
    print(f"truth in {args.out / 'truth.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
