"""Find out what `trace_attributes` returns for edges, before anything is built on it.

    .venv/bin/python -I days/02-lines/probe_edges.py

Street-level segments need three things the current pipeline never asked for: the OSM way
id behind each matched edge, the edge's own length, and which raw trackpoints matched to
it. Print the real response rather than coding against a remembered shape.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
import urllib.request

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402

WANT = [
    "edge.way_id",
    "edge.names",
    "edge.length",
    "edge.begin_shape_index",
    "edge.end_shape_index",
    "edge.road_class",
    "matched.point",
    "matched.type",
    "matched.edge_index",
    "matched.begin_route_discontinuity",
    "matched.end_route_discontinuity",
    "shape",
]


def post(action: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{gn.VALHALLA_URL}/{action}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "30daymapchallenge-gps-noise (one-off, non-commercial)",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    # A real route, resampled and jittered, so the matcher has something plausible.
    route = post(
        "route",
        {
            "locations": [
                {"lat": 47.6100, "lon": -122.3240},
                {"lat": 47.6190, "lon": -122.3330},
            ],
            "costing": "pedestrian",
            "units": "kilometers",
            "directions_type": "none",
        },
    )
    rlat, rlon = gn.decode_polyline(route["trip"]["legs"][0]["shape"], 6)
    x, y, frame = gn.project(rlat, rlon)
    s = np.concatenate([[0.0], np.cumsum(gn.segment_lengths(x, y))])
    want = np.arange(0.0, s[-1], 3.0)
    lat, lon = gn.unproject(np.interp(want, s, x), np.interp(want, s, y), frame)
    rng = np.random.default_rng(3)
    px, py, pf = gn.project(lat, lon)
    lat, lon = gn.unproject(
        px + rng.normal(0, 2.5, px.size), py + rng.normal(0, 2.5, py.size), pf
    )
    print(f"probe trace: {lat.size} points over {route['trip']['summary']['length'] * 1000:.0f} m")

    res = post(
        "trace_attributes",
        {
            "shape": [{"lat": float(a), "lon": float(o)} for a, o in zip(lat, lon)],
            "costing": "pedestrian",
            "shape_match": "map_snap",
            "filters": {"attributes": WANT, "action": "include"},
        },
    )

    print(f"\ntop-level keys: {sorted(res.keys())}")
    edges = res.get("edges") or []
    matched = res.get("matched_points") or []
    print(f"edges: {len(edges)}   matched_points: {len(matched)}")

    if edges:
        print(f"\nedge[0] keys: {sorted(edges[0].keys())}")
        print("\nfirst 6 edges:")
        for e in edges[:6]:
            names = e.get("names")
            print(
                f"  way_id={e.get('way_id')!s:>12}  length={e.get('length')}  "
                f"shape[{e.get('begin_shape_index')}..{e.get('end_shape_index')}]  "
                f"class={e.get('road_class')}  names={names}"
            )
        have = {k: sum(1 for e in edges if k in e) for k in
                ("way_id", "length", "begin_shape_index", "end_shape_index", "names")}
        print(f"\nfield presence across {len(edges)} edges: {have}")
        ways = [e.get("way_id") for e in edges if e.get("way_id") is not None]
        runs = [k for k, _ in collections.Counter(ways).most_common(5)]
        print(f"distinct way_ids: {len(set(ways))}; most repeated: {runs}")
        lens = np.array([e["length"] for e in edges if "length" in e]) * 1000
        if lens.size:
            print(
                f"edge length (m): min {lens.min():.1f}  median {np.median(lens):.1f}  "
                f"max {lens.max():.1f}  total {lens.sum():.0f}"
            )

    if matched:
        print(f"\nmatched_point[0] keys: {sorted(matched[0].keys())}")
        print("first 5 matched points:")
        for m in matched[:5]:
            print(f"  {m}")
        kinds = collections.Counter(m.get("type") for m in matched)
        print(f"types: {dict(kinds)}")
        has_edge = sum(1 for m in matched if m.get("edge_index") is not None)
        print(f"{has_edge}/{len(matched)} carry edge_index")
        # Does matched_points line up with the input, one per point?
        print(f"input points {lat.size}, matched_points {len(matched)}: "
              f"{'one per input point' if len(matched) == lat.size else 'DIFFERENT LENGTH'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
