#!/usr/bin/env python3
"""Print the resolved title-bar gradient for the focused and an unfocused window.

`check.py` asserts the two differ, which does not say which is which. A focused window
wearing the unfocused gradient passes that assertion and is still wrong, so this prints the
values for a person to read.

    .venv/bin/python scripts/inspect_titlebars.py [--port 8111]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    parser.add_argument("--port", type=int, default=8111)
    args = parser.parse_args(argv)
    base = f"http://127.0.0.1:{args.port}"

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
            page.navigate(base + "/")
            time.sleep(1.0)
            page.evaluate("window.webxp.launch('notepad')")
            time.sleep(0.8)
            for row in page.evaluate(REPORT):
                state = "FOCUSED " if row["focused"] else "unfocused"
                print(f"{state}  {row['title']}")
                print(f"           colour   {row['colour']}")
                print(f"           gradient {row['gradient']}")
    finally:
        server.terminate()
        server.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
