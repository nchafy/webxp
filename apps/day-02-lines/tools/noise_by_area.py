"""Does GPS noise depend on *where* you are? Measured per segment, not per activity.

    .venv/bin/python -I days/02-lines/noise_by_area.py ~/path/to/strava_export

Walks every trace in short windows, measures how much each window is inflated, and bins
the windows into a spatial grid. A cell with many windows from many activities gets a
median, which is the number worth looking at.

Writes a GeoJSON of cells next to the measured data, and prints the distribution.

## Why per window, and not per activity

Per-activity inflation is dominated by sample spacing: in this export the activities under
1.5 m median spacing were inflated 61% and those over 3.5 m by 0.2%. Any per-activity
comparison between places is really a comparison between how fast you were moving. A
window is short enough that the terrain under it is one kind of place.

## The confound that would otherwise invent a finding

**Standing still looks exactly like bad reception.** Stop at a traffic light with the watch
running and you get thirty points inside a four-metre circle: enormous local inflation,
and nothing to do with the sky. Bin that spatially and every junction in the city lights
up as a GPS black spot, which would be a confident, reproducible, wrong answer.

So a window only counts when its mean speed says you were running through it. That is the
single most important line in this file:

    SPEED_BAND_MS = (2.0, 5.0)

Windows outside it are dropped and counted separately, so the cost of the filter is
visible rather than hidden. Everything else here is bookkeeping.

A second, smaller confound in the same direction: slowing for a hill or a crowd raises
inflation without any change in reception. The speed band narrows that but does not remove
it, so the report prints inflation against speed *within* the band. If the relationship is
still strong inside the band, the spatial signal is not safely environmental and the right
conclusion is that this method cannot separate them.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402
import prepare  # noqa: E402

#: Metres of *simplified* path per window. Windowing on the simplified length rather than
#: the recorded length keeps windows comparable: a window of recorded length would be
#: physically shorter wherever the trace is noisy, which is the thing being measured.
WINDOW_M = 150.0

#: The simplification tolerance that defines "noise". 5 m is above the amplitude of
#: receiver jitter at a running pace and below the scale of a street corner.
FLOOR_M = 5.0

#: Mean speed a window must fall in to count, in m/s. 2.0 is a slow jog, 5.0 a fast one.
#: Below 2.0 is walking, waiting, or stopped -- see the module docstring.
SPEED_BAND_MS = (2.0, 5.0)

#: Grid cell size in degrees of latitude. 0.002 is about 222 m, so a cell is roughly a
#: couple of city blocks: small enough to separate a park from the street beside it, large
#: enough to collect repeat passes.
CELL_DEG = 0.002

#: A cell needs this many windows, from at least this many different activities, before it
#: is reported. One activity passing through ten times is one day's satellite geometry, not
#: a property of the place.
MIN_WINDOWS = 12
MIN_ACTIVITIES = 4

#: Only activities whose median spacing sits here, so spacing cannot masquerade as place.
SPACING_BAND_M = (3.0, 4.0)


def windows_for(track):
    """Yield one record per window of a track.

    Each is (lat, lon, inflation, speed_ms, simplified_m, i0, i1) -- the last two being
    the raw point indices the window spans, so a pass can be drawn back onto its own run.
    """
    x, y, _ = gn.project(track.lat, track.lon)
    keep = gn.simplify_mask(x, y, FLOOR_M)
    kept = np.flatnonzero(keep)
    if kept.size < 3:
        return
    seg = gn.segment_lengths(x, y)
    raw_cum = np.concatenate([[0.0], np.cumsum(seg)])

    start = 0
    while start < kept.size - 1:
        # Extend to at least WINDOW_M of simplified path.
        simp = 0.0
        end = start
        while end < kept.size - 1 and simp < WINDOW_M:
            a, b = kept[end], kept[end + 1]
            simp += math.hypot(x[b] - x[a], y[b] - y[a])
            end += 1
        if simp < WINDOW_M * 0.6:
            # A trailing stub, too short to be comparable.
            break

        i0, i1 = kept[start], kept[end]
        # The simplified points inside this window, which is the shape of the road rather
        # than the shape of the recording. This is what the map draws for the segment.
        span = kept[start : end + 1]
        raw = raw_cum[i1] - raw_cum[i0]
        if simp > 0 and raw > 0:
            dt = track.t[i1] - track.t[i0] if np.isfinite(track.t[i1]) and np.isfinite(track.t[i0]) else float("nan")
            # Speed from the *simplified* distance: the recorded distance includes the
            # jitter, so using it would make a noisy window look faster and partly cancel
            # the very filter this exists for.
            speed = simp / dt if dt and dt > 0 and np.isfinite(dt) else float("nan")
            yield (
                float(np.mean(track.lat[i0 : i1 + 1])),
                float(np.mean(track.lon[i0 : i1 + 1])),
                raw / simp,
                speed,
                simp,
                int(i0),
                int(i1),
                np.column_stack([track.lon[span], track.lat[span]]),
            )
        start = end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=prepare.PRIVATE_OUT)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    files = prepare.find_activities(args.targets)
    print(f"{len(files)} activity file(s)")

    rows = []
    skipped_sport = 0
    skipped_spacing = 0
    skipped_speed = 0
    used_activities = 0

    for i, path in enumerate(files, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(files)}...", flush=True)
        try:
            track = gn.read_activity(path)
        except Exception:
            continue
        if len(track) < 20:
            continue
        sport = (track.sport or "").lower()
        if "run" not in sport:
            skipped_sport += 1
            continue
        x, y, _ = gn.project(track.lat, track.lon)
        steps = gn.segment_lengths(x, y)
        if steps.size == 0:
            continue
        step = float(np.median(steps))
        if not (SPACING_BAND_M[0] <= step <= SPACING_BAND_M[1]):
            skipped_spacing += 1
            continue

        kept_any = False
        aid = prepare.activity_id(path, set())
        for lat, lon, infl, speed, simp, i0, i1, geom in windows_for(track):
            if not np.isfinite(speed) or not (SPEED_BAND_MS[0] <= speed <= SPEED_BAND_MS[1]):
                skipped_speed += 1
                continue
            rows.append((lat, lon, infl, speed, path.name, aid, i0, i1, track.name, geom))
            kept_any = True
        if kept_any:
            used_activities += 1

    print(
        f"\n{used_activities} activities contributed {len(rows):,} windows\n"
        f"  dropped: {skipped_sport} not runs, {skipped_spacing} outside "
        f"{SPACING_BAND_M[0]}-{SPACING_BAND_M[1]} m spacing, "
        f"{skipped_speed:,} windows outside {SPEED_BAND_MS[0]}-{SPEED_BAND_MS[1]} m/s"
    )
    if not rows:
        print("nothing to report")
        return 1

    infl = np.array([r[2] for r in rows])
    speed = np.array([r[3] for r in rows])
    print("\n--- window inflation (recorded / simplified) ---")
    for q in (5, 25, 50, 75, 95, 99):
        print(f"  p{q:<3} {(np.percentile(infl, q) - 1) * 100:+7.2f}%")

    # The residual confound, stated as a number rather than waved at.
    print("\n--- inflation against speed, inside the band ---")
    print(f"  {'speed':>12}  {'n':>7}  {'median inflation':>17}")
    edges = np.arange(SPEED_BAND_MS[0], SPEED_BAND_MS[1] + 0.001, 0.5)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (speed >= lo) & (speed < hi)
        if sel.sum() < 50:
            continue
        print(f"  {lo:.1f}-{hi:.1f} m/s  {int(sel.sum()):>7,}  "
              f"{(np.median(infl[sel]) - 1) * 100:>+16.2f}%")
    r = float(np.corrcoef(speed, infl)[0, 1])
    print(f"  Pearson r between speed and inflation: {r:+.3f}")
    if abs(r) > 0.3:
        print("  NOTE: still strongly related to speed inside the band. Any spatial")
        print("  pattern below may be where you run slowly, not where reception is poor.")

    # Take the speed trend out before looking at the map.
    #
    # The band narrows the confound; it does not remove it, and the r above says so. So
    # each window is compared against the median inflation of *other windows at the same
    # speed*, and what gets binned spatially is the residual. A cell that is only noisy
    # because you run slowly through it now sits at zero.
    #
    # Non-parametric on purpose: equal-count speed bins and a median, rather than a fitted
    # line. The relationship is visibly curved -- +4.5% at 2.0-2.5 m/s falling to +1.7% at
    # 4.0-4.5 -- and a straight line through that would leave structure in the residual at
    # both ends, which is exactly where the slow, built-up windows are.
    order = np.argsort(speed)
    n_bins = max(8, min(40, len(rows) // 400))
    bin_of = np.empty(len(rows), dtype=int)
    bin_of[order] = np.minimum((np.arange(len(rows)) * n_bins) // len(rows), n_bins - 1)
    expected = np.array([np.median(infl[bin_of == b]) for b in range(n_bins)])
    residual = infl - expected[bin_of]

    r_after = float(np.corrcoef(speed, residual)[0, 1])
    print(f"\n--- after removing the speed trend ({n_bins} equal-count speed bins) ---")
    print(f"  Pearson r between speed and residual: {r_after:+.3f} (was {r:+.3f})")
    # Judged on variance explained, not on r itself. r = -0.065 sounds like a residual
    # relationship and is 0.4% of the variance; the 0.1 bar is "under one percent left",
    # which is the question that matters. A bar on r alone would have to be arbitrary.
    print(
        f"  variance explained by speed: {r * r * 100:.1f}% before, "
        f"{r_after * r_after * 100:.1f}% after"
    )
    if abs(r_after) > 0.1:
        print("  WARNING: over 1% of the variance is still speed. Treat the map as unexplained.")
    print(f"  residual p5 {np.percentile(residual, 5) * 100:+.2f} pts, "
          f"p50 {np.percentile(residual, 50) * 100:+.2f} pts, "
          f"p95 {np.percentile(residual, 95) * 100:+.2f} pts")

    # Bin spatially. Both the raw inflation and the speed-adjusted residual, so the report
    # can say whether the pattern is the same one.
    cells = {}
    for idx, (lat, lon, value, spd, name, aid, i0, i1, title, geom) in enumerate(rows):
        key = (round(lat / CELL_DEG), round(lon / CELL_DEG))
        cell = cells.setdefault(
            key, {"values": [], "resid": [], "acts": set(), "passes": [], "geoms": []}
        )
        cell["geoms"].append(geom)
        cell["values"].append(value)
        cell["resid"].append(residual[idx])
        cell["acts"].add(name)
        # One pass: which run, which stretch of it, and what it measured. This is the
        # drill-down -- a segment is a place, a pass is one crossing of it, and `activity`
        # plus the index span is enough to go back to the run and draw just that stretch.
        cell["passes"].append(
            {
                "activity": aid,
                "name": title,
                "i0": i0,
                "i1": i1,
                "inflation_pct": round((value - 1) * 100, 2),
                "adjusted_pts": round(float(residual[idx]) * 100, 2),
                "speed_ms": round(float(spd), 2),
            }
        )

    good = {
        k: v
        for k, v in cells.items()
        if len(v["values"]) >= MIN_WINDOWS and len(v["acts"]) >= MIN_ACTIVITIES
    }
    print(
        f"\n{len(cells):,} cells touched; {len(good):,} with at least {MIN_WINDOWS} "
        f"windows from {MIN_ACTIVITIES}+ activities"
    )
    if not good:
        print("no cell is sampled well enough to compare")
        return 0

    stats = []
    for (ky, kx), v in good.items():
        # A stable key for the cell, so the page can filter on identity rather than on a
        # measured value -- two cells can share a median.
        cell_id = f"{ky}:{kx}"
        vals = np.array(v["values"])
        res = np.array(v["resid"])
        # One crossing stands for the segment's shape: the one whose own reading is nearest
        # the segment's median, so the line drawn is a real trace through the place rather
        # than an average of traces, which would be a path nobody ran.
        pick = int(np.argmin(np.abs(res - np.median(res))))
        geom = v["geoms"][pick]
        stats.append(
            {
                "id": cell_id,
                "lat": ky * CELL_DEG,
                "lon": kx * CELL_DEG,
                "median": float(np.median(vals)),
                "resid": float(np.median(res)),
                "passes": sorted(v["passes"], key=lambda q: -q["adjusted_pts"]),
                "geometry": np.round(geom, prepare.COORD_DP).tolist(),
                "p90": float(np.percentile(vals, 90)),
                "n": len(vals),
                "activities": len(v["acts"]),
            }
        )
    meds = np.array([s["median"] for s in stats])
    res_meds = np.array([s["resid"] for s in stats])
    print("\n--- across well-sampled cells ---")
    print(f"  {'':5} {'raw inflation':>15} {'speed-adjusted':>16}")
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<4} {(np.percentile(meds, q) - 1) * 100:>+14.2f}% "
              f"{np.percentile(res_meds, q) * 100:>+15.2f}")
    raw_spread = (np.percentile(meds, 95) - np.percentile(meds, 5)) * 100
    res_spread = (np.percentile(res_meds, 95) - np.percentile(res_meds, 5)) * 100
    print(f"\n  p95-p5 spread: {raw_spread:.2f} points raw, {res_spread:.2f} after speed")
    kept_frac = res_spread / raw_spread if raw_spread else float("nan")
    print(f"  {kept_frac:.0%} of the between-cell spread survives the speed control")
    if kept_frac < 0.4:
        print("  Most of the spatial pattern was speed. Do not call it reception.")

    # Ranked on the residual, which is the thing that might be about the place.
    stats.sort(key=lambda s: -s["resid"])
    header = f"  {'lat':>10} {'lon':>11} {'adjusted':>9} {'raw':>8} {'n':>6} {'acts':>5}"
    print(f"\n--- {args.top} noisiest cells, speed-adjusted ---")
    print(header)
    for s in stats[: args.top]:
        print(f"  {s['lat']:>10.4f} {s['lon']:>11.4f} "
              f"{s['resid'] * 100:>+8.2f} {(s['median'] - 1) * 100:>+7.2f}% "
              f"{s['n']:>6} {s['activities']:>5}")
    print(f"\n--- {args.top} quietest cells, speed-adjusted ---")
    print(header)
    for s in stats[-args.top :]:
        print(f"  {s['lat']:>10.4f} {s['lon']:>11.4f} "
              f"{s['resid'] * 100:>+8.2f} {(s['median'] - 1) * 100:>+7.2f}% "
              f"{s['n']:>6} {s['activities']:>5}")

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "area-noise.json"
    out.write_text(
        json.dumps(
            {
                "window_m": WINDOW_M,
                "floor_m": FLOOR_M,
                "cell_deg": CELL_DEG,
                "speed_band_ms": list(SPEED_BAND_MS),
                "spacing_band_m": list(SPACING_BAND_M),
                "min_windows": MIN_WINDOWS,
                "min_activities": MIN_ACTIVITIES,
                "speed_inflation_r": r,
                "speed_residual_r": r_after,
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        # The stretch of road, not the box that binned it. A grid of
                        # opaque squares reads as blotches, cuts across junctions, and
                        # claims an extent the measurement never had: a window is 150 m of
                        # path, and the square was only ever the key used to group repeats.
                        "geometry": {
                            "type": "LineString",
                            "coordinates": s["geometry"],
                        },
                        "properties": {
                            "id": s["id"],
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
    print(f"\nwrote {out} ({out.stat().st_size / 1e6:.2f} MB, {len(stats)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
