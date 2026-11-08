"""Summarise a measured directory: the three distances, and how far apart they are.

    .venv/bin/python -I days/02-lines/report.py days/02-lines/private

Prints the aggregate and the distribution. Three figures per activity, none of them the
truth:

- **recorded** -- the trackpoints summed. Receiver jitter counts as distance covered.
- **matched**  -- the trace snapped to the OSM network. Follows centrelines, so it cuts
                  corners and never sees a weave.
- **strava**   -- what Strava itself reports, which is neither of the above: it applies
                  its own smoothing, and for a device with a footpod may not use GPS at
                  all.

Reports percentiles rather than a mean, because the distribution has a long tail: a
handful of activities where the trace is dense relative to movement dominate a mean and
describe none of the rest.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np


def pct(a, q):
    return float(np.percentile(a, q))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=pathlib.Path)
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    index = json.loads((args.data_dir / "index.json").read_text())
    rows = index["activities"]
    failures = index.get("failures", [])

    have_all = [r for r in rows if r.get("matched_m") and r.get("strava_m")]
    rec = np.array([r["recorded_m"] for r in have_all])
    mat = np.array([r["matched_m"] for r in have_all])
    stv = np.array([r["strava_m"] for r in have_all])
    step = np.array([r["step_m"] or np.nan for r in have_all])

    print(f"{len(rows)} activities in the index; {len(have_all)} have all three figures")
    if failures:
        print(f"{len(failures)} unreadable:")
        for f in failures:
            print(f"  {f['file']}: {f['why']}")

    print("\n--- totals ---")
    print(f"recorded  {rec.sum() / 1000:10,.1f} km")
    print(f"matched   {mat.sum() / 1000:10,.1f} km")
    print(f"Strava    {stv.sum() / 1000:10,.1f} km")
    print(f"\nrecorded is {(rec.sum() / mat.sum() - 1) * 100:+.2f}% of matched")
    print(f"Strava   is {(stv.sum() / mat.sum() - 1) * 100:+.2f}% of matched")
    print(f"recorded is {(rec.sum() / stv.sum() - 1) * 100:+.2f}% of Strava")

    print("\n--- per activity, recorded over matched (%) ---")
    infl = (rec / mat - 1) * 100
    for q in (5, 25, 50, 75, 95, 99):
        print(f"  p{q:<3} {pct(infl, q):+7.2f}%")
    print(f"  worst {infl.max():+.1f}%   best {infl.min():+.1f}%")
    print(f"  negative on {int((infl < 0).sum())} of {infl.size} activities "
          f"-- matching found a longer path than the recording")

    print("\n--- per activity, recorded over Strava (%) ---")
    vs = (rec / stv - 1) * 100
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3} {pct(vs, q):+7.2f}%")

    print("\n--- per activity, Strava over matched (%) ---")
    sm = (stv / mat - 1) * 100
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3} {pct(sm, q):+7.2f}%")

    print("\n--- median sample spacing (m) ---")
    finite = step[np.isfinite(step)]
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3} {pct(finite, q):7.2f} m")

    print(f"\n--- {args.top} most inflated ---")
    order = np.argsort(-infl)
    print(f"  {'activity':28} {'rec km':>8} {'match km':>9} {'strava':>8} {'infl':>8} {'step':>6}")
    for i in order[: args.top]:
        r = have_all[i]
        print(
            f"  {r['name'][:28]:28} {r['recorded_m'] / 1000:8.2f} "
            f"{r['matched_m'] / 1000:9.2f} {r['strava_m'] / 1000:8.2f} "
            f"{infl[i]:+7.1f}% {r['step_m'] or float('nan'):6.1f}"
        )

    # By sport, because the aggregate hides that one sport behaves completely differently
    # -- and because a matched distance far below Strava's own figure is more likely a
    # failed match than a noisy trace, which is a different claim entirely.
    print("\n--- by sport ---")
    print(f"  {'sport':16} {'n':>4} {'recorded':>10} {'matched':>10} {'strava':>10} "
          f"{'rec/match':>10} {'median':>8}")
    sports = sorted({r["sport"] or "?" for r in have_all})
    for sport in sports:
        sel = [r for r in have_all if (r["sport"] or "?") == sport]
        r_ = np.array([x["recorded_m"] for x in sel])
        m_ = np.array([x["matched_m"] for x in sel])
        s_ = np.array([x["strava_m"] for x in sel])
        med = np.median((r_ / m_ - 1) * 100)
        print(
            f"  {sport:16} {len(sel):>4} {r_.sum() / 1000:>9,.1f}k {m_.sum() / 1000:>9,.1f}k "
            f"{s_.sum() / 1000:>9,.1f}k {(r_.sum() / m_.sum() - 1) * 100:>+9.1f}% {med:>+7.1f}%"
        )

    # Where the matched distance disagrees with Strava's own figure by a lot, the matched
    # one is the suspect. Strava is not ground truth, but it is an independent estimate of
    # the same quantity, and the two only diverge wildly when the matcher lost the trace --
    # an unmapped trail, or a path it snapped off repeatedly.
    print("\n--- matches to distrust (matched far below Strava's figure) ---")
    ratio = mat / stv
    bad = [i for i in range(len(have_all)) if ratio[i] < 0.8]
    print(f"  {len(bad)} of {len(have_all)} activities match to under 80% of Strava's figure")
    by_sport = {}
    for i in bad:
        by_sport.setdefault(have_all[i]["sport"] or "?", 0)
        by_sport[have_all[i]["sport"] or "?"] += 1
    print(f"  by sport: {by_sport}")
    noted = [r for r in have_all if r.get("match_note")]
    print(f"  {len(noted)} activities carry a match warning")
    for r in noted[:5]:
        print(f"    {r['name'][:30]}: {r['match_note'][:90]}")

    trust = [i for i in range(len(have_all)) if ratio[i] >= 0.8]
    if trust:
        tr = rec[trust]
        tm = mat[trust]
        ts = stv[trust]
        print(f"\n--- excluding those {len(bad)}, on the remaining {len(trust)} ---")
        print(f"  recorded {tr.sum() / 1000:,.1f} km   matched {tm.sum() / 1000:,.1f} km   "
              f"Strava {ts.sum() / 1000:,.1f} km")
        print(f"  recorded is {(tr.sum() / tm.sum() - 1) * 100:+.2f}% of matched, "
              f"{(tr.sum() / ts.sum() - 1) * 100:+.2f}% of Strava")
        ti = (tr / tm - 1) * 100
        print(f"  median inflation {np.median(ti):+.2f}%, p95 {pct(ti, 95):+.2f}%")

    # Disentangling the hikes.
    #
    # A hike's recorded trace is far longer than its matched one, and the matched one is
    # far below Strava's figure. Two different things could cause that -- a genuinely
    # noisy trace, or a matcher that lost an unmapped trail -- and map matching cannot
    # tell them apart, because it is the suspect.
    #
    # The simplification ladder can, because it never consults a map. If simplifying the
    # recording to a few metres lands near Strava's independent figure, the extra length
    # was jitter and the matcher is the thing that is wrong.
    if bad:
        print("\n--- the suspect matches, measured without a map ---")
        print("  simplifying the recording, against Strava's own figure:")
        rungs = [2.0, 5.0, 8.0, 12.0]
        header = "  ".join(f"{t:g}m" .rjust(8) for t in rungs)
        print(f"  {'activity':24} {'recorded':>9} {'strava':>8} {header}  {'matched':>8}")
        for i in bad[: args.top]:
            r = have_all[i]
            doc = json.loads((args.data_dir / f"{r['id']}.json").read_text())
            ladder = {row[0]: row[1] for row in doc["ladder"]}
            cells = "  ".join(
                f"{ladder.get(t, float('nan')) / 1000:8.2f}" for t in rungs
            )
            print(
                f"  {r['name'][:24]:24} {r['recorded_m'] / 1000:9.2f} "
                f"{r['strava_m'] / 1000:8.2f} {cells}  {r['matched_m'] / 1000:8.2f}"
            )
        # Which rung lands closest to Strava, across the suspect set.
        print("\n  closest rung to Strava's figure, per activity:")
        for i in bad[: args.top]:
            r = have_all[i]
            doc = json.loads((args.data_dir / f"{r['id']}.json").read_text())
            best = min(doc["ladder"], key=lambda row: abs(row[1] - r["strava_m"]))
            err = (best[1] / r["strava_m"] - 1) * 100
            print(
                f"    {r['name'][:24]:24} tol {best[0]:>5g} m -> "
                f"{best[1] / 1000:6.2f} km ({err:+.1f}% of Strava)"
            )

    # Spacing is the single number that most predicts inflation, so show the relationship
    # rather than asserting it.
    print("\n--- inflation by sample spacing ---")
    bands = [(0, 1.5), (1.5, 2.5), (2.5, 3.0), (3.0, 3.5), (3.5, 4.0), (4.0, 1e9)]
    print(f"  {'spacing':>14}  {'n':>4}  {'median inflation':>17}")
    for lo, hi in bands:
        sel = np.isfinite(step) & (step >= lo) & (step < hi)
        if not sel.any():
            continue
        label = f"{lo:.1f}-{hi:.1f} m" if hi < 1e9 else f">{lo:.1f} m"
        print(f"  {label:>14}  {int(sel.sum()):>4}  {np.median(infl[sel]):>+16.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
