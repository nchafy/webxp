#!/usr/bin/env python3
"""Show `check.py`'s `COLOUR_READER` reading black as black and junk as unreadable.

The colour sweeps in `check.py` route every value through `window.__wxColours`, and an entry
it cannot parse is a failure rather than a skip. So the reader has to separate two things a
canvas does not distinguish on its own: a value that resolved to black, and a value the
browser could not parse at all — `fillStyle` keeps its previous value in the second case and
says nothing. Probing against black conflated them, and `rgb(0, 0, 0)` — what
`getComputedStyle` returns for `color: black` — came back `UNREADABLE:`.

Both outcomes are measured here against the real reader, imported from `check.py` rather
than copied, so this cannot pass against a stale duplicate.

    .venv/bin/python scripts/inspect_colour_reader.py
    .venv/bin/python scripts/inspect_colour_reader.py --prove-failure

Without `--port` the server takes a free port the kernel picks, which cannot collide with
another run out of this checkout. A fixed port is what you pass when you want to attach to
the page yourself.

`--prove-failure` reinstalls the reader with the black sentinel it used to carry and requires
the black control to break. A control that has never been seen to fail is a control nobody
has checked.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402
from check import COLOUR_READER, rgb_stops, unreadable  # noqa: E402
from serve import ServeError, serve  # noqa: E402

#: The line the sentinel lives on, and the value it had while the bug was live. `--prove-failure`
#: swaps one for the other; a swap that matched nothing is an error, not a quiet no-op.
SENTINEL_LINE = "const SENTINEL = '#010203';"
BLACK_SENTINEL_LINE = "const SENTINEL = '#000';"

#: Read off a real element rather than written as a literal: `rgb(0, 0, 0)` is what
#: `getComputedStyle` hands back for a black declaration, and it is the spelling the old
#: reader mistook for unparseable. `#000` and `black` were special-cased by name and so
#: would have passed — they are not the control.
BLACK = """(() => {
  const el = document.createElement('span');
  el.style.color = 'black';
  el.style.backgroundImage = 'linear-gradient(90deg, black 0%, white 100%)';
  document.body.append(el);
  try {
    const s = getComputedStyle(el);
    return { colour: s.color, gradient: s.backgroundImage,
             readColour: window.__wxColours(s.color),
             readGradient: window.__wxColours(s.backgroundImage) };
  } finally {
    el.remove();
  }
})()"""

#: Not a colour in any notation, so the browser leaves `fillStyle` alone and the reader must
#: say so. An unparseable value is a failure in the sweeps; it must never become an empty list.
JUNK = "window.__wxColours('not-a-colour, rgb(255, 0, 0)')"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        help="serve on this port instead of a free one the kernel picks; for attaching to "
        "the page yourself",
    )
    parser.add_argument(
        "--prove-failure",
        action="store_true",
        help="install the old black sentinel and require the black control to fail",
    )
    args = parser.parse_args(argv)

    reader = COLOUR_READER
    if args.prove_failure:
        if SENTINEL_LINE not in reader:
            print(
                f"COLOUR_READER no longer contains {SENTINEL_LINE!r}, so this flag would "
                "install the current reader and prove nothing",
                file=sys.stderr,
            )
            return 1
        reader = reader.replace(SENTINEL_LINE, BLACK_SENTINEL_LINE)

    try:
        # `serve` picks the port, proves the bytes on it are this tree's, and stops the
        # server on the way out.
        with serve(args.port) as base, Browser() as browser:
            page = browser.page()
            page.navigate(base + "/")
            page.evaluate(reader)
            black = page.evaluate(BLACK)
            junk = page.evaluate(JUNK)
    except ServeError as error:
        print(error, file=sys.stderr)
        return 1

    print(json.dumps({"black": black, "junk": junk}, indent=2))

    bad = []

    # Control one: a colour that genuinely resolves to black reads as black.
    if unreadable(black["readColour"]):
        bad.append(
            f"black: {black['colour']} came back "
            f"{unreadable(black['readColour'])} — a value that resolves to black is being "
            "reported as unparseable, which the sweeps treat as a failure"
        )
    elif rgb_stops(black["readColour"]) != [(0, 0, 0)]:
        bad.append(
            f"black: {black['colour']} read as {rgb_stops(black['readColour'])}, want "
            "[(0, 0, 0)]"
        )
    if (0, 0, 0) not in rgb_stops(black["readGradient"]):
        bad.append(
            f"black: the black stop of {black['gradient']} is missing from "
            f"{black['readGradient']} — a gradient stop is read by the same probe"
        )

    # Control two: an unparseable value is still named, not skipped.
    if unreadable(junk) != ["not-a-colour"]:
        bad.append(
            f"junk: 'not-a-colour' read as {junk} — an unparseable value must be reported, "
            "never dropped"
        )
    if rgb_stops(junk) != [(255, 0, 0)]:
        bad.append(
            f"junk: the parseable half read as {rgb_stops(junk)}, want [(255, 0, 0)] — the "
            "unreadable entry must not take the rest of the list with it"
        )

    if args.prove_failure:
        # Only the black control is expected to break: the junk control does not depend on
        # which sentinel is in use, and if it breaks too the swap did something else.
        black_broke = [message for message in bad if message.startswith("black:")]
        junk_broke = [message for message in bad if message.startswith("junk:")]
        if junk_broke:
            print("\n--prove-failure: " + "\n".join(junk_broke), file=sys.stderr)
            print("the black sentinel should not change what an unparseable value reads as",
                  file=sys.stderr)
            return 1
        if not black_broke:
            print(
                "\n--prove-failure: the black sentinel reported no failure, so this control "
                "has not been shown capable of failing",
                file=sys.stderr,
            )
            return 1
        print("\n--prove-failure: black failed as required —\n  " + "\n  ".join(black_broke))
        return 0

    if bad:
        print()
        for message in bad:
            print(f"  [FAIL] {message}")
        return 1
    print("\nblack reads as black, and an unparseable value still reads as UNREADABLE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
