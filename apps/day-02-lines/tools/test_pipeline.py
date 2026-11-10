"""Hold `prepare.py`'s output against the fixture's known truth.

    .venv/bin/python apps/day-02-lines/tools/make_fixture.py --out apps/day-02-lines/fixture
    .venv/bin/python apps/day-02-lines/tools/prepare.py apps/day-02-lines/fixture \
        --out apps/day-02-lines/data --trim-m 0
    .venv/bin/python apps/day-02-lines/tools/test_pipeline.py

`--trim-m 0` is required, not optional. `prepare.py` crops a quarter mile off each end by
default, and these assertions compare against whole-route lengths — a cropped fixture fails
every one of them for a reason that has nothing to do with what they test. The fixture's
routes are 1.1 to 4.8 km, so the crop would take most of the short ones. `main` checks for
this and says so rather than reporting six mysterious failures.

`fixture/truth.json` records, per activity, the length Valhalla reported for the route
the trace was built from. That number came out of a different request than the one
`prepare.py` makes, so agreeing with it is evidence rather than tautology.

The claim being checked is the one the whole day rests on: **snapping a noisy trace back
to the network recovers the distance actually covered.** If that fails, the page is
comparing a recorded length against a number that means nothing.
"""

from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prepare  # noqa: E402

#: The app folder. `fixture/` and `data/` live beside `app.mjs`, one level above these
#: scripts. Anchored on `HERE` this found neither, printed "run make_fixture.py first" and
#: returned 1 without executing a single `check()` — while the README cited its output as
#: live evidence.
APP = HERE.parent
FAILURES: list[str] = []

# How far the matched length may sit from the route it was generated from. Measured at
# -0.07% to -0.52% across the fixture's six activities, every one of them short; 2% leaves
# headroom for a tileset update changing a path slightly without turning this into a
# tripwire. Re-run this file to reproduce the six figures -- it prints each one.
MATCH_TOLERANCE = 0.02


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def main() -> int:
    fixture = APP / "fixture"
    data = APP / "data"
    truth_path = fixture / "truth.json"
    if not truth_path.is_file():
        print(f"no fixture truth at {truth_path}; run make_fixture.py first")
        return 1
    index_path = data / "index.json"
    if not index_path.is_file():
        print(f"no pipeline output at {index_path}; run prepare.py first")
        return 1

    truth = json.loads(truth_path.read_text())
    index = json.loads(index_path.read_text())
    by_name = {a["name"]: a for a in index["activities"]}

    # Refuse a cropped fixture outright, rather than failing six assertions obscurely.
    #
    # `prepare.py` crops a quarter mile off each end by default, and every assertion below
    # compares against a whole-route length. A cropped `data/` is not a regression in the
    # measurement; it is the wrong input, and saying which is the difference between a
    # five-minute fix and an afternoon. Detected by the recorded lengths coming in far under
    # what the fixture recorded, which no measurement error could produce.
    short = []
    for fname, t in truth.items():
        got = by_name.get(t["name"])
        if got and got.get("recorded_m") and t.get("recorded_m"):
            if got["recorded_m"] < t["recorded_m"] * 0.9:
                short.append((t["name"], t["recorded_m"], got["recorded_m"]))
    if len(short) >= max(2, len(truth) // 2):
        print(
            f"{len(short)} of {len(truth)} activities are much shorter than the fixture "
            "recorded, so `data/` was built with the end crop on."
        )
        for name, want, got in short[:3]:
            print(f"    {name}: {want:.0f} m recorded, {got:.0f} m in the output")
        print("  Rebuild it with --trim-m 0; these checks compare whole-route lengths.")
        return 1

    check("pipeline reported no failures", not index["failures"], str(index["failures"]))
    check(
        "every fixture activity is in the output",
        len(by_name) == len(truth),
        f"{len(by_name)} of {len(truth)}",
    )

    for fname, t in sorted(truth.items()):
        name = t["name"]
        got = by_name.get(name)
        if got is None:
            check(f"{name}: present in output", False, f"missing (from {fname})")
            continue

        # The headline claim.
        if got["matched_m"] is None:
            check(f"{name}: matched", False, got.get("match_note") or "no match")
        else:
            err = (got["matched_m"] - t["route_m"]) / t["route_m"]
            check(
                f"{name}: matching recovers the route length",
                abs(err) <= MATCH_TOLERANCE,
                f"matched {got['matched_m'] / 1000:.3f} km vs route "
                f"{t['route_m'] / 1000:.3f} km ({err:+.2%})",
            )

        # The recorded length must reproduce what the fixture measured when it wrote the
        # file -- a different code path, through GPX text at six decimal places. A
        # mismatch means the writer and the reader disagree about the geometry.
        rel = abs(got["recorded_m"] - t["recorded_m"]) / t["recorded_m"]
        check(
            f"{name}: recorded length survives the GPX round trip",
            rel < 2e-3,
            f"read {got['recorded_m']:,.0f} m, written {t['recorded_m']:,.0f} m ({rel:.3%})",
        )

        # And the ordering. Noise adds length; snapping removes it.
        if got["matched_m"]:
            check(
                f"{name}: recorded exceeds matched",
                got["recorded_m"] > got["matched_m"],
                f"{got['recorded_m']:,.0f} m vs {got['matched_m']:,.0f} m",
            )

        # The reference distance must have been resolved to the right scale. The fixture
        # writes the route length into the CSV, in two differently-scaled columns.
        if got["strava_m"] is None:
            check(f"{name}: reference distance resolved", False, "no strava_m")
        else:
            rel = abs(got["strava_m"] - t["route_m"]) / t["route_m"]
            check(
                f"{name}: reference distance is on the right scale",
                rel < 0.01,
                f"{got['strava_m']:,.0f} m vs {t['route_m']:,.0f} m ({rel:.2%})",
            )

    # The ladder must be monotone in every written document, not just in the unit test's
    # synthetic trace.
    #
    # Driven from the index rather than by globbing the directory. Globbing and skipping
    # `index.json` by name broke the moment `build_overview.py` started writing
    # `overview.json` alongside: the test read it as an activity and died on a missing key.
    # Asking the index which documents it claims exist is both narrower and a stronger
    # assertion -- a row with no document now fails here instead of being skipped.
    for row in index["activities"]:
        p = data / f"{row['id']}.json"
        if not p.is_file():
            check(f"{row['name']}: document written", False, f"{p.name} is missing")
            continue
        doc = json.loads(p.read_text())
        lengths = [row[1] for row in doc["ladder"]]
        mono = all(lengths[i] <= lengths[i - 1] + 1e-6 for i in range(1, len(lengths)))
        check(f"{doc['name']}: written ladder is monotone", mono,
              f"{lengths[0]:,.0f} m down to {lengths[-1]:,.0f} m")
        check(f"{doc['name']}: geometry written", len(doc["recorded"]) > 10,
              f"{len(doc['recorded'])} recorded, {len(doc['matched'])} matched points")

    # The two committed overlays, which nothing else holds.
    #
    # `check_day_02_segment_spans` in `scripts/check.py` counts crossings whose `[i0, i1]`
    # runs past the end of the run it names -- and cannot tell a file with no crossing in it
    # from a file it was never given, because both loop zero times. An overlay that degraded
    # to empty would pass there while the file sat on disk looking measured. So the counts are
    # asserted here, where the fixture that produced them is, and before any span is trusted.
    #
    # Counted from the file rather than read out of a figure written beside the features: a
    # stated count and the features it describes are two renderings of one fact, and the one
    # that goes stale is the one being asserted on.
    points = {a["id"]: a["points"] for a in index["activities"]}
    for name in prepare.OVERLAYS:
        path = data / name
        if not path.is_file():
            check(f"{name}: written into data/", False, f"{path} is missing")
            continue
        overlay = json.loads(path.read_text())
        segments = overlay.get("features") or []
        crossings = [
            crossing
            for feature in segments
            for crossing in feature["properties"].get("passes", [])
        ]
        check(f"{name}: segments measured", bool(segments), f"{len(segments)} segments")
        check(f"{name}: crossings measured", bool(crossings), f"{len(crossings)} crossings")
        # `data/` is the synthetic fixture, and a percentage measured over it is not a
        # percentage measured over an export -- white noise against a receiver, and a widened
        # spacing band against the published one. The label travels in the file, so a reader
        # who meets the figures meets the distinction. Without it a green suite reads as
        # evidence about a real recording, which nothing here measured.
        check(
            f"{name}: labelled synthetic",
            overlay.get("synthetic") is True,
            f"synthetic={overlay.get('synthetic')!r}, measured={overlay.get('measured')!r}",
        )
        # Both ends of every span, against the run it names. `i1` is inclusive: `lines.mjs`
        # slices `doc.recorded[i0 : i1 + 1]`, so `i1 == points` reads one past the end.
        off = [
            f"{c['activity']}[{c['i0']}:{c['i1']}]"
            for c in crossings
            if points.get(c["activity"]) is None
            or not (0 <= c["i0"] <= c["i1"] < points[c["activity"]])
        ]
        check(
            f"{name}: every span addresses its own run",
            not off,
            f"{len(crossings) - len(off)} of {len(crossings)} fit"
            + (f"; {', '.join(off[:3])}" if off else ""),
        )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  {f}")
        return 1
    print("pipeline output agrees with the fixture truth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
