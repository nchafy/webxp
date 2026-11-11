#!/usr/bin/env python3
"""Every stylesheet an app declares really loads when that app is opened alone.

`src/apps.mjs` links a stylesheet once per session, keyed by href. That is deliberate — a
second app declaring the same sheet should not get a second `<link>`. The consequence is
that **a wrong declaration is invisible whenever another app declares the same sheet
correctly.** Misspell the path in one app, and the sheet is still in the document, still
applying, put there by the other app. Open that app on its own and the window is unstyled.

Nothing in the loader sees it: from its side the sheet is present. Nothing in a suite that
opens several apps sees it either, because the app that declares it correctly links it for
everything after. One app in a fresh document is the only instrument that does.

## What this cannot catch, stated because the first version of it claimed otherwise

An **absent** declaration. This derives what an app needs from what it declares, so a
missing line is something nothing here expects — the reasoning is circular and no control
can rescue it. That is a real defect: a folder app rendered its grid correctly for an
afternoon with no `src/folder.css` entry of its own, because another app declaring that sheet
had been opened first.

The instrument for that is different: launch one app alone and assert a *styling property*
it should have — the border its own sheet removes, the grid its own sheet sets. That is what
found that case, and it needs one assertion per app rather than a sweep.

Run it through the virtualenv, as `README.md` runs every browser script here: `cdp.py` imports
`websocket`, which the system interpreter does not have, so a bare `python3` ends in
`ModuleNotFoundError` before anything is checked.

    .venv/bin/python scripts/check_styles_isolated.py [--port 8188]
    .venv/bin/python scripts/check_styles_isolated.py --prove-failure

`--prove-failure` really misspells one shared declaration in one app's source, runs, requires
to be told, and puts the file back. Two earlier versions of this control were empty: the first
skipped the assertion for that app, and a skipped assertion cannot fail; the second removed
the declaration, which also removed the expectation derived from it. A control has to break
the thing being checked, not the checking.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser, CDPError, find_chrome  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = REPO_ROOT / "apps"

#: How long to let the desktop register its programs before launching one.
BOOT_SECONDS = 3.5

#: How long after `launch` resolves before reading the document's links. `launch` already
#: awaits the app's stylesheets, so this is slack rather than a race.
SETTLE_SECONDS = 1.5


def declared_styles(app: str) -> list[str]:
    """The `styles` array of one app's spec, read out of its source.

    Parsed rather than imported: this runs in Python and the spec is an ES module. A spec
    whose `styles` is built at runtime would be missed, which is why the count is reported
    — a zero where a sheet is expected is worth looking at by hand.
    """
    source = (APPS_DIR / app / "app.mjs").read_text(encoding="utf-8")
    match = re.search(r"styles:\s*\[(.*?)\]", source, re.S)
    if not match:
        return []
    return re.findall(r"'([^']+)'", match.group(1))


def apps() -> list[str]:
    return sorted(
        child.name
        for child in APPS_DIR.iterdir()
        if child.is_dir() and (child / "app.mjs").is_file()
    )


def shared_sheets(by_app: dict[str, list[str]]) -> dict[str, list[str]]:
    """Sheet -> the apps declaring it, for sheets more than one app declares.

    These are the only ones that can hide a missing declaration. A sheet exactly one app
    declares cannot: drop that declaration and nothing links it.
    """
    # Keyed by resolved path, not by the declared string. `'app.css'` is relative, so every
    # app declares that same string for a different file — keying by the string reported
    # eleven app-private sheets as one shared one, which is the opposite of the truth.
    owners: dict[str, list[str]] = {}
    for app, sheets in by_app.items():
        for sheet in sheets:
            resolved = str((APPS_DIR / app / sheet).resolve())
            owners.setdefault(resolved, []).append(app)
    return {
        str(Path(sheet).relative_to(REPO_ROOT)): names
        for sheet, names in owners.items()
        if len(names) > 1
    }


def linked_in_isolation(page, app: str) -> list[str] | None:
    """Every stylesheet href in the document after launching `app` and nothing else."""
    opened = page.evaluate(
        f"(window.webxp||window.desktop).launch({json.dumps(app)}).then((w) => Boolean(w))",
        await_promise=True,
    )
    if not opened:
        return None
    time.sleep(SETTLE_SECONDS)
    return json.loads(
        page.evaluate(
            "JSON.stringify([...document.querySelectorAll('link[rel=stylesheet]')]"
            ".map((l) => l.href))"
        )
    )


def audit(base: str, by_app: dict[str, list[str]]) -> list[str]:
    """One fresh document per app. Returns a line per app that borrowed a sheet."""
    problems: list[str] = []
    for app, sheets in by_app.items():
        expected = list(sheets)
        if not expected:
            continue
        # A new browser, not a new tab: a tab shares the document's `<head>` only within a
        # page, but the loader's own cache is per page load, and a fresh browser removes any
        # doubt about what carried over.
        with Browser() as browser:
            page = browser.page()
            page.navigate(base + "/")
            time.sleep(BOOT_SECONDS)
            linked = linked_in_isolation(page, app)
        if linked is None:
            problems.append(f"{app}: did not launch, so its styles were never checked")
            continue
        for sheet in expected:
            name = sheet.rsplit("/", 1)[-1]
            if not any(name in href for href in linked):
                problems.append(
                    f"{app}: declares {sheet} and it is not linked when {app} opens alone"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument(
        "--prove-failure",
        action="store_true",
        help="drop one shared declaration and require this to notice",
    )
    args = parser.parse_args(argv)

    if not find_chrome():
        print("error: no Chrome or Chromium found, so nothing was checked", file=sys.stderr)
        return 2

    by_app = {app: declared_styles(app) for app in apps()}
    by_app = {app: sheets for app, sheets in by_app.items() if sheets}
    shared = shared_sheets(by_app)

    victim: tuple[Path, str, str, str] | None = None
    if args.prove_failure:
        if not shared:
            print(
                "error: no sheet is declared by two apps, so there is nothing for href "
                "dedupe to hide and this check has no subject",
                file=sys.stderr,
            )
            return 2
        _, owners = sorted(shared.items())[0]
        app = sorted(owners)[0]
        declared = next(s for s in by_app[app] if s.endswith(sorted(shared)[0].rsplit("/", 1)[-1]))
        source = APPS_DIR / app / "app.mjs"
        original = source.read_text(encoding="utf-8")
        broken = declared.replace(".css", "-nonesuch.css")
        if f"'{declared}'" not in original:
            print(f"error: could not find {declared!r} in {source}", file=sys.stderr)
            return 2
        source.write_text(original.replace(f"'{declared}'", f"'{broken}'", 1), encoding="utf-8")
        victim = (source, original, app, declared)
        by_app[app] = [broken if s == declared else s for s in by_app[app]]
        print(f"proving failure: misspelled {declared} in {app}/app.mjs")

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{args.port}"
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(base, timeout=1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        else:
            print(f"error: nothing answered on {base}", file=sys.stderr)
            return 2
        problems = audit(base, by_app)
    except CDPError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        server.terminate()
        if victim is not None:
            path, original, _, _ = victim
            path.write_text(original, encoding="utf-8")

    print(f"{len(by_app)} apps checked in isolation; {len(shared)} sheets are shared:")
    for sheet, owners in sorted(shared.items()):
        print(f"  {sheet} — {', '.join(sorted(owners))}")

    if args.prove_failure:
        # The removal makes the app borrow the sheet from the other declarer, which is
        # exactly the defect. It must be named, and named against the app that lost it.
        app = victim[2] if victim else ""
        problems = [line for line in problems if line.startswith(f"{app}:")]
        if problems:
            print("\nthe planted omission was caught:")
            for line in problems:
                print(f"  {line}")
            return 0
        print(
            "\nthe planted omission was NOT caught, so this check reads nothing",
            file=sys.stderr,
        )
        return 1

    if problems:
        print(f"\n{len(problems)} apps borrow a sheet they do not declare:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("\nevery app links what it declares when opened alone")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
