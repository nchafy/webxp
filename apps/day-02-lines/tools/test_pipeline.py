"""Hold `prepare.py`'s output against the fixture's known truth.

    .venv/bin/python days/02-lines/make_fixture.py --out days/02-lines/fixture
    .venv/bin/python days/02-lines/prepare.py days/02-lines/fixture --out days/02-lines/data
    .venv/bin/python days/02-lines/test_pipeline.py

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
FAILURES: list[str] = []

# How far the matched length may sit from the route it was generated from. Measured at
# 0.1% to 0.5% across the fixture; 2% leaves headroom for a tileset update changing a
# path slightly without turning this into a tripwire.
MATCH_TOLERANCE = 0.02


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def main() -> int:
    fixture = HERE / "fixture"
    data = HERE / "data"
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
