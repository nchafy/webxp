"""Turn a Strava export into the data the day 2 page reads.

    .venv/bin/python days/02-lines/prepare.py ~/Downloads/strava_export
    .venv/bin/python days/02-lines/prepare.py ~/claude/strava-sample/run.gpx --no-match

Point it at a bulk export directory, at the `activities/` folder inside one, or at
individual files. It writes one JSON per activity plus an `index.json` into
`days/02-lines/data/`, and prints a table of what it measured.

Coordinates are written at six decimal places, which is about 0.11 m -- an order of
magnitude finer than the receiver, so the rounding is free, and it roughly halves the
file compared with the full float.

The page does its own simplification so its tolerance slider can be continuous, so the
recorded geometry is shipped at full resolution rather than pre-simplified. The `ladder`
written here is therefore not what the page draws; it is what `scripts/check.py` holds
the page's own arithmetic against, so that the two implementations of Douglas-Peucker
cannot drift apart unnoticed.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
import traceback

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gpsnoise as gn  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
ACTIVITY_SUFFIXES = (".gpx", ".fit", ".tcx", ".gpx.gz", ".fit.gz", ".tcx.gz")

#: Where a real run writes, and where the page looks first.
#:
#: This is gitignored, and that is the whole reason it exists. The obvious default was
#: `data/`, which is committed so the page renders for someone without an export -- and
#: which therefore means the documented command overwrites tracked files with real traces
#: and the next `git add -A` publishes somebody's home address. An activity trace says
#: where you live and when you are out.
#:
#: So: `data/` holds the synthetic fixture and is committed, `private/` holds real output
#: and cannot be, and `map.mjs` tries `private/` first and falls back to `data/`. Writing
#: into `data/` now takes an explicit `--out`, which is what regenerating the fixture does.
PRIVATE_OUT = HERE / "private"
FIXTURE_OUT = HERE / "data"

#: Decimal places the coordinates are written at, and quantised to *before* anything is
#: measured. About 0.11 m at this latitude -- an order of magnitude finer than the
#: receiver, so nothing real is lost, and it roughly halves the file.
#:
#: Quantising first is the part that matters. The page recomputes lengths from the
#: coordinates in the file, so a ladder computed here at full precision describes a
#: geometry the page does not have. That gap was 3e-5 relative, found by the ladder
#: cross-check in `scripts/check.py` rather than by reading the code.
COORD_DP = 6

#: Decimal places for a written length, in metres. Millimetres is far past anything the
#: page displays, and that is the point: `scripts/check.py` compares the browser's own
#: arithmetic against the ladder written here, so the file has to carry more precision
#: than the comparison needs. At one decimal place the rounding alone was 3e-5 of a
#: 1.6 km activity, which is larger than any real disagreement would be and made the
#: cross-check impossible to state tightly.
LENGTH_DP = 3


def find_activities(targets: list[pathlib.Path]) -> list[pathlib.Path]:
    """Collect activity files from directories or explicit paths, sorted by name."""
    found: list[pathlib.Path] = []
    for t in targets:
        if t.is_file():
            found.append(t)
            continue
        if not t.is_dir():
            print(f"  skipping {t}: not a file or directory", file=sys.stderr)
            continue
        # A bulk export has an `activities/` subdirectory; being pointed at either the
        # export root or that folder should both work.
        roots = [t / "activities", t] if (t / "activities").is_dir() else [t]
        for root in roots:
            for p in sorted(root.iterdir()):
                if p.is_file() and p.name.lower().endswith(ACTIVITY_SUFFIXES):
                    found.append(p)
    # De-duplicate while keeping order, in case the export root and activities/ overlap.
    seen = set()
    out = []
    for p in found:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def read_strava_csv(export_dir: pathlib.Path) -> dict[str, dict]:
    """Index `activities.csv` by activity filename, if it is there.

    Strava's export CSV is not a stable interface: the column set has changed between
    exports, there have been two differently-scaled `Distance` columns in the same file,
    and localised headers exist. So nothing here is positional and no unit is assumed --
    `resolve_distance_unit` works the scale out by measurement instead. Columns that
    cannot be found are reported and left empty rather than guessed at.
    """
    path = export_dir / "activities.csv"
    if not path.is_file():
        return {}
    rows: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        name_col = _pick(headers, ["activity name"])
        date_col = _pick(headers, ["activity date"])
        type_col = _pick(headers, ["activity type"])
        file_col = _pick(headers, ["filename"])
        dist_cols = [h for h in headers if h and h.strip().lower().startswith("distance")]
        print(f"  activities.csv: {len(headers)} columns")
        print(f"    name={name_col!r} date={date_col!r} type={type_col!r} file={file_col!r}")
        print(f"    distance candidates={dist_cols!r}")
        if not file_col:
            print("    no filename column, so CSV rows cannot be matched to files")
            return {}
        for row in reader:
            fname = (row.get(file_col) or "").strip()
            if not fname:
                continue
            key = pathlib.PurePath(fname).name
            rows[key] = {
                "name": (row.get(name_col) or "").strip() if name_col else "",
                "date": (row.get(date_col) or "").strip() if date_col else "",
                "sport": (row.get(type_col) or "").strip() if type_col else "",
                "distances": {c: _as_float(row.get(c)) for c in dist_cols},
            }
    return rows


def _pick(headers: list[str], wants: list[str]) -> str | None:
    for h in headers:
        if h and h.strip().lower() in wants:
            return h
    return None


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", ""))
    except ValueError:
        return None
    return f if f > 0 else None


# Candidate scales for a distance column, as metres per unit.
DISTANCE_UNITS = ((1.0, "metres"), (1000.0, "kilometres"), (1609.344, "miles"))

# How far the CSV figure may sit from the reference before a scale stops being credible.
#
# This number does two jobs, and the second is why it is as tight as it is. It separates
# the units -- kilometres and miles are a factor of 1.609 apart, so the window must span
# less than that. And it decides whether the reference is trustworthy enough to be used
# at all. At +/-20% a kilometres column measured against a reference inflated by 45%
# lands 11% away from *miles* and is read as miles: a wrong answer, delivered with
# confidence. At +/-10% it is 31% from kilometres and 11% from miles, so neither fits and
# the column is refused, which is the correct outcome.
#
# A matched length is accurate to about 0.5%, and a well-behaved reported distance sits a
# few percent above it, so 10% leaves real data plenty of room.
# `test_gpsnoise.py` asserts both halves: that the unit windows stay disjoint, and that
# an inflated reference is refused rather than resolved.
UNIT_TOLERANCE = 0.10


def resolve_distance_unit(
    pairs: list[tuple[float, float]], column: str, reference: str
) -> tuple[float, str]:
    """Work out a CSV distance column's unit by measurement, or refuse to.

    `pairs` is (csv_value, reference_metres) per activity. The median ratio identifies
    the scale, so the export's format does not have to be known in advance -- which
    matters, because Strava's CSV has carried two differently-scaled `Distance` columns
    in one file and the header does not say which is which.

    Two things this must get right, because getting them wrong is silent:

    1. **The reference has to be accurate.** Compared against the *recorded* length,
       which noise inflates by anything from 20% to 300%, kilometres and miles are
       indistinguishable -- they are a factor of 1.609 apart and the reference is worse
       than that. The matched length is within about 0.5% of truth, so that is what the
       caller passes when it has one.
    2. **An ambiguous answer must be refused, not resolved.** If the measured ratio is
       consistent with more than one unit, picking the first would make every reference
       distance on the page wrong by a constant factor while still looking plausible.
    """
    if not pairs:
        return 0.0, f"{column}: no overlapping activities, cannot resolve unit"
    ratios = np.array([c / r for c, r in pairs if r > 0])
    if ratios.size == 0:
        return 0.0, f"{column}: no usable rows"
    med = float(np.median(ratios))
    spread = float(np.percentile(ratios, 90) - np.percentile(ratios, 10)) if ratios.size > 2 else 0.0

    fits = [
        (scale, label)
        for scale, label in DISTANCE_UNITS
        if abs(med * scale - 1.0) <= UNIT_TOLERANCE
    ]
    detail = (
        f"median ratio to {reference} length {med:.4g}, "
        f"n={ratios.size}, 10-90 spread {spread:.3g}"
    )
    if len(fits) == 1:
        return fits[0][0], f"{column}: {fits[0][1]} ({detail})"
    if len(fits) > 1:
        names = " or ".join(label for _, label in fits)
        return 0.0, f"{column}: ambiguous between {names} -- refused ({detail})"
    return 0.0, f"{column}: no unit fits -- refused ({detail})"


def activity_id(path: pathlib.Path, taken: set) -> str:
    """A filename-safe id for one activity, unique within a run.

    Strips only the suffixes this tool knows, rather than splitting on the first dot.
    `2026-01-01.run.gpx` and `2026-01-01.ride.gpx` both reduce to `2026-01-01` under a
    split: the second document overwrites the first, and `index.json` carries two rows
    with the same id -- which `map.mjs` then fetches twice, draws twice, and counts twice
    in its total distance. Strava's own `<id>.gpx.gz` never collides, so this only bites a
    hand-assembled directory, which is exactly the case nobody tests.

    `taken` is mutated, so callers get a distinct id per file even when the stems match.
    """
    name = path.name
    for suffix in sorted(ACTIVITY_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    stem = name or path.name
    if stem not in taken:
        taken.add(stem)
        return stem
    n = 2
    while f"{stem}-{n}" in taken:
        n += 1
    taken.add(f"{stem}-{n}")
    return f"{stem}-{n}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("targets", nargs="+", type=pathlib.Path)
    ap.add_argument(
        "--out",
        type=pathlib.Path,
        default=PRIVATE_OUT,
        help=f"where to write (default {PRIVATE_OUT.name}/, which is gitignored)",
    )
    ap.add_argument("--no-match", action="store_true", help="skip map matching (no network)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N activities")
    ap.add_argument("--costing", default="", help="force a Valhalla costing model")
    args = ap.parse_args()

    files = find_activities(args.targets)
    if not files:
        print("No activity files found. Looked for: " + ", ".join(ACTIVITY_SUFFIXES))
        return 1
    if args.limit:
        files = files[: args.limit]
    print(f"{len(files)} activity file(s)")

    export_dirs = [t for t in args.targets if t.is_dir()]
    csv_rows: dict[str, dict] = {}
    for d in export_dirs:
        csv_rows.update(read_strava_csv(d))
        if not csv_rows and (d.parent / "activities.csv").is_file():
            csv_rows.update(read_strava_csv(d.parent))

    # Say where the output is going, every run. The difference between the ignored path
    # and the committed one is the difference between a private file and a published one,
    # so it is not something to leave implicit.
    out = args.out.resolve()
    if out == FIXTURE_OUT.resolve():
        print(f"writing to {out} -- this directory is COMMITTED. Only the fixture belongs here.")
    else:
        print(f"writing to {out}")

    args.out.mkdir(parents=True, exist_ok=True)
    measured: list[tuple[pathlib.Path, gn.Measured]] = []
    failures: list[tuple[pathlib.Path, str]] = []

    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}", flush=True)
        try:
            track = gn.read_activity(path)
        except Exception as exc:
            # Named, not swallowed: a file that cannot be read is reported and the run
            # continues, because one corrupt activity in an export of hundreds should
            # not take the whole thing down.
            failures.append((path, f"read failed: {exc}"))
            print(f"    read failed: {exc}", file=sys.stderr)
            continue
        # Quantise before measuring, not on the way out: see COORD_DP.
        track.lat = np.round(track.lat, COORD_DP)
        track.lon = np.round(track.lon, COORD_DP)

        meta = csv_rows.get(path.name, {})
        if meta.get("sport"):
            track.sport = meta["sport"]
        if meta.get("name"):
            track.name = meta["name"]
        try:
            m = gn.measure(
                track, do_match=not args.no_match, costing=args.costing or None
            )
        except Exception as exc:
            failures.append((path, f"measure failed: {exc}"))
            traceback.print_exc()
            continue
        # Same quantisation for the snapped path, and its length recomputed from the
        # quantised coordinates, so `matched_m` describes the geometry in the file rather
        # than a slightly finer one nobody has.
        if m.matched_lat is not None:
            m.matched_lat = np.round(m.matched_lat, COORD_DP)
            m.matched_lon = np.round(m.matched_lon, COORD_DP)
            m.matched_m = gn.track_length_m(m.matched_lat, m.matched_lon)

        measured.append((path, m))
        note = f"  match: {m.match_note}" if m.match_note else ""
        print(
            f"    {len(track)} pts, step {m.step_m:.1f} m, recorded {m.recorded_m / 1000:.3f} km"
            + (f", matched {m.matched_m / 1000:.3f} km" if m.matched_m else ", matched -")
            + note
        )

    if not measured:
        print("\nNothing measured.")
        return 1

    # Resolve the CSV distance columns against what was actually measured.
    scale_for: dict[str, float] = {}
    all_dist_cols: set[str] = set()
    for p, _m in measured:
        all_dist_cols.update((csv_rows.get(p.name, {}).get("distances") or {}).keys())
    # Prefer the matched length as the reference: it is within about 0.5% of truth,
    # where the recorded length is inflated by the very noise this page is measuring.
    # Only fall back to recorded when nothing matched, and say so, because a unit
    # resolved against an inflated reference cannot separate kilometres from miles.
    n_matched = sum(1 for _p, m in measured if m.matched_m)
    ref_kind = "matched" if n_matched >= max(3, len(measured) // 2) else "recorded"
    if all_dist_cols:
        print(f"\nResolving CSV distance columns against the {ref_kind} length:")
        if ref_kind == "recorded":
            print("  warning: too few activities matched, so the reference is the inflated")
            print("  recorded length. Kilometres and miles may be indistinguishable.")
    for col in sorted(all_dist_cols):
        pairs = []
        for p, m in measured:
            v = (csv_rows.get(p.name, {}).get("distances") or {}).get(col)
            ref = m.matched_m if ref_kind == "matched" else m.recorded_m
            if v is not None and ref and ref > 0:
                pairs.append((v, ref))
        scale, note = resolve_distance_unit(pairs, col, ref_kind)
        print(f"  {note}")
        if scale:
            scale_for[col] = scale
    if all_dist_cols and not scale_for:
        print("  no distance column resolved: the page will show no Strava figure.")

    index = []
    used_ids: set[str] = set()
    for p, m in measured:
        meta = csv_rows.get(p.name, {})
        strava_m = None
        strava_col = None
        for col, scale in scale_for.items():
            v = (meta.get("distances") or {}).get(col)
            if v is not None:
                strava_m, strava_col = v * scale, col
                break

        aid = activity_id(p, used_ids)
        # Already quantised above, so this round only formats; the geometry written is
        # byte-for-byte the geometry measured.
        rec_lonlat = np.round(np.column_stack([m.track.lon, m.track.lat]), COORD_DP).tolist()
        mat_lonlat = (
            np.round(np.column_stack([m.matched_lon, m.matched_lat]), COORD_DP).tolist()
            if m.matched_lat is not None
            else []
        )
        doc = {
            "id": aid,
            "name": m.track.name,
            "sport": m.track.sport,
            "date": meta.get("date", ""),
            "source_file": p.name,
            "points": len(m.track),
            "step_m": _clean(m.step_m),
            "duration_s": _clean(m.duration_s),
            "recorded_m": round(m.recorded_m, LENGTH_DP),
            "matched_m": round(m.matched_m, LENGTH_DP) if m.matched_m else None,
            "strava_m": round(strava_m, 1) if strava_m else None,
            "strava_column": strava_col,
            "match_note": m.match_note,
            "ladder": [[t, round(l, LENGTH_DP), n] for t, l, n in m.ladder],
            "recorded": rec_lonlat,
            "matched": mat_lonlat,
        }
        (args.out / f"{aid}.json").write_text(json.dumps(doc, separators=(",", ":")))
        index.append({k: doc[k] for k in (
            "id", "name", "sport", "date", "points", "step_m", "duration_s",
            "recorded_m", "matched_m", "strava_m", "match_note",
        )})

    index.sort(key=lambda r: (r["date"] or "", r["id"]))
    (args.out / "index.json").write_text(json.dumps(
        {"activities": index, "failures": [{"file": p.name, "why": w} for p, w in failures]},
        indent=1,
    ))

    print(f"\n{'activity':28} {'rec km':>8} {'match km':>9} {'strava':>8} {'inflation':>10} {'step':>6}")
    for r in index:
        infl = ""
        if r["matched_m"]:
            infl = f"{(r['recorded_m'] / r['matched_m'] - 1) * 100:+.1f}%"
        print(
            f"{r['name'][:28]:28} {r['recorded_m'] / 1000:8.3f} "
            f"{(r['matched_m'] / 1000 if r['matched_m'] else float('nan')):9.3f} "
            f"{(r['strava_m'] / 1000 if r['strava_m'] else float('nan')):8.3f} "
            f"{infl:>10} {r['step_m'] if r['step_m'] else float('nan'):6.1f}"
        )
    tot_rec = sum(r["recorded_m"] for r in index)
    tot_mat = sum(r["matched_m"] for r in index if r["matched_m"])
    matched_n = sum(1 for r in index if r["matched_m"])
    print(f"\n{len(index)} activities, {matched_n} matched.")
    print(f"recorded {tot_rec / 1000:.1f} km across all; matched {tot_mat / 1000:.1f} km "
          f"across the {matched_n} that snapped.")
    if failures:
        print(f"{len(failures)} file(s) failed; see data/index.json")
    print(f"wrote {args.out}")
    return 0


def _clean(v: float) -> float | None:
    return None if v is None or not np.isfinite(v) else round(float(v), 2)


if __name__ == "__main__":
    sys.exit(main())
