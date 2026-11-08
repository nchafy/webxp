"""Build the map's overview geometry from an already-measured directory.

    .venv/bin/python -I days/02-lines/build_overview.py days/02-lines/private

Separate from `prepare.py` on purpose: `prepare.py` does the expensive, network-bound map
matching, and this only reshapes what it wrote. Changing how the overview is drawn should
not mean matching four thousand kilometres of running again.

## Why an overview exists

A real history is too big to hand a browser whole. 401 activities at 1 Hz is 1.6M points,
about 35 MB of JSON, and re-simplifying all of it on every frame of a slider drag is
seconds of work per frame.

So the page gets two things. This file -- every activity, simplified to a stated floor --
is what draws the map. The full-resolution per-activity documents stay on disk and are
fetched one at a time when the reader selects a run, which is when the slider becomes
continuous and exact.

The floor is labelled in the interface rather than hidden. An overview drawn at 5 m and a
distance quoted at full resolution would be a map disagreeing with its own caption.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402
from prepare import COORD_DP, LENGTH_DP  # noqa: E402

#: Simplification floor for the overview, in metres.
#:
#: Chosen against the data rather than picked: at 5 m a 1 Hz running trace keeps about a
#: twentieth of its points, which takes the whole history from 35 MB to under 2 MB while
#: leaving every street corner and park path visible at city zoom. Below about 3 m the
#: saving disappears; above about 10 m the shape of a loop through a park starts to go.
OVERVIEW_FLOOR_M = 5.0


#: Grid cell for finding where most of the activity is, in degrees. About 55 km of
#: latitude, which is comfortably bigger than a city's running radius and smaller than the
#: distance between two cities.
HOME_CELL_DEG = 0.5


def densest_bbox(features: list[dict]) -> list | None:
    """The bounding box of the cluster holding the most activities.

    A history that spans continents cannot be opened by fitting all of it: the camera goes
    to world zoom and 400 traces become sub-pixel threads. This finds where the running
    actually happens -- bin each activity's first point into a half-degree cell, take the
    fullest cell and its eight neighbours, and return the box around those activities.

    Returns `[[west, south], [east, north]]`, or None when there is nothing to bin. The
    page falls back to fitting everything, which is the right behaviour for a history that
    really is all in one place.
    """
    if not features:
        return None
    cells: dict[tuple[int, int], list[dict]] = {}
    for f in features:
        lon, lat = f["geometry"]["coordinates"][0]
        key = (int(lon // HOME_CELL_DEG), int(lat // HOME_CELL_DEG))
        cells.setdefault(key, []).append(f)

    best = max(cells, key=lambda k: len(cells[k]))
    # The neighbouring cells too, so a cluster straddling a cell edge is not cut in half.
    chosen: list[dict] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            chosen.extend(cells.get((best[0] + dx, best[1] + dy), []))

    west = south = 1e9
    east = north = -1e9
    for f in chosen:
        for lon, lat in f["geometry"]["coordinates"]:
            west = min(west, lon)
            east = max(east, lon)
            south = min(south, lat)
            north = max(north, lat)
    print(
        f"home cluster holds {len(chosen)} of {len(features)} activities "
        f"({len(chosen) / len(features):.0%})"
    )
    return [[west, south], [east, north]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=pathlib.Path)
    ap.add_argument("--floor", type=float, default=OVERVIEW_FLOOR_M)
    args = ap.parse_args()

    index_path = args.data_dir / "index.json"
    if not index_path.is_file():
        print(f"no index.json in {args.data_dir}; run prepare.py first", file=sys.stderr)
        return 1
    index = json.loads(index_path.read_text())

    features = []
    kept_total = 0
    raw_total = 0
    for row in index["activities"]:
        doc_path = args.data_dir / f"{row['id']}.json"
        if not doc_path.is_file():
            print(f"  missing {doc_path.name}, skipping", file=sys.stderr)
            continue
        doc = json.loads(doc_path.read_text())
        coords = np.array(doc["recorded"])
        if coords.shape[0] < 2:
            continue
        lon, lat = coords[:, 0], coords[:, 1]
        x, y, _ = gn.project(lat, lon)
        mask = gn.simplify_mask(x, y, args.floor)
        raw_total += int(coords.shape[0])
        kept_total += int(mask.sum())

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": np.round(coords[mask], COORD_DP).tolist(),
                },
                "properties": {
                    "id": row["id"],
                    "name": row["name"],
                    "sport": row["sport"],
                    # The drawn length, which is *not* the recorded length. The interface
                    # quotes `recorded_m` from the index; this is here so a reader poking
                    # at the file can see what the simplification cost.
                    "drawn_m": round(gn.plane_length(x[mask], y[mask]), LENGTH_DP),
                },
            }
        )

    # Matched geometry too, simplified the same way. It is already far sparser than the
    # recording -- it is a path along a network, not a sample of one.
    matched = []
    for row in index["activities"]:
        doc_path = args.data_dir / f"{row['id']}.json"
        if not doc_path.is_file():
            continue
        doc = json.loads(doc_path.read_text())
        coords = np.array(doc.get("matched") or [])
        if coords.shape[0] < 2:
            continue
        lon, lat = coords[:, 0], coords[:, 1]
        x, y, _ = gn.project(lat, lon)
        mask = gn.simplify_mask(x, y, args.floor)
        matched.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": np.round(coords[mask], COORD_DP).tolist(),
                },
                "properties": {"id": row["id"], "name": row["name"]},
            }
        )

    home = densest_bbox(features)

    out = args.data_dir / "overview.json"
    out.write_text(
        json.dumps(
            {
                "floor_m": args.floor,
                "home": home,
                "recorded": {"type": "FeatureCollection", "features": features},
                "matched": {"type": "FeatureCollection", "features": matched},
            },
            separators=(",", ":"),
        )
    )
    size_mb = out.stat().st_size / 1e6
    print(f"{len(features)} recorded, {len(matched)} matched line(s)")
    print(
        f"{kept_total:,} of {raw_total:,} points kept at a {args.floor} m floor "
        f"({kept_total / max(raw_total, 1):.1%})"
    )
    print(f"wrote {out} ({size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
