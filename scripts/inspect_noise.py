#!/usr/bin/env python3
"""Open day 02, turn the segments on, and screenshot the window.

The ramp changed from diverging blue-to-red on a signed residual to sequential amber on a
percentage. `check.py` asserts the layers exist and the status bar sits below the map; it does
not look at what the ramp painted, and a ramp is exactly the thing that has to be looked at.

    .venv/bin/python scripts/inspect_noise.py [--port PORT]

Without `--port` the server takes a free port the kernel picks, which cannot collide with
another run out of this checkout. A fixed port is what you pass when you want to attach to
the page yourself.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402
from serve import ServeError, serve  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


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
            page.clear_cache()
            page.call(
                "Emulation.setDeviceMetricsOverride",
                width=1280, height=860, deviceScaleFactor=1, mobile=False,
            )
            page.navigate(base + "/")
            time.sleep(1.2)
            page.evaluate("document.querySelector('.clippy')?.remove()")
            page.evaluate("window.webxp.launch('day-02-lines')")
            time.sleep(14)

            # Maximise it and turn the segments on through the real control.
            page.evaluate(
                "(() => { const w = window.webxp.windows.find((w) => w.programId === 'day-02-lines');"
                " if (!w) return; w.maximize();"
                " const t = w.bodyEl.querySelector('[data-role=noise-toggle]');"
                " if (t) t.click(); })()"
            )
            time.sleep(9)

            state = page.evaluate(
                "(() => { const w = window.webxp.windows.find((w) => w.programId === 'day-02-lines');"
                " if (!w) return { error: 'no window' };"
                " const note = w.bodyEl.querySelector('[data-role=noise-note]');"
                " const legend = [...w.bodyEl.querySelectorAll('[data-role=noise-legend] > div')]"
                "   .map((r) => r.textContent.trim());"
                " const agg = w.bodyEl.querySelector('[data-role=aggregate]');"
                " return { note: note && note.textContent,"
                "   legend,"
                "   aggregate: agg && agg.textContent,"
                "   legendHidden: w.bodyEl.querySelector('[data-role=noise-legend]')?.hidden }; })()"
            )
            print(json.dumps(state, indent=2))

            out = REPO_ROOT / "screenshots" / "noise.png"
            out.parent.mkdir(exist_ok=True)
            shot = page.call("Page.captureScreenshot", format="png")
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"\nwrote {out}")
    except ServeError as error:
        # No server, or not this tree's server. Nothing was measured.
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
