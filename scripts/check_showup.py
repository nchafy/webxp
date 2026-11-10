#!/usr/bin/env python3
"""The seam between this repository and the ETL that feeds it, asserted.

The Show Up NYC programs live here and the data they read is generated in
`council_access_nyc`. Two repositories, two owners, no shared build — which is a seam, and
a seam nobody checks is one that is found by a reader seeing an empty window.

This is deliberately not a browser test. It answers the two questions that can be answered
without one, and both of them are the quiet kind of wrong:

1. Does `apps/manifest.mjs` still list the programs that exist? A browser cannot list a
   directory over static HTTP, so a program absent from the manifest is simply not there,
   with nothing logged.
2. Does every artifact the programs fetch exist, parse, and carry what they index by? A
   board in `lookup.json` with no row in `preview.json` shows a reader an empty panel.

    scripts/check_showup.py          exit 1 on any disagreement

What it deliberately does not check is whether the shell API these programs call still
behaves — `createBaseMap`'s root-scoped controls, `widgets.group`'s cell handling. That needs
a browser, and `scripts/check.py` is where it belongs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = REPO_ROOT / "apps"
DATA_DIR = REPO_ROOT / "data" / "showup"

#: The programs this seam covers. Named rather than globbed: a program that stops existing
#: should fail here, and a glob would simply find one fewer.
PROGRAMS = ("showup", "showup-boards", "showup-councils", "showup-map", "showup-readme")

#: Artifact -> the keys its readers index by. Each is a path into the parsed JSON, so a
#: shape change upstream fails here rather than in a window.
EXPECTED_SHAPE: dict[str, tuple[str, ...]] = {
    "lookup.json": ("districts", "boards"),
    "preview.json": ("districts", "boards", "as_of"),
    "board-meetings.json": ("boards", "generated_at"),
    "references.json": ("datasets", "derived", "cited", "caveats"),
    "manifest.json": ("built_at", "sources"),
    "districts.geo.json": ("features",),
    "boards.geo.json": ("features",),
    "zips.geo.json": ("features",),
}

#: What the City publishes, and therefore what a complete artifact holds. A short file is the
#: usual way this breaks: a source that answered with less rather than one that threw.
DISTRICT_COUNT = 51
BOARD_COUNT = 59

#: Counts keyed by artifact and the key inside it.
EXPECTED_COUNTS: tuple[tuple[str, str, int], ...] = (
    ("lookup.json", "districts", DISTRICT_COUNT),
    ("lookup.json", "boards", BOARD_COUNT),
    ("preview.json", "districts", DISTRICT_COUNT),
    ("preview.json", "boards", BOARD_COUNT),
    ("board-meetings.json", "boards", BOARD_COUNT),
    ("districts.geo.json", "features", DISTRICT_COUNT),
    ("boards.geo.json", "features", BOARD_COUNT),
)


def manifest_problems() -> list[str]:
    """Whether the generated app index still agrees with the folders on disk."""
    problems = []
    for name in PROGRAMS:
        if not (APPS_DIR / name / "app.mjs").is_file():
            problems.append(f"apps/{name}/app.mjs is missing, so the program is gone")

    manifest = APPS_DIR / "manifest.mjs"
    if not manifest.is_file():
        return [*problems, "apps/manifest.mjs is missing, so no program loads at all"]

    listed = set(re.findall(r"'([a-z][a-z0-9-]*)'", manifest.read_text(encoding="utf-8")))
    for name in PROGRAMS:
        if name not in listed:
            problems.append(
                f"{name} is not in apps/manifest.mjs — a browser cannot list a directory, "
                "so it is simply absent, with nothing logged. Run scripts/apps.py."
            )
    return problems


def data_problems() -> list[str]:
    """Whether every artifact exists, parses, and carries what the programs index by."""
    problems = []
    parsed: dict[str, object] = {}
    for name, keys in EXPECTED_SHAPE.items():
        path = DATA_DIR / name
        if not path.is_file():
            problems.append(f"data/showup/{name} is missing; run scripts/refresh_showup.py")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            problems.append(f"data/showup/{name} is not valid JSON ({error})")
            continue
        parsed[name] = payload
        if not isinstance(payload, dict):
            problems.append(f"data/showup/{name} is not an object")
            continue
        for key in keys:
            if key not in payload:
                problems.append(f"data/showup/{name} has no {key!r}, which its reader indexes by")

    for name, key, expected in EXPECTED_COUNTS:
        payload = parsed.get(name)
        if not isinstance(payload, dict) or key not in payload:
            continue  # already reported above
        found = len(payload[key])
        if found != expected:
            problems.append(
                f"data/showup/{name} carries {found} {key}, expected {expected}. A short "
                "artifact draws a map with holes in it rather than failing."
            )
    return problems


def staleness_problems() -> list[str]:
    """Whether the committed data still matches the ETL, when the ETL is reachable.

    Reported, never failed. A clone of this repository is not expected to carry the ETL, and
    treating "cannot compare" as "stale" would make this unrunnable everywhere else.
    """
    refresh = Path(__file__).with_name("refresh_showup.py")
    if not refresh.is_file():
        return []
    result = subprocess.run(
        [sys.executable, str(refresh), "--check"], capture_output=True, text=True, check=False
    )
    if result.returncode == 2:
        return []  # no ETL checkout here, which is normal
    if result.returncode != 0:
        return [f"(reported, not failed) {result.stdout.strip() or result.stderr.strip()}"]
    return []


def main() -> int:
    problems = manifest_problems() + data_problems()
    notes = staleness_problems()

    for note in notes:
        print(note)
    if problems:
        print(f"showup seam BROKEN: {len(problems)} problems", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(
        f"showup seam OK: {len(PROGRAMS)} programs in the manifest, "
        f"{len(EXPECTED_SHAPE)} artifacts with {DISTRICT_COUNT} districts and "
        f"{BOARD_COUNT} boards"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
