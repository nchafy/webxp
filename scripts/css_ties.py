#!/usr/bin/env python3
"""Pin the specificity decisions the stylesheets rely on.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is a **regression guard over an explicit list of already-resolved ties**. It is
**not a tie detector.** It cannot tell you that a new tie exists. It can only tell you
that the decisions listed in `OUTRANKS`, `DISJOINT` and `GONE` below are still decided the
way `docs/apps.md` says they are.

The distinction matters, because the two halves of the list are pinned differently:

* `DISJOINT` pairs are checked **as ties**: both specificities are computed and the pair is
  asserted to share no property. Reintroduce the overlap and this names the property. It
  would also catch a *newly* introduced overlap between those two selectors.
* `OUTRANKS` and `GONE` entries are pinned as **"this selector exists and sets these
  properties"** — `.map-root .map-area > .map` over `.map-root .maplibregl-map`,
  `.clippy .clippy-bubble` over `.card`, `.window.is-max:not(.is-active)` over
  `.window.is-max`, and in `GONE` the spellings those replaced — `.map-root > .map`,
  `.clippy-bubble`. Delete the fix and the assertion fails because the fix is gone — not
  because a tie was detected. **A brand-new tie of the same shape, anywhere else in the
  tree, is not caught by this file.**

The missing piece for the general case is ancestor reachability: deciding whether one
element can match two selectors requires knowing which class sets co-occur *and* which
ancestor chains are possible. Without it a generic sweep over every stylesheet here reports
a few hundred candidate ties, nearly all of them pairs of rules that can never meet on one
element. That is not a gate, and nothing here pretends to be one.

What it does read is every stylesheet in the tree: `sheets()` discovers them, so a sheet
cannot be present on disk and absent from this gate.

AN ABSENT SELECTOR IS A FAILURE, NEVER A SKIP
---------------------------------------------
`_rule()` records a miss and the run fails. This is deliberate and it is the defect class
`docs/review.md` names under "a check that cannot fail": a guard on a spelling, where a
renamed class turned an assertion into a branch that quietly did nothing and printed ok.
Every selector named below is a spelling. If one is renamed, this file must say so rather
than assert nothing about it.

PROVING IT CAN FAIL
-------------------
`check(invert=True)` applies `REVERTS` to the stylesheet text in memory — never to disk —
and runs the same assertions against the undone tree. Every pinned case is expected to fail.
`scripts/check.py` treats an inverted check that returns nothing as a failure of the control
itself.

The two modes cover each other. A revert whose `old` text no longer matches means the file
moved under the revert table; that same edit makes the corresponding assertion fail in
normal mode, so the staleness surfaces either way rather than silently weakening the
control.

Standalone:  python3 -I scripts/css_ties.py [--prove-failure]
As a check:  ("css ties", check) in CHECKS, and `check` in INVERTIBLE.
             It takes no `page` and touches no network, port or browser.
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class Unreadable(RuntimeError):
    """Nothing was there to assert about. Never a skip; see the module docstring."""


def sheets(root: Path) -> tuple[str, ...]:
    """Every stylesheet in the tree, discovered. Sorted, so a run is reproducible.

    Discovered and not listed, because a list is a second place a sheet has to be written
    down: the sheet lands, the list does not grow, and this gate reports ok about a file it
    has never opened. The assertions below name their own selectors and do not care which
    file holds one, so there is nothing a list could add here.

    Dot-directories are skipped and that exclusion is load-bearing: `.claude/worktrees/`
    holds whole copies of this tree on other branches, so a `GONE` selector still live in one
    of them would fail this gate about a file nothing links. `.venv/` holds installed
    packages nobody here wrote.

    An empty result is a broken sweep, not a tree without stylesheets, and raises.
    """
    found = tuple(
        sorted(
            rel.as_posix()
            for rel in (path.relative_to(root) for path in root.rglob("*.css"))
            if not any(part.startswith(".") for part in rel.parts)
        )
    )
    if not found:
        raise Unreadable(f"no stylesheet found under {root} — the sweep is reading nothing")
    return found


# ---------------------------------------------------------------------------
# A CSS parser and a specificity calculator, enough for the sheets in this tree.
# ---------------------------------------------------------------------------

CLS = re.compile(r"\.[-\w]+")
IDS = re.compile(r"#[-\w]+")
ATTR = re.compile(r"\[[^\]]*\]")
PSEUDO_EL = re.compile(r"::[-\w]+(\([^)]*\))?")
PSEUDO_CLS = re.compile(r":(?!:)([-\w]+)(\([^)]*\))?")


def strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def parse(text: str) -> list[tuple[str, dict[str, str]]]:
    """Flatten a stylesheet to (selector, declarations) pairs.

    At-rule preludes are dropped and their bodies are flattened in with everything else:
    every selector this file names sits either at the top level or inside a `@container` /
    `@media` block, and no assertion below compares across that boundary.

    Declarations are split on `;`, which a semicolon inside a `url()` would break. No sheet
    in this tree contains one; `check()` asserts the parse is not empty, which is what would
    catch a file this parser could not read.
    """
    src = strip_comments(text)
    out: list[tuple[str, dict[str, str]]] = []
    buf = ""
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "{":
            prelude, buf = buf.strip(), ""
            if prelude.startswith("@"):
                i += 1
                continue
            depth, j = 1, i + 1
            while j < n and depth:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                j += 1
            decls: dict[str, str] = {}
            for decl in src[i + 1 : j - 1].split(";"):
                if ":" not in decl:
                    continue
                prop, _, value = decl.partition(":")
                prop = prop.strip().lower()
                if prop and not prop.startswith("--"):
                    decls[prop] = value.strip()
            for sel in prelude.split(","):
                if sel.strip():
                    out.append((sel.strip(), decls))
            i = j
            continue
        if ch == "}":
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    return out


def _count(sel: str) -> tuple[int, int, int]:
    """(ids, class column, type column), per CSS Selectors 4.

    A functional pseudo-class contributes its *argument*, not itself: `:not(.is-active)` is
    one class, and `:where(...)` is nothing at all. Counting the name as well as the argument
    is the arithmetic bug that makes a three-class selector look like four, which is the
    error this whole file exists to catch in stylesheets.
    """
    sel = PSEUDO_EL.sub(" @pel@ ", sel)
    ids = classes = types = 0
    while True:
        match = PSEUDO_CLS.search(sel)
        if match is None:
            break
        name, arg = match.group(1), match.group(2)
        if arg and name in ("not", "is", "has", "where"):
            if name != "where":
                best = (0, 0, 0)
                for alternative in arg[1:-1].split(","):
                    best = max(best, _count(alternative))
                ids += best[0]
                classes += best[1]
                types += best[2]
        else:
            classes += 1
        sel = sel[: match.start()] + " " + sel[match.end() :]
    classes += len(ATTR.findall(sel))
    sel = ATTR.sub(" ", sel)
    ids += len(IDS.findall(sel))
    sel = IDS.sub(" ", sel)
    classes += len(CLS.findall(sel))
    sel = CLS.sub(" ", sel)
    types += len(re.findall(r"\b[a-zA-Z][-\w]*\b", sel.replace("@pel@", "x")))
    return ids, classes, types


def specificity(sel: str) -> tuple[int, int, int]:
    return _count(sel)


#: Hand-computed specificities. The calculator above is what every other assertion in this
#: file rests on, so it is checked against arithmetic done by a person before it is used to
#: check anything else. The last three reproduce the three rows of `docs/apps.md`'s own
#: table, which is an independent source for the same numbers.
SELF_TEST = {
    ".card": (0, 1, 0),
    ".card p": (0, 1, 1),
    ".title-box.close": (0, 2, 0),
    "a:focus-visible": (0, 1, 1),
    "button:active": (0, 1, 1),
    "button:focus-visible": (0, 1, 1),
    ".icon-tile:active": (0, 2, 0),
    ".desktop-icon:hover": (0, 2, 0),
    ".clippy .clippy-bubble": (0, 2, 0),
    '.task-button[aria-pressed="true"]': (0, 2, 0),
    '.task-button[aria-pressed="true"]:hover': (0, 3, 0),
    ".window:not(.is-active) .title-box": (0, 3, 0),
    ".window.is-max:not(.is-active)": (0, 3, 0),
    ".window > .window-body.is-loading": (0, 3, 0),
    ".window-body.app-terminal": (0, 2, 0),
    ".app-day-02-lines .key i": (0, 2, 1),
    ".app-day-02-lines .key.noise-legend i": (0, 3, 1),
    ".app-day-02-lines .pass-row:not(:focus-visible)": (0, 3, 0),
    ".group-table tbody tr + tr th": (0, 1, 4),
    '.segment button[aria-pressed="true"]': (0, 2, 1),
    ".map-root .map-area > .map": (0, 3, 0),
    ".map-root .maplibregl-map": (0, 2, 0),
    ".map-root .map-zoom button": (0, 2, 1),
}


# ---------------------------------------------------------------------------
# The pinned decisions.
# ---------------------------------------------------------------------------

#: (winner, loser, property, label). The winner must set the property and must outrank the
#: loser on specificity, so the outcome does not depend on which file was linked first.
OUTRANKS = (
    (
        ".app-day-02-lines .key.noise-legend i",
        ".app-day-02-lines .key i",
        "width",
        "noise legend swatch width",
    ),
    (".clippy .clippy-bubble", ".card", "background", "clippy bubble over .card"),
    (".clippy .clippy-bubble", ".card", "border", "clippy bubble over .card"),
    (".clippy .clippy-bubble", ".card", "box-shadow", "clippy bubble over .card"),
    (".clippy-bubble .clippy-says", ".card p", "margin", "clippy line margin"),
    (
        '.task-button[aria-pressed="true"]:hover',
        '.task-button[aria-pressed="true"]',
        "background",
        "taskbar button pressed+hover",
    ),
    (
        '.task-button[aria-pressed="true"]:hover',
        ".task-button:hover",
        "background",
        "taskbar button pressed+hover",
    ),
    (
        ".app-day-02-lines .pass-row:not(:focus-visible)",
        "button:focus-visible",
        "box-shadow",
        "pass-row keeps the focus halo",
    ),
    (
        ".window.is-max:not(.is-active)",
        ".window.is-max",
        "box-shadow",
        "maximised inactive window shadow",
    ),
    (
        ".window.is-max:not(.is-active)",
        ".window:not(.is-active)",
        "box-shadow",
        "maximised inactive window shadow",
    ),
    (
        ".window > .window-body.is-loading",
        ".window-body.app-terminal",
        "background",
        "holding colour beats an app's pane",
    ),
    # The original defect, and the most expensive one in the project's history: MapLibre puts
    # `maplibregl-map` on the same element and the vendored block sits last, so an equal count
    # collapses the container to 0 px high while every MapLibre flag reads healthy.
    (
        ".map-root .map-area > .map",
        ".map-root .maplibregl-map",
        "position",
        "map container stays absolute",
    ),
    (
        ".map-root .map-zoom .map-compass",
        ".map-root .map-zoom button",
        "width",
        "compass is not sized by the zoom button",
    ),
    (
        ".map-root .map-zoom .map-compass",
        ".map-root .map-zoom button",
        "height",
        "compass is not sized by the zoom button",
    ),
)

#: (a, b, label). These pairs tie on purpose and must therefore share no property at all, so
#: the tie decides nothing. Raising either one would change a different outcome: lifting
#: `.icon-tile:active` above `.desktop-icon:hover` deletes the hover wash on a pressed icon.
#: The `background` shorthand is what makes this subtle — it reaches `background-color` and
#: `background-image` both, so one side using it is enough to recreate the overlap.
DISJOINT = (
    (".icon-tile:active", ".desktop-icon:hover", "desktop icon pressed vs hovered"),
    (".icon-tile:active", ".desktop-icon:focus-visible", "desktop icon pressed vs focused"),
    (".start-item:active", ".start-item:hover", "start item pressed vs hovered"),
    (".start-item:active", ".start-item:focus-visible", "start item pressed vs focused"),
)

#: Shorthands and the longhands they reach, for the `DISJOINT` overlap test.
REACHES = {
    "background": ("background-color", "background-image"),
    "background-color": ("background",),
    "background-image": ("background",),
    "border": ("border-color", "border-width", "border-style"),
    "border-color": ("border",),
    "margin": ("margin-top", "margin-right", "margin-bottom", "margin-left"),
    "padding": ("padding-top", "padding-right", "padding-bottom", "padding-left"),
    "outline": ("outline-color", "outline-width", "outline-style"),
}

#: Selectors that must not exist. Dead rules, hand-synced duplicates, and the pre-fix
#: spelling of every selector `OUTRANKS` names — so a fix that is reverted by rewriting the
#: selector rather than deleting it is caught from both ends.
GONE = (
    # Dead: no construction site builds a `.btn`, and the repo holds one `<input>` and no
    # `<select>`. Named one selector at a time, never as a list: `parse()` splits a selector
    # list into its members, so an entry like `button, .btn` could never match and would be a
    # pin that cannot fail.
    ".btn",
    ".btn:active",
    'input[type="search"]',
    'input[type="number"]',
    "select",
    "select:focus-visible",
    # Deleted duplicates: `.segment` in theme.css is the only definition of the segmented
    # row, and `a { color: var(--accent) }` the only definition of a link colour.
    ".map-root .layer-switch",
    ".map-root .map-attrib a",
    # Four MapLibre corner containers that match and are permanently empty.
    ".map-root .maplibregl-ctrl-bottom-left",
    ".map-root .maplibregl-ctrl-bottom-right",
    ".map-root .maplibregl-ctrl-top-left",
    ".map-root .maplibregl-ctrl-top-right",
    # Pre-fix spellings. Each is a count that lost: `docs/apps.md` carries what the reader
    # saw for every one.
    ".map-root > .map",
    ".map-root .map-compass",
    # The pre-`shared/` spellings. Scoping the map rules under the app class is what flattened
    # the count in the first place, and the stale header that told you to do it has been
    # deleted — so these three are pinned as never-again rather than as history.
    ".app-basemap .basemap-root > .map",
    ".app-basemap .map",
    ".app-basemap .maplibregl-map",
    ".app-day-02-lines .noise-legend i",
    ".clippy-bubble",
    ".clippy-says",
    ".window-body.is-loading",
)

#: (sheet, find, replace, label) — undoes one fix. Applied to the text in memory by
#: `check(invert=True)`, never written to disk.
REVERTS = (
    (
        "apps/day-02-lines/app.css",
        ".app-day-02-lines .key.noise-legend i",
        ".app-day-02-lines .noise-legend i",
        "noise legend swatch width",
    ),
    (
        "apps/day-02-lines/app.css",
        ".app-day-02-lines .pass-row:not(:focus-visible) { box-shadow: none; }",
        "",
        "pass-row keeps the focus halo",
    ),
    (
        "apps/day-02-lines/app.css",
        "  border-radius: 2px;\n  cursor: pointer;",
        "  border-radius: 2px;\n  box-shadow: none;\n  cursor: pointer;",
        "pass-row keeps the focus halo",
    ),
    ("demo/demo.css", ".clippy .clippy-bubble {", ".clippy-bubble {", "clippy bubble over .card"),
    (
        "demo/demo.css",
        ".clippy-bubble .clippy-says { margin:",
        ".clippy-says { margin:",
        "clippy line margin",
    ),
    (
        "src/shell.css",
        '.task-button[aria-pressed="true"]:hover {\n'
        "  background: linear-gradient(to bottom, #164497 0%, #2a66c4 100%);\n"
        "}\n",
        "",
        "taskbar button pressed+hover",
    ),
    (
        "src/shell.css",
        ".icon-tile:active { background-image: none;",
        ".icon-tile:active { background: none;",
        "desktop icon pressed vs hovered",
    ),
    (
        "src/shell.css",
        "  background-color: rgb(10 36 106 / 42%);",
        "  background: rgb(10 36 106 / 42%);",
        "desktop icon pressed vs hovered",
    ),
    (
        "src/shell.css",
        ".start-item:active { background-image: none;",
        ".start-item:active { background: none;",
        "start item pressed vs hovered",
    ),
    (
        "src/shell.css",
        ".start-item:focus-visible { background-color: var(--title-b);",
        ".start-item:focus-visible { background: var(--title-b);",
        "start item pressed vs hovered",
    ),
    (
        "src/window.css",
        ".window.is-max:not(.is-active) { box-shadow: none; }\n",
        "",
        "maximised inactive window shadow",
    ),
    (
        "src/window.css",
        ".window > .window-body.is-loading {",
        ".window-body.is-loading {",
        "holding colour beats an app's pane",
    ),
    (
        "src/theme.css",
        "button {\n  font: inherit;",
        "button, .btn {\n  font: inherit;",
        "dead .btn",
    ),
    (
        "src/theme.css",
        "button:active {",
        "button:active, .btn:active {",
        "dead .btn:active",
    ),
    (
        "src/theme.css",
        'input[type="text"], textarea {',
        'input[type="text"], input[type="search"], input[type="number"], select, textarea {',
        "dead input types",
    ),
    (
        "src/theme.css",
        "button:focus-visible,",
        "button:focus-visible,\nselect:focus-visible,",
        "dead select in the focus ring",
    ),
    (
        "shared/map/map.css",
        ".map-root .maplibregl-canvas { position: absolute; left: 0; top: 0; }",
        ".map-root .maplibregl-canvas { position: absolute; left: 0; top: 0; }\n"
        ".map-root .maplibregl-ctrl-bottom-left,\n"
        ".map-root .maplibregl-ctrl-bottom-right,\n"
        ".map-root .maplibregl-ctrl-top-left,\n"
        ".map-root .maplibregl-ctrl-top-right "
        "{ position: absolute; pointer-events: none; z-index: 2; }",
        "empty MapLibre corner containers",
    ),
    (
        "shared/map/map.css",
        ".map-root .map-area > .map {",
        ".map-root > .map {",
        "map container stays absolute",
    ),
    (
        "shared/map/map.css",
        ".map-root {\n",
        ".app-basemap .basemap-root > .map { position: absolute; }\n"
        ".app-basemap .map { position: absolute; }\n"
        ".app-basemap .maplibregl-map { position: relative; }\n"
        ".map-root {\n",
        "map rules re-scoped under the app class",
    ),
    (
        "shared/map/map.css",
        ".map-root .map-zoom .map-compass {",
        ".map-root .map-compass {",
        "compass is not sized by the zoom button",
    ),
    (
        "shared/map/map.css",
        ".map-root .tilt-row { align-items: center;",
        ".map-root .layer-switch { display: flex; flex-wrap: wrap; gap: 3px; }\n"
        ".map-root .map-attrib a { color: var(--accent); }\n"
        ".map-root .tilt-row { align-items: center;",
        "map.css duplicates of .segment and the link colour",
    ),
)


# ---------------------------------------------------------------------------
# The check.
# ---------------------------------------------------------------------------


def _load(root: Path) -> dict[str, str]:
    return {sheet: (root / sheet).read_text(encoding="utf-8") for sheet in sheets(root)}


def _apply_reverts(sources: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    out = dict(sources)
    stale = []
    for sheet, find, replace, label in REVERTS:
        if sheet not in out:
            stale.append(f"{sheet}: the sweep never read the sheet this revert names")
            continue
        if find not in out[sheet]:
            stale.append(f"{sheet}: revert for {label!r} no longer matches the file")
            continue
        out[sheet] = out[sheet].replace(find, replace, 1)
    return out, stale


def check(page=None, invert: bool = False) -> list[str]:
    """Return a list of failure messages; empty means the decisions still hold.

    `page` is accepted and ignored, so this slots into `scripts/check.py`'s `CHECKS` beside
    the browser checks without needing one.
    """
    del page

    bad: list[str] = []

    for sel, want in SELF_TEST.items():
        got = specificity(sel)
        if got != want:
            bad.append(f"specificity: {sel} computed {got}, hand-counted {want}")
    if bad:
        # Everything below is arithmetic on this function. Reporting ties computed by a
        # calculator known to be wrong would be worse than reporting nothing.
        return bad + ["specificity: calculator disagrees with the hand count; nothing else ran"]

    sources = _load(REPO_ROOT)
    control: list[str] = []
    if invert:
        sources, control = _apply_reverts(sources)

    index: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for sheet, text in sources.items():
        rules = parse(text)
        if not rules:
            bad.append(f"parse: {sheet} yielded no rules — the parser could not read it")
        for sel, decls in rules:
            index.setdefault(sel, []).append((sheet, decls))

    missing: list[str] = []

    def rule(sel: str) -> list[tuple[str, dict[str, str]]]:
        """An unknown selector is a failure. See the module docstring."""
        if sel not in index:
            missing.append(sel)
            return []
        return index[sel]

    def declared(sel: str, prop: str) -> str | None:
        for _sheet, decls in rule(sel):
            if prop in decls:
                return decls[prop]
        return None

    fired: list[str] = []

    for winner, loser, prop, label in OUTRANKS:
        if declared(winner, prop) is None:
            bad.append(f"{label}: {winner} does not set {prop}")
            continue
        if declared(loser, prop) is None:
            bad.append(f"{label}: {loser} does not set {prop}, so the pin asserts nothing")
            continue
        sw, sl = specificity(winner), specificity(loser)
        if sw <= sl:
            bad.append(f"{label}: {winner} {sw} does not outrank {loser} {sl} on {prop}")
        else:
            fired.append(f"{label}: {winner} > {loser}")

    for a, b, label in DISJOINT:
        da = {prop for _sheet, decls in rule(a) for prop in decls}
        db = {prop for _sheet, decls in rule(b) for prop in decls}
        if not da or not db:
            bad.append(f"{label}: {a} or {b} declares nothing, so the pin asserts nothing")
            continue
        overlap = set(da & db)
        for prop in da:
            overlap |= {reached for reached in REACHES.get(prop, ()) if reached in db}
        sa, sb = specificity(a), specificity(b)
        if sa != sb:
            bad.append(
                f"{label}: {a} {sa} and {b} {sb} no longer tie — this pin assumed they do"
            )
        elif overlap:
            bad.append(
                f"{label}: {a} and {b} both {sa} and share {sorted(overlap)}; "
                "source order decides it"
            )
        else:
            fired.append(f"{label}: {a} / {b} share nothing")

    for sel in GONE:
        if sel in index:
            bad.append(f"deleted rule is back: {sel} ({index[sel][0][0]})")
        else:
            fired.append(f"gone: {sel}")

    bad += [f"absent from the stylesheets: {sel}" for sel in sorted(set(missing))]

    if invert:
        # The control's own health. A revert that no longer matches, or a pin that survived
        # its fix being undone, is a hole in this file and has to be read as one even though
        # the returned list is non-empty either way.
        bad = [f"control: {message}" for message in control] + bad
        bad += [f"control: pin still passed with its fix reverted — {item}" for item in fired]

    return bad


def main(argv: list[str]) -> int:
    invert = "--prove-failure" in argv
    label = "css ties[--prove-failure]" if invert else "css ties"
    try:
        found = check(invert=invert)
    except Unreadable as error:
        # Fails closed in both modes. An inverted run is supposed to report failures, so a
        # sweep that read nothing would otherwise look exactly like a control doing its job.
        print(f"  {label}: {error}")
        return 1
    for message in found:
        print(f"  {label}: {message}")
    if invert:
        if not found:
            print(
                "\n--prove-failure: reverting every fix produced no failure. "
                "The check cannot fail and is asserting nothing."
            )
            return 1
        survived = [m for m in found if "still passed with its fix reverted" in m]
        # Every other `control:` line comes from `_apply_reverts`: a revert that no longer
        # matches, or one naming a sheet the sweep never read. Keyed on the prefix rather than
        # on either wording, so a new way for a revert to go stale cannot land unreported.
        stale = [
            m
            for m in found
            if m.startswith("control: ") and "still passed with its fix reverted" not in m
        ]
        print(f"\n--prove-failure: {len(found)} failure(s) as required")
        if stale:
            print(f"--prove-failure: {len(stale)} revert(s) are stale — see above")
        if survived:
            print(f"--prove-failure: {len(survived)} pin(s) survived their revert — see above")
        return 1 if (survived or stale) else 0
    if found:
        print(f"\ncss ties: {len(found)} failure(s)")
        return 1
    print("css ties: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
