"""Noise per street, keyed by the OSM way the matcher put you on.

    .venv/bin/python -I days/02-lines/street_segments.py ~/path/to/strava_export

Writes `street-noise.json` beside the measured data: one LineString per way, with its
median speed-adjusted inflation and every crossing of it.

## Why this replaces the grid

The grid was a 222 m square used as a key to group repeat passes. It worked, and it was
always an admission: a square is not a place, it cuts across junctions, and the line drawn
for it was one crossing's path standing in for a box. A street is the unit the question is
actually about -- "is reception worse on this road" -- and OSM already has an identifier
for it.

`trace_attributes` returns, for every point of a trace, which graph edge it matched to, and
for every edge its `way_id`. That is the mapping from a recording to the street network,
and it is the thing the first pass never asked for.

## What an edge is, and why traversals exist

Valhalla splits ways at every node, so its edges are tiny: measured on a 1.4 km probe, 86
edges with a median length of 7 m, over 54 distinct ways. An inflation ratio over 7 m of
road is noise about noise.

So consecutive edges sharing a `way_id` are merged into one **traversal** -- one continuous
run along one street -- and a traversal has to cover `MIN_TRAVERSAL_M` before it counts.
That makes the unit "this street, this time", which is what a crossing should be.

## What is measured

For each traversal:

- **snapped** length, summed over the matched shape between the traversal's first and last
  edge. The shape rather than the reported `edge.length`, because the first and last edges
  of a match are entered part-way through (`source_percent_along`), and summing full edge
  lengths would overstate them.
- **recorded** length, summed over the raw trackpoints that matched into those edges.
- **inflation** = recorded / snapped.
- **speed** from the timestamps across those same points, from the snapped distance, so a
  noisy traversal does not read as a faster one and partly cancel the speed control.

Then the same two defences as the grid: a speed band, and a residual against other
traversals at the same speed. `gpsnoise.speed_residuals` is shared with the grid pipeline
so the two cannot disagree about what was corrected for.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402
import prepare  # noqa: E402

#: Attributes the matcher must return. Everything here is used; `edge.names` is the street
#: name when OSM carries one, which is often not.
ATTRIBUTES = [
    "edge.way_id",
    "edge.names",
    "edge.road_class",
    "edge.begin_shape_index",
    "edge.end_shape_index",
    "matched.point",
    "matched.type",
    "matched.edge_index",
    "shape",
]

#: Metres of snapped path a traversal must cover to be measured. Below this the ratio is
#: dominated by where the matcher placed two endpoints.
MIN_TRAVERSAL_M = 60.0

#: As the grid. Walking or standing still is indistinguishable from bad reception.
SPEED_BAND_MS = (2.0, 5.0)
SPACING_BAND_M = (3.0, 4.0)

#: A way needs this many traversals, from this many different activities, before it is
#: reported. One activity passing ten times is one day's satellite geometry.
MIN_TRAVERSALS = 6
MIN_ACTIVITIES = 3

#: Points per matching request.
#:
#: Deliberately far below `gpsnoise.MATCH_CHUNK`'s 15,000. That figure was measured for a
#: request that asks only for the snapped shape; this one asks for every edge and one
#: matched point per input point, so a 15,000-point chunk answers with several megabytes. At
#: that size the public instance reset the connection partway through a four-hundred-
#: activity run. Politeness is bytes as well as request count, and this trades more requests
#: for very much smaller ones.
CHUNK = 4000

#: A reset or a timeout is weather, not a verdict. Retry the chunk, backing off, before
#: giving up on it and moving on.
RETRIES = 4
BACKOFF_S = 4.0


def match_edges(lat, lon, costing):
    """Match a trace and yield `(offset, edges, matched_points, shape_lat, shape_lon)`.

    One tuple per chunk, with `offset` the index of the chunk's first point in the whole
    trace, so a matched point can be traced back to the recording it came from.
    """
    start = 0
    while start < lat.size - 1:
        stop = min(start + CHUNK, lat.size)
        body = {
            "shape": [
                {"lat": float(a), "lon": float(o)}
                for a, o in zip(lat[start:stop], lon[start:stop])
            ],
            "costing": costing,
            "shape_match": "map_snap",
            "filters": {"attributes": ATTRIBUTES, "action": "include"},
        }
        req = urllib.request.Request(
            f"{gn.VALHALLA_URL}/trace_attributes",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "30daymapchallenge-gps-noise (one-off, non-commercial)",
            },
        )
        got = None
        for attempt in range(RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    got = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode()).get("error", "")
                except Exception:
                    detail = ""
                # A 4xx is a verdict on this request. Retrying sends the same bytes again.
                print(f"    chunk {start}-{stop}: HTTP {exc.code} {detail}", file=sys.stderr)
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                # `OSError` on purpose, and it is the whole point of this block.
                # `ConnectionResetError` is an `OSError` that `urlopen` does not wrap in a
                # `URLError`, so the narrower tuple this used to catch let it through and it
                # ended a run 110 activities in. A four-hundred-request job must not stop
                # because one response was cut off.
                #
                # Named, not swallowed: a silently dropped chunk is a street that looks
                # quiet, which is the one wrong answer this whole file exists to avoid.
                wait = BACKOFF_S * (2**attempt)
                last = attempt == RETRIES - 1
                print(
                    f"    chunk {start}-{stop}: {type(exc).__name__} {exc}"
                    + ("" if last else f"; retrying in {wait:.0f}s"),
                    file=sys.stderr,
                )
                if last:
                    break
                time.sleep(wait)

        if got is not None:
            slat, slon = gn.decode_polyline(got.get("shape", ""), 6)
            yield (
                start,
                got.get("edges") or [],
                got.get("matched_points") or [],
                slat,
                slon,
            )
        start = stop - 1 if stop < lat.size else stop
        if start < lat.size - 1:
            time.sleep(1.0)


def traversals(edges, matched, offset, shape_x, shape_y, raw_x, raw_y, times, frame):
    """Merge consecutive same-way edges and measure each run along one street.

    Yields dicts with the way, the two lengths, the speed, the raw index span and the
    snapped geometry.
    """
    if not edges or not matched:
        return

    # Which raw points landed on each edge. `matched_points` is one per input point, in
    # input order, so its index is the point's index within this chunk.
    points_for = collections.defaultdict(list)
    for i, m in enumerate(matched):
        idx = m.get("edge_index")
        if idx is None or idx < 0 or idx >= len(edges):
            continue
        points_for[idx].append(i)

    # Each raw point's own snapped position, in the trace's frame.
    #
    # This, and not the edge shape, is what the snapped length is measured over. Measuring
    # the full shape span against a raw span that enters and leaves the edges part-way
    # overstates the snapped side at both ends -- it put the 5th percentile at -10.5%,
    # recorded apparently shorter than the road. Point-for-point, the two measurements
    # share their endpoints by construction and the bias is gone.
    mlat = np.array([m.get("lat", np.nan) for m in matched])
    mlon = np.array([m.get("lon", np.nan) for m in matched])
    match_x = (mlon - frame["lon0"]) * frame["kx"]
    match_y = (mlat - frame["lat0"]) * frame["ky"]

    # Group consecutive edges by way.
    group_start = 0
    for i in range(1, len(edges) + 1):
        same = i < len(edges) and edges[i].get("way_id") == edges[group_start].get("way_id")
        if same:
            continue
        span = range(group_start, i)
        group_start = i

        way = edges[span.start].get("way_id")
        if way is None:
            continue
        b = edges[span.start].get("begin_shape_index")
        e = edges[span.stop - 1].get("end_shape_index")
        if b is None or e is None or e <= b or e >= shape_x.size:
            continue

        pts = sorted(p for idx in span for p in points_for.get(idx, []))
        if len(pts) < 5:
            continue
        p0, p1 = pts[0], pts[-1]
        if not (np.isfinite(match_x[p0 : p1 + 1]).all()):
            continue

        # Both lengths over the same point span: the raw positions, and the snapped
        # positions of those same points.
        snapped = gn.plane_length(match_x[p0 : p1 + 1], match_y[p0 : p1 + 1])
        recorded = gn.plane_length(raw_x[p0 : p1 + 1], raw_y[p0 : p1 + 1])
        if snapped < MIN_TRAVERSAL_M or recorded <= 0:
            continue

        t0, t1 = times[p0], times[p1]
        dt = t1 - t0 if np.isfinite(t0) and np.isfinite(t1) else float("nan")
        speed = snapped / dt if dt and dt > 0 and np.isfinite(dt) else float("nan")

        names = edges[span.start].get("names") or []
        yield {
            "way": int(way),
            "name": names[0] if names else "",
            "road_class": edges[span.start].get("road_class") or "",
            "snapped_m": snapped,
            "recorded_m": recorded,
            "inflation": recorded / snapped,
            "speed_ms": speed,
            "i0": int(offset + p0),
            "i1": int(offset + p1),
            "geometry": np.column_stack([shape_x[b : e + 1], shape_y[b : e + 1]]),
            "shape_span": (int(b), int(e)),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=prepare.PRIVATE_OUT)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fresh", action="store_true", help="clear the resume cache first")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    files = prepare.find_activities(args.targets)
    if args.limit:
        files = files[: args.limit]
    print(f"{len(files)} activity file(s)")

    # Resume, because this job talks to the network four hundred times and has already died
    # once. Every activity's traversals are appended as one JSON line the moment they are
    # measured, and an activity already in the cache is not matched again. A death now costs
    # one activity rather than the run. `--fresh` clears it.
    args.out.mkdir(parents=True, exist_ok=True)
    cache_path = args.out / "street-cache.jsonl"
    if args.fresh and cache_path.exists():
        cache_path.unlink()
    cached: dict[str, list] = {}
    if cache_path.is_file():
        for line in cache_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                # A line half-written when the process died. Drop it and re-measure that
                # one activity rather than refusing to resume at all.
                continue
            cached[rec["activity"]] = rec["traversals"]
        print(f"  resuming: {len(cached)} already measured")
    cache = cache_path.open("a")

    rows = []
    geoms: dict[int, np.ndarray] = {}
    names: dict[int, str] = {}
    classes: dict[int, str] = {}
    used = 0
    fresh_count = 0
    skipped_sport = skipped_spacing = 0

    for n, path in enumerate(files, 1):
        print(f"[{n}/{len(files)}] {path.name}", flush=True)
        try:
            track = gn.read_activity(path)
        except Exception as exc:
            print(f"    read failed: {exc}", file=sys.stderr)
            continue
        if len(track) < 20:
            continue
        if "run" not in (track.sport or "").lower():
            skipped_sport += 1
            continue

        track.lat = np.round(track.lat, prepare.COORD_DP)
        track.lon = np.round(track.lon, prepare.COORD_DP)
        raw_x, raw_y, frame = gn.project(track.lat, track.lon)
        steps = gn.segment_lengths(raw_x, raw_y)
        if steps.size == 0:
            continue
        step = float(np.median(steps))
        if not (SPACING_BAND_M[0] <= step <= SPACING_BAND_M[1]):
            skipped_spacing += 1
            continue

        aid = prepare.activity_id(path, set())

        # One record per traversal, in a form that survives a round trip through the cache.
        # The geometry travels as lat/lon pairs: it is what the output needs, and a numpy
        # array is not JSON.
        measured = cached.get(aid)
        if measured is None:
            measured = []
            for offset, edges, matched, slat, slon in match_edges(
                track.lat, track.lon, gn.costing_for(track.sport)
            ):
                if slat.size < 2:
                    continue
                # The snapped shape is projected in the *trace's* frame, so both length
                # measurements are in one coordinate system and directly comparable.
                sx = (slon - frame["lon0"]) * frame["kx"]
                sy = (slat - frame["lat0"]) * frame["ky"]
                for t in traversals(
                    edges, matched, offset, sx, sy, raw_x, raw_y, track.t, frame
                ):
                    if not np.isfinite(t["speed_ms"]) or not (
                        SPEED_BAND_MS[0] <= t["speed_ms"] <= SPEED_BAND_MS[1]
                    ):
                        continue
                    b, e = t["shape_span"]
                    measured.append(
                        {
                            "way": t["way"],
                            "name": t["name"],
                            "road_class": t["road_class"],
                            "snapped_m": round(t["snapped_m"], 3),
                            "recorded_m": round(t["recorded_m"], 3),
                            "inflation": t["inflation"],
                            "speed_ms": t["speed_ms"],
                            "i0": t["i0"],
                            "i1": t["i1"],
                            "geometry": np.round(
                                np.column_stack([slon[b : e + 1], slat[b : e + 1]]),
                                prepare.COORD_DP,
                            ).tolist(),
                        }
                    )
            # Written the moment it is known, and flushed, so a kill -9 loses nothing that
            # has already been printed as done.
            cache.write(json.dumps({"activity": aid, "traversals": measured}) + "\n")
            cache.flush()
            cached[aid] = measured
            fresh_count += 1

        found = 0
        for t in measured:
            way = t["way"]
            rows.append((way, t, aid, track.name))
            found += 1
            # First geometry wins. Every crossing of a way snaps to the same graph edges,
            # so they agree.
            if way not in geoms:
                geoms[way] = np.array(t["geometry"])
            if t["name"] and way not in names:
                names[way] = t["name"]
            if t["road_class"] and way not in classes:
                classes[way] = t["road_class"]
        if found:
            used += 1
        print(f"    {found} traversal(s)", flush=True)

    cache.close()
    print(
        f"\n{used} activities gave {len(rows):,} traversals over "
        f"{len({r[0] for r in rows}):,} ways"
    )
    print(f"  {fresh_count} matched this run, {len(cached) - fresh_count} from the cache")
    print(f"  dropped {skipped_sport} not runs, {skipped_spacing} outside the spacing band")
    if not rows:
        print("nothing to report")
        return 1

    infl = np.array([r[1]["inflation"] for r in rows])
    speed = np.array([r[1]["speed_ms"] for r in rows])
    residual, n_bins = gn.speed_residuals(speed, infl)
    r_before = float(np.corrcoef(speed, infl)[0, 1])
    r_after = float(np.corrcoef(speed, residual)[0, 1])

    print("\n--- traversal inflation (recorded / snapped) ---")
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3} {(np.percentile(infl, q) - 1) * 100:+7.2f}%")
    print(f"\n--- speed control ({n_bins} equal-count bins) ---")
    print(f"  r speed vs inflation {r_before:+.3f}  ->  vs residual {r_after:+.3f}")
    print(
        f"  variance explained by speed: {r_before * r_before * 100:.1f}% before, "
        f"{r_after * r_after * 100:.1f}% after"
    )
    if abs(r_after) > 0.1:
        print("  WARNING: over 1% of the variance is still speed.")

    by_way: dict[int, dict] = {}
    for i, (way, t, aid, title) in enumerate(rows):
        w = by_way.setdefault(way, {"infl": [], "resid": [], "acts": set(), "passes": []})
        w["infl"].append(t["inflation"])
        w["resid"].append(float(residual[i]))
        w["acts"].add(aid)
        w["passes"].append(
            {
                "activity": aid,
                "name": title,
                "i0": t["i0"],
                "i1": t["i1"],
                "inflation_pct": round((t["inflation"] - 1) * 100, 2),
                "adjusted_pts": round(float(residual[i]) * 100, 2),
                "speed_ms": round(float(t["speed_ms"]), 2),
                "snapped_m": round(t["snapped_m"], 1),
            }
        )

    good = {
        w: v
        for w, v in by_way.items()
        if len(v["infl"]) >= MIN_TRAVERSALS and len(v["acts"]) >= MIN_ACTIVITIES
    }
    print(
        f"\n{len(by_way):,} ways touched; {len(good):,} with {MIN_TRAVERSALS}+ traversals "
        f"from {MIN_ACTIVITIES}+ activities"
    )
    if not good:
        print("no way is sampled well enough to compare")
        return 0

    stats = []
    for way, v in good.items():
        coords = geoms.get(way)
        if coords is None or len(coords) < 2:
            continue
        stats.append(
            {
                "id": str(way),
                "name": names.get(way, ""),
                "road_class": classes.get(way, ""),
                "median": float(np.median(v["infl"])),
                "resid": float(np.median(v["resid"])),
                "p90": float(np.percentile(v["infl"], 90)),
                "n": len(v["infl"]),
                "activities": len(v["acts"]),
                "passes": sorted(v["passes"], key=lambda q: -q["adjusted_pts"]),
                "geometry": np.round(coords, prepare.COORD_DP).tolist(),
                "lat": float(np.mean(coords[:, 1])),
                "lon": float(np.mean(coords[:, 0])),
            }
        )

    res_meds = np.array([s["resid"] for s in stats])
    print("\n--- speed-adjusted, across well-sampled ways (points) ---")
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3} {np.percentile(res_meds, q) * 100:+7.2f}")
    spread = (np.percentile(res_meds, 95) - np.percentile(res_meds, 5)) * 100
    print(f"  p95-p5 spread: {spread:.2f} points")

    stats.sort(key=lambda s: -s["resid"])
    header = (
        f"  {'adjusted':>9} {'raw':>8} {'n':>4} {'acts':>5} {'class':<14} name / id"
    )
    print(f"\n--- {args.top} noisiest ways ---")
    print(header)
    for s in stats[: args.top]:
        label = s["name"] or f"way {s['id']}"
        print(
            f"  {s['resid'] * 100:>+8.2f} {(s['median'] - 1) * 100:>+7.2f}% "
            f"{s['n']:>4} {s['activities']:>5} {s['road_class'][:14]:<14} {label}"
        )
    print(f"\n--- {args.top} quietest ways ---")
    print(header)
    for s in stats[-args.top :]:
        label = s["name"] or f"way {s['id']}"
        print(
            f"  {s['resid'] * 100:>+8.2f} {(s['median'] - 1) * 100:>+7.2f}% "
            f"{s['n']:>4} {s['activities']:>5} {s['road_class'][:14]:<14} {label}"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "street-noise.json"
    out.write_text(
        json.dumps(
            {
                "unit": "osm_way",
                "min_traversal_m": MIN_TRAVERSAL_M,
                "speed_band_ms": list(SPEED_BAND_MS),
                "spacing_band_m": list(SPACING_BAND_M),
                "min_traversals": MIN_TRAVERSALS,
                "min_activities": MIN_ACTIVITIES,
                "speed_inflation_r": r_before,
                "speed_residual_r": r_after,
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": s["geometry"]},
                        "properties": {
                            "id": s["id"],
                            "name": s["name"],
                            "road_class": s["road_class"],
                            "lat": round(s["lat"], 6),
                            "lon": round(s["lon"], 6),
                            "inflation_pct": round((s["median"] - 1) * 100, 3),
                            "adjusted_pts": round(s["resid"] * 100, 3),
                            "p90_pct": round((s["p90"] - 1) * 100, 3),
                            "windows": s["n"],
                            "activities": s["activities"],
                            "passes": s["passes"],
                        },
                    }
                    for s in stats
                ],
            },
            separators=(",", ":"),
        )
    )
    print(f"\nwrote {out} ({out.stat().st_size / 1e6:.2f} MB, {len(stats)} ways)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
