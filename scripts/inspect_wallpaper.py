#!/usr/bin/env python3
"""Screenshot the bare desktop, for iterating on the wallpaper.

The full check suite takes minutes and draws windows over the thing being looked at. This
loads the page, dismisses Clippy, closes every window and captures the desktop alone.

    .venv/bin/python scripts/inspect_wallpaper.py [--wall bliss] [--out wall.png]
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8271)
    parser.add_argument("--wall", default="bliss", help="bliss, flat or night")
    parser.add_argument("--out", default="screenshots/wall.png")
    parser.add_argument("--keep-clippy", action="store_true")
    args = parser.parse_args(argv)
    base = f"http://127.0.0.1:{args.port}"

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/", timeout=5)
                break
            except OSError:
                time.sleep(0.1)

        with Browser() as browser:
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
    finally:
        server.terminate()
        server.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
