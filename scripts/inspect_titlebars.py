#!/usr/bin/env python3
"""Print the resolved title-bar gradient for the focused and an unfocused window.

`check.py` asserts the two differ, which does not say which is which. A focused window
wearing the unfocused gradient passes that assertion and is still wrong, so this prints the
values for a person to read.

    .venv/bin/python scripts/inspect_titlebars.py [--port PORT]

Without `--port` the server takes a free port the kernel picks, which cannot collide with
another run out of this checkout. A fixed port is what you pass when you want to attach to
the page yourself.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402
from serve import ServeError, serve  # noqa: E402

REPORT = """(() => {
  const d = window.webxp;
  return d.windows.map((w) => {
    const s = getComputedStyle(w.el.querySelector('.window-title'));
    return {
      title: w.titleText,
      focused: d.active === w,
      gradient: s.backgroundImage,
      colour: s.color,
    };
  });
})()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        help="serve on this port instead of a free one the kernel picks; for attaching to "
        "the page yourself",
    )
    args = parser.parse_args(argv)

    try:
        # `serve` picks the port, proves the bytes on it are this tree's, and stops the
        # server on the way out.
        with serve(args.port) as base, Browser() as browser:
            page = browser.page()
            page.navigate(base + "/")
            time.sleep(1.0)
            page.evaluate("window.webxp.launch('notepad')")
            time.sleep(0.8)
            for row in page.evaluate(REPORT):
                state = "FOCUSED " if row["focused"] else "unfocused"
                print(f"{state}  {row['title']}")
                print(f"           colour   {row['colour']}")
                print(f"           gradient {row['gradient']}")
    except ServeError as error:
        # No server, or not this tree's server. Nothing was measured.
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
