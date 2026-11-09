"""Aggregate a measured directory into the one statistics file that can be committed.

    .venv/bin/python -I apps/day-02-lines/tools/make_summary.py apps/day-02-lines/private
    .venv/bin/python -I apps/day-02-lines/tools/make_summary.py --self-test

Writes `apps/day-02-lines/data/noise-summary.json`. Every figure quoted in `lines.mjs`,
`geo.mjs`, the tool docstrings and `README.md` about the real export comes from here, so a
reader can check a number without being handed a trace.

## Why this file exists

An activity trace records where someone lives and when they are not home, so the export and
the per-activity documents built from it are gitignored. That left every figure in the
documentation unverifiable: a statistic whose only reproduction path is "run this against
your own Strava account" is an assertion.

So the *aggregates* are committed and the geometry is not. Nothing here narrows where
anybody runs.

## What is deliberately excluded, and why

The header written into the file repeats this list, because the file travels further than
this docstring.

- **No coordinates.** Not a point, not a centroid, not a bounding box, not a cell id. Any
  one of those is a map of where somebody runs, which is the whole thing being protected.
  `overview.lon_span_deg` and `overview.home_span_deg` are *widths* with no position: a
  span of 262 degrees says the runner has been to two continents and nothing about which.
- **No activity ids, names or dates.** An id resolves against Strava's own URLs; a name is
  often a place; a date plus a span is an itinerary. Counts and distributions only.
- **No street or way ids.** An OSM way id is a street, by name, in one query.
- **No per-item rows, with one named exception.** Counts, percentiles, class counts and the
  headline figures the docs cite. A long tail of per-street values is a fingerprint even
  without an id, because the set of streets somebody runs is close to unique. The exception
  is `streets.ghost_sampling`: five scalars about one street, the most-crossed one, because
  `lines.mjs` cites them for the overlay's sampling bias and dropping the row would break a
  live citation. They carry no id, no name and no geometry: the row says how often the
  busiest street is crossed and how far the drawn sample sits from its own mean, and nothing
  that resolves to a street.
- **Timestamps are excluded, including this file's own.** A build time is one more fact
  about when somebody was at a keyboard, and the figures are reproducible without it.

What remains is a few dozen scalars over one measured export. Counts are not quoted here;
they are fields in the file, where they cannot go stale. The test for adding a field is
whether it would narrow a search for the runner; if the answer is "only a little", it does
not go in.

`scan_for_identifiers` is the gate that holds the list above, and `--self-test` is the
negative control for it. A change to the gate MUST be run against that table.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402
import prepare  # noqa: E402

APP = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = APP / "data" / "noise-summary.json"

#: The class edges `lines.mjs` paints the segments with. One definition would be better than
#: two, and there is no import across the language boundary; the guard is that
#: `class_counts` below is written from *this* list, so a change there and not in `lines.mjs`
#: shows up as a legend that disagrees with the file it was built from.
NOISE_BREAKS = (1.0, 2.0, 3.0, 4.5, 7.0, 10.0)

#: How many crossings the overlay draws. Mirrors `GHOST_CAP` in `lines.mjs`.
GHOST_CAP = 12

#: The header the file carries about itself. Stated in the data rather than only here,
#: because the file is read on its own.
EXCLUDED = [
    "no coordinates: no points, centroids, bounding boxes or cell ids",
    "no activity ids, names or dates",
    "no OSM way ids or street names",
    (
        "aggregates, with one exception: streets.ghost_sampling is five scalars about the "
        "single most-crossed street, carrying no id, name or geometry"
    ),
    "no timestamps, including this file's own build time",
]

WHY = (
    "An activity trace records where someone lives and when they are not home. The export "
    "and everything built from it are gitignored, so the figures the documentation quotes "
    "would otherwise be unreproducible. These aggregates are committed in their place; "
    "nothing here narrows where anybody runs."
)

def crop_stats(index: dict) -> dict:
    """What was cropped off the ends before anything was measured, read from `index.json`.

    This is the first thing a reader of this file needs, because it changes what every other
    figure *means*. A cropped "recorded length" is the middle of a run rather than the run,
    and the crop removes about 8% of the distance and roughly a quarter of the measured
    inflation -- the ends are where the receiver is coldest and where somebody stands still.
    Figures from a cropped and an uncropped export are different quantities.

    Both numbers are the crop's **identity**, not details of it. Cropping 402 m of path is a
    different operation from cropping 402 m of path *and* 200 m of straight-line
    displacement, and only the second produced this data: path distance is not displacement,
    and a watch left running at a door piles up metres without going anywhere.

    `dropped` is how many activities could not survive the crop, which is what lets a reader
    tell 397-of-401 from 397-of-397.
    """
    crop = index.get("crop")
    if not crop:
        # Named rather than assumed either way. An older `index.json` predates the field,
        # and guessing "uncropped" would publish every figure under a false quantity.
        return {
            "recorded": False,
            "note": (
                "index.json carries no crop field, so this export predates the record and "
                "the crop cannot be derived. Re-run prepare.py to stamp it."
            ),
        }
    return {
        "recorded": True,
        "trim_m": crop.get("trim_m"),
        "min_end_offset_m": crop.get("min_end_offset_m"),
        "dropped": crop.get("dropped"),
        "means": (
            "every length here is the cropped middle of a run, not the whole run. Both "
            "figures are the crop's identity: a quarter mile of recorded path off each end, "
            "and then inward until the kept end is at least min_end_offset_m straight-line "
            "from the original end. Path distance is not displacement."
        ),
    }


def pct(a: np.ndarray, q: float) -> float:
    """A percentile, rounded to the two decimals the docs quote.

    numpy's default linear interpolation. Worth stating, because the estimator moves the
    answer by more than the rounding: p10 of the street inflations is 1.23% here and 1.22%
    under `method="lower"`.
    """
    return round(float(np.percentile(a, q)), 2)


def even_sample(items: list, cap: int) -> list:
    """`evenSample` from `lines.mjs`, so the overlay's bias can be quoted from a measurement.

    Reimplemented rather than imported -- the original is JavaScript -- and the thing that
    keeps the two honest is that both are three lines with no branches.
    """
    if len(items) <= cap:
        return items
    step = (len(items) - 1) / (cap - 1)
    # `round` is Python's banker's rounding and `Math.round` is half-up, so they part on an
    # exact .5. `step` is a ratio of integers, which lands on .5 for some caps, so this is
    # spelled out rather than left to `round`.
    return [items[int(i * step + 0.5)] for i in range(cap)]


def overlay_fits_activities(doc: dict, points: dict[str, int]) -> dict:
    """Whether an overlay file's crossings still index the activity documents beside it.

    Both overlay files store each crossing as an index span `[i0, i1]` into one activity's
    `recorded` array, and `lines.mjs` slices that array to draw the ghosts. So the overlay
    is only meaningful against the per-activity documents it was built from. Change the
    geometry under it -- re-run `prepare.py` with a different `--trim-m`, for instance --
    and every index shifts.

    The failure is silent and it is the bad kind. A span past the end slices short and the
    crossing vanishes, so a noisy street reads as a quiet one; a span still in range points
    at a different stretch of road and gets drawn with full confidence.

    `i1 >= points` is the cheap half and catches a shortened trace. It does not catch a
    trace that grew, so the count is a floor on the damage, not a measurement of it.
    """
    total = 0
    past_end = 0
    unknown = 0
    for feature in doc["features"]:
        for crossing in feature["properties"]["passes"]:
            total += 1
            n = points.get(crossing["activity"])
            if n is None:
                unknown += 1
            elif crossing["i1"] >= n:
                past_end += 1
    return {
        "crossings": total,
        "crossings_past_the_end_of_their_run": past_end,
        "crossings_from_an_unknown_run": unknown,
        "spans_fit_their_runs": past_end == 0 and unknown == 0,
    }


def street_stats(path: pathlib.Path) -> dict:
    doc = json.loads(path.read_text())
    props = [f["properties"] for f in doc["features"]]
    infl = np.array([p["inflation_pct"] for p in props])
    adj = np.array([p["adjusted_pts"] for p in props])
    windows = np.array([p["windows"] for p in props])

    edges = [-np.inf, *NOISE_BREAKS, np.inf]
    class_counts = [
        int(((infl >= edges[i]) & (infl < edges[i + 1])).sum()) for i in range(len(edges) - 1)
    ]

    # The overlay's bias, from the most-crossed street. `passes` arrives sorted by
    # descending `adjusted_pts`, so the head of the list is the noisy tail and taking it
    # would make the drawn bundle wider than the street ever was.
    worst = props[int(np.argmax(windows))]
    pas = worst["passes"]
    head = np.array([q["adjusted_pts"] for q in pas[:GHOST_CAP]])
    even = np.array([q["adjusted_pts"] for q in even_sample(pas, GHOST_CAP)])
    allp = np.array([q["adjusted_pts"] for q in pas])

    crossing_infl = np.array([q["inflation_pct"] for p in props for q in p["passes"]])

    return {
        "streets": len(props),
        "inflation_pct": {
            "above_zero": int((infl > 0).sum()),
            "above_zero_frac": round(float((infl > 0).mean()), 4),
            "min": round(float(infl.min()), 2),
            "p10": pct(infl, 10),
            "median": pct(infl, 50),
            "p90": pct(infl, 90),
            "max": round(float(infl.max()), 2),
            "percentile_method": "numpy default (linear interpolation)",
        },
        "adjusted_pts": {
            "p5": pct(adj, 5),
            "median": pct(adj, 50),
            "p95": pct(adj, 95),
            "p95_p5_spread": round(pct(adj, 95) - pct(adj, 5), 2),
        },
        "noise_breaks": {
            "edges_pct": list(NOISE_BREAKS),
            "class_counts": class_counts,
            "mirrors": "NOISE_BREAKS in lines.mjs",
        },
        "crossings": {
            "total": int(windows.sum()),
            "p5_pct": pct(crossing_infl, 5),
            "median_pct": pct(crossing_infl, 50),
            "p90_pct": pct(crossing_infl, 90),
        },
        "ghost_sampling": {
            "cap": GHOST_CAP,
            "streets_over_cap": int((windows > GHOST_CAP).sum()),
            "max_crossings": int(windows.max()),
            "on_max_street_head_mean_adj_pts": round(float(head.mean()), 2),
            "on_max_street_even_mean_adj_pts": round(float(even.mean()), 2),
            "on_max_street_all_mean_adj_pts": round(float(allp.mean()), 2),
            # Differences, not a ratio. `adjusted_pts` is a speed residual and sits either
            # side of zero, so a street's mean can be negative -- and a ratio of two means
            # across zero is not an overstatement factor, it is noise with a sign. An
            # earlier version of this field reported "-2.15x" for a head that is plainly
            # 5.5 points too high. Points are the unit the panel already prints.
            "on_max_street_head_minus_all_pts": round(float(head.mean() - allp.mean()), 2),
            "on_max_street_even_minus_all_pts": round(float(even.mean() - allp.mean()), 2),
        },
        "gates": {
            "min_traversal_m": doc.get("min_traversal_m"),
            "speed_band_ms": doc.get("speed_band_ms"),
            "spacing_band_m": doc.get("spacing_band_m"),
            "min_traversals": doc.get("min_traversals"),
            "min_activities": doc.get("min_activities"),
        },
    }


def grid_stats(path: pathlib.Path) -> dict:
    doc = json.loads(path.read_text())
    props = [f["properties"] for f in doc["features"]]
    raw = np.array([p["inflation_pct"] for p in props])
    adj = np.array([p["adjusted_pts"] for p in props])
    raw_spread = pct(raw, 95) - pct(raw, 5)
    adj_spread = pct(adj, 95) - pct(adj, 5)

    speed = np.array([q["speed_ms"] for p in props for q in p["passes"]])
    window_infl = np.array([q["inflation_pct"] for p in props for q in p["passes"]])
    by_speed = []
    lo = doc["speed_band_ms"][0]
    while lo < doc["speed_band_ms"][1] - 1e-9:
        sel = (speed >= lo) & (speed < lo + 0.5)
        # 50 is the same floor `noise_by_area.py` prints its table at: below it a median is
        # a handful of windows and reads as structure.
        if sel.sum() >= 50:
            by_speed.append(
                {
                    "band_ms": [round(lo, 1), round(lo + 0.5, 1)],
                    "n": int(sel.sum()),
                    "median_inflation_pct": round(float(np.median(window_infl[sel])), 2),
                }
            )
        lo += 0.5

    r_before = doc["speed_inflation_r"]
    r_after = doc["speed_residual_r"]
    return {
        "cells": len(props),
        "cell_deg": doc.get("cell_deg"),
        "cell_m_approx": round(float(doc.get("cell_deg") or 0) * 111_000),
        "window_m": doc.get("window_m"),
        "floor_m": doc.get("floor_m"),
        "min_windows": doc.get("min_windows"),
        "min_activities": doc.get("min_activities"),
        "between_cell_spread_pts": {
            "raw": round(raw_spread, 2),
            "after_speed_control": round(adj_spread, 2),
            "survives_frac": round(adj_spread / raw_spread, 3) if raw_spread else None,
            "definition": "p95 minus p5 of the per-cell medians, in points",
        },
        "speed_control": {
            "r_before": round(float(r_before), 3),
            "r_after": round(float(r_after), 3),
            "variance_explained_before_frac": round(float(r_before) ** 2, 4),
            "variance_explained_after_frac": round(float(r_after) ** 2, 4),
            # Windows kept by a reported cell, not every window measured: the per-window
            # values only survive into the file for cells that cleared `min_windows`.
            "windows_in_reported_cells": int(speed.size),
            "median_inflation_by_speed": by_speed,
        },
    }


def activity_stats(data_dir: pathlib.Path) -> dict:
    index = json.loads((data_dir / "index.json").read_text())
    rows = index["activities"]
    points = np.array([r["points"] for r in rows])
    step = np.array([r["step_m"] or np.nan for r in rows])

    # Matched is required; a Strava figure is not. Requiring all three empties the set
    # whenever the CSV column did not resolve -- which `prepare.py --trim-m` makes the normal
    # case, because its lengths cover the cropped middle and Strava's cover the whole run --
    # and an empty set divides to `nan` and then raises out of `np.percentile`.
    have = [r for r in rows if r.get("matched_m")]
    if not have:
        raise SystemExit(
            f"nothing in {data_dir}/index.json carries a matched length; "
            "re-run prepare.py with matching enabled"
        )
    with_strava = [r for r in have if r.get("strava_m")]
    rec = np.array([r["recorded_m"] for r in have])
    mat = np.array([r["matched_m"] for r in have])
    infl = (rec / mat - 1) * 100

    size_mb = sum((data_dir / f"{r['id']}.json").stat().st_size for r in rows) / 1e6

    # By sport, because the aggregate hides that hikes behave completely differently. The
    # sport name is a category, not an identifier.
    by_sport = []
    for sport in sorted({r["sport"] or "?" for r in have}):
        sel = [r for r in have if (r["sport"] or "?") == sport]
        r_ = np.array([x["recorded_m"] for x in sel])
        m_ = np.array([x["matched_m"] for x in sel])
        by_sport.append(
            {
                "sport": sport,
                "n": len(sel),
                "median_inflation_pct": round(float(np.median((r_ / m_ - 1) * 100)), 2),
                "total_inflation_pct": round(float((r_.sum() / m_.sum() - 1) * 100), 2),
            }
        )

    # `infl` is indexed over `have` and `step` over `rows`, so the bands below are only
    # meaningful when the two are the same set. Asserted rather than assumed: a silent
    # misalignment would put a real-looking median against the wrong spacing band.
    step_aligned = np.array([r["step_m"] or np.nan for r in have])
    bands = [(0.0, 1.5), (1.5, 2.5), (2.5, 3.0), (3.0, 3.5), (3.5, 4.0), (4.0, None)]
    by_spacing = []
    for low, high in bands:
        sel = np.isfinite(step_aligned) & (step_aligned >= low)
        if high is not None:
            sel &= step_aligned < high
        if not sel.any():
            continue
        by_spacing.append(
            {
                "band_m": [low, high],
                "n": int(sel.sum()),
                "median_inflation_pct": round(float(np.median(infl[sel])), 2),
            }
        )

    out = {
        "activities": len(rows),
        "unreadable": len(index.get("failures", [])),
        "matched": len(have),
        "with_a_strava_figure": len(with_strava),
        "points_total": int(points.sum()),
        "per_activity_json_mb": round(size_mb, 2),
        "recorded_km": round(float(rec.sum()) / 1000, 1),
        "matched_km": round(float(mat.sum()) / 1000, 1),
        "recorded_over_matched_pct": {
            "p5": round(float(np.percentile(infl, 5)), 2),
            "median": round(float(np.median(infl)), 2),
            "p95": round(float(np.percentile(infl, 95)), 2),
            "max": round(float(infl.max()), 1),
            "negative": int((infl < 0).sum()),
        },
        "totals_pct": {
            "recorded_over_matched": round(float((rec.sum() / mat.sum() - 1) * 100), 2),
        },
        "by_sport": by_sport,
        "median_inflation_by_spacing": by_spacing,
        "chunk_offset": chunk_offset_stats(data_dir, rows),
    }
    if with_strava:
        s_mat = np.array([r["matched_m"] for r in with_strava])
        s_rec = np.array([r["recorded_m"] for r in with_strava])
        s_stv = np.array([r["strava_m"] for r in with_strava])
        out["totals_pct"]["recorded_over_strava"] = round(
            float((s_rec.sum() / s_stv.sum() - 1) * 100), 2
        )
        out["strava_over_matched_median_ratio"] = round(float(np.median(s_stv / s_mat)), 4)
        out["matched_under_80pct_of_strava"] = int((s_mat / s_stv < 0.8).sum())
    else:
        # Named in the data, not left as an absent key a reader would read as zero.
        out["strava_figures"] = (
            "none resolved; sections comparing against Strava's own distance are absent"
        )
    return out


def chunk_offset_stats(data_dir: pathlib.Path, rows: list[dict]) -> dict:
    """How far a mixed-up index space could reach, as a count rather than an anecdote.

    `street_segments.py` matches in `CHUNK`-point requests and the matched points of each
    chunk are indexed from zero. Reading the whole trace at those indices measures the wrong
    part of the run. This counts the exposure: activities that span more than one chunk, and
    reported streets carrying at least one crossing read out of a second-or-later chunk.
    """
    import importlib

    street = importlib.import_module("street_segments")
    chunk = street.CHUNK
    big = {r["id"] for r in rows if r["points"] > chunk}
    out = {
        "chunk_points": chunk,
        "activities_over_one_chunk": len(big),
        "activities": len(rows),
    }
    grid = data_dir / "street-noise.json"
    if grid.is_file():
        doc = json.loads(grid.read_text())
        exposed = 0
        for f in doc["features"]:
            if any(
                q["activity"] in big and q["i0"] >= chunk - 1 for q in f["properties"]["passes"]
            ):
                exposed += 1
        out["streets_with_a_second_chunk_crossing"] = exposed
        out["streets"] = len(doc["features"])
    return out


def overview_stats(data_dir: pathlib.Path) -> dict:
    """Widths, never positions.

    `lon_span_deg` is why the camera is not fitted to the data and `home_span_deg` is why
    `overview.home` is not used as an opening view. Both are the *size* of a box. Neither
    says where it is, and the two are reported to two decimals so they cannot be combined
    with anything to recover one.
    """
    path = data_dir / "overview.json"
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text())
    lons: list[float] = []
    lats: list[float] = []
    for f in doc["recorded"]["features"]:
        geom = f["geometry"]
        parts = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
        for part in parts:
            for lon, lat in part:
                lons.append(lon)
                lats.append(lat)
    out = {
        "floor_m": doc.get("floor_m"),
        "overview_json_mb": round(path.stat().st_size / 1e6, 2),
        "overview_points": len(lons),
    }
    if lons:
        out["lon_span_deg"] = round(max(lons) - min(lons), 2)
        out["lat_span_deg"] = round(max(lats) - min(lats), 2)
    home = doc.get("home")
    if home:
        (w, s), (e, n) = home
        out["home_span_deg"] = round(e - w, 2)
        out["home_lat_span_deg"] = round(n - s, 2)
    return out


def ladder_floor() -> dict:
    """What a JS/Python ladder comparison has to be stated above.

    Not a measurement of the two implementations -- that needs `node` -- but the rounding
    floor underneath any such comparison, which is a property of `prepare.LENGTH_DP` alone.
    """
    return {
        "length_dp": prepare.LENGTH_DP,
        "rounding_floor_m": round(0.5 * 10.0**-prepare.LENGTH_DP, 6),
        "tolerances_m": list(gn.TOLERANCES_M),
        "rungs_per_activity": len(gn.TOLERANCES_M),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "data_dir",
        nargs="?",
        type=pathlib.Path,
        default=prepare.PRIVATE_OUT,
        help=f"a directory `prepare.py` wrote (default {prepare.PRIVATE_OUT})",
    )
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="run the identifier scan over its probe table and exit; reads no export",
    )
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not (args.data_dir / "index.json").is_file():
        # Named, not a traceback: the usual cause is pointing this at the app folder rather
        # than at the measured directory inside it.
        print(f"no index.json in {args.data_dir} -- run prepare.py first", file=sys.stderr)
        return 1

    index = json.loads((args.data_dir / "index.json").read_text())
    points = {a["id"]: a["points"] for a in index["activities"]}

    summary = {
        "what": "Aggregates over one measured Strava export. No geometry, no identifiers.",
        "why_aggregated": WHY,
        "excluded": EXCLUDED,
        "produced_by": "apps/day-02-lines/tools/make_summary.py",
        "reproduce": "apps/day-02-lines/tools/make_summary.py <a directory prepare.py wrote>",
        "crop": crop_stats(index),
        "ladder": ladder_floor(),
        "export": activity_stats(args.data_dir),
        "overview": overview_stats(args.data_dir),
    }

    streets = args.data_dir / "street-noise.json"
    if streets.is_file():
        summary["streets"] = street_stats(streets)
        summary["streets"]["dataset"] = overlay_fits_activities(
            json.loads(streets.read_text()), points
        )
    grid = args.data_dir / "area-noise.json"
    if grid.is_file():
        summary["grid"] = grid_stats(grid)
        summary["grid"]["dataset"] = overlay_fits_activities(
            json.loads(grid.read_text()), points
        )

    # Refuse rather than write a half file. A summary missing the street figures while every
    # doc comment cites them is worse than no summary: the citations would resolve to a file
    # that does not carry them.
    missing = [k for k in ("streets", "grid") if k not in summary]
    if missing:
        print(
            f"{args.data_dir} has no {', '.join(missing)} data -- "
            "run street_segments.py and noise_by_area.py first",
            file=sys.stderr,
        )
        return 1

    # The scan that makes the privacy claim a check rather than an intention: key names that
    # could carry a position, and values that read as a position or an id. Shown capable of
    # failing by `--self-test`, which runs it over `SCAN_PROBES` and returns 1 if a probe
    # lands on the wrong side. Run before anything is written, so a refusal never leaves a
    # file behind.
    leaked = scan_for_identifiers(summary)
    if leaked:
        print("refusing to write: " + "; ".join(leaked), file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=1, sort_keys=False) + "\n")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1000:.1f} kB)")

    # The overlay files and the activity documents must describe one dataset, and the file
    # records which. Written and *then* reported as a failure, deliberately: the figures are
    # still the figures the page is drawing from right now, so a reader needs them, and the
    # non-zero exit is what stops a harness from calling this clean.
    stale = [k for k in ("streets", "grid") if not summary[k]["dataset"]["spans_fit_their_runs"]]
    if stale:
        for key in stale:
            d = summary[key]["dataset"]
            print(
                f"\n{key}: {d['crossings_past_the_end_of_their_run']} of "
                f"{d['crossings']} crossings index past the end of their activity, and "
                f"{d['crossings_from_an_unknown_run']} name an activity that is gone.",
                file=sys.stderr,
            )
        print(
            "\nThe overlay files predate the per-activity documents beside them. Their\n"
            "index spans point at the wrong stretch of road, so the drill-down draws the\n"
            "wrong geometry and a dropped crossing reads as a quiet street. The figures\n"
            "above describe the overlay as it stands and are marked `spans_fit_their_runs:\n"
            "false`. Rebuild with `street_segments.py --fresh` and `noise_by_area.py`.",
            file=sys.stderr,
        )
        return 1
    return 0


#: Key name *words* that would carry a position or an identifier.
#:
#: Matched token by token, splitting a key on underscores, rather than as substrings. A
#: substring rule reads "lat" inside "inflation" and "id" inside "grid", so it refuses the
#: whole file over a key carrying a percentage -- which is a check that fails closed on its
#: own output and therefore gets switched off. Token matching still catches a field added as
#: `start_lat`, `way_id` or `activity_name`, which is the case this is for.
#:
#: Singular where the plural is a count: `activity` is an identifier, `activities` is how
#: many.
#:
#: `cell`, `street` and `point` are deliberately *not* here. A cell size, a per-street
#: statistic and a point count are all aggregates, and forbidding the words would flag
#: `cell_deg` and `chunk_points` -- a gate that fires on the file's own legitimate output is
#: a gate someone switches off. What makes those fields safe is `id` and `name`, which catch
#: `cell_id`, `way_id` and `street_name`.
#:
#: `home` and `center` are here because `overview.json` already carries a `home` bounding
#: box and `overview_stats` already reads `doc["home"]`. One edit that forwards the field
#: instead of differencing it ships the box, which makes it the likeliest accident in the
#: pipeline rather than a hypothetical one.
#:
#: The residual risk this leaves is a free-text field holding a place name under an innocent
#: key. The value gate below is the second layer and reads numbers and strings, not prose, so
#: a string field added here needs reading rather than trusting.
FORBIDDEN_TOKENS = frozenset(
    """lat lats latitude latitudes lon lons lng longitude longitudes
       coord coords coordinate coordinates bbox bounds box extent centroid
       center centre home
       id ids uuid guid name names title date dates datetime timestamp
       activity way ways""".split()
)

#: Substrings that cannot be innocent however they are spelled, for a key that does not
#: split into tokens the way the convention expects.
FORBIDDEN_SUBSTRINGS = ("latitude", "longitude", "centroid", "bbox", "_id", "_ids")

#: How many digits make a number an id rather than a quantity. A Strava activity id is ten
#: digits and an OSM way id is eight or nine, and both arrive as JSON numbers as readily as
#: strings. The largest count this file publishes is `export.points_total`, seven digits, so
#: there is about an order of magnitude of headroom. An export that grows past ten million
#: points trips this on its own point count; raise the threshold and say so here, rather than
#: deleting the rule.
ID_DIGITS = 8

#: How many decimal places make a pair of numbers a position. 0.01 degrees is roughly a
#: kilometre and 0.1 degrees is eleven, so two places is where a pair starts naming a place
#: instead of a region. Every band edge, tolerance and class break in this file is a whole
#: number or a half, which is what makes the precision a usable separator: `[1.5, 2.5]` m/s
#: is a speed band and `[35.68, 139.77]` is somebody's street.
COORD_DECIMALS = 2

#: The longest list that can be a position: `[lon, lat]` or `[lon, lat, altitude]`. Adjacent
#: pairs inside it are tested. Longer lists are not, because a positional list of statistics
#: -- three percentiles, say -- would read as a coordinate and get the file refused. The
#: convention here is that statistics get named keys and lists hold bands and edge tables.
COORD_LIST_LENGTH = 3

#: The largest dict that can be a position. A coordinate record is small; a statistics block
#: is not. See `_coordinate_dict` for why a dict needs more evidence than a list.
COORD_DICT_MEMBERS = 3


def _number(value) -> float | None:
    """`value` as a float if it is a JSON number, else None.

    `bool` is an `int` in Python and `True` is not a quantity. Anything numpy-shaped is
    excluded too, and that is a backstop rather than a hole: `json.dumps` refuses an
    `np.int64`, so a number this does not recognise cannot reach the file either.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _decimals(value: float) -> int:
    """Decimal places as written: 139.70 is one place, 35.65 is two."""
    return len(f"{value:.10f}".rstrip("0").partition(".")[2])


def _has_a_fraction(value: float) -> bool:
    return value % 1 != 0


def looks_like_a_coordinate_pair(first: float, second: float) -> bool:
    """Whether two numbers side by side are a latitude and a longitude.

    Four conditions, all of them needed. One number in [-90, 90] and the other in
    [-180, 180], because those are the ranges. Both with a fractional part, because a band
    edge is a round number and a position is not. One of them to `COORD_DECIMALS` places,
    because that is the precision at which a pair names a place.

    Either order. GeoJSON writes lon-lat, a person writes lat-lon, and nothing in this file
    would let the gate assume one.
    """
    if not (_has_a_fraction(first) and _has_a_fraction(second)):
        return False
    if max(_decimals(first), _decimals(second)) < COORD_DECIMALS:
        return False
    first_fits_lat, second_fits_lat = abs(first) <= 90.0, abs(second) <= 90.0
    first_fits_lon, second_fits_lon = abs(first) <= 180.0, abs(second) <= 180.0
    return (first_fits_lat and second_fits_lon) or (second_fits_lat and first_fits_lon)


def _coordinate_dict(node: dict) -> tuple[str, str] | None:
    """The two keys of a dict that reads as one position, or None.

    A dict needs more evidence than a list. In a list, adjacency is itself the claim that
    two numbers belong together. In a dict, two numbers in the coordinate ranges are the
    normal case -- `totals_pct` holds two percentages and `between_cell_spread_pts` holds
    four -- so the rule asks for a small record, exactly two numbers in it, and one of them
    past 90 degrees. A latitude cannot be past 90, so that member can only be a longitude,
    and no two-or-three-member record in this file holds a figure that large. The percentages
    that do go past 90 -- a maximum inflation runs into the hundreds -- sit in blocks of five
    keys, which this skips.

    The cost is a dict whose pair are both inside 90 degrees, which this does not catch.
    """
    if len(node) > COORD_DICT_MEMBERS:
        return None
    numbers = [(key, _number(value)) for key, value in node.items()]
    numbers = [(key, value) for key, value in numbers if value is not None]
    if len(numbers) != 2:
        return None
    (first_key, first), (second_key, second) = numbers
    if not looks_like_a_coordinate_pair(first, second):
        return None
    if max(abs(first), abs(second)) <= 90.0:
        return None
    return first_key, second_key


def _is_a_span(value) -> bool:
    """A width in degrees: a number, not negative, no wider than the globe.

    What the constraint buys: a span cannot be negative, so a southern latitude cannot ride
    in under one of these names. A northern one still can, which is why the exemption is
    four named keys rather than a pattern.
    """
    number = _number(value)
    return number is not None and 0.0 <= number <= 360.0


def _is_a_count(value) -> bool:
    """A count or a size: a number, not negative."""
    number = _number(value)
    return number is not None and number >= 0.0


#: What `activity_stats` writes as the sport when the CSV column did not resolve. Allowed
#: through `_is_a_category` because it is the file's own output: a category check that
#: refuses `?` turns an unresolved sport into a refusal to write anything at all.
UNRESOLVED_CATEGORY = "?"


def _is_a_category(value) -> bool:
    """A category: letters, or the unresolved placeholder.

    A place name is letters too, so `sport` is read rather than trusted. The constraint only
    rules out a coordinate, an id or a structure arriving under the name.
    """
    if not isinstance(value, str) or len(value) > 32:
        return False
    return value.isalpha() or value == UNRESOLVED_CATEGORY


#: Keys carrying a forbidden word that are nonetheless a width, a count or a category. Each
#: is listed because it was looked at one at a time; the list is short on purpose, and a new
#: entry is the moment to ask whether the field belongs in the file at all.
#:
#: An exemption is a key *and* a shape. The name alone was a blanket pass, so anything at all
#: shipped as `lon_span_deg` went through unread -- a bounding box, a pair, a string. Now the
#: value must be the thing the reason claims it is, and a value that is not is reported under
#: the exemption rather than waved past it.
ALLOWED_KEYS = {
    # Widths with no position. A span says how far apart two edges are and nothing about
    # where either edge is, which is the whole reason these are the only geometry here.
    "lon_span_deg": ("a width, not a position", _is_a_span),
    "lat_span_deg": ("a width, not a position", _is_a_span),
    "home_span_deg": ("a width, not a position", _is_a_span),
    "home_lat_span_deg": ("a width, not a position", _is_a_span),
    # Counts and sizes measured per activity, not anything about one.
    "rungs_per_activity": ("how many tolerances the ladder has", _is_a_count),
    "per_activity_json_mb": ("total bytes divided by the activity count", _is_a_count),
    # Categories.
    "sport": ("Run, Ride or Hike", _is_a_category),
}


#: `2026-11-09`, `2026/10/20`, `2026.10.20`, with or without a time after it. The separator
#: has to repeat, so a decimal does not read as a date.
_DATE_STAMP = re.compile(r"\b\d{4}([-/.])\d{2}\1\d{2}\b")

#: A decimal number with at most three integral digits -- the shape of a degree.
_DECIMAL = r"[-+]?\d{1,3}\.\d+"

#: Two of those side by side, separated by punctuation rather than words: `35.68,139.77`,
#: `35.680_139.760`, `35.68 139.77`. A cell id and a printed pair both land here.
_PAIR_IN_TEXT = re.compile(rf"({_DECIMAL})[\s,;:_/|]+({_DECIMAL})")

#: A plus code: eight characters of Open Location Code's alphabet, a `+`, then two or three
#: more. Ten characters resolve to about fourteen metres.
_PLUS_CODE = re.compile(r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b")

#: A geohash: seven to twelve characters of base32 with `a`, `i`, `l` and `o` left out.
#:
#: The digit requirement is load-bearing, not decoration. English words drop out of the
#: alphabet by containing `a`, `i`, `l` or `o`, but not all of them -- "between" is seven
#: legal characters -- and requiring both a digit and a letter is what stops the rule
#: refusing the file's own prose.
_GEOHASH = re.compile(
    r"\b(?=[0-9bcdefghjkmnpqrstuvwxyz]*\d)(?=[0-9bcdefghjkmnpqrstuvwxyz]*[a-z])"
    r"[0-9bcdefghjkmnpqrstuvwxyz]{7,12}\b"
)

#: Degrees and minutes with a hemisphere: `35 40 N`, `139° 46' E`.
_DMS = re.compile(r"\b\d{1,3}[\s°]\d{1,2}(?:[\s'′]\d{1,2}(?:\.\d+)?)?\s*([NSEW])\b")


def _findings_for_a_number(value: float, path: str) -> list[str]:
    """What a single number can be refused for, which is being an id and nothing else.

    A lone number is never refused on its range. A percentage, a count, a correlation
    coefficient, a span in degrees and a contrast ratio all live inside [-180, 180], so a
    range ban would fire on nearly every field here, and a gate that refuses its own output
    is a gate switched off within a week. Pairs and precision carry the position rule
    instead.
    """
    if not value.is_integer():
        return []
    digits = len(str(abs(int(value))))
    if digits >= ID_DIGITS:
        return [
            f"{path}: {digits} digits reads as an activity id, a way id or an epoch, "
            "whatever the key is called"
        ]
    return []


def _findings_for_a_string(value: str, path: str) -> list[str]:
    """What a string can be refused for: an id, a date, or a position in any notation."""
    bad: list[str] = []
    runs = "".join(ch if ch.isdigit() else " " for ch in value).split()
    if any(len(run) >= ID_DIGITS for run in runs):
        bad.append(f"{path}: a run of {ID_DIGITS}+ digits reads as an activity or way id")
    if _DATE_STAMP.search(value):
        bad.append(f"{path}: a date")
    for first, second in _PAIR_IN_TEXT.findall(value):
        if looks_like_a_coordinate_pair(float(first), float(second)):
            bad.append(f"{path}: {first} and {second} read as a latitude and a longitude")
    if _PLUS_CODE.search(value):
        bad.append(f"{path}: a plus code")
    if _GEOHASH.search(value):
        bad.append(f"{path}: a geohash")
    hemispheres = set(_DMS.findall(value))
    if hemispheres & {"N", "S"} and hemispheres & {"E", "W"}:
        bad.append(f"{path}: degrees and minutes with a hemisphere on each")
    return bad


def scan_for_identifiers(node, path: str = "") -> list[str]:
    """Walk the summary and refuse anything that could carry a position or an identifier.

    Two gates, because either alone is easy to slip past. Key names catch a field added by
    name -- `lat`, `way_id`, `activity`, `home`. Values catch one added under a harmless
    name, since a latitude and a longitude are still a latitude and a longitude when the keys
    are called `x` and `y`.

    The value gate has four rules.

    - **A pair.** Two numbers side by side are far stronger evidence than one: a list of two
      or three, or a dict of at most three members holding exactly two numbers. The test is
      `looks_like_a_coordinate_pair`, and a dict needs one member past 90 degrees on top of
      it.
    - **An id.** A run of `ID_DIGITS` digits, applied to numbers as well as strings. An
      activity id is an id whether JSON spells it `10521528007` or `"10521528007"`.
    - **A coded position.** A plus code, a geohash, degrees-minutes with a hemisphere. Each
      is a coordinate in a costume and none of them looks like a statistic.
    - **A date.** Four digits, two, two, in any of the three separators.

    What it still misses, stated rather than implied:

    - A coordinate rounded to a whole number or one decimal. `[35.0, 139.5]` passes. One
      decimal degree is eleven kilometres, which is a city and not a doorstep.
    - A single coordinate alone under an innocent key. `{"x": 35.68}` passes.
    - A dict pair with both numbers inside 90 degrees, and a pair in a list longer than
      `COORD_LIST_LENGTH`.
    - A coordinate scaled or offset: metres from a local origin, or a latitude plus 1000.
    - A place name in prose. Nothing here reads language.

    A string or a free-text field added to the summary MUST therefore be read rather than
    trusted. This narrows what can reach the file by accident; it does not make the file safe
    on its own.

    It has two false positives worth knowing, both from the precision rule. A prose string
    quoting two decimals with only punctuation between them -- `2.43, 2.16` -- is refused as a
    position. So would a band written to two decimals, `[1.25, 2.75]`, where every band,
    tolerance and class break here is a whole number or a half. An exemption in
    `ALLOWED_KEYS` does not help: it covers the key gate only, by design, because covering
    the value gate is the blanket pass it replaced. A field that genuinely needs that
    precision needs the rule changed on purpose and a `pass` probe added below.

    `--self-test` runs `SCAN_PROBES` through this function and returns 1 if any probe lands
    on the wrong side of the gate.
    """
    bad: list[str] = []
    if isinstance(node, dict):
        pair = _coordinate_dict(node)
        if pair:
            bad.append(
                f"{path or '<root>'}: {pair[0]} and {pair[1]} read as a latitude and a "
                "longitude"
            )
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            low = key.lower()
            tokens = set(low.replace("-", "_").split("_"))
            exemption = ALLOWED_KEYS.get(key)
            if exemption is not None:
                reason, is_that_shape = exemption
                if not is_that_shape(value):
                    bad.append(f"{here}: exempt as {reason}, and the value is not one")
            elif tokens & FORBIDDEN_TOKENS or any(s in low for s in FORBIDDEN_SUBSTRINGS):
                bad.append(f"{here}: key name could carry an identifier or a position")
            bad += scan_for_identifiers(value, here)
    elif isinstance(node, list):
        if 2 <= len(node) <= COORD_LIST_LENGTH:
            numbers = [_number(value) for value in node]
            for i, (first, second) in enumerate(zip(numbers, numbers[1:])):
                if first is None or second is None:
                    continue
                if looks_like_a_coordinate_pair(first, second):
                    bad.append(
                        f"{path}[{i}:{i + 2}]: two numbers side by side read as a latitude "
                        "and a longitude"
                    )
        for i, value in enumerate(node):
            bad += scan_for_identifiers(value, f"{path}[{i}]")
    elif isinstance(node, str):
        bad += _findings_for_a_string(node, path)
    elif (number := _number(node)) is not None:
        bad += _findings_for_a_number(number, path)
    return bad


#: Every probe `scan_for_identifiers` is held to, and which side of the gate each belongs on.
#:
#: `catch` means the scan must return a finding, and that one finding must contain the given
#: phrase -- so a probe cannot be satisfied by the wrong rule firing and a rule cannot be
#: deleted while its probes still pass on someone else's catch. `pass` means no finding at
#: all.
#:
#: The passing half carries as much weight as the catching half. A gate tightened until it
#: refuses a speed band, a pair of percentages or a sentence of prose is a gate that gets
#: switched off, and the file is then unguarded rather than strictly guarded. Every probe
#: below with `pass` is a field shape the real summary actually contains.
#:
#: Coordinates here are synthetic. They are in Tokyo to the degree and nowhere nearer.
SCAN_PROBES: tuple[tuple[str, object, str], ...] = (
    # A position under a harmless name, which is the case the key gate cannot see.
    ("catch", {"x": 35.68, "y": 139.77}, "latitude and a longitude"),
    ("catch", {"spot": [35.68, 139.77]}, "latitude and a longitude"),
    ("catch", {"home": [[139.70, 35.65], [139.80, 35.72]]}, "latitude and a longitude"),
    ("catch", {"home_center": [35.68, 139.77]}, "latitude and a longitude"),
    ("catch", {"camera": {"center": [139.77, 35.68], "zoom": 14}}, "latitude and a longitude"),
    ("catch", {"point": [139.77, 35.68, 12.5]}, "latitude and a longitude"),
    # A position inside a string: a cell id, a printed pair, and three notations.
    ("catch", {"cells": ["35.680_139.760"]}, "latitude and a longitude"),
    ("catch", {"densest": "35.6812,139.7671"}, "latitude and a longitude"),
    ("catch", {"loc": "8Q7XMQ2M+2M"}, "plus code"),
    ("catch", {"hash": "xn774cn8"}, "geohash"),
    ("catch", {"spot": "35 40 N 139 46 E"}, "hemisphere"),
    # An id or a time as a number, which the string rule alone never saw.
    ("catch", {"worst": 10521528007}, "digits"),
    ("catch", {"busiest": 29184773}, "digits"),
    ("catch", {"first": 1761000000}, "digits"),
    ("catch", {"when": "2026/10/20"}, "date"),
    # The half that already worked. A regression that breaks these is as bad as the hole.
    ("catch", {"start_lat": 35.68}, "key name"),
    ("catch", {"activity_id": 7}, "key name"),
    ("catch", {"way_id": 7}, "key name"),
    ("catch", {"note": "10521528007"}, "digits"),
    ("catch", {"note": "2026-10-20"}, "date"),
    # An exemption is a key and a shape. A span cannot be negative and a sport is letters.
    ("catch", {"lon_span_deg": -35.68}, "exempt"),
    ("catch", {"sport": "35.68,139.77"}, "exempt"),
    # Field shapes the real summary contains. Each must pass.
    ("pass", {"band_m": [1.5, 2.5], "n": 8, "median_inflation_pct": 2.86}, ""),
    ("pass", {"band_m": [0.0, 1.5], "n": 20, "median_inflation_pct": 54.59}, ""),
    ("pass", {"band_ms": [2.0, 2.5], "n": 279, "median_inflation_pct": 4.44}, ""),
    ("pass", {"speed_band_ms": [2.0, 5.0], "spacing_band_m": [3.0, 4.0]}, ""),
    ("pass", {"edges_pct": [1.0, 2.0, 3.0, 4.5, 7.0, 10.0]}, ""),
    ("pass", {"tolerances_m": [0.0, 0.5, 1.0, 2.0, 3.0, 5.0]}, ""),
    ("pass", {"class_counts": [27, 72, 77, 79, 68, 23, 5]}, ""),
    ("pass", {"recorded_over_matched": 4.13, "recorded_over_strava": 2.71}, ""),
    ("pass", {"points_total": 1462437, "overview_points": 55373}, ""),
    ("pass", {"lon_span_deg": 262.19, "home_span_deg": 1.01}, ""),
    ("pass", {"sport": "Hike", "n": 19, "median_inflation_pct": 60.09}, ""),
    # `activity_stats` writes this when the CSV column did not resolve.
    ("pass", {"sport": UNRESOLVED_CATEGORY, "n": 1, "median_inflation_pct": 1.96}, ""),
    ("pass", {"cell_deg": 0.002, "window_m": 150.5, "floor_m": 5.0}, ""),
    ("pass", {"percentile_method": "numpy default (linear interpolation)"}, ""),
    ("pass", {"mirrors": "NOISE_BREAKS in lines.mjs"}, ""),
    ("pass", {"reproduce": "apps/day-02-lines/tools/make_summary.py <a directory>"}, ""),
    ("pass", {"why_aggregated": WHY, "excluded": EXCLUDED}, ""),
    # Derived from the writers rather than copied out of them, so a reworded block is
    # re-checked instead of checked against a stale copy of itself.
    ("pass", {"ladder": ladder_floor()}, ""),
    ("pass", {"crop": crop_stats({})}, ""),
    (
        "pass",
        {"crop": crop_stats({"crop": {"trim_m": 402.336, "min_end_offset_m": 200.0}})},
        "",
    ),
)


def self_test() -> int:
    """Hold `scan_for_identifiers` against `SCAN_PROBES`, and the committed file against it.

    The negative control for the privacy claim. Without it, "shown capable of failing" is a
    sentence rather than a check. Weakening a rule turns the probes that rest on it red:
    setting `ID_DIGITS` to 99 fails the four id probes and returns 1.

    Reads no export and writes nothing. The probes are literals.
    """
    failures: list[str] = []
    for verdict, probe, needle in SCAN_PROBES:
        label = json.dumps(probe)
        if len(label) > 64:
            label = label[:61] + "..."
        found = scan_for_identifiers(probe)
        if verdict == "catch":
            ok = any(needle in finding for finding in found)
        else:
            ok = not found
        detail = "; ".join(found) if found else "clean"
        if not ok:
            failures.append(label)
        print(f"{'ok  ' if ok else 'FAIL'} {verdict:<5} {label} -- {detail}")

    # The committed file, because the probes above are hand-built and the output is not. A
    # gate that refuses the real summary is the failure this project has a name for, and it
    # would otherwise show up only the next time somebody ran the generator.
    if not DEFAULT_OUT.is_file():
        failures.append(str(DEFAULT_OUT))
        print(f"FAIL pass  {DEFAULT_OUT} is missing, so the gate was not held against it")
    else:
        found = scan_for_identifiers(json.loads(DEFAULT_OUT.read_text()))
        if found:
            failures.append(DEFAULT_OUT.name)
        print(
            f"{'FAIL' if found else 'ok  '} pass  {DEFAULT_OUT.name} -- "
            f"{'; '.join(found) if found else 'clean'}"
        )

    print(f"\n{len(SCAN_PROBES) + 1} probes, {len(failures)} failed")
    for label in failures:
        print(f"    {label}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
