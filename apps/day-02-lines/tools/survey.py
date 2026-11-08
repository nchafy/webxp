"""Survey an export before measuring it: how much data, and where in the world.

    .venv/bin/python -I days/02-lines/survey.py ~/path/to/export

Reads every activity, reports point counts, sample spacing and the bounding box, and
prices the map matching in requests. Nothing is written and nothing is fetched.

The reason this exists ahead of `prepare.py`: map matching a whole history is thousands of
requests, and `docs/gps-noise.md` says not to point that at the FOSSGIS volunteers' public
instance. Knowing the number first is what makes that a decision rather than a discovery.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402
import prepare  # noqa: E402

#: Matches `match_trace`'s default, so the request estimate is the real one.
CHUNK = 1500


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", type=pathlib.Path)
    args = ap.parse_args()

    files = prepare.find_activities(args.targets)
    print(f"{len(files)} activity file(s)")
    if not files:
        return 1

    rows = []
    sports = collections.Counter()
    failures = []
    west, south, east, north = 180.0, 90.0, -180.0, -90.0

    for i, path in enumerate(files, 1):
        if i % 50 == 0:
            print(f"  read {i}/{len(files)}...", flush=True)
        try:
            track = gn.read_activity(path)
        except Exception as exc:
            failures.append((path.name, str(exc)))
            continue
        if len(track) < 2:
            failures.append((path.name, f"only {len(track)} point(s)"))
            continue
        x, y, _ = gn.project(track.lat, track.lon)
        steps = gn.segment_lengths(x, y)
        rows.append(
            {
                "name": path.name,
                "points": len(track),
                "recorded_m": gn.plane_length(x, y),
                "step_m": float(np.median(steps)) if steps.size else float("nan"),
                "sport": track.sport or "?",
                "chunks": max(1, -(-(len(track) - 1) // (CHUNK - 1))),
            }
        )
        sports[track.sport or "?"] += 1
        west = min(west, float(track.lon.min()))
        east = max(east, float(track.lon.max()))
        south = min(south, float(track.lat.min()))
        north = max(north, float(track.lat.max()))

    if not rows:
        print("nothing readable")
        return 1

    points = np.array([r["points"] for r in rows])
    dist = np.array([r["recorded_m"] for r in rows])
    steps = np.array([r["step_m"] for r in rows])
    chunks = sum(r["chunks"] for r in rows)

    print(f"\nreadable: {len(rows)}; unreadable: {len(failures)}")
    for name, why in failures[:10]:
        print(f"  {name}: {why}")

    print(f"\npoints      total {points.sum():>12,}   median {int(np.median(points)):>7,}   max {points.max():>7,}")
    print(f"recorded    total {dist.sum() / 1000:>11,.1f}km  median {np.median(dist) / 1000:>6.2f}km  max {dist.max() / 1000:>6.2f}km")
    print(f"median step       {np.median(steps):>11.1f}m   p10 {np.percentile(steps, 10):>7.1f}m   p90 {np.percentile(steps, 90):>7.1f}m")
    print(f"\nsports: {dict(sports.most_common())}")
    print(f"\nbounding box  W {west:.4f}  S {south:.4f}  E {east:.4f}  N {north:.4f}")
    print(f"  spans {(east - west):.2f} deg lon, {(north - south):.2f} deg lat")

    print(f"\nmap matching would be {chunks:,} requests at chunk={CHUNK}")
    print(f"  against the public instance at 1 s apart: about {chunks / 60:.0f} min of sustained load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
