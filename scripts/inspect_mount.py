#!/usr/bin/env python3
"""Browse the mounted repository in a real terminal window, and print what it says.

`check.py` asserts that `/usr/src/webxp` holds the project's own files, which does not show
whether a reader can *use* it: a listing that fits the assertion and wraps into mush, a
fetched file that arrives with its newlines mangled, or a size column that reads `0` are all
green. This runs a short session and prints the pane, plus a screenshot to look at.

    .venv/bin/python scripts/inspect_mount.py [--port 8112] [--out screenshots]

It also prints what each command cost in fetches, because "the bodies are lazy" is the whole
design and a `find` that quietly pulled 126 files would satisfy every assertion in the suite.
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

#: A session that exercises the three things the mount has to get right: metadata without a
#: fetch, contents with one, and the two refusals.
SESSION = (
    "ls /usr/src/webxp",
    "ls -l /usr/src/webxp/src",
    "du -sh /usr/src/webxp",
    "head -n 3 /usr/src/webxp/src/icons.mjs",
    "grep -rln MAX_FILE_SIZE /usr/src/webxp/shared",
    "cat /usr/src/webxp/shared/map/vendor/maplibre-gl.mjs",
    "find /usr/src/webxp -name private",
    "df -h",
)

#: Counts every request the page makes, so a command's fetches can be attributed to it.
COUNTER = """(() => {
  window.__fetches = [];
  const original = window.fetch;
  window.fetch = (...args) => {
    window.__fetches.push(String(args[0] && args[0].url ? args[0].url : args[0]));
    return original.apply(window, args);
  };
})()"""

RUN = """(async () => {
  const win = window.webxp.windows.find((w) => w.programId === 'terminal');
  if (!win || !win.app) return { error: 'no terminal window' };
  const before = window.__fetches.length;
  const log = win.el.querySelector('.term-log');
  const was = log.textContent.length;
  await win.app.shell.run(LINE, {
    write: (text) => { const s = document.createElement('span'); s.textContent = text; log.append(s); },
    warn: (text) => { const s = document.createElement('span'); s.className = 'term-error'; s.textContent = text; log.append(s); },
    control: () => {},
  });
  log.scrollTop = log.scrollHeight;
  return {
    output: log.textContent.slice(was),
    fetches: window.__fetches.length - before,
  };
})()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8112)
    parser.add_argument("--out", default="screenshots")
    args = parser.parse_args(argv)
    base = f"http://127.0.0.1:{args.port}"
    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(exist_ok=True)

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
        else:
            print("server did not come up", file=sys.stderr)
            return 1

        with Browser() as browser:
            page = browser.page()
            page.call("Emulation.setDeviceMetricsOverride", width=1280, height=820,
                      deviceScaleFactor=1, mobile=False)
            page.navigate(base + "/")
            time.sleep(1.0)
            page.evaluate(COUNTER)
            page.evaluate("window.webxp.launch('terminal')")
            time.sleep(1.2)
            page.evaluate(
                "window.webxp.windows.find((w) => w.programId === 'terminal').sizeTo(880, 560)"
            )
            time.sleep(0.3)

            for line in SESSION:
                import json as _json
                result = page.evaluate(RUN.replace("LINE", _json.dumps(line)), await_promise=True)
                if result.get("error"):
                    print(result["error"], file=sys.stderr)
                    return 1
                print(f"\n$ {line}    [{result['fetches']} fetch(es)]")
                body = result["output"].rstrip("\n")
                for row in body.split("\n")[:14]:
                    print(f"  {row}")
                if body.count("\n") >= 14:
                    print(f"  … {body.count(chr(10)) - 13} more lines")

            data = page.call("Page.captureScreenshot", format="png")
            target = out_dir / "mount-session.png"
            target.write_bytes(base64.b64decode(data["data"]))
            print(f"\nwrote {target.relative_to(REPO_ROOT)}")

            # The same mount, seen the other way. A folder window is the view where a wrong
            # size column or an unreadable row is obvious and a pane of text is not.
            page.evaluate(
                "window.webxp.windows.find((w) => w.programId === 'terminal').minimize()"
            )
            page.evaluate("window.webxp.launch('explorer', { path: '/usr/src/webxp/src' })")
            time.sleep(1.4)
            page.evaluate(
                "(() => { const w = window.webxp.windows.find((w) => w.programId === 'explorer');"
                " w.moveTo(120, 60); w.sizeTo(840, 480); })()"
            )
            time.sleep(0.4)
            rows = page.evaluate(
                "(() => { const w = window.webxp.windows.find((w) => w.programId === 'explorer');"
                " return [...w.el.querySelectorAll('.exp-row')].map((r) => ["
                "   r.querySelector('.exp-name').textContent.trim(),"
                "   r.querySelector('.exp-size').textContent.trim(),"
                "   r.querySelector('.exp-type').textContent.trim()].join(' | ')); })()"
            )
            print("\nExplorer at /usr/src/webxp/src:")
            for row in rows:
                print(f"  {row}")
            data = page.call("Page.captureScreenshot", format="png")
            target = out_dir / "mount-explorer.png"
            target.write_bytes(base64.b64decode(data["data"]))
            print(f"\nwrote {target.relative_to(REPO_ROOT)}")
    finally:
        server.terminate()
        server.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
