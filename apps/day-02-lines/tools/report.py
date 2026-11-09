"""Summarise a measured directory: the three distances, and how far apart they are.

    .venv/bin/python -I apps/day-02-lines/tools/report.py apps/day-02-lines/private

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

    # Two sets, because a Strava figure is optional and most of this report does not need
    # one. `have_all` used to require all three, so an index with no resolved Strava column
    # left it empty -- and an empty array divides to `nan`, prints a totals block of zeroes,
    # and then raises out of `np.percentile` four lines later. A tool that cannot answer
    # must say which input it is missing, not traceback.
    have_all = [r for r in rows if r.get("matched_m")]
    with_strava = [r for r in have_all if r.get("strava_m")]

    print(f"{len(rows)} activities in the index; {len(have_all)} matched to the map")
    if failures:
        print(f"{len(failures)} unreadable:")
        for f in failures:
            print(f"  {f['file']}: {f['why']}")
    if not have_all:
        print(
            "\nNothing in this index carries a matched length, so there is nothing to\n"
            "compare a recording against. Re-run prepare.py with matching enabled."
        )
        return 1
    if not with_strava:
        # Named rather than left as blank columns. The usual cause is cropping: `prepare.py`
        # trims each end of a run, so its lengths cover the middle while Strava's CSV figure
        # covers the whole activity, and `resolve_distance_unit` refuses rather than
        # resolving a unit against a reference that is measuring something else.
        print(
            "\nNo activity carries a Strava distance, so every section comparing against\n"
            "one is skipped. Either the CSV distance column did not resolve, or the\n"
            "recorded lengths are cropped and the CSV figure is not the same quantity."
        )

    rec = np.array([r["recorded_m"] for r in have_all])
    mat = np.array([r["matched_m"] for r in have_all])
    step = np.array([r["step_m"] or np.nan for r in have_all])

    print("\n--- totals ---")
    print(f"recorded  {rec.sum() / 1000:10,.1f} km")
    print(f"matched   {mat.sum() / 1000:10,.1f} km")
    print(f"\nrecorded is {(rec.sum() / mat.sum() - 1) * 100:+.2f}% of matched")
    if with_strava:
        s_rec = np.array([r["recorded_m"] for r in with_strava])
        s_mat = np.array([r["matched_m"] for r in with_strava])
        s_stv = np.array([r["strava_m"] for r in with_strava])
        print(f"Strava    {s_stv.sum() / 1000:10,.1f} km over {len(with_strava)} activities")
        print(f"Strava   is {(s_stv.sum() / s_mat.sum() - 1) * 100:+.2f}% of matched")
        print(f"recorded is {(s_rec.sum() / s_stv.sum() - 1) * 100:+.2f}% of Strava")

    print("\n--- per activity, recorded over matched (%) ---")
    infl = (rec / mat - 1) * 100
    for q in (5, 25, 50, 75, 95, 99):
        print(f"  p{q:<3} {pct(infl, q):+7.2f}%")
    print(f"  worst {infl.max():+.1f}%   best {infl.min():+.1f}%")
    print(f"  negative on {int((infl < 0).sum())} of {infl.size} activities "
          f"-- matching found a longer path than the recording")

    if with_strava:
        print("\n--- per activity, recorded over Strava (%) ---")
        vs = (s_rec / s_stv - 1) * 100
        for q in (5, 25, 50, 75, 95):
            print(f"  p{q:<3} {pct(vs, q):+7.2f}%")

        print("\n--- per activity, Strava over matched (%) ---")
        sm = (s_stv / s_mat - 1) * 100
        for q in (5, 25, 50, 75, 95):
            print(f"  p{q:<3} {pct(sm, q):+7.2f}%")

    print("\n--- median sample spacing (m) ---")
    finite = step[np.isfinite(step)]
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3} {pct(finite, q):7.2f} m")

    #: A missing figure prints as a dash rather than as a zero, which would read as a
    #: measurement of nothing.
    def km(value) -> str:
        return f"{value / 1000:8.2f}" if value else "       -"

    print(f"\n--- {args.top} most inflated ---")
    order = np.argsort(-infl)
    print(f"  {'activity':28} {'rec km':>8} {'match km':>9} {'strava':>8} {'infl':>8} {'step':>6}")
    for i in order[: args.top]:
        r = have_all[i]
        print(
            f"  {r['name'][:28]:28} {km(r['recorded_m'])} "
            f"{km(r['matched_m']):>9} {km(r.get('strava_m'))} "
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
        s_ = np.array([x["strava_m"] for x in sel if x.get("strava_m")])
        med = np.median((r_ / m_ - 1) * 100)
        strava_col = f"{s_.sum() / 1000:>9,.1f}k" if s_.size else f"{'-':>10}"
        print(
            f"  {sport:16} {len(sel):>4} {r_.sum() / 1000:>9,.1f}k {m_.sum() / 1000:>9,.1f}k "
            f"{strava_col} {(r_.sum() / m_.sum() - 1) * 100:>+9.1f}% {med:>+7.1f}%"
        )

    # Everything from here to the spacing table needs Strava's own figure as an independent
    # reference, so it is skipped rather than approximated when there is none. Substituting
    # the recorded length would compare the matcher against the thing it is being used to
    # judge.
    if with_strava:
        # Where the matched distance disagrees with Strava's own figure by a lot, the matched
        # one is the suspect. Strava is not ground truth, but it is an independent estimate of
        # the same quantity, and the two only diverge wildly when the matcher lost the trace --
        # an unmapped trail, or a path it snapped off repeatedly.
        print("\n--- matches to distrust (matched far below Strava's figure) ---")
        ratio = s_mat / s_stv
        bad = [i for i in range(len(with_strava)) if ratio[i] < 0.8]
        print(
            f"  {len(bad)} of {len(with_strava)} activities match to under 80% of "
            "Strava's figure"
        )
        by_sport = {}
        for i in bad:
            by_sport.setdefault(with_strava[i]["sport"] or "?", 0)
            by_sport[with_strava[i]["sport"] or "?"] += 1
        print(f"  by sport: {by_sport}")
        noted = [r for r in have_all if r.get("match_note")]
        print(f"  {len(noted)} activities carry a match warning")
        for r in noted[:5]:
            print(f"    {r['name'][:30]}: {r['match_note'][:90]}")

        trust = [i for i in range(len(with_strava)) if ratio[i] >= 0.8]
        if trust:
            tr = s_rec[trust]
            tm = s_mat[trust]
            ts = s_stv[trust]
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
                r = with_strava[i]
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
                r = with_strava[i]
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
