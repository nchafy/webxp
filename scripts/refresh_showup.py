#!/usr/bin/env python3
"""Refresh `data/showup/` from the ETL that generates it.

The Show Up NYC programs read eight JSON artifacts. This repository does not build them:
they are scraped from the City's open data by `council_access_nyc`, which is a headless tool
with its own fetch cache, floors and tests. Those artifacts are committed here so that this
stays a site with no build step, which means refreshing them is a copy — and a copy is worth
one script rather than eight `cp` lines, because the failure modes are quiet.

    scripts/refresh_showup.py                     copy from the default ETL checkout
    scripts/refresh_showup.py --from ../elsewhere  copy from another one
    scripts/refresh_showup.py --check              exit 1 if what is here differs

What this refuses to do is the point. A partial refresh is worse than a stale one: the
programs cross-reference these files — a board in `lookup.json` with no row in `preview.json`
shows a reader an empty panel — so either all eight land or none do. The copy is staged and
swapped, and a file that shrank sharply stops it, because the usual way an ETL fails is a
source that answered with less rather than one that threw.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEST = REPO_ROOT / "data" / "showup"

#: Where the ETL is expected to be, and where in it the artifacts land. Overridable, because
#: nothing here should assume one machine's layout.
DEFAULT_SOURCE = Path("~/personal/council_access_nyc").expanduser()
SOURCE_SITE = "site"

#: Artifact -> the floor, in bytes, below which the copy is refused.
#:
#: These are not size budgets; they are "the source answered with almost nothing" detectors,
#: set well under the measured size of each file so that ordinary growth and shrinkage pass.
#: Measured 2026-11-09: board-meetings 243 KB, boards.geo 411 KB, districts.geo 153 KB,
#: lookup 28 KB, preview 120 KB, references 6.3 KB, zips.geo 259 KB, manifest 870 B.
FLOORS = {
    "board-meetings.json": 80_000,
    "boards.geo.json": 150_000,
    "districts.geo.json": 60_000,
    "lookup.json": 10_000,
    "preview.json": 40_000,
    "references.json": 2_000,
    "zips.geo.json": 80_000,
    "manifest.json": 300,
}

#: `manifest.json` sits at the root of the ETL's output and the rest under `data/`.
AT_SITE_ROOT = {"manifest.json"}


class RefreshError(RuntimeError):
    """The refresh was refused, with the reason."""


class NotBuiltError(RefreshError):
    """No artifact is there at all, which is an unbuilt ETL rather than a broken one.

    Distinguished because the two want different answers. "You have not run the build" is a
    note; "the build produced a 2-byte lookup.json" is a failure. Collapsing them meant a
    caller that merely had no ETL checkout printed six lines of alarm.
    """


def source_path(source_root: Path, name: str) -> Path:
    site = source_root / SOURCE_SITE
    return site / name if name in AT_SITE_ROOT else site / "data" / name


def collect(source_root: Path) -> dict[str, bytes]:
    """Read all eight artifacts, or raise naming every one that is wrong.

    Every problem at once rather than the first: a reader who has just run the ETL wants to
    know whether one source failed or the whole run did.
    """
    problems: list[str] = []
    payloads: dict[str, bytes] = {}
    for name, floor in FLOORS.items():
        path = source_path(source_root, name)
        if not path.is_file():
            problems.append(f"{name}: not at {path}")
            continue
        payload = path.read_bytes()
        if len(payload) < floor:
            problems.append(
                f"{name}: {len(payload)} bytes, under the {floor} floor — the ETL probably "
                "ran against a source that answered with almost nothing"
            )
            continue
        try:
            json.loads(payload)
        except json.JSONDecodeError as error:
            problems.append(f"{name}: not valid JSON ({error})")
            continue
        payloads[name] = payload

    if problems and not payloads:
        raise NotBuiltError(
            f"no artifacts under {source_root / SOURCE_SITE} — run the ETL's build first"
        )
    if problems:
        raise RefreshError(
            "refusing to refresh; nothing was written:\n  " + "\n  ".join(problems)
        )
    return payloads


def write(payloads: dict[str, bytes]) -> list[str]:
    """Stage the whole set, then swap it in. Return the names that changed.

    Staged because a half-written `data/showup/` is a desktop whose programs disagree with
    each other, and that state is reachable by any interruption mid-copy.
    """
    DEST.mkdir(parents=True, exist_ok=True)
    changed = [
        name
        for name, payload in payloads.items()
        if not (DEST / name).is_file() or (DEST / name).read_bytes() != payload
    ]
    staging = Path(tempfile.mkdtemp(prefix="showup-data-"))
    try:
        for name, payload in payloads.items():
            (staging / name).write_bytes(payload)
        for name in payloads:
            shutil.move(str(staging / name), DEST / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from",
        dest="source",
        default=str(DEFAULT_SOURCE),
        help=f"the ETL checkout to copy from (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare only; exit 1 if the committed data differs from the ETL's output",
    )
    args = parser.parse_args(argv)

    source_root = Path(args.source).expanduser().resolve()
    if not (source_root / SOURCE_SITE).is_dir():
        print(
            f"error: no built output at {source_root / SOURCE_SITE}.\n"
            f"       Run the ETL first: cd {source_root} && make build",
            file=sys.stderr,
        )
        return 2

    try:
        payloads = collect(source_root)
    except NotBuiltError as error:
        # 2, the same status as "no checkout here": both mean there is nothing to compare
        # against, which a caller may legitimately ignore.
        print(f"error: {error}", file=sys.stderr)
        return 2
    except RefreshError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.check:
        stale = [
            name
            for name, payload in payloads.items()
            if not (DEST / name).is_file() or (DEST / name).read_bytes() != payload
        ]
        if stale:
            print(f"data/showup/ is behind the ETL in {len(stale)} files: {sorted(stale)}")
            return 1
        print(f"data/showup/ matches the ETL: {len(payloads)} artifacts identical")
        return 0

    changed = write(payloads)
    total = sum(len(payload) for payload in payloads.values())
    if changed:
        print(f"refreshed data/showup/ from {source_root}")
        for name in sorted(changed):
            print(f"  {name}")
        print(f"{len(payloads)} artifacts, {total:,} bytes")
    else:
        print(f"data/showup/ already matches {source_root}: {len(payloads)} artifacts")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
