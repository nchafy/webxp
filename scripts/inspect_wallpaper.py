#!/usr/bin/env python3
"""Screenshot the bare desktop, for iterating on the wallpaper.

The full check suite takes minutes and draws windows over the thing being looked at. This
loads the page, dismisses Clippy, closes every window and captures the desktop alone.

    .venv/bin/python scripts/inspect_wallpaper.py [--wall bliss] [--out wall.png] [--port PORT]

Without `--port` the server takes a free port the kernel picks, which cannot collide with
another run out of this checkout. A fixed port is what you pass when you want to attach to
the page yourself.
"""

from __future__ import annotations

import argparse
import base64
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
    parser.add_argument("--wall", default="bliss", help="bliss, flat or night")
    parser.add_argument("--out", default="screenshots/wall.png")
    parser.add_argument("--keep-clippy", action="store_true")
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
            time.sleep(1.4)

            page.evaluate(
                "(() => { const d = window.webxp;"
                " for (const w of [...d.windows]) w.close();"
                " for (const name of ['bliss', 'flat', 'night'])"
                "   d.surface.classList.toggle('wall-' + name, name === %r);"
                " const icons = d.surface.querySelector('.desktop-icons');"
                " if (icons) icons.style.setProperty('display', 'none');"
                " %s })()"
                % (args.wall, "" if args.keep_clippy else
                   "const c = document.querySelector('.clippy'); if (c) c.remove();")
            )
            time.sleep(0.5)

            out = REPO_ROOT / args.out
            out.parent.mkdir(exist_ok=True)
            data = page.call("Page.captureScreenshot", format="png")
            out.write_bytes(base64.b64decode(data["data"]))
            print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    except ServeError as error:
        # No server, or not this tree's server. Nothing was measured.
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
