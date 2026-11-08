"""Find the public matcher's shape limit, so chunking can be sized to it rather than guessed.

    .venv/bin/python -I days/02-lines/probe_limits.py

`match_trace` chunks at 1500 points, which was a guess. At that size a 1.6M-point history
is over 1,200 requests against a volunteer-run service. If the instance accepts a much
larger shape, the same work is a fraction of the requests, which is the difference between
a rude job and a polite one.

Probes with a synthetic trace -- a real Valhalla route, resampled and jittered -- rather
than with anybody's actual run, since a limit probe has no business sending real data.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402

SIZES = (1500, 3000, 5000, 8000, 10000, 16000, 20000)


def route(a, b, costing="pedestrian"):
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
        res = json.loads(resp.read().decode())
    leg = res["trip"]["legs"][0]
    return gn.decode_polyline(leg["shape"], 6)


def main() -> int:
    print(f"instance: {gn.VALHALLA_URL}")
    try:
        with urllib.request.urlopen(f"{gn.VALHALLA_URL}/status", timeout=30) as resp:
            print(f"status: {json.loads(resp.read().decode())}")
    except OSError as exc:
        print(f"status failed: {exc}")

    # A long pedestrian route, densely resampled, as the probe trace.
    rlat, rlon = route((40.7030, -74.0170), (40.8105, -73.9641))
    x, y, frame = gn.project(rlat, rlon)
    s = np.concatenate([[0.0], np.cumsum(gn.segment_lengths(x, y))])
    print(f"probe route is {s[-1] / 1000:.1f} km")
    rng = np.random.default_rng(5)

    for n in SIZES:
        want = np.linspace(0.0, s[-1], n)
        lat, lon = gn.unproject(np.interp(want, s, x), np.interp(want, s, y), frame)
        px, py, pf = gn.project(lat, lon)
        lat, lon = gn.unproject(
            px + rng.normal(0, 2.0, n), py + rng.normal(0, 2.0, n), pf
        )
        body = {
            "shape": [{"lat": float(a), "lon": float(o)} for a, o in zip(lat, lon)],
            "costing": "pedestrian",
            "shape_match": "map_snap",
            "filters": {"attributes": ["shape"], "action": "include"},
        }
        req = urllib.request.Request(
            f"{gn.VALHALLA_URL}/trace_attributes",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "30daymapchallenge-gps-noise (one-off, non-commercial)",
            },
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read().decode())
            took = time.monotonic() - t0
            mlat, mlon = gn.decode_polyline(payload["shape"], 6)
            print(
                f"{n:>6} points  OK   {took:6.1f}s  matched "
                f"{gn.track_length_m(mlat, mlon) / 1000:6.2f} km"
            )
        except urllib.error.HTTPError as exc:
            took = time.monotonic() - t0
            try:
                detail = json.loads(exc.read().decode())
            except Exception:
                detail = {}
            print(
                f"{n:>6} points  HTTP {exc.code} after {took:5.1f}s  "
                f"{detail.get('error', '')!r}"
            )
            # A limit refusal is the answer; no point probing larger.
            if exc.code in (400, 413):
                break
        except OSError as exc:
            print(f"{n:>6} points  failed after {time.monotonic() - t0:5.1f}s  {exc}")
            break
        time.sleep(2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
