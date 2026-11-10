#!/usr/bin/env python3
"""Measure the compass button against the SVG inside it.

Reported as "misaligned" from a screenshot. A screenshot says it is wrong; these numbers say
which box is off and by how much, which is what a fix needs.

    .venv/bin/python scripts/inspect_compass.py [--port PORT]

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

PROBE = """(() => {
  const w = window.webxp.windows.find((w) => w.programId === 'basemap');
  if (!w) return { error: 'no basemap window' };
  const zoom = w.bodyEl.querySelector('.map-zoom');
  const compass = w.bodyEl.querySelector('.map-compass');
  const svg = compass && compass.querySelector('svg');
  const plus = w.bodyEl.querySelector('.map-zoom-in');
  if (!compass || !svg) return { error: 'no compass' };

  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { x: +r.left.toFixed(1), y: +r.top.toFixed(1),
             w: +r.width.toFixed(1), h: +r.height.toFixed(1),
             cx: +(r.left + r.width / 2).toFixed(1),
             cy: +(r.top + r.height / 2).toFixed(1) };
  };
  const cs = getComputedStyle(compass);
  const ss = getComputedStyle(svg);
  return {
    zoomCol: box(zoom),
    plus: plus ? box(plus) : null,
    compass: box(compass),
    svg: box(svg),
    // The two numbers the fix turns on: how far the drawn circle's centre is from the
    // button's centre.
    offsetX: +(box(svg).cx - box(compass).cx).toFixed(2),
    offsetY: +(box(svg).cy - box(compass).cy).toFixed(2),
    compassStyle: {
      display: cs.display, placeItems: cs.placeItems, padding: cs.padding,
      border: cs.borderWidth, boxSizing: cs.boxSizing, lineHeight: cs.lineHeight,
      font: cs.fontSize,
    },
    svgStyle: { display: ss.display, width: ss.width, height: ss.height,
                verticalAlign: ss.verticalAlign },
    svgAttrs: { width: svg.getAttribute('width'), height: svg.getAttribute('height'),
                viewBox: svg.getAttribute('viewBox') },
  };
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
            page.clear_cache()
            page.call(
                "Emulation.setDeviceMetricsOverride",
                width=1280, height=820, deviceScaleFactor=1, mobile=False,
            )
            page.navigate(base + "/")
            time.sleep(1.2)
            page.evaluate("window.webxp.launch('basemap')")
            time.sleep(8)

            result = page.evaluate(PROBE)
            print(json.dumps(result, indent=2))

            if not result.get("error"):
                clip = result["compass"]
                pad = 10
                shot = page.call(
                    "Page.captureScreenshot",
                    format="png",
                    clip={"x": clip["x"] - pad, "y": clip["y"] - pad,
                          "width": clip["w"] + pad * 2, "height": clip["h"] + pad * 2,
                          "scale": 6},
                )
                out = REPO_ROOT / "screenshots" / "compass.png"
                out.parent.mkdir(exist_ok=True)
                out.write_bytes(base64.b64decode(shot["data"]))
                print(f"\nwrote {out}")
    except ServeError as error:
        # No server, or not this tree's server. Nothing was measured.
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
