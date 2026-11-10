#!/usr/bin/env python3
"""Index the repository so the filesystem can mount it.

    scripts/mount.py                 write shared/fs/mount.json from the files on disk
    scripts/mount.py --check         exit 1 if the index disagrees with the disk

A browser cannot list a directory over static HTTP. `scripts/apps.py` exists for that reason
and answers it for one directory; this answers it for the whole tree, so `/usr/src/webxp` in
the terminal is the repository rather than a story about it.

## Why an index and not the contents

Shipping the contents would mean paying the whole repository at boot to make `cat` work, so
the index carries sizes and the bodies are fetched on first read. The figures are printed by
`--check`, which is where they should be read from rather than from this comment.

## What may be indexed

Tracked files only. See `tracked()` — the reason is a privacy rule in `.gitignore`, not
tidiness, and there is deliberately no fallback to walking the working tree.

## The schema

    {
      "version": 1,
      "root": "/usr/src/webxp",
      "files": [[path, size, mtimeSeconds, isText], ...]
    }

`path` is relative to the repository root and is also the URL to fetch, so one string is both
the name and the address. Directories are not listed: they are implied by the paths, which
keeps one fact in one place. `isText` is decided here, by extension, because the filesystem
holds text and a `.png` is a file it can describe and cannot read — refusing it before the
fetch costs nothing and says why.

**`size` is a character count for text, and a byte count for everything else.** The
filesystem it feeds measures files in characters — a JS string is the contents — so an index
in bytes made `ls -l` print one number before a file was read and a different one after, for
93 of this repository's 124 text files. Counting characters here costs one read per text file
in a generator that already walks the tree, and makes the unit one thing. A non-text file is
never read, so its byte size is the only figure available and the only one anybody sees.

Sorted, and with no timestamp of its own, so regenerating on an unchanged tree produces no
diff.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "shared" / "fs" / "mount.json"

#: Where the tree is grafted. The repository is read-only inside the filesystem, so it sits
#: under `/usr/src`, which is where a Unix keeps source it did not write.
MOUNT_ROOT = "/usr/src/webxp"

SCHEMA_VERSION = 1

#: Extensions the filesystem can actually hold. Everything else is indexed — it is a real
#: file with a real size — and refuses to be read.
TEXT_SUFFIXES = {
    ".mjs", ".js", ".css", ".md", ".html", ".htm", ".py", ".json", ".jsonl", ".svg",
    ".txt", ".csv", ".tsv", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".tex",
    ".geojson", ".map", ".lock", "",
}


def tracked() -> list[str]:
    """The repository's tracked files, from git.

    **Not a walk of the working tree, and there is no fallback to one.** `.gitignore` excludes
    `apps/day-*/private/` because it holds real Strava traces — a record of where someone
    lives and when they are not home — and a walk would mount them into a filesystem with a
    browsable `ls`. The directory is absent from some worktrees and present in others, so a
    walk produces a clean index on one machine and publishes the traces from another; that is
    the worst possible failure mode, because the clean run is the one you would trust.

    Tracked-only has one consequence worth knowing: a file you have created but not yet
    `git add`ed is not in the filesystem until you do. That is the right default for the same
    reason — the index can only ever contain what the repository already publishes.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"") or b""
        raise SystemExit(
            "scripts/mount.py needs git to decide what may be published: "
            f"{detail.decode(errors='replace').strip() or error}\n"
            "  Refusing to walk the working tree instead — see the privacy rule in "
            ".gitignore."
        )
    return [name for name in result.stdout.decode().split("\0") if name]


def indexed() -> list[list]:
    """Every tracked file, as index rows, sorted by path."""
    rows = []
    for name in tracked():
        path = REPO_ROOT / name
        if path == INDEX:
            # The index cannot describe itself: writing it changes its own size, so `--check`
            # would be stale the moment it passed.
            continue
        if not path.is_file():
            # Tracked and not on disk: a deletion that has not been committed. Named rather
            # than skipped silently, because the index would otherwise describe a file the
            # server will 404.
            print(f"  skipped {name} — tracked but not on disk", file=sys.stderr)
            continue
        stat = path.stat()
        is_text = path.suffix.lower() in TEXT_SUFFIXES
        size = stat.st_size
        if is_text:
            try:
                # Characters, not bytes — see the schema note above.
                #
                # Decoded from the bytes rather than read with `read_text`, which applies
                # universal-newline translation and collapses every CRLF to one character.
                # `response.text()` in the browser does not, so `read_text` undercounted
                # `apps/day-02-lines/fixture/activities.csv` by exactly its 7 CRLFs — and the
                # mismatch would have reappeared as a size that changes when the file is read,
                # which is the defect this whole column is fixing.
                size = len(path.read_bytes().decode("utf-8"))
            except UnicodeDecodeError:
                # A file with a text extension that is not valid UTF-8 cannot be held by a
                # filesystem of strings. Indexed as unreadable rather than as a character
                # count nobody can reproduce.
                print(f"  {name} is not valid UTF-8 — indexed as unreadable", file=sys.stderr)
                is_text = False
        rows.append([name, size, int(stat.st_mtime), 1 if is_text else 0])
    rows.sort(key=lambda row: row[0])
    return rows


def render(rows: list[list]) -> str:
    """The index, one file per line.

    One row per line rather than `json.dumps(indent=2)`: the rows are the payload, and a diff
    that shows one changed line per changed file is readable where a reformatted 2,000-line
    document is not.
    """
    body = ",\n".join(f"  {json.dumps(row, separators=(',', ' '))}" for row in rows)
    return (
        "{\n"
        f'"version": {SCHEMA_VERSION},\n'
        f'"root": {json.dumps(MOUNT_ROOT)},\n'
        '"files": [\n'
        f"{body}\n"
        "]}\n"
    )


#: The filesystem's own per-file ceiling, from `MAX_FILE_SIZE` in `shared/fs/vfs.mjs`.
#: Duplicated rather than imported — this is Python and that is JavaScript — and duplicated
#: deliberately: a summary that computed the ceiling from the code it describes could not
#: catch the ceiling changing. `check.py` duplicates `MIN_W` for the same reason.
READ_CEILING = 256 * 1024


def summarise(rows: list[list]) -> str:
    total = sum(row[1] for row in rows)
    # Readable means a reader can actually `cat` it: text, and under the ceiling. Summing
    # every text row called 1.5 MB of vendored MapLibre "readable" when both files are
    # refused with EFBIG before they are even fetched — a 58% overstatement in the figure
    # `shared/fs/README.md` points at as authoritative.
    readable = sum(row[1] for row in rows if row[3] and row[1] <= READ_CEILING)
    refused = sum(1 for row in rows if not row[3] or row[1] > READ_CEILING)
    return (
        f"{len(rows)} files, {total / 1_048_576:.1f} MB, "
        f"of which {readable / 1024:.0f} KB is readable text "
        f"({refused} refused: not text, or over the {READ_CEILING // 1024} KB ceiling)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the index disagrees with the files on disk",
    )
    args = parser.parse_args(argv)

    rows = indexed()

    if args.check:
        if not INDEX.is_file():
            print(f"{INDEX.relative_to(REPO_ROOT)} does not exist.", file=sys.stderr)
            print("  run scripts/mount.py to write it.", file=sys.stderr)
            return 1
        try:
            current = json.loads(INDEX.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            print(f"{INDEX.relative_to(REPO_ROOT)} is not JSON: {error}", file=sys.stderr)
            return 1
        if current.get("version") != SCHEMA_VERSION:
            print(
                f"{INDEX.relative_to(REPO_ROOT)} is version {current.get('version')}, "
                f"this script writes {SCHEMA_VERSION}.",
                file=sys.stderr,
            )
            return 1
        listed = [list(row) for row in current.get("files", [])]
        was = {row[0] for row in listed}
        now = {row[0] for row in rows}

        # Staleness is about the *set of paths*, not about sizes and dates.
        #
        # The index exists to answer the one question a browser cannot — what files are
        # there — and that answer is wrong only when a file is added or removed. Sizes and
        # mtimes are a convenience that degrades gracefully: a stale size makes `ls -l` show
        # the figure from the last index, and contents are always fetched fresh, so nothing
        # reads wrong.
        #
        # Comparing them would make this fail after editing any tracked file, which is every
        # commit. A check that is red for reasons the reader did not cause is a check the
        # reader learns to regenerate past without looking — and then a genuinely missing app
        # slips through in the same shrug.
        if was != now:
            print(f"{INDEX.relative_to(REPO_ROOT)} is stale.", file=sys.stderr)
            # Named rather than counted: "3 files differ" sends the reader back to the diff,
            # and the names are the diff.
            for path in sorted(now - was)[:10]:
                print(f"  tracked but not indexed: {path}", file=sys.stderr)
            for path in sorted(was - now)[:10]:
                print(f"  indexed but no longer tracked: {path}", file=sys.stderr)
            print("  run scripts/mount.py to regenerate.", file=sys.stderr)
            return 1

        by_path = {row[0]: row for row in listed}
        drifted = [row[0] for row in rows if by_path[row[0]][1:] != row[1:]]
        note = (
            f" ({len(drifted)} file{'' if len(drifted) == 1 else 's'} changed since indexing; "
            "regenerate to refresh the sizes `ls -l` shows)"
            if drifted else ""
        )
        print(f"{INDEX.relative_to(REPO_ROOT)} is current — {summarise(rows)}{note}")
        return 0

    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(render(rows), encoding="utf-8")
    print(f"wrote {INDEX.relative_to(REPO_ROOT)} — {summarise(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
