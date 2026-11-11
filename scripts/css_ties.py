#!/usr/bin/env python3
"""Pin the specificity decisions the stylesheets rely on.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is a **regression guard over an explicit list of already-resolved ties**. It is
**not a tie detector.** It cannot tell you that a new tie exists. It can only tell you
that the decisions listed in `OUTRANKS`, `DISJOINT`, `ORDERED` and `GONE` below are still
decided the way `docs/apps.md` says they are.

The distinction matters, because the tables are pinned differently:

* `DISJOINT` pairs are checked **as ties**: both specificities are computed and the pair is
  asserted to share no property. Reintroduce the overlap and this names the property. It
  would also catch a *newly* introduced overlap between those two selectors.
* `ORDERED` pairs are also checked as ties, and are the case where the overlap is intended
  and neither side can be raised from here. A tie falls through to source position, so
  position is what gets asserted, along with the premise that makes it the only option: one
  sheet, both sides setting the property, equal counts. It is the weakest pin here, and the
  one to delete first: raising either side of the pair in CSS is the real fix, and it retires
  the row in favour of an `OUTRANKS` one.
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
`main()` treats an inverted run that reports nothing as a failure of the control itself, as
`scripts/check.py` does for the checks it inverts.

The two modes cover each other. A revert whose `old` text no longer matches means the file
moved under the revert table; that same edit makes the corresponding assertion fail in
normal mode, so the staleness surfaces either way rather than silently weakening the
control.

Standalone:   python3 -I scripts/css_ties.py [--prove-failure]
As a check:   `scripts/check.py` imports `check` and runs it as `("css ties", ...)`, in its
              `INVERTIBLE` set so the suite drives `invert=True` too. It takes no `page` and
              touches no network, port or browser, so it costs the suite a file read per
              sheet and nothing else.
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


def parse(text: str) -> list[tuple[str, dict[str, str], int]]:
    """Flatten a stylesheet to (selector, declarations, position) triples.

    `position` counts blocks from the top of the sheet, and the members of one selector list
    share it. `ORDERED` is the only assertion that reads it, and it needs the position of the
    block rather than of the selector text: the same selector can be spelled inside a longer
    one elsewhere in the file, so searching the source for it finds the wrong rule.

    At-rule preludes are dropped and their bodies are flattened in with everything else:
    every selector this file names sits either at the top level or inside a `@container` /
    `@media` block, and no assertion below compares across that boundary.

    Declarations are split on `;`, which a semicolon inside a `url()` would break. No sheet
    in this tree contains one; `check()` asserts the parse is not empty, which is what would
    catch a file this parser could not read.
    """
    src = strip_comments(text)
    out: list[tuple[str, dict[str, str], int]] = []
    buf = ""
    block = 0
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
                    out.append((sel.strip(), decls, block))
            block += 1
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
#: check anything else. Whichever selectors also appear in `docs/apps.md`'s specificity table
#: (`grep -n 'What decided it' docs/apps.md`) carry the same numbers from a separate hand
#: count there, so the overlap is a second independent source rather than a slice of this dict.
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
    ".menu-popup.context-menu": (0, 2, 0),
    ".menu-item:hover": (0, 2, 0),
    '.menu-title[aria-expanded="true"]': (0, 2, 0),
    '.menu-popup .menu-item[aria-disabled="true"]': (0, 3, 0),
    # Two attributes and a pseudo-element on one selector, which is the shape this calculator
    # gets wrong most easily: the attributes are class-column, the pseudo-element is
    # type-column, and the descendant class is a fourth in the class column.
    '.menu-item[aria-checked="true"] .menu-check::before': (0, 3, 1),
    '.menu-item[role="menuitemradio"][aria-checked="true"] .menu-check::before': (0, 4, 1),
    ".dialog.picker": (0, 2, 0),
    ".picker-row:hover": (0, 2, 0),
    '.picker-row[aria-selected="true"]': (0, 2, 0),
    '.picker-list .picker-row[aria-selected="true"]': (0, 3, 0),
    ".picker-list .picker-row:focus-visible": (0, 3, 0),
    "[tabindex]:focus-visible": (0, 2, 0),
    ".window-body.app-explorer": (0, 2, 0),
    ".app-explorer .exp-row:hover": (0, 3, 0),
    '.app-explorer .exp-row[aria-selected="true"]': (0, 3, 0),
    '.app-explorer .exp-list .exp-row[aria-selected="true"]': (0, 4, 0),
    '.app-explorer .exp-row[aria-selected="true"] .exp-type': (0, 4, 0),
    ".app-explorer .exp-type": (0, 2, 0),
    # Single-quoted attribute value, which is the spelling `src/folder.css` uses. The
    # calculator must not read the quote style as part of the count.
    ".folder-item:hover": (0, 2, 0),
    ".folder-item[aria-selected='true']": (0, 2, 0),
    ".folder-items .folder-item[aria-selected='true']": (0, 3, 0),
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
    # `src/contextmenu.css` over `src/menu.css`. `contextmenu.mjs` links the pair in a fixed
    # order, and the first menu bar a program builds links `menu.css` on its own — so either
    # sheet can be the later one and neither override may rest on that.
    (
        ".menu-popup.context-menu",
        ".menu-popup",
        "z-index",
        "context menu paints over the taskbar",
    ),
    # `aria-checked` is the attribute for both menu roles, so the tick rule matches a radio row
    # too and the bullet is an override rather than an addition.
    (
        '.menu-item[role="menuitemradio"][aria-checked="true"] .menu-check::before',
        '.menu-item[aria-checked="true"] .menu-check::before',
        "content",
        "radio row draws a bullet, not a tick",
    ),
    # A dead row must not light up. Both halves of the highlight are pinned, and `:focus`
    # beside `:hover` because a menu moves focus with the pointer: the row under the pointer
    # and the row the arrow keys reached are the same row.
    (
        '.menu-popup .menu-item[aria-disabled="true"]',
        ".menu-item:hover",
        "background-color",
        "disabled menu row stays unlit",
    ),
    (
        '.menu-popup .menu-item[aria-disabled="true"]',
        ".menu-item:hover",
        "color",
        "disabled menu row stays unlit",
    ),
    (
        '.menu-popup .menu-item[aria-disabled="true"]',
        ".menu-item:focus",
        "background-color",
        "disabled menu row stays unlit",
    ),
    (
        '.menu-popup .menu-item[aria-disabled="true"]',
        ".menu-item:focus",
        "color",
        "disabled menu row stays unlit",
    ),
    # The picker wears `.dialog`'s skin and overrides its two sizes. `min-height` is the one
    # that shows: a CSS minimum beats a maximum, so `.dialog`'s 150px winning here pushes the
    # footer — and both answers — out of a short window's frame.
    (".dialog.picker", ".dialog", "width", "picker is wider than a message box"),
    (".dialog.picker", ".dialog", "min-height", "picker gives its height up"),
    # The theme's focus ring is a white inset line with a navy halo, drawn for chrome and
    # invisible on a white list. The picker replaces it, so it has to outrank the `box-shadow`
    # half rather than sit after it.
    (
        ".picker-list .picker-row:focus-visible",
        "button:focus-visible",
        "box-shadow",
        "picker row's own focus ring",
    ),
    (
        ".picker-list .picker-row:focus-visible",
        "button:focus-visible",
        "outline",
        "picker row's own focus ring",
    ),
    # Explorer's list scrolls, not the client area around it.
    (
        ".window-body.app-explorer",
        ".window-body",
        "overflow",
        "explorer scrolls its list",
    ),
    # `--muted` is a dark grey chosen for white; on the selection blue it measures 2.14:1.
    (
        '.app-explorer .exp-row[aria-selected="true"] .exp-type',
        ".app-explorer .exp-type",
        "color",
        "selected row's grey cells inherit white",
    ),
    (
        '.app-explorer .exp-row[aria-selected="true"] .exp-modified',
        ".app-explorer .exp-modified",
        "color",
        "selected row's grey cells inherit white",
    ),
    # A selected row or tile under the pointer. Each of the three pairs below tied on
    # `background` at equal counts, so the selection won by sitting below hover and nothing
    # else; the selected side now carries a third class and wins on the count. What the tie
    # was holding up is a contrast floor: hover's wash over `--bg` leaves `color: #fff` at
    # 1.24:1 on the two 16% lists and 1.31:1 on the folder's 20% one, against the 5.31:1 and
    # 14.25:1 the two selection blues are chosen for.
    (
        '.picker-list .picker-row[aria-selected="true"]',
        ".picker-row:hover",
        "background",
        "picker selection is not washed out by hover",
    ),
    (
        '.app-explorer .exp-list .exp-row[aria-selected="true"]',
        ".app-explorer .exp-row:hover",
        "background",
        "explorer selection is not washed out by hover",
    ),
    # Two properties, because the folder's two rules overlap on both. `border-color` alone
    # decides whether a selected tile keeps its own edge or hover's.
    (
        ".folder-items .folder-item[aria-selected='true']",
        ".folder-item:hover",
        "background",
        "folder selection is not washed out by hover",
    ),
    (
        ".folder-items .folder-item[aria-selected='true']",
        ".folder-item:hover",
        "border-color",
        "folder selection keeps its own edge",
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
    # `src/menu.css` is the same arrangement: the pressed row flattens the theme's button with
    # `background-image` and `box-shadow`, and the highlight paints `background-color` and
    # `color`. A `background` shorthand on the pressed side reaches `background-color` and
    # hands the result back to the order of two blocks in one file.
    (".menu-item:active", ".menu-item:hover", "menu row pressed vs hovered"),
    (".menu-item:active", ".menu-item:focus", "menu row pressed vs focused"),
    (".menu-title:active", ".menu-title:hover", "menu title pressed vs hovered"),
    (".menu-title:active", ".menu-title:focus", "menu title pressed vs focused"),
    (".menu-title:active", '.menu-title[aria-expanded="true"]', "menu title pressed vs open"),
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

#: (earlier, later, property, label). A tie inside one sheet whose outcome is source order,
#: pinned as the order it rests on. This is the weakest pin in this file, and the only one
#: available when neither side can be raised: an equal count falls through to position, so
#: position is what there is to assert.
#:
#: The premise is asserted with it — one sheet, both sides setting the property, equal counts.
#: Raising either side is caught as a rename, because specificity is a function of the selector
#: text: the old spelling goes absent and the run fails. The equality assertion is therefore a
#: guard on this table rather than on the sheets, in the same role as `SELF_TEST` — it catches a
#: row whose two sides never tied, and so asserted an order that nothing rested on.
#:
#: Empty, and kept rather than deleted: it is the only pin available when neither side of a
#: tie can be raised. Every tie it would hold is instead in `OUTRANKS`, where the outcome does
#: not depend on the order of two blocks in one file.
ORDERED: tuple[tuple[str, str, str, str], ...] = ()

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
    # The flattened spelling of each selector the menu, context menu, picker and explorer
    # sheets win a tie with. Every one of these ties a live rule on a property both set, which
    # hands the outcome to source order: `.context-menu` ties `.menu-popup` on `z-index`,
    # `.menu-item[aria-disabled="true"]` ties `.menu-item:hover` on the highlight, `.picker`
    # ties `.dialog` on both sizes, `.picker-row:focus-visible` ties the theme's
    # `[tabindex]:focus-visible` — the picker's rows carry a roving `tabindex`, so one element
    # matches both — and `.app-explorer` ties `.window-body` on `overflow`.
    ".context-menu",
    '.menu-item[aria-disabled="true"]',
    ".picker",
    ".picker-row:focus-visible",
    ".app-explorer",
    # The flattened spelling of the three selection rules. Each tied its own sheet's hover
    # rule on `background` at equal counts, which handed a contrast floor to source order.
    '.picker-row[aria-selected="true"]',
    '.app-explorer .exp-row[aria-selected="true"]',
    ".folder-item[aria-selected='true']",
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
    (
        "src/contextmenu.css",
        ".menu-popup.context-menu {",
        ".context-menu {",
        "context menu paints over the taskbar",
    ),
    (
        "src/contextmenu.css",
        '.menu-item[role="menuitemradio"][aria-checked="true"] .menu-check::before',
        '.menu-item[aria-checked="true"] .menu-check::before',
        "radio row draws a bullet, not a tick",
    ),
    (
        "src/menu.css",
        '.menu-popup .menu-item[aria-disabled="true"] {',
        '.menu-item[aria-disabled="true"] {',
        "disabled menu row stays unlit",
    ),
    (
        "src/menu.css",
        ".menu-item:active { background-image: none;",
        ".menu-item:active { background: none;",
        "menu row pressed vs hovered",
    ),
    ("src/picker.css", ".dialog.picker {", ".picker {", "picker is wider than a message box"),
    (
        "src/picker.css",
        ".picker-list .picker-row:focus-visible {",
        ".picker-row:focus-visible {",
        "picker row's own focus ring",
    ),
    # Each of the three flattens one selection rule back to the spelling that tied its sheet's
    # hover rule. Flattening is the revert these fixes actually get in life: the scope looks
    # redundant to a reader who does not know it is carrying the count, and deleting it leaves
    # a rule that still matches everything it matched before and still paints the same blue —
    # until a pointer is over the selected row.
    (
        "src/picker.css",
        '.picker-list .picker-row[aria-selected="true"] {',
        '.picker-row[aria-selected="true"] {',
        "picker selection is not washed out by hover",
    ),
    (
        "apps/explorer/app.css",
        '.app-explorer .exp-list .exp-row[aria-selected="true"] {',
        '.app-explorer .exp-row[aria-selected="true"] {',
        "explorer selection is not washed out by hover",
    ),
    (
        "src/folder.css",
        ".folder-items .folder-item[aria-selected='true'] {",
        ".folder-item[aria-selected='true'] {",
        "folder selection is not washed out by hover",
    ),
    (
        "apps/explorer/app.css",
        ".window-body.app-explorer {",
        ".app-explorer {",
        "explorer scrolls its list",
    ),
    (
        "apps/explorer/app.css",
        '.app-explorer .exp-row[aria-selected="true"] .exp-type,\n'
        '.app-explorer .exp-row[aria-selected="true"] .exp-modified {',
        ".app-explorer .exp-type,\n.app-explorer .exp-modified {",
        "selected row's grey cells inherit white",
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

    index: dict[str, list[tuple[str, dict[str, str], int]]] = {}
    for sheet, text in sources.items():
        rules = parse(text)
        if not rules:
            bad.append(f"parse: {sheet} yielded no rules — the parser could not read it")
        for sel, decls, block in rules:
            index.setdefault(sel, []).append((sheet, decls, block))

    missing: list[str] = []

    def rule(sel: str) -> list[tuple[str, dict[str, str], int]]:
        """An unknown selector is a failure. See the module docstring."""
        if sel not in index:
            missing.append(sel)
            return []
        return index[sel]

    def declared(sel: str, prop: str) -> str | None:
        for _sheet, decls, _block in rule(sel):
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
        da = {prop for _sheet, decls, _block in rule(a) for prop in decls}
        db = {prop for _sheet, decls, _block in rule(b) for prop in decls}
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

    for earlier, later, prop, label in ORDERED:
        places = {
            side: [(sheet, block) for sheet, decls, block in rule(side) if prop in decls]
            for side in (earlier, later)
        }
        if not places[earlier] or not places[later]:
            bad.append(
                f"{label}: {earlier} or {later} does not set {prop}, "
                "so the pin asserts nothing"
            )
            continue
        se, sl = specificity(earlier), specificity(later)
        if se != sl:
            bad.append(
                f"{label}: {earlier} {se} and {later} {sl} no longer tie, so order no longer "
                f"decides {prop} — replace this pin with an OUTRANKS row"
            )
            continue
        held = {sheet for side in places.values() for sheet, _block in side}
        if len(held) != 1:
            bad.append(
                f"{label}: {earlier} and {later} set {prop} across {sorted(held)}; "
                "between sheets the order is link order, which this pin cannot assert"
            )
            continue
        if max(block for _sheet, block in places[earlier]) >= min(
            block for _sheet, block in places[later]
        ):
            bad.append(
                f"{label}: {later} no longer sits below every {earlier} block in "
                f"{held.pop()}, so the tie on {prop} now goes the other way"
            )
        else:
            fired.append(f"{label}: {later} after {earlier}")

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
