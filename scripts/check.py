#!/usr/bin/env python3
"""Drive the demo desktop in headless Chrome and assert the window behaviour.

A window manager cannot be checked by screenshot. An inert frame and a working one are the
same picture; what separates them is whether the geometry changed after a drag. So every
check here dispatches real input through CDP and then reads the numbers back off the
window's own state.

    .venv/bin/python scripts/check.py [--port PORT] [--out screenshots]

Without `--port` the server takes a free port the kernel picks, which cannot collide with a
second run out of this checkout. A fixed port is what you pass when you want to attach to the
page yourself.

A flag exists to keep the harness honest:

    --prove-failure   Inverts the drag assertion, so a run that reports clean is reporting
                      something. A harness that has never been seen to fail is a harness
                      nobody has checked.

The negative control is not a flag: `check_drag_is_title_bar_only` performs the identical
drag starting inside the window body and requires that nothing moves. That is what rules
out "the probe reads a number that is always right".
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser, CDPError  # noqa: E402
from serve import ServeError, serve  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

#: One desktop size. A window manager's behaviour is not viewport-dependent the way a
#: stylesheet's is, and the narrow case that does matter — content inside a small window —
#: is exercised by resizing a window rather than by shrinking the screen.
VIEWPORT = (1280, 820)

#: The viewport `check_viewport_shrink` forces, small enough that a window parked at x=1000
#: is entirely off the desktop. Named rather than inlined because that check has to assert
#: the shrink arrived at the surface, not merely that it was requested.
SHRUNK_VIEWPORT = (520, 420)

#: How much of a window must stay reachable, from `src/window.mjs`'s `MIN_VISIBLE`.
#:
#: Asserted at the documented figure. Both clamp checks previously allowed 60 while the
#: product and `docs/windows.md` promise 72, so twelve pixels of the guarantee could be lost
#: with nothing failing — `verification.md` §"Harness honesty": a check must not be weakened
#: to make it pass.
MIN_VISIBLE = 72

#: The minimums `src/window.mjs` exports. Duplicated here because a check that imports the
#: value it is checking cannot catch the value changing.
MIN_W = 220
MIN_H = 120

#: Every program the desktop registers, and whether it is resizable. The string is the
#: window's title once it has mounted — not its Start menu label, which `launch` asks the
#: desktop for: Explorer is listed as `Explorer` and names its window after the folder.
PROGRAMS = {
    "readme": ("Read me", True),
    "notepad": ("Untitled - Notepad", True),
    "display": ("Display Properties", True),
    "windows": ("Window list", True),
    "webview": ("Webview", True),
    "basemap": ("Base map", True),
    "map-challenge": ("30DaysOfMaps", True),
    "terminal": ("Terminal", True),
    "explorer": ("user - Explorer", True),
    "about": ("About webxp", False),
}

#: Where `shared/fs/fs.mjs` keeps the filesystem. Duplicated here rather than imported,
#: because a check that reads the key out of the code cannot catch the key changing — and
#: because the filesystem checks have to be able to clear it before they trust the tree.
FS_KEY = "webxp.fs.v1"

#: Settling time after an animated state change. `--zoom-ms` is 160; this is comfortably
#: past it plus a frame, without being long enough to hide a hang.
SETTLE_S = 0.45

def apps_on_disk() -> list[str]:
    """Every app folder, in the order `scripts/apps.py` writes them to the manifest.

    Read from the filesystem rather than from the manifest on purpose: a check that reads the
    manifest to find out what should be in the manifest cannot catch a stale one.
    """
    root = REPO_ROOT / "apps"
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / "app.mjs").is_file()
    )


APPS_ON_DISK = apps_on_disk()

#: Set by `main`, so a check that has to reload the page can reach the server.
BASE_URL = ""

#: Where a check writes an image of its own. Set by `main`.
SHOT_DIR = REPO_ROOT / "screenshots"

#: Console messages that are noise rather than a defect. Kept narrow on purpose: a blanket
#: filter is how a real error gets ignored for a week. Both entries exist only because the
#: Base map app fetches real tiles.
IGNORED: tuple[str, ...] = (
    # A raster pyramid 404s at its own edge, which is normal and which MapLibre reports
    # rather than swallows.
    "Failed to load resource: the server responded with a status of 404",
    # A tile request still in flight when the camera moves on.
    "AbortError",
    # Esri's imagery runs out before its own declared maximum. `BASEMAPS.satellite` says
    # `maxZoom: 19` and the service does serve 19 in most places, but where it has no tile at
    # that level it answers with an error page carrying no `Access-Control-Allow-Origin`, so
    # the browser reports CORS and `net::ERR_FAILED` rather than a status. Same condition as
    # the 404 above, surfacing differently because of the missing header.
    #
    # Keyed to the host on purpose. "CORS policy" or "ERR_FAILED" alone would hide a
    # same-origin module or a local fetch failing, which is the class of defect this sweep
    # exists to catch.
    "server.arcgisonline.com",
)


#: The desktop's whole observable state, read in one round trip.
#:
#: `window.webxp` is the Desktop instance `demo/boot.mjs` publishes for the devtools
#: console. Geometry comes back three ways, and the distinction between them is the point:
#: `box` is what the last gesture asked for, `placed` is `box` held inside the desktop that
#: exists now and is what reaches `--win-x/y/w/h`, and `rect` is what the browser actually
#: laid out. All three, because a geometry the code updated and the layout ignored is
#: exactly the bug a screenshot misses — and because a clamp check reading the request where
#: it means the placement measures the window the user cannot see.
PROBE = """(() => {
  const d = window.webxp;
  if (!d) return { error: 'window.webxp missing' };
  const surface = d.surface.getBoundingClientRect();
  return {
    surface: { x: surface.left, y: surface.top, w: surface.width, h: surface.height },
    active: d.active ? d.active.programId : null,
    taskButtons: document.querySelectorAll('.task-button').length,
    live: (document.getElementById('webxp-live') || {}).textContent || '',
    wins: d.windows.map((w) => {
      const rect = w.el.getBoundingClientRect();
      const grips = [...w.el.querySelectorAll('.window-grip')];
      return {
        program: w.programId,
        title: w.titleText,
        state: w.state,
        hidden: w.el.hidden,
        id: w.id,
        // Holes named here rather than left for Python to trip over.
        //
        // `in`, never a truth test: `0` is a legitimate coordinate, so `if (!w.placed.x)`
        // calls a window at the left edge broken. And `JSON.stringify` drops a key whose
        // value is `undefined` rather than sending `null`, so a field read before it was
        // set arrives with the key *absent* — which is how `KeyError: 'w'` presented, at a
        // subscript hundreds of lines away from the early read. The page knows which field
        // was not there; by the time Python has the object, that is gone.
        holes: ['box', 'placed'].flatMap((name) => {
          const source = w[name];
          if (!source || typeof source !== 'object') return [name + ' is ' + String(source)];
          return ['x', 'y', 'w', 'h']
            .filter((axis) => !(axis in source))
            .map((axis) => name + '.' + axis);
        }),
        box: { ...w.box },
        placed: { ...w.placed },
        rect: { x: Math.round(rect.left), y: Math.round(rect.top),
                w: Math.round(rect.width), h: Math.round(rect.height) },
        z: Number(w.el.style.getPropertyValue('--win-z')) || 0,
        pressed: w.taskButton.getAttribute('aria-pressed') === 'true',
        activeClass: w.el.classList.contains('is-active'),
        // A grip that is in the DOM but has no box cannot be grabbed, which is the
        // distinction a fixed-size window depends on.
        liveGrips: grips.filter((g) => g.getBoundingClientRect().width > 0).length,
        maxBoxHidden: w.maxBtn.hidden || w.maxBtn.getBoundingClientRect().width === 0,
      };
    }),
  };
})()"""


def console_errors(page) -> list[str]:
    """Error-level console messages and uncaught exceptions, minus the known noise."""
    found = []
    for event in page.consume_events():
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = (details.get("exception") or {}).get("description") or details.get("text")
            found.append(f"uncaught: {text}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
            found.append("console.error: " + " ".join(parts))
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                found.append(f"log: {entry.get('text')} {entry.get('url') or ''}".strip())
    return [m for m in found if not any(skip in m for skip in IGNORED)]


#: Console output drained before the end-of-run sweep, kept rather than discarded.
#:
#: `cdp.py`'s `consume_events` is destructive, so a bare `console_errors(page)` mid-run wiped
#: the buffer and every error raised by a later check vanished. Anything drained for a reason
#: lands here and is folded into the run's failures.
PENDING: list[str] = []


def drain(page) -> None:
    """Take the console buffer and keep it, for a check that must clear it to do its job."""
    PENDING.extend(console_errors(page))


def drain_expecting(page, needle: str, label: str) -> list[str]:
    """Consume the console, require a message containing `needle`, and surface the rest.

    Two checks provoke a console error on purpose, because the code under test is supposed
    to report rather than swallow. Draining here rather than adding `needle` to `IGNORED`
    keeps the end-of-run sweep strict: an ignore list that grows for every deliberate error
    is how a real one gets filtered out six months later. Requiring the message also turns
    "the error path reports" into an assertion instead of an assumption.
    """
    messages = console_errors(page)
    bad = []
    if not any(needle in message for message in messages):
        bad.append(f"{label}: no console error mentioning {needle!r}; saw {messages}")
    bad += [
        f"{label}: unexpected console error: {message}"
        for message in messages
        if needle not in message
    ]
    return bad


#: Floor on the compressed size of a screenshot of the map canvas, in bytes.
#:
#: A stand-in for "did it paint", because there is no image library here to measure variance
#: with and a WebGL canvas without `preserveDrawingBuffer` cannot be read with `toDataURL`.
#: PNG compresses a flat colour to almost nothing and a map to a lot, so the gap is wide
#: enough to be unambiguous.
#:
#: Measured by this check at 1280x820 with the Base map window at 760x520: the blank case
#: (the `.map` specificity defect, canvas stuck at 758x300 over the holding colour) produced
#: 14,810 bytes, and the same region with the simple basemap drawn produced 446,833. The
#: blank figure is not near zero because the clip includes the control card and the
#: attribution, which are real content either way.
#:
#: 20,000 is just above the blank case and 22x below the drawn one.
MAP_PAINT_FLOOR = 20_000


def shoot_clip(page, rect: dict, target: Path) -> int:
    """Screenshot one region and return its compressed size."""
    data = page.call(
        "Page.captureScreenshot",
        format="png",
        clip={
            "x": float(rect["x"]), "y": float(rect["y"]),
            "width": float(rect["w"]), "height": float(rect["h"]),
            "scale": 1,
        },
    )
    blob = base64.b64decode(data["data"])
    target.write_bytes(blob)
    return len(blob)


def shoot(page, target: Path) -> int:
    data = page.call("Page.captureScreenshot", format="png")
    blob = base64.b64decode(data["data"])
    target.write_bytes(blob)
    return len(blob)


# ---------------------------------------------------------------------------
# Input. Real events, not synthesised ones: a `dispatchEvent` in page script would prove
# the listener runs and nothing about whether the listener is reachable.
# ---------------------------------------------------------------------------

#: Arrow key codes. `cdp.py` is a verbatim copy of another project's client and carries only
#: the keys that project needed, so the two it lacks are dispatched here rather than by
#: diverging the copy.
ARROWS = {"ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40}


def mouse(page, event_type: str, x: float, y: float, *, buttons: int = 0,
          clicks: int = 1) -> None:
    page.call(
        "Input.dispatchMouseEvent",
        type=event_type,
        x=float(x),
        y=float(y),
        button="left",
        buttons=buttons,
        clickCount=clicks,
    )


def double_click(page, x: float, y: float) -> None:
    """A real double-click: two presses, the second carrying `clickCount: 2`.

    The count is what makes it one. Two presses that both say `clickCount: 1` are two single
    clicks and the browser never fires `dblclick` — which looks exactly like an app that
    ignores double-clicks, and is how this helper came to exist.
    """
    for count in (1, 2):
        mouse(page, "mousePressed", x, y, buttons=1, clicks=count)
        mouse(page, "mouseReleased", x, y, buttons=0, clicks=count)
    time.sleep(0.1)


def drag(page, x0: float, y0: float, dx: float, dy: float, steps: int = 4) -> None:
    """Press, move in several steps, release.

    Several steps and not one: a single jump can satisfy a handler that reads the event's
    delta and miss one that accumulates from the press position, and the second kind is the
    one that drifts.
    """
    mouse(page, "mousePressed", x0, y0, buttons=1)
    for step in range(1, steps + 1):
        mouse(page, "mouseMoved", x0 + dx * step / steps, y0 + dy * step / steps, buttons=1)
    mouse(page, "mouseReleased", x0 + dx, y0 + dy, buttons=1)
    time.sleep(0.08)


def arrow(page, key: str, *, shift: bool = False) -> None:
    code = ARROWS[key]
    for event_type in ("rawKeyDown", "keyUp"):
        page.call(
            "Input.dispatchKeyEvent",
            type=event_type,
            key=key,
            code=key,
            windowsVirtualKeyCode=code,
            nativeVirtualKeyCode=code,
            modifiers=8 if shift else 0,
        )


# ---------------------------------------------------------------------------
# Reading the page.
# ---------------------------------------------------------------------------


class HarnessError(CDPError):
    """The harness could not reach, or could not read, what it was pointed at.

    The distinction this exists to make: a check's returned list says *what was measured is
    wrong*, and this says *the measurement did not happen*. The suite had one word for both
    — a bare `CDPError` out of `probe` — and `verification.md` §"Harness honesty" makes the
    second the worse of the two, because a run that could not measure must not report clean.

    A `CDPError` subclass so the one existing `except CDPError` keeps catching it. Caught by
    the check loop in `main`, which reports it as a failed check under that check's name.
    """


#: The four numbers every geometry reading carries.
GEOMETRY_KEYS = ("x", "y", "w", "h")

#: The three readings `PROBE` returns per window. The names, and the reason they differ, are
#: at `PROBE`.
GEOMETRY_READINGS = ("box", "placed", "rect")


#: How long `probe` waits before deciding a geometry that was not there is not coming.
#:
#: The wait is what separates "the probe was early" from "the field is gone". One re-read is
#: enough to tell them apart and neither answer is a pass, so this is short.
PROBE_RETRY_S = 0.3


def probe(page) -> dict:
    """The desktop's state, with the keys every check indexes into proved present.

    Validated rather than trusted. A peer ran the suite mid-edit and hit `KeyError: 'w'` on
    `after["placed"]["w"]`: it killed the run inside the check loop while the log still read
    14 passes, so the 26 checks after it were never measured and nothing said so. A probe
    that hands back a dict missing a key it promises turns a harness defect into a product
    verdict two frames later, in whichever check indexes it first.

    So the failure is raised here, where the shape is known and the message can name the
    window and the key, rather than wherever the first subscript happens to land.

    Three outcomes, not two, because they send a reader to three different places:

    - *not yet* — the field was not populated when the probe ran and is there a moment
      later. That is this harness reading too early, and the fix is here.
    - *absent* — it is still not there after `PROBE_RETRY_S`. That is the product: a renamed
      field, or a window that never finished being built.
    - *wrong* — the key is there holding something that is not a number. That is arithmetic,
      and it is what the checks themselves are about.

    None of the three is a pass.
    """
    state, missing = _probe_once(page)
    if missing:
        # One re-read, which is the only thing that can tell "early" from "gone".
        time.sleep(PROBE_RETRY_S)
        _, still = _probe_once(page)
        if not still:
            raise HarnessError(
                "probe: the geometry was not populated when the probe ran and is there "
                f"{PROBE_RETRY_S}s later — this harness read too early: "
                + "; ".join(missing)
            )
        raise HarnessError(
            f"probe: the geometry PROBE promises is not all there, still, after "
            f"{PROBE_RETRY_S}s — " + "; ".join(still)
        )
    return state


def _probe_once(page) -> tuple[dict, list[str]]:
    """One read of the desktop's state, and everything wrong with its shape.

    Split from `probe` so the re-read that distinguishes "early" from "gone" is the same
    code path as the first read. A second, looser reader would be a second definition of
    what a valid geometry is.
    """
    state = page.evaluate(PROBE)
    if not isinstance(state, dict):
        raise HarnessError(f"probe: the desktop answered {state!r}, not a state object")
    if state.get("error"):
        raise HarnessError(f"probe: {state['error']}")
    for key in ("surface", "wins"):
        if key not in state:
            raise HarnessError(f"probe: no {key!r} in the state — PROBE and the checks disagree")
    if not isinstance(state["wins"], list):
        raise HarnessError(f"probe: 'wins' is {state['wins']!r}, not a list")

    missing: list[str] = []
    for index, win in enumerate(state["wins"]):
        # The window id, because that is what names it in the DOM and in the shell's own
        # logs. The program id is the friendlier name, so both when both are there.
        who = win.get("id") or f"wins[{index}]"
        if win.get("program"):
            who = f"{who} ({win['program']})"
        # What the page itself said was not there. Kept first because it is the only report
        # that distinguishes an absent key from a key the serialiser dropped: `PROBE` tested
        # with `in`, before `JSON.stringify` erased the difference.
        for hole in win.get("holes") or []:
            missing.append(f"{who}: the page reports no {hole}")
        for reading in GEOMETRY_READINGS:
            geometry = win.get(reading)
            if not isinstance(geometry, dict):
                missing.append(f"{who}: {reading} is {geometry!r}, not a geometry")
                continue
            # An absent key and a present one holding something that is not a number point
            # at different places, so they are reported differently. Every axis absent means
            # the object `PROBE` spread was `undefined` — the field has been renamed, or the
            # browser is running a different `window.mjs` from the one on disk. A present
            # key holding `None` is a number that did not survive JSON: `NaN` or an
            # infinity, which is arithmetic. Reported as one line when the whole reading is
            # gone, because four identical lines bury the fact that it is all four.
            absent = [axis for axis in GEOMETRY_KEYS if axis not in geometry]
            if len(absent) == len(GEOMETRY_KEYS):
                missing.append(
                    f"{who}: {reading} came back as {{}} — whatever PROBE spread was "
                    "undefined, so this is a renamed field or a stale module, not a bad number"
                )
                continue
            for axis in GEOMETRY_KEYS:
                if axis in absent:
                    missing.append(f"{who}: {reading}[{axis!r}] is absent")
                    continue
                value = geometry[axis]
                # `bool` first: it is an `int` in Python, and a geometry that came back
                # `True` is not a measurement.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    missing.append(
                        f"{who}: {reading}[{axis!r}] is {value!r}, not a number"
                        + (" (NaN or an infinity does not survive JSON)"
                           if value is None else "")
                    )
    return state, missing


def window_of(state: dict, program: str) -> dict | None:
    for win in state["wins"]:
        if win["program"] == program:
            return win
    return None


def need(state: dict, program: str) -> dict:
    win = window_of(state, program)
    if win is None:
        raise CDPError(f"no window open for {program!r}")
    return win


def rect_of(page, selector: str) -> dict | None:
    """One element's box, for aiming at a title bar or a resize grip."""
    return page.evaluate(
        "(() => { const e = document.querySelector(%s);"
        " if (!e) return null; const r = e.getBoundingClientRect();"
        " return r.width ? {x: r.left, y: r.top, w: r.width, h: r.height} : null; })()"
        % json.dumps(selector)
    )


def win_selector(page, program: str) -> str:
    """A CSS selector for one program's window, by the id the shell generated."""
    win_id = page.evaluate(
        "(window.webxp.windows.find((w) => w.programId === %s) || {}).id || ''"
        % json.dumps(program)
    )
    if not win_id:
        raise CDPError(f"no window open for {program!r}")
    return f"#{win_id}"


def launch(page, program: str) -> None:
    """Open a program through the Start menu, so the menu is covered too.

    The menu label is asked of the desktop rather than read from `PROGRAMS`. The two are not
    the same string for every app: Explorer is listed as `Explorer` and titles its window
    after the folder it is showing, so a table serving as both the label and the expected
    title has to lie about one of them.
    """
    label = page.evaluate(
        "((window.webxp.programs.get(%s) || {}).name) || ''" % json.dumps(program)
    )
    if not label:
        raise CDPError(f"the desktop has no program registered as {program!r}")
    page.evaluate("document.getElementById('start').click()")
    time.sleep(0.05)
    clicked = page.evaluate(
        "(() => { const items = [...document.querySelectorAll('.start-item')];"
        " const want = %s;"
        " const hit = items.find((b) => b.textContent.includes(want));"
        " if (!hit) return false; hit.click(); return true; })()"
        % json.dumps(label)
    )
    if not clicked:
        raise CDPError(f"no Start menu item labelled {label!r} for {program!r}")
    # Past the zoom, the mount and the minimum loading floor.
    time.sleep(0.6)


def title_click(page, program: str, label: str) -> None:
    """Click one of the three title boxes by its accessible name."""
    hit = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === %s);"
        " if (!w) return false;"
        " const b = [...w.el.querySelectorAll('.title-box')]"
        "   .find((b) => b.getAttribute('aria-label') === %s);"
        " if (!b) return false; b.click(); return true; })()"
        % (json.dumps(program), json.dumps(label))
    )
    if not hit:
        raise CDPError(f"{program}: no title box labelled {label!r}")
    time.sleep(SETTLE_S)


def task_click(page, program: str) -> None:
    hit = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === %s);"
        " if (!w || !w.taskButton) return false; w.taskButton.click(); return true; })()"
        % json.dumps(program)
    )
    if not hit:
        raise CDPError(f"{program}: no taskbar button")
    time.sleep(SETTLE_S)


def near(got: float, want: float, slack: float = 2.0) -> bool:
    return abs(got - want) <= slack


def set_viewport(page, width: int, height: int, settle: float = 0.4) -> None:
    """Force the layout viewport and wait out `watchViewport`'s coalescing frame.

    One definition, because four checks resize the browser and a resize that is not waited
    out measures the desktop before the re-clamp rather than after it.
    """
    page.call(
        "Emulation.setDeviceMetricsOverride",
        width=width, height=height, deviceScaleFactor=1, mobile=False,
    )
    time.sleep(settle)


#: Collect what the shell's live region says, per mutation, into `window.__said`.
#:
#: Per mutation and not by reading `textContent` afterwards: `announce` clears the region and
#: writes it in the same turn, so two announcements inside one task are one final value and a
#: reader that polls the text sees only the last. Measured — polling the text reported
#: `['Read me restored']` for a minimise followed by a restore, losing the minimise the
#: assertion is about.
WATCH_LIVE = """(() => {
  const live = document.getElementById('webxp-live');
  if (!live) return false;
  window.__said = [];
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        const text = (node.textContent || '').trim();
        if (text) window.__said.push(text);
      }
    }
  });
  observer.observe(live, { childList: true, characterData: true, subtree: true });
  window.__saidStop = () => { observer.disconnect(); delete window.__saidStop; };
  return true;
})()"""


def watch_live(page) -> None:
    """Start recording the live region. A missing region is a failure, never a skip."""
    if not page.evaluate(WATCH_LIVE):
        raise HarnessError(
            "no #webxp-live element — every announcement assertion below would pass on its "
            "absence"
        )


def said(page) -> list[str]:
    return page.evaluate("window.__said || []")


def stop_watching_live(page) -> None:
    page.evaluate("(() => { if (window.__saidStop) window.__saidStop(); return true; })()")


# ---------------------------------------------------------------------------
# Contrast. The stylesheets claim specific ratios in their comments, as the reason for
# departing from Luna's own values; these turn the claim into something that fails.
# ---------------------------------------------------------------------------

RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*[,/]\s*([\d.]+))?")

#: Installed in the page before the contrast sweep, and used by its probe.
#:
#: `getComputedStyle` hands back whatever notation the stylesheet used — Chrome does not
#: convert `oklch()` to `rgb()` — so a regex over `rgb()` reads nothing from a modern palette
#: and every assertion built on it vanishes. A canvas `fillStyle` round trip normalises any
#: colour the browser can parse, which is the set that matters: if the browser cannot parse
#: it, it is not painted either.
#:
#: Gradient stops are split at top-level commas so a nested `rgb(1, 2, 3)` or
#: `color-mix(in oklab, a, b)` is not torn apart by its own separators.
#:
#: An unparseable value is detected by assigning a sentinel first and seeing whether the
#: assignment displaced it. `scripts/inspect_colour_reader.py` is the control for both
#: outcomes.
COLOUR_READER = """
window.__wxColours = (css) => {
  if (!css || css === 'none') return [];
  const ctx = document.createElement('canvas').getContext('2d');
  // The sentinel the probe has to displace. Canvas `fillStyle` keeps its previous value when
  // handed something it cannot parse, so the only way to tell "unparseable" from "parsed"
  // is what is left behind — and the thing left behind must therefore be a colour no
  // stylesheet in this tree resolves to. `#000` was used here, which made a value that
  // legitimately resolves to black indistinguishable from an unparseable one: it came back
  // `UNREADABLE:`, which `unreadable` reports as a failure. Nothing resolves to pure black
  // today, so that was one palette edit away from a false failure.
  const SENTINEL = '#010203';
  const read = (token) => {
    const probe = token.trim().replace(/\s+(-?[\d.]+(%|px|r?em)|calc\(.*\))$/, '').trim();
    if (!probe) return null;
    ctx.fillStyle = SENTINEL;
    const before = ctx.fillStyle;
    ctx.fillStyle = probe;
    if (ctx.fillStyle === before) return null;
    return ctx.fillStyle;
  };
  // The wrapper comes off first. Splitting before stripping it treated a bare
  // `rgb(255, 255, 255)` as three parts, because a colour function opens a depth too.
  const wrapper = /^\s*(repeating-)?(linear|radial|conic)-gradient\(([\s\S]*)\)\s*$/i.exec(css);
  const body = wrapper ? wrapper[3] : css;

  // Top-level comma split, so nested colour functions survive.
  const parts = [];
  let depth = 0;
  let current = '';
  for (const ch of body) {
    if (ch === '(') depth += 1;
    if (ch === ')') depth -= 1;
    if (ch === ',' && depth === 0) { parts.push(current); current = ''; continue; }
    current += ch;
  }
  parts.push(current);

  const out = [];
  for (const part of parts) {
    // A gradient's geometry, not a colour.
    if (/^\s*(to\b|-?[\d.]+(deg|rad|turn|grad)|in\b|from\b|circle|ellipse|at\b|closest|farthest)/i.test(part)) continue;
    if (!part.trim()) continue;
    const got = read(part);
    if (got) out.push(got);
    else out.push('UNREADABLE:' + part.trim());
  }
  return out;
};
"""

#: WCAG 2.2 SC 1.4.3 for text below 18.66px bold / 24px regular.
AA_NORMAL = 4.5


def rgb_stops(values) -> list[tuple[int, int, int]]:
    """Opaque colours out of `__wxColours`' normalised output.

    A fully transparent colour is dropped: it is not a surface anything sits on. An entry the
    page could not read is kept as `None` by `unreadable` below, which is what makes an
    unparseable palette a failure rather than an empty list.
    """
    out = []
    for value in values or []:
        if not isinstance(value, str) or value.startswith("UNREADABLE:"):
            continue
        if value.startswith("#"):
            hexed = value.lstrip("#")
            out.append(tuple(int(hexed[i:i + 2], 16) for i in (0, 2, 4)))
            continue
        found = RGB.findall(value)
        for r, g, b, a in found:
            if a and float(a) == 0:
                continue
            out.append((int(r), int(g), int(b)))
    return out


def unreadable(values) -> list[str]:
    """Colours the page could not parse. Any entry here is a failure, never a skip."""
    return [v[len("UNREADABLE:"):] for v in (values or [])
            if isinstance(v, str) and v.startswith("UNREADABLE:")]


def luminance(colour: tuple[int, int, int]) -> float:
    """WCAG relative luminance (https://www.w3.org/TR/WCAG22/#dfn-relative-luminance)."""
    channels = []
    for raw in colour:
        c = raw / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG contrast ratio (https://www.w3.org/TR/WCAG22/#dfn-contrast-ratio)."""
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# ---------------------------------------------------------------------------
# The checks. Each returns a list of failure strings.
# ---------------------------------------------------------------------------


def check_boot(page) -> list[str]:
    """An empty desktop, then a window that lays out, focuses and fills.

    Nothing autostarts: the desktop comes up bare, which is the behaviour Nick asked for and
    is asserted here rather than merely no longer contradicted. Everything after the launch
    is what this check was always really about -- a frame at a real size, focused, with its
    grips, and past its loading marquee.
    """
    bad = []
    state = probe(page)
    if state["wins"]:
        bad.append(
            f"boot: {len(state['wins'])} windows open on a fresh desktop, want 0 — "
            f"{[w['program'] for w in state['wins']]} autostarted"
        )
    elif state["taskButtons"]:
        bad.append(f"boot: {state['taskButtons']} taskbar buttons with no windows open")

    launch(page, "readme")
    state = probe(page)
    if state["taskButtons"] != len(state["wins"]):
        bad.append(
            f"boot: {state['taskButtons']} taskbar buttons for {len(state['wins'])} windows"
        )
    win = window_of(state, "readme")
    if win is None:
        return bad + ["boot: Read me did not open"]
    if win["rect"]["w"] < MIN_W or win["rect"]["h"] < MIN_H:
        # The failure this catches is a frame that reports its geometry and lays out at
        # zero, which is invisible to any assertion that only reads `box`.
        bad.append(f"boot: Read me laid out at {win['rect']['w']}x{win['rect']['h']}")
    if not win["activeClass"] or not win["pressed"]:
        bad.append("boot: Read me is not the active window")
    if win["liveGrips"] != 8:
        bad.append(f"boot: Read me has {win['liveGrips']} grabbable resize grips, want 8")

    # The loading marquee must be gone once the mount has run. A window stuck on its
    # placeholder is the failure mode the minimum-load floor could cause.
    loading = page.evaluate("document.querySelectorAll('.window-body.is-loading').length")
    if loading:
        bad.append(f"boot: {loading} windows still showing the loading marquee")
    return bad


def check_launch_all(page) -> list[str]:
    """Every program opens, gets a button, and does not land on top of the last one."""
    bad = []
    for program in ("notepad", "display", "windows", "webview", "basemap", "map-challenge",
                    "terminal", "explorer", "about"):
        launch(page, program)
    state = probe(page)

    if len(state["wins"]) != len(PROGRAMS):
        bad.append(f"launch: {len(state['wins'])} windows, want {len(PROGRAMS)}")
    if state["taskButtons"] != len(state["wins"]):
        bad.append(f"launch: {state['taskButtons']} buttons for {len(state['wins'])} windows")

    for program, (title, resizable) in PROGRAMS.items():
        win = window_of(state, program)
        if win is None:
            bad.append(f"launch: {program} did not open")
            continue
        if win["title"] != title:
            bad.append(f"launch: {program} titled {win['title']!r}, want {title!r}")
        want_grips = 8 if resizable else 0
        if win["liveGrips"] != want_grips:
            bad.append(
                f"launch: {program} has {win['liveGrips']} grabbable grips, want {want_grips}"
            )
        if not resizable and not win["maxBoxHidden"]:
            bad.append(f"launch: {program} is fixed-size but still offers a maximise box")

    # The cascade. Two windows at the same origin is a pile, which is the thing the cascade
    # exists to prevent.
    #
    # On `placed`, because the pile is what the reader sees: `placed` is what reaches
    # `--win-x/y`, and two windows whose requests differ while their placements coincide are
    # one window as far as anyone looking at the screen is concerned.
    #
    # Measured, because the reason given for the change was not the reason it is right.
    # `clampBox` cannot collapse two *cascade* origins: the y cap is `surface.h - barHeight`
    # and `cascade` only ever writes `CASCADE_TOP + slot * CASCADE_STEP`, which needs a
    # window shorter than 12px to reach it, and `cascade` caps its own x below the clamp's.
    # Swept at five viewports (1280x820, 520x420, 300x820, 1280x300, 820x1400) the two
    # readings agreed on every one, including the two where the cascade itself collapses —
    # 5 origins of 7 at 520x420 and 1 of 7 at 1280x300. So this is not a defect `box` was
    # hiding here.
    #
    # It is still the reading the invariant wants, and it catches a pile `box` cannot see:
    # two programs declaring `x: 5000, y: 5000` and `x: 6000, y: 6000` — explicit origins
    # never cascade — come back as 2 distinct `box` origins and 1 distinct `placed` origin,
    # both at (1208, 762). That measurement is the control for this line.
    origins = {(w["placed"]["x"], w["placed"]["y"]) for w in state["wins"]}
    if len(origins) != len(state["wins"]):
        bad.append(
            f"launch: {len(state['wins'])} windows at {len(origins)} distinct placed origins"
        )

    # Launch order is z order, and the last one opened is the active one.
    if state["active"] != "about":
        bad.append(f"launch: active window is {state['active']!r}, want 'about'")
    return bad


def check_active_styling(page) -> list[str]:
    """The focused window's title bar is drawn differently from an unfocused one's.

    This is the one aesthetic claim worth asserting rather than eyeballing: "which window
    am I typing into" is carried by the title bar alone, and a stylesheet where the two
    states resolve to the same gradient looks finished and tells the reader nothing. Read
    from `getComputedStyle`, so a rule that lost its specificity battle is caught.
    """
    page.evaluate(COLOUR_READER)
    bars = page.evaluate(
        "(() => { const d = window.webxp;"
        " const C = window.__wxColours;"
        " const pick = (want) => {"
        "   const w = d.windows.find((w) => (d.active === w) === want && w.state !== 'min');"
        "   if (!w) return null;"
        "   const bar = w.el.querySelector('.window-title');"
        "   const heading = bar.querySelector('h2');"
        "   const boxes = [...w.el.querySelectorAll('.title-box')].map((b) => {"
        "     const s = getComputedStyle(b);"
        "     return { label: b.getAttribute('aria-label'), colour: C(s.color),"
        # Read apart, not concatenated. The transparent-black skip below used to test a
        # substring of the two joined, so a genuine black gradient stop beside a transparent
        # background colour was skipped — and a black glyph on it is 1:1.
        "              image: C(s.backgroundImage), fill: C(s.backgroundColor) }; });"
        "   const bs = getComputedStyle(bar);"
        "   return { program: w.programId,"
        "            bar: C(bs.backgroundImage), barFill: C(bs.backgroundColor),"
        "            barRaw: bs.backgroundImage,"
        # The white text lives on the heading, not on the bar. Reading `color` off the bar
        # returns the inherited black and compares black to black, which is a check that
        # cannot fail — the first version of this did exactly that.
        "            textColour: C(getComputedStyle(heading).color),"
        "            boxes }; };"
        " return { active: pick(true), idle: pick(false) }; })()"
    )
    if not bars.get("active") or not bars.get("idle"):
        return ["active styling: need one focused and one unfocused window to compare"]

    bad = []
    if bars["active"]["barRaw"] == bars["idle"]["barRaw"]:
        bad.append(
            "active styling: the focused and unfocused title bars resolve to the same "
            f"gradient ({bars['active']['barRaw'][:60]}…)"
        )

    # Luna's own unfocused bar kept near-white text on a pale blue and measured about
    # 2.3:1. Both of these carry white text, so every stop of both gradients has to hold it
    # — including the stop in the middle, which is where a four-stop gradient is lightest.
    for state in ("active", "idle"):
        surfaces = (bars[state]["bar"] or []) + (bars[state]["barFill"] or [])
        broken = unreadable(bars[state]["textColour"]) + unreadable(surfaces)
        if broken:
            bad.append(
                f"contrast: could not read these {state} title colours: {broken} — the "
                "sweep asserts nothing it cannot parse, so this is a failure, not a skip"
            )
        text = rgb_stops(bars[state]["textColour"])
        if not text:
            bad.append(f"contrast: could not read the {state} title colour")
            continue
        stops = rgb_stops(surfaces)
        if not stops:
            # The hole this check shipped with: an empty stop list ran the loop zero times
            # and returned clean over white text on a near-white bar, measured at 1.04:1.
            bad.append(
                f"contrast: no readable surface colour behind the {state} title text — "
                "nothing was measured"
            )
        for stop in stops:
            ratio = contrast(text[0], stop)
            if ratio < AA_NORMAL:
                bad.append(
                    f"contrast: {state} title text {text[0]} on bar stop {stop} is "
                    f"{ratio:.2f}:1, below {AA_NORMAL}:1"
                )
        for box in bars[state]["boxes"]:
            glyph = rgb_stops(box["colour"])
            if not glyph:
                bad.append(
                    f"contrast: could not read the {state} {box['label']!r} glyph colour "
                    f"({box['colour']})"
                )
                continue
            # The image's stops, plus the background colour only when it is not transparent.
            box_stops = rgb_stops(box["image"]) + rgb_stops(box["fill"])
            if not box_stops:
                bad.append(
                    f"contrast: no readable surface behind the {state} {box['label']!r} glyph"
                )
            for stop in box_stops:
                ratio = contrast(glyph[0], stop)
                if ratio < AA_NORMAL:
                    bad.append(
                        f"contrast: {state} {box['label']!r} glyph {glyph[0]} on {stop} is "
                        f"{ratio:.2f}:1, below {AA_NORMAL}:1"
                    )

    # Which bar is which, not merely that they differ.
    #
    # `inspect_titlebars.py` recorded this gap in writing: a focused window wearing the
    # unfocused gradient passes a difference assertion and is still wrong. Exchange the two
    # rule bodies — gradient and heading colour together — and every window reads backwards
    # while the difference and both contrast assertions still pass. Luna's focused bar is the
    # saturated dark one; the unfocused is the pale wash. Darkness is the property a reader
    # uses to tell them apart, so that is what is pinned.
    active_stops = rgb_stops(bars["active"]["bar"]) + rgb_stops(bars["active"]["barFill"])
    idle_stops = rgb_stops(bars["idle"]["bar"]) + rgb_stops(bars["idle"]["barFill"])
    if active_stops and idle_stops:
        active_mean = sum(luminance(c) for c in active_stops) / len(active_stops)
        idle_mean = sum(luminance(c) for c in idle_stops) / len(idle_stops)
        if active_mean >= idle_mean:
            bad.append(
                f"active styling: the focused bar (mean luminance {active_mean:.3f}) is not "
                f"darker than the unfocused one ({idle_mean:.3f}) — the two states are "
                "swapped, so every window reads backwards"
            )

    # The taskbar buttons carry window titles, which is text a reader has to pick a window
    # out of. Both states, because the pressed one is a different gradient.
    #
    # The Start button is deliberately not in this sweep. Its white italic on Luna green
    # measures 2.16:1 and is the one place the imitation is kept over the correction;
    # `docs/windows.md` records that with the number.
    buttons = page.evaluate(
        "(() => { const C = window.__wxColours;"
        " return [...document.querySelectorAll('.task-button')].map((b) => {"
        " const s = getComputedStyle(b);"
        " return { pressed: b.getAttribute('aria-pressed'), colour: C(s.color),"
        "          image: C(s.backgroundImage), fill: C(s.backgroundColor) }; }); })()"
    )
    # No buttons means no windows, which is not a clean taskbar — it is a sweep over nothing.
    if not buttons:
        bad.append("contrast: no taskbar buttons to measure; the sweep asserted nothing")
    for button in buttons:
        text = rgb_stops(button["colour"])
        if not text:
            bad.append(
                f"contrast: could not read a taskbar button's text colour "
                f"({button['colour']}) — a failure, not a skip"
            )
            continue
        stops = rgb_stops(button["image"]) + rgb_stops(button["fill"])
        if not stops:
            bad.append(
                f"contrast: no readable surface behind a taskbar button "
                f"(pressed={button['pressed']})"
            )
        for stop in stops:
            ratio = contrast(text[0], stop)
            if ratio < AA_NORMAL:
                bad.append(
                    f"contrast: taskbar button (pressed={button['pressed']}) text "
                    f"{text[0]} on {stop} is {ratio:.2f}:1, below {AA_NORMAL}:1"
                )
    return bad


def check_loading_frame(page) -> list[str]:
    """The frame arrives before the content does.

    `MIN_LOAD_MS` is deliberate latency, so it is worth proving it does the thing it costs
    something for: a window whose body is still the marquee while the frame is already
    laid out and already draggable.

    Notepad and not a singleton program: relaunching a singleton focuses the window that is
    already open and already filled, which looks exactly like a loading floor that does not
    work. The first version of this check made that mistake.
    """
    before = page.evaluate("window.webxp.windows.map((w) => w.id)")
    was = page.evaluate("window.webxp.minLoadMs")
    page.evaluate("window.webxp.minLoadMs = 1500")
    try:
        page.evaluate("window.webxp.launch('notepad')")
        time.sleep(0.5)
        shot = page.evaluate(
            "(() => { const w = window.webxp.windows[window.webxp.windows.length - 1];"
            " if (!w) return null;"
            " const body = w.bodyEl; const marquee = body.querySelector('.xp-progress-blocks');"
            " const r = w.el.getBoundingClientRect();"
            " return { loading: body.classList.contains('is-loading'),"
            "          marqueeWidth: marquee ? marquee.getBoundingClientRect().width : 0,"
            "          frameWidth: r.width, frameHeight: r.height }; })()"
        )
        bad = []
        if shot is None:
            return ["loading frame: Notepad did not open"]
        if not shot["loading"]:
            bad.append("loading frame: the body filled before the loading floor elapsed")
        if shot["marqueeWidth"] <= 0:
            bad.append("loading frame: the progress marquee is not laid out")
        if shot["frameWidth"] < MIN_W or shot["frameHeight"] < MIN_H:
            bad.append(
                f"loading frame: frame is {shot['frameWidth']}x{shot['frameHeight']} while "
                "loading — it should already be at full size"
            )
        # And it must not stay that way.
        time.sleep(1.4)
        still = page.evaluate(
            "document.querySelectorAll('.window-body.is-loading').length"
        )
        if still:
            bad.append(f"loading frame: {still} windows never left the loading state")
        return bad
    finally:
        page.evaluate(f"window.webxp.minLoadMs = {was}")
        # Close only the window this check opened, so the desktop is handed back exactly as
        # it was found and a later check cannot be broken by this one.
        page.evaluate(
            "(() => { const keep = new Set(%s);"
            " for (const w of [...window.webxp.windows]) if (!keep.has(w.id)) w.close(); })()"
            % json.dumps(before)
        )
        time.sleep(0.4)


def check_drag(page, *, invert: bool = False) -> list[str]:
    """A title-bar drag moves the window by the drag delta."""
    bad = []
    before = need(probe(page), "readme")
    bar = rect_of(page, f"{win_selector(page, 'readme')} .window-title")
    if bar is None:
        return ["drag: Read me has no laid-out title bar"]

    dx, dy = 150, 90
    # Left of the title boxes, so the press cannot land on one.
    drag(page, bar["x"] + 40, bar["y"] + bar["h"] / 2, dx, dy)

    after = need(probe(page), "readme")
    # `after["box"]` against `before["placed"]`, which is the move gesture's own contract:
    # `beginGesture` seeds `start` from `placed` and `applyGesture` writes `start + delta`
    # straight into `box` (`src/window.mjs`). Reading `before["box"]` instead measures a
    # position the gesture never started from, and it fires on a correct drag whenever a
    # clamp is live. Measured from a y-clamped start (`box.y=-50`, `placed.y=0`):
    # `drag: box went to (550, 90), want (550, 40)` — a pass that depended on nothing ahead
    # of this check in `CHECKS` leaving a clamp on.
    want_x = before["placed"]["x"] + dx
    want_y = before["placed"]["y"] + dy
    moved = near(after["box"]["x"], want_x) and near(after["box"]["y"], want_y)

    if invert:
        # --prove-failure. The window did move; asserting that it did not must fail, which
        # is the demonstration that a reported pass is a measurement.
        if moved:
            bad.append(
                "drag[--prove-failure]: window moved to "
                f"({after['box']['x']}, {after['box']['y']}) as it should — "
                "the inverted assertion failed, so the harness can fail"
            )
        return bad

    if not moved:
        bad.append(
            f"drag: box went to ({after['box']['x']}, {after['box']['y']}), "
            f"want ({want_x}, {want_y})"
        )
    # The laid-out box has to agree with the believed box, or the window moved in state
    # only — the half that reading `box` alone cannot catch.
    if not near(after["rect"]["w"], after["box"]["w"]):
        bad.append(f"drag: believed width {after['box']['w']} but laid out {after['rect']['w']}")
    return bad


def check_drag_is_title_bar_only(page) -> list[str]:
    """The negative control: the same drag from inside the body must move nothing.

    This is what makes the drag check meaningful. If the geometry changed here too, the
    probe would be reading something that moves whatever you do, and a passing drag check
    would mean nothing.
    """
    before = need(probe(page), "readme")
    body = rect_of(page, f"{win_selector(page, 'readme')} .window-body")
    if body is None:
        return ["negative control: Read me has no laid-out body"]

    drag(page, body["x"] + body["w"] / 2, body["y"] + body["h"] / 2, 150, 90)

    after = need(probe(page), "readme")
    if after["box"] != before["box"]:
        return [
            "negative control: dragging the window body moved the window from "
            f"{before['box']} to {after['box']} — the drag handler is not title-bar only"
        ]
    return []


def check_resize_corner(page) -> list[str]:
    """The south-east grip grows the window without moving its origin."""
    bad = []
    selector = win_selector(page, "readme")
    before = need(probe(page), "readme")
    grip = rect_of(page, f'{selector} .window-grip[data-dir="se"]')
    if grip is None:
        return ["resize: no south-east grip laid out"]

    dx, dy = 130, 70
    press_x, press_y = grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2
    # A press outside the viewport hits nothing, and `beginGesture` never runs. Without this
    # the assertions below still read `box` and `placed` and report the arithmetic of a
    # gesture that never started — "the origin moved", for a window nobody touched. This
    # check takes whatever geometry the previous one left, so the grip's reachability is not
    # something it can assume.
    if not (0 <= press_x < VIEWPORT[0] and 0 <= press_y < VIEWPORT[1]):
        return [
            f"resize se: the grip's centre is at {round(press_x)},{round(press_y)}, outside "
            f"the {VIEWPORT[0]}x{VIEWPORT[1]} viewport — the press cannot land, so nothing "
            "below would be measuring a resize"
        ]
    drag(page, press_x, press_y, dx, dy)

    after = need(probe(page), "readme")
    # `after["box"]` against `before["placed"]`, which is the gesture's own contract with
    # neither end of it clamped: `beginGesture` seeds `start` from `placed` and
    # `applyGesture` writes the result straight into `box` (`src/window.mjs`).
    #
    # Both of the other pairings are wrong, in opposite directions. `before["box"]` fires on
    # a correct resize whenever a clamp is live, because the gesture never started from it —
    # it passes here only because nothing ahead of this check in `CHECKS` leaves a clamp on.
    # `after["placed"]` hides a real defect: with the window against the y clamp at 762, an
    # SE drag that wrongly slid the top edge down by 70 writes `box.y = 832`, which clamps
    # straight back to 762 and compares equal.
    if not near(after["box"]["w"], before["placed"]["w"] + dx):
        bad.append(
            f"resize se: width {before['placed']['w']} -> {after['box']['w']}, want +{dx}"
        )
    if not near(after["box"]["h"], before["placed"]["h"] + dy):
        bad.append(
            f"resize se: height {before['placed']['h']} -> {after['box']['h']}, want +{dy}"
        )
    if after["box"]["x"] != before["placed"]["x"] or after["box"]["y"] != before["placed"]["y"]:
        bad.append(
            f"resize se: the origin moved from {before['placed']['x']},{before['placed']['y']}"
            f" to {after['box']['x']},{after['box']['y']} — the window slid instead of growing"
        )
    # The layout has to follow the state, and the element is written from `placed`.
    if not near(after["rect"]["w"], after["placed"]["w"]) or not near(after["rect"]["h"], after["placed"]["h"]):
        bad.append(
            f"resize se: believed {after['placed']['w']}x{after['placed']['h']} but laid out "
            f"{after['rect']['w']}x{after['rect']['h']}"
        )
    return bad


def check_resize_west(page) -> list[str]:
    """The west grip moves the left edge and the width together.

    This is the arithmetic that goes wrong: subtracting the delta from the width while also
    adding it to the left leaves the right edge drifting, which nobody notices until they
    resize against a screen edge.
    """
    bad = []
    selector = win_selector(page, "readme")
    before = need(probe(page), "readme")
    grip = rect_of(page, f'{selector} .window-grip[data-dir="w"]')
    if grip is None:
        return ["resize w: no west grip laid out"]

    dx = 60
    drag(page, grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2, dx, 0)

    after = need(probe(page), "readme")
    # `after["box"]` against `before["placed"]`, the pairing `resize se` documents: the
    # gesture starts from `placed` and lands in `box`. This check takes whatever geometry the
    # one before it left, so it cannot assume the window is unclamped on the way in — reading
    # `before["box"]` made it depend on the order of `CHECKS` for its correctness.
    start = before["placed"]
    if not near(after["box"]["x"], start["x"] + dx):
        bad.append(f"resize w: left edge {start['x']} -> {after['box']['x']}, want +{dx}")
    if not near(after["box"]["w"], start["w"] - dx):
        bad.append(f"resize w: width {start['w']} -> {after['box']['w']}, want -{dx}")
    right_before = start["x"] + start["w"]
    right_after = after["box"]["x"] + after["box"]["w"]
    if not near(right_after, right_before):
        bad.append(f"resize w: the right edge drifted {right_before} -> {right_after}")
    return bad


def check_resize_north_at_edge(page) -> list[str]:
    """Dragging the north edge into the top of the desktop must not move the bottom edge.

    The regression this exists for: computing a resize as a position plus a size lets the
    `y` clamp at the desktop top fire while the height keeps growing, so the bottom edge
    slides down and a resize from the north edge quietly becomes a move. Found by review,
    fixed by computing edges instead, and kept here because the fix is not obvious enough to
    survive an unrelated edit.
    """
    selector = win_selector(page, "readme")
    # Put the window near the top first, so one drag can reach past the desktop edge.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " w.moveTo(360, 30); w.sizeTo(460, 300); })()"
    )
    time.sleep(0.1)
    before = need(probe(page), "readme")
    grip = rect_of(page, f'{selector} .window-grip[data-dir="n"]')
    if grip is None:
        return ["resize n: no north grip laid out"]

    # Far enough up that the clamp has to fire.
    drag(page, grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2, 0, -200, steps=8)

    after = need(probe(page), "readme")
    bad = []
    # `box`, not `placed`. `applyGesture` stops the dragged top edge at 0 itself — `if (top <
    # 0) top = 0` in `src/window.mjs` — so the request is where the guarantee lives. Reading
    # `placed` would pass on a gesture that wrote `box.y = -200` and let `clampBox` tidy it
    # up, which is the defect this check exists for wearing a different hat: the clamp would
    # be holding the top while the height kept the 200px and the bottom slid.
    if after["box"]["y"] != 0:
        bad.append(f"resize n: top is {after['box']['y']}, want 0 at the desktop edge")
    # `before["placed"]` because the gesture seeds from it. The `moveTo`/`sizeTo` above puts
    # the window fully inside the desktop, so the two agree here today; naming `placed`
    # removes the dependency on that setup staying in bounds.
    bottom_before = before["placed"]["y"] + before["placed"]["h"]
    bottom_after = after["box"]["y"] + after["box"]["h"]
    if not near(bottom_after, bottom_before):
        bad.append(
            f"resize n: the bottom edge moved {bottom_before} -> {bottom_after} while the "
            "top was clamped — the resize became a move"
        )
    return bad


def check_offscreen_clamp(page) -> list[str]:
    """A window dragged at the desktop edges keeps enough of itself to be grabbed back.

    `docs/windows.md` promises 72 px horizontally and a title bar never above the top. A
    documented guarantee with no check is the kind that stops being true quietly.
    """
    bad = []
    selector = win_selector(page, "readme")
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " w.moveTo(400, 200); w.sizeTo(400, 260); })()"
    )
    time.sleep(0.1)
    surface = probe(page)["surface"]

    for label, dx, dy in (
        ("right", 4000, 0),
        ("left", -4000, 0),
        ("up", 0, -4000),
        ("down", 0, 4000),
    ):
        bar = rect_of(page, f"{selector} .window-title")
        if bar is None:
            return [f"clamp {label}: no title bar laid out"]
        drag(page, bar["x"] + 40, bar["y"] + bar["h"] / 2, dx, dy, steps=10)
        win = need(probe(page), "readme")
        # `placed`, not `box`: the clamp is the subject, and `box` keeps the 4000px the drag
        # asked for. Reading the request here measures a window nobody can see.
        placed = win["placed"]

        # Horizontally: some of the window has to remain over the desktop.
        visible = min(placed["x"] + placed["w"], surface["w"]) - max(placed["x"], 0)
        if visible < MIN_VISIBLE - 2:
            bad.append(
                f"clamp {label}: only {round(visible)}px of the window is over the desktop, below the {MIN_VISIBLE}px `docs/windows.md` promises"
            )
        if placed["y"] < 0:
            bad.append(f"clamp {label}: title bar is at y={placed['y']}, above the desktop")
        # And the title bar has to be on the desktop, not under the taskbar.
        if placed["y"] > surface["h"] - 20:
            bad.append(
                f"clamp {label}: title bar at y={placed['y']} is below the desktop's "
                f"{round(surface['h'])}px"
            )
        # The drag must not have resized anything. On `box`, because this asks what the move
        # gesture wrote: the clamp is allowed to cap a size to the desktop, a move is not.
        if win["box"]["w"] != 400 or win["box"]["h"] != 260:
            bad.append(
                f"clamp {label}: the window resized to {win['box']['w']}x{win['box']['h']}"
            )
    return bad


def check_resize_oversize(page) -> list[str]:
    """Resizing a window that extends past the desktop must not move the anchored edge.

    The regression this exists for survived the first edge-based rewrite: `clamp()` capped
    the size to the desktop while leaving the position alone, so the anchored edge absorbed
    the cap. `resize n at desktop edge` could not see it, because that check starts with the
    window fully inside the desktop and the cap never binds.

    Two measured cases, both from the review that found it:
      north — a window hanging below the desktop lost 90px off its bottom edge;
      west  — a window extending past the right edge lost 110px off its right edge.
    """
    bad = []
    selector = win_selector(page, "readme")
    surface = probe(page)["surface"]

    # North: put the bottom edge below the desktop, then drag the top edge up hard.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " w.moveTo(300, 500); w.sizeTo(460, 400); })()"
    )
    time.sleep(0.1)
    before = need(probe(page), "readme")
    if before["box"]["y"] + before["box"]["h"] <= surface["h"]:
        bad.append(
            "resize oversize: setup failed — the window does not hang below the desktop, "
            "so this check cannot bind the cap it is about"
        )
    grip = rect_of(page, f'{selector} .window-grip[data-dir="n"]')
    if grip is None:
        return bad + ["resize oversize: no north grip laid out"]
    drag(page, grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2, 0, -480, steps=10)
    after = need(probe(page), "readme")
    # `before["placed"]` on the left of every comparison below, `after["box"]` on the right:
    # the anchored edge is where the gesture started from, and the gesture starts from
    # `placed`. The `moveTo`/`sizeTo` setups keep `box` and `placed` equal on the way in —
    # both origins are inside the desktop — so this is the choice made explicit rather than a
    # behaviour change. The setup guard above is what would catch the day they diverge.
    bottom_before = before["placed"]["y"] + before["placed"]["h"]
    bottom_after = after["box"]["y"] + after["box"]["h"]
    if not near(bottom_after, bottom_before):
        bad.append(
            f"resize oversize n: the bottom edge moved {bottom_before} -> {bottom_after} "
            "while the top was dragged — the size cap moved the anchored edge"
        )

    # West: put the right edge past the desktop, then drag the left edge left hard.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " const s = window.webxp.surface.getBoundingClientRect();"
        " w.moveTo(Math.round(s.width) - 240, 200); w.sizeTo(240, 300); })()"
    )
    time.sleep(0.1)
    before = need(probe(page), "readme")
    grip = rect_of(page, f'{selector} .window-grip[data-dir="w"]')
    if grip is None:
        return bad + ["resize oversize: no west grip laid out"]
    drag(page, grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2, -1000, 0, steps=12)
    after = need(probe(page), "readme")
    right_before = before["placed"]["x"] + before["placed"]["w"]
    right_after = after["box"]["x"] + after["box"]["w"]
    if not near(right_after, right_before):
        bad.append(
            f"resize oversize w: the right edge moved {right_before} -> {right_after} "
            "while the left was dragged — the size cap moved the anchored edge"
        )
    return bad


def check_mount_failure(page) -> list[str]:
    """A program whose mount throws gets a window that says so, not a permanent marquee."""
    bad = []
    before = page.evaluate("window.webxp.windows.map((w) => w.id)")
    page.evaluate(
        "(() => { window.webxp.register({ id: 'boom', name: 'Boom', icon: 'info',"
        " width: 360, height: 220, status: true,"
        " mount() { throw new Error('mount exploded'); } }); })()"
    )
    try:
        # Registered after construction, so this also covers the Start menu re-render.
        in_menu = page.evaluate(
            "[...document.querySelectorAll('.start-item')]"
            ".some((b) => b.textContent.includes('Boom'))"
        )
        if not in_menu:
            bad.append(
                "mount failure: a program registered after construction never reached the "
                "Start menu, so it is launchable only from code"
            )

        page.evaluate("window.webxp.launch('boom')")
        time.sleep(1.0)

        shot = page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'boom');"
            " if (!w) return null;"
            " return { loading: w.bodyEl.classList.contains('is-loading'),"
            "          text: w.bodyEl.textContent,"
            "          status: w.statusEl ? w.statusEl.textContent : '' }; })()"
        )
        if shot is None:
            return bad + ["mount failure: the window was never created"]
        if shot["loading"]:
            bad.append("mount failure: the window is still showing the loading marquee")
        # An error path must name what failed. "Something went wrong" in a finished-looking
        # window is worse than the hang it replaces.
        if "mount exploded" not in shot["text"]:
            bad.append(
                f"mount failure: the window does not carry the error message — body reads "
                f"{shot['text'][:80]!r}"
            )
        if "boom" not in shot["text"] and "boom" not in shot["status"]:
            bad.append("mount failure: neither the body nor the status names the program")

        # And the failure must not take the rest of the desktop with it.
        others = page.evaluate(
            "document.querySelectorAll('.window-body.is-loading').length"
        )
        if others:
            bad.append(f"mount failure: {others} other windows left loading")
    finally:
        page.evaluate(
            "(() => { const keep = new Set(%s);"
            " for (const w of [...window.webxp.windows]) if (!keep.has(w.id)) w.close();"
            " window.webxp.programs.delete('boom');"
            " window.webxp.renderStartMenu(); window.webxp.renderDesktopIcons(); })()"
            % json.dumps(before)
        )
        time.sleep(0.4)
    # The thrown error is reported to the console on purpose: a swallowed failure is a
    # defect, so its absence is a failure too.
    return bad + drain_expecting(page, "mount exploded", "mount failure")


#: A program that comes up fine and then rejects, long after a reader could have shut it.
#:
#: 1500 ms because the close has to land inside the rejection, and the close cannot happen
#: until the window exists — which is past the mount, the loading floor and the two frames
#: `launch` waits before calling `ready`. Measured: the window is up and filled at 750 ms.
LATE_READY = """(() => {
  window.webxp.register({ id: 'late', name: 'Late', icon: 'info', width: 300, height: 200,
    mount: () => { const p = document.createElement('p'); p.textContent = 'up'; return p; },
    ready: () => new Promise((resolve, reject) => {
      setTimeout(() => reject(new Error('ready exploded after the close')), 1500);
    }) });
  return true;
})()"""


def check_ready_after_close(page) -> list[str]:
    """A `ready` that throws after its own window closed reports it, and writes nothing.

    `ready` is awaited across two frames and then across whatever the program does inside
    it, so it can throw after the reader has already shut the window. `failStart` is written
    for that: the console line is unconditional, because a swallowed failure is a defect and
    the program really did fail, and the window and the live region are touched only while
    the window is still there. Without the guard the reader is told "Late failed to start"
    about a window they closed, and content is written into a frame nobody can see.

    Three assertions, because each covers a different half of the guard. "No announcement"
    alone passes on a shell that reports nothing at all; "the console line is there" alone
    passes on a shell that also shouts at the reader.
    """
    bad = []
    before = page.evaluate("window.webxp.windows.map((w) => w.id)")
    page.evaluate(LATE_READY)
    try:
        watch_live(page)
        page.evaluate("window.webxp.launch('late').then(() => true)")
        # Past the mount and the loading floor, and well inside the rejection.
        time.sleep(0.75)
        up = page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'late');"
            " if (!w) return null;"
            " return { loading: w.bodyEl.classList.contains('is-loading'),"
            "          text: w.bodyEl.textContent.slice(0, 40) }; })()"
        )
        # The setup is the check. If the window is not up and filled at this point the close
        # below lands before `ready` was ever entered, the rejection has nothing to race,
        # and every assertion after it passes on a sequence that never happened.
        if up is None:
            return ["ready after close: the window never opened, so nothing closed during ready"]
        if up["loading"]:
            return [
                "ready after close: the window is still on its loading marquee at 750ms, so "
                f"`ready` has not been entered yet and the close cannot race it ({up['text']!r})"
            ]
        page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'late');"
            " if (w) w.close(); })()"
        )
        # Past the rejection, which is 1500ms from the launch.
        time.sleep(1.6)

        announced = said(page)
        if any("failed to start" in message for message in announced):
            bad.append(
                f"ready after close: the live region said {announced!r} — a reader who shut "
                "the window was told the program they are no longer looking at failed"
            )
        # And it did keep talking, or "no announcement about the failure" is satisfied by a
        # live region that stopped working.
        if not any("closed" in message for message in announced):
            bad.append(
                f"ready after close: the live region said {announced!r} and never mentioned "
                "the close — the assertion above would pass on a region that says nothing"
            )
        still = page.evaluate(
            "window.webxp.windows.filter((w) => w.programId === 'late').length"
        )
        if still:
            bad.append(
                f"ready after close: {still} 'late' windows are still open — the throwing "
                "`ready` put its window back"
            )
    finally:
        stop_watching_live(page)
        page.evaluate(
            "(() => { const keep = new Set(%s);"
            " for (const w of [...window.webxp.windows]) if (!keep.has(w.id)) w.close();"
            " window.webxp.programs.delete('late');"
            " window.webxp.renderStartMenu(); window.webxp.renderDesktopIcons(); })()"
            % json.dumps(before)
        )
        time.sleep(0.4)
    # The report is the other half of the guard: `failStart` logs before it looks at
    # `win.closed`, because the program did fail and a dropped error is a defect.
    return bad + drain_expecting(page, "ready failed for program late", "ready after close")


#: A program that can be launched more than once, for the cascade. Three windows of one
#: program, so every lane budget in `cascade` is the same number and the slots are
#: comparable — `lanes` is computed from the window's own height.
CASCADE_PROGRAM = """(() => {
  window.webxp.register({ id: 'lane', name: 'Lane', icon: 'info', width: 260, height: 180,
    mount: () => { const p = document.createElement('p'); p.textContent = 'lane'; return p; } });
  return true;
})()"""


def check_cascade_across_close(page) -> list[str]:
    """A cascade lane freed by a close is handed out again, and no two open windows share one.

    `cascadeIndex` reads the lanes off the open windows rather than counting launches,
    because a count is not the question. A counter only `launch` advances gives the same
    lane to a window built directly and to the next launched one; a count of open windows
    gives back the lane of a window that is still on screen as soon as an earlier one
    closes. Either way two windows open at one origin, which is the pile the cascade exists
    to prevent — and a fresh desktop cannot tell the two implementations apart, because
    both are right until something closes.

    Deliberately *not* asserted: that two bare `new XPWindow(...)` calls get different
    lanes. They cannot. A window never handed to the desktop is not in `windows`, so it is
    invisible to `cascadeIndex`, and `cascadeIndex`'s own docstring names that as one of the
    two cases it cannot answer. Asserting it would be asserting against the contract.
    """
    bad = []
    before = page.evaluate("window.webxp.windows.map((w) => w.id)")
    page.evaluate(CASCADE_PROGRAM)
    try:
        # Through `launch` on the desktop rather than the Start-menu helper: the helper
        # matches a menu item by its window title, and three windows of one program all
        # carry the same one.
        for _ in range(3):
            page.evaluate("window.webxp.launch('lane').then(() => true)")
            time.sleep(0.7)
        opened = page.evaluate(
            "window.webxp.windows.filter((w) => w.programId === 'lane')"
            ".map((w) => ({ id: w.id, slot: w.cascadeSlot }))"
        )
        if len(opened) != 3:
            return bad + [
                f"cascade across a close: {len(opened)} 'lane' windows opened, want 3 — "
                "there is no middle one to close"
            ]
        slots = [row["slot"] for row in opened]
        if len(set(slots)) != 3:
            bad.append(
                f"cascade across a close: three windows took lanes {slots} — two are in one "
                "lane before anything has even closed"
            )

        middle = opened[1]
        page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
            " if (w) w.close(); })()" % json.dumps(middle["id"])
        )
        time.sleep(0.6)

        # The invariant, stated as the set question `cascadeIndex` is written to answer.
        offered = page.evaluate("window.webxp.cascadeIndex()")
        taken = page.evaluate(
            "window.webxp.windows.map((w) => w.cascadeSlot).filter((s) => s !== undefined)"
        )
        if offered in taken:
            bad.append(
                f"cascade across a close: the next lane offered is {offered}, which is "
                f"already occupied — open lanes are {sorted(taken)}, and the next window "
                "will open on top of one that is there"
            )
        if offered != middle["slot"]:
            bad.append(
                f"cascade across a close: lane {middle['slot']} was freed by the close and "
                f"the next lane offered is {offered} — the freed lane is not being reused, "
                "which is a counter rather than a reading of what is open"
            )

        page.evaluate("window.webxp.launch('lane').then(() => true)")
        time.sleep(0.7)
        fourth = page.evaluate(
            "(() => { const w = window.webxp.windows.filter((w) => w.programId === 'lane');"
            " const last = w[w.length - 1];"
            " return last ? { id: last.id, slot: last.cascadeSlot } : null; })()"
        )
        if fourth is None:
            bad.append("cascade across a close: the fourth window did not open")
        elif fourth["slot"] != middle["slot"]:
            bad.append(
                f"cascade across a close: the fourth window took lane {fourth['slot']}, not "
                f"the {middle['slot']} the close freed"
            )

        # And the thing all of that is for: no pile. On `placed`, for the reason given in
        # `check_launch_all`.
        state = probe(page)
        origins = {(w["placed"]["x"], w["placed"]["y"]) for w in state["wins"]}
        if len(origins) != len(state["wins"]):
            bad.append(
                f"cascade across a close: {len(state['wins'])} windows at {len(origins)} "
                "distinct placed origins after a close and a launch"
            )
    finally:
        page.evaluate(
            "(() => { const keep = new Set(%s);"
            " for (const w of [...window.webxp.windows]) if (!keep.has(w.id)) w.close();"
            " window.webxp.programs.delete('lane');"
            " window.webxp.renderStartMenu(); window.webxp.renderDesktopIcons(); })()"
            % json.dumps(before)
        )
        time.sleep(0.4)
    return bad


def check_announce_states(page) -> list[str]:
    """Every state a window lands in is announced, and a race ends on the one it is in.

    `stateChanged` is the only place a transition is said out loud, synchronously and before
    any animation. Announced after an awaited zoom instead, it describes a state the window
    has already left: minimise, then restore inside the zoom, and the reader is told the
    window was minimised while it is back on screen.

    Both halves, because either alone is satisfied by a defect. Ending on `restored` passes
    on a shell that announces nothing but the last transition; "all three states announce"
    passes on a shell that announces them in the order they finish animating rather than the
    order they happened.
    """
    bad = []
    title = page.evaluate(
        "(window.webxp.windows.find((w) => w.programId === 'readme') || {}).titleText || ''"
    )
    if not title:
        return ["announce: no Read me window to change the state of"]
    watch_live(page)
    try:
        # Both inside one turn, so `restore` lands while `minimize`'s zoom is still awaited.
        page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
            " w.minimize(); w.restore(); })()"
        )
        time.sleep(SETTLE_S + 0.3)
        race = said(page)
        if len(race) < 2 or "minimized" not in race[0]:
            bad.append(
                f"announce: a minimise followed by a restore said {race!r} — the minimise "
                "went unsaid, so a reader who cannot see the taskbar was not told"
            )
        if not race or "restored" not in race[-1]:
            bad.append(
                f"announce: the race ended on {race!r} — the last thing said is not the "
                "state the window is in, which is the announcement arriving after the zoom"
            )
        # And the window is really back, or "ends on restored" is a lie the region is telling.
        win = need(probe(page), "readme")
        if win["state"] != "normal" or win["hidden"]:
            bad.append(
                f"announce: the region says restored but the window is {win['state']!r} "
                f"hidden={win['hidden']} — the zoom's late `hidden` landed after the restore"
            )

        page.evaluate("window.__said = []")
        title_click(page, "readme", "Maximize")
        title_click(page, "readme", "Restore")
        both = said(page)
        for state_name in ("maximized", "restored"):
            if not any(state_name in message for message in both):
                bad.append(
                    f"announce: a maximise and a restore said {both!r}, with nothing for "
                    f"{state_name!r} — `SAID` has an entry for every state so that no "
                    "transition passes unsaid"
                )
        if both and title not in both[0]:
            bad.append(
                f"announce: {both[0]!r} does not name the window — on a desktop of five "
                "cascaded windows, which one changed is the whole message"
            )
    finally:
        stop_watching_live(page)
    return bad


def check_reentrant_gesture(page, *, invert: bool = False) -> list[str]:
    """A second pointer arriving mid-drag takes the gesture over; only its release ends it.

    `beginGesture` ends the gesture in flight before starting the new one, because its three
    listeners only answer to their own pointer id — left attached they leak three per
    re-entry for the window's lifetime. What the reader must not be able to reach is a
    window still carrying `is-moving`, or a `drag` that outlives every pointer, because the
    window is then stuck to a pointer that is not there.

    Driven with a real second pointer, through touch emulation, and this is the only check
    in the suite that is. A synthetic `PointerEvent` cannot stand in: its `pointerId` is not
    an active pointer, so `setPointerCapture` throws `NotFoundError` out of `beginGesture`
    halfway through, and the measurement that comes back — `drag === null`, no `is-moving` —
    is the crash tidying up rather than the re-entry being handled. Measured: that route
    reports exactly the state a correct re-entry would, with an uncaught exception beside it.

    What is *not* asserted, because the measurement says otherwise: that releasing the
    first pointer ends the gesture. It does not, and it should not — the second pointer is
    still down and the window correctly follows it. Measured at each step, with the title
    bar grabbed by the mouse and then by a touch: `drag.id` 1 -> 2, the mouse released with
    `drag.id` still 2 and `is-moving` still on, the window following the touch from x=148 to
    x=248, and only the touch's own release clearing both.
    """
    bad = []
    selector = win_selector(page, "readme")
    bar = rect_of(page, f"{selector} .window-title")
    if bar is None:
        return ["re-entrant gesture: Read me has no laid-out title bar"]
    x, y = bar["x"] + 40, bar["y"] + bar["h"] / 2
    second_x = bar["x"] + bar["w"] / 2

    def gesture_state() -> dict:
        return page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
            " return { drag: w.drag ? w.drag.id : null,"
            "          moving: w.el.classList.contains('is-moving'),"
            "          box: { ...w.box } }; })()"
        )

    page.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    try:
        mouse(page, "mousePressed", x, y, buttons=1)
        mouse(page, "mouseMoved", x + 40, y, buttons=1)
        first = gesture_state()
        # The setup is an assertion: without a live gesture there is nothing to re-enter,
        # and every line below would be measuring a window nobody is holding.
        if first["drag"] is None or not first["moving"]:
            return [
                f"re-entrant gesture: the first pointer did not start a gesture ({first!r}) "
                "— there is nothing for a second pointer to re-enter"
            ]

        page.call(
            "Input.dispatchTouchEvent", type="touchStart",
            touchPoints=[{"x": float(second_x), "y": float(y), "id": 7}],
        )
        time.sleep(0.1)
        second = gesture_state()
        if second["drag"] is None or second["drag"] == first["drag"]:
            bad.append(
                f"re-entrant gesture: a second pointer left the gesture on "
                f"{second['drag']!r} (it was {first['drag']!r}) — the re-entry did not take "
                "the window over, so the first pointer's listeners are still the live ones"
            )

        # Releasing the pointer that no longer owns the gesture must change nothing. This is
        # the `endEvent.pointerId !== gesture.id` guard: without it, lifting one finger of
        # two drops the window mid-drag.
        mouse(page, "mouseReleased", x + 40, y, buttons=0)
        time.sleep(0.15)
        handed = gesture_state()
        if handed["drag"] != second["drag"] or not handed["moving"]:
            bad.append(
                f"re-entrant gesture: releasing the pointer that does not own the gesture "
                f"ended it ({handed!r}) — the second pointer is still down and the window "
                "stopped following it"
            )
        page.call(
            "Input.dispatchTouchEvent", type="touchMove",
            touchPoints=[{"x": float(second_x + 100), "y": float(y + 30), "id": 7}],
        )
        time.sleep(0.15)
        followed = gesture_state()
        if followed["box"] == handed["box"]:
            bad.append(
                f"re-entrant gesture: the window did not follow the second pointer — box is "
                f"still {followed['box']!r} after a 100px move"
            )

        page.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        time.sleep(0.15)
        done = gesture_state()
        if invert:
            # --prove-failure. The gesture did end; asserting that it did not must fail.
            if done["drag"] is None and not done["moving"]:
                bad.append(
                    "re-entrant gesture[--prove-failure]: the owning pointer's release "
                    f"cleared the gesture as it should ({done!r}) — the inverted assertion "
                    "failed, so this check can fail"
                )
            return bad
        if done["drag"] is not None or done["moving"]:
            bad.append(
                f"re-entrant gesture: every pointer is up and the window is still held "
                f"({done!r}) — it is stuck to a pointer that is not there"
            )
    finally:
        # Whatever happened above, no pointer may be left down: the next check drags the same
        # window.
        try:
            page.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        except CDPError:
            pass
        mouse(page, "mouseReleased", x + 40, y, buttons=0)
        page.call("Emulation.setTouchEmulationEnabled", enabled=False)
        time.sleep(0.1)
    return bad


#: Two desktops in scratch roots: one destroyed, one left running.
#:
#: `.desktop` is `position: fixed; inset: 0 0 var(--taskbar-h) 0`, so a scratch desktop's
#: surface tracks the viewport whatever its root is — which is what makes the resize
#: observable. The roots are `visibility: hidden` so neither paints or hit-tests over the
#: real desktop, and both are removed before the check returns: a second desktop builds its
#: own `#webxp-live`, `#start` and `.task-button`s, and `PROBE` counts task buttons
#: document-wide.
SCRATCH_DESKTOPS = """(async () => {
  // The class off the instance the page is running, not a fresh `import()` of
  // `/src/webxp.mjs`. Two reasons, and the second is the one that matters: an import is a
  // fetch against the single-threaded `http.server` and has been seen to fail
  // intermittently for one module while `curl` served it 200, and a module reached any way
  // other than the one the page used is not necessarily the one the page is using. A check
  // that destroys a `Desktop` the shell never built would pass on a shell whose own
  // `destroy` is broken.
  const Desktop = window.webxp.constructor;
  if (typeof Desktop !== 'function') return { error: 'window.webxp has no constructor' };
  const make = (id) => {
    const root = document.createElement('div');
    root.id = id;
    root.style.visibility = 'hidden';
    document.body.append(root);
    const desktop = new Desktop({ root, name: id, minLoadMs: 0 });
    desktop.register({ id: 'scratch', name: 'Scratch', icon: 'info', width: 240, height: 160,
      mount: () => { const p = document.createElement('p'); p.textContent = id; return p; } });
    return { root, desktop };
  };
  window.__scratch = [make('scratch-destroyed'), make('scratch-running')];
  await Promise.all(window.__scratch.map((s) => s.desktop.launch('scratch')));
  for (const s of window.__scratch) {
    if (!s.desktop.windows.length) return { error: 'a scratch desktop opened no window' };
    s.desktop.windows[0].moveTo(1000, 600);
    s.desktop.windows[0].sizeTo(240, 160);
  }
  window.__scratch[0].desktop.destroy();
  return { error: '', ...window.__readScratch() };
})()"""

READ_SCRATCH = """window.__readScratch = () => {
  const read = (s) => ({
    surface: Math.round(s.desktop.surface.getBoundingClientRect().width),
    placed: { ...s.desktop.windows[0].placed },
  });
  return { destroyed: read(window.__scratch[0]), running: read(window.__scratch[1]) };
};
true"""


def check_desktop_destroy(page) -> list[str]:
    """`Desktop.destroy()` stops the resize listener, and nothing else stops it.

    A desktop reaches `window` and `document` for the resize and for the clicks that close
    the Start menu, and runs a timer for the clock. None of that goes away with the root
    element, so a page that builds a second desktop otherwise leaves the first re-clamping
    its windows on every resize for as long as the page lives — moving windows in a desktop
    nobody is looking at, and holding every node in it alive.

    Nothing in this suite built a second `Desktop` before this check, which is why the
    teardown had no cover at all.

    The second desktop is the negative control and it is inside the check, not beside it: a
    destroyed desktop whose windows do not move proves nothing unless a desktop that was
    *not* destroyed does move. Without it the check passes on a resize that never reached
    either of them. Measured on a 1280x820 -> 520x420 shrink: the destroyed desktop's window
    stayed at placed (1000, 600) and the running one's moved to (448, 362), with both
    surfaces reporting the new 520px width.
    """
    bad = []
    width, height = VIEWPORT
    small_w, small_h = SHRUNK_VIEWPORT
    # Inside the `try`, both of them: the builder appends two roots to the document before
    # it can fail, and a leaked scratch desktop puts a second `#webxp-live` and a second set
    # of `.task-button`s in the page for every check after this one.
    try:
        page.evaluate(READ_SCRATCH)
        built = page.evaluate(SCRATCH_DESKTOPS, await_promise=True)
        if built.get("error"):
            return [f"desktop destroy: {built['error']}"]
        set_viewport(page, small_w, small_h)
        after = page.evaluate("window.__readScratch()")
        # The shrink has to have reached both surfaces, or neither half below means anything.
        for which in ("destroyed", "running"):
            if abs(after[which]["surface"] - small_w) > 20:
                return bad + [
                    f"desktop destroy: the {which} desktop's surface is "
                    f"{after[which]['surface']}px after the viewport was forced to {small_w} "
                    "— the shrink never reached it, so nothing here measures what it names"
                ]
        if after["running"]["placed"] == built["running"]["placed"]:
            return bad + [
                "desktop destroy: the desktop that was NOT destroyed left its window at "
                f"{after['running']['placed']!r} too — the resize reached neither desktop, "
                "so the destroyed one standing still proves nothing"
            ]
        if after["destroyed"]["placed"] != built["destroyed"]["placed"]:
            bad.append(
                "desktop destroy: the destroyed desktop moved its window from "
                f"{built['destroyed']['placed']!r} to {after['destroyed']['placed']!r} — "
                "`destroy()` left the resize listener attached, so a replaced desktop keeps "
                "re-clamping windows nobody can see"
            )
    finally:
        page.evaluate(
            "(() => { for (const s of window.__scratch || []) {"
            " s.desktop.destroy(); s.root.remove(); }"
            " delete window.__scratch; delete window.__readScratch; return true; })()"
        )
        set_viewport(page, width, height)
    return bad


#: Render every icon and every filetype icon, and report each `var()` presentation
#: attribute next to the token it names and the value the browser computed for it.
#:
#: `icons.mjs` states the premise as a fact — "a presentation attribute is parsed as CSS, so
#: `var()` resolves in it" — and nine of the set's fills and strokes are written that way.
#: Nothing measured it. If it were false the icons would draw in `fill`'s initial value,
#: which is black on black, and every icon in the shell would be a silhouette.
#:
#: The control is in the same round trip: a `fill` naming a token that does not exist. If
#: that does not come back black then an unresolved `var()` is not black here, and the
#: assertions this returns are void rather than passing.
ICON_TOKENS = """(async () => {
  // An `import()`, and the only one in the new checks. There is no global for the icon set
  // — `window.webxp` publishes the desktop, not `icons.mjs` — and the names are what let
  // this cover an icon no open window happens to be showing. It is a registry hit rather
  // than a fetch, because `window.mjs` already loaded this exact URL, which is why it is
  // acceptable here: a fresh fetch against the single-threaded `http.server` is the thing
  // that has been seen to fail intermittently. If this check ever fails intermittently,
  // that is the first cause to rule out, not the assertions below.
  //
  // The icons already in the document are swept as well, so the check still says something
  // about the nodes the reader is actually looking at and not only about a set built here.
  const icons = await import('/src/icons.mjs');
  const ctx = document.createElement('canvas').getContext('2d');
  //: One notation for every colour, via a `fillStyle` round trip, so `rgb(10, 36, 106)` off
  //: `getComputedStyle` and `#0a246a` off a custom property compare equal.
  //:
  //: The sentinel is `#010203` and not black on purpose. An unparseable value leaves
  //: `fillStyle` untouched, which is the only signal the canvas gives — so a black sentinel
  //: reports *black itself* as unparseable, and black is precisely the value this check
  //: exists to catch. Measured: with `#000` as the sentinel the control's own
  //: `rgb(0, 0, 0)` came back unreadable and the check failed on its own premise.
  //: `COLOUR_READER` above probes the same way, for the same reason. The two sentinels are
  //: separate on purpose: each only has to be a colour its own sweep never meets, and
  //: neither reads the other's value.
  const UNPARSEABLE = '#010203';
  const norm = (value) => {
    const text = String(value || '').trim();
    if (!text) return null;
    ctx.fillStyle = UNPARSEABLE;
    ctx.fillStyle = text;
    return ctx.fillStyle === UNPARSEABLE && text !== UNPARSEABLE ? null : ctx.fillStyle;
  };
  const host = document.createElement('div');
  host.style.position = 'fixed';
  host.style.left = '-9999px';
  host.style.top = '0';
  const control = document.createElement('div');
  control.style.cssText = host.style.cssText;
  // Marked so the live-desktop sweep below can tell the shell's own icons from the two
  // containers this check puts in the document. Without it the control's deliberately
  // broken token is reported as a defect on the live desktop.
  host.dataset.iconProbe = '1';
  control.dataset.iconProbe = '1';
  document.body.append(host, control);
  try {
    const names = [...icons.ICON_NAMES,
                   ...icons.FILETYPE_NAMES.map((f) => icons.iconFor({ filetype: f }))];
    for (const name of names) host.append(icons.icon(name, 16));

    const probe = icons.icon('window', 16);
    probe.firstChild.setAttribute('fill', 'var(--webxp-no-such-token)');
    control.append(probe);

    const root = getComputedStyle(document.documentElement);
    const rows = [];
    const sweep = (nodes, where) => {
      for (const node of nodes) {
        const computed = getComputedStyle(node);
        for (const property of ['fill', 'stroke']) {
          const attribute = node.getAttribute(property);
          if (!attribute || !attribute.startsWith('var(')) continue;
          const token = attribute.slice(4, attribute.indexOf(')')).trim();
          rows.push({
            node: node.tagName, property, attribute, token, where,
            declared: norm(root.getPropertyValue(token)),
            computed: norm(computed[property]) || String(computed[property]),
          });
        }
      }
    };
    sweep(host.querySelectorAll('*'), 'the icon set');
    sweep([...document.querySelectorAll('.xp-icon *')]
            .filter((node) => !node.closest('[data-icon-probe]')), 'the live desktop');
    const black = norm('#000');
    return { icons: names.length,
             live: document.querySelectorAll('.xp-icon').length, rows, black,
             control: norm(getComputedStyle(probe.firstChild).fill)
                      || String(getComputedStyle(probe.firstChild).fill) };
  } finally {
    host.remove();
    control.remove();
  }
})()"""


def check_icon_tokens(page) -> list[str]:
    """Every `var()` in an icon's fill or stroke resolves to the token it names.

    `icons.mjs` takes its palette from `theme.css` by writing `fill="var(--title-a)"` on the
    shapes, so a colour the theme already names is not retyped. That rests on `var()` being
    substituted in an SVG presentation attribute — which the file asserts in a comment and
    nothing measured. An engine that does not substitute paints `fill`'s initial value, and
    the whole icon set goes black on black: legible as a shape, wrong in every colour, and
    invisible to any assertion that an icon is present and has a box.

    The premise holds here and is now asserted rather than assumed: Chrome does substitute
    `var()` in a presentation attribute — `var(--title-a)` computes to `rgb(10, 36, 106)`.

    Measured with Read me and Notepad open: 160 `var()` fills and strokes over 7 tokens
    (`--muted`, `--panel`, `--raised`, `--rule`, `--start-a`, `--title-a`, `--title-b`), 46
    of them in the 18 icons rendered here and 114 across the 70 `.xp-icon`s already on the
    desktop. Both halves, because `--panel` is the `readme` and `notepad` papers and neither
    is in the DOM unless that program is open — and because a set built inside this check
    says nothing about the nodes the reader is looking at.
    """
    found = page.evaluate(ICON_TOKENS, await_promise=True)
    bad = []
    if not found["rows"]:
        # Never a skip. A renamed attribute or a palette that stopped using tokens leaves
        # nothing to compare, and an empty comparison is not a passing one.
        return [
            f"icon tokens: none of the {found['icons']} icons in the set and none of the "
            f"{found['live']} on the desktop carries a `var()` fill or stroke — either the "
            "palette stopped coming from the theme or the attribute names changed, and there "
            "is nothing here to check"
        ]
    if not any(row["where"] == "the live desktop" for row in found["rows"]):
        return [
            f"icon tokens: the {found['live']} icons on the desktop carry no `var()` fill or "
            "stroke, so only the set built inside this check was measured — the nodes the "
            "reader is looking at went unasserted"
        ]
    # The premise, in the same round trip as the assertions that rest on it.
    if found["control"] != found["black"]:
        return [
            f"icon tokens: a fill of `var(--webxp-no-such-token)` computed to "
            f"{found['control']!r}, not {found['black']!r} — an unresolved `var()` is not "
            "black in this engine, so the comparisons below are not measuring what they claim"
        ]
    for row in found["rows"]:
        where = f"{row['node']} {row['property']}={row['attribute']} in {row['where']}"
        if row["declared"] is None:
            bad.append(
                f"icon tokens: {where} names {row['token']}, which `theme.css` does not "
                f"declare as a colour — it computed to {row['computed']!r}"
            )
            continue
        if row["computed"] != row["declared"]:
            bad.append(
                f"icon tokens: {where} computed to {row['computed']!r} but {row['token']} is "
                f"{row['declared']!r}"
                + (
                    " — the engine is not substituting `var()` in a presentation attribute, "
                    "so the icon set is drawing in black"
                    if row["computed"] == found["black"]
                    else ""
                )
            )
    return bad


def check_viewport_shrink(page) -> list[str]:
    """Shrinking the desktop pulls windows back into reach.

    Without a resize listener a window is clamped only when it moves, so a window at
    x=1000 on a desktop that becomes 520 wide stays there — off the surface, with the title
    bar that is its only move handle off it too, and no route back.
    """
    bad = []
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " w.moveTo(1000, 600); w.sizeTo(240, 160); })()"
    )
    time.sleep(0.1)
    before = need(probe(page), "readme")
    width, height = VIEWPORT
    small_w, small_h = SHRUNK_VIEWPORT
    try:
        set_viewport(page, small_w, small_h)
        state = probe(page)
        win = need(state, "readme")
        surface = state["surface"]
        # The shrink has to have reached the desktop. `setDeviceMetricsOverride` forces the
        # layout viewport; it does not stop an element from exceeding it, so a `min-width` on
        # `.desktop` would leave the surface its old size and every assertion below would be
        # measured against the viewport this check was trying to leave — passing whether the
        # resize listener exists or not.
        #
        # Width exactly, height only as "smaller than it was": the surface is the viewport
        # less the taskbar, and copying the taskbar's height here would be a second
        # definition of a token this check does not otherwise need.
        if abs(surface["w"] - small_w) > 20 or surface["h"] >= height:
            return bad + [
                f"viewport shrink: the desktop is {round(surface['w'])}x{round(surface['h'])} "
                f"after the viewport was forced to {small_w}x{small_h} — the shrink never "
                "reached the surface, so nothing here measures what it names"
            ]
        # `placed`, not `box`. `box` is still the 1000,600 the setup asked for — `apply()`
        # never writes it back — so measuring it reports -480px whether the listener ran or
        # not, which is the one number `docs/review.md` records as the signature of the
        # listener being *absent*. The check could not tell the two apart in either
        # direction.
        placed, was = win["placed"], before["placed"]
        visible = min(placed["x"] + placed["w"], surface["w"]) - max(placed["x"], 0)
        # Two defects reach these assertions and they want different reading, so the message
        # names which. The discriminator is `placed` against where it was *before the
        # shrink*, not against `box`: compared to `box` the "nothing ran" branch is
        # unreachable at any viewport where the window was already clamped on the way in —
        # at a 1280x600 `VIEWPORT` the setup's y=600 is clamped to 542 before the shrink, so
        # a deleted listener would be reported as the clamp's arithmetic. Within a pixel
        # rather than equal, because `clampBox` rounds.
        #
        # The probe cannot see *why* nothing ran, so that branch names both candidates
        # rather than picking one.
        untouched = abs(placed["x"] - was["x"]) <= 1 and abs(placed["y"] - was["y"]) <= 1
        cause = (
            f"the placement has not moved off {was['x']},{was['y']} since the desktop shrank "
            "— either the resize listener never fired or the clamp is a no-op"
            if untouched
            else f"the clamp moved it from {was['x']},{was['y']} and still left it there"
        )
        if visible < MIN_VISIBLE - 2:
            bad.append(
                f"viewport shrink: the window sits at x={placed['x']} with {round(visible)}px "
                f"of {MIN_VISIBLE} over a {round(surface['w'])}px desktop; {cause}"
            )
        if placed["y"] > surface["h"] - 20:
            bad.append(
                f"viewport shrink: the title bar sits at y={placed['y']}, below the "
                f"{round(surface['h'])}px desktop; {cause}"
            )
    finally:
        set_viewport(page, width, height)
    return bad


def check_viewport_round_trip(page) -> list[str]:
    """A desktop that shrinks and grows back returns the window to where it was put.

    The counterpart to `viewport shrink`, and the reason `apply()` derives `placed` from
    `box` instead of writing the clamp back into it: the clamp belongs to the surface the
    window happens to be on, not to the window. Baking it in destroys the position the
    reader chose and there is no undo — and dragging a browser's corner in and out again is
    one gesture, not two deliberate ones.

    Asserted on `rect`. `box` is the intent and would come back unchanged whether the
    element followed it or not; `placed` is recomputed from `box` by the same line this
    check is about, so reading it is the assertion agreeing with itself. `rect` is the only
    one of the three that answers "is the window back where the reader left it".
    """
    bad = []
    width, height = VIEWPORT
    small_w, small_h = SHRUNK_VIEWPORT
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " w.moveTo(900, 520); w.sizeTo(300, 200); })()"
    )
    time.sleep(0.1)
    before = need(probe(page), "readme")
    try:
        set_viewport(page, small_w, small_h)
        squeezed = need(probe(page), "readme")
        # The negative control, and it has to be inside the check rather than beside it: if
        # the narrow desktop does not move the window, the return proves nothing, because
        # nothing went anywhere. A `SHRUNK_VIEWPORT` wide enough to hold the setup, or a
        # clamp that stopped firing, would otherwise leave this reporting a clean round trip
        # over a window that never left.
        if near(squeezed["rect"]["x"], before["rect"]["x"]) and near(
            squeezed["rect"]["y"], before["rect"]["y"]
        ):
            return bad + [
                f"viewport round trip: the window is still laid out at "
                f"{squeezed['rect']['x']},{squeezed['rect']['y']} on a "
                f"{small_w}x{small_h} viewport — the narrow desktop did not move it, so "
                "the return below would prove nothing"
            ]
    finally:
        set_viewport(page, width, height)

    after = need(probe(page), "readme")
    for axis in GEOMETRY_KEYS:
        if not near(after["rect"][axis], before["rect"][axis]):
            bad.append(
                f"viewport round trip: {axis} came back {after['rect'][axis]}, left from "
                f"{before['rect'][axis]} (it was squeezed to {squeezed['rect'][axis]}) — the "
                "clamp was written into the window instead of derived from the desktop, so "
                "shrinking the browser and widening it again lost the position"
            )
    return bad


def check_link_schemes(page) -> list[str]:
    """A cell link to a scheme that is not http, https or mailto is refused."""
    result = page.evaluate(
        "(async () => {"
        " const { table } = await import('/src/widgets.mjs');"
        " const probe = (href) => {"
        "   const t = table({ columns: ['a', 'b'], rows: [['row', ['click me', href]]] });"
        "   const a = t.querySelector('tbody a');"
        "   return { link: !!a, text: t.querySelector('tbody td').textContent }; };"
        " return { js: probe('javascript:window.__webxpPwned = 1'),"
        "          data: probe('data:text/html,<script>1</script>'),"
        "          https: probe('https://example.com/'),"
        "          relative: probe('/docs/windows.md'),"
        "          mail: probe('mailto:nobody@example.com') }; })()",
        await_promise=True,
    )
    bad = []
    for scheme in ("js", "data"):
        if result[scheme]["link"]:
            bad.append(f"link schemes: a {scheme}: href was rendered as a live link")
        if "click me" not in result[scheme]["text"]:
            bad.append(
                f"link schemes: a refused {scheme}: href dropped its text as well as its "
                f"link — cell reads {result[scheme]['text']!r}"
            )
    # The negative control: the allowlist must still let the real schemes through, or this
    # check would pass on a renderer that refuses every link.
    for scheme in ("https", "relative", "mail"):
        if not result[scheme]["link"]:
            bad.append(f"link schemes: a {scheme} href was refused, but should be allowed")
    if page.evaluate("window.__webxpPwned === 1"):
        bad.append("link schemes: the javascript: href executed")
    return bad + drain_expecting(page, "refused a cell link", "link schemes")


def check_app_loading(page) -> list[str]:
    """Every folder in the manifest became a registered program, in manifest order."""
    bad = []
    state = page.evaluate(
        "(() => ({ manifest: window.webxpApps ? null : 'webxpApps missing',"
        " loaded: window.webxpApps ? window.webxpApps.loaded : [],"
        " broken: window.webxpApps ? Object.keys(window.webxpApps.broken) : [],"
        " registered: [...window.webxp.programs.keys()],"
        " menu: [...document.querySelectorAll('.start-item')].map((b) => b.textContent) }))()"
    )
    if state["manifest"]:
        return [f"app loading: {state['manifest']}"]
    if state["broken"]:
        bad.append(f"app loading: these apps did not load: {state['broken']}")
    if sorted(state["loaded"]) != sorted(APPS_ON_DISK):
        bad.append(
            f"app loading: loaded {sorted(state['loaded'])}, the folders on disk are "
            f"{sorted(APPS_ON_DISK)}"
        )
    # Manifest order is the Start menu's order. Imports resolve in whatever order they
    # finish, so without ordered registration the menu would shuffle between loads.
    if state["registered"] != APPS_ON_DISK:
        bad.append(
            f"app loading: registered in order {state['registered']}, want manifest order "
            f"{APPS_ON_DISK}"
        )
    if len(state["menu"]) != len(APPS_ON_DISK):
        bad.append(f"app loading: {len(state['menu'])} Start menu rows for {len(APPS_ON_DISK)} apps")
    return bad


def check_app_filetypes(page) -> list[str]:
    """A filetype picks the icon, an explicit icon overrides it, and a bad one is rejected.

    The point of a filetype is that every app of one filetype looks the same, so the thing worth
    asserting is that two map apps resolved to the *same* icon — not merely that each
    resolved to something.
    """
    result = page.evaluate(
        "(async () => {"
        " const { validate } = await import('/src/apps.mjs');"
        " const { iconFor, FILETYPES, FILETYPE_NAMES, ICON_NAMES } = await import('/src/icons.mjs');"
        " return {"
        "   byFiletype: iconFor({ filetype: 'map' }),"
        "   override: iconFor({ filetype: 'map', icon: 'check' }),"
        "   neither: iconFor({}),"
        "   filetypes: FILETYPE_NAMES,"
        "   iconByFiletype: Object.fromEntries("
        "     Object.entries(FILETYPES).map(([k, v]) => [k, v.icon])),"
        "   shapeNames: ICON_NAMES,"
        "   mapIcon: FILETYPES.map.icon,"
        "   badFiletype: validate({ id: 'x', name: 'X', mount(){}, filetype: 'nope' }, 'x'),"
        "   goodFiletype: validate({ id: 'x', name: 'X', mount(){}, filetype: 'map' }, 'x'),"
        "   registered: [...window.webxp.programs.entries()]"
        "     .map(([id, p]) => [id, p.filetype || null, p.icon]) }; })()",
        await_promise=True,
    )
    bad = []
    # Pinned to the literal, not to the table being tested.
    #
    # This read `result["mapIcon"]`, which `iconFor` returns by reading the same entry — the
    # two sides were one expression. Typo `FILETYPES.map.icon` to `'mapp'` and `SHAPES[name]
    # || SHAPES.window` silently draws the generic window for every map app, while this
    # assertion and the set comparison below both pass.
    if result["byFiletype"] != "map":
        bad.append(
            f"app filetypes: filetype 'map' gave {result['byFiletype']!r}, want 'map'"
        )
    # And every registered filetype must name a shape that exists, which is what the
    # `|| SHAPES.window` fallback would otherwise hide.
    for name, icon_name in (result.get("iconByFiletype") or {}).items():
        if icon_name not in (result.get("shapeNames") or []):
            bad.append(
                f"app filetypes: filetype {name!r} names icon {icon_name!r}, which is not a "
                "drawn shape — it would silently fall back to the generic window"
            )
    # An explicit icon has to win, or an app cannot be an exception to its own filetype.
    if result["override"] != "check":
        bad.append(f"app filetypes: an explicit icon lost to the filetype — got {result['override']!r}")
    if result["neither"] != "window":
        bad.append(f"app filetypes: neither icon nor filetype gave {result['neither']!r}")
    if not result["badFiletype"]:
        bad.append("app filetypes: validate() accepted an unknown filetype")
    if result["goodFiletype"]:
        bad.append(f"app filetypes: validate() rejected a valid filetype — {result['goodFiletype']}")

    # Every map app must share one icon. This is the property the feature exists for, and the
    # one a per-app `icon` would let drift.
    maps = [row for row in result["registered"] if row[1] == "map"]
    if len(maps) < 2:
        bad.append(f"app filetypes: only {len(maps)} apps declare filetype 'map' — want at least 2 to compare")
    icons = {row[2] for row in maps}
    if len(icons) > 1:
        bad.append(f"app filetypes: map apps resolved to differing icons {icons}")
    elif maps and icons != {result["mapIcon"]}:
        bad.append(f"app filetypes: map apps resolved to {icons}, want {{{result['mapIcon']!r}}}")
    return bad


def check_autostart(page) -> list[str]:
    """`Desktop.autostart()` opens programs that ask for it, and skips the ones that do not.

    The documented key lost its only user when the read-me stopped autostarting, so it was
    documented, untested, and forbidden by `check_boot` all at once. This exercises the
    mechanism directly instead of relying on an app to carry it.
    """
    before = page.evaluate("window.webxp.windows.map((w) => w.id)")
    result = page.evaluate(
        "(async () => { const d = window.webxp;"
        " d.register({ id: 'auto-yes', name: 'Auto Yes', filetype: 'tool', autostart: true,"
        "   mount: () => document.createTextNode('yes') });"
        " d.register({ id: 'auto-no', name: 'Auto No', filetype: 'tool',"
        "   mount: () => document.createTextNode('no') });"
        " await d.autostart();"
        " return d.windows.map((w) => w.programId); })()",
        await_promise=True,
    )
    bad = []
    if "auto-yes" not in result:
        bad.append("autostart: a program with autostart: true was not opened by autostart()")
    if "auto-no" in result:
        bad.append("autostart: a program without autostart was opened anyway")
    page.evaluate(
        "(() => { const keep = new Set(%s);"
        " for (const w of [...window.webxp.windows]) if (!keep.has(w.id)) w.close();"
        " window.webxp.programs.delete('auto-yes');"
        " window.webxp.programs.delete('auto-no');"
        " window.webxp.renderStartMenu(); window.webxp.renderDesktopIcons(); })()"
        % json.dumps(before)
    )
    time.sleep(0.4)
    return bad


def check_app_styles(page) -> list[str]:
    """An app's stylesheet is linked once, applies, and is scoped to its own windows."""
    bad = []
    # Notepad declares `styles: ['app.css']`, whose only rule targets `.app-notepad`.
    launch(page, "notepad")
    probe_result = page.evaluate(
        "(() => { const wins = window.webxp.windows.filter((w) => w.programId === 'notepad');"
        " const links = [...document.querySelectorAll('link[rel=stylesheet]')]"
        "   .filter((l) => l.href.includes('/apps/notepad/app.css'));"
        " const area = wins.length ? wins[0].bodyEl.querySelector('.notepad-area') : null;"
        " const readme = window.webxp.windows.find((w) => w.programId === 'readme');"
        " return { links: links.length,"
        "          bodyClass: wins.length ? wins[0].bodyEl.className : '',"
        "          font: area ? getComputedStyle(area).fontFamily : '',"
        "          readmeClass: readme ? readme.bodyEl.className : '',"
        "          windows: wins.length }; })()"
    )
    if probe_result["links"] != 1:
        bad.append(
            f"app styles: {probe_result['links']} links to apps/notepad/app.css, want exactly "
            "1 — N windows of one app must share one stylesheet"
        )
    if "app-notepad" not in probe_result["bodyClass"]:
        bad.append(f"app styles: window body class is {probe_result['bodyClass']!r}, want app-notepad")
    # The rule only applies through the scoping class, so a monospace font here proves both
    # that the stylesheet loaded and that the class is what connected it.
    if "Lucida Console" not in probe_result["font"]:
        bad.append(
            f"app styles: the textarea font is {probe_result['font']!r} — apps/notepad/app.css "
            "did not apply"
        )
    if "app-notepad" in probe_result["readmeClass"]:
        bad.append("app styles: another app's window carries the app-notepad class")
    return bad


def check_broken_app(page) -> list[str]:
    """An app whose spec is wrong is reported, and still appears saying what is wrong.

    A broken app that is simply absent from the Start menu is the worst outcome: the question
    "did I forget the folder, or is the manifest stale, or did I misname it" has no answer on
    screen. `loadApp` registers an entry that explains itself instead.
    """
    bad = []
    result = page.evaluate(
        "(async () => {"
        " const { validate, loadApp } = await import('/src/apps.mjs');"
        # Straight at `validate`, which is the function the contract actually lives in.
        " const cases = {"
        "   idMismatch: validate({ id: 'other', name: 'X', mount(){} }, 'mine'),"
        "   noName: validate({ id: 'mine', mount(){} }, 'mine'),"
        "   noFill: validate({ id: 'mine', name: 'X' }, 'mine'),"
        "   bothFills: validate({ id: 'mine', name: 'X', mount(){}, frame: { src: 'a' } }, 'mine'),"
        "   badIcon: validate({ id: 'mine', name: 'X', mount(){}, icon: 'nope' }, 'mine'),"
        "   typo: validate({ id: 'mine', name: 'X', mount(){}, singelton: true }, 'mine'),"
        "   good: validate({ id: 'mine', name: 'X', mount(){} }, 'mine'),"
        " };"
        # And at a folder that does not exist, which is the manifest-is-stale case.
        " const missing = await loadApp('does-not-exist');"
        " return { cases, missingProblems: missing.problems.length,"
        "          missingHasProgram: typeof missing.program.mount === 'function' }; })()",
        await_promise=True,
    )
    for name in ("idMismatch", "noName", "noFill", "bothFills", "badIcon", "typo"):
        if not result["cases"][name]:
            bad.append(f"broken app: validate() accepted the {name} case")
    # The negative control: a valid spec must pass, or the validator could be rejecting
    # everything and every assertion above would still hold.
    if result["cases"]["good"]:
        bad.append(f"broken app: validate() rejected a valid spec — {result['cases']['good']}")
    if not result["missingProblems"]:
        bad.append("broken app: loading a folder that does not exist reported no problem")
    if not result["missingHasProgram"]:
        bad.append(
            "broken app: a folder that failed to load produced no program, so it would be "
            "silently absent from the Start menu"
        )
    # Asking for a folder that does not exist logs a 404, which `IGNORED` already filters
    # because the Base map app's tiles 404 at a pyramid's edge. No drain is needed, and
    # asserting on it would only re-test the filter: `missingProblems` above is what proves
    # the import went out and was reported.
    return bad


def check_iframe_app(page) -> list[str]:
    """A `frame:` app hosts its page, and clicking it raises its window.

    The shim is the whole reason `frame` is a first-class filetype: a pointer event inside an
    iframe does not reach the parent document, so without it a hosted app cannot be raised
    by clicking it.
    """
    bad = []
    launch(page, "webview")
    shape = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'webview');"
        " if (!w) return null;"
        " const frame = w.bodyEl.querySelector('iframe.app-frame');"
        " const shim = w.bodyEl.querySelector('.app-frame-shim');"
        " const r = frame ? frame.getBoundingClientRect() : null;"
        " return { hasFrame: !!frame, src: frame ? frame.src : '',"
        "          title: frame ? frame.title : '',"
        "          sandbox: frame ? frame.getAttribute('sandbox') : null,"
        "          w: r ? Math.round(r.width) : 0, h: r ? Math.round(r.height) : 0,"
        "          shimShown: shim ? getComputedStyle(shim).display : 'absent' }; })()"
    )
    if shape is None or not shape["hasFrame"]:
        return ["iframe app: no iframe in the Webview window"]
    if not shape["src"].endswith("/apps/webview/page.html"):
        bad.append(f"iframe app: src is {shape['src']!r}, want it resolved against the app folder")
    if not shape["title"]:
        bad.append("iframe app: the frame has no title, so it is an unnamed document in the tab order")
    if shape["sandbox"] is None:
        bad.append("iframe app: the declared sandbox was not applied")
    if shape["w"] < 100 or shape["h"] < 100:
        bad.append(f"iframe app: the frame laid out at {shape['w']}x{shape['h']}")
    # Focused window: no shim, so clicks reach the page.
    if shape["shimShown"] != "none":
        bad.append(
            f"iframe app: the shim is {shape['shimShown']} on the focused window, so clicks "
            "cannot reach the hosted page"
        )

    # Unfocused window: the shim must be there to catch the click that raises it.
    launch(page, "notepad")
    unfocused = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'webview');"
        " const shim = w.bodyEl.querySelector('.app-frame-shim');"
        " const r = shim.getBoundingClientRect();"
        " return { display: getComputedStyle(shim).display, active: w.el.classList.contains('is-active'),"
        "          w: Math.round(r.width), h: Math.round(r.height) }; })()"
    )
    if unfocused["active"]:
        bad.append("iframe app: Webview is still active after launching Notepad over it")
    elif unfocused["display"] == "none":
        bad.append(
            "iframe app: no shim on the unfocused window — clicking the hosted page would "
            "not raise its window"
        )
    elif unfocused["w"] < 100 or unfocused["h"] < 100:
        bad.append(f"iframe app: the shim laid out at {unfocused['w']}x{unfocused['h']}")

    # And clicking the shim must actually raise the window.
    #
    # Moved somewhere nothing else covers first. The cascade stacks every window near the
    # top-left, so the shim of an unfocused window is usually underneath the one that
    # unfocused it — and a click there lands on the wrong window and looks like the shim
    # failing. `moveTo` does not change focus, so the window stays unfocused.
    page.evaluate(
        "(() => { const s = window.webxp.surface.getBoundingClientRect();"
        " const w = window.webxp.windows.find((w) => w.programId === 'webview');"
        " w.moveTo(Math.round(s.width) - w.box.w - 20, Math.round(s.height) - w.box.h - 20); })()"
    )
    time.sleep(0.15)
    rect = rect_of(page, f"{win_selector(page, 'webview')} .app-frame-shim")
    if rect is None:
        return bad + ["iframe app: the shim has no box to click"]

    x = rect["x"] + rect["w"] / 2
    y = rect["y"] + rect["h"] / 2
    # What is actually under the pointer. Without this, a click that misses reports as a
    # product defect — which is exactly what happened the first time this check ran.
    on_top = page.evaluate(
        f"(() => {{ const e = document.elementFromPoint({x}, {y});"
        " return e ? e.className : 'nothing'; })()"
    )
    if "app-frame-shim" not in str(on_top):
        return bad + [
            f"iframe app: could not isolate the shim to click it — {on_top!r} is on top at "
            f"({round(x)}, {round(y)}). This is a harness problem, not a product one."
        ]

    mouse(page, "mousePressed", x, y, buttons=1)
    mouse(page, "mouseReleased", x, y)
    time.sleep(0.15)
    after = probe(page)
    if after["active"] != "webview":
        bad.append(
            f"iframe app: clicking the shim left {after['active']!r} active, not the "
            "window that was clicked"
        )
    return bad


#: The elements OpenTopoMap's own notice names, as a reader sees them
#: (https://opentopomap.org/about#verwendung). The data half credits OSM and SRTM, the
#: rendering half credits OpenTopoMap under CC-BY-SA; `OPENTOPO_CREDIT` in
#: `shared/map/basemap.mjs` is the English rendering of it. Dropping any one of these drops
#: a required credit, which is a licence breach and not a copy nit.
OPENTOPO_CREDIT_PARTS = ("OpenStreetMap", "SRTM", "OpenTopoMap", "CC-BY-SA")

#: How many `©` the notice carries: one for the data, one for the rendering.
OPENTOPO_CREDIT_MARKS = 2

#: How many links it carries: OSM's copyright page, OpenTopoMap, and the CC-BY-SA deed.
OPENTOPO_CREDIT_LINKS = 3

#: The parties the satellite preset's two services name in their own `copyrightText`, which is
#: where `ESRI_IMAGERY_CREDIT` and `ESRI_PLACES_CREDIT` in `shared/map/basemap.mjs` come from.
#: Re-read with:
#:   curl -s 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer?f=json'
#:   curl -s 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer?f=json'
#: `World_Imagery` names Esri, Vantor, Earthstar Geographics and the GIS User Community;
#: `World_Boundaries_and_Places` names Esri, HERE, Garmin, OpenStreetMap contributors and the
#: GIS user community. Both layers are drawn, so a reader on this preset has to be shown both
#: notices. Esri's own terms are unconfirmed (shared/map/README.md §Licensing), which makes a
#: missing name the worse case and not the lesser one; OSM's half is ODbL and required outright.
#: Cased as the services case it — "Community" in one notice, "community" in the other — so
#: this tuple asserts the capitalised form once, where the imagery notice carries it.
ESRI_CREDIT_PARTS = (
    "Esri",
    "Vantor",
    "Earthstar Geographics",
    "GIS User Community",
    "HERE",
    "Garmin",
    "OpenStreetMap",
)

#: How many links it carries: Esri on each of the two notices, and OSM's copyright page for
#: the reference layer's ODbL data.
ESRI_CREDIT_LINKS = 3


def switch_basemap(page, want: str) -> dict:
    """Click the Base map app's `want` preset and wait for that style to be installed.

    Identified by the style, not by the moment. `createBaseMap` stamps a serial into every
    style's `name` (`stamp`, `shared/map/basemap.mjs`) because `styledata` fires repeatedly
    as sprites, glyphs and tiles arrive and the *outgoing* style keeps firing it after
    `setStyle` — so `map.loaded()` can read true off the style being replaced, and
    `w.base.basemap` flips before `setStyle` is even called. Waiting for a `name` that
    differs from the one installed before the click is what cannot be satisfied by a stale
    event.

    Returns the last reading, with `settled` true only when the new style is in and loaded,
    and `why` describing the reading when it is not.
    """
    read = (
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'basemap');"
        " if (!w || !w.base) return { gone: true };"
        " const map = w.base.map;"
        " const style = map.getStyle();"
        " const attribEl = w.bodyEl.querySelector('.map-attrib');"
        " return { basemap: w.base.basemap, loaded: map.loaded(),"
        "   styleName: style ? String(style.name || '') : '',"
        "   layers: style ? style.layers.map((l) => l.id) : [],"
        # `textContent`, because the credit is prose to a reader: `&copy;` in the source is
        # a `©` on screen, and asserting the source string would pass while the element it
        # is supposed to be written into stayed empty.
        "   creditText: attribEl ? attribEl.textContent : '',"
        "   creditHtml: attribEl ? attribEl.innerHTML : '' }; })()"
    )
    before = page.evaluate(read)
    if not isinstance(before, dict) or before.get("gone"):
        raise HarnessError(f"switch_basemap({want!r}): no Base map window with a map in it")
    # `setBasemap` returns early when the preset is already active, so asking for a switch
    # that is not one would install no style and wait out the deadline below reporting a
    # failure in the app. That is a mistake in the caller, and it is named as one.
    if before["basemap"] == want:
        raise HarnessError(f"switch_basemap({want!r}): that preset is already the active one")

    clicked = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'basemap');"
        " const b = w.bodyEl.querySelector(%s);"
        " if (!b) return false; b.click(); return true; })()"
        % json.dumps(f'[data-basemap="{want}"]')
    )
    if not clicked:
        raise HarnessError(f"switch_basemap({want!r}): the app has no [data-basemap] button for it")

    deadline = time.monotonic() + 45
    state = before
    while time.monotonic() < deadline:
        state = page.evaluate(read)
        if state.get("gone"):
            raise HarnessError(f"switch_basemap({want!r}): the Base map window went away mid-switch")
        if (
            state["basemap"] == want
            and state["loaded"]
            and state["styleName"]
            and state["styleName"] != before["styleName"]
        ):
            state["settled"] = True
            return state
        time.sleep(0.5)
    state["settled"] = False
    state["why"] = (
        f"after 45s the app says {state['basemap']!r}, map.loaded() is {state['loaded']}, and "
        f"the installed style is named {state['styleName']!r} against {before['styleName']!r} "
        "before the click"
    )
    return state


def check_basemap_app(page) -> list[str]:
    """The ported project draws, in a window, at the window's size.

    This is the check the whole `ready` hook exists for. A map created while its container
    has no box sizes itself to 0x0, reports its style loaded, fetches every tile and draws
    nothing — and every assertion short of measuring the canvas passes over it.

    It reaches the real tile services. That is deliberate: there is no fixture that could
    tell us a source stopped answering. A network failure is reported as one rather than as a
    defect in the app.
    """
    bad = []
    launch(page, "basemap")

    # The map loads MapLibre, a style and its first tiles. Polled rather than slept through,
    # so a fast machine is not made to wait and a slow one is not cut off.
    deadline = time.monotonic() + 45
    state = None
    while time.monotonic() < deadline:
        state = page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'basemap');"
            " if (!w) return { stage: 'no window' };"
            " if (w.bodyEl.classList.contains('is-loading')) return { stage: 'loading' };"
            " if (!w.app) return { stage: 'no app state' };"
            " if (!w.base) return { stage: 'no map',"
            "   status: (w.bodyEl.querySelector('.map-status') || {}).textContent || '' };"
            " const map = w.base.map;"
            " const canvas = w.bodyEl.querySelector('.maplibregl-canvas');"
            " const holder = w.bodyEl.querySelector('.map');"
            # `.map-area` is what the canvas is supposed to fill. The selector matters: this
            # read `.basemap-root`, a class no element has carried since the map layer moved
            # to `shared/`, so `root` came back null and the comparison below — the one
            # assertion this check exists for — was skipped on every run while reporting ok.
            " const areaEl = w.bodyEl.querySelector('.map-area');"
            " const attribEl = w.bodyEl.querySelector('.map-attrib');"
            " const cr = canvas ? canvas.getBoundingClientRect() : null;"
            " const hr = holder ? holder.getBoundingClientRect() : null;"
            " const ar = areaEl ? areaEl.getBoundingClientRect() : null;"
            " const tr = attribEl ? attribEl.getBoundingClientRect() : null;"
            # What sits on top of the credit's own midpoint. If anything outside the credit is
            # there, it is covered — which is a licence problem, not a layout nit.
            #
            # Resolved through `closest`, for the reason spelled out on the same read in
            # `day_02_panel_sweep`: the credit's own links are unclassed `<a>` elements, and
            # reading `className` off one reports a cover that does not exist.
            " const onCredit = tr && tr.width"
            "   ? (() => { const top = document.elementFromPoint("
            "        tr.left + tr.width / 2, tr.top + tr.height / 2);"
            "      if (!top) return 'nothing';"
            "      const owner = top.closest('.map-attrib') ? attribEl : top;"
            "      return String(owner.className || owner.tagName); })()"
            "   : 'no credit';"
            " return { stage: map.loaded() ? 'ready' : 'style',"
            "   basemap: w.base.basemap,"
            "   canvas: cr ? { w: Math.round(cr.width), h: Math.round(cr.height) } : null,"
            "   canvasRect: cr ? { x: cr.left, y: cr.top, w: cr.width, h: cr.height } : null,"
            "   holder: hr ? { w: Math.round(hr.width), h: Math.round(hr.height) } : null,"
            "   area: ar ? { w: Math.round(ar.width), h: Math.round(ar.height) } : null,"
            "   areaBottom: ar ? Math.round(ar.bottom) : null,"
            "   creditBox: tr ? { w: Math.round(tr.width), h: Math.round(tr.height) } : null,"
            "   creditTop: tr ? Math.round(tr.top) : null,"
            "   onCredit: String(onCredit),"
            "   attrib: (attribEl || {}).innerHTML || '',"
            "   layers: map.getStyle() ? map.getStyle().layers.map((l) => l.id) : [],"
            "   status: (w.bodyEl.querySelector('.map-status') || {}).textContent || '' }; })()"
        )
        if state and state.get("stage") == "ready":
            break
        time.sleep(0.5)

    if not state or state.get("stage") != "ready":
        stage = (state or {}).get("stage", "unknown")
        said = (state or {}).get("status", "")
        return [
            f"basemap app: never became ready (stopped at {stage!r}"
            + (f", status line says {said!r}" if said else "")
            + "). If this machine has no network, that is the cause, not the app."
        ]

    # The regression this check is really about. A 0x0 canvas is what a map built before its
    # container was laid out looks like, and it is invisible to every other assertion here.
    if not state["canvas"] or state["canvas"]["w"] < 200 or state["canvas"]["h"] < 200:
        bad.append(
            f"basemap app: the canvas laid out at {state['canvas']} — the map was built "
            "before its container had a box"
        )
    # Against `.map-area`, not against `.map`. `.map` is the element a stylesheet accident can
    # collapse to 0px high while the canvas inside it keeps a stale size, so comparing the two
    # agrees with itself and proves nothing. The area is the room the map was given.
    #
    # Guarding on a selector is what let this assertion die silently once, so a missing area is
    # a failure rather than a skip.
    if not state["area"]:
        bad.append("basemap app: no .map-area — nothing to measure the canvas against")
    elif state["canvas"]:
        for axis in ("w", "h"):
            if abs(state["canvas"][axis] - state["area"][axis]) > 6:
                bad.append(
                    f"basemap app: canvas {axis} is {state['canvas'][axis]}px in a "
                    f"{state['area'][axis]}px area — the map is not filling its window"
                )

    # The credit is a licence term for all three sources: it has to be there, and it has to be
    # somewhere nothing can cover.
    #
    # Two assertions, because "nothing covers it" and "nothing can cover it" are different
    # claims and only the second is the invariant. `elementFromPoint` catches a credit that is
    # covered right now — but this app docks nothing over it, so moving the credit back onto
    # the canvas passed that assertion while re-creating the exact bug. What fails on it is the
    # geometry: a row of the column starts where the map area ends, and a credit that overlaps
    # the area is a credit in a stacking context the map controls, whatever is on top today.
    if not state["creditBox"] or state["creditBox"]["h"] < 8:
        bad.append(f"basemap app: the credit laid out at {state['creditBox']} — it must be visible")
    elif "map-attrib" not in state["onCredit"]:
        bad.append(
            f"basemap app: {state['onCredit']!r} sits on top of the credit — attribution is a "
            "licence term and cannot be covered"
        )
    elif state["areaBottom"] is not None and state["creditTop"] < state["areaBottom"] - 1:
        bad.append(
            f"basemap app: the credit starts at y={state['creditTop']} but the map area runs to "
            f"y={state['areaBottom']} — it is over the canvas, where a docked panel covers it"
        )
    if state["holder"] and (state["holder"]["w"] < 200 or state["holder"]["h"] < 200):
        bad.append(
            f"basemap app: the .map holder laid out at {state['holder']} — a rule it needs "
            "lost a specificity battle with MapLibre's own stylesheet"
        )

    # The compass rose has to fit inside its own button and be centred in it.
    #
    # This is the third time in this project that rescoping a stylesheet tied a specificity
    # battle and the generic rule won. Here `.map-root .map-zoom button` (two classes and a
    # type) beat `.map-root .map-compass` (two classes), so the compass stayed 28px with a
    # 34px rose inside it: overflowing three pixels a side and sitting four down and right,
    # because grid centring cannot centre an oversized item. Nothing else in the suite looks
    # inside a control, so nothing caught it — it was reported from a screenshot.
    compass = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'basemap');"
        " if (!w) return null;"
        " const b = w.bodyEl.querySelector('.map-compass');"
        " const svg = b && b.querySelector('svg');"
        " if (!b || !svg) return { missing: true };"
        " const br = b.getBoundingClientRect(), sr = svg.getBoundingClientRect();"
        " return { bw: Math.round(br.width), bh: Math.round(br.height),"
        "          sw: Math.round(sr.width), sh: Math.round(sr.height),"
        "          dx: +(sr.left + sr.width / 2 - (br.left + br.width / 2)).toFixed(1),"
        "          dy: +(sr.top + sr.height / 2 - (br.top + br.height / 2)).toFixed(1) }; })()"
    )
    if compass is None or compass.get("missing"):
        bad.append("basemap app: no compass button to measure")
    else:
        if compass["sw"] > compass["bw"] or compass["sh"] > compass["bh"]:
            bad.append(
                f"basemap app: the compass rose is {compass['sw']}x{compass['sh']} in a "
                f"{compass['bw']}x{compass['bh']} button — it overflows its own control"
            )
        if abs(compass["dx"]) > 1 or abs(compass["dy"]) > 1:
            bad.append(
                f"basemap app: the compass rose is off centre by ({compass['dx']}, "
                f"{compass['dy']})px"
            )

    # And it has to have painted. Every flag above can read healthy over a blank canvas:
    # MapLibre reported styleLoaded, tilesLoaded and 113 layers while drawing nothing at all.
    if state["canvasRect"]:
        size = shoot_clip(page, state["canvasRect"], SHOT_DIR / "basemap-canvas.png")
        if size < MAP_PAINT_FLOOR:
            bad.append(
                f"basemap app: a screenshot of the canvas compresses to {size} bytes, under "
                f"{MAP_PAINT_FLOOR} — the map reports loaded and painted nothing"
            )
    # Attribution is a licence term for every source in `basemap.mjs`, not a nicety.
    if "href" not in state["attrib"]:
        bad.append(f"basemap app: the attribution is {state['attrib'][:60]!r}, with no credit link")

    # The overlay has to survive a basemap switch. `setStyle` discards every layer added to
    # the old style, so a `draw` that is not re-run leaves a map that looks fine and has
    # silently lost its data. This is the original project's own key regression, carried over.
    overlay = {"clicked-point", "clicked-point-halo"}
    missing = overlay - set(state["layers"])
    if missing:
        bad.append(f"basemap app: overlay layers {sorted(missing)} missing before any switch")

    switched = switch_basemap(page, "satellite")
    if not switched["settled"]:
        bad.append(f"basemap app: the satellite switch did not take — {switched['why']}")
    else:
        missing = overlay - set(switched["layers"])
        if missing:
            bad.append(
                f"basemap app: overlay layers {sorted(missing)} were discarded by the basemap "
                "switch — draw() was not re-run"
            )
        # Satellite's credit, read off the page, to the same standard as geography's below.
        # This preset draws two Esri services with different party lists, and the two strings
        # it used to carry had already drifted apart: the preset named the GIS User Community
        # and linked esri.com, the imagery source named neither, and the reference layer drawn
        # over the top named nobody at all. A `href`-is-present assertion passed throughout.
        text = switched["creditText"]
        absent = [part for part in ESRI_CREDIT_PARTS if part not in text]
        if absent:
            bad.append(
                f"basemap app: the satellite credit reads {text!r}, with no "
                f"{', '.join(absent)} — Esri's two services name every one of those in their "
                "own copyrightText, and this preset draws both of them"
            )
        links = switched["creditHtml"].count("href")
        if links < ESRI_CREDIT_LINKS:
            bad.append(
                f"basemap app: the satellite credit carries {links} link(s), want "
                f"{ESRI_CREDIT_LINKS} — Esri on each notice, and OSM's copyright page for the "
                "reference layer's ODbL data"
            )

    # Geography's credit, read off the page rather than out of the module.
    #
    # `paintAttribution` writes only the active preset's string, so a preset nothing
    # switches to has its notice unrendered — and a rendering that never happens cannot be
    # asserted. Reading `OPENTOPO_CREDIT` here instead would pass with `paintAttribution`
    # broken, which is the defect that matters: the constant is right and the reader sees
    # someone else's credit. OpenTopoMap's is the notice with terms of its own, crediting
    # the data and the rendering separately with a `©` for each, and dropping either mark
    # drops a required credit.
    #
    # Inside this check rather than beside it because the switch is the expensive part — a
    # style, its glyphs and a screen of raster tiles — and this window has already paid for
    # a map that is ready, laid out and drawing.
    topo = switch_basemap(page, "geography")
    if not topo["settled"]:
        bad.append(f"basemap app: the geography switch did not take — {topo['why']}")
    else:
        text = topo["creditText"]
        marks = text.count("©")
        if marks < OPENTOPO_CREDIT_MARKS:
            bad.append(
                f"basemap app: the geography credit reads {text!r} — {marks} copyright "
                f"mark(s), want {OPENTOPO_CREDIT_MARKS}. OpenTopoMap's notice marks the data "
                "(OSM, SRTM) and their own rendering separately, and dropping either mark "
                "drops a required credit"
            )
        absent = [part for part in OPENTOPO_CREDIT_PARTS if part not in text]
        if absent:
            bad.append(
                f"basemap app: the geography credit reads {text!r}, with no {', '.join(absent)} "
                "— every one of those is named by the notice OpenTopoMap specifies"
            )
        links = topo["creditHtml"].count("href")
        if links < OPENTOPO_CREDIT_LINKS:
            bad.append(
                f"basemap app: the geography credit carries {links} link(s), want "
                f"{OPENTOPO_CREDIT_LINKS} — OSM's copyright page, OpenTopoMap, and the "
                "CC-BY-SA deed share-alike points a reader at"
            )
        missing = overlay - set(topo["layers"])
        if missing:
            bad.append(
                f"basemap app: overlay layers {sorted(missing)} were discarded by the "
                "geography switch — draw() was not re-run"
            )

    # Back to satellite, because the choice is remembered in `localStorage` and the `day 02
    # lines` check reads the basemap this one leaves behind — see the comment on `own` there.
    # Asserted rather than assumed: a restore that silently failed would hand that check a
    # different style than its own comment describes.
    restored = switch_basemap(page, "satellite")
    if not restored["settled"]:
        bad.append(
            "basemap app: could not put satellite back after the geography switch — "
            f"{restored['why']}. Later checks open a map on whatever this one remembered"
        )
    return bad


def check_min_size(page) -> list[str]:
    """Dragging the corner far past the minimum stops at the minimum."""
    selector = win_selector(page, "readme")
    grip = rect_of(page, f'{selector} .window-grip[data-dir="se"]')
    if grip is None:
        return ["min size: no south-east grip laid out"]

    drag(page, grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2, -1200, -1200, steps=8)

    after = need(probe(page), "readme")
    bad = []
    # `box`, because the minimum is the gesture's own: `applyGesture` stops the edge under
    # the pointer at `MIN_W`/`MIN_H` before anything is clamped. `placed` would pass on a
    # gesture that wrote a 20px box and let `clampBox`'s own floor repair it, which is a
    # window that snaps away from the pointer mid-drag.
    if after["box"]["w"] != MIN_W:
        bad.append(f"min size: width clamped to {after['box']['w']}, want {MIN_W}")
    if after["box"]["h"] != MIN_H:
        bad.append(f"min size: height clamped to {after['box']['h']}, want {MIN_H}")
    # And `rect`, because the minimum is a promise about the window the reader is looking at:
    # below it the title bar loses its buttons. The two assertions above both read state the
    # code wrote; a `min-width` in `window.css` disagreeing with `MIN_W` is invisible to
    # them and is exactly the gap between "the code believes" and "the browser laid out".
    if not near(after["rect"]["w"], MIN_W) or not near(after["rect"]["h"], MIN_H):
        bad.append(
            f"min size: believed {after['box']['w']}x{after['box']['h']} but laid out "
            f"{after['rect']['w']}x{after['rect']['h']}"
        )
    return bad


def check_container_query(page) -> list[str]:
    """A narrow window restacks its table, which no media query could do.

    The window is 220px wide after the previous check and the desktop is 1280. If the table
    header is still laid out, the rule is keyed to the viewport and the content does not
    respond to its own window.
    """
    selector = win_selector(page, "readme")
    head = page.evaluate(
        "(() => { const t = document.querySelector(%s);"
        " if (!t) return null; const r = t.getBoundingClientRect();"
        " return { w: r.width, h: r.height }; })()"
        % json.dumps(f"{selector} .group-table thead")
    )
    if head is None:
        return ["container query: no table in the Read me window to measure"]
    # The narrow rule clips the header to a 1px box rather than hiding it, so that a screen
    # reader still reaches it. A visible header is a header wider than that.
    if head["w"] > 2:
        return [
            f"container query: table header still {round(head['w'])}px wide in a "
            f"{MIN_W}px window — the rule is not responding to the container"
        ]
    return []


def check_minimize_restore(page) -> list[str]:
    """Minimise hides the window and clears its button; restore puts it back where it was."""
    bad = []
    before = need(probe(page), "readme")

    title_click(page, "readme", "Minimize")
    state = probe(page)
    win = need(state, "readme")
    if win["state"] != "min":
        bad.append(f"minimize: state is {win['state']!r}, want 'min'")
    if not win["hidden"]:
        bad.append("minimize: the window is still in the layout and the tab order")
    if win["pressed"]:
        bad.append("minimize: the taskbar button is still pressed")
    if state["active"] == "readme":
        bad.append("minimize: the minimised window is still the active one")
    if "minimized" not in state["live"]:
        bad.append(f"minimize: live region says {state['live']!r}, want a minimised notice")
    # The taskbar button has to survive, or there is no way back.
    if state["taskButtons"] != len(state["wins"]):
        bad.append("minimize: the taskbar button went away with the window")

    task_click(page, "readme")
    state = probe(page)
    win = need(state, "readme")
    if win["state"] != "normal" or win["hidden"]:
        bad.append(f"restore: state {win['state']!r}, hidden={win['hidden']}")
    if win["box"] != before["box"]:
        bad.append(f"restore: came back at {win['box']}, left from {before['box']}")
    if not win["pressed"] or state["active"] != "readme":
        bad.append("restore: the window came back without focus")
    return bad


def check_taskbar_toggles(page) -> list[str]:
    """XP's rule: the button for the front window puts it away."""
    bad = []
    state = probe(page)
    if state["active"] != "readme":
        return [f"taskbar toggle: expected Read me active first, got {state['active']!r}"]

    task_click(page, "readme")
    win = need(probe(page), "readme")
    if win["state"] != "min":
        bad.append(f"taskbar toggle: clicking the active window's button left it {win['state']!r}")

    task_click(page, "readme")
    win = need(probe(page), "readme")
    if win["state"] != "normal":
        bad.append(f"taskbar toggle: a second click left it {win['state']!r}, want 'normal'")
    return bad


def check_maximize(page) -> list[str]:
    """Maximise fills the desktop above the taskbar; restore returns the exact box."""
    bad = []
    state = probe(page)
    before = need(state, "readme")
    surface = state["surface"]

    title_click(page, "readme", "Maximize")
    state = probe(page)
    win = need(state, "readme")
    if win["state"] != "max":
        bad.append(f"maximize: state is {win['state']!r}, want 'max'")
    for axis, got, want in (
        ("x", win["rect"]["x"], surface["x"]),
        ("y", win["rect"]["y"], surface["y"]),
        ("w", win["rect"]["w"], surface["w"]),
        ("h", win["rect"]["h"], surface["h"]),
    ):
        if not near(got, want):
            bad.append(f"maximize: {axis} is {got}, desktop's is {round(want)}")
    if win["liveGrips"]:
        # A maximised window with live grips can be resized off its own maximised state,
        # which leaves the state saying 'max' and the geometry saying otherwise.
        bad.append(f"maximize: {win['liveGrips']} resize grips still grabbable")

    # A maximised window does not move, which is what XP did.
    bar = rect_of(page, f"{win_selector(page, 'readme')} .window-title")
    drag(page, bar["x"] + 40, bar["y"] + bar["h"] / 2, 120, 60)
    win = need(probe(page), "readme")
    if win["state"] != "max" or not near(win["rect"]["x"], surface["x"]):
        bad.append("maximize: a title-bar drag moved the maximised window")

    title_click(page, "readme", "Restore")
    win = need(probe(page), "readme")
    if win["state"] != "normal":
        bad.append(f"restore from max: state is {win['state']!r}")
    if win["box"] != before["box"]:
        bad.append(f"restore from max: came back at {win['box']}, left from {before['box']}")
    return bad


def check_title_double_click(page) -> list[str]:
    """Double-clicking the title bar maximises, and again restores; the buttons are exempt.

    `src/window.mjs` has carried a `dblclick` listener on the bar since it was written and
    nothing had ever double-clicked, because a double click needs `clickCount: 2` on the
    second press and the harness sent 1 on every event. Two presses that both say 1 are two
    single clicks — the browser never synthesises `dblclick`, the handler never runs, and a
    check built on them reports exactly what an app that ignores double clicks would.

    So the first thing asserted is that two single clicks do *nothing*. That is the control
    for the helper rather than for the product, and it belongs inside the check: without it,
    "the double click maximised" is satisfied by any pair of clicks that happens to toggle.
    """
    bad = []
    state = probe(page)
    win = need(state, "readme")
    if win["state"] != "normal":
        return [f"title double click: Read me is {win['state']!r}, want 'normal' to start"]
    bar = rect_of(page, f"{win_selector(page, 'readme')} .window-title")
    if bar is None:
        return ["title double click: Read me has no laid-out title bar"]
    # Left of the title boxes, so neither click lands on one.
    x, y = bar["x"] + 40, bar["y"] + bar["h"] / 2
    started = win["box"]

    # The control. Two clicks that do not claim to be a double must leave the state alone.
    for _ in range(2):
        mouse(page, "mousePressed", x, y, buttons=1)
        mouse(page, "mouseReleased", x, y, buttons=0)
    time.sleep(SETTLE_S)
    single = need(probe(page), "readme")
    if single["state"] != "normal":
        bad.append(
            f"title double click: two single clicks left the window {single['state']!r} — "
            "the browser is treating them as a double, so the assertions below would pass "
            "whether `double_click` sets a click count or not"
        )

    double_click(page, x, y)
    time.sleep(SETTLE_S)
    maxed = need(probe(page), "readme")
    if maxed["state"] != "max":
        return bad + [
            f"title double click: the window is {maxed['state']!r} after a double click on "
            "the title bar, want 'max' — the `dblclick` listener in `src/window.mjs` did "
            "not run"
        ]
    # And the window actually moved. A state flag that flips while the frame stays put is
    # the half `state` alone cannot see, and it is the failure a screenshot would catch and
    # an assertion on `state` would not.
    surface = state["surface"]
    if not near(maxed["rect"]["w"], surface["w"]) or not near(maxed["rect"]["h"], surface["h"]):
        bad.append(
            f"title double click: the state says 'max' but the frame is laid out at "
            f"{maxed['rect']['w']}x{maxed['rect']['h']} on a "
            f"{round(surface['w'])}x{round(surface['h'])} desktop"
        )

    # Re-read the bar: a maximised window's title bar is at the top of the desktop, not
    # where the window used to be, so the point the first double click was aimed at is now
    # somewhere in the body. Aiming at the stale point reported `a second double click left
    # it 'max'` — a true statement about a click that never reached the bar.
    bar = rect_of(page, f"{win_selector(page, 'readme')} .window-title")
    if bar is None:
        return bad + ["title double click: the maximised window has no laid-out title bar"]
    x, y = bar["x"] + 40, bar["y"] + bar["h"] / 2
    double_click(page, x, y)
    time.sleep(SETTLE_S)
    back = need(probe(page), "readme")
    if back["state"] != "normal":
        bad.append(f"title double click: a second double click left it {back['state']!r}")
    if back["box"] != started:
        bad.append(
            f"title double click: came back at {back['box']}, left from {started} — the "
            "double click lost the geometry a maximise is supposed to keep"
        )
    if not near(back["rect"]["w"], started["w"]) or not near(back["rect"]["h"], started["h"]):
        bad.append(
            f"title double click: the state says 'normal' but the frame is still "
            f"{back['rect']['w']}x{back['rect']['h']}, not the {started['w']}x{started['h']} "
            "it left from"
        )

    # The exemption, which is the half worth having: `startMove` and the `dblclick` handler
    # both ignore a target inside `.title-buttons`, so a double click that lands among the
    # boxes does not also maximise. Delete that `closest` line and only this assertion
    # notices.
    #
    # Aimed at the 2px `gap` between two boxes (`src/window.css`), not at a box.
    #
    # A box is the obvious target and it cannot work: its own click handler maximises on the
    # first click, which moves the whole title bar, so the second click lands in the body of
    # a now-maximised window and `dblclick` never reaches the bar at all. Measured — aiming
    # at the Maximize box reported `left the window 'max'` for exactly that reason, which is
    # a true sentence about a click that tested nothing. In the gap no box handler fires, the
    # window does not move between the two clicks, and the guard is the only thing deciding
    # the outcome.
    gap = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
        " const boxes = [...w.el.querySelectorAll('.title-box')]"
        "   .map((b) => b.getBoundingClientRect())"
        "   .filter((r) => r.width).sort((a, b) => a.left - b.left);"
        " for (let i = 1; i < boxes.length; i += 1) {"
        "   const x = (boxes[i - 1].right + boxes[i].left) / 2;"
        "   const y = boxes[i].top + boxes[i].height / 2;"
        "   const hit = document.elementFromPoint(x, y);"
        "   if (hit && hit.classList.contains('title-buttons')) return { x, y };"
        " }"
        " return null; })()"
    )
    if gap is None:
        # Never a skip: the guard would then be unreachable by a pointer and unasserted, and
        # saying so is the only honest outcome.
        bad.append(
            "title double click: found no point inside `.title-buttons` that is not on a "
            "box, so the `closest('.title-buttons')` guard could not be exercised — "
            "deleting that line would go unnoticed"
        )
        return bad
    double_click(page, gap["x"], gap["y"])
    time.sleep(SETTLE_S)
    on_box = need(probe(page), "readme")
    if on_box["state"] != "normal":
        bad.append(
            f"title double click: a double click between the title boxes left the window "
            f"{on_box['state']!r} — the bar's `dblclick` handler ran on a target inside "
            "`.title-buttons`, which it is written not to"
        )
        # Put it back, so the next check does not inherit a maximised window.
        if on_box["state"] == "max":
            title_click(page, "readme", "Restore")

    # A window that may not be maximised must not be, by this route either. `about` is the
    # one fixed-size program.
    #
    # The guard is in `maximize()` — `if (!this.maximizable || …) return` — and not in the
    # `dblclick` handler, which calls `toggleMaximize` unconditionally. So this asserts a
    # guarantee that holds one call further down than where a reader of the handler would
    # look for it, which is the third defect shape `docs/review.md` names: the handler is
    # the entry point, and reading it does not tell you whether the rule holds.
    fixed = window_of(probe(page), "about")
    if fixed is None:
        bad.append(
            "title double click: no About window open, so the fixed-size case went "
            "unchecked — every assertion above is about a maximizable window"
        )
        return bad
    fixed_bar = rect_of(page, f"{win_selector(page, 'about')} .window-title")
    if fixed_bar is None:
        return bad + ["title double click: the About window has no laid-out title bar"]
    double_click(page, fixed_bar["x"] + 40, fixed_bar["y"] + fixed_bar["h"] / 2)
    time.sleep(SETTLE_S)
    after_fixed = need(probe(page), "about")
    if after_fixed["state"] != "normal":
        bad.append(
            f"title double click: About is {after_fixed['state']!r} after a double click — "
            "a window that hides its maximise box was maximised anyway, and the only route "
            "back is a box that is not there"
        )
    return bad


def check_keyboard(page) -> list[str]:
    """The title bar is focusable and the arrow keys are the drag's keyboard equivalent."""
    bad = []
    selector = win_selector(page, "readme")
    before = need(probe(page), "readme")

    focused = page.evaluate(
        "(() => { const bar = document.querySelector(%s);"
        " if (!bar) return null; bar.focus();"
        " return document.activeElement === bar; })()"
        % json.dumps(f"{selector} .window-title")
    )
    if not focused:
        return ["keyboard: the title bar did not take focus"]

    for _ in range(3):
        arrow(page, "ArrowRight")
    for _ in range(2):
        arrow(page, "ArrowDown")
    after = need(probe(page), "readme")
    # `after["box"]` against `before["placed"]`, for the same reason the drag check reads
    # `placed`: `onBarKey` steps from `placed` — `moveTo(this.placed.x + kx * KEY_STEP, …)`
    # in `src/window.mjs` — because an arrow moves the window from where it is, not from a
    # remembered position it was clamped out of. Measured from a y-clamped start
    # (`box.y=-50`, `placed.y=0`): `keyboard move: 400,-50 -> 430,20, want +30,+20`.
    if not near(after["box"]["x"], before["placed"]["x"] + 30) or not near(
        after["box"]["y"], before["placed"]["y"] + 20
    ):
        bad.append(
            f"keyboard move: {before['placed']['x']},{before['placed']['y']} -> "
            f"{after['box']['x']},{after['box']['y']}, want +30,+20"
        )

    sized = need(probe(page), "readme")
    for _ in range(3):
        arrow(page, "ArrowRight", shift=True)
    grown = need(probe(page), "readme")
    # `sized["placed"]["w"]` for the same reason: `sizeTo(this.placed.w + kx * KEY_STEP, …)`.
    # Masked today, because only the position clamp can separate `box` from `placed` on the
    # width — `clampBox` floors `w` at `MIN_W` and caps it at nothing — but reading the
    # request where the code reads the placement is the defect either way, and a size cap
    # returning to `clampBox` would make it bite without this line changing.
    if not near(grown["box"]["w"], sized["placed"]["w"] + 30):
        bad.append(
            f"keyboard resize: width {sized['placed']['w']} -> {grown['box']['w']}, want +30"
        )
    # `box` on both sides, deliberately: `sizeTo` writes `{...this.box, w, h}` and never
    # touches `x`, so the question "did shift+arrow also move the window" is a question about
    # the request. Reading `placed` here would fire on a correct resize, because widening a
    # window against the left clamp legitimately moves its placement.
    if grown["box"]["x"] != sized["box"]["x"]:
        bad.append("keyboard resize: shift and an arrow moved the window as well as sizing it")
    return bad


def check_focus_order(page) -> list[str]:
    """Clicking a window raises it above the one that was on top."""
    bad = []
    # Opens its own Notepad if an earlier check has not. It relied on one being there, which
    # made it unrunnable alone — and a check that cannot be run by itself cannot be shown to
    # fail by itself either.
    if window_of(probe(page), "notepad") is None:
        launch(page, "notepad")
    state = probe(page)
    notepad = need(state, "notepad")

    selector = win_selector(page, "notepad")
    body = rect_of(page, f"{selector} .window-title")
    if body is None:
        return ["focus: Notepad has no laid-out title bar"]

    # What is actually under the pointer, before clicking there.
    #
    # This check computes a rect and clicks it blind, so anything covering that point gets the
    # click and the result reads as a product failure: "active is 'day-02-lines' after clicking
    # Notepad" is a true sentence about a click that never reached Notepad. It happened, on a
    # 1040x680 window another check had left open.
    #
    # A harness MUST fail when it cannot reach what it was pointed at, and MUST say which kind
    # of failure it is. This is the harness's problem, not the window manager's.
    x = body["x"] + 40
    y = body["y"] + body["h"] / 2
    covered = page.evaluate(
        "(() => { const hit = document.elementFromPoint(%f, %f);"
        " if (!hit) return 'nothing';"
        " const win = hit.closest('.window');"
        " const wanted = document.querySelector(%s);"
        " if (win === wanted) return '';"
        " const other = win ? (win.querySelector('.window-title span') || {}).textContent : null;"
        " return other ? `the ${other} window` : 'the desktop'; })()"
        % (x, y, json.dumps(selector))
    )
    if covered:
        return [
            f"focus: cannot isolate Notepad's title bar — {covered} is at ({x:.0f}, {y:.0f}). "
            "This is a harness problem, not a product one: an earlier check left a window over "
            "the click target."
        ]

    mouse(page, "mousePressed", body["x"] + 40, body["y"] + body["h"] / 2, buttons=1)
    mouse(page, "mouseReleased", body["x"] + 40, body["y"] + body["h"] / 2)
    time.sleep(0.1)

    state = probe(page)
    notepad_after = need(state, "notepad")
    if state["active"] != "notepad":
        bad.append(f"focus: active is {state['active']!r} after clicking Notepad")
    if notepad_after["z"] <= notepad["z"]:
        bad.append(f"focus: z stayed at {notepad_after['z']} instead of rising")
    top = max(w["z"] for w in state["wins"])
    if notepad_after["z"] != top:
        bad.append(f"focus: Notepad at z {notepad_after['z']}, topmost is {top}")
    if sum(1 for w in state["wins"] if w["activeClass"]) != 1:
        bad.append("focus: more than one window is drawn as active")
    return bad


def check_desktop_blur(page) -> list[str]:
    """Clicking bare desktop clears the focus, as it did in XP."""
    state = probe(page)
    surface = state["surface"]
    # Bottom-right of the desktop, which no window reaches after the cascade.
    x = surface["x"] + surface["w"] - 12
    y = surface["y"] + surface["h"] - 12
    mouse(page, "mousePressed", x, y, buttons=1)
    mouse(page, "mouseReleased", x, y)
    time.sleep(0.1)

    state = probe(page)
    bad = []
    if state["active"] is not None:
        bad.append(f"desktop blur: {state['active']!r} is still active")
    if any(w["pressed"] for w in state["wins"]):
        bad.append("desktop blur: a taskbar button is still pressed")
    return bad


def check_close(page) -> list[str]:
    """Close removes the window and its button, and hands focus to the topmost survivor.

    The window is focused first on purpose. This check used to run straight after
    `desktop blur`, which leaves nothing active, so it closed a window that was not the
    active one and satisfied its own "nothing took focus" assertion through the easy path.
    Closing the *active* window is the case worth asserting, and the survivor has to be the
    topmost one rather than merely some window.
    """
    bad = []
    task_click(page, "about")
    state = probe(page)
    if state["active"] != "about":
        return [f"close: wanted About active before closing it, got {state['active']!r}"]

    before = state
    survivors = [w for w in before["wins"] if w["program"] != "about" and w["state"] != "min"]
    want_next = max(survivors, key=lambda w: w["z"])["program"] if survivors else None

    title_click(page, "about", "Close")
    time.sleep(0.2)

    state = probe(page)
    if want_next and state["active"] != want_next:
        bad.append(
            f"close: focus went to {state['active']!r}, want the topmost survivor "
            f"{want_next!r}"
        )
    if window_of(state, "about") is not None:
        bad.append("close: the window is still in the desktop's list")
    if len(state["wins"]) != len(before["wins"]) - 1:
        bad.append(f"close: {len(state['wins'])} windows left, want {len(before['wins']) - 1}")
    if state["taskButtons"] != len(state["wins"]):
        bad.append(f"close: {state['taskButtons']} buttons for {len(state['wins'])} windows")
    if "closed" not in state["live"]:
        bad.append(f"close: live region says {state['live']!r}, want a closed notice")
    if state["active"] is None:
        bad.append("close: nothing took focus after the active window closed")
    return bad


def check_singleton(page) -> list[str]:
    """A singleton program focuses its window instead of opening a second one."""
    before = probe(page)
    launch(page, "display")
    state = probe(page)
    bad = []
    if len(state["wins"]) != len(before["wins"]):
        bad.append(
            f"singleton: Display Properties opened again — {len(before['wins'])} windows "
            f"became {len(state['wins'])}"
        )
    if state["active"] != "display":
        bad.append(f"singleton: relaunch left {state['active']!r} active")

    # Notepad is not a singleton, so the same move must open one more. Without this the check
    # above would also pass on a desktop that refuses to open anything twice.
    #
    # Counted relative to what is already open rather than against a fixed number: other
    # checks open Notepad too, and an absolute expectation here breaks whenever one of them
    # changes. It did.
    notepads_before = sum(1 for w in state["wins"] if w["program"] == "notepad")
    launch(page, "notepad")
    after = probe(page)
    notepads_after = sum(1 for w in after["wins"] if w["program"] == "notepad")
    if notepads_after != notepads_before + 1:
        bad.append(
            f"singleton control: {notepads_before} Notepad windows became {notepads_after} "
            "after a relaunch, want one more"
        )
    return bad


#: How many day apps the folder must show. The challenge has thirty days, and the folder is
#: the only place they are reachable from, so a filter that quietly drops one is invisible.
WANT_DAYS = 30


def check_map_challenge_folder(page) -> list[str]:
    """The folder lists every day, opens one as its own window, and reads on a white sheet.

    Three separate things can go wrong here and only the first is obvious:

    1. The membership filter. The folder derives its contents from `desktop.programs`, so a
       wrong prefix shows nothing, or shows the whole desktop. Both are asserted: thirty
       days present, and no non-day program among them.
    2. The launch route. A tile that builds but opens nothing looks identical to one that
       works until it is clicked.
    3. The surface. `iconTile` is shared with the desktop, where the label is white with a
       shadow because it sits on a wallpaper. Inherited onto the folder's white sheet that
       is invisible text — which renders as a grid of unlabelled icons, not as an error. The
       desktop's own icon is measured in the same breath, so the split cannot be fixed in one
       direction by breaking the other.
    """
    bad = []
    launch(page, "map-challenge")

    state = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.programId === 'map-challenge');"
        " if (!w) return { error: 'no folder window' };"
        " const tiles = [...w.bodyEl.querySelectorAll('.day-grid .folder-icon')];"
        " const label = tiles.length ? tiles[0].querySelector('span') : null;"
        " const deskIcon = document.querySelector('.desktop-icons .desktop-icon');"
        " const style = label ? getComputedStyle(label) : null;"
        " const deskStyle = deskIcon ? getComputedStyle(deskIcon) : null;"
        " return { tiles: tiles.map((t) => t.textContent),"
        "   boxes: tiles.map((t) => { const r = t.getBoundingClientRect();"
        "     return { w: Math.round(r.width), h: Math.round(r.height) }; }),"
        "   svgs: tiles.filter((t) => t.querySelector('svg')).length,"
        "   folderColor: style ? style.color : '',"
        "   folderShadow: style ? style.textShadow : '',"
        "   deskColor: deskStyle ? deskStyle.color : '',"
        "   deskShadow: deskStyle ? deskStyle.textShadow : '',"
        "   programs: [...window.webxp.programs.keys()] }; })()"
    )
    if state.get("error"):
        return [f"folder: {state['error']}"]

    tiles = state["tiles"]
    days = [p for p in state["programs"] if p.startswith("day-")]
    if len(days) != WANT_DAYS:
        bad.append(f"folder: {len(days)} day apps registered, want {WANT_DAYS}")
    if len(tiles) != len(days):
        bad.append(f"folder: {len(tiles)} tiles for {len(days)} day apps")
    if state["svgs"] != len(tiles):
        bad.append(f"folder: {state['svgs']} of {len(tiles)} tiles drew an icon")

    # The membership control. A filter of `() => true` would also satisfy every count above
    # once the numbers happened to line up; this fails on it.
    strays = [t for t in tiles if not t.startswith("Day ")]
    if strays:
        bad.append(f"folder: non-day programs listed: {strays[:3]}")
    if tiles and not tiles[0].startswith("Day 01"):
        bad.append(f"folder: first tile is {tiles[0]!r}, want Day 01 — the sort is by id")
    if tiles and not tiles[-1].startswith("Day 30"):
        bad.append(f"folder: last tile is {tiles[-1]!r}, want Day 30")

    # The tile's box comes from `.icon-tile` in the shell, not from this app. A zero-width or
    # unstyled tile means the shared rule stopped reaching it.
    flat = [b for b in state["boxes"] if b["w"] < 40 or b["h"] < 30]
    if flat:
        bad.append(f"folder: {len(flat)} tiles are smaller than an icon, e.g. {flat[0]}")

    if state["folderColor"] != "rgb(0, 0, 0)":
        bad.append(
            f"folder: the label colour is {state['folderColor']!r}, want black — a white "
            "label on the folder's white sheet is invisible, not broken"
        )
    if state["folderShadow"] not in ("none", ""):
        bad.append(
            f"folder: the label carries text-shadow {state['folderShadow']!r} — that is the "
            "wallpaper's contrast mechanism and has no job on a sheet"
        )
    # The other half of the split, measured here so neither surface can be fixed by breaking
    # the other.
    if state["deskColor"] != "rgb(255, 255, 255)":
        bad.append(f"folder: the desktop icon's label is {state['deskColor']!r}, want white")
    if state["deskShadow"] in ("none", ""):
        bad.append(
            "folder: the desktop icon lost its text-shadow — white on a wallpaper is "
            "unreadable without it"
        )

    # Clicking a tile opens that day, as its own window rather than as a pane in this one.
    before = len(probe(page)["wins"])
    opened = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.programId === 'map-challenge');"
        " const tile = w && w.bodyEl.querySelector('.day-grid .folder-icon');"
        " if (!tile) return false; tile.click(); return true; })()"
    )
    if not opened:
        return bad + ["folder: no tile to click"]
    time.sleep(0.6)
    after = probe(page)
    if len(after["wins"]) != before + 1:
        bad.append(
            f"folder: clicking a tile took {before} windows to {len(after['wins'])}, want one more"
        )
    if window_of(after, "day-01-points") is None:
        bad.append("folder: clicking the first tile did not open day-01-points")
    if after["active"] != "day-01-points":
        bad.append(f"folder: the day opened behind {after['active']!r} instead of on top")
    return bad


#: Window sizes the drill-down panel is opened at, with a long footer, to ask whether it
#: covers the status bar. All are reachable by dragging a grip (`MIN_W`/`MIN_H` in
#: `src/window.mjs`), and each was a confirmed failure before `.detail` got `overflow:
#: hidden`: the head and foot are fixed-height and only the body shrank, so a long footer
#: pushed content past the sheet's 48% cap and painted over the bar.
#:
#: Three, not the sixty-four a full sweep covers: the short-and-wide corner is where the
#: overhang was largest, and a check that takes a minute gets run.
#: Where day 02's measured directories live. `private/` is a real export and is gitignored;
#: `data/` is the committed synthetic fixture. Both are checked when present, because a
#: clean clone has only the second and a working machine has both.
DAY_02_DATA_DIRS = ("private", "data")

#: The overlay files whose crossings index into the per-activity documents.
DAY_02_OVERLAYS = ("street-noise.json", "area-noise.json")

#: The floor under a disagreement between the two ladder implementations, in metres.
#:
#: `prepare.LENGTH_DP` writes lengths to three decimals, so half a millimetre is rounding
#: rather than disagreement. Measured: 4,835 of 4,836 rungs across the committed fixture and
#: a 397-activity export fall inside this.
LADDER_FLOOR_M = 0.0005

#: The relative bar the one rung over the floor has to clear.
#:
#: That rung is a Douglas-Peucker decision flipped by a last-bit difference between
#: `math.cos` and `Math.cos` at the frame origin. Measured worst 6.5e-6 relative, so this
#: has 1.5x headroom. Relative and not scaled by trace length on purpose: a flip's size
#: comes from local geometry, and the measurement says so — 72 mm on an 11.1 km trace and
#: 8.5 mm on a 14.8 km one. An earlier `0.0005 + 1e-6 * L` form was withdrawn for that.
LADDER_REL_BAR = 1e-5

#: The two ladders for the selected run: the one the browser computes and the one
#: `prepare.py` wrote into that run's document.
#:
#: A named constant rather than a literal inside the check, so a control can perturb one
#: side of the pair and the comparison can be shown to fail. Rows are `[tolerance, length,
#: kept]` on both sides.
DAY_02_LADDERS = """(() => {
  const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');
  if (!w || !w.lines) return { mine: null, written: null };
  return { mine: w.lines.ladder(), written: w.lines.writtenLadder() };
})()"""

PANEL_SIZES = ((420, 140), (240, 200), (820, 200))

#: Fill `.detail-foot` until it carries more text than the panel can show, and report the
#: four heights that decide whether this size is stressing anything.
#:
#: Grown against `.detail`'s own height, not written once. A fixed `repeat(24)` is the same
#: 384 characters at every window size, so the widest size in `PANEL_SIZES` got roughly a
#: third of the lines the narrowest did and was the least stressed of the three: 820x200
#: takes 32 repeats to reach the pressure 420x140 reaches at 8. The old test was `height >
#: 0`, which one line of text satisfies at any size and which therefore said nothing about
#: how much there was to overflow.
#:
#: `panelWanted` against `panelGiven` is the assertion that carries the sweep, and the
#: footer's own length is not what moves it. Measured at the three sizes, across 8 to 1024
#: repeats: `footGiven` is 14 px and `panelWanted` is 91 px, both flat. `max-height: 50%`
#: plus `min-height: 0` let flex shrink `.detail-body` and `.detail-foot` to zero content
#: height, so what overflows the panel is the part that cannot shrink — the head, plus the
#: two paddings — and `.detail { overflow: hidden }` is what keeps it off the bar. A change
#: that lets that minimum fit (dropping the body or foot padding, or letting the head
#: shrink) makes the clipping dead and the hit tests below unable to tell a contained panel
#: from a clipped one. The footer is still filled: it is the stress the sizes were chosen
#: for, and it is what would overflow again if the foot stopped being held.
#:
#: The outcomes that are not a measurement are named rather than collapsed into one falsy
#: answer: `missing` is a renamed or deleted `.detail-foot`, `closed` is a panel that is shut
#: — `.detail` carries `hidden`, so every child measures 0x0 — `no-panel` is no `.detail` at
#: all, and `no-window` is the day gone from the desktop. They send a reader to different
#: places, and none of them is a skip.
FOOT_STRESS = """(() => {
  const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');
  if (!w || !w.bodyEl) return { state: 'no-window' };
  const detail = w.bodyEl.querySelector('.detail');
  if (!detail) return { state: 'no-panel' };
  if (detail.hidden || !detail.getBoundingClientRect().width) return { state: 'closed' };
  const foot = w.bodyEl.querySelector('.detail-foot');
  if (!foot) return { state: 'missing' };
  let repeats = 8;
  foot.textContent = 'Overflow probe. '.repeat(repeats);
  // Re-read the panel every round. In the bottom-sheet branch `.detail` is content-sized up
  // to its 48% cap, so filling the footer can move the height the loop is aiming at, and a
  // target captured before the first fill is a different number from the one asserted on.
  let panelGiven = detail.clientHeight;
  if (!panelGiven) return { state: 'closed' };
  while (foot.scrollHeight < panelGiven && repeats < 4096) {
    repeats *= 2;
    foot.textContent = 'Overflow probe. '.repeat(repeats);
    panelGiven = detail.clientHeight;
  }
  return { state: 'ok', repeats, footWanted: foot.scrollHeight, footGiven: foot.clientHeight,
           panelWanted: detail.scrollHeight, panelGiven };
})()"""


def day_02_panel_sweep(page) -> list[str]:
    """Open the drill-down panel and check it covers neither pane of the status bar.

    The rest of `check_day_02` tests one window size with the panel shut, which is exactly
    the gap this closes: the panel is `position: absolute; z-index: 5` and `.map-bar` is not
    positioned, so anything overflowing the panel paints over the credit and the live region.
    """
    bad = []
    # Awaited, not slept through.
    #
    # `openSegment` is `async` (`apps/day-02-lines/lines.mjs`) and `await drawGhosts(...)`
    # sits before the `panel.open(...)` that ends it, fanning out one full-resolution fetch
    # per crossing. Discarding the promise and sleeping a second meant the fetches could
    # finish *after* the footer was stressed, and `panel.open` ends with
    # `this.foot.textContent = foot || ''` (`shared/map/panel.mjs`) — so the stress was
    # erased, `.detail-foot:empty { display: none }` dropped the element, and all three sizes
    # swept with nothing to overflow and reported clean. The guard could not catch it: it ran
    # before the thing that erases it.
    # Wait for the street file before concluding there is no street data.
    #
    # `street-noise.json` is 1.58 MB and fetched after the window is already usable, so a
    # single read right after the earlier assertions is a race. It lost: the suite reported
    # "no segment with crossings in the loaded data" on an export that has 351 of them, and
    # said so in the words reserved for a clean clone. Impatience wearing the costume of a
    # legitimate skip is worse than either a pass or a failure.
    deadline = time.monotonic() + 30
    opened = "no day 02 window"
    while time.monotonic() < deadline:
        opened = page.evaluate(
            "(async () => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " if (!w || !w.lines) return 'no day 02 window';"
            " const cell = w.lines.firstCellWithPasses();"
            " if (!cell) return 'no segment with crossings in the loaded data';"
            " await w.lines.openSegment(cell); return ''; })()",
            await_promise=True,
        )
        if not opened:
            break
        time.sleep(0.5)
    if opened:
        # Still nothing after waiting: that is the clean-clone case, where the committed
        # synthetic data carries no street file. Returned as a finding, so it is counted and
        # printed as a failure — and worded as one. Calling it "skipped" on a `[FAIL]` line
        # invites exactly the reading this wait exists to prevent: a reader who clears it as
        # nothing to see. Measured with the data forced absent: 30.4s, then one failure.
        return [
            f"day 02 panel: nothing to measure after 30s — {opened}. The panel's layout went "
            "unasserted; this is not a clean result."
        ]
    # Past the panel's slide: `SLIDE_MS` in `shared/map/panel.mjs` is 220 ms and `.detail` is
    # transformed clear of the map area until it finishes, so a hit test taken mid-slide finds
    # the panel covering nothing wherever it is pointed.
    time.sleep(SETTLE_S)

    for width, height in PANEL_SIZES:
        page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " if (w.state === 'max') w.toggleMaximize();"
            " w.el.style.left = '10px'; w.el.style.top = '10px';"
            " w.el.style.width = %d + 'px'; w.el.style.height = %d + 'px'; })()"
            % (width, height)
        )
        time.sleep(0.15)

        # The stress, created at the size it is asserted at.
        #
        # It used to be written once, before this loop, at the 1040x680 launch size — so it
        # verified the stress at a size the sweep never visits and asserted nothing about the
        # three it does. The panel's geometry is what changes between the sizes, so the
        # footer has to be re-filled and re-measured at each of them.
        stress = page.evaluate(FOOT_STRESS)
        if stress["state"] == "no-window":
            return bad + [
                f"day 02 panel: the day-02-lines window is gone by {width}x{height} — the "
                "sweep cannot measure a panel that has no window"
            ]
        if stress["state"] == "no-panel":
            bad.append(
                f"day 02 panel: no .detail in the window at {width}x{height} — there is no "
                "panel to measure, and the hit tests below would pass on its absence"
            )
            continue
        if stress["state"] == "closed":
            bad.append(
                f"day 02 panel: the panel is shut at {width}x{height} — `.detail` measures "
                "0x0, so nothing it holds can cover anything and the hit tests below would "
                "report clean on a panel that never opened"
            )
            continue
        if stress["state"] == "missing":
            bad.append(
                f"day 02 panel: no .detail-foot at {width}x{height} — the sweep's window "
                "sizes were chosen for a long footer and assert nothing without one"
            )
            continue
        # The panel has to be overflowing something, or the hit tests below cannot tell a
        # panel that is being clipped from one that fits. This is the assertion the sweep
        # rests on: `.detail { overflow: hidden }` is only doing work while its content
        # exceeds it.
        if stress["panelWanted"] <= stress["panelGiven"]:
            bad.append(
                f"day 02 panel: at {width}x{height} the panel's {stress['panelWanted']}px of "
                f"content fits its {stress['panelGiven']}px box — nothing is being clipped, "
                "so the hit tests below pass whether `.detail` clips or not"
            )
            continue
        if stress["footWanted"] < stress["panelGiven"]:
            bad.append(
                f"day 02 panel: at {width}x{height} the footer took {stress['repeats']} "
                f"repeats of the probe text and still wants only {stress['footWanted']}px "
                f"against the panel's {stress['panelGiven']}px — there is not enough in it "
                "to reach the panel's edge, so the footer is not part of the stress"
            )
            continue
        if stress["footWanted"] <= stress["footGiven"]:
            bad.append(
                f"day 02 panel: at {width}x{height} the footer's {stress['footWanted']}px of "
                f"content fits the {stress['footGiven']}px box it was given — the foot is no "
                "longer being held to a share of the panel"
            )
            continue
        time.sleep(0.2)

        hit = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " const at = (sel) => { const e = w.bodyEl.querySelector(sel);"
            "   if (!e) return 'missing';"
            "   const r = e.getBoundingClientRect();"
            "   if (!r.width || !r.height) return 'zero';"
            "   const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
            "   if (!top) return 'nothing';"
            # Resolved through `closest`, not read off the hit element. A credit carries links,
            # and an unclassed `<a>` answers `className` with '' and `tagName` with 'A' — so a
            # midpoint that lands on one of its own links reported 'A' covering the credit and
            # failed a credit that nothing was touching. Which word a wrap puts under the
            # midpoint is not the invariant; whether the pane or something outside it is on top
            # is. Measured: at 420x140 the satellite credit wraps to three lines and the
            # midpoint falls on its Esri link.
            "   const owner = top.closest(sel) ? e : top;"
            "   return String(owner.className || owner.tagName); };"
            " return { status: at('.map-status'), credit: at('.map-attrib') }; })()"
        )
        if "map-status" not in hit["status"]:
            bad.append(
                f"day 02 panel: at {width}x{height} the panel's {hit['status']!r} covers the "
                "status line — a failure reported there would be reported to nobody"
            )
        if "map-attrib" not in hit["credit"]:
            bad.append(
                f"day 02 panel: at {width}x{height} the panel's {hit['credit']!r} covers the "
                "credit — attribution is a licence term for three tile sources"
            )

    # The close box, which is its own defect class.
    #
    # `onClose` is where the app undoes what the panel described, and the natural way to
    # write that calls a routine which itself closes the panel — a cycle. It threw
    # `RangeError: Maximum call stack size exceeded` out of MapLibre's `setPaintProperty`,
    # naming neither the panel nor the recursion. The console sweep at the end of the run
    # catches the error now that something clicks the box; this also asserts the map state
    # actually went away, which is the reason `onClose` exists.
    closed = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
        " if (w.state === 'max') w.toggleMaximize();"
        " w.el.style.width = '1000px'; w.el.style.height = '640px';"
        " const box = w.bodyEl.querySelector('.detail-close');"
        " if (!box) return { error: 'no close box' };"
        " box.click();"
        " return { error: '' }; })()"
    )
    if closed.get("error"):
        return bad + [f"day 02 panel: {closed['error']}"]
    time.sleep(0.6)
    after = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
        " return { cell: !!w.lines.selectedCell(), ghosts: w.lines.ghostCount(),"
        "   shut: !!w.bodyEl.querySelector('.detail').hidden }; })()"
    )
    if not after["shut"]:
        bad.append("day 02 panel: the close box did not close the panel")
    if after["cell"] or after["ghosts"]:
        bad.append(
            f"day 02 panel: closing left state on the map (cell={after['cell']}, "
            f"ghosts={after['ghosts']}) — a halo and a worm with nothing explaining them"
        )
    return bad


def check_day_02(page) -> list[str]:
    """Day 02 draws its runs, measures one, and the measurement answers the slider.

    This is the only day with real content, and it is the first app to use `shared/map/` from
    somewhere other than `apps/basemap/`. Three things are asserted, in the order they can go
    wrong:

    1. It loaded data and drew. The day reads from `private/` first and the committed
       synthetic `data/` second, so a working day is either — but a day that found neither
       reports "no activities" and looks like an empty map, not like an error.
    2. The measurement responds. Simplifying a trace must shorten it: that is the whole
       argument the day makes, and a slider wired to nothing looks identical.
    3. The status bar and the credit are both readable. Both were inside layers that could
       cover or scroll them, and both failures were silent.
    """
    bad = []
    page.evaluate("window.webxp.launch('day-02-lines')")

    # It fetches an index, a per-activity document and the street file, then builds 124 layers.
    # Polled rather than slept through.
    deadline = time.monotonic() + 60
    state = None
    while time.monotonic() < deadline:
        state = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " if (!w) return { stage: 'no window' };"
            " if (w.bodyEl.classList.contains('is-loading')) return { stage: 'loading' };"
            " if (!w.lines || !w.base) return { stage: 'no day',"
            "   status: (w.bodyEl.querySelector('.map-status') || {}).textContent || '' };"
            " const map = w.base.map;"
            " const area = w.bodyEl.querySelector('.map-area');"
            " const statusEl = w.bodyEl.querySelector('.map-status');"
            " const attribEl = w.bodyEl.querySelector('.map-attrib');"
            " const sr = statusEl && statusEl.getBoundingClientRect();"
            " const ar = area && area.getBoundingClientRect();"
            " const tr = attribEl && attribEl.getBoundingClientRect();"
            " const onStatus = sr && sr.width"
            "   ? (document.elementFromPoint(sr.left + 4, sr.top + sr.height / 2) || {}).className || ''"
            "   : 'no status';"
            " return { stage: map.loaded() ? 'ready' : 'style',"
            "   status: statusEl ? statusEl.textContent : '',"
            "   activities: w.lines.activities().length,"
            "   dataDir: w.lines.dataDir() || '',"
            "   aggregate: w.lines.aggregate ? w.lines.aggregate() : null,"
            "   cells: w.lines.noiseCells(),"
            "   basemap: w.base.basemap,"
            # The day's own layers, by id. Counting the style's total asserts the symptom and
            # gets the wrong answer: `basemap app` leaves satellite remembered in
            # localStorage, so this window opens on a four-layer raster style and a threshold
            # tuned to liberty's hundred-odd fails on a day that drew correctly.
            "   own: ['all', 'all-hit', 'matched', 'focus'].filter((id) => map.getLayer(id)),"
            # Where the camera actually is, against where the day says it opens. Compared to
            # the app's own constant rather than to a copy here, so there is one definition.
            "   openAt: w.lines.openAt,"
            "   center: [map.getCenter().lng, map.getCenter().lat],"
            "   zoom: map.getZoom(),"
            # How many distinct runs are painted in that view.
            "   tracesOnScreen: map.getLayer('all')"
            "     ? new Set(map.queryRenderedFeatures({ layers: ['all'] })"
            "         .map((f) => f.properties.id)).size"
            "     : 0,"
            "   statusTop: sr ? Math.round(sr.top) : null,"
            "   areaBottom: ar ? Math.round(ar.bottom) : null,"
            "   creditTop: tr ? Math.round(tr.top) : null,"
            "   onStatus: String(onStatus),"
            "   cards: w.bodyEl.querySelectorAll('.overlay-cards .card').length }; })()"
        )
        if state and state.get("stage") == "ready":
            break
        time.sleep(0.6)

    if not state or state.get("stage") != "ready":
        stage = (state or {}).get("stage", "unknown")
        said = (state or {}).get("status", "")
        return [
            f"day 02: never became ready (stopped at {stage!r}"
            + (f", status says {said!r}" if said else "")
            + "). If this machine has no network, that is the cause, not the app."
        ]

    if state["activities"] < 1:
        bad.append(
            f"day 02: loaded {state['activities']} activities from {state['dataDir']!r} — "
            "neither private/ nor the committed data/ answered"
        )
    if state["cards"] < 3:
        bad.append(f"day 02: {state['cards']} of its own cards built, want at least 3")
    # `draw` is re-run from scratch against every style, so the day's layers must be present
    # whatever basemap this window opened on — including the raster ones, where a day that
    # only ever drew over liberty would silently have no overlay.
    missing = {"all", "all-hit", "matched", "focus"} - set(state["own"])
    if missing:
        bad.append(
            f"day 02: on the {state['basemap']!r} basemap these layers are absent: "
            f"{sorted(missing)} — the day's overlay was not added"
        )

    # The camera opens where the day says it opens, and nothing moves it afterwards.
    #
    # This is the assertion, and getting to it took two wrong ones. The day used to fit
    # `overview.home` — a bbox spanning a degree of longitude, from a city to the mountains
    # an hour east — which centred the view on the gap between two clusters of runs. The
    # first attempt asserted "some runs are on screen", which that camera satisfies: its
    # bbox does contain its own cluster, just as distant threads. The property that actually
    # distinguishes the two is whether the camera is where `OPEN_AT` put it.
    #
    # `openAt` is read from the app rather than copied here, so the expectation cannot drift
    # from the constant it is checking.
    want = state["openAt"]
    if not want:
        bad.append("day 02: the app exposes no openAt, so the opening camera cannot be checked")
    else:
        for axis, got, expected in (
            ("longitude", state["center"][0], want["center"][0]),
            ("latitude", state["center"][1], want["center"][1]),
        ):
            # A tenth of a degree is about 11 km of latitude: far tighter than the 1-degree
            # drift the old camera produced, and loose enough for a fitted or nudged view.
            if abs(got - expected) > 0.1:
                bad.append(
                    f"day 02: opens at {axis} {got:.3f}, want {expected:.3f} — something "
                    "moved the camera away from OPEN_AT"
                )
        if abs(state["zoom"] - want["zoom"]) > 1.0:
            bad.append(
                f"day 02: opens at zoom {state['zoom']:.1f}, want {want['zoom']} — at a "
                "wider zoom every trace is a thread and there is nothing to click"
            )
    if state["activities"] and state["tracesOnScreen"] < 1:
        bad.append(
            "day 02: the opening view contains no runs at all — the layers exist and the "
            "camera is pointed away from every one of them"
        )

    # The argument. Simplifying has to shorten, and the two ends have to differ by more than
    # rounding, or the slider is wired to something inert.
    if state["activities"]:
        # `select` fetches that run's full-resolution document, so it is asynchronous. Reading
        # the measurement in the same turn returns null and looks like a dead slider.
        wanted = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " const id = w.lines.activities()[0];"
            " w.lines.select(id); return id; })()"
        )
        measured = {"id": wanted, "selected": None, "raw": None, "coarse": None}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            measured = page.evaluate(
                "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
                " const at = (t) => w.lines.focusLengthAt(t);"
                " return { id: %s, selected: w.lines.selectedId(),"
                "   raw: at(0), coarse: at(60) }; })()" % json.dumps(wanted)
            )
            if measured["selected"] == wanted and measured["raw"]:
                break
            time.sleep(0.4)

        if measured["selected"] != measured["id"]:
            bad.append(
                f"day 02: selecting {measured['id']!r} left {measured['selected']!r} selected"
            )
        if not measured["raw"] or measured["raw"] <= 0:
            bad.append(f"day 02: the selected run measured {measured['raw']} m at full detail")
        elif not (measured["coarse"] < measured["raw"] * 0.999):
            # The day's claim, which outlived the control that used to demonstrate it: the
            # same trace measured at a coarser resolution is shorter. There is no slider any
            # more, so this asserts the arithmetic rather than a widget — and it is the
            # browser half of the pair `tools/gpsnoise.py` implements, so a divergence
            # between the two shows up here.
            bad.append(
                f"day 02: simplifying to 60 m took {measured['raw']:.0f} m to "
                f"{measured['coarse']:.0f} m — a coarser measurement must be shorter"
            )

        # The two ladders held against each other: the one `geo.mjs` computes in the browser
        # and the one `prepare.py` wrote into the selected run's document. Two
        # implementations of Douglas-Peucker over one trace, in two languages, and until now
        # nothing compared them — `shared/map/README.md` recorded the gap.
        #
        # Reached through the window, not through a fresh import, for the reason the icon
        # check records: `w.lines` is the instance the app is drawing with.
        pair = page.evaluate(DAY_02_LADDERS)
        if not pair["mine"] or not pair["written"]:
            bad.append("day 02: no ladder to compare — nothing selected, or the document carries none")
        elif len(pair["mine"]) != len(pair["written"]):
            bad.append(f"day 02: {len(pair['mine'])} rungs in the browser, {len(pair['written'])} written")
        else:
            over_floor = 0
            for (t_js, len_js, kept_js), (t_py, len_py, kept_py) in zip(pair["mine"], pair["written"]):
                if t_js != t_py:
                    bad.append(f"day 02: rung {t_js} m vs {t_py} m — different tolerances measured")
                    break
                # The strong half: exact on all 4,836 rungs measured, no tolerance wanted.
                if kept_js != kept_py:
                    bad.append(f"day 02: at {t_py} m the browser kept {kept_js} points, prepare.py "
                               f"kept {kept_py} — the two Douglas-Peucker implementations disagree")
                gap = abs(len_js - len_py)
                if gap > LADDER_FLOOR_M:
                    over_floor += 1
                    rel = gap / len_py
                    if rel > LADDER_REL_BAR:
                        bad.append(f"day 02: at {t_py} m the lengths are {gap * 1000:.2f} mm apart "
                                   f"({rel:.1e} relative), past the {LADDER_REL_BAR:.0e} bar")
            # One rung over the rounding floor is the known libm flip. Two or more is
            # arithmetic: a real divergence moves every rung, which is how the
            # one-decimal-place bug presented.
            if over_floor > 1:
                bad.append(f"day 02: {over_floor} of {len(pair['written'])} rungs exceed the "
                           f"{LADDER_FLOOR_M * 1000:.1f} mm rounding floor — a single flipped "
                           "decision affects one rung, so this is the arithmetic")

    # Both panes of the status bar have to be where a reader can see them: below the map area,
    # and with nothing on top. The status line was inside the scrolling overlay column once,
    # where four cards pushed it out of view while it still held the right text.
    if state["statusTop"] is None or state["areaBottom"] is None:
        bad.append("day 02: no status line or no map area to place it against")
    else:
        if state["statusTop"] < state["areaBottom"] - 1:
            bad.append(
                f"day 02: the status line starts at y={state['statusTop']} but the map area "
                f"runs to y={state['areaBottom']} — it is over the canvas and can be covered"
            )
        if "map-status" not in state["onStatus"]:
            bad.append(
                f"day 02: {state['onStatus']!r} sits on top of the status line — a failure "
                "reported there would be reported to nobody"
            )
    # A missing credit is a failure, not a skip. This read
    # `if state["creditTop"] is not None and ...`, which meant deleting `.map-attrib` — the
    # licence failure itself — made the check report ok. The same mistake as the
    # `.basemap-root` guard fixed earlier in this file, re-made two screens later.
    if state["creditTop"] is None:
        bad.append("day 02: no .map-attrib — the basemap credit is a licence term for three sources")
    elif state["areaBottom"] is not None and state["creditTop"] < state["areaBottom"] - 1:
        bad.append("day 02: the credit is over the canvas")
    if not re.search(r"\d", state["status"] or ""):
        bad.append(f"day 02: the status line says {state['status']!r}, with no figure in it")

    bad += day_02_panel_sweep(page)

    # Closed before handing the desktop back. This window is 1040x680 and lands on top of
    # almost everything, and `focus order` reads z-indexes and clicks taskbar buttons — it
    # failed on the window this check left behind. Closing it also exercises `win.onClose`,
    # which is where the WebGL context is released; a leak shows up as a later map failing to
    # create at all.
    closed = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
        " if (!w) return 'already gone'; w.close(); return ''; })()"
    )
    time.sleep(0.4)
    still_open = page.evaluate(
        "window.webxp.windows.some((x) => x.programId === 'day-02-lines')"
    )
    if still_open:
        bad.append(f"day 02: the window did not close ({closed or 'close() returned'})")
    return bad


def check_day_02_segment_spans(page) -> list[str]:
    """Every segment crossing must address the activity document it names.

    Both overlay files store a crossing as an index span `[i0, i1]` into one activity's
    `recorded` array, and `lines.mjs` slices that array to draw the ghost worm. So an
    overlay is only meaningful against the per-activity documents it was built from, and
    re-measuring the export with a different `--trim-m` shifts every index.

    The failure is silent in both directions, which is why it needs a check rather than a
    glance. A span past the end slices short, the crossing is dropped, and a noisy street
    reads as a quiet one. A span still in range after a shift points at a different stretch
    of road and is drawn with no indication that anything is wrong.

    Counted apart because they need different fixes: a crossing naming an activity that is
    gone means the export lost a run, and a span past the end means the run was shortened.

    Needs no browser. It reads the files `lines.mjs` would fetch, which is the point — this
    fails on a checkout with stale data, before anything is rendered.
    """
    bad: list[str] = []
    app = REPO_ROOT / "apps" / "day-02-lines"
    examined: list[str] = []
    for name in DAY_02_DATA_DIRS:
        data_dir = app / name
        index = data_dir / "index.json"
        if not index.is_file():
            continue
        points = {a["id"]: a["points"] for a in json.loads(index.read_text())["activities"]}
        for overlay in DAY_02_OVERLAYS:
            path = data_dir / overlay
            if not path.is_file():
                continue
            examined.append(f"{name}/{overlay}")
            doc = json.loads(path.read_text())
            total = past_end = unknown = 0
            for feature in doc.get("features", []):
                for crossing in feature["properties"].get("passes", []):
                    total += 1
                    n = points.get(crossing["activity"])
                    if n is None:
                        unknown += 1
                    elif crossing["i1"] >= n:
                        past_end += 1
            # An overlay with no crossing in it is this check's own vacuous pass, in the one
            # costume the guard below cannot see through: the file exists, so `examined` is
            # not empty, the loops above run zero times, and nothing is found wrong. A fixture
            # that degraded to empty, or a generator whose gates stopped admitting anything,
            # would read as a clean result.
            #
            # Measured by pointing `REPO_ROOT` at a copy of the tree whose two overlays were
            # replaced by `{"type": "FeatureCollection", "features": []}` and calling this
            # function directly: before this block it returned no finding at all, and the
            # same copy with the real overlays in place returns none either way. The
            # generators refuse to write such a file
            # (`apps/day-02-lines/tools/street_segments.py`, `noise_by_area.py`) and
            # `tools/test_pipeline.py` asserts the counts over the committed pair; this is the
            # same assertion at the layer that reads the file.
            if not total:
                bad.append(
                    f"day 02: {name}/{overlay} carries "
                    f"{len(doc.get('features', []))} segment(s) and no crossing at all, so "
                    "no span was checked against any activity document. Not a clean result "
                    "— nothing was measured."
                )
            if past_end or unknown:
                bad.append(
                    f"day 02: {name}/{overlay} has {past_end} of {total} crossings indexing "
                    f"past the end of their run and {unknown} naming a run that is gone — "
                    "the overlay predates the activity documents beside it, so the "
                    "drill-down draws the wrong road. Rebuild it."
                )
    # Named, never silent. Both loops above are keyed on a file being there, so a checkout
    # carrying no overlay under `private/` or `data/` runs them zero times and this check
    # would otherwise report ok having read nothing. What a clean clone measures is the
    # committed synthetic pair in `data/`, written from the fixture by
    # `apps/day-02-lines/tools/street_segments.py` and `noise_by_area.py`. That is the shape
    # `docs/review.md` calls a check whose precondition can go false without failing, and it
    # is the one outcome this file may not produce.
    if not examined:
        bad.append(
            "day 02: no overlay file was found under "
            + " or ".join(f"apps/day-02-lines/{name}/" for name in DAY_02_DATA_DIRS)
            + f" ({', '.join(DAY_02_OVERLAYS)}), so no crossing was checked against any "
            "activity document. Not a clean result — nothing was measured."
        )
    return bad


def check_day_02_report(page) -> list[str]:
    """`report.py` runs to completion over every measured directory on the machine.

    It is the documented reproduction path for the day's figures, so a crash there makes
    every citation to it dangling. It crashed once for a reason worth guarding: `have_all`
    required a Strava distance as well as a matched one, and cropping makes the Strava
    column unresolvable — so the set came out empty, the totals printed as zeroes and
    `+nan%`, and `np.percentile` then raised. A tool that cannot answer must say which
    input it is missing.

    `-I` because the day's tools are run that way everywhere else: it keeps the script's
    own directory off `sys.path` so a stray module beside the data cannot be imported.
    """
    bad: list[str] = []
    app = REPO_ROOT / "apps" / "day-02-lines"
    tool = app / "tools" / "report.py"
    if not tool.is_file():
        return [f"day 02: no {tool.relative_to(REPO_ROOT)} to run"]
    ran: list[str] = []
    for name in DAY_02_DATA_DIRS:
        data_dir = app / name
        if not (data_dir / "index.json").is_file():
            continue
        ran.append(name)
        result = subprocess.run(
            [sys.executable, "-I", str(tool), str(data_dir)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            tail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            bad.append(
                f"day 02: report.py exited {result.returncode} over {name}/ — "
                + " | ".join(tail[-3:])
            )
    if not ran:
        bad.append(
            "day 02: no directory under apps/day-02-lines/ carries an index.json, so "
            "report.py was never run. Not a clean result — nothing was measured."
        )
    return bad


def check_manifest_current(page) -> list[str]:
    """The generated manifest agrees with the folders on disk.

    A stale manifest is the one failure mode of this whole arrangement that looks like
    nothing: the app is simply not there. This runs the same `--check` a person would.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "apps.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        return [
            "manifest: scripts/apps.py --check failed — "
            + (result.stderr.strip() or result.stdout.strip()).replace("\n", " | ")
        ]
    return []


#: Routes, parsed and formatted without a browser's help, in the browser.
#:
#: The pure half of the router asserted directly: these are the cases a hand-written hash can
#: be wrong in, and none of them needs a desktop to be standing.
ROUTE_ASSERTIONS = """(async () => {
  const bad = [];
  // The page's own router, not a second copy. A missing one is a failure, not a skip.
  const routes = window.webxpRoutes;
  if (!routes) return ['window.webxpRoutes is absent — the host page published no router'];
  const want = (label, got, expected) => {
    if (JSON.stringify(got) !== JSON.stringify(expected)) {
      bad.push(label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(expected));
    }
  };
  const shape = (hash) => {
    const route = routes.parse(hash);
    if (!route) return null;
    return { id: route.id, path: route.path, params: [...route.params] };
  };

  want('no hash is no route', shape(''), null);
  want('a bare # is no route', shape('#'), null);
  // A browser produces a bare `#` for "top of page", and reading that as a route would open
  // a window for a reader who clicked an anchor.
  want('an empty route is no route', shape('#/'), null);
  want('a plain id', shape('#/terminal'), { id: 'terminal', path: '', params: [] });
  want('an id and a path', shape('#/explorer/home/user/notes'),
       { id: 'explorer', path: 'home/user/notes', params: [] });
  want('a query', shape('#/notepad/etc/motd?ro=1'),
       { id: 'notepad', path: 'etc/motd', params: [['ro', '1']] });
  // Each segment is decoded on its own, so `home` and `a%2Fb` are two components and the
  // result is `home/a/b`.
  //
  // Which means an encoded slash and a real one are indistinguishable once joined:
  // `#/explorer/home/a%2Fb` and `#/explorer/home/a/b` both arrive as `home/a/b`. Nothing is
  // lost, because a name in this filesystem cannot contain a slash — `#lookup` splits paths
  // on it — so there is no file the ambiguity could hide.
  want('each segment decodes on its own', shape('#/explorer/home/a%2Fb'),
       { id: 'explorer', path: 'home/a/b', params: [] });
  want('a space', shape('#/notepad/home/user/my%20notes.txt'),
       { id: 'notepad', path: 'home/user/my notes.txt', params: [] });

  want('format a plain id', routes.format('terminal'), '#/terminal');
  want('format a path', routes.format('explorer', { path: '/home/user' }), '#/explorer/home/user');
  want('format drops a trailing slash', routes.format('explorer', { path: '/home/user/' }),
       '#/explorer/home/user');
  want('format a query', routes.format('notepad', { path: '/etc/motd', params: { ro: '1' } }),
       '#/notepad/etc/motd?ro=1');
  // A round trip is the property that matters: what a window publishes must parse back to
  // the state it published.
  for (const [id, state] of [
    ['terminal', { path: '/usr/src/webxp' }],
    ['explorer', { path: '/home/user/links' }],
    ['notepad', { path: '/home/user/my notes.txt' }],
  ]) {
    const back = routes.parse(routes.format(id, state));
    want('round trip ' + id, { id: back.id, path: routes.asAbsolute(back.path) },
         { id, path: state.path });
  }

  want('asAbsolute adds the slash', routes.asAbsolute('home/user'), '/home/user');
  want('asAbsolute keeps one', routes.asAbsolute('/home/user'), '/home/user');
  want('asAbsolute on nothing', routes.asAbsolute(''), '');
  return bad;
})()"""


def check_routes(page) -> list[str]:
    """A link opens a window on the state it names, and a window publishes a link to itself.

    The reason this exists at all: before it, nothing in the desktop was linkable. Open a
    folder, drill into a street, and there was no URL to send anybody — `grep -rn
    'location.hash'` over the source returned nothing.
    """
    bad = []
    wait_for_apps(page)
    try:
        found = page.evaluate(ROUTE_ASSERTIONS, await_promise=True)
    except CDPError as error:
        return [f"routes: the assertion table could not run — {error}"]
    bad += [f"routes: {message}" for message in found]

    # A link, loaded the way a reader loads one: the hash in place, the document fresh.
    #
    # Set-then-reload rather than `navigate` to the hashed URL: a navigation that differs
    # only by the fragment is a same-document navigation, so no load event fires and the
    # harness's `navigate` waits for one until the channel to Chrome times out. Which is
    # itself the right behaviour to know about — the first version of this check killed the
    # run.
    def open_link(hash_: str) -> None:
        page.evaluate("window.location.hash = %s" % json.dumps(hash_))
        page.call("Page.reload", ignoreCache=True)
        time.sleep(0.8)
        wait_for_route(page)
        # The window the route launched is awaited inside `restore`, but its mount and the
        # loading floor are not.
        time.sleep(0.6)

    for hash_, program, expect in (
        ("#/explorer/usr/src/webxp/src", "explorer", "/usr/src/webxp/src"),
        ("#/terminal/etc", "terminal", "/etc"),
        ("#/notepad/etc/motd", "notepad", "motd"),
    ):
        open_link(hash_)
        console_errors(page)
        state = page.evaluate(
            "(() => { const route = window.webxpRoute;"
            " const win = window.webxp.windows.find((w) => w.programId === %s);"
            " if (!win) return { route, opened: false };"
            " return { route, opened: true, title: win.titleText,"
            "          address: (win.el.querySelector('.exp-address') || {}).value || null,"
            "          prompt: (win.el.querySelector('.term-prompt') || {}).textContent || null,"
            "          status: win.statusEl ? win.statusEl.textContent : '' }; })()"
            % json.dumps(program)
        )
        if not state["opened"]:
            bad.append(f"routes: {hash_} opened no {program} window — route was {state['route']!r}")
            continue
        # Each app reports where it landed differently; one of the three has to carry it.
        where = state["address"] or state["prompt"] or state["status"]
        if expect not in (where or ""):
            bad.append(f"routes: {hash_} opened {program} at {where!r}, want {expect!r}")
        # And only that window: a link names one app and its state, not a desktop.
        others = page.evaluate("window.webxp.windows.length")
        if others != 1:
            bad.append(f"routes: {hash_} opened {others} windows, want 1")

    # A link to a program that does not exist is a dead link, and must say so rather than
    # opening nothing in silence.
    open_link("#/no-such-program")
    dead = page.evaluate("window.webxpRoute")
    if not dead or dead.get("opened") is not False:
        bad.append(f"routes: a link to a missing program reported {dead!r}")
    console_errors(page)

    # And the other direction: a window publishes a link to itself as it moves.
    page.evaluate("window.history.replaceState(null, '', window.location.pathname)")
    page.call("Page.reload", ignoreCache=True)
    time.sleep(0.8)
    wait_for_route(page)
    console_errors(page)
    shell = terminal_session(page)
    term_run(page, "cd /usr/src/webxp/docs", shell)
    time.sleep(0.2)
    published = page.evaluate("window.location.hash")
    if published != "#/terminal/usr/src/webxp/docs":
        bad.append(f"routes: after a cd the address bar reads {published!r}")
    # `replaceState`, so using a window does not fill the back button with steps nobody took.
    before = page.evaluate("window.history.length")
    term_run(page, "cd /etc", shell)
    time.sleep(0.2)
    after = page.evaluate("window.history.length")
    if after != before:
        bad.append(
            f"routes: a cd added {after - before} history entries — the back button now "
            "steps through someone's working directory"
        )
    if page.evaluate("window.location.hash") != "#/terminal/etc":
        bad.append("routes: the published link did not follow the second cd")

    # `/pages`: a view of the route table, in the filesystem, through the pane.
    #
    # Driven from the shell because that is the artifact the feature is for — "the CLI
    # traverses the pages" is the ask, and a directory nobody can `cd` into would satisfy an
    # in-page assertion while failing the request.
    listing = term_run(page, "ls /pages", shell)
    for program in ("terminal", "explorer", "notepad", "basemap"):
        if program not in listing:
            bad.append(f"routes: /pages does not list {program} — {listing.strip()[:120]!r}")
    described = term_run(page, "cat /pages/terminal", shell)
    for fragment in ("app:   terminal", "link:  #/terminal", "state: a path"):
        if fragment not in described:
            bad.append(f"routes: cat /pages/terminal is missing {fragment!r}")
    # An app that restores no state says so by omission rather than by claiming it does.
    plain = term_run(page, "cat /pages/about", shell)
    if "state:" in plain:
        bad.append(f"routes: /pages/about claims state it cannot restore — {plain.strip()[:120]!r}")
    if "link:  #/about" not in plain:
        bad.append(f"routes: /pages/about carries no link — {plain.strip()[:120]!r}")

    # The link a page advertises has to be the link that works. Read it out of the file and
    # open it, rather than asserting a string that happens to match one written elsewhere.
    advertised = term_run(page, "grep link: /pages/explorer", shell)
    link = advertised.split("link:")[-1].strip().split()[0] if "link:" in advertised else ""
    if not link.startswith("#/"):
        bad.append(f"routes: /pages/explorer advertises {link!r}, which is not a link")
    else:
        open_link(link)
        if not page.evaluate(
            "window.webxp.windows.some((w) => w.programId === 'explorer')"
        ):
            bad.append(f"routes: the link /pages/explorer advertises ({link}) opened nothing")
        page.evaluate("window.history.replaceState(null, '', window.location.pathname)")
        page.call("Page.reload", ignoreCache=True)
        time.sleep(1.0)
        wait_for_apps(page)
        console_errors(page)
        shell = terminal_session(page)

    # Read-only, and refused by the mode bits like the repository mount.
    for line, want_text in (
        ("echo x > /pages/terminal", "Permission denied"),
        ("rm /pages/terminal", "Permission denied"),
        ("chmod 644 /pages/terminal", "Mounted read-only"),
        ("touch /pages/terminal", "Permission denied"),
    ):
        refused = term_run(page, line, shell)
        if want_text not in refused:
            bad.append(f"routes: {line!r} was not refused — {refused.strip()[:120]!r}")

    fs_reset(page)
    return bad

def check_mount_index(page) -> list[str]:
    """The index agrees with the tracked files, and lists nothing it must not.

    Two separate claims. The first is the stale-generated-file problem `manifest is current`
    already guards for `apps/`. The second is a privacy rule: `.gitignore` excludes
    `apps/day-*/private/` because it holds real Strava traces, and an index built by walking
    the working tree would mount them into a filesystem with a browsable `ls`. That
    directory is absent from some worktrees and present in others, so the walk produces a
    clean index on one machine and publishes the traces from another — and the clean run is
    the one you would trust. Asserted against the index rather than against the generator's
    intent, because the index is what ships.
    """
    bad = []
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "mount.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        bad.append(
            "mount index: scripts/mount.py --check failed — "
            + (result.stderr.strip() or result.stdout.strip()).replace("\n", " | ")
        )

    index_path = REPO_ROOT / "shared" / "fs" / "mount.json"
    if not index_path.is_file():
        return bad + ["mount index: shared/fs/mount.json does not exist"]
    index = json.loads(index_path.read_text())
    rows = index.get("files") or []
    if not rows:
        return bad + ["mount index: no files listed, so nothing below is being tested"]

    for row in rows:
        if "/private/" in row[0] or row[0].endswith("/private"):
            bad.append(f"mount index: lists {row[0]} — ignored private data must never be indexed")

    # And every indexed path is tracked. A path git does not know is a path `.gitignore` was
    # not consulted about.
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True, check=False,
    )
    if tracked.returncode != 0:
        bad.append("mount index: could not ask git what is tracked")
    else:
        known = {name for name in tracked.stdout.decode().split("\0") if name}
        for row in rows:
            if row[0] not in known:
                bad.append(f"mount index: lists {row[0]}, which git does not track")
    return bad


def check_scaffold(page) -> list[str]:
    """Adding a folder adds an app: scaffold, reload, and it is there and it launches.

    This is the end-to-end form of the contract, and the only check that exercises the path a
    person actually takes. It reloads the page, so it runs last.
    """
    bad = []
    app_id = "scaffold-probe"
    folder = REPO_ROOT / "apps" / app_id
    created = False
    try:
        made = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "apps.py"), "--new", app_id],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if made.returncode != 0:
            return [f"scaffold: --new failed: {made.stderr.strip() or made.stdout.strip()}"]
        created = True
        if not (folder / "app.mjs").is_file() or not (folder / "app.css").is_file():
            bad.append("scaffold: --new did not write both app.mjs and app.css")

        page.navigate(BASE_URL + "/")
        time.sleep(1.2)

        state = page.evaluate(
            "(async () => {"
            " const { APPS } = await import('/apps/manifest.mjs');"
            " return { loaded: window.webxpApps.loaded,"
            "   broken: window.webxpApps.broken,"
            "   manifest: APPS,"
            "   inMenu: [...document.querySelectorAll('.start-item')]"
            "     .some((b) => b.textContent.includes('Scaffold Probe')) }; })()",
            await_promise=True,
        )
        # Asked of the browser, not of the disk. If the generator wrote the app and the page
        # cannot see it, the page is reading a stale manifest — a different fault with a
        # different fix, and one this check used to report as "the manifest missed it".
        on_disk = (REPO_ROOT / "apps" / app_id / "app.mjs").is_file()
        if on_disk and app_id not in state["manifest"]:
            bad.append(
                f"scaffold: apps/{app_id}/app.mjs is on disk and the manifest the browser "
                "loaded does not list it — a stale read, not a generator fault"
            )
        if app_id in state["broken"]:
            bad.append(
                f"scaffold: the generated app does not satisfy the contract it was generated "
                f"from: {state['broken'][app_id]}"
            )
        if app_id not in state["loaded"]:
            bad.append(f"scaffold: {app_id} is not in the loaded list — the manifest missed it")
        if not state["inMenu"]:
            bad.append("scaffold: the new app is not in the Start menu")

        # And it has to actually open. A scaffold that produces an app which fails to mount
        # is worse than no scaffold, because it teaches the contract wrong.
        page.evaluate(f"window.webxp.launch({json.dumps(app_id)})")
        time.sleep(0.8)
        opened = page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === %s);"
            " if (!w) return null;"
            " return { loading: w.bodyEl.classList.contains('is-loading'),"
            "          text: w.bodyEl.textContent.slice(0, 60),"
            "          bodyClass: w.bodyEl.className }; })()"
            % json.dumps(app_id)
        )
        if opened is None:
            bad.append("scaffold: the new app did not open")
        else:
            if opened["loading"]:
                bad.append("scaffold: the new app is stuck on the loading marquee")
            if f"app-{app_id}" not in opened["bodyClass"]:
                bad.append(f"scaffold: body class is {opened['bodyClass']!r}, want app-{app_id}")
            if "Scaffold Probe" not in opened["text"]:
                bad.append(f"scaffold: the window does not show its content — {opened['text']!r}")
    finally:
        if created:
            shutil.rmtree(folder, ignore_errors=True)
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "apps.py")],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        page.navigate(BASE_URL + "/")
        time.sleep(1.0)
        # The scaffolded app's stylesheet 404s nothing and the reloads log nothing, but the
        # navigations buffer events that the end-of-run sweep would otherwise attribute to
        # whatever ran last.
        drain(page)
    return bad


# ---------------------------------------------------------------------------
# The filesystem, the shell, and the two apps that share a mount.
# ---------------------------------------------------------------------------

#: The model's own assertions, run in the browser that will run it.
#:
#: Against a filesystem built from the seed rather than against the mounted one, so the
#: result does not depend on what is in `localStorage` — and so a failure here is a failure
#: of the model rather than of persistence, which `filesystem persists` covers separately.
FS_ASSERTIONS = """(async () => {
  const bad = [];
  // An `import()`, because there is no published global for the filesystem model and these
  // are module-level constants and free functions rather than state a window owns. One URL
  // in one realm is one instance, so this is the same `MAX_LINKS` the shell enforces rather
  // than a copy of it — and naming the ceiling is the point: a check that writes `40` here
  // cannot catch the ceiling changing.
  const { VFS, MAX_FILE_SIZE, MAX_LINKS, basename, normalize } =
    await import('/shared/fs/vfs.mjs');
  const { buildSeed, HOME } = await import('/shared/fs/seed.mjs');
  const fs = new VFS();
  buildSeed(fs);

  // Both take a thunk, so an assertion that throws is one failure with a label on it rather
  // than an exception that abandons the table. The first version evaluated eagerly, and a
  // negative control that broke symlink resolution reported one `undefined.slice` from
  // somewhere in a hundred lines.
  const want = (label, thunk, expected) => {
    let got;
    try {
      got = thunk();
    } catch (error) {
      bad.push(label + ': threw ' + (error.code || '') + ' ' + error.message);
      return;
    }
    if (got !== expected) {
      bad.push(label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(expected));
    }
  };
  const refuses = (label, code, thunk) => {
    try {
      thunk();
      bad.push(label + ': expected ' + code + ', nothing was thrown');
    } catch (error) {
      if (error.code !== code) {
        bad.push(label + ': expected ' + code + ', got ' + (error.code || error.message));
      }
    }
  };
  // A step the table depends on rather than asserts — a fixture. A throw here is reported
  // and the table carries on, because the assertions after it are still worth running.
  const setup = (label, thunk) => {
    try {
      thunk();
    } catch (error) {
      bad.push('while setting up ' + label + ': ' + (error.code || '') + ' ' + error.message);
    }
  };

  // Nested directories, and a parent that knows its child.
  setup('a nested tree', () => fs.mkdir('/t/a/b/c', { recursive: true }));
  want('mkdir -p type', () => fs.stat('/t/a/b/c').type, 'dir');
  want('nested parent', () => fs.realpath('/t/a/b/c/../..'), '/t/a');
  refuses('mkdir over a file', 'EEXIST', () => fs.mkdir('/etc/motd'));
  refuses('mkdir under a file', 'ENOTDIR', () => fs.mkdir('/etc/motd/x'));

  // Symlinks: relative, absolute, through a directory, and the dangling one.
  want('relative link', () => fs.realpath(HOME + '/links/relative'), HOME + '/readme.txt');
  want('absolute link', () => fs.readFile(HOME + '/links/absolute').slice(0, 7), 'Windows');
  want('link to a dir', () => fs.realpath(HOME + '/links/relative-dir'), HOME + '/notes');
  want('read through a dir link',
       () => fs.readFile(HOME + '/links/relative-dir/todo.txt').slice(0, 2), '- ');
  want('lstat sees the link', () => fs.lstat(HOME + '/links/broken').type, 'link');
  refuses('dangling link reads', 'ENOENT', () => fs.readFile(HOME + '/links/broken'));

  // `..` after a symlink is the parent of the target, not of the link. This is the pair
  // that makes pwd and pwd -P different commands.
  setup('a link to notes', () => fs.symlink(HOME + '/notes', '/t/toNotes'));
  want('physical ..', () => fs.realpath('/t/toNotes/..'), HOME);
  want('lexical ..', () => normalize('/t/toNotes/..'), '/t');

  // A write through a dangling link creates its target, as POSIX does.
  setup('a write through a dangling link',
        () => fs.writeFile(HOME + '/links/broken', 'created through the link\\n'));
  want('write through a dangling link', () => fs.readFile(HOME + '/links/nowhere.txt'),
       'created through the link\\n');

  // A cycle is not distinguishable from a long chain, so the answer is a limit.
  setup('two links pointing at each other', () => {
    fs.symlink('loopB', '/t/loopA');
    fs.symlink('loopA', '/t/loopB');
  });
  refuses('mutual loop', 'ELOOP', () => fs.readFile('/t/loopA'));
  refuses('self loop', 'ELOOP', () => fs.readFile(HOME + '/links/loop'));

  // Hard links: one inode, two names.
  setup('a hard link', () => {
    fs.writeFile('/t/one', 'first\\n');
    fs.link('/t/one', '/t/two');
  });
  want('nlink after link', () => fs.lstat('/t/one').nlink, 2);
  want('same inode', () => fs.lstat('/t/one').ino, fs.lstat('/t/two').ino);
  setup('an append through one name', () => fs.appendFile('/t/one', 'second\\n'));
  want('shared contents', () => fs.readFile('/t/two'), 'first\\nsecond\\n');
  setup('removing one name', () => fs.unlink('/t/one'));
  want('survives one unlink', () => fs.readFile('/t/two'), 'first\\nsecond\\n');
  want('nlink after unlink', () => fs.lstat('/t/two').nlink, 1);
  want('seeded hard link', () => fs.lstat(HOME + '/notes/ideas.txt').nlink, 2);
  refuses('hard link to a directory', 'EPERM', () => fs.link('/t/a', '/t/aLink'));

  // Renames, including the two that would corrupt the tree.
  refuses('rename into its own subtree', 'EINVAL', () => fs.rename('/t/a', '/t/a/b/inside'));
  setup('a directory with something in it', () => {
    fs.mkdir('/t/full');
    fs.writeFile('/t/full/x', 'x');
  });
  refuses('rename over a full directory', 'ENOTEMPTY', () => fs.rename('/t/a', '/t/full'));
  setup('a rename', () => fs.rename('/t/two', '/t/renamed'));
  want('rename moved it', () => fs.readFile('/t/renamed'), 'first\\nsecond\\n');
  refuses('the old name is gone', 'ENOENT', () => fs.readFile('/t/two'));
  setup('moving a directory', () => {
    fs.mkdir('/t/newhome');
    fs.rename('/t/a', '/t/newhome/a');
  });
  want('a moved directory keeps its subtree', () => fs.stat('/t/newhome/a/b/c').type, 'dir');
  want('a moved directory knows its parent', () => fs.realpath('/t/newhome/a/..'), '/t/newhome');

  // Kinds are not interchangeable.
  refuses('rmdir on a full directory', 'ENOTEMPTY', () => fs.rmdir('/t/full'));
  refuses('unlink on a directory', 'EISDIR', () => fs.unlink('/t/full'));
  refuses('read a directory', 'EISDIR', () => fs.readFile('/t/full'));
  refuses('list a file', 'ENOTDIR', () => fs.readdir('/etc/motd'));
  refuses('readlink a file', 'EINVAL', () => fs.readlink('/etc/motd'));
  setup('emptying a directory', () => {
    fs.unlink('/t/full/x');
    fs.rmdir('/t/full');
  });
  refuses('the directory is gone', 'ENOENT', () => fs.stat('/t/full'));

  // The mode bits, in both directions. A permission that only ever refuses is a permission
  // nobody has shown to be read at all.
  refuses('mode 000 refuses a read', 'EACCES', () => fs.readFile('/etc/secret.conf'));
  setup('a file to lock', () => {
    fs.writeFile('/t/locked', 'open\\n');
    fs.chmod('/t/locked', 0o000);
  });
  refuses('chmod 000 refuses', 'EACCES', () => fs.readFile('/t/locked'));
  refuses('chmod 000 refuses a write', 'EACCES', () => fs.writeFile('/t/locked', 'no'));
  setup('unlocking it', () => fs.chmod('/t/locked', 0o644));
  want('chmod 644 allows it again', () => fs.readFile('/t/locked'), 'open\\n');
  // Execute on a directory is the right to enter it, which is a different bit from read.
  setup('a directory to seal', () => {
    fs.mkdir('/t/sealed');
    fs.writeFile('/t/sealed/inside', 'hidden\\n');
    fs.chmod('/t/sealed', 0o644);
  });
  refuses('a directory without x cannot be entered', 'EACCES',
          () => fs.readFile('/t/sealed/inside'));
  setup('x only', () => fs.chmod('/t/sealed', 0o111));
  want('a directory with only x can be entered', () => fs.readFile('/t/sealed/inside'),
       'hidden\\n');
  refuses('a directory without r cannot be listed', 'EACCES', () => fs.readdir('/t/sealed'));
  setup('r and x', () => fs.chmod('/t/sealed', 0o755));
  want('r and x together list it', () => fs.readdir('/t/sealed').join(), 'inside');

  // The ceiling exists so a write cannot be accepted and then fail at save time.
  refuses('past the file ceiling', 'ENOSPC',
          () => fs.writeFile('/t/big', 'x'.repeat(MAX_FILE_SIZE + 1)));
  // And it leaves nothing behind. The first version created the inode and the name before
  // measuring, so a refused write left an empty file — one that was also missing from
  // storage, because the throw skipped the event the save listens to.
  refuses('a refused write creates nothing', 'ENOENT', () => fs.stat('/t/big'));

  // Walking reports symlinks and does not follow them: a link to an ancestor would make
  // the walk infinite.
  want('walk reports the link',
       () => [...fs.walk(HOME + '/links')].some((entry) => entry.name === 'relative-dir'), true);
  want('walk did not descend it',
       () => [...fs.walk(HOME + '/links')].some((entry) => entry.name === 'todo.txt'), false);
  want('maxdepth', () => [...fs.walk('/t/newhome', { maxDepth: 1 })].length, 2);

  // Watchers see the op and the path, and stop when they are told to.
  const seen = [];
  setup('a watcher', () => {
    const unwatch = fs.watch((event) => seen.push(event.op + ' ' + event.path));
    fs.writeFile('/t/watched', 'x');
    unwatch();
    fs.writeFile('/t/watched', 'y');
  });
  want('the watcher saw the write, once', () => seen.join(), 'write /t/watched');

  // Serialisation: a round trip keeps the structure, including the two names on one inode.
  const image = JSON.parse(JSON.stringify(fs.serialize()));
  let copy = null;
  setup('a round trip', () => { copy = VFS.parse(image); });
  want('round trip contents', () => copy.readFile('/t/renamed'), 'first\\nsecond\\n');
  want('round trip symlink', () => copy.readlink(HOME + '/links/relative'), '../readme.txt');
  want('round trip mode', () => copy.lstat('/etc/secret.conf').mode, 0);
  want('round trip hard link', () => copy.lstat(HOME + '/links/ideas-too').ino,
       copy.lstat(HOME + '/notes/ideas.txt').ino);
  want('round trip stats', () => JSON.stringify(copy.stats()), JSON.stringify(fs.stats()));

  // And the validator refuses an image that would not hold together, which is the whole
  // reason it is a validator and not a cast.
  refuses('an entry pointing at nothing', 'EINVAL', () => {
    const corrupt = JSON.parse(JSON.stringify(image));
    corrupt.inodes.find((row) => row.ino === 1).entries.push(['ghost', 9999]);
    VFS.parse(corrupt);
  });
  refuses('a version from the future', 'EINVAL',
          () => VFS.parse({ ...image, version: image.version + 1 }));
  // An entry list that is not a list of pairs. `new Map([1, 2])` throws a TypeError, which
  // is not an FsError, which made the mount rethrow instead of reseeding: one malformed
  // byte in localStorage and both apps failed to start on every launch, with no way back
  // from inside the desktop.
  refuses('entries that are not pairs', 'EINVAL', () => {
    const corrupt = JSON.parse(JSON.stringify(image));
    corrupt.inodes.find((row) => row.ino === 1).entries = [1, 2];
    VFS.parse(corrupt);
  });
  refuses('an entry whose name is not a string', 'EINVAL', () => {
    const corrupt = JSON.parse(JSON.stringify(image));
    corrupt.inodes.find((row) => row.ino === 1).entries.push([7, 1]);
    VFS.parse(corrupt);
  });
  // Refused rather than defaulted. A directory with no usable entry list came back as an
  // empty directory, which restores a tree with every file silently missing.
  refuses('a directory with no entry list', 'EINVAL', () => {
    const corrupt = JSON.parse(JSON.stringify(image));
    corrupt.inodes.find((row) => row.ino === 1).entries = 'nope';
    VFS.parse(corrupt);
  });
  refuses('a symlink with no target', 'EINVAL', () => {
    const corrupt = JSON.parse(JSON.stringify(image));
    delete corrupt.inodes.find((row) => row.type === 'link').target;
    VFS.parse(corrupt);
  });
  refuses('a file with no contents', 'EINVAL', () => {
    const corrupt = JSON.parse(JSON.stringify(image));
    delete corrupt.inodes.find((row) => row.type === 'file').data;
    VFS.parse(corrupt);
  });

  // `splitPath` drops `.`, so a basename that fell back to '/' answered '/' for '.' — and
  // `joinPath(dest, '/')` is the root, which is where `cp -r . dest` wrote.
  want('basename of a dotted path', () => basename('.'), '.');
  want('basename of the root', () => basename('/'), '/');

  // One ceiling, one value. `realpath` used MAX_LINKS * 8, so it printed a path for a chain
  // `cat` called unreachable.
  setup('a chain longer than the ceiling', () => {
    fs.mkdir('/t/chain');
    fs.writeFile('/t/chain/end', 'end\\n');
    fs.symlink('end', '/t/chain/l0');
    for (let i = 1; i <= MAX_LINKS + 19; i += 1) fs.symlink('l' + (i - 1), '/t/chain/l' + i);
  });
  refuses('a chain past the ceiling does not resolve', 'ELOOP',
          () => fs.readFile('/t/chain/l' + (MAX_LINKS + 19)));
  refuses('and realpath refuses the same chain', 'ELOOP',
          () => fs.realpath('/t/chain/l' + (MAX_LINKS + 19)));
  want('a chain inside the ceiling still resolves', () => fs.realpath('/t/chain/l10'),
       '/t/chain/end');

  // The seed's relative link to a directory is relative. Holding an absolute target, the
  // name contradicted `ls -l`.
  want('relative-dir is relative', () => fs.readlink(HOME + '/links/relative-dir'),
       '../notes');

  // A write that does not pass through `writeFile` inherits none of its guards. `touch`
  // stamped an existing inode directly, so `chmod 444 f` then `touch f` moved the mtime and
  // announced a change on a file the caller may not write — and the announcement is what the
  // debounced save in `fs.mjs` acts on.
  setup('a file to lock against touching', () => {
    fs.writeFile('/t/untouchable', 'open\\n');
    fs.chmod('/t/untouchable', 0o444);
    // Date.now() is milliseconds, so a stamp inside the same millisecond as the write would
    // be indistinguishable from no stamp at all. Waited out, so this can fail.
    const until = Date.now() + 3;
    while (Date.now() < until);
  });
  let lockedAt = null;
  setup("the locked file's timestamp", () => { lockedAt = fs.lstat('/t/untouchable').mtime; });
  const whileLocked = [];
  setup('watching a refused touch', () => {
    const unwatch = fs.watch((event) => whileLocked.push(event.op + ' ' + event.path));
    refuses('touch without the write bit', 'EACCES', () => fs.touch('/t/untouchable'));
    unwatch();
  });
  want('and the mtime did not move', () => fs.lstat('/t/untouchable').mtime, lockedAt);
  // A refusal that still notifies has told the saver that a read-only file changed.
  want('and nothing was announced', () => whileLocked.join(), '');
  want('and the contents are untouched', () => fs.readFile('/t/untouchable'), 'open\\n');
  // Not a blanket refusal. The create branch, and a touch the caller may make, both work.
  want('touch creates a name in a directory the caller may write',
       () => { fs.touch('/t/touched'); return fs.lstat('/t/touched').size; }, 0);
  setup('unlocking it, a moment later', () => {
    fs.chmod('/t/untouchable', 0o644);
    const until = Date.now() + 3;
    while (Date.now() < until);
  });
  // `chmod` moves the ctime and not the mtime, so only the touch can satisfy this.
  want('touch moves the mtime once the bit is back', () => {
    fs.touch('/t/untouchable');
    return fs.lstat('/t/untouchable').mtime > lockedAt;
  }, true);
  // `touch dir` is a write to the directory, and needs the bit on the directory itself.
  setup('a directory without its write bit', () => {
    fs.mkdir('/t/nowrite');
    fs.chmod('/t/nowrite', 0o555);
  });
  refuses('touch on a directory without the write bit', 'EACCES', () => fs.touch('/t/nowrite'));
  // Every route to a mounted inode, refused.
  //
  // One permission enforced in one function is enforced nowhere: `cp`, `chmod`, `touch` and
  // `ln` each reached the inode without passing through `readFile` or `writeFile`, and each
  // one got through. These assert the doors, not the lock.
  setup('a mount to write at', () => {
    fs.mountIndex('/mnt-probe', {
      version: 1,
      files: [['readme.txt', 12, 1762000000, 1]],
    }, (relative) => `/usr/src/webxp/${relative}`);
  });
  refuses('a write to a mounted file', 'EACCES', () => fs.writeFile('/mnt-probe/readme.txt', 'x'));
  refuses('chmod on a mounted file', 'EPERM', () => fs.chmod('/mnt-probe/readme.txt', 0o644));
  refuses('touch on a mounted file', 'EACCES', () => fs.touch('/mnt-probe/readme.txt'));
  refuses('a hard link into the mount', 'EPERM', () => fs.link('/mnt-probe/readme.txt', '/t/ln'));
  refuses('unlinking a mounted file', 'EACCES', () => fs.unlink('/mnt-probe/readme.txt'));
  refuses('a write inside a mounted directory', 'EACCES', () => fs.writeFile('/mnt-probe/new', 'x'));

  // And a refused write must not announce one. A guard that refuses the change but still
  // fires the event has told the persistence layer that a read-only file changed, which is a
  // save of a tree nobody edited.
  const announced = [];
  setup('watching a refused write', () => {
    const unwatch = fs.watch((event) => announced.push(event.op));
    for (const attempt of [
      () => fs.touch('/mnt-probe/readme.txt'),
      () => fs.writeFile('/mnt-probe/readme.txt', 'x'),
      () => fs.chmod('/mnt-probe/readme.txt', 0o644),
    ]) {
      try {
        attempt();
      } catch {
        // The refusal is asserted above. What is asserted here is the silence after it.
      }
    }
    unwatch();
  });
  want('a refused write notifies nobody', () => announced.join(), '');

  // A batch notifies once, at the end, with the tree whole. Un-batched, the first mutation
  // of a reseed told every watcher the filesystem was empty.
  //
  // This block MUST stay last: it calls `fs.reset()`, so anything below it runs against a
  // bare root. Appended after it, the two blocks above reported `ENOENT /t/untouchable` ten
  // times instead of passing.
  const during = [];
  setup('a batched rebuild', () => {
    const unwatch = fs.watch(() => during.push(fs.exists(HOME)));
    fs.batch('reset', () => {
      fs.reset();
      buildSeed(fs);
    });
    unwatch();
  });
  want('one event for a batch', () => during.length, 1);
  want('and the tree was whole when it arrived', () => during[0], true);

  return bad;
})()"""


#: The mount, asked of the filesystem the apps are actually holding.
#:
#: Reached through the terminal's own session rather than through a fresh `VFS`, because the
#: claim is about the mount that `fs.mjs` grafted at boot — a table that built its own tree
#: would be asserting about a mount it had just made itself.
MOUNT_ASSERTIONS = """(async () => {
  const bad = [];
  const term = window.webxp.windows.find((w) => w.programId === 'terminal');
  if (!term || !term.app || !term.app.shell) {
    // A missing precondition is a failure, not a skip: `docs/review.md` rule 1.
    return ['no terminal session to ask — nothing was tested'];
  }
  const fs = term.app.shell.fs;
  const { mounted, persistence } = await import('/shared/fs/fs.mjs');
  const index = await (await fetch('/shared/fs/mount.json')).json();

  const want = (label, got, expected) => {
    if (got !== expected) {
      bad.push(label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(expected));
    }
  };

  if (mounted.error) bad.push('the mount reported: ' + mounted.error);
  want('every indexed file was grafted', mounted.files, index.files.length);
  want('the mount root is where the index says', mounted.root, index.root);

  // Sizes come from the index, so `ls -l` and `du` are answered without a fetch. Checked
  // against the index's own row rather than a number written here, so the check cannot go
  // stale when a file changes.
  const row = index.files.find((each) => each[0] === 'src/window.mjs');
  if (!row) bad.push('the index does not list src/window.mjs, which this check is about');
  else {
    const path = index.root + '/src/window.mjs';
    want('size without fetching', fs.lstat(path).size, row[1]);
    want('read-only', fs.lstat(path).modeString, '-r--r--r--');
    want('marked as mounted', fs.lstat(path).mounted, true);
    // The proof that metadata costs nothing: after reading the size, the body is still
    // outstanding. If `lstat` had fetched it, there would be nothing pending.
    want('metadata did not hydrate it', fs.pending([path]).length, 1);
    try {
      fs.readFile(path);
      bad.push('an unfetched mounted file read as if it had contents');
    } catch (error) {
      want('and reading it says why', error.code, 'EAGAIN');
    }
  }

  // The repository must not be saved into localStorage: it would go stale against the disk
  // and win on the next reload.
  //
  // The first version of this asserted that the image does not contain the string
  // '/usr/src'. It never does, on any tree: directory rows carry `[name, ino]` pairs and no
  // absolute path appears anywhere in the format. It was false by construction — the named
  // pattern in `docs/review.md` — and the sibling assertion below carried the whole claim.
  const image = fs.serialize();
  const saved = JSON.stringify(image);
  want('no mounted file is in what is saved', saved.includes('window.mjs'), false);
  // Counted, so dropping the filter would fail rather than being invisible: every inode
  // written has to be one the mount did not graft.
  const expected = [...fs.inodes.values()].filter((each) => !each.source && !each.mounted).length;
  want('only unmounted inodes are written', image.inodes.length, expected);
  // The live key, not a copy of it. `fs.mjs` derives the key from the format version
  // precisely so there is one, and a hand-written 'webxp.fs.v1' here would read null — and
  // assert nothing — the moment that version changed.
  const stored = window.localStorage.getItem(persistence.key);
  if (stored && stored.includes('maplibre')) {
    bad.push('the stored tree contains the repository');
  }

  // Metadata is free and contents are not — the design's headline claim, and until now it
  // rested on a script the suite never runs.
  //
  // Driven through `shell.run` rather than through keystrokes: the claim is about how many
  // requests a command makes, and `terminal` already covers the input plumbing.
  const original = window.fetch;
  let fetches = 0;
  window.fetch = (...args) => { fetches += 1; return original.apply(window, args); };
  const quiet = { write() {}, warn() {}, control() {} };
  const cost = async (line) => {
    const before = fetches;
    await term.app.shell.run(line, quiet);
    return fetches - before;
  };
  try {
    const src = index.root + '/src';
    want('ls -l fetches nothing', await cost(`ls -l ${src}`), 0);
    want('du fetches nothing', await cost(`du -s ${src}`), 0);
    want('find fetches nothing', await cost(`find ${src} -name '*.mjs'`), 0);
    want('stat fetches nothing', await cost(`stat ${src}/webxp.mjs`), 0);
    want('df fetches nothing', await cost('df'), 0);
    want('reading one file fetches once', await cost(`head -n 1 ${src}/webxp.mjs`), 1);
    want('reading it again fetches nothing', await cost(`head -n 1 ${src}/webxp.mjs`), 0);
    want('two names for one file fetch once',
         await cost(`wc -l ${src}/widgets.mjs ${src}/widgets.mjs`), 1);
    // Refused before the download, not after: the whole point of carrying sizes.
    want('an over-ceiling file fetches nothing',
         await cost(`cat ${index.root}/shared/map/vendor/maplibre-gl.mjs`), 0);
  } finally {
    window.fetch = original;
  }

  // Nothing ignored is reachable. The same claim the index check makes on disk, made again
  // from inside the filesystem, because this is where it would do harm.
  for (const entry of fs.walk(index.root)) {
    if (entry.path.includes('/private')) bad.push('reachable in the mount: ' + entry.path);
  }
  return bad;
})()"""


def check_mount(page) -> list[str]:
    """The grafted repository, asked of the filesystem the apps hold."""
    wait_for_apps(page)
    terminal_session(page)
    try:
        found = page.evaluate(MOUNT_ASSERTIONS, await_promise=True)
    except CDPError as error:
        return [f"mount: the assertion table could not run — {error}"]
    if not isinstance(found, list):
        return [f"mount: the assertion table did not run — {found!r}"]
    return [f"mount: {message}" for message in found]


def fs_reset(page) -> None:
    """Clear the stored filesystem and reload, so the tree is the seed and not last run's.

    A check whose fixture is the previous run's output is not a check: `localStorage` on
    127.0.0.1 outlives the run, and the first terminal check asserts the contents of files
    an earlier run could have edited.
    """
    page.evaluate("window.localStorage.removeItem(%s)" % json.dumps(FS_KEY))
    page.navigate(BASE_URL + "/")
    time.sleep(1.2)
    # The navigation itself buffers events that would otherwise be attributed to whatever
    # check runs next. Kept rather than discarded: a drain for a reason must not also delete
    # a real error raised before it.
    drain(page)


def check_filesystem(page) -> list[str]:
    """The filesystem model: resolution, symlinks, links, modes, serialisation."""
    fs_reset(page)
    wait_for_apps(page)
    try:
        found = page.evaluate(FS_ASSERTIONS, await_promise=True)
    except CDPError as error:
        # Reported as a failure rather than raised, so an unreachable module does not take
        # the rest of the suite down with it — and so the message says which module.
        return [f"filesystem: the assertion table could not run — {error}"]
    if not isinstance(found, list):
        return [f"filesystem: the assertion table did not run — {found!r}"]
    return [f"filesystem: {message}" for message in found]


#: A terminal window's pane and prompt. Addressed by window id, never by position in the
#: list: windows open and close around these checks, and an index silently retargets.
TERM_PARTS = """(() => {
  const wins = window.webxp.windows.filter((w) => w.programId === 'terminal');
  const win = ID ? wins.find((w) => w.id === ID) : wins[wins.length - 1];
  if (!win) return null;
  const input = win.el.querySelector('.term-input');
  const log = win.el.querySelector('.term-log');
  if (!input || !log) return null;
  const r = input.getBoundingClientRect();
  return { id: win.id, text: log.textContent, value: input.value,
           prompt: win.el.querySelector('.term-prompt').textContent,
           x: r.left + r.width / 2, y: r.top + r.height / 2, count: wins.length };
})()"""


def wait_for_route(page, timeout: float = 8.0) -> None:
    """Block until `boot.mjs` has resolved the route, which may be `null`.

    `wait_for_apps` is not enough and the difference is subtle: `window.webxpApps` is
    published several awaits before `window.webxpRoute` — the router import, the `/pages`
    registration and `restore()` all come after it. A check that read `webxpRoute` on the
    strength of `webxpApps` got `undefined`, which `JSON.stringify` omits, so the value
    arrived as a missing key and surfaced as a `KeyError` three hundred lines from the cause.

    `in` rather than a truth test, because `null` is the right answer for a URL with no hash
    and has to be distinguishable from "not yet".
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate("'webxpRoute' in window"):
            return
        time.sleep(0.1)
    raise CDPError("demo/boot.mjs never published window.webxpRoute — it did not finish")


def wait_for_apps(page, timeout: float = 8.0) -> None:
    """Block until the desktop has finished loading its apps.

    `demo/boot.mjs` publishes `window.webxpApps` once `loadApps` resolves, which is the only
    signal that the forty-odd module fetches a boot makes are done.

    This exists because of a flake that cost two runs: `python3 -m http.server` is
    single-threaded, and a check's own `import()` arriving while those fetches are in flight
    comes back as `Failed to fetch dynamically imported module` — which Chrome then caches
    against that specifier, so the check fails for the rest of the run with a message that
    reads like a missing file. Waiting is not a workaround for a product defect; the server
    is the constraint.

    A timeout is a failure, not a skip: a desktop that never finishes loading is the thing
    worth reporting.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if page.evaluate("Boolean(window.webxpApps && window.webxpApps.loaded)"):
            return
        time.sleep(0.15)
    raise CDPError(
        "the desktop never published window.webxpApps — its apps did not finish loading"
    )


def term_parts(page, win_id: str | None = None) -> dict:
    parts = page.evaluate(TERM_PARTS.replace("ID", json.dumps(win_id) if win_id else "null"))
    if parts is None:
        raise CDPError(f"no terminal window {win_id or '(last)'}")
    return parts


def raise_window(page, win_id: str) -> None:
    """Bring a window to the front before anything is clicked inside it.

    Through the desktop's own `focus`, not by clicking the title bar, because a click is
    exactly what cannot be aimed yet: the window may be underneath another one at that
    coordinate. Omitting this was a real harness fault — `term_run` clicked where the input
    had been measured, a later check opened a window over it, and the keystrokes went to
    whichever window was on top while the assertions read the one underneath.
    """
    found = page.evaluate(
        "(() => { const d = window.webxp;"
        " const win = d.windows.find((w) => w.id === %s);"
        " if (!win) return false; d.focus(win, { moveFocus: true }); return true; })()"
        % json.dumps(win_id)
    )
    if not found:
        raise CDPError(f"no window {win_id}")
    time.sleep(0.12)


def terminal_session(page) -> str:
    """The id of a terminal to drive, opening one if no check has left any.

    Without this, `--only explorer` died in `term_parts` on a desktop with no terminal — the
    documented way to exercise one check did not work for three of them.
    """
    open_now = page.evaluate(
        "window.webxp.windows.filter((w) => w.programId === 'terminal').length"
    )
    if not open_now:
        launch(page, "terminal")
    return term_parts(page)["id"]


def term_run(page, line: str, win_id: str | None = None, settle: float = 0.35) -> str:
    """Type a line into one terminal and return only what it printed.

    Real input: the window is raised, the input is clicked, `Input.insertText`, a real Enter.
    A `dispatchEvent` in page script would prove the listener runs and nothing about whether
    it is reachable.
    """
    target = win_id or term_parts(page)["id"]
    raise_window(page, target)
    before = term_parts(page, target)
    mouse(page, "mousePressed", before["x"], before["y"], buttons=1)
    mouse(page, "mouseReleased", before["x"], before["y"], buttons=0)
    page.type_text(line)
    page.press("Enter")
    time.sleep(settle)
    after = term_parts(page, target)
    if not after["text"].startswith(before["text"]):
        # `clear` empties the pane, so there is no prefix to strip.
        return after["text"]
    return after["text"][len(before["text"]):]


def notepad_parts(page, win_id: str | None = None) -> dict | None:
    """One Notepad window's title, buffer, status and where its text area is."""
    found = _notepad_parts(page, win_id)
    if found and found.get("error"):
        raise CDPError(found["error"])
    return found


def _notepad_parts(page, win_id: str | None = None) -> dict | None:
    return page.evaluate(
        "(() => { const pads = window.webxp.windows.filter((w) => w.programId === 'notepad');"
        " const w = %s ? pads.find((p) => p.id === %s) : pads[pads.length - 1];"
        " if (!w) return null;"
        " const area = w.el.querySelector('.notepad-area');"
        # A missing text area is reported as itself. Returning null for `value` and 0 for
        # the coordinates made a renamed class fail as 'Notepad overwrote an external change
        # silently', which sends the reader after the wrong defect.
        " if (!area) return { id: w.id, error: 'no .notepad-area in the Notepad window' };"
        " const r = area.getBoundingClientRect();"
        " return { id: w.id, title: w.titleText, value: area.value,"
        "          status: w.statusEl ? w.statusEl.textContent : '',"
        "          x: r.left + 20, y: r.top + 10 }; })()"
        % (json.dumps(win_id) if win_id else "null", json.dumps(win_id) if win_id else "null")
    )


def notepad_caret(page, win_id: str) -> None:
    """Raise a Notepad window, click into its text, and put the caret at the end.

    The click is real input and is what focuses the field. The caret is then moved by script,
    because `Input.insertText` lands wherever the caret is and a click lands it wherever the
    pointer was — which is a position, not a value, so nothing about the edit is synthesised.
    """
    raise_window(page, win_id)
    pad = notepad_parts(page, win_id)
    mouse(page, "mousePressed", pad["x"], pad["y"], buttons=1)
    mouse(page, "mouseReleased", pad["x"], pad["y"], buttons=0)
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " const area = w.el.querySelector('.notepad-area');"
        " area.setSelectionRange(area.value.length, area.value.length); })()"
        % json.dumps(win_id)
    )


#: Stands in for "this row asserts nothing" — a step that builds the state a later row
#: checks. Named rather than left as a bare `[]`, because eight of these rows are setup and a
#: reader cannot otherwise tell them from an assertion that happens to be weak. Every one of
#: them is followed by a row asserting its effect.
SETUP: list[str] = []

#: One row is one line typed, the strings its output must contain, and the strings it must
#: not. `must_not` is what catches a check that passes because everything contains nothing.
#: Both are tested against the output only, never against the echoed command line.
TERM_LINES = (
    ("pwd", ["/home/user"], []),
    ("ls", ["readme.txt", "notes", "links"], []),
    ("cat notes/todo.txt", ["read the window model"], []),
    ("ls -l links", ["lrwxrwxrwx", "relative -> ../readme.txt", "ideas-too"], []),
    # A symlink named directly stays the symlink. Following it printed a plain file, in the
    # one directory that exists to show that a symlink is not one.
    ("ls -l links/absolute", ["lrwxrwxrwx", "-> /etc/motd"], ["-rw-r--r--"]),
    ("ls -F links/absolute", ["links/absolute@"], []),
    # A symlink to a directory is still followed, because that is what makes `ls` on one
    # list its contents.
    ("ls links/relative-dir", ["todo.txt"], ["relative-dir"]),
    ("cat links/relative", ["webxp filesystem"], []),
    ("cd links/relative-dir", [], ["No such file"]),
    ("pwd", ["/home/user/links/relative-dir"], []),
    ("pwd -P", ["/home/user/notes"], ["links"]),
    # `.` and `..` name the directory being listed and its real parent. Synthesised
    # lexically, `.` inside a symlinked directory reported the symlink instead — a listing
    # in which the directory appeared as a link to somewhere else.
    ("ls -la", ["drwxr-xr-x"], [". -> ", ".. -> "]),
    ("cd ..", SETUP, []),
    ("pwd", ["/home/user/links"], ["relative-dir"]),
    ("cd ~", SETUP, []),
    # The failures. A shell that cannot report is worse than one that cannot run.
    ("cat /nope", ["No such file or directory"], []),
    ("cat /etc/secret.conf", ["Permission denied"], ["permission bits are not being checked"]),
    ("cd readme.txt", ["Not a directory"], []),
    ("ls -Z", ["invalid option"], []),
    ("ls --colour", ["unrecognised option"], []),
    ("nosuchcommand", ["command not found"], []),
    ("cat 'unterminated", ["unterminated single quote"], []),
    # An operator this shell does not have is an error, not a filename. Read as a word,
    # `&&` made a directory called `&&`.
    ("mkdir /tmp/d && echo ran-anyway", ["&& is not supported"], ["ran-anyway"]),
    ("ls /tmp", [], ["&&", "/tmp/d"]),
    ("echo 'a;b'", ["a;b"], ["not supported"]),
    # A bracket body that is not a usable character class degrades to a literal. It reached
    # `new RegExp` from `find -name` outside any try, as a stack trace and status 70.
    (r"find . -name '[\]'", [], ["internal error", "Unterminated"]),
    # Globs, pipes, redirection.
    ("find / -name '*.log'", ["/var/log/boot.log"], []),
    ("grep -n error /var/log/boot.log", ["10:", "no sound card"], ["kernel booting"]),
    ("ls notes/*.txt", ["notes/ideas.txt", "notes/todo.txt"], []),
    ("ls nope*", ["No such file or directory"], []),
    ("echo written-by-the-shell > /tmp/probe.txt", [], ["No such file"]),
    ("cat /tmp/probe.txt", ["written-by-the-shell"], []),
    ("echo appended >> /tmp/probe.txt", SETUP, []),
    ("wc -l < /tmp/probe.txt", ["2"], []),
    ("ls /etc | wc -l", ["4"], []),
    ("mkdir -p /tmp/a/b/c", SETUP, []),
    ("find /tmp -type d", ["/tmp/a/b/c"], []),
    ("rm -r /tmp/a", SETUP, []),
    ("find /tmp -type d", [], ["/tmp/a"]),
    # A directory copied into its own subtree never terminated: the copy created a child,
    # the walk reached it, and it copied that too. Both edges, because a guard that refuses
    # everything is also a guard.
    ("mkdir -p /tmp/src/inner", SETUP, []),
    ("cp -r /tmp/src /tmp/src/copy", ["into itself"], []),
    ("cp -r /tmp/src /tmp/elsewhere", [], ["into itself"]),
    ("ls /tmp/elsewhere", ["inner"], []),
    ("ln -s /etc/hosts /tmp/h", SETUP, []),
    ("readlink /tmp/h", ["/etc/hosts"], []),
    ("realpath /tmp/h", ["/etc/hosts"], []),
    ("rm /tmp/h", SETUP, []),
    ("cat /etc/hosts", ["localhost"], []),
    # `basename('.')` answered '/', and `joinPath(dest, '/')` is the root: `cp -r . dest`
    # copied into `/` and exited 0. The basename of the *resolved* source is the fix.
    ('mkdir -p /tmp/dot/dst /tmp/dot/moved', [], []),
    ('cd notes', [], []),
    ('cp -r . /tmp/dot/dst', [], ['Permission denied', 'No such file']),
    ('cd ~', [], []),
    ('ls /tmp/dot/dst', ['notes'], []),
    ('ls /tmp/dot/dst/notes', ['todo.txt', 'ideas.txt'], []),
    ('ls /', ['etc', 'home'], ['todo.txt', 'ideas.txt']),
    ('cd /tmp/dot/dst/notes', [], []),
    # The pane also reports this session's directory being moved out from under it, which is
    # the watcher doing its job; the assertion is that `mv .` aimed at the resolved name.
    ('mv . /tmp/dot/moved', [], ['File exists', 'Directory not empty']),
    ('cd ~', [], []),
    ('ls /tmp/dot/moved', ['notes'], []),
    ('ls /', ['etc', 'home'], ['todo.txt']),
    # `rm -rf /` emptied the filesystem and then reported a child it happened to fail on
    # last — a path the reader never typed.
    ('rm -rf /', ['dangerous', "'/'"], ['No such file', 'Permission denied']),
    ('ls /', ['etc', 'home', 'tmp'], []),
    ('cd /', [], []),
    ('rm -rf .', ['dangerous', "which is '/'"], []),
    ('cd ~', [], []),
    ('cat /etc/motd', ['POSIX shell'], []),
    # `..` is resolved, not cancelled lexically before anything is looked at. `cd` and `ls`
    # disagreed about the same string.
    ('cd /nope/..', ['No such file or directory'], []),
    ('pwd', ['/home/user'], ['/nope']),
    ('cd /etc/motd/..', ['Not a directory'], []),
    ('pwd', ['/home/user'], ['/etc']),
    # A symlink child is recreated, never walked through. Two links pointing through each
    # other re-entered the same directories once per hop: 642 stray directories from one cp.
    ('mkdir -p /tmp/loopy/a /tmp/loopy/b', [], []),
    ('ln -s /tmp/loopy/b /tmp/loopy/a/tob', [], []),
    ('ln -s /tmp/loopy/a /tmp/loopy/b/toa', [], []),
    ('cp -r /tmp/loopy/a /tmp/loopy/copy', [], ['Too many levels']),
    ('ls -l /tmp/loopy/copy', ['tob -> /tmp/loopy/b'], []),
    ('find /tmp/loopy/copy -type d', ['/tmp/loopy/copy'], ['/tmp/loopy/copy/tob']),
    # One walk's bound, not the command's: two operands that resolve to one directory are
    # two copies, which is what a shell does. The guard that keeps the recursion finite is
    # scoped to a single operand's walk, so it cannot refuse this.
    ('mkdir /tmp/sametree', [], []),
    ('cp -r notes links/relative-dir /tmp/sametree', [], ['already', 'Not a directory', 'No such file']),
    ('ls /tmp/sametree', ['notes', 'relative-dir'], []),
    ('ls /tmp/sametree/notes', ['todo.txt', 'ideas.txt'], []),
    ('ls /tmp/sametree/relative-dir', ['todo.txt', 'ideas.txt'], []),
    ('mkdir /tmp/twice', [], []),
    ('cp -r notes notes /tmp/twice', [], ['already']),
    ('ls /tmp/twice/notes', ['todo.txt', 'ideas.txt'], []),
    # One child that cannot be read is reported and skipped. It used to abandon every
    # sibling after it, leaving a copy silently missing two of six names.
    ('cp -r links /tmp/lcopy', [], ['No such file or directory']),
    ('ls -a /tmp/lcopy', ['absolute', 'broken', 'ideas-too', 'loop', 'relative', 'relative-dir'], []),
    ('mkdir /tmp/perm', [], []),
    ('touch /tmp/perm/a /tmp/perm/zlast', [], []),
    ('chmod 000 /tmp/perm/a', [], []),
    ('cp -r /tmp/perm /tmp/permcopy', ['Permission denied'], []),
    ('ls /tmp/permcopy', ['zlast'], []),
    # `tree` reports an unreadable directory and carries on. Thrown, it cost every later
    # sibling and the summary line, so the output described a smaller tree than the one there.
    ('mkdir -p /tmp/tr/sub', [], []),
    ('touch /tmp/tr/zlast', [], []),
    ('chmod 000 /tmp/tr/sub', [], []),
    ('tree /tmp/tr', ['Permission denied', 'zlast', '1 directory, 1 file'], []),
    # Quoting is per character span. A word-wide flag made one quoted character suppress
    # globbing across the whole word.
    ('ls "no"*', ['ideas.txt', 'todo.txt'], ['No such file']),
    ("touch '/tmp/star*'", [], []),
    ("ls '/tmp/star*'", ['/tmp/star*'], ['No such file']),
    # `\` is honoured inside double quotes, which the header advertises. The closing-quote
    # scan stopped at an escaped one and reported a line with no unterminated quote in it.
    ('echo "a\\"b"', ['a"b'], ['unterminated']),
    # A trailing separator is the directories-only constraint. Split off as an empty
    # component, it was dropped and `echo */` answered with regular files.
    ('echo */', ['links/', 'notes/'], ['readme.txt']),
    # `echo` takes no options past a leading -n. Through the general flag parser, `echo -5`
    # answered `invalid option -- '5'` where a shell prints `-5`.
    ('echo -5', ['-5'], ['invalid option']),
    ('echo -n hi', ['hi'], []),
    # An operand a command cannot carry out is an error, not a word to drop: `uniq a b`
    # ignored `b` and `pwd junk` printed the directory and exited 0.
    ('echo one > /tmp/u1', [], []),
    ('echo two > /tmp/u2', [], []),
    ('uniq /tmp/u1 /tmp/u2', ["extra operand '/tmp/u2'"], ['one', 'two']),
    ('pwd /tmp', ['extra operand'], ['/home/user']),
    ('whoami root', ['extra operand'], ['user']),
    ('date now', ['extra operand'], []),
    ('df -h /', ['extra operand'], ['webxp-vfs']),
    ('tree /tmp /etc', ['extra operand'], ['directories']),
    ('history 5', ['extra operand'], []),
    # `[]]` is a class holding `]`. Unescaped, it compiled to the empty class `[]`, which
    # matched nothing and threw nothing, so the literal fallback never engaged.
    ("touch '/tmp/]'", [], []),
    ("find /tmp -name '[]]'", ['/tmp/]'], []),
    ('ls /tmp/[]]', ['/tmp/]'], ['No such file']),
    # `touch` is a write, and a write needs the bit. It used to move the mtime of a file the
    # reader cannot write and announce a change the saver then wrote out.
    ('echo open > /tmp/ro', [], []),
    ('chmod 444 /tmp/ro', [], []),
    ('touch /tmp/ro', ['Permission denied'], []),
    ('touch /tmp/ro /tmp/ro-sibling', ['Permission denied'], []),
    ('ls /tmp/ro-sibling', ['/tmp/ro-sibling'], ['No such file']),
    ('cat /tmp/ro', ['open'], []),
    ('chmod 644 /tmp/ro', [], []),
    ('touch /tmp/ro', [], ['Permission denied']),
    # Every redirection in a stage is opened; the last one gets the output.
    ('echo a > /tmp/r1 > /tmp/r2', [], []),
    ('cat /tmp/r1', [], ['No such file']),
    ('cat /tmp/r2', ['a'], ['No such file']),
    ('cd /tmp', [], []),
    ('cd -', ['/home/user'], ['OLDPWD not set']),
    ('pwd', ['/home/user'], ['/tmp']),
    # The mount, through the pane. `/usr/src/webxp` is the repository: these rows read the
    # project's own source out of the filesystem it ships in.
    ("ls /usr/src/webxp", ["src", "apps", "shared", "docs"], []),
    ("head -n 1 /usr/src/webxp/src/icons.mjs", ["The icon set, drawn rather than fetched"], []),
    ("grep -c . /usr/src/webxp/.gitignore", [], ["No such file"]),
    ("grep -rln MAX_FILE_SIZE /usr/src/webxp/shared", ["vfs.mjs"], []),
    ("find /usr/src/webxp/src -name '*.css'", ["theme.css", "window.css"], []),
    # Read-only, and refused by the mode bits rather than by a special case.
    ("echo x > /usr/src/webxp/src/window.mjs", ["Permission denied"], []),
    ("rm /usr/src/webxp/README.md", ["Permission denied"], []),
    # Two files it can describe and cannot read, each saying which of the two reasons applies.
    ("cat /usr/src/webxp/shared/map/vendor/maplibre-gl.mjs", ["File too large"], ["sourceMappingURL"]),
    ("cat /usr/src/webxp/apps/day-02-lines/fixture/activities/202610200000.gpx.gz",
     ["Not a text file"], []),
    # The privacy rule, from the prompt: ignored data is not in the filesystem at all.
    ("find /usr/src/webxp -name private", [], ["private"]),
    ("help", ["Where you are", "Reading", "Writing"], []),
    ("whoami", ["user"], []),
    ("df", ["webxp-vfs", "persistence:"], []),
)


def check_terminal(page, *, invert: bool = False) -> list[str]:
    """The shell, driven by real keystrokes: output, failures, pipes, globs, redirection."""
    bad = []
    launch(page, "terminal")
    # Named once and used throughout. Every later check opens more terminals, and a helper
    # that means "the last one" would quietly move this session out from under the rows.
    shell = term_parts(page)["id"]
    banner = term_parts(page, shell)
    if "Type 'help'" not in banner["text"]:
        bad.append(f"terminal: no banner in the pane — {banner['text'][:60]!r}")
    if "user@webxp:~$" not in banner["prompt"]:
        bad.append(f"terminal: prompt is {banner['prompt']!r}")

    # These run before the row table, not after it, and the order is load-bearing: the rows
    # leave `/tmp/perm/a` and `/tmp/tr/sub` at mode 000 on purpose, and `du -s /` below
    # cannot walk an unreadable directory — run afterwards it would compare a short total
    # against `df` and report a defect in `du` that is really the rows' fixture.

    # 1. An unreadable ancestor of the working directory must not freeze the status bar.
    # `fs.exists` rethrows EACCES and the watcher had no guard, so one `chmod 000` on an
    # ancestor stopped the bar and the prompt for the rest of the session, silently.
    def status_line() -> str:
        return page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
            " return w.statusEl ? w.statusEl.textContent : ''; })()" % json.dumps(shell)
        )

    term_run(page, "mkdir -p /tmp/sealed/deep", shell)
    term_run(page, "cd /tmp/sealed/deep", shell)
    term_run(page, "chmod 000 /tmp/sealed", shell)
    frozen = status_line()
    term_run(page, "touch /tmp/after-the-chmod", shell)
    if status_line() == frozen:
        bad.append(f"terminal: the status bar stopped at {frozen!r} when an ancestor of the "
                   "working directory became unreadable")
    term_run(page, "chmod 755 /tmp/sealed", shell)
    # And the probe still does what it is for: a removed directory moves the session.
    moved = term_run(page, "rm -rf /tmp/sealed/deep", shell)
    if "is gone; moved to" not in moved:
        bad.append(f"terminal: a removed working directory was not reported — {moved!r}")
    term_run(page, "cd ~", shell)

    # 2. `help` is generated from the table, groups included. A written-out list of group
    # titles dropped any command whose group was not in it — from the one command that is
    # the reference, silently.
    #
    # An `import()`, because `COMMANDS` is a module-level table and no window publishes it.
    # One URL in one realm is one instance, so this is the table `win.app.shell` dispatches
    # against and not a copy — asserted rather than assumed on the next line, because that
    # identity is the whole reason the injection reaches `help`. Measured: injecting here
    # makes the live shell's own completion offer `zzprobe`, and deleting it takes it away.
    probed = page.evaluate(
        "(async () => { const { COMMANDS } = await import('/apps/terminal/commands.mjs');"
        " COMMANDS.zzprobe = { group: 'experimental', usage: 'zzprobe',"
        "   summary: 'a probe', run: () => 0 };"
        " const w = window.webxp.windows.find((w) => w.id === %s);"
        " return w.app.shell.complete('zzprob').matches; })()"
        % json.dumps(shell),
        await_promise=True,
    )
    listing = term_run(page, "help", shell)
    restored = page.evaluate(
        "(async () => { const { COMMANDS } = await import('/apps/terminal/commands.mjs');"
        " delete COMMANDS.zzprobe;"
        " const w = window.webxp.windows.find((w) => w.id === %s);"
        " return { live: w.app.shell.complete('zzprob').matches,"
        "          inTable: 'zzprobe' in COMMANDS }; })()"
        % json.dumps(shell),
        await_promise=True,
    )
    if probed != ["zzprobe"]:
        bad.append(
            f"terminal: the probe command was injected and the live shell offers {probed!r} "
            "— the table this reached is not the one the window dispatches against, so the "
            "listing below says nothing about a missing group title"
        )
    elif "zzprobe" not in listing:
        bad.append("terminal: help dropped a command whose group has no title")
    # The injection is a mutation of the one live table, so the removal is asserted and not
    # merely attempted: a probe command left behind outlives this check and every one after.
    if restored["inTable"] or restored["live"]:
        bad.append(
            f"terminal: the probe command survived its removal ({restored!r}) — the command "
            "table is left mutated for the rest of the run"
        )

    # 3. `uname -a` reports the filesystem's own format version. Written out as `vfs-1`, it
    # would keep saying 1 after FORMAT_VERSION was bumped.
    #
    # An `import()` again, and safe for the same reason: a module-level constant, shared, and
    # read rather than written.
    version = page.evaluate(
        "import('/shared/fs/vfs.mjs').then((m) => m.FORMAT_VERSION)", await_promise=True)
    named = term_run(page, "uname -a", shell)
    if f"vfs-{version}" not in named:
        bad.append(f"terminal: uname -a printed {named.strip()[:80]!r}, want vfs-{version}")

    # 4. readlink and realpath print a symlink's target; grouped as writes, `help` told the
    # reader that printing it writes to the filesystem.
    groups = page.evaluate(
        "(async () => { const { COMMANDS } = await import('/apps/terminal/commands.mjs');"
        " return [COMMANDS.readlink.group, COMMANDS.realpath.group]; })()", await_promise=True)
    if groups != ["read", "read"]:
        bad.append(f"terminal: readlink/realpath are grouped {groups}, want both 'read'")

    # 5. One inode with two names is one file's worth of space. `du` charged it per name, so
    # it reported space the filesystem does not use.
    #
    # Through the window rather than through `openFileSystem()`: `win.app.shell.fs` is the
    # mount this terminal's `du` and `df` both run against, so there is no module specifier
    # here to mis-spell into a second instance.
    used = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " return w.app.shell.fs.stats().used; })()" % json.dumps(shell)
    )
    counted = term_run(page, "du -s /", shell)
    if f"{used}\t" not in counted:
        bad.append(f"terminal: du -s / counted {counted.strip()[:40]!r}, and df says {used}")

    # 6. Tab offers command names in every command position, not only the first word.
    offered = page.evaluate(
        "window.webxp.windows.find((w) => w.id === %s).app.shell.complete('ls | ca').matches"
        % json.dumps(shell))
    if offered != ["cat"]:
        bad.append(f"terminal: Tab after a pipe offered {offered!r}, want ['cat']")

    # 7. The prompt's identity and `whoami` are one fact, and the hostname is the one in
    # /etc/hosts. Built with {uid: 0}, `whoami` said root while the prompt said user@webxp.
    #
    # The one block here that deliberately does *not* use the live instance: it constructs a
    # root-owned `VFS` and a `Shell` on it, because the defect is in how the identity is
    # derived at construction and the desktop only ever builds one uid. A second instance is
    # the subject, not an accident.
    IDENTITY = """(async () => {
      const { VFS } = await import('/shared/fs/vfs.mjs');
      const { buildSeed } = await import('/shared/fs/seed.mjs');
      const { Shell } = await import('/apps/terminal/shell.mjs');
      const fs = new VFS({ uid: 0, gid: 0 });
      fs.batch('seed', () => buildSeed(fs));
      const shell = new Shell({ fs });
      let printed = '';
      await shell.run('whoami', { write: (t) => { printed += t; },
                                  warn: (t) => { printed += t; } });
      const host = shell.prompt().split('@')[1].split(':')[0];
      return { prompt: shell.prompt(), printed: printed.trim(),
               hostInHosts: fs.readFile('/etc/hosts').includes('127.0.0.1   ' + host),
               rootInPasswd: fs.readFile('/etc/passwd').includes('root:x:0:0:'),
               oldcwdDeclared: Object.prototype.hasOwnProperty.call(shell, 'oldcwd') };
    })()"""
    who = page.evaluate(IDENTITY, await_promise=True)
    if who["printed"] != "root" or not who["prompt"].startswith("root@"):
        bad.append(f"terminal: whoami said {who['printed']!r} and the prompt is "
                   f"{who['prompt']!r} — the identity is not one fact")
    if not who["hostInHosts"] or not who["rootInPasswd"]:
        bad.append(f"terminal: the prompt's host is not the one in /etc/hosts — {who!r}")
    if not who["oldcwdDeclared"]:
        bad.append("terminal: `oldcwd` is session state a command invents")

    # 8. A defect inside a command must not print in the register of a filesystem refusal.
    # `cat: Cannot read properties of undefined` and `cat: Permission denied` are the same
    # line to a reader, and only one of them is a fact about their files. The fault is
    # injected into the mount this window's shell holds, so a real command's real catch site
    # is what is measured — and `delete` puts the prototype method back rather than leaving
    # an own property that merely looks like it.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " w.app.shell.fs.readFile = () => { const nothing = undefined;"
        "   return nothing.length; };"
        " return true; })()" % json.dumps(shell)
    )
    swallowed = term_run(page, "cat notes/todo.txt", shell)
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " delete w.app.shell.fs.readFile; return true; })()" % json.dumps(shell)
    )
    if "internal error" not in swallowed:
        bad.append(f"terminal: a defect inside cat printed {swallowed.strip()[:90]!r}, which "
                   "reads as a refusal from the filesystem")
    # Drained here on purpose: this one console error is the assertion, and left in the
    # buffer the end-of-run sweep would attribute it to whatever check runs next.
    if not any("cat threw" in line for line in console_errors(page)):
        bad.append("terminal: a defect inside cat did not reach the console under its name")
    # And the mount is unpatched again, or every check after this one is measuring a stub.
    if "read the window model" not in term_run(page, "cat notes/todo.txt", shell):
        bad.append("terminal: the defect control left the filesystem patched")

    lines = TERM_LINES
    if invert:
        # `--prove-failure` inverts one expectation here as well as the drag's, so a clean
        # run is reporting something about the shell too.
        lines = (("cat /etc/motd", ["No such file or directory"], []),) + lines

    for line, musts, must_nots in lines:
        output = term_run(page, line, shell)
        # The echoed command line is dropped before anything is asserted. It contains the
        # prompt, which contains the working directory, which contains most of the strings
        # these rows are about — `pwd -P` must not print `links`, and the prompt it was typed
        # at does. Asserting against the echo is how a check passes on its own input.
        body = output.split("\n", 1)[1] if "\n" in output else ""
        for must in musts:
            if must not in body:
                bad.append(f"terminal: {line!r} printed {body.strip()[:120]!r}, want {must!r}")
        for must_not in must_nots:
            if must_not in body:
                bad.append(f"terminal: {line!r} printed {must_not!r}, which it must not")

    # A command that prints nothing has to print nothing. Without this, every assertion
    # above could be satisfied by a pane that accumulates text from somewhere else.
    quiet = term_run(page, "touch /tmp/quiet", shell)
    if quiet.strip() != "user@webxp:~$ touch /tmp/quiet":
        bad.append(f"terminal: touch printed {quiet.strip()!r}, want only the echoed line")

    # Tab completion, and history on the arrow keys.
    raise_window(page, shell)
    parts = term_parts(page, shell)
    mouse(page, "mousePressed", parts["x"], parts["y"], buttons=1)
    mouse(page, "mouseReleased", parts["x"], parts["y"], buttons=0)
    page.type_text("cat note")
    page.press("Tab")
    time.sleep(0.15)
    completed = term_parts(page, shell)["value"]
    if completed != "cat notes/":
        bad.append(f"terminal: Tab completed to {completed!r}, want 'cat notes/'")
    page.type_text("todo.txt")
    page.press("Enter")
    time.sleep(0.3)
    page.press("ArrowUp")
    time.sleep(0.1)
    recalled = term_parts(page, shell)["value"]
    if recalled != "cat notes/todo.txt":
        bad.append(f"terminal: ArrowUp recalled {recalled!r}, want the last line")
    page.press("ArrowDown")
    time.sleep(0.1)
    if term_parts(page, shell)["value"] != "":
        bad.append("terminal: ArrowDown past the end of the history did not clear the line")

    # `ls` lays out columns for the pane it is in, so the same listing has to reflow. Both
    # extremes, because a layout checked at one width is a layout checked nowhere.
    def ls_rows(width: int) -> int:
        page.evaluate(
            "window.webxp.windows.find((w) => w.id === %s).sizeTo(%d, 420)"
            % (json.dumps(shell), width)
        )
        time.sleep(0.2)
        output = term_run(page, "ls /etc", shell)
        return len([row for row in output.strip().split("\n")[1:] if row.strip()])

    wide = ls_rows(640)
    narrow = ls_rows(260)
    if wide != 1:
        bad.append(f"terminal: ls in a 640px window used {wide} rows, want 1")
    if narrow <= wide:
        bad.append(
            f"terminal: ls used {narrow} rows at 260px and {wide} at 640px — the columns do "
            "not reflow with the window"
        )
    page.evaluate(
        "window.webxp.windows.find((w) => w.id === %s).sizeTo(620, 400)" % json.dumps(shell)
    )

    # `clear` empties the pane, which is the one command whose effect is not text.
    term_run(page, "clear", shell)
    if term_parts(page, shell)["text"].strip() != "":
        bad.append("terminal: clear left text in the pane")
    return bad


#: One Explorer window's state, by window id. Every lookup is required to find something:
#: a renamed class must fail the check rather than skip it — `docs/review.md` rule 1.
EXPLORER_PARTS = """(() => {
  const wins = window.webxp.windows.filter((w) => w.programId === 'explorer');
  const win = ID ? wins.find((w) => w.id === ID) : wins[wins.length - 1];
  if (!win) return { error: 'no Explorer window' };
  const list = win.el.querySelector('.exp-list');
  const address = win.el.querySelector('.exp-address');
  const up = win.el.querySelector('.exp-up');
  const head = win.el.querySelector('.exp-head');
  if (!list) return { error: 'no .exp-list in the Explorer window — nothing to list' };
  if (!address) return { error: 'no .exp-address — the address bar is gone' };
  if (!up) return { error: 'no .exp-up — the Up button is gone' };
  const rows = [...list.querySelectorAll('.exp-row')].map((row) => {
    const cell = (name) => {
      const found = row.querySelector('.exp-' + name);
      // A missing cell reads as the string 'MISSING', so a renamed column fails an
      // assertion instead of comparing undefined to undefined.
      return found ? found.textContent.trim() : 'MISSING';
    };
    const box = row.getBoundingClientRect();
    return {
      name: row.dataset.name,
      label: cell('name'),
      size: cell('size'),
      type: cell('type'),
      modified: cell('modified'),
      selected: row.getAttribute('aria-selected') === 'true',
      // The laid-out rect, not the window's own numbers: a row with no height is a row
      // nobody can click, and that is invisible to a textContent assertion.
      height: Math.round(box.height),
      x: box.left + 40,
      y: box.top + box.height / 2,
    };
  });
  return {
    id: win.id,
    title: win.titleText,
    status: win.statusEl ? win.statusEl.textContent : '',
    address: address.value,
    upDisabled: up.disabled,
    note: (list.querySelector('.exp-note') || {}).textContent || '',
    // Measured on a *row's* cell, not the header's. The header is a sibling of the thing
    // under test, so hiding only the heading passed while every row still rendered a fourth
    // cell into a three-column grid. 'absent' rather than null, so a renamed class is a
    // failed comparison instead of a falsy value that reads as a pass.
    modifiedShown: (() => {
      const cell = list.querySelector('.exp-row .exp-modified');
      if (!cell) return 'absent';
      return getComputedStyle(cell).display !== 'none';
    })(),
    headColumns: getComputedStyle(head).gridTemplateColumns.split(' ').length,
    rowColumns: (() => {
      const row = list.querySelector('.exp-row');
      return row ? getComputedStyle(row).gridTemplateColumns.split(' ').length : 0;
    })(),
    rows,
  };
})()"""


def explorer_parts(page, win_id: str | None = None) -> dict:
    parts = page.evaluate(EXPLORER_PARTS.replace("ID", json.dumps(win_id) if win_id else "null"))
    if parts.get("error"):
        raise CDPError(parts["error"])
    return parts


def explorer_row(parts: dict, name: str) -> dict | None:
    for row in parts["rows"]:
        if row["name"] == name:
            return row
    return None


def check_explorer(page) -> list[str]:
    """A folder window over the real filesystem: it lists it, navigates it, and follows it.

    Every condition this asserts is created here, from a terminal, rather than inherited from
    whatever the seed tree happens to contain — `docs/review.md` rule 4. The files it makes
    are named so a failure says which check left them behind.
    """
    bad = []
    shell = terminal_session(page)
    term_run(page, "rm -rf ~/exp-probe", shell)
    term_run(page, "mkdir -p ~/exp-probe/inner", shell)
    term_run(page, "echo twenty-characters-x > ~/exp-probe/note.txt", shell)
    term_run(page, "ln -s /etc/motd ~/exp-probe/shortcut", shell)

    page.evaluate("window.webxp.launch('explorer', { path: '/home/user/exp-probe' })")
    time.sleep(1.2)
    parts = explorer_parts(page)
    window_id = parts["id"]

    if "exp-probe" not in parts["title"]:
        bad.append(f"explorer: the window is titled {parts['title']!r}")
    if parts["address"] != "/home/user/exp-probe":
        bad.append(f"explorer: the address bar reads {parts['address']!r}")

    # Folders first, then by name. Asserted as the order of the rows rather than as "inner
    # is present", because the sort is the claim.
    order = [row["name"] for row in parts["rows"]]
    if order != ["inner", "note.txt", "shortcut"]:
        bad.append(f"explorer: rows are {order}, want folders first then by name")

    note = explorer_row(parts, "note.txt")
    if note is None:
        bad.append("explorer: the file this check created is not listed")
    else:
        # The Name cell, asserted. Rows are found by `data-name`, so a renamed `.exp-name`
        # left every label reading 'MISSING' with the suite green.
        if note["label"] != "note.txt":
            bad.append(f"explorer: note.txt's Name cell reads {note['label']!r}")
        if note["size"] != "20 chars":
            bad.append(f"explorer: note.txt's size reads {note['size']!r}, want '20 chars'")
        if note["type"] != "File":
            bad.append(f"explorer: note.txt's type reads {note['type']!r}")
        # The Modified cell had no assertion anywhere: `stamp()` could return
        # 'undefined NaN:NaN' and every check passed.
        if not re.match(r"^[A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}$", note["modified"]):
            bad.append(f"explorer: note.txt's Modified cell reads {note['modified']!r}")
        if note["height"] < 8:
            bad.append(f"explorer: a row laid out {note['height']}px high — nothing to click")

    # Selection, by real input, and the status bar that reports it. Both were collected and
    # neither was asserted, which is why a keyboard that could never select a row was
    # invisible to this check.
    target = explorer_row(parts, "note.txt")
    if target is not None:
        raise_window(page, window_id)
        mouse(page, "mousePressed", target["x"], target["y"], buttons=1)
        mouse(page, "mouseReleased", target["x"], target["y"], buttons=0)
        time.sleep(0.2)
        picked = explorer_parts(page, window_id)
        chosen = [row["name"] for row in picked["rows"] if row["selected"]]
        if chosen != ["note.txt"]:
            bad.append(f"explorer: clicking a row selected {chosen}")
        if "note.txt" not in picked["status"]:
            bad.append(f"explorer: the status bar after a click reads {picked['status']!r}")
        # Space is the select key in a listbox, and it must not navigate.
        page.press("Space")
        time.sleep(0.3)
        spaced = explorer_parts(page, window_id)
        if spaced["address"] != "/home/user/exp-probe":
            bad.append(f"explorer: Space navigated to {spaced['address']!r}")
        if not any(row["selected"] for row in spaced["rows"]):
            bad.append("explorer: Space left nothing selected — the keyboard cannot select")
        # One tab stop for the listbox, not one per file.
        stops = page.evaluate(
            "window.webxp.windows.find((w) => w.id === %s)"
            ".el.querySelectorAll('.exp-row[tabindex=\"0\"]').length" % json.dumps(window_id)
        )
        if stops != 1:
            bad.append(f"explorer: {stops} rows are tab stops, want 1")

    link = explorer_row(parts, "shortcut")
    if link is None:
        bad.append("explorer: the symlink is not listed")
    elif "Shortcut to /etc/motd" not in link["type"]:
        # lstat, not stat: a shortcut lists as a shortcut and says where it points.
        bad.append(f"explorer: the symlink's type reads {link['type']!r}")

    folder = explorer_row(parts, "inner")
    if folder is None:
        bad.append("explorer: the folder this check created is not listed")
    elif folder["size"] != "":
        bad.append(f"explorer: a folder's size column reads {folder['size']!r}, want empty")

    # The filesystem is shared, so a write from the terminal has to appear with no
    # interaction here at all. This is the whole reason the window watches it.
    term_run(page, "touch ~/exp-probe/appeared-by-itself.txt", shell)
    time.sleep(0.4)
    live = explorer_parts(page, window_id)
    if explorer_row(live, "appeared-by-itself.txt") is None:
        bad.append(
            "explorer: a file created in the terminal did not appear — the window is not "
            "watching the filesystem it is showing"
        )

    # Double-click a folder: it navigates, and Up comes back.
    target = explorer_row(explorer_parts(page, window_id), "inner")
    if target is None:
        bad.append("explorer: cannot test navigation, the folder is missing")
    else:
        raise_window(page, window_id)
        double_click(page, target["x"], target["y"])
        time.sleep(0.5)
        inner = explorer_parts(page, window_id)
        if inner["address"] != "/home/user/exp-probe/inner":
            bad.append(f"explorer: double-click left the address at {inner['address']!r}")
        if "This folder is empty" not in inner["note"]:
            bad.append(f"explorer: an empty folder says {inner['note']!r}")
        page.evaluate(
            "window.webxp.windows.find((w) => w.id === %s).el.querySelector('.exp-up').click()"
            % json.dumps(window_id)
        )
        time.sleep(0.4)
        back = explorer_parts(page, window_id)
        if back["address"] != "/home/user/exp-probe":
            bad.append(f"explorer: Up left the address at {back['address']!r}")

        # The same navigation from the keyboard. A single click focuses the row — it is a
        # button — and Enter opens it, so a reader who never touches the mouse gets there.
        again = explorer_row(explorer_parts(page, window_id), "inner")
        mouse(page, "mousePressed", again["x"], again["y"], buttons=1)
        mouse(page, "mouseReleased", again["x"], again["y"], buttons=0)
        page.press("Enter")
        time.sleep(0.5)
        typed = explorer_parts(page, window_id)
        if typed["address"] != "/home/user/exp-probe/inner":
            bad.append(f"explorer: Enter on a focused row left the address at {typed['address']!r}")
        page.evaluate(
            "window.webxp.windows.find((w) => w.id === %s).el.querySelector('.exp-up').click()"
            % json.dumps(window_id)
        )
        time.sleep(0.4)

    # The root has no parent, so Up must be unavailable rather than inert.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " w.app.show('/'); })()" % json.dumps(window_id)
    )
    time.sleep(0.3)
    at_root = explorer_parts(page, window_id)
    if not at_root["upDisabled"]:
        bad.append("explorer: Up is still enabled at /")
    if at_root["address"] != "/":
        bad.append(f"explorer: showing / reports {at_root['address']!r}")

    # A mounted file: read-only in the list, and opening it hands Notepad a path it has to
    # fetch before it can read. That is the EAGAIN path, end to end.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " w.app.show('/usr/src/webxp/src'); })()" % json.dumps(window_id)
    )
    time.sleep(0.4)
    mounted = explorer_parts(page, window_id)
    source = explorer_row(mounted, "icons.mjs")
    if source is None:
        bad.append("explorer: the mounted repository does not list src/icons.mjs")
    else:
        if source["type"] != "File, read-only":
            bad.append(f"explorer: a mounted file's type reads {source['type']!r}")
        raise_window(page, window_id)
        double_click(page, source["x"], source["y"])
        time.sleep(1.4)
        pad = notepad_parts(page)
        if pad is None:
            bad.append("explorer: double-clicking a file opened no Notepad window")
        else:
            if not (pad["value"] or "").startswith("/* The icon set"):
                bad.append(
                    "explorer: Notepad shows "
                    f"{(pad['value'] or '')[:40]!r} — a mounted file was not fetched"
                )
            if "read-only" not in pad["status"]:
                bad.append(f"explorer: Notepad's status for a mounted file is {pad['status']!r}")

    # An unreadable directory reports instead of looking finished and empty.
    term_run(page, "mkdir -p ~/exp-probe/sealed", shell)
    term_run(page, "chmod 000 ~/exp-probe/sealed", shell)
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " w.app.show('/home/user/exp-probe/sealed'); })()" % json.dumps(window_id)
    )
    time.sleep(0.4)
    sealed = explorer_parts(page, window_id)
    if "Permission denied" not in sealed["note"]:
        bad.append(f"explorer: an unreadable folder shows {sealed['note']!r}, want the refusal")
    if sealed["rows"]:
        bad.append("explorer: an unreadable folder listed rows")

    # The selection's colours, which `app.css` states as ratios and nothing asserted.
    # `active styling` does this for the title bar; the same two lines of arithmetic cover
    # the one other surface in this app that carries text on a filled background.
    #
    # It navigates somewhere with rows first rather than inheriting whatever the previous
    # block left on screen — which was the sealed folder, with none. `docs/review.md` rule 4.
    page.evaluate(
        "window.webxp.windows.find((w) => w.id === %s).app.show('/home/user/exp-probe')"
        % json.dumps(window_id)
    )
    time.sleep(0.3)
    colours = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " const row = w.el.querySelector('.exp-row[aria-selected=\"true\"]')"
        "   || w.el.querySelector('.exp-row');"
        " if (!row) return { error: 'no row to measure' };"
        " row.setAttribute('aria-selected', 'true');"
        " const seen = getComputedStyle(row);"
        " const type = row.querySelector('.exp-type');"
        " if (!type) return { error: 'no .exp-type cell to measure' };"
        " return { ink: seen.color, fill: seen.backgroundColor,"
        "          typeInk: getComputedStyle(type).color }; })()"
        % json.dumps(window_id)
    )
    if colours.get("error"):
        bad.append(f"explorer: {colours['error']}")
    else:
        fill = rgb_stops(colours["fill"])
        ink = rgb_stops(colours["ink"])
        type_ink = rgb_stops(colours["typeInk"])
        if not fill or not ink:
            bad.append(f"explorer: could not read the selection's colours — {colours}")
        else:
            ratio = contrast(ink[0], fill[0])
            if ratio < AA_NORMAL:
                bad.append(
                    f"explorer: selected text measures {ratio:.2f}:1 on the selection "
                    f"background, below AA's {AA_NORMAL}"
                )
            # The type and date columns are `--muted`, which is chosen for white and
            # measures 2.14:1 on the selection blue. They inherit the row's colour instead,
            # and this is the assertion that keeps them inheriting.
            if type_ink:
                secondary = contrast(type_ink[0], fill[0])
                if secondary < AA_NORMAL:
                    bad.append(
                        f"explorer: the Type column measures {secondary:.2f}:1 on a selected "
                        f"row, below AA's {AA_NORMAL} — it is not inheriting the row's colour"
                    )

    # The columns reflow. Both extremes, because a layout checked at one width is a layout
    # checked nowhere.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " w.app.show('/home/user/exp-probe'); w.sizeTo(660, 420); })()" % json.dumps(window_id)
    )
    time.sleep(0.3)
    wide = explorer_parts(page, window_id)
    page.evaluate(
        "window.webxp.windows.find((w) => w.id === %s).sizeTo(320, 420)" % json.dumps(window_id)
    )
    time.sleep(0.3)
    narrow = explorer_parts(page, window_id)
    if wide["modifiedShown"] is not True:
        bad.append(f"explorer: the Modified column reads {wide['modifiedShown']!r} in a 660px window")
    if narrow["modifiedShown"] is not False:
        bad.append(f"explorer: the Modified column reads {narrow['modifiedShown']!r} in a 320px window")
    # The header and the rows must agree about how many columns there are, at both widths —
    # the drift that hiding one and not the other produces.
    for label, parts_at in (("660px", wide), ("320px", narrow)):
        if parts_at["headColumns"] != parts_at["rowColumns"]:
            bad.append(
                f"explorer: at {label} the header has {parts_at['headColumns']} columns and "
                f"the rows have {parts_at['rowColumns']}"
            )

    term_run(page, "chmod 755 ~/exp-probe/sealed", shell)
    term_run(page, "rm -rf ~/exp-probe", shell)
    return bad

def check_open_across_apps(page) -> list[str]:
    """`open` hands another app a path, and both apps turn out to hold one filesystem.

    This is the claim the single mount in `shared/fs/fs.mjs` makes, and it cannot be checked
    inside either app: the terminal writes, Notepad reads, Notepad writes, the terminal
    reads. Anything less is two apps that each work.
    """
    bad = []
    shell = terminal_session(page)
    term_run(page, "echo bridge-one > /tmp/bridge.txt", shell)
    opened = term_run(page, "open /tmp/bridge.txt", shell, settle=1.2)
    if "opened /tmp/bridge.txt in Notepad" not in opened:
        bad.append(f"open: the terminal said {opened.strip()[:120]!r}")

    pad = notepad_parts(page)
    if pad is None:
        return bad + ["open: no Notepad window opened"]
    if pad["title"] != "bridge.txt - Notepad":
        bad.append(f"open: Notepad is titled {pad['title']!r}, want 'bridge.txt - Notepad'")
    if pad["value"] != "bridge-one\n":
        bad.append(f"open: Notepad holds {pad['value']!r}, want the file the shell wrote")

    # Typed into, then saved with Ctrl+S. The caret is moved by script and the text is
    # inserted as real input: `insertText` lands wherever the caret is, and a click lands it
    # wherever the pointer was.
    notepad_caret(page, pad["id"])
    page.type_text("edited-in-notepad\n")
    time.sleep(0.1)

    def ctrl_s() -> None:
        for event_type in ("rawKeyDown", "keyUp"):
            page.call(
                "Input.dispatchKeyEvent",
                type=event_type,
                key="s",
                code="KeyS",
                windowsVirtualKeyCode=83,
                nativeVirtualKeyCode=83,
                # 2 is Ctrl. Notepad's handler asks for `ctrlKey`, and a keypress without it
                # is the browser's own Save dialog rather than the editor's.
                modifiers=2,
            )
        time.sleep(0.3)

    ctrl_s()
    saved = notepad_parts(page, pad["id"])["status"]
    if "saved" not in saved:
        bad.append(f"open: Notepad's status after Ctrl+S is {saved!r}")

    # A write from the other window, behind this buffer's back. The first Ctrl+S after it has
    # to refuse: Notepad read the file once at mount, and saving over a change it never saw
    # would lose the shell's write while reporting success.
    term_run(page, "echo written-behind-notepad > /tmp/bridge.txt", shell)
    notepad_caret(page, pad["id"])
    page.type_text("one-more-edit\n")
    ctrl_s()
    conflict = notepad_parts(page, pad["id"])["status"]
    if "changed on disk" not in conflict:
        bad.append(f"open: Notepad overwrote an external change silently — status {conflict!r}")
    held = term_run(page, "cat /tmp/bridge.txt", shell)
    if "written-behind-notepad" not in held:
        bad.append(f"open: the refused save wrote anyway — the file reads {held.strip()[:120]!r}")
    # And the second one goes through, because replacing it may well be what was meant.
    raise_window(page, pad["id"])
    ctrl_s()
    forced = term_run(page, "cat /tmp/bridge.txt", shell)
    if "one-more-edit" not in forced:
        bad.append(f"open: a second Ctrl+S did not overwrite — the file reads {forced.strip()[:120]!r}")

    back = term_run(page, "cat /tmp/bridge.txt", shell)
    if "edited-in-notepad" not in back:
        bad.append(
            f"open: the terminal reads {back.strip()[:120]!r} — Notepad saved into a "
            "different filesystem from the one the shell is holding"
        )

    # A directory opens a second terminal, already in it. Two sessions, one filesystem.
    term_run(page, "open /tmp", shell, settle=1.2)
    second = term_parts(page)
    if second["count"] < 2:
        bad.append("open: a directory did not open a second terminal")
    elif second["id"] == shell:
        bad.append("open: a directory did not open a second terminal")
    elif "/tmp$" not in second["prompt"]:
        bad.append(f"open: the second terminal's prompt is {second['prompt']!r}, want /tmp")
    elif "bridge.txt" not in term_run(page, "ls", second["id"]):
        bad.append("open: the second terminal cannot see the file the first one wrote")
    return bad


def check_launch_arguments(page) -> list[str]:
    """`launch(id, args)` reaches `mount`, and a singleton says it cannot take them."""
    bad = []
    page.evaluate(
        "(() => { window.__warned = [];"
        " const original = console.warn;"
        " console.warn = (...parts) => { window.__warned.push(parts.map(String).join(' '));"
        "   original.apply(console, parts); }; })()"
    )
    page.evaluate("window.webxp.launch('terminal', { path: '/etc' })")
    time.sleep(1.0)
    started = term_parts(page)
    if "/etc$" not in started["prompt"]:
        bad.append(
            f"launch arguments: the new terminal's prompt is {started['prompt']!r}, want /etc"
        )
    if "motd" not in term_run(page, "ls", started["id"]):
        bad.append("launch arguments: the terminal did not start in the directory it was given")

    # `about` is a singleton: the first launch mounts, the second has no mount to hand
    # arguments to and must say so rather than dropping them.
    page.evaluate("window.webxp.launch('about')")
    time.sleep(0.8)
    page.evaluate("window.webxp.launch('about', { path: '/etc/motd' })")
    time.sleep(0.5)
    warned = page.evaluate("window.__warned.join(' | ')")
    if "singleton" not in warned:
        bad.append(
            "launch arguments: a singleton was launched with arguments and nothing was "
            f"reported — console.warn said {warned!r}"
        )
    return bad


def check_fs_persistence(page) -> list[str]:
    """A file written in the shell is still there after a reload.

    The reload is the check. Everything else about persistence is a claim about
    `localStorage` that a same-page assertion cannot distinguish from an in-memory tree.
    """
    bad = []
    # Its own session, at home. The terminal left open by the previous check is in /etc, and
    # the assertions below are about what a prompt at home prints.
    page.evaluate("window.webxp.launch('terminal')")
    time.sleep(1.0)
    shell = term_parts(page)["id"]
    written = term_run(page, "echo survives-a-reload > /home/user/persist.txt", shell)
    # The echoed line and nothing else. Written first as `!= echo and "No such file" in it`,
    # which is a condition the `and` makes dead for every failure that is not ENOENT: a write
    # refused for permissions or for space passed it silently.
    if written.strip() != "user@webxp:~$ echo survives-a-reload > /home/user/persist.txt":
        bad.append(f"persistence: the write printed {written.strip()[:160]!r}, want nothing")
    # Past the 300 ms debounce in `shared/fs/fs.mjs`.
    time.sleep(0.8)
    state = term_run(page, "df", shell)
    if "persistence: webxp.fs.v1" not in state:
        bad.append(f"persistence: df reports {state.strip()[:160]!r}")

    page.navigate(BASE_URL + "/")
    time.sleep(1.2)
    drain(page)
    launch(page, "terminal")
    restored = term_parts(page)
    shell = restored["id"]
    if "Filesystem restored" not in restored["text"]:
        bad.append(f"persistence: after a reload the banner says {restored['text'][:80]!r}")
    read = term_run(page, "cat persist.txt", shell)
    if "survives-a-reload" not in read:
        bad.append(
            f"persistence: after a reload the file reads {read.strip()[:120]!r} — the tree "
            "did not come back out of localStorage"
        )

    # A second session, sitting in a directory the seed tree also has. `format` must not
    # move it: the rebuild is one batch, and un-batched its first step told every watcher the
    # filesystem was empty — which moved this window to `/` and left it there.
    page.evaluate("window.webxp.launch('terminal', { path: '/home/user/notes' })")
    time.sleep(1.0)
    bystander = term_parts(page)["id"]
    opened_at = term_parts(page, bystander)["prompt"]
    if "/notes$" not in opened_at:
        bad.append(f"persistence: the second terminal opened at {opened_at!r}")

    # `format` puts the seed tree back, which is also how this check hands the desktop on
    # without the files it wrote.
    reset = term_run(page, "format --yes", shell, settle=0.8)
    if "filesystem reset" not in reset:
        bad.append(f"persistence: format said {reset.strip()[:120]!r}")
    after = term_run(page, "cat persist.txt", shell)
    if "No such file" not in after:
        bad.append(f"persistence: format left persist.txt behind — {after.strip()[:120]!r}")

    survivor = term_parts(page, bystander)
    if "/notes$" not in survivor["prompt"]:
        bad.append(
            f"persistence: after format the other terminal is at {survivor['prompt']!r} — it "
            "was moved out of a directory the seed tree still has"
        )
    elif "ideas.txt" not in term_run(page, "ls", bystander):
        bad.append("persistence: after format the other terminal cannot list its directory")
    fs_reset(page)
    return bad


#: The checks `--prove-failure` knows how to invert.
#:
#: Each takes `invert=True` and asserts the opposite of its usual claim, so a run with the
#: flag must report them as failures. Three is still thin, and the gap is recorded in
#: `docs/review.md` rather than papered over: for every other check, "shown capable of
#: failing" rests on a negative control run at the time it was written, or on the review
#: history, rather than on a control the suite re-runs.
INVERTIBLE = (check_drag, check_reentrant_gesture, check_terminal)
INVERTIBLE_NAMES = ("drag", "re-entrant gesture", "terminal")


CHECKS = (
    ("manifest is current", check_manifest_current),
    ("mount index is current", check_mount_index),
    ("boot", check_boot),
    ("launch all", check_launch_all),
    ("app loading", check_app_loading),
    ("app filetypes", check_app_filetypes),
    ("autostart", check_autostart),
    ("app styles", check_app_styles),
    ("broken app", check_broken_app),
    ("iframe app", check_iframe_app),
    ("basemap app", check_basemap_app),
    ("30DaysOfMaps folder", check_map_challenge_folder),
    # Before `day 02 lines`, deliberately: both read the files that day is about, and a
    # stale segment file should be named as stale before a browser renders it and the
    # failure arrives as a status-line message instead.
    ("day 02 segment spans", check_day_02_segment_spans),
    ("day 02 report", check_day_02_report),
    ("day 02 lines", check_day_02),
    ("active styling", check_active_styling),
    ("loading frame", check_loading_frame),
    ("drag", check_drag),
    ("drag is title-bar only (negative control)", check_drag_is_title_bar_only),
    ("resize se", check_resize_corner),
    ("resize w", check_resize_west),
    ("resize n at desktop edge", check_resize_north_at_edge),
    ("resize past the desktop", check_resize_oversize),
    ("offscreen clamp", check_offscreen_clamp),
    ("viewport shrink", check_viewport_shrink),
    ("viewport round trip", check_viewport_round_trip),
    ("minimum size", check_min_size),
    ("container query", check_container_query),
    ("minimise and restore", check_minimize_restore),
    ("taskbar toggle", check_taskbar_toggles),
    ("maximise", check_maximize),
    ("title bar double click", check_title_double_click),
    ("keyboard move and resize", check_keyboard),
    ("re-entrant gesture", check_reentrant_gesture),
    ("state announcements", check_announce_states),
    ("icon tokens", check_icon_tokens),
    ("focus order", check_focus_order),
    ("desktop blur", check_desktop_blur),
    ("close", check_close),
    ("singleton", check_singleton),
    ("link schemes", check_link_schemes),
    # Last, because it registers a program and provokes a console error. Its cleanup puts
    # the desktop back, but a check that deliberately breaks something belongs at the end.
    ("mount failure", check_mount_failure),
    ("ready after close", check_ready_after_close),
    # These two open and close windows of their own, and `desktop destroy` builds two
    # desktops in scratch roots. Here because the filesystem group reloads the page straight
    # after, so anything they leave behind is cleared by something other than their own
    # cleanup — which is the belt to their braces.
    ("cascade across a close", check_cascade_across_close),
    ("desktop destroy", check_desktop_destroy),
    # The filesystem group. After the window-manager checks because the first of these
    # reloads the page to clear the stored tree, and a reload closes every window the
    # earlier checks opened.
    ("filesystem", check_filesystem),
    ("terminal", check_terminal),
    # After `terminal`, because it asks the session that check opens.
    ("mount", check_mount),
    ("explorer", check_explorer),
    # After the app checks, because it navigates with a hash and reloads: it hands the
    # desktop back on a clean URL rather than leaving a route in the address bar.
    ("routes", check_routes),
    ("open across apps", check_open_across_apps),
    ("launch arguments", check_launch_arguments),
    ("filesystem persists", check_fs_persistence),
    # Last: it writes a folder and reloads the page, so it hands the desktop back fresh.
    ("scaffold a new app", check_scaffold),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        help="serve on this port instead of a free one the kernel picks; for attaching to "
        "the page yourself",
    )
    parser.add_argument("--out", default="screenshots")
    parser.add_argument(
        "--prove-failure",
        action="store_true",
        help="invert the drag and shell assertions, so the run must report a failure",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="SUBSTRING",
        help="run just the checks whose name contains SUBSTRING; repeatable. For proving one "
        "check can fail, not for reporting a clean tree.",
    )
    args = parser.parse_args(argv)

    checks = CHECKS
    if args.only:
        checks = tuple(
            (name, fn) for name, fn in CHECKS
            if any(want.lower() in name.lower() for want in args.only)
        )
        # A filter that matches nothing would otherwise run zero checks and report success,
        # which is the one outcome a harness must never produce.
        if not checks:
            print(f"--only matched no checks: {args.only}", file=sys.stderr)
            return 1

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(exist_ok=True)
    global SHOT_DIR
    SHOT_DIR = out_dir
    global BASE_URL

    failures: list[str] = []
    try:
        width, height = VIEWPORT
        # `serve` picks the port, proves the bytes on it are this tree's, and stops the
        # server on the way out.
        with serve(args.port) as base, Browser() as browser:
            BASE_URL = base
            page = browser.page()
            page.call("Runtime.enable")
            page.call("Log.enable")
            # No HTTP cache for the run.
            #
            # `scaffold a new app` writes an app folder, regenerates `apps/manifest.mjs` and
            # reloads. `http.server` validates with `Last-Modified`, whose granularity is one
            # second, so a manifest rewritten inside the same second as an earlier fetch
            # answers 304 and Chrome keeps the previous module. The check then reports the new
            # app missing from the loaded list — blaming the generator for a stale read,
            # intermittently, which is the worst kind of failure a harness can produce.
            #
            # Reproduced by `scripts/inspect_manifest_cache.py`, which forces the one-second
            # window on purpose: 2 stale reads in 5 rounds with the cache on, 0 in 5 with it
            # off. Re-run that script to re-confirm rather than trusting this comment.
            page.clear_cache()
            page.call(
                "Emulation.setDeviceMetricsOverride",
                width=width,
                height=height,
                deviceScaleFactor=1,
                mobile=False,
            )
            page.navigate(base + "/")
            # The apps have to finish loading before anything is measured.
            time.sleep(1.0)

            inverted_ran: list[str] = []
            inverted_fired: list[str] = []
            #: Every check the loop got to the end of. Compared to `checks` afterwards, so
            #: "this check never ran" cannot be inferred from a gap in the log — it is stated.
            ran: list[str] = []
            abandoned = ""
            try:
                for name, check in checks:
                    # A check that raises is a failed check, named.
                    #
                    # There was no `try` here. One raise took the run out through a
                    # traceback with no `[FAIL]` line and nothing identifying the check: a
                    # `KeyError: 'w'` out of `probe` stopped the loop while the log still
                    # read 14 passes, and the 26 checks behind it were unmeasured and unnamed.
                    # A harness must fail when it cannot read what it was pointed at, and it
                    # must say which thing it was.
                    try:
                        if args.prove_failure and check in INVERTIBLE:
                            inverted_ran.append(name)
                            found = check(page, invert=True)
                            if found:
                                inverted_fired.append(name)
                        else:
                            found = check(page)
                    except Exception as error:  # noqa: BLE001 - reported, never discarded
                        # The traceback to stderr, because the address of the raise is the
                        # most useful thing about it and a one-line message loses it.
                        traceback.print_exc()
                        kind = (
                            "could not measure" if isinstance(error, HarnessError)
                            else "raised"
                        )
                        found = [
                            f"{kind} {type(error).__name__}: {error} — this check did not "
                            "finish, so nothing it asserts was measured"
                        ]
                    failures += [f"{name}: {message}" for message in found]
                    mark = "FAIL" if found else "ok"
                    print(f"  [{mark:>4}] {name}")
                    for message in found:
                        print(f"         {message}")
                    shot = out_dir / f"{name.replace(' ', '-').replace('(', '').replace(')', '')}.png"
                    try:
                        shoot(page, shot)
                    except Exception as error:  # noqa: BLE001 - reported, never discarded
                        failures.append(f"{name}: the screenshot after it failed — {error}")
                        print(f"  [FAIL] {failures[-1]}")
                    ran.append(name)
            except Exception as error:  # noqa: BLE001 - reported, never discarded
                # Only a dead browser or a dead page reaches here, since every check is
                # already wrapped. Caught rather than raised so the report below still runs:
                # the one outcome this harness must never produce is a short run that looks
                # like a clean one.
                traceback.print_exc()
                abandoned = f"{type(error).__name__}: {error}"

            never = [name for name, _ in checks if name not in ran]
            if never:
                failures.append(
                    f"the run was abandoned after {len(ran)} of {len(checks)} checks"
                    + (f" — {abandoned}" if abandoned else "")
                    + f"; never measured: {', '.join(never)}"
                )
                print(f"  [FAIL] {failures[-1]}")

            # The end-of-run sweep and the final screenshot, guarded for the same reason the
            # loop is: a page that died mid-run took `main` out through a traceback from
            # here, so the failure count and the exit code were never printed and the only
            # evidence of 40 unmeasured checks was the absence of their lines. Measured by
            # closing the page from inside a check: the loop reported all three remaining
            # checks and then `shoot` raised out of `main`.
            try:
                errors = PENDING + console_errors(page)
            except Exception as error:  # noqa: BLE001 - reported, never discarded
                errors = []
                failures.append(f"console: the end-of-run sweep could not run — {error}")
                print(f"  [FAIL] {failures[-1]}")
            failures += [f"console: {message}" for message in errors]
            for message in errors:
                print(f"  [FAIL] console: {message}")

            try:
                size = shoot(page, out_dir / "desktop.png")
                print(f"\n  desktop.png  {size // 1024} KB")
            except Exception as error:  # noqa: BLE001 - reported, never discarded
                failures.append(f"the closing screenshot failed — {error}")
                print(f"  [FAIL] {failures[-1]}")
    except ServeError as error:
        # No server, or not this tree's server. Nothing was measured, so this is a failed
        # run and not a clean one.
        print(error, file=sys.stderr)
        return 1

    # `--prove-failure` inverts the exit code: the run is correct when the inversions fired.
    #
    # Without this the flag was decoration. It inverted two assertions, never asked whether
    # they had produced a failure, and printed "all checks passed" either way — so the one
    # control standing behind every clean run could not itself fail. A `--only` that excludes
    # both invertible checks is now an error rather than a vacuous pass.
    if args.prove_failure:
        if not inverted_ran:
            print(
                "\n--prove-failure inverted nothing: the filter excluded every invertible "
                f"check ({', '.join(sorted(INVERTIBLE_NAMES))})",
                file=sys.stderr,
            )
            return 1
        missed = [name for name in inverted_ran if name not in inverted_fired]
        if missed:
            print(
                f"\n--prove-failure: {', '.join(missed)} ran inverted and reported no "
                "failure, so the harness has not been shown capable of failing",
                file=sys.stderr,
            )
            return 1
        print(f"\n--prove-failure: {', '.join(inverted_fired)} failed as required")
        return 0

    if failures:
        print(f"\n{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
