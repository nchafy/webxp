#!/usr/bin/env python3
"""Ask the Base map window whether it actually drew anything.

`check.py` measured the canvas and found it the right size, which a map that fetched no tile
also is. This prints what MapLibre itself knows — tiles loaded, sources, errors — plus the
drawing buffer's dimensions, so a blank window can be told apart from a working one.

    .venv/bin/python scripts/inspect_basemap.py [--port 8160]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

REPORT = """(() => {
  const w = window.webxp.windows.find((w) => w.programId === 'basemap');
  if (!w) return { error: 'no basemap window' };
  if (!w.base) return { error: 'no map handle', body: w.bodyEl.textContent.slice(0, 120) };
  const map = w.base.map;
  const canvas = w.bodyEl.querySelector('.maplibregl-canvas');
  const rect = canvas.getBoundingClientRect();
  const style = map.getStyle();
  const sources = {};
  for (const id of Object.keys(style.sources || {})) {
    const s = map.getSource(id);
    sources[id] = { type: s && s.type, loaded: s && s.loaded ? s.loaded() : null };
  }
  return {
    basemap: w.base.basemap,
    styleLoaded: map.isStyleLoaded(),
    loaded: map.loaded(),
    tilesLoaded: map.areTilesLoaded(),
    repaint: map.repaint,
    cssSize: { w: Math.round(rect.width), h: Math.round(rect.height) },
    bufferSize: { w: canvas.width, h: canvas.height },
    pixelRatio: window.devicePixelRatio,
    layers: style.layers.length,
    sources,
    center: map.getCenter(),
    zoom: map.getZoom(),
    status: (w.bodyEl.querySelector('.map-status') || {}).textContent,
    chain: (() => {
      const out = [];
      let node = canvas;
      while (node && node !== document.body) {
        const r = node.getBoundingClientRect();
        const cs = getComputedStyle(node);
        out.push({
          tag: node.tagName.toLowerCase(),
          cls: (node.className || '').toString().slice(0, 40),
          box: [Math.round(r.width), Math.round(r.height)],
          position: cs.position, display: cs.display,
          height: cs.height, overflow: cs.overflow,
        });
        node = node.parentElement;
      }
      return out;
    })(),
  };
})()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8160)
    parser.add_argument("--wait", type=float, default=20.0)
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
            page.call("Runtime.enable")
            page.call("Log.enable")
            page.call(
                "Emulation.setDeviceMetricsOverride",
                width=1280, height=820, deviceScaleFactor=1, mobile=False,
            )
            page.navigate(base + "/")
            time.sleep(1.2)
            page.evaluate("window.webxp.launch('basemap')")
            time.sleep(args.wait)

            print(json.dumps(page.evaluate(REPORT), indent=2, default=str))

            # The flags above are what the map believes. This is what it drew.
            import base64
            shot = page.call("Page.captureScreenshot", format="png")
            out = REPO_ROOT / "screenshots" / "inspect-basemap.png"
            out.parent.mkdir(exist_ok=True)
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"\nwrote {out}")

            print("\n--- console ---")
            for event in page.consume_events():
                method = event.get("method")
                params = event.get("params") or {}
                if method == "Log.entryAdded":
                    entry = params.get("entry") or {}
                    print(f"  [{entry.get('level')}] {entry.get('text')} {entry.get('url') or ''}")
                elif method == "Runtime.exceptionThrown":
                    details = params.get("exceptionDetails") or {}
                    print(f"  [uncaught] {(details.get('exception') or {}).get('description')}")
                elif method == "Runtime.consoleAPICalled":
                    parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
                    print(f"  [{params.get('type')}] {' '.join(parts)}")
    finally:
        server.terminate()
        server.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
