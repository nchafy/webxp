#!/usr/bin/env python3
"""Show that the browser can read a stale `apps/manifest.mjs`, and that disabling the cache
is what stops it.

`scaffold a new app` failed once in roughly a dozen runs with the scaffolded app missing from
the loaded list, and passed on the retry. The diagnosis was HTTP caching: `http.server`
validates with `Last-Modified`, whose granularity is one second, so a manifest rewritten
inside the same second as an earlier fetch answers 304 and Chrome keeps the previous module.

A diagnosis is not an observation until it is reproduced at the layer it names. This forces
the race deliberately — fetch the manifest, rewrite it in the same second, reload — with the
cache on and then off, and prints what the page saw each time.

    .venv/bin/python scripts/inspect_manifest_cache.py [--port PORT] [--rounds 4]

Without `--port` the server takes a free port the kernel picks, which cannot collide with
another run out of this checkout. A fixed port is what you pass when you want to attach to
the page yourself.

Expected: `stale` rows with the cache on, `fresh` rows with it off. Any `fresh` row with the
cache on means that round simply missed the one-second window; the stale rows are the signal.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser  # noqa: E402
from serve import ServeError, serve  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_ID = "cache-probe"

#: What the page itself loaded, not a fresh import.
#:
#: The first version of this asked `import('/apps/manifest.mjs?' + 0)`, which is a different
#: URL from the one `demo/boot.mjs` imports and so a separately cached module. It reported
#: `fresh` every time while saying nothing about what the desktop saw. `window.webxpApps` is
#: the loader's own result, which is the thing the failing check reads.
SEEN = "window.webxpApps ? window.webxpApps.loaded : []"


def scaffold() -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "apps.py"), "--new", PROBE_ID],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    )


def remove() -> None:
    shutil.rmtree(REPO_ROOT / "apps" / PROBE_ID, ignore_errors=True)
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "apps.py")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def round_trip(page, base: str) -> str:
    """Load the page, rewrite the manifest at once, reload, and report what the page sees."""
    remove()
    page.navigate(base + "/")
    time.sleep(0.6)
    # Inside the same second as the fetch above, which is the whole point.
    scaffold()
    page.navigate(base + "/")
    time.sleep(0.8)
    seen = page.evaluate(SEEN)
    return "fresh" if PROBE_ID in seen else "stale"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        help="serve on this port instead of a free one the kernel picks; for attaching to "
        "the page yourself",
    )
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args(argv)

    results: dict[str, list[str]] = {"cache on": [], "cache off": []}
    try:
        # `serve` picks the port, proves the bytes on it are this tree's, and stops the
        # server on the way out.
        with serve(args.port) as base:
            try:
                for label in ("cache on", "cache off"):
                    with Browser() as browser:
                        page = browser.page()
                        page.call(
                            "Emulation.setDeviceMetricsOverride",
                            width=1280, height=820, deviceScaleFactor=1, mobile=False,
                        )
                        if label == "cache off":
                            page.clear_cache()
                        for _ in range(args.rounds):
                            results[label].append(round_trip(page, base))
                            print(f"  {label}: {results[label][-1]}")
            finally:
                # The probe app is scaffolded into the tree, so it comes out whatever happens.
                remove()
    except ServeError as error:
        # No server, or not this tree's server. Nothing was measured.
        print(error, file=sys.stderr)
        return 1

    print("\n" + json.dumps(results, indent=2))
    stale_on = results["cache on"].count("stale")
    stale_off = results["cache off"].count("stale")
    print(f"\nstale reads: {stale_on}/{args.rounds} with the cache on, "
          f"{stale_off}/{args.rounds} with it off")
    if stale_on and not stale_off:
        print("Diagnosis confirmed at the layer it names.")
        return 0
    if not stale_on:
        print("Did not reproduce. The one-second window was missed, or the cause is elsewhere.")
        return 1
    print("Disabling the cache did not stop it. The cause is elsewhere.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
