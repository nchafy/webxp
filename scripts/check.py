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

    --prove-failure   Inverts the assertions of the checks in `INVERTIBLE` and requires that
                      every one of them failed and nothing else did, so a run that reports
                      clean is reporting something. A harness that has never been seen to
                      fail is a harness nobody has checked. `--help` lists them; the list is
                      derived from `INVERTIBLE`, so it cannot go stale.

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
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mount  # noqa: E402
from cdp import Browser, CDPError, Session  # noqa: E402
from css_ties import check as check_css_ties  # noqa: E402
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

#: The modifier bits `Input.dispatchKeyEvent` takes.
#:
#: Measured rather than remembered: each value was dispatched and the flags read back off the
#: event — 1 alt, 2 ctrl, 4 meta, 8 shift. A transposed pair reads as a product that ignores
#: its own accelerator, so the numbers are named here once instead of spelled at each call.
ALT, CTRL, SHIFT = 1, 2, 8
META = 4

#: The modifiers that suppress `text`, and so the insertion Blink would perform. Shift is
#: absent: it selects which character the key inserts, where Alt, Ctrl and Meta mean the
#: keypress is a command and inserts nothing. Ctrl alone is included although Blink already
#: drops it — the rule is about what a chord means, not about which of them Blink forgives.
TEXTLESS_MODIFIERS = ALT | CTRL | META

#: Key name -> (virtual key code, the text the key inserts). Extends `cdp.py`'s own table
#: rather than restating it.
#:
#: `cdp.py` is a verbatim copy of another project's client and carries only the keys that
#: project needed, so the keys it lacks are added here rather than by diverging the copy.
#: Derived from `Session.KEYS` and not re-spelled: two tables keyed by the same thing are a
#: hand-synced copy, and the four entries this file used to redeclare already lived there.
#:
#: The text field is load-bearing, which is why the value is a pair. A key that inserts text
#: has to be dispatched as `keyDown` carrying it; as a text-less `rawKeyDown` Blink never
#: synthesises the char event, so a `button` never fires its `click` and Enter on a menu row
#: silently does nothing.
MENU_KEYS = {
    **Session.KEYS,
    "ArrowLeft": (37, ""),
    "ArrowRight": (39, ""),
    "Home": (36, ""),
    "End": (35, ""),
    "Backspace": (8, ""),
    "F10": (121, ""),
    "ContextMenu": (93, ""),
}

#: The `code` of a single-letter key, which is what carries the letter when Alt is held.
LETTER_CODE = re.compile(r"Key([A-Z])$")


def press_key(page, name: str, *, modifiers: int = 0, code: str | None = None) -> None:
    """One real keypress: a name from `MENU_KEYS`, or a single character.

    The superset of `page.press`, and it shares that method's key table and its
    keyDown/rawKeyDown rule — `Session._event_type_for_key`, called rather than spelled again,
    underscore and all: `cdp.py` is a verbatim copy of another project's client, so promoting
    the method would diverge the copy for nothing. `page.press` is enough for the six
    keys `cdp.py` carries with no modifier but Shift; anything else — a letter, a chord, a
    `code` that differs from the `key` — comes here. A third dispatcher is not needed, and
    three is what this file had: `menu_key`, `key` and `arrow`, plus an inline Ctrl+S.

    `code` is separable from `name` on purpose, and it is a measured platform fact rather
    than generality for its own sake. macOS composes with Alt and reports the composed
    character, so Alt+V arrives as `key: '√'` with `code: 'KeyV'`; a probe that cannot
    express that cannot tell a mnemonic matcher reading `event.key` from one reading
    `event.code`, and the two disagree on every Alt chord a Mac reader presses.

    A single character is dispatched as the text it inserts, which is what makes a `button`
    fire its `click`: Enter sent as a text-less `rawKeyDown` leaves Blink waiting for a char
    event, so a menu row that works reports as doing nothing.

    `TEXTLESS_MODIFIERS` is the exception, and it is the browser's editing path that requires
    it rather than anything in the shell. `text` is what Blink inserts, so a chord carrying
    it types its own letter into whatever has the keyboard. Measured by pressing `q` into a
    Notepad buffer of known contents with the rule off: Alt+Q and Ctrl+Alt+Q each left a `q`
    behind, Ctrl+Q alone left nothing — Blink reads Ctrl+Alt as AltGr, which composes. The
    `Alt+Q` arm of `check_open_across_apps` is that measurement kept as an assertion.

    A key with no recorded code raises. Dispatching nothing and then reporting a working
    control as unresponsive is the worst outcome available to a harness.
    """
    physical = code or (name if name in MENU_KEYS else f"Key{name.upper()}")
    if name in MENU_KEYS:
        virtual, text = MENU_KEYS[name]
    else:
        letter = LETTER_CODE.match(physical)
        if not letter:
            raise HarnessError(f"press_key: no virtual key code for {name!r} / {physical!r}")
        virtual, text = ord(letter.group(1)), name
    if modifiers & TEXTLESS_MODIFIERS:
        text = ""
    for event_type in (Session._event_type_for_key(text), "keyUp"):
        params = {
            "type": event_type,
            "key": name,
            "code": physical,
            "windowsVirtualKeyCode": virtual,
            "nativeVirtualKeyCode": virtual,
            "modifiers": modifiers,
        }
        if text and event_type == "keyDown":
            params["text"] = text
            params["unmodifiedText"] = text
        page.call("Input.dispatchKeyEvent", **params)
    time.sleep(0.05)


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


def hover_at(page, x: float, y: float) -> None:
    """Put the real pointer there, so `:hover` is the browser's own."""
    mouse(page, "mouseMoved", x, y)
    time.sleep(0.08)


def click_at(page, x: float, y: float) -> None:
    """A real left click: move the pointer there, press, release.

    The move is not decoration. A menu switches under the pointer and moves its highlight on
    `pointerover`, so a press with no move before it lands on a row the menu was never told
    the pointer was over — a gesture no reader makes.

    One settle for both of this file's callers. The two were written at 0.08 s and 0.2 s;
    0.2 s is the one measured against the slower subject, a context-menu row whose `onSelect`
    re-derives the desktop's icons, so the faster value is the one that would go intermittent.
    """
    hover_at(page, x, y)
    mouse(page, "mousePressed", x, y, buttons=1)
    mouse(page, "mouseReleased", x, y)
    time.sleep(0.2)


def right_click(page, x: float, y: float) -> None:
    """A real right-button press, which is what makes Chrome synthesise `contextmenu`.

    `mouse` hardcodes the left button. Dispatching a `contextmenu` event from page script
    instead would prove the listener runs and nothing about whether a right-click reaches it.
    """
    mouse(page, "mouseMoved", x, y)
    for event_type, held in (("mousePressed", 2), ("mouseReleased", 0)):
        page.call("Input.dispatchMouseEvent", type=event_type, x=float(x), y=float(y),
                  button="right", buttons=held, clickCount=1)
    time.sleep(0.15)


def centre(page, selector: str, *, index: int = 0) -> tuple[float, float]:
    """The middle of an element, to aim real input at.

    A zero box is a raise, not a click into nothing: aiming at a hidden element is the
    harness failing to reach what it was pointed at.
    """
    box = page.evaluate(
        "(() => { const el = document.querySelectorAll(" + repr(selector) + ")["
        + str(index) + "];"
        " if (!el) return null; const r = el.getBoundingClientRect();"
        " return [r.left + r.width / 2, r.top + r.height / 2, r.width, r.height]; })()"
    )
    if not box or box[2] < 1 or box[3] < 1:
        raise HarnessError(f"nothing to aim at for {selector!r}[{index}]: {box!r}")
    return box[0], box[1]


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


def expose(page, program: str, *also: str) -> dict:
    """Move a window so its own title bar is what is topmost at the bar's midpoint.

    For the checks that compute a rect and press it. A press lands on whatever is topmost at
    that point, so a window left open by an earlier check can take it — and the press then
    reports as the product failing to respond. `launch all` leaves one window per program
    open and cascades them into the upper left, so the occlusion arrives with the fortieth
    app rather than with any change to what is being tested.

    Moving the target, rather than closing the others: closing them is what a reader would
    reach for first, and it breaks every later check that expects a window an earlier one
    opened — measured, three of them. A check must create its own condition without
    destroying anyone else's.

    Returns the fresh probe state. Raises if the bar is still covered, because a press that
    cannot land must not be reported as a product verdict.
    """
    for name in (program, *also):
        if window_of(probe(page), name) is None:
            launch(page, name)
    selector = win_selector(page, program)
    # Minimised, not closed and not moved. Closed breaks every later check expecting a window
    # an earlier one opened — measured, three of them. Moved is not enough either: with one
    # window per program open there is no clear ground to move to, and this function then
    # correctly refuses rather than pressing blind. A minimised window keeps its taskbar
    # button and its place in `windows`, so nothing downstream can tell, and the desktop is
    # clear.
    page.evaluate(
        "(() => { const keep = new Set(%s);"
        " for (const w of window.webxp.windows) {"
        # Restored as well as minimised: an earlier check may have left the target minimised,
        # and a minimised window has no laid-out title bar to press.
        "   if (keep.has(w.programId)) { if (w.state === 'min') w.restore(); }"
        "   else if (w.state !== 'min') w.minimize(); } })()"
        % json.dumps([program, *also])
    )
    time.sleep(0.5)
    bar = rect_of(page, f"{selector} .window-title")
    if bar is None:
        raise CDPError(f"{program}: no laid-out title bar to expose")
    on_top = page.evaluate(
        "(() => { const e = document.elementFromPoint(%f, %f);"
        " return e ? !!e.closest('%s .window-title') : false; })()"
        % (bar["x"] + bar["w"] / 2, bar["y"] + bar["h"] / 2, selector)
    )
    if not on_top:
        raise CDPError(
            f"{program}: its title bar is still covered after being moved clear, so a press "
            "there would measure whatever is over it rather than this window"
        )
    return probe(page)


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

#: WCAG 2.2 SC 1.4.11, for the boundary of a control against what it sits on. `AA_NORMAL`
#: above it is SC 1.4.3, for text.
AA_NON_TEXT = 3.0


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


#: Keys measured to hide a headless document, and so to stop its frame clock: Escape, the
#: four arrows, Home, End, Backspace and F10 all do it, and Tab, Enter, Space, ContextMenu
#: and a letter do not. Two of them are driven, because the hide is one browser-side
#: behaviour and not a property of any one key. This is the only place that list is written
#: down; every other site names this tuple.
STRAY_KEYS = ("Escape", "ArrowDown")


def check_stray_keypress(page) -> list[str]:
    """A key no shell handler consumes must not stop the desktop opening windows.

    Every awaited zoom in the shell comes back through `animation.finished` at
    `src/window.mjs:136` — the launch's at `src/desktop.mjs:532`, and `restore`, `minimize`
    and `close` at `src/window.mjs:573`, `:560` and `:613`. A hidden document never settles
    it, because finish events are dispatched from the rendering lifecycle a hidden document
    skips. Measured with the document hidden: `webxp.launch('notepad')` stays pending while
    the zoom's `playState` already reads `finished`.

    So a dead clock stalls the launch before `win.setContent` (`src/desktop.mjs:545`), and
    the window does show it — the body is still `is-loading` over its `window-loading`
    child, which is what `check_boot` asserts above. The promise is read here instead because
    it names the clock itself rather than one window's symptom, and because the `launch`
    helper cannot: it sleeps past the loading floor and clicks, awaiting nothing.

    `src/desktop.mjs:555-557` waits two frames of its own before `program.ready`, and that is
    a second stall, not this one. It is reached only after `setContent`, so a window wedged
    there has its content in and `is-loading` cleared. Measured with `requestAnimationFrame`
    stubbed out and the document visible: `about`, which defines no `ready`, resolves, while
    `notepad`, which defines one at `apps/notepad/app.mjs:362`, stays pending with a plain
    `window-body app-notepad`.

    Early in `CHECKS` on purpose. The frame clock is a precondition of every check that opens
    a window, and a dead one has to be named here rather than inferred from a wall of
    downstream failures with reasons of their own.
    """
    bad: list[str] = []
    drain(page)
    for key in STRAY_KEYS:
        press_key(page, key)
    pressed = ", ".join(STRAY_KEYS)
    page.evaluate(
        "(() => { window.__frame = 'pending';"
        " requestAnimationFrame(() => { window.__frame = 'fired'; }); })()"
    )
    deadline = time.time() + 1.0
    while time.time() < deadline and page.evaluate("window.__frame") == "pending":
        time.sleep(0.05)
    if page.evaluate("window.__frame") != "fired":
        bad.append(
            f"stray keypress: no frame arrived in the second after {pressed}, which nothing "
            "consumed — the shell waits on frames, so everything it opens stops finishing"
        )
    page.evaluate(
        "(() => { window.__strayLaunch = 'pending';"
        " window.webxp.launch('notepad').then("
        "   (w) => { window.__strayLaunch = w.closed ? 'closed' : 'resolved'; },"
        "   (e) => { window.__strayLaunch = 'rejected: ' + e; }); })()"
    )
    deadline = time.time() + 3.0
    while time.time() < deadline and page.evaluate("window.__strayLaunch") == "pending":
        time.sleep(0.1)
    outcome = page.evaluate("window.__strayLaunch")
    if outcome != "resolved":
        bad.append(
            f"stray keypress: after {pressed}, launching notepad answered {outcome!r} within "
            "3s — a launch that never settles leaves a window nobody can finish"
        )
    closed = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'notepad');"
        " delete window.__frame; delete window.__strayLaunch;"
        " if (!w) return 'no window'; w.close(); return 'closed'; })()"
    )
    if closed != "closed":
        bad.append(f"stray keypress: the window it opened could not be handed back ({closed})")
    time.sleep(0.3)
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

        # And the thing all of that is for: no pile — among the windows that hold a lane.
        #
        # Not every open window: `cascade` takes `index % lanes` (`src/window.mjs`), and the
        # comment on `lanes` there says why — it wraps rather than letting the cascade walk off
        # the desktop and get clamped into a pile in the corner. `lanes` is what the desktop's
        # height affords this window, so once more windows are open than there are lanes two of
        # them share an origin by design. Asserting distinctness across all of them would make
        # this check a count of how many apps the tree ships. Distinct lanes while there are
        # lanes to be distinct in, and not one window further.
        #
        # The lane assignment is the invariant, and `cascadeSlot` is where it is readable. A
        # window placed from an explicit `x`/`y` never cascades and holds no slot, which is
        # why those are excluded rather than counted as collisions.
        #
        # What `cascadeIndex` does promise is a lane nobody holds *when one exists*. That is
        # the half worth asserting, and the budget is what separates the two.
        lanes = page.evaluate(
            "(() => { const d = window.webxp;"
            " const open = d.windows.filter((w) => w.cascadeSlot !== undefined);"
            " const budget = open.map((w) => Math.max("
            "   Math.floor((w.surface().h - w.box.h - 40) / 26), 1));"
            " return { slots: open.map((w) => w.cascadeSlot),"
            "          budget: budget.length ? Math.min(...budget) : 0 }; })()"
        )
        slots, budget = lanes["slots"], lanes["budget"]
        if len(slots) <= budget and len(set(slots)) != len(slots):
            doubled = sorted({s for s in slots if slots.count(s) > 1})
            bad.append(
                f"cascade across a close: {len(slots)} cascaded windows fit in {budget} "
                f"lanes and hold {len(set(slots))} — lane(s) {doubled} are held twice, so "
                "two windows opened on top of each other with a free lane going spare"
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


#: The box `check_modal` puts up: three answers, a default, and a cancel.
#:
#: One spec, because every assertion below is about this same box — a second spelling of it
#: could disagree with the first and the check would still pass.
MODAL_SPEC = {
    "title": "Notepad",
    "text": "The text in this file has changed. Save it?",
    "icon": "notepad",
    "buttons": [
        {"id": "save", "text": "Save", "default": True},
        {"id": "discard", "text": "Don't Save"},
        {"id": "cancel", "text": "Cancel", "cancel": True},
    ],
}

#: `src/dialogs.mjs`, imported into the page and parked on `window`.
#:
#: Nothing in the shipped graph imports it yet — the file picker is what will — so the module
#: is reached the way its first caller will reach it, by path. A path that 404s raises out of
#: `evaluate` rather than returning, so a renamed file is a failed check and not a skipped one.
LOAD_DIALOGS = """(async () => {
  window.__dialogs = await import('/src/dialogs.mjs');
  return { keys: Object.keys(window.__dialogs).sort(), dismissed: window.__dialogs.DISMISSED };
})()"""

#: A focusable control in the Read me window's own client area, and the focus on it.
#:
#: The check makes its own: `inert` is only observable against something that would otherwise
#: take the keyboard, and the Read me is prose. It is also the element focus must come back to
#: when the dialog goes, which is why it is focused here rather than merely appended.
MODAL_PROBE = """(() => {
  const win = window.webxp.windows.find((w) => w.programId === 'readme');
  if (!win) return 'no Read me window to put a dialog on';
  let button = win.bodyEl.querySelector('#modal-probe');
  if (!button) {
    button = document.createElement('button');
    button.id = 'modal-probe';
    button.type = 'button';
    button.textContent = 'probe';
    win.bodyEl.append(button);
  }
  button.focus();
  return document.activeElement === button
    ? ''
    : 'the probe button in the client area would not take focus, so nothing below measures inert';
})()"""

#: Open a box on the Read me window and park its answer on `window.__answer`.
#:
#: `'pending'` until the promise settles, so "the box resolved" and "the box resolved to the
#: wrong id" are different readings — an unresolved promise is the defect a dangling `await`
#: is made of, and a check that only compared ids would call it a pass.
OPEN_BOX = """((spec) => {
  const win = window.webxp.windows.find((w) => w.programId === 'readme');
  if (!win) return 'no Read me window to put a dialog on';
  window.__answer = 'pending';
  try {
    window.__dialogs.messageBox(win, spec).then((id) => { window.__answer = id; });
  } catch (error) {
    return String((error && error.message) || error);
  }
  return '';
})(%s)"""

#: What a refused spec said, and whether it got as far as the screen.
MODAL_REFUSAL = """((spec) => {
  const win = window.webxp.windows.find((w) => w.programId === 'readme');
  if (!win) return { error: 'no Read me window to put a dialog on' };
  try {
    window.__dialogs.messageBox(win, spec);
    return { threw: '', layers: win.el.querySelectorAll('.window-modal').length };
  } catch (error) {
    return {
      threw: String((error && error.message) || error),
      layers: win.el.querySelectorAll('.window-modal').length,
    };
  }
})(%s)"""

#: A second modal on a window that already has one.
MODAL_SECOND = """(() => {
  const win = window.webxp.windows.find((w) => w.programId === 'readme');
  if (!win) return { error: 'no Read me window to put a dialog on' };
  try {
    win.showModal(document.createElement('div'));
    return { threw: '', layers: win.el.querySelectorAll('.window-modal').length };
  } catch (error) {
    return {
      threw: String((error && error.message) || error),
      layers: win.el.querySelectorAll('.window-modal').length,
    };
  }
})()"""

#: Close the window the question is on, keeping the window object reachable afterwards.
#:
#: The object outliving its frame is the point: `messageBox` on it must refuse, because a
#: detached frame has no button anyone can press and the promise could never settle.
CLOSE_OWNER = """(() => {
  const win = window.webxp.windows.find((w) => w.programId === 'readme');
  if (!win) return 'no Read me window to close';
  window.__closedWin = win;
  win.close();
  return '';
})()"""

#: A box asked for in a window that has already closed.
STALE_BOX = """((spec) => {
  if (!window.__closedWin) return 'no closed window was kept';
  try {
    window.__dialogs.messageBox(window.__closedWin, spec);
    return '';
  } catch (error) {
    return String((error && error.message) || error);
  }
})(%s)"""

#: Everything one reading of the modal layer can answer.
#:
#: `inert` is asked by trying it, not by reading the property: the property being set is a
#: statement about the code, and "the keyboard cannot reach the content behind the dialog" is
#: the statement the visitor is affected by. The poke is undone unless it worked, in which case
#: the check is already failing and where the focus sits no longer matters.
MODAL_STATE = """(() => {
  const win = window.webxp.windows.find((w) => w.programId === 'readme');
  if (!win) return { error: 'no Read me window' };
  const layer = win.el.querySelector('.window-modal');
  const dialog = layer ? layer.querySelector('.dialog') : null;
  const active = document.activeElement;
  const probeButton = win.bodyEl.querySelector('#modal-probe');
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left), y: Math.round(r.top),
             w: Math.round(r.width), h: Math.round(r.height) };
  };
  const named = (attr) => {
    if (!dialog) return null;
    const target = document.getElementById(dialog.getAttribute(attr) || '');
    return target ? target.textContent : null;
  };
  let bodyTookFocus = null;
  if (probeButton) {
    probeButton.focus();
    bodyTookFocus = document.activeElement === probeButton;
    if (!bodyTookFocus && active && active.focus) active.focus();
  }
  return {
    answer: window.__answer,
    hasModal: win.hasModal === true,
    layers: win.el.querySelectorAll('.window-modal').length,
    parentedToWindow: layer ? layer.parentElement === win.el : null,
    inBody: layer ? win.bodyEl.contains(layer) : null,
    inert: win.bodyEl.inert === true,
    bodyTookFocus: bodyTookFocus,
    role: dialog ? dialog.getAttribute('role') : null,
    ariaModal: dialog ? dialog.getAttribute('aria-modal') : null,
    label: named('aria-labelledby'),
    description: named('aria-describedby'),
    buttons: [...(dialog ? dialog.querySelectorAll('.dialog-buttons button') : [])]
      .map((b) => Object.assign({ id: b.dataset.id || '' }, box(b))),
    activeId: active && active.dataset ? (active.dataset.id || '') : '',
    activeInDialog: dialog ? dialog.contains(active) : false,
    activeIsProbe: Boolean(probeButton) && active === probeButton,
    activeWhere: active ? (active.id || active.className || active.tagName) : 'nothing',
    win: box(win.el),
    bar: box(win.bar),
    layerBox: layer ? box(layer) : null,
    dialogBox: dialog ? box(dialog) : null,
  };
})()"""


def modal_state(page) -> dict:
    """One reading of the modal layer, with the window it needs proved present."""
    state = page.evaluate(MODAL_STATE)
    if not isinstance(state, dict) or state.get("error"):
        raise HarnessError(f"modal: {(state or {}).get('error', state)}")
    return state


def check_modal(page) -> list[str]:
    """A window can ask a question, and while it is asking, the question is the only answer.

    Four properties, and each one is reachable without the others — which is why all four are
    measured rather than inferred from a dialog being on screen:

    - *parented to the window*, not to the desktop. A dialog floating over the whole shell is
      a web app's modal; XP's belonged to the window that asked, and this one is a child of
      the frame so a drag and a maximise carry it for free.
    - *modal*. The client area is `inert`, so the control behind the dialog refuses the
      keyboard. win32.run's scrim only blinks: the window underneath stays live, and this is
      the assertion that tells the two apart.
    - *trapped*. Tab cycles the dialog's own buttons and wraps, instead of walking out into
      the taskbar behind a question that is still waiting.
    - *resolvable*. Escape, a press, and the owning window closing all settle the promise. An
      unresolved promise is a dangling `await` with no error and no window to point at, so
      `'pending'` is read back explicitly rather than compared against an id.

    The box is Notepad's dirty-buffer question, because that is the thing this shell could not
    previously ask: closing a modified file threw the buffer away with no prompt.
    """
    bad = []
    expose(page, "readme")

    module = page.evaluate(LOAD_DIALOGS, await_promise=True)
    for name in ("DISMISSED", "messageBox"):
        if name not in module["keys"]:
            return [f"modal: src/dialogs.mjs exports {module['keys']} — no {name!r} to call"]
    dismissed = module["dismissed"]

    refused = page.evaluate(MODAL_PROBE)
    if refused:
        return [f"modal: {refused}"]

    # --- the box goes up -----------------------------------------------------
    rejected = page.evaluate(OPEN_BOX % json.dumps(MODAL_SPEC))
    if rejected:
        return [f"modal: messageBox refused a usable spec — {rejected}"]
    time.sleep(0.15)
    state = modal_state(page)

    if not state["hasModal"] or state["layers"] != 1:
        return [
            f"modal: hasModal={state['hasModal']} with {state['layers']} layer(s) in the "
            "window — the box never reached the screen, so nothing below was measured"
        ]
    if not state["parentedToWindow"]:
        bad.append(
            "modal: the layer is not a child of the window element — a dialog parented "
            "anywhere else does not travel with a drag or a maximise"
        )
    if state["inBody"]:
        bad.append(
            "modal: the layer is inside .window-body, which is the element that goes inert — "
            "the dialog would make itself unreachable"
        )

    # Over the content and not over the title bar. The bar stays live while a modal is up, so
    # a scrim across it would dim a strip that still answers the pointer.
    layer, win, bar = state["layerBox"], state["win"], state["bar"]
    if not near(layer["y"], win["y"] + bar["h"], 2):
        bad.append(
            f"modal: the layer starts at y={layer['y']} with the title bar ending at "
            f"{win['y'] + bar['h']} — it is over the bar, or short of the content"
        )
    for edge, got, want in (
        ("left", layer["x"], win["x"]),
        ("right", layer["x"] + layer["w"], win["x"] + win["w"]),
        ("bottom", layer["y"] + layer["h"], win["y"] + win["h"]),
    ):
        if not near(got, want, 2):
            bad.append(
                f"modal: the layer's {edge} edge is at {got} and the window's is at {want} — "
                "the scrim does not cover the window's own content"
            )
    dialog = state["dialogBox"]
    if dialog["x"] < layer["x"] or dialog["x"] + dialog["w"] > layer["x"] + layer["w"]:
        bad.append(
            f"modal: the dialog {dialog} hangs outside the layer {layer} — a window at "
            "MIN_W is the case this breaks in"
        )

    if state["role"] != "dialog" or state["ariaModal"] != "true":
        bad.append(
            f"modal: the dialog is role={state['role']!r} aria-modal={state['ariaModal']!r} — "
            "a screen reader in browse mode reads the tree, not the focus rules"
        )
    if state["label"] != MODAL_SPEC["title"]:
        bad.append(
            f"modal: aria-labelledby resolves to {state['label']!r}, not the title "
            f"{MODAL_SPEC['title']!r} — the dialog is announced unnamed"
        )
    if state["description"] != MODAL_SPEC["text"]:
        bad.append(
            f"modal: aria-describedby resolves to {state['description']!r}, not the question "
            f"{MODAL_SPEC['text']!r}"
        )
    want_buttons = [button["id"] for button in MODAL_SPEC["buttons"]]
    got_buttons = [button["id"] for button in state["buttons"]]
    if got_buttons != want_buttons:
        bad.append(
            f"modal: the button row is {got_buttons} and the spec asked for "
            f"{want_buttons} — in that order, left to right"
        )

    # --- it is actually modal ------------------------------------------------
    if not state["inert"]:
        bad.append(
            "modal: .window-body is not inert while a dialog is up — the content behind it "
            "is live, which is win32.run's blinking scrim rather than a modal"
        )
    if state["bodyTookFocus"]:
        bad.append(
            "modal: a button in the client area took the focus with the dialog up — the "
            "keyboard reaches behind the question"
        )
    if not state["activeInDialog"] or state["activeId"] != "save":
        bad.append(
            f"modal: focus opened on {state['activeWhere']!r} (data-id {state['activeId']!r}) "
            "instead of the button marked default — the dialog opened with the keyboard "
            "somewhere else"
        )

    # --- and it fits at the narrowest a window goes -----------------------------
    #
    # `min(400px, ...)` on the dialog and the wrap on its button row are the load-bearing half
    # of the box's size, and the default window is too wide to show either. A window can be
    # dragged to MIN_W; a flat 400px dialog hangs out of both sides of the frame that owns it,
    # and three 75px buttons do not fit across what is left.
    page.evaluate(
        "window.webxp.windows.find((w) => w.programId === 'readme')"
        ".sizeTo(%d, %d)" % (MIN_W, MIN_H * 3)
    )
    time.sleep(0.2)
    narrow = modal_state(page)
    inside = narrow["layerBox"]
    for name, got in (("the dialog", narrow["dialogBox"]), *[
        (f"the {button['id']} button", button) for button in narrow["buttons"]
    ]):
        if got["x"] < inside["x"] or got["x"] + got["w"] > inside["x"] + inside["w"]:
            bad.append(
                f"modal: in a {MIN_W}px window, {name} spans "
                f"{got['x']}..{got['x'] + got['w']} and the window's content is "
                f"{inside['x']}..{inside['x'] + inside['w']} — an answer is off the edge of "
                "the frame that is asking"
            )
    # Back to the size it was found at, which is the one the rest of this check presses
    # buttons in.
    page.evaluate(
        "window.webxp.windows.find((w) => w.programId === 'readme').sizeTo(%d, %d)"
        % (state["win"]["w"], state["win"]["h"])
    )
    time.sleep(0.2)

    # --- Tab is trapped and wraps -------------------------------------------
    for step, want in enumerate(("discard", "cancel", "save"), start=1):
        page.press("Tab")
        time.sleep(0.05)
        got = modal_state(page)
        if got["activeId"] != want or not got["activeInDialog"]:
            bad.append(
                f"modal: Tab {step} of 3 landed on {got['activeWhere']!r} (data-id "
                f"{got['activeId']!r}) instead of {want!r} — focus is not cycling the "
                "dialog's own buttons"
            )
            break
    page.press("Tab", shift=True)
    time.sleep(0.05)
    back = modal_state(page)
    if back["activeId"] != "cancel":
        bad.append(
            f"modal: Shift+Tab off the first button landed on {back['activeWhere']!r} "
            f"(data-id {back['activeId']!r}) instead of the last — the wrap is one-way"
        )

    # --- Escape resolves it, and hands the keyboard back --------------------
    page.press("Escape")
    time.sleep(0.15)
    after = modal_state(page)
    if after["answer"] != "cancel":
        bad.append(
            f"modal: Escape resolved {after['answer']!r} — the box marks 'cancel' as the "
            "dismissal, and 'pending' means the promise is still unresolved with the dialog "
            "already gone"
        )
    if after["hasModal"] or after["layers"]:
        bad.append(
            f"modal: after Escape, hasModal={after['hasModal']} with {after['layers']} "
            "layer(s) still in the window"
        )
    if after["inert"]:
        bad.append(
            "modal: .window-body is still inert after the dialog closed — the window is left "
            "unusable, which is worse than never having asked"
        )
    if not after["activeIsProbe"]:
        bad.append(
            f"modal: focus landed on {after['activeWhere']!r} after the dialog closed, not "
            "back on the control that had it — the next Tab starts from the top of the page"
        )

    # --- a box with no cancel still has an answer ---------------------------
    page.evaluate(OPEN_BOX % json.dumps({
        "title": "Notepad",
        "text": "Saved.",
        "buttons": [{"id": "ok", "text": "OK"}],
    }))
    time.sleep(0.1)
    page.press("Escape")
    time.sleep(0.15)
    plain = modal_state(page)
    if plain["answer"] != dismissed:
        bad.append(
            f"modal: a box with no button marked cancel resolved {plain['answer']!r} on "
            f"Escape, not {dismissed!r} — a promise that can be left pending is a leak, and "
            "an id a button carries would be indistinguishable from a press"
        )

    # --- and taking the layer down by hand still answers it -----------------
    #
    # `closeModal` is public and a caller will reach for it. Every route down goes through it,
    # so it is the one place the owner is told; a teardown that only removed the layer would
    # strand the promise exactly as a close once did.
    page.evaluate(OPEN_BOX % json.dumps(MODAL_SPEC))
    time.sleep(0.1)
    page.evaluate("window.webxp.windows.find((w) => w.programId === 'readme').closeModal()")
    time.sleep(0.15)
    by_hand = modal_state(page)
    if by_hand["answer"] != "cancel":
        bad.append(
            f"modal: closeModal() left the question at {by_hand['answer']!r} — the layer came "
            "down without its owner being told, which is a promise nobody resolves"
        )

    # --- a press resolves its own id ----------------------------------------
    page.evaluate(OPEN_BOX % json.dumps(MODAL_SPEC))
    time.sleep(0.1)
    selector = f'{win_selector(page, "readme")} .dialog-buttons button[data-id="discard"]'
    target = rect_of(page, selector)
    if target is None:
        bad.append(f"modal: no laid-out button at {selector} to press")
        page.press("Escape")
    else:
        x = target["x"] + target["w"] / 2
        y = target["y"] + target["h"] / 2
        on_top = page.evaluate(
            "(() => { const hit = document.elementFromPoint(%f, %f);"
            " return hit ? (hit.dataset.id || hit.className) : 'nothing'; })()" % (x, y)
        )
        if on_top != "discard":
            # The harness's problem, not the product's: a press that lands elsewhere would be
            # reported as the dialog ignoring its own button.
            bad.append(
                f"modal: {on_top!r} is over the Don't Save button at ({x:.0f}, {y:.0f}), so a "
                "press there measures whatever is covering it"
            )
            page.press("Escape")
        else:
            mouse(page, "mousePressed", x, y, buttons=1)
            mouse(page, "mouseReleased", x, y)
            time.sleep(0.15)
            pressed = modal_state(page)
            if pressed["answer"] != "discard":
                bad.append(
                    f"modal: pressing Don't Save resolved {pressed['answer']!r} — a box "
                    "resolves the id of the button that was pressed"
                )
            if pressed["hasModal"]:
                bad.append("modal: the dialog is still up after its own button was pressed")

    # --- a second modal is refused, not stacked -----------------------------
    page.evaluate(OPEN_BOX % json.dumps(MODAL_SPEC))
    time.sleep(0.1)
    second = page.evaluate(MODAL_SECOND)
    if "already open" not in (second["threw"] or ""):
        bad.append(
            f"modal: a second showModal on the same window said {second['threw']!r} — two "
            "dialogs over one window have no close order, and the one underneath is a "
            "question somebody is awaiting"
        )
    if second["layers"] != 1:
        bad.append(f"modal: {second['layers']} layers in the window after the refusal")

    # --- the owning window closing answers the open question ----------------
    missing = page.evaluate(CLOSE_OWNER)
    if missing:
        return bad + [f"modal: {missing}"]
    time.sleep(SETTLE_S + 0.3)
    closed = page.evaluate(
        "({ answer: window.__answer,"
        " windows: window.webxp.windows.filter((w) => w.programId === 'readme').length })"
    )
    if closed["answer"] != "cancel":
        bad.append(
            f"modal: closing the window left the question at {closed['answer']!r} — whoever "
            "awaited the answer waits for the rest of the session, with no error and no "
            "window left to point at"
        )
    if closed["windows"]:
        bad.append(f"modal: {closed['windows']} Read me window(s) survived the close")

    stale = page.evaluate(STALE_BOX % json.dumps(MODAL_SPEC))
    if "closed" not in (stale or ""):
        bad.append(
            f"modal: asking a question in a window that has closed said {stale!r} — nothing "
            "can press a button on a detached frame, so the box has to be refused rather "
            "than returning a promise that cannot settle"
        )

    # Put the desktop back: every later check that wants a Read me window expects to find one.
    launch(page, "readme")
    time.sleep(0.2)
    if page.evaluate("window.webxp.windows.filter((w) => w.programId === 'readme').length") != 1:
        bad.append("modal: the Read me window was not relaunched — the desktop is not as found")

    # --- a malformed question is refused before it reaches the visitor ------
    for spec, needle in (
        ({"title": "x", "text": "y", "buttons": [{"id": "ok", "label": "OK"}]}, "label"),
        ({"title": "x", "text": "y", "buttons": []}, "non-empty array"),
        ({"title": "", "text": "y", "buttons": [{"id": "ok", "text": "OK"}]}, "title"),
        ({"title": "x", "text": "y", "buttons": [
            {"id": "ok", "text": "OK"}, {"id": "ok", "text": "Fine"},
        ]}, "repeats the id"),
    ):
        refusal = page.evaluate(MODAL_REFUSAL % json.dumps(spec))
        if needle not in (refusal["threw"] or ""):
            bad.append(
                f"modal: messageBox({spec}) said {refusal['threw']!r}, which does not name "
                f"{needle!r} — a box whose answer the caller cannot handle must be refused "
                "where the defect is, not rendered"
            )
        if refusal["layers"]:
            bad.append(f"modal: a refused spec still put {refusal['layers']} layer(s) up")

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
    # The read-me alone. This presses a computed point on its title bar, and a window left
    # open over that point takes the press instead — which reports as "the first pointer did
    # not start a gesture", a true sentence about a press that never reached the bar.
    expose(page, "readme")
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


# ---------------------------------------------------------------------------
# The menu bar. `src/widgets.mjs` holds `menuBar`; `src/menu.css` is its skin, shared with
# the context menus.
# ---------------------------------------------------------------------------

#: How long the bar is given to settle after a build or an open.
SETTLE_MENU_S = 0.35

#: The id the fixture puts on its own bar, so every read below addresses that bar and not
#: whichever one is first in the document.
#:
#: Not cosmetic. `apps/notepad/app.mjs` builds a menu bar of its own and `src/contextmenu.mjs`
#: renders the same `.menu-popup` / `.menu-item` classes, so a bare `.menu-bar` or
#: `.menu-item[aria-disabled="true"]` resolves to whatever the suite has left open — and a
#: check that measures a different widget from the one it names reports the wrong verdict
#: about the wrong thing.
MENU_BAR = "#menu-probe-bar"

# --- the fixture -----------------------------------------------------------------------
# A window with a menu bar covering every feature: groups, an accelerator, a check item, a
# disabled row, and one submenu.

MENU_BUILD = """
(async () => {
  const { menuBar } = await import('/src/widgets.mjs');
  window.__menuLog = [];
  const say = (what) => window.__menuLog.push(what);
  // On `document`, in the bubble phase, so it runs after the bar's own handler on the window
  // body and therefore sees whether that handler took the chord off the browser.
  //
  // The listener is kept on `window` by name so the teardown can remove it. An anonymous
  // arrow function cannot be detached, and `check_menu_handed_back` holds what that cost.
  window.__menuKeys = [];
  if (!window.__menuKeyWatch) {
    window.__menuKeyWatch = (event) => window.__menuKeys.push({
      key: event.key, ctrl: event.ctrlKey, shift: event.shiftKey, alt: event.altKey,
      taken: event.defaultPrevented,
    });
    document.addEventListener('keydown', window.__menuKeyWatch);
  }
  const win = await window.webxp.launch('readme');
  // Both numbers fixed, and the width is not decoration. 120px is short on purpose: `menu
  // geometry` has to measure a popup taller than what is left of the client area under the
  // bar, which is the case the fixed positioning exists for. The width was `win.box.w`, i.e.
  // whatever the resize and maximise checks had left this window at — which made `menu
  // edges` order-dependent and cost a run: parked at the same inset the popup overflowed the
  // viewport in isolation and fitted inside it in a full run, and the check's guard reported
  // "no flip is being asked for" rather than passing on nothing. 560 is readme's own
  // declared width.
  win.sizeTo(560, 120);
  const bar = menuBar({
    scope: win.bodyEl,
    label: 'Probe menu bar',
    menus: [
      { label: '&File', groups: [
        [{ label: '&Open\\u2026', accel: 'Ctrl+O', onSelect: () => say('open') },
         { label: 'Save &As\\u2026', accel: 'Ctrl+Shift+S', onSelect: () => say('saveas') }],
        [{ label: 'Recen&t', groups: [[{ label: 'readme.txt', onSelect: () => say('recent') }]] }],
        [{ label: 'E&xit', onSelect: () => say('exit') }],
      ] },
      { label: '&Edit', groups: [
        [{ label: '&Undo', accel: 'Ctrl+Z', disabled: true, onSelect: () => say('undo') },
         { label: '&Word wrap', checked: false, onSelect: (on) => say(`wrap:${on}`) }],
      ] },
      { label: '&View', groups: [[{ label: '&Status bar', onSelect: () => say('status') }]] },
    ],
  });
  bar.id = 'menu-probe-bar';
  // A second bar, built and dropped. `menu style` asserts one link per document, which only
  // measures anything if `menuBar` has been called more than once.
  menuBar({ menus: [{ label: '&Help', groups: [[{ label: '&About', onSelect: () => say('about') }]] }] });
  win.setContent([bar, document.createElement('textarea')]);
  window.__menuWin = win;
  return { ok: true };
})()
"""

#: Whether the fixture is still standing, asked of the document rather than of a flag.
#:
#: A module-level `built` boolean was the first shape of this, and it is the shape
#: `verification.md` forbids: the flag survives a page reload and the DOM it describes does
#: not, so the fixture would be reported present while every read below addressed nothing.
MENU_STANDING = """
(() => {
  const win = window.__menuWin;
  const bar = document.querySelector('%s');
  if (!win || win.closed || !bar || !win.el.contains(bar)) return null;
  return { id: win.id, titles: bar.querySelectorAll('.menu-title').length };
})()
""" % MENU_BAR


def menu_fixture(page) -> None:
    """Build the fixture window if it is not already standing.

    Raises rather than returning a problem list: a check that could not build what it is
    pointed at has measured nothing, and `HarnessError` is this suite's word for that.
    """
    standing = page.evaluate(MENU_STANDING)
    if standing and standing["titles"] == 3:
        return
    built = page.evaluate(MENU_BUILD, await_promise=True)
    if not built or not built.get("ok"):
        raise HarnessError(f"the menu fixture did not build: {built!r}")
    time.sleep(SETTLE_MENU_S)
    standing = page.evaluate(MENU_STANDING)
    if not standing or standing["titles"] != 3:
        raise HarnessError(f"the menu fixture built but is not in the document: {standing!r}")


#: Closed through the route a reader uses — a pointer down outside the bar — not by hiding
#: the popups. Reaching into the DOM leaves the bar's own state saying a menu is open, and the
#: next click on a title then reads as a click on the open one and closes it.
#:
#: What this deliberately does *not* do is put the roving tab stop back. The stop is per-bar
#: state that `rove` (`src/widgets.mjs:753`) leaves wherever the last menu was opened, and
#: three checks here move it — so an assertion that names one title passes or fails on which
#: check ran before it. The fix is in the assertion and not in the reset: `MENU_HANDOFF` and
#: `MENU_ROVING` compare the focused title against whichever title carries tabIndex 0, which
#: is the invariant. Forcing the stop here instead would make `menu roles`' "want one 0" read
#: this line rather than `src/widgets.mjs:667`.
MENU_RESET = """
(() => {
  document.querySelector('.desktop').dispatchEvent(
    new PointerEvent('pointerdown', { bubbles: true }));
  const bar = document.querySelector('%s');
  if (!bar) return { missing: 'no fixture bar in the document' };
  const titles = [...bar.querySelectorAll('.menu-title')];
  return {
    expanded: titles.map((t) => t.getAttribute('aria-expanded')),
    hidden: [...bar.querySelectorAll('.menu-popup')].every((p) => p.hidden),
  };
})()
""" % MENU_BAR


def menu_reset(page) -> None:
    """Dismiss every menu, and fail if that did not take."""
    state = page.evaluate(MENU_RESET)
    # The harness has to fail when its own setup did not take, or every check after this one
    # measures a bar in a state nobody asked for.
    if state.get("missing"):
        raise HarnessError(f"menu reset: {state['missing']}")
    if state["expanded"] != ["false", "false", "false"] or not state["hidden"]:
        raise HarnessError(f"the reset left the bar at {state}")
    time.sleep(0.05)


FOCUSED = """
(() => {
  const el = document.activeElement;
  if (!el) return null;
  const label = el.querySelector && el.querySelector('.menu-label');
  return label ? label.textContent : (el.className || el.tagName);
})()
"""

EXPANDED = """
[...document.querySelectorAll('%s .menu-title')].map((t) => t.getAttribute('aria-expanded'))
""" % MENU_BAR


def focused(page):
    return page.evaluate(FOCUSED)


def expanded(page):
    return page.evaluate(EXPANDED)


ROLES = """
(() => {
  const bar = document.querySelector('%s');
  if (!bar) return { missing: 'no fixture menu bar in the document' };
  const titles = [...bar.querySelectorAll('.menu-title')];
  const rows = [...bar.querySelectorAll('.menu-item')];
  return {
    barRole: bar.getAttribute('role'),
    barLabel: bar.getAttribute('aria-label'),
    titleRoles: titles.map((t) => t.getAttribute('role')),
    haspopup: titles.map((t) => t.getAttribute('aria-haspopup')),
    expanded: titles.map((t) => t.getAttribute('aria-expanded')),
    tabindexes: titles.map((t) => t.tabIndex),
    popupRoles: [...bar.querySelectorAll('.menu-popup')].map((p) => p.getAttribute('role')),
    popupNamed: [...bar.querySelectorAll('.menu-popup')].every(
      (p) => p.hasAttribute('aria-labelledby') || p.hasAttribute('aria-label')),
    groupRoles: [...bar.querySelectorAll('.menu-group')].map((g) => g.getAttribute('role')),
    rowRoles: rows.map((r) => r.getAttribute('role')),
    rowTabindexes: [...new Set(rows.map((r) => r.tabIndex))],
    rowTags: [...new Set(rows.map((r) => r.tagName))],
    underlined: titles.map((t) => (t.querySelector('u') || {}).textContent || null),
    accels: rows.filter((r) => r.querySelector('.menu-accel'))
      .map((r) => r.querySelector('.menu-accel').textContent),
    checkable: rows.filter((r) => r.getAttribute('role') === 'menuitemcheckbox')
      .map((r) => r.getAttribute('aria-checked')),
    disabled: rows.filter((r) => r.getAttribute('aria-disabled') === 'true')
      .map((r) => r.querySelector('.menu-label').textContent),
  };
})()
""" % MENU_BAR

#: Where the frame hands focus, and whether that is the bar's own tab stop.
#:
#: `isRoving` and not a hardcoded 'File'. The roving stop is per-bar state that `rove` leaves
#: wherever the last menu was opened, so naming one title asserts the order the checks happen
#: to run in; the invariant is that the title holding focus is the title holding tabIndex 0.
MENU_HANDOFF = """
(() => {
  const win = window.__menuWin;
  const titles = [...document.querySelectorAll('%s .menu-title')];
  document.querySelector('.start').focus();
  win.takeFocus();
  const el = document.activeElement;
  const label = el.querySelector && el.querySelector('.menu-label');
  return {
    on: el.className, text: label ? label.textContent : null, tabIndex: el.tabIndex,
    roving: titles.findIndex((t) => t.tabIndex === 0),
    at: titles.indexOf(el),
  };
})()
""" % MENU_BAR


def check_menu_roles(page) -> list[str]:
    """The bar is a menubar a screen reader and a keyboard can both work."""
    menu_fixture(page)
    menu_reset(page)
    state = page.evaluate(ROLES)
    if state.get("missing"):
        return [f"menu bar: {state['missing']}"]
    bad = []
    if state["barRole"] != "menubar":
        bad.append(f"menu bar: role is {state['barRole']!r}, want 'menubar'")
    if not state["barLabel"]:
        bad.append("menu bar: the bar has no aria-label")
    if state["titleRoles"] != ["menuitem"] * 3:
        bad.append(f"menu bar: title roles are {state['titleRoles']}")
    if state["haspopup"] != ["true"] * 3:
        bad.append(f"menu bar: titles report aria-haspopup {state['haspopup']}")
    if state["expanded"] != ["false"] * 3:
        bad.append(f"menu bar: a closed bar reports aria-expanded {state['expanded']}")
    # One tab stop for the whole bar: a menubar is reached with Tab and walked with arrows.
    # Three tab stops is the defect this asserts against, not a style preference.
    if sorted(state["tabindexes"]) != [-1, -1, 0]:
        bad.append(f"menu bar: title tabindexes are {state['tabindexes']}, want one 0")
    if state["popupRoles"] != ["menu"] * 4:
        bad.append(f"menu bar: popup roles are {state['popupRoles']}, want four 'menu'")
    if not state["popupNamed"]:
        bad.append("menu bar: a popup has no accessible name")
    if set(state["groupRoles"]) != {"group"}:
        bad.append(f"menu bar: group roles are {state['groupRoles']}")
    if set(state["rowRoles"]) != {"menuitem", "menuitemcheckbox"}:
        bad.append(f"menu bar: row roles are {set(state['rowRoles'])}")
    if state["rowTabindexes"] != [-1]:
        bad.append(f"menu bar: row tabindexes are {state['rowTabindexes']}, want only -1")
    # win32.run's rows are `div on:click`. A row has to be something Enter activates on its
    # own, which is the half of the fix that is not an ARIA attribute.
    if state["rowTags"] != ["BUTTON"]:
        bad.append(f"menu bar: rows are {state['rowTags']}, want BUTTON")
    # Every row is a `button` with `tabindex="-1"`, and `window.mjs`'s `FOCUSABLE` picks the
    # first match in the client area when the frame hands focus inwards. The titles come
    # before the rows in the tree, so the bar's own tab stop is what that lands on — asserted,
    # because the alternative is focus going to a row inside a popup that is `hidden`.
    landed = page.evaluate(MENU_HANDOFF)
    if landed["on"] != "menu-title" or landed["tabIndex"] != 0 or landed["at"] != landed["roving"]:
        bad.append(
            f"menu bar: the frame handed focus to {landed}, not the bar's own tab stop — the "
            "title holding focus has to be the title holding tabIndex 0"
        )
    if state["underlined"] != ["F", "E", "V"]:
        bad.append(f"menu bar: the mnemonics underlined are {state['underlined']}")
    if state["accels"] != ["Ctrl+O", "Ctrl+Shift+S", "Ctrl+Z"]:
        bad.append(f"menu bar: the accelerator column reads {state['accels']}")
    if state["checkable"] != ["false"]:
        bad.append(f"menu bar: check items report aria-checked {state['checkable']}")
    if state["disabled"] != ["Undo"]:
        bad.append(f"menu bar: the disabled rows are {state['disabled']}")
    return bad


#: The bar's open state and its roving stop, read together.
MENU_ROVING = """
(() => {
  const titles = [...document.querySelectorAll('%s .menu-title')];
  const el = document.activeElement;
  return {
    expanded: titles.map((t) => t.getAttribute('aria-expanded')),
    roving: titles.findIndex((t) => t.tabIndex === 0),
    at: titles.indexOf(el),
    tabindexes: titles.map((t) => t.tabIndex),
  };
})()
""" % MENU_BAR


def check_menu_keyboard(page) -> list[str]:
    """Arrow keys walk the bar, the popups and one submenu; Escape comes back out.

    Driven with real keypresses. A synthesised `KeyboardEvent` proves the handler runs and
    says nothing about whether the keystroke reaches it.
    """
    menu_fixture(page)
    menu_reset(page)
    bad = []
    # Entered the way a reader does, and the assertion that the bar is one tab stop rather
    # than three. From the last title box, because that is the element immediately before the
    # client area: the window's title bar is followed by its own three buttons.
    page.evaluate(
        "(() => { const boxes = window.__menuWin.el.querySelectorAll('.title-box');"
        " boxes[boxes.length - 1].focus(); return 1; })()"
    )
    press_key(page, "Tab")
    entered = page.evaluate(MENU_ROVING)
    if entered["at"] < 0 or entered["at"] != entered["roving"]:
        return [f"menu keyboard: Tab from the title bar landed on {focused(page)!r} "
                f"({entered}), not the bar's one tab stop"]
    # Home on the closed bar, so the walk below starts from a title this check names rather
    # than from wherever `rove` was left by whichever menu was opened last. `move(0)` roves
    # and focuses in one step, which is also the assertion that Home works before any menu
    # is down — the later "home on the bar" step asserts it again after a walk.
    press_key(page, "Home")
    if focused(page) != "File":
        return [f"menu keyboard: Home on the closed bar reached {focused(page)!r}, not the "
                "first title, so the walk below has no known starting point"]

    steps = []

    def step(name):
        steps.append((name, focused(page), expanded(page)))

    press_key(page, "ArrowDown");  step("down opens")
    press_key(page, "ArrowDown");  step("next row")
    press_key(page, "ArrowUp"); press_key(page, "ArrowUp"); step("wrap up")
    press_key(page, "End");        step("end")
    press_key(page, "Home");       step("home")
    press_key(page, "Escape");     step("escape")
    press_key(page, "ArrowRight"); step("right along bar")
    press_key(page, "ArrowDown");  step("second menu")
    # Home and End on the bar itself, which move the roving tab stop rather than opening.
    press_key(page, "Escape")
    press_key(page, "End");        step("end of the bar")
    # Read before Home takes it back: the roving stop has to travel with the focus, and a
    # census taken before anything moved cannot tell a stop that follows from one that does
    # not. `menu roles` asserts the count; this asserts that the count is on the right title.
    roved = page.evaluate(MENU_ROVING)
    press_key(page, "Home");       step("home on the bar")

    want = {
        "down opens": ("Open…", ["true", "false", "false"]),
        "next row": ("Save As…", ["true", "false", "false"]),
        "wrap up": ("Exit", ["true", "false", "false"]),
        "end": ("Exit", ["true", "false", "false"]),
        "home": ("Open…", ["true", "false", "false"]),
        "escape": ("File", ["false", "false", "false"]),
        "right along bar": ("Edit", ["false", "false", "false"]),
        "second menu": ("Undo", ["false", "true", "false"]),
        "end of the bar": ("View", ["false", "false", "false"]),
        "home on the bar": ("File", ["false", "false", "false"]),
    }
    seen = dict((name, (where, state)) for name, where, state in steps)
    for name, expect in want.items():
        if seen[name] != expect:
            bad.append(f"menu keyboard: after {name!r} saw {seen[name]}, want {expect}")
    if roved["at"] != roved["roving"] or sorted(roved["tabindexes"]) != [-1, -1, 0]:
        bad.append(
            f"menu keyboard: End along the bar left focus on title {roved['at']} with the "
            f"tab stop on title {roved['roving']} ({roved['tabindexes']}) — a stop that does "
            "not travel with the walk puts the next Tab back where the reader started"
        )

    # One level of submenu, in and out by three different keys.
    press_key(page, "ArrowDown")
    press_key(page, "ArrowDown"); press_key(page, "ArrowDown")
    if focused(page) != "Recent":
        return bad + [f"menu keyboard: three ArrowDowns reached {focused(page)!r}, not the "
                      "submenu row, so nothing below is measuring a submenu"]
    press_key(page, "ArrowRight")
    if focused(page) != "readme.txt":
        bad.append(f"menu keyboard: ArrowRight on a submenu row reached {focused(page)!r}")
    # Escape one level in closes the submenu and leaves the menu down. Separately from the
    # next step, because closing the whole bar here looks like a working Escape until you are
    # one level in.
    press_key(page, "Escape")
    if (focused(page), expanded(page)) != ("Recent", ["true", "false", "false"]):
        bad.append(
            f"menu keyboard: Escape inside a submenu left focus on {focused(page)!r} and the "
            f"bar at {expanded(page)}"
        )
    press_key(page, "ArrowRight")
    press_key(page, "ArrowLeft")
    if (focused(page), expanded(page)) != ("Recent", ["true", "false", "false"]):
        bad.append(
            f"menu keyboard: ArrowLeft inside a submenu left focus on {focused(page)!r} and "
            f"the bar at {expanded(page)}"
        )
    # Tab out closes the bar and does not trap focus in it.
    press_key(page, "Tab")
    if expanded(page) != ["false", "false", "false"]:
        bad.append(f"menu keyboard: Tab left the bar open at {expanded(page)}")
    if focused(page) == "Recent":
        bad.append("menu keyboard: Tab left focus inside the popup it closed")

    # Focus leaving the bar by a route that is not Tab — a script, or the shell raising
    # another window — closes the menu too, and focus moving *within* the bar does not.
    #
    # Both edges, because the handler is one `if`: `focusout` closes unless
    # `bar.contains(event.relatedTarget)`. Asserting only the close would pass on a handler
    # that closes on every focus move, which would take the menu down as the arrows walk it.
    #
    # Drivable only because the document has focus: `focusout` does not fire otherwise, and
    # `main` holds `Emulation.setFocusEmulationEnabled` on for the whole run. So
    # `document.hasFocus()` is true here and a scripted `.start.focus()` over an open menu
    # closes the bar.
    menu_reset(page)
    click_at(page, *centre(page, f"{MENU_BAR} .menu-title"))
    press_key(page, "ArrowDown")
    inside_before = expanded(page)
    page.evaluate(
        "(() => { document.querySelector('%s .menu-title').focus(); })()" % MENU_BAR
    )
    time.sleep(0.1)
    if inside_before != ["true", "false", "false"]:
        bad.append(
            f"menu keyboard: the bar was at {inside_before} before the focus moves, so "
            "neither focusout assertion measured a menu coming down"
        )
    elif expanded(page) != ["true", "false", "false"]:
        bad.append(
            f"menu keyboard: focus moving from a row to its own title closed the bar "
            f"({expanded(page)}) — focusout has to ignore a move inside the bar, or the "
            "arrows take the menu down as they walk it"
        )
    page.evaluate("(() => { document.querySelector('.start').focus(); })()")
    time.sleep(0.1)
    if expanded(page) != ["false", "false", "false"]:
        bad.append(
            f"menu keyboard: a script moving focus out of the bar left it at {expanded(page)} "
            "— a menu left open over the window that took the focus is painted over it and "
            "answers the pointer"
        )
    return bad


LOG = "window.__menuLog"
KEYLOG = "window.__menuKeys"


def take_log(page, what=LOG):
    """Read a log and clear it, so each step reads only its own."""
    return page.evaluate(f"(() => {{ const seen = {what}; {what} = []; return seen; }})()")


def open_menu(page, index):
    """Dismiss whatever is open, then click a title open, and say so if it did not open.

    Unconditional on purpose. A click on the title of a menu that is already down closes it,
    so a probe that opens one menu and later asks for another is reading a hidden element —
    which `centre` reports as a zero box several steps away from the cause.
    """
    menu_reset(page)
    x, y = centre(page, f"{MENU_BAR} .menu-title", index=index)
    click_at(page, x, y)
    state = expanded(page)
    if state[index] != "true":
        hit = page.evaluate(
            f"(() => {{ const el = document.elementFromPoint({round(x)}, {round(y)});"
            " return el ? (el.className || el.tagName) : null; })()"
        )
        raise HarnessError(
            f"a click on title {index} at ({round(x)}, {round(y)}) left the bar at {state}; "
            f"what is at that point is {hit!r}"
        )


def focus_textarea(page):
    page.evaluate(
        "(() => { window.__menuWin.bodyEl.querySelector('textarea').focus();"
        " return document.activeElement.tagName; })()"
    )


def check_menu_pointer(page) -> list[str]:
    """What the pointer does to the bar: open, switch, toggle shut, and open again.

    The sequence is the ordinary one and it is the one that broke: the pointer crossing the
    second title opens that menu on the way, so a click-toggle keyed only on "is it open"
    shuts the bar on click File, click Edit.
    """
    menu_fixture(page)
    menu_reset(page)
    bad = []
    steps = []
    for index in (1, 1, 0, 1, 1):
        click_at(page, *centre(page, f"{MENU_BAR} .menu-title", index=index))
        steps.append((index, expanded(page)))
    want = [
        (1, ["false", "true", "false"]),     # a click on a closed bar opens that menu
        (1, ["false", "false", "false"]),    # a second click on the same title closes it
        (0, ["true", "false", "false"]),     # and another title opens its own
        (1, ["false", "true", "false"]),     # click File then click Edit: Edit, not nothing
        (1, ["false", "false", "false"]),    # still a toggle once the click is what opened it
    ]
    if steps != want:
        bad.append(f"menu pointer: the click sequence went {steps}, want {want}")
    return bad


def check_menu_activation(page) -> list[str]:
    """What a row does when it is activated, and what a disabled one does not do.

    Every chord here is a real keypress into the window, not a synthesised event, because the
    property under test is that it reaches the bar from wherever focus happens to be.
    """
    menu_fixture(page)
    menu_reset(page)
    bad = []
    rows = f"{MENU_BAR} .menu-item"
    # The log is not cleared by the reset: `open_menu` resets mid-check, and a flip recorded
    # before it has to survive. Cleared once here instead.
    take_log(page)

    # A disabled row: Enter on it does nothing, and its chord is left to the browser rather
    # than swallowed by a row that does nothing.
    open_menu(page, 1)
    hover_at(page, *centre(page, f'{rows}[aria-disabled="true"]'))
    press_key(page, "Enter")
    fired = take_log(page)
    if fired:
        bad.append(f"menu activation: Enter on a disabled row fired {fired}")
    menu_reset(page)
    focus_textarea(page)
    take_log(page, KEYLOG)
    press_key(page, "z", modifiers=CTRL)
    if take_log(page):
        bad.append("menu activation: Ctrl+Z under a disabled Undo ran its action")
    chords = take_log(page, KEYLOG)
    if not chords:
        return bad + ["menu activation: the keydown recorder saw no Ctrl+Z, so nothing here "
                      "is measuring a real keypress"]
    if chords[-1]["taken"]:
        bad.append(
            "menu activation: Ctrl+Z under a disabled Undo was taken off the browser — a row "
            "that does nothing must not swallow its own chord"
        )

    # A live accelerator fires, and is taken off the browser.
    press_key(page, "o", modifiers=CTRL)
    press_key(page, "s", modifiers=CTRL | SHIFT)
    if take_log(page) != ["open", "saveas"]:
        bad.append("menu activation: Ctrl+O and Ctrl+Shift+S did not both reach their rows")
    chords = take_log(page, KEYLOG)
    # The recorder saw both, guarded exactly as the Ctrl+Z arm above is: `all()` is true of an
    # empty log, so a `__menuKeyWatch` that skipped `addEventListener` on a stale global left
    # this assertion passing on no keypresses at all.
    if len(chords) != 2:
        return bad + [f"menu activation: the keydown recorder saw {chords}, want the two "
                      "chords just pressed, so nothing here is measuring a real keypress"]
    if not all(chord["taken"] for chord in chords):
        bad.append(f"menu activation: a live accelerator was left to the browser — {chords}")

    # The near misses. A bare letter and an extra modifier are different chords.
    press_key(page, "o")
    press_key(page, "o", modifiers=CTRL | ALT)
    near_miss = take_log(page)
    if near_miss:
        bad.append(f"menu activation: a bare 'o' or Ctrl+Alt+O fired {near_miss}, want neither")
    take_log(page, KEYLOG)

    # Bound in its own scope. The Start button is outside the window, so this chord has to
    # reach nothing: a document-level binding would make every window answer every other
    # window's accelerators.
    page.evaluate("(() => { document.querySelector('.start').focus(); return 1; })()")
    press_key(page, "o", modifiers=CTRL)
    outside = take_log(page)
    if outside:
        bad.append(
            f"menu activation: Ctrl+O fired {outside} with focus on the Start button — an "
            "accelerator is bound in its own scope, not on the document"
        )
    if not take_log(page, KEYLOG):
        bad.append("menu activation: the out-of-scope chord was never dispatched")

    # Alt and the mnemonic, spelled the way macOS spells it.
    focus_textarea(page)
    press_key(page, "√", code="KeyV", modifiers=ALT)
    if expanded(page) != ["false", "false", "true"]:
        bad.append(f"menu activation: Alt+V left the bar at {expanded(page)}")
    if focused(page) != "Status bar":
        bad.append(f"menu activation: Alt+V focused {focused(page)!r}")

    # A check item is the bar's own state: Enter flips it and reports what it flipped to.
    menu_reset(page)
    open_menu(page, 1)
    hover_at(page, *centre(page, f"{rows}[role='menuitemcheckbox']"))
    press_key(page, "Enter")
    first = page.evaluate(
        "document.querySelector('%s .menu-item[role=\"menuitemcheckbox\"]')"
        ".getAttribute('aria-checked')" % MENU_BAR
    )
    if first != "true":
        bad.append(f"menu activation: a check item read aria-checked={first!r} after one Enter")
    open_menu(page, 1)
    hover_at(page, *centre(page, f"{rows}[role='menuitemcheckbox']"))
    press_key(page, "Enter")
    flips = take_log(page)
    if flips != ["wrap:true", "wrap:false"]:
        bad.append(f"menu activation: the check item reported {flips}, want both flips")
    return bad


BEFORE_OPEN = """
(() => {
  const body = window.__menuWin.bodyEl;
  return { scrollHeight: body.scrollHeight, clientHeight: body.clientHeight };
})()
"""

GEOMETRY = """
(() => {
  const bar = document.querySelector('%s');
  const body = window.__menuWin.bodyEl;
  const title = bar.querySelector('.menu-title');
  const popup = bar.querySelector('.menu-popup');
  const pop = popup.getBoundingClientRect();
  const box = body.getBoundingClientRect();
  const titleBox = title.getBoundingClientRect();
  // Hit-tested, not inferred from the rect: a clipped popup still reports its full box.
  const row = [...popup.querySelectorAll('.menu-item')].pop();
  const rowBox = row.getBoundingClientRect();
  const hit = document.elementFromPoint(
    Math.round(rowBox.left + rowBox.width / 2), Math.round(rowBox.top + rowBox.height / 2));
  return {
    open: title.getAttribute('aria-expanded'),
    below: Math.round(pop.top - titleBox.bottom),
    aligned: Math.round(pop.left - titleBox.left),
    overflowsBody: pop.bottom > box.bottom + 1,
    scrollHeight: body.scrollHeight,
    lastRowReachable: row.contains(hit) || hit === row,
    hitClass: hit ? hit.className : null,
    insideViewport: pop.right <= document.documentElement.clientWidth + 1
      && pop.bottom <= document.documentElement.clientHeight + 1,
    sticky: getComputedStyle(bar).position,
  };
})()
""" % MENU_BAR


def check_menu_geometry(page) -> list[str]:
    """A popup leaves the client area instead of being clipped by it."""
    menu_fixture(page)
    menu_reset(page)
    before = page.evaluate(BEFORE_OPEN)
    click_at(page, *centre(page, f"{MENU_BAR} .menu-title"))
    state = page.evaluate(GEOMETRY)
    bad = []
    if state["open"] != "true":
        return ["menu geometry: a click on the first title did not open its menu"]
    # The popup must add nothing to what the client area can scroll. Measured as a delta,
    # because the fixture window is deliberately too short for its own content and so has
    # something to scroll either way.
    if state["scrollHeight"] != before["scrollHeight"]:
        bad.append(
            f"menu geometry: opening the popup took the client area's scroll height from "
            f"{before['scrollHeight']} to {state['scrollHeight']}, so the popup is in the "
            "body's flow rather than over it"
        )
    if state["below"] != 0:
        bad.append(f"menu geometry: the popup opens {state['below']}px below its title's foot")
    if state["aligned"] != 0:
        bad.append(f"menu geometry: the popup's left edge is {state['aligned']}px off its title")
    # The point of the whole arrangement: the fixture window is 120px tall and the File menu
    # is taller than what is left of it under the bar.
    if not state["overflowsBody"]:
        bad.append(
            "menu geometry: the popup fits inside the client area, so this measures nothing — "
            "the fixture window has to be shorter than its own File menu"
        )
    if not state["lastRowReachable"]:
        bad.append(
            f"menu geometry: the last row is not hit-testable — {state['hitClass']!r} is on top "
            "of it, so the popup is being clipped by the client area"
        )
    if not state["insideViewport"]:
        bad.append("menu geometry: the popup is placed outside the viewport")
    if state["sticky"] != "sticky":
        bad.append(f"menu geometry: the bar's position is {state['sticky']!r}, want sticky")
    return bad


#: Read with the pointer on a row and the keyboard on its neighbour, so `:hover` is the one
#: thing the highlight can be coming from.
#:
#: Both states are read back and asserted, because they coincide by default and that is what
#: made the first version of this vacuous: `src/widgets.mjs:932-933` — the row's own
#: `pointerenter` listener — focuses the row under the pointer, and `src/menu.css:80-87` puts
#: `.menu-item:hover` and `.menu-item:focus` in one rule. Measured: with the pointer parked
#: and the focus left on the row, deleting `.menu-item:hover` from the cascade changed nothing
#: the check could see. With focus moved off by one ArrowDown the same deletion reads
#: `rgba(0, 0, 0, 0)`.
STYLE = """
((hovered) => {
  const bar = document.querySelector('%s');
  const sheets = [...document.querySelectorAll('link[rel="stylesheet"]')]
    .filter((link) => link.href.endsWith('/src/menu.css'));
  const title = bar.querySelector('.menu-title');
  const popup = bar.querySelector('.menu-popup');
  const rows = [...popup.querySelectorAll('.menu-item')];
  const flat = getComputedStyle(rows[0]);
  const row = bar.querySelector(hovered);
  const lit = getComputedStyle(row);
  return {
    sheetCount: sheets.length,
    titleLit: getComputedStyle(title).backgroundColor,
    titleMatchesHover: title.matches(':hover'),
    titleMatchesFocus: title.matches(':focus'),
    borderWidth: flat.borderTopWidth,
    backgroundImage: flat.backgroundImage,
    rowHeight: Math.round(rows[0].getBoundingClientRect().height),
    rowMatchesHover: row.matches(':hover'),
    rowMatchesFocus: row.matches(':focus'),
    hovered: lit.backgroundColor,
    hoveredInk: lit.color,
    separators: [...popup.querySelectorAll(':scope > .menu-group')]
      .map((g) => getComputedStyle(g).borderTopWidth),
    gutter: getComputedStyle(rows[0].querySelector('.menu-check')).width,
  };
})('.menu-popup .menu-item')
""" % MENU_BAR

DISABLED = """
(() => {
  const row = document.querySelector('%s .menu-item[aria-disabled="true"]');
  const style = getComputedStyle(row);
  return { colour: style.color, background: style.backgroundColor,
           matchesHover: row.matches(':hover') };
})()
""" % MENU_BAR


def check_menu_style(page) -> list[str]:
    """One stylesheet, a flat row, and the ties `theme.css`'s `button` rules create."""
    menu_fixture(page)
    menu_reset(page)
    click_at(page, *centre(page, f"{MENU_BAR} .menu-title"))
    hover_at(page, *centre(page, f"{MENU_BAR} .menu-popup .menu-item"))
    # One step of the keyboard, to take focus off the row the pointer is on. Without it the
    # two states coincide and the highlight read below is a reading of `:focus`.
    press_key(page, "ArrowDown")
    state = page.evaluate(STYLE)
    bad = []
    if not state["rowMatchesHover"] or state["rowMatchesFocus"]:
        return bad + [
            f"menu style: the row being read is hover={state['rowMatchesHover']} "
            f"focus={state['rowMatchesFocus']} — it has to be hovered and not focused, or the "
            "highlight assertions below are reading the focus rule and `.menu-item:hover` "
            "could be deleted without this check noticing"
        ]
    if state["hovered"] != "rgb(58, 110, 165)":
        bad.append(f"menu style: the row under the pointer is {state['hovered']}, want --title-b")
    if state["hoveredInk"] != "rgb(255, 255, 255)":
        bad.append(f"menu style: its label is {state['hoveredInk']}, want white")
    # The gutter stripe and the check column are one number, `--menu-gutter`.
    if state["gutter"] != "20px":
        bad.append(f"menu style: the check gutter is {state['gutter']}, want --menu-gutter")
    if state["sheetCount"] != 1:
        bad.append(f"menu style: menu.css is linked {state['sheetCount']} times, want once")
    # `theme.css` gives every button a bevel and a gradient. A menu row that kept them reads
    # as a column of buttons, and the rules that flatten it are one class against a type.
    if state["borderWidth"] != "0px":
        bad.append(f"menu style: a row kept its button border ({state['borderWidth']})")
    if state["backgroundImage"] != "none":
        bad.append(f"menu style: a row kept a background image ({state['backgroundImage']})")
    # The open menu's title stays lit while the pointer is in its popup. The two other
    # selectors in that rule are read back as not matching, which is what makes this the
    # `[aria-expanded="true"]` arm rather than a second reading of hover or focus.
    if state["titleMatchesHover"] or state["titleMatchesFocus"]:
        bad.append(
            f"menu style: the title is hover={state['titleMatchesHover']} "
            f"focus={state['titleMatchesFocus']}, so its colour below is not the "
            "[aria-expanded] rule's"
        )
    elif state["titleLit"] != "rgb(58, 110, 165)":
        bad.append(f"menu style: an open menu's title is {state['titleLit']}, want --title-b")
    # The first group carries no rule; every later one does. That is what makes the grouping
    # the separator rather than an item a caller has to place.
    if state["separators"][:1] != ["0px"] or set(state["separators"][1:]) != {"1px"}:
        bad.append(f"menu style: the group rules are {state['separators']}")

    # A dead row does not light up under the pointer.
    #
    # What this does *not* do is decide the specificity tie it looks like it decides.
    # `src/menu.css:98`'s disabled rule is (0,3,0) against `.menu-item:hover`'s (0,2,0), but
    # it also sits after the highlight rule in the file, so source order already settles it:
    # measured, this read still reports ok with the selector dropped to two classes. The pin
    # that would catch that belongs in `scripts/css_ties.py`, which compares specificity and
    # nothing else. This is the behavioural half — the row is transparent with a real pointer
    # on it — and the pointer being there is asserted rather than assumed.
    #
    # Reached by hovering Edit's title, not clicking it. With File already down, the pointer
    # crossing Edit opens Edit (XP's behaviour) and a click would then read as a click on the
    # open menu and close it. Asserted, because the aim below depends on it.
    hover_at(page, *centre(page, f"{MENU_BAR} .menu-title", index=1))
    switched = expanded(page)
    if switched != ["false", "true", "false"]:
        return bad + [
            f"menu style: the pointer crossing the second title left the bar at {switched}, so "
            "sliding along an open bar does not switch menus"
        ]
    hover_at(page, *centre(page, f'{MENU_BAR} .menu-item[aria-disabled="true"]'))
    dead = page.evaluate(DISABLED)
    if not dead["matchesHover"]:
        return bad + [
            "menu style: the disabled row is not under the pointer, so the two readings below "
            "say nothing about what a reader sees when they cross it"
        ]
    if dead["colour"] != "rgb(184, 180, 162)":
        bad.append(f"menu style: a disabled row is {dead['colour']}, want --sunk")
    if dead["background"] != "rgba(0, 0, 0, 0)":
        bad.append(f"menu style: a disabled row lights up under the pointer ({dead['background']})")
    return bad


REFUSALS = (
    ("no onSelect", "{ menus: [{ label: 'F', groups: [[{ label: 'Dead' }]] }] }",
     "would do nothing"),
    ("two levels deep",
     "{ menus: [{ label: 'F', groups: [[{ label: 'A', groups: [[{ label: 'B',"
     " groups: [[{ label: 'C', onSelect(){} }]] }]] }]] }] }",
     "one level is the most"),
    ("an accel with no scope",
     "{ menus: [{ label: 'F', groups: [[{ label: 'A', accel: 'Ctrl+O', onSelect(){} }]] }] }",
     "no scope to bind it in"),
    ("an empty group", "{ menus: [{ label: 'F', groups: [[]] }] }", "empty group"),
    ("no groups", "{ menus: [{ label: 'F' }] }", "has no groups"),
    ("no label", "{ menus: [{ label: 'F', groups: [[{ onSelect(){} }]] }] }", "has no label"),
    ("no menus", "{ menus: [] }", "at least one menu"),
    ("an accel on a submenu",
     "{ scope: document.body, menus: [{ label: 'F', groups: [[{ label: 'A', accel: 'Ctrl+O',"
     " groups: [[{ label: 'B', onSelect(){} }]] }]] }] }",
     "cannot have an accelerator"),
    ("an unbindable accel",
     "{ scope: document.body, menus: [{ label: 'F', groups: [[{ label: 'A',"
     " accel: 'Ctrl+Squiggle', onSelect(){} }]] }] }",
     "cannot bind"),
)


def check_menu_refusals(page) -> list[str]:
    """Every shape that would ship a row doing nothing is a throw naming the row."""
    menu_fixture(page)
    menu_reset(page)
    bad = []
    for name, spec, needle in REFUSALS:
        message = page.evaluate(
            "(async () => {"
            " const { menuBar } = await import('/src/widgets.mjs');"
            f" try {{ menuBar({spec}); return null; }}"
            " catch (error) { return error.message; } })()",
            await_promise=True,
        )
        if message is None:
            bad.append(f"menu refusals: {name} was accepted")
        elif needle not in message:
            bad.append(f"menu refusals: {name} said {message!r}, wanted {needle!r}")
    # The negative control: a spec with all of those shapes avoided has to be accepted, or
    # this check would pass on a `menuBar` that refuses everything.
    ok = page.evaluate(
        "(async () => {"
        " const { menuBar } = await import('/src/widgets.mjs');"
        " try { const bar = menuBar({ scope: document.body, menus: [{ label: '&File',"
        "   groups: [[{ label: '&Open', accel: 'Ctrl+O', onSelect() {} },"
        "             { label: 'Dim', disabled: true }],"
        "            [{ label: 'More', groups: [[{ label: 'In', onSelect() {} }]] }]] }] });"
        "   return bar.querySelectorAll('.menu-item').length; }"
        " catch (error) { return error.message; } })()",
        await_promise=True,
    )
    if ok != 4:
        bad.append(f"menu refusals: a usable spec was refused or mis-built — {ok!r}")
    return bad


#: How far past the edge the fixture is parked, in pixels.
#:
#: Small, and both guards in `check_menu_edges` depend on it being small: the popup has to
#: overflow the taskbar's top while still fitting under the *viewport* bottom, or a bound
#: taken from the viewport flips it anyway and the taskbar half of the rule is asserted by
#: nothing. The gap between the two is the taskbar's own height, 30px.
MENU_OVERHANG = 12

#: What a popup measures, and where the title sits inside its window.
#:
#: Read so that `check_menu_edges` can compute where to park rather than carry a constant.
#: It used to park at a fixed inset from the desktop's right edge, and that number silently
#: depended on how wide the resize checks had left the fixture window: the same inset
#: overflowed the viewport when the group was run alone and fitted inside it in a full run.
#: A fixture positioned by arithmetic over its own measurements cannot drift that way.
MENU_METRICS = """
(() => {
  const bar = document.querySelector('BARID');
  const win = window.__menuWin;
  const dismiss = () => document.querySelector('.desktop').dispatchEvent(
    new PointerEvent('pointerdown', { bubbles: true }));
  dismiss();
  const title = bar.querySelector('.menu-title');
  title.click();
  const popup = document.getElementById(title.getAttribute('aria-controls'));
  if (popup.hidden) return { unopened: title.getAttribute('aria-expanded') };
  const pop = popup.getBoundingClientRect();
  const titleBox = title.getBoundingClientRect();
  const winBox = win.el.getBoundingClientRect();
  const html = document.documentElement;
  const taskbar = parseFloat(getComputedStyle(html).getPropertyValue('--taskbar-h'));
  dismiss();
  return {
    popW: Math.round(pop.width), popH: Math.round(pop.height),
    titleW: Math.round(titleBox.width), titleH: Math.round(titleBox.height),
    offLeft: Math.round(titleBox.left - winBox.left),
    offBottom: Math.round(titleBox.bottom - winBox.top),
    clientW: html.clientWidth, clientH: html.clientHeight,
    floor: Math.round(html.clientHeight - taskbar),
  };
})()
""".replace("BARID", MENU_BAR)

#: Park the fixture window at a computed point, open its first menu, and read the flip.
MENU_PARK = """
((x, y, wantSub) => {
  const win = window.__menuWin;
  const bar = document.querySelector('BARID');
  const html = document.documentElement;
  const taskbar = parseFloat(getComputedStyle(html).getPropertyValue('--taskbar-h'));
  win.moveTo(x, y);
  // Closed first. A click on the title of a menu that is already down closes it, and the
  // previous call to this script leaves one open.
  document.querySelector('.desktop').dispatchEvent(
    new PointerEvent('pointerdown', { bubbles: true }));
  const title = bar.querySelector('.menu-title');
  title.click();
  const popup = document.getElementById(title.getAttribute('aria-controls'));
  if (popup.hidden) return { unopened: title.getAttribute('aria-expanded') };
  const pop = popup.getBoundingClientRect();
  const titleBox = title.getBoundingClientRect();
  const out = {
    popRight: Math.round(pop.right), popLeft: Math.round(pop.left),
    popTop: Math.round(pop.top), popBottom: Math.round(pop.bottom),
    titleRight: Math.round(titleBox.right), titleLeft: Math.round(titleBox.left),
    titleTop: Math.round(titleBox.top), titleBottom: Math.round(titleBox.bottom),
    viewportRight: html.clientWidth, floor: Math.round(html.clientHeight - taskbar),
    titleOnScreen: titleBox.right <= html.clientWidth && titleBox.bottom <= html.clientHeight,
    overflowsRight: Math.round(titleBox.left + pop.width) > html.clientWidth,
    overflowsTaskbar: Math.round(titleBox.bottom + pop.height)
      > Math.round(html.clientHeight - taskbar),
    insideViewportBottom: Math.round(titleBox.bottom + pop.height) <= html.clientHeight,
  };
  if (wantSub) {
    const parent = [...popup.querySelectorAll('.menu-item')]
      .find((row) => row.getAttribute('aria-haspopup') === 'true');
    if (parent) {
      parent.click();
      const sub = document.getElementById(parent.getAttribute('aria-controls'))
        .getBoundingClientRect();
      out.subLeft = Math.round(sub.left);
      out.subRight = Math.round(sub.right);
    }
  }
  return out;
})(%d, %d, %s)
""".replace("BARID", MENU_BAR)


def check_menu_edges(page) -> list[str]:
    """A popup against the right edge or the taskbar flips rather than going off."""
    menu_fixture(page)
    menu_reset(page)
    bad = []
    metrics = page.evaluate(MENU_METRICS)
    if metrics.get("unopened") is not None:
        return [f"menu edges: the first title did not open, so nothing could be measured to "
                f"park against (aria-expanded={metrics['unopened']!r})"]
    # The taskbar's strip is what the low case has to land in: overflowing its top while
    # staying under the viewport's bottom. If the popup is taller than the strip there is no
    # such position, and this check cannot ask for the flip it is about.
    strip = metrics["clientH"] - metrics["floor"]
    if MENU_OVERHANG >= strip:
        return bad + [
            f"menu edges: a {MENU_OVERHANG}px overhang cannot sit inside a {strip}px taskbar "
            "strip, so the low fixture would overflow the viewport too and the taskbar half "
            "of the rule would go unasserted"
        ]

    # Right edge: put the title far enough right that the popup opening leftwards from it
    # would leave the viewport, while the title itself stays on screen.
    x = metrics["clientW"] - metrics["popW"] + MENU_OVERHANG - metrics["offLeft"]
    right = page.evaluate(MENU_PARK % (x, 40, "true"))
    if right.get("unopened") is not None:
        return bad + [f"menu edges: the right fixture's menu did not open "
                      f"(aria-expanded={right['unopened']!r})"]
    if not right["titleOnScreen"]:
        return bad + [f"menu edges: the right fixture put the menu bar off the desktop, so "
                      f"nothing below measures a flip — {right}"]
    if not right["overflowsRight"]:
        return bad + [f"menu edges: the right fixture leaves room for the popup where it "
                      f"normally goes, so no flip is being asked for — {right}"]
    if right["popRight"] > right["viewportRight"]:
        bad.append(
            f"menu edges: against the right edge the popup ends at {right['popRight']} on a "
            f"{right['viewportRight']}px viewport"
        )
    # Flipped, not merely clamped: its right edge lands on its title's, which is what keeps a
    # menu under the thing it belongs to.
    if right["popRight"] != right["titleRight"]:
        bad.append(
            f"menu edges: the flipped popup's right edge is {right['popRight']}, its title's is "
            f"{right['titleRight']}"
        )
    if "subLeft" not in right:
        bad.append("menu edges: the fixture's File menu has no submenu to flip")
    elif right["subRight"] > right["viewportRight"]:
        bad.append(
            f"menu edges: the submenu ends at {right['subRight']} on a "
            f"{right['viewportRight']}px viewport, so it did not flip to the other side"
        )
    elif right["subLeft"] >= right["popLeft"]:
        bad.append(
            f"menu edges: the submenu's left edge is {right['subLeft']} against its popup's "
            f"{right['popLeft']}, so it is still opening rightwards"
        )

    # The foot of the desktop. The title's bottom is placed so the popup under it would cross
    # the taskbar's top by `MENU_OVERHANG` and no more, which keeps it inside the viewport —
    # otherwise a bound taken from the viewport flips it anyway and the taskbar half of the
    # rule is asserted by nothing. Both are read back below.
    y = metrics["floor"] - metrics["popH"] + MENU_OVERHANG - metrics["offBottom"]
    low = page.evaluate(MENU_PARK % (40, y, "false"))
    if low.get("unopened") is not None:
        return bad + [f"menu edges: the low fixture's menu did not open "
                      f"(aria-expanded={low['unopened']!r})"]
    if not low["titleOnScreen"]:
        return bad + [f"menu edges: the low fixture put the menu bar off the desktop — {low}"]
    if not low["overflowsTaskbar"]:
        return bad + [f"menu edges: the low fixture leaves room below the bar — {low}"]
    if not low["insideViewportBottom"]:
        return bad + [
            f"menu edges: unflipped, the low fixture's popup would leave the viewport as well "
            f"as the taskbar — {low}. A viewport bound alone would then flip it, and the "
            "taskbar is what this case is about"
        ]
    if low["popBottom"] > low["floor"]:
        bad.append(
            f"menu edges: at the foot of the desktop the popup ends at {low['popBottom']}, "
            f"under the taskbar, whose top is {low['floor']}"
        )
    # Flipped up: its foot lands on its title's head.
    if low["popBottom"] != low["titleTop"]:
        bad.append(
            f"menu edges: the popup flipped up to {low['popBottom']} rather than onto its "
            f"title's head at {low['titleTop']}"
        )
    # Put back, or every check after this one measures a window in a corner.
    page.evaluate("(() => { window.__menuWin.moveTo(200, 40); return 1; })()")
    return bad


def check_menu_handed_back(page) -> list[str]:
    """The Read me window the menu fixture took over is handed back as it was found.

    A check of its own rather than a `finally` in the last menu check, because the loop in
    `main` catches a raise per check and carries on: a teardown that hangs off one check is
    skipped by exactly the failure that makes it necessary.

    What there is to put back: `readme` is `singleton: true`, so the fixture's
    `launch('readme')` does not open a window of its own — it takes over the shared Read me,
    sizes it to 120px and replaces its content with a menu bar and an empty textarea. Four
    later checks drive that window, and `check_cascade_across_close` reads its geometry.

    Builds the fixture first, which looks redundant after eight checks that already have and
    is not. Run alone — `--only "menu bar handed back"`, which is how a new check is shown to
    fail here — there is no fixture, so every assertion below passes on an absence: nothing
    to close, no globals to find, no bar in the document. Measured: the control for the
    orphaned-listener defect reported `[  ok]` with the defect restored, because the listener
    was never installed.
    """
    bad = []
    menu_fixture(page)
    page.evaluate(
        "(() => { const win = window.__menuWin;"
        " if (window.__menuKeyWatch) {"
        "   document.removeEventListener('keydown', window.__menuKeyWatch);"
        "   delete window.__menuKeyWatch; }"
        " delete window.__menuWin; delete window.__menuLog; delete window.__menuKeys;"
        " if (win && !win.closed) win.close(); return true; })()"
    )
    time.sleep(SETTLE_S + 0.3)
    launch(page, "readme")
    time.sleep(0.3)
    state = page.evaluate(
        "(() => { const wins = window.webxp.windows.filter((w) => w.programId === 'readme');"
        " return { count: wins.length, bars: document.querySelectorAll('%s').length,"
        "   content: wins.length === 1"
        "     ? [...wins[0].bodyEl.children].map((n) => n.className || n.tagName) : null };"
        " })()" % MENU_BAR
    )
    if state["count"] != 1:
        bad.append(
            f"menu bar: {state['count']} Read me windows after the fixture was torn down, want "
            "1 — the checks after this one expect to find the window they found before it"
        )
    if state["bars"]:
        bad.append(
            f"menu bar: {state['bars']} fixture bars still in the document after teardown"
        )
    if state["content"] and any("menu-bar" in str(name) for name in state["content"]):
        bad.append(
            f"menu bar: the Read me window still holds {state['content']} — the fixture's "
            "content was not replaced by the app's own"
        )

    # The fixture's keydown recorder is detached, not merely orphaned, and a keypress after
    # the teardown says nothing to the console.
    #
    # This is the check for a defect this teardown shipped: the recorder was an anonymous
    # arrow function on `document`, so `removeEventListener` had nothing to pass, and the
    # teardown deleted the array it pushed into. Every keypress in the checks after the menu
    # group then threw `Cannot read properties of undefined (reading 'push')`, and the only
    # thing that noticed was the end-of-run console sweep — failures attributed to no check
    # at all. The assertion is the keypress, not the absence of a global, because a
    # listener that writes somewhere harmless is still a listener nothing can remove.
    leftovers = page.evaluate(
        "['__menuKeyWatch', '__menuWin', '__menuLog', '__menuKeys']"
        ".filter((name) => name in window)"
    )
    if leftovers:
        bad.append(
            f"menu bar: {leftovers} are still on `window` after the teardown — a recorder "
            "that cannot be detached outlives the fixture it was built for"
        )
    # `drain` and not a bare read: `consume_events` is destructive, so reading the buffer
    # here without keeping it would discard whatever the checks before this one raised.
    #
    # Tab, and the choice is no longer load-bearing: any key reaches a listener on
    # `document`. It is Tab because the alternative once wedged every check after this
    # one, and the cause was not the shell — it is headless Chrome hiding the page on a key
    # the renderer reports unhandled, which stops the frame clock. `main` suppresses that for
    # the whole run with `Emulation.setFocusEmulationEnabled` and `check_stray_keypress`
    # asserts it there; nothing about it is a property of `src/desktop.mjs`.
    drain(page)
    press_key(page, "Tab")
    noise = console_errors(page)
    if noise:
        bad.append(
            f"menu bar: a keypress after the fixture was torn down said {noise} to the "
            "console — something the fixture attached is still running against state it no "
            "longer has"
        )
    return bad


# ---------------------------------------------------------------------------
# Context menus. `src/contextmenu.mjs` is the host; `src/desktop.mjs` holds the two
# definitions. Right-click used to reach Chrome's own menu everywhere in the shell.
#
# The keys, the right-click and the settled left click all come from the shared input
# helpers above. This block declared a `MENU_KEYS` of its own, and so did the menu bar's —
# same name, one holding `(code, text)` pairs and one holding bare ints, so whichever was
# defined second broke the other's caller. One table now, in the pair shape.
# ---------------------------------------------------------------------------

#: Every row the desktop's menu offers, in the order it draws them, submenu included.
#: Spelled out rather than read off the page: a check that asks the menu what it contains
#: cannot catch the menu losing a row.
CONTEXT_DESKTOP_ROWS = ("Arrange Icons By", "Name", "Program order", "Refresh", "Properties")

#: Every row the window menu offers, on both surfaces XP gave it to.
CONTEXT_WINDOW_ROWS = ("Restore", "Minimize", "Maximize", "Close")

#: Which rows are refused in each window state, from `Desktop.windowMenu`. Written out and
#: not derived: a table computed from the code under test asserts nothing about it.
CONTEXT_WINDOW_DISABLED = {
    "normal": {"Restore"},
    "max": {"Maximize"},
    "min": {"Minimize", "Maximize"},
}

#: Every malformed menu `contextmenu.mjs` refuses, and the words its message has to carry.
#:
#: Each line is a program that would otherwise ship a row doing nothing, a one-of-N group with
#: two bullets or none, a submenu nested past what a 220px window can show, or an accelerator
#: column nothing binds. The expected text is the specific clause and not just a verb, so a
#: message that stops naming the item fails here.
CONTEXT_REFUSALS = (
    ("{label: 'x', groups: [[{label: 'A', accel: 'Ctrl+O', onSelect(){}}]]}",
     'x > A carries an unknown key "accel"'),
    ("{label: 'x', groups: [[{label: 'A'}]]}",
     "x > A has no onSelect and no submenu"),
    ("{label: 'x', groups: [[{label: 'A', selected: true, onSelect(){}},"
     " {label: 'B', onSelect(){}}]]}",
     "a group in x mixes one-of-N rows with plain ones"),
    ("{label: 'x', groups: [[{label: 'A', selected: true, onSelect(){}},"
     " {label: 'B', selected: true, onSelect(){}}]]}",
     "a one-of-N group in x has 2 rows marked selected"),
    ("{label: 'x', groups: [[{label: 'A', selected: false, onSelect(){}}]]}",
     "a one-of-N group in x has 0 rows marked selected"),
    ("{label: 'x', groups: []}", "x has no groups"),
    ("{label: 'x', groups: [[]]}", "x has an empty group"),
    ("{label: '', groups: [[{label: 'A', onSelect(){}}]]}", "a menu needs a label"),
    ("{label: 'x', groups: [[{label: '', onSelect(){}}]]}", "an item in x has no label"),
    ("{label: 'x', groups: [[{label: 'A', groups: [[{label: 'B', groups:"
     " [[{label: 'C', onSelect(){}}]]}]]}]]}", "x > A > B is a submenu 2 deep"),
    ("{label: 'x', groups: [[{label: 'A', onSelect: 1}]]}",
     "x > A has an onSelect that is not a function"),
    ("{label: 'x', groups: [[{label: 'A', selected: true, groups:"
     " [[{label: 'B', onSelect(){}}]]}]]}",
     "x > A is both a submenu and a one-of-N row"),
    # Two sibling rows on one letter: the typeahead takes the first, so the second underlines
    # a letter that does nothing. Shipped for one pass as `&Refresh` beside `P&roperties`.
    #
    # `src/widgets.mjs`'s `menuBar` does not refuse this shape — measured, a bar with
    # `&Refresh` beside `&Rename` builds both rows and throws nothing. That is one fact with
    # two renderings and it is recorded as a finding rather than asserted here, because an
    # assertion on `menuBar` would fail and a failing check is not coverage.
    ("{label: 'x', groups: [[{label: '&Refresh', onSelect(){}}],"
     " [{label: 'P&roperties', onSelect(){}}]]}",
     'both answer the mnemonic "r"'),
)

#: A well-formed menu of the same shape, for the control in `check_context_refusals`. Without
#: it "no console error" is a constant rather than a reading: a drain that never sees anything
#: cannot tell a refusal that fired from a refusal that was deleted.
CONTEXT_WELL_FORMED = ("{label: 'Fine', groups: [[{label: '&A', onSelect(){}}],"
                       " [{label: '&B', selected: true, onSelect(){}},"
                       "  {label: '&C', selected: false, onSelect(){}}]]}")


#: The open context menu, its rows, and where each row can be pressed.
#:
#: `hitsSelf` is the reading a DOM query cannot give: a menu that is in the document, named,
#: and painted behind the taskbar is a menu nobody can use.
CONTEXT_STATE = """(() => {
  const popup = document.querySelector('.context-menu');
  const live = document.getElementById('webxp-live');
  const active = document.activeElement;
  const state = {
    open: !!popup,
    isOpen: window.webxp.contextMenus.isOpen,
    popups: document.querySelectorAll('.context-menu').length,
    live: live ? live.textContent : null,
    icons: [...document.querySelectorAll('.desktop-icon span')].map((s) => s.textContent),
    // What the strip is supposed to hold, from the registry it is derived from. Sortedness
    // alone is vacuous on an empty strip — `[] === sorted([])` — so three assertions about
    // arranging icons passed on a desktop with no icons on it.
    onDesktop: [...window.webxp.programs.values()]
      .filter((program) => program.onDesktop).map((program) => program.name),
    active: active ? (active.className || active.tagName) : null,
    activeRow: (active && active.classList && active.classList.contains('menu-item'))
      ? active.querySelector('.menu-label').textContent : null,
  };
  if (!popup) return state;
  const rect = popup.getBoundingClientRect();
  const style = getComputedStyle(popup);
  state.label = popup.getAttribute('aria-label');
  state.role = popup.getAttribute('role');
  state.z = parseInt(style.zIndex, 10);
  state.shadow = style.boxShadow;
  state.box = {x: rect.left, y: rect.top, w: rect.width, h: rect.height,
               right: rect.right, bottom: rect.bottom};
  // `:scope >` and not a descendant sweep: a submenu's groups are inside this popup, so
  // the loose selector counts them and the number stops being "how many separators does this
  // menu draw". Measured at 4 for a three-group menu with one submenu.
  state.groups = popup.querySelectorAll(':scope > .menu-group[role="group"]').length;
  state.rows = [...popup.querySelectorAll('.menu-item')].map((button) => {
    const box = button.getBoundingClientRect();
    const x = box.left + 20;
    const y = box.top + box.height / 2;
    const hit = document.elementFromPoint(x, y);
    const mark = button.querySelector('.menu-label u');
    return {
      text: button.querySelector('.menu-label').textContent,
      role: button.getAttribute('role'),
      checked: button.getAttribute('aria-checked'),
      disabled: button.getAttribute('aria-disabled') === 'true',
      expanded: button.getAttribute('aria-expanded'),
      tabindex: button.tabIndex,
      mnemonic: mark ? mark.textContent : null,
      bullet: getComputedStyle(button.querySelector('.menu-check'), '::before').content,
      visible: !!button.getClientRects().length,
      x, y,
      hitsSelf: !!(hit && hit.closest('.menu-item') === button),
    };
  });
  return state;
})()"""


def context_state(page) -> dict:
    state = page.evaluate(CONTEXT_STATE)
    if not isinstance(state, dict) or "open" not in state:
        raise HarnessError(f"context menu: the page answered {state!r}, not a menu state")
    return state


def icons_missing(state: dict) -> str | None:
    """Why the desktop strip is not the registry's `onDesktop` programs, or None if it is.

    Belongs beside every assertion about icon order, because order alone is vacuous on an
    empty strip: `[] == sorted([])`. `Desktop.renderDesktopIcons` ends in
    `iconsEl.replaceChildren(...rows)` (`src/desktop.mjs:789`), so a regression in its
    `program.onDesktop` filter empties the strip — and three assertions about arranging
    icons then reported ok about a desktop with no icons on it.
    """
    if sorted(state["icons"]) != sorted(state["onDesktop"]):
        return (
            f"the desktop shows {state['icons']}, want the {len(state['onDesktop'])} "
            f"programs the registry flags onDesktop: {state['onDesktop']}"
        )
    if len(state["icons"]) < 2:
        return f"{len(state['icons'])} desktop icon(s), so nothing here measures an order"
    return None


def context_row(state: dict, text: str) -> dict:
    """One row by its label. A missing row is a failure here, never a silent skip."""
    for row in state.get("rows") or []:
        if row["text"] == text:
            return row
    raise HarnessError(
        f"context menu: no {text!r} row among "
        f"{[row['text'] for row in state.get('rows') or []]} — every assertion below about "
        "it would pass on its absence"
    )


def bare_desktop(page) -> tuple[float, float]:
    """A point on the wallpaper that no window is over, proved before it is pressed.

    The suite leaves one window per program open and cascaded into the upper left, so a
    computed point can land on a window — where a right-click is *correctly* declined, which
    would read as the desktop menu failing to open. That is the harness's problem and it says
    so rather than reporting a product verdict.
    """
    surface = probe(page)["surface"]
    x = surface["x"] + surface["w"] - 240
    y = surface["y"] + surface["h"] - 160
    on_top = page.evaluate(
        "(() => { const e = document.elementFromPoint(%f, %f);"
        " return e ? (e.className || e.tagName) : 'nothing'; })()" % (x, y)
    )
    if "desktop" not in str(on_top):
        raise HarnessError(
            f"context menu: {on_top!r} is at ({x:.0f}, {y:.0f}), not bare desktop — a "
            "right-click there is declined by design, so this is a harness problem and not "
            "the shell's"
        )
    return x, y


def open_desktop_menu(page) -> dict:
    """Right-click bare desktop and hand back the menu, or fail saying it did not open."""
    x, y = bare_desktop(page)
    right_click(page, x, y)
    state = context_state(page)
    if not state["open"]:
        raise HarnessError(
            f"context menu: a right-click at ({x:.0f}, {y:.0f}) on bare desktop opened no "
            "menu, so nothing below it is measured"
        )
    state["pressedAt"] = (x, y)
    return state


#: A `contextmenu` recorder on the document, installed once.
CONTEXT_WATCH = """
(() => { window.__ctxPrevented = null;
  if (!window.__ctxWatch) { window.__ctxWatch = true;
    document.addEventListener('contextmenu', (e) => {
      window.__ctxPrevented = e.defaultPrevented; }); } return true; })()
"""


def check_context_desktop(page) -> list[str]:
    """A right-click on the wallpaper raises the desktop's menu instead of Chrome's.

    The suppression is half the point, so it is measured and not inferred: a `contextmenu`
    listener on `document` records `defaultPrevented`, which is the only reading that
    separates "the shell's menu is up" from "the shell's menu is up under the browser's".
    """
    bad = []
    watch_live(page)
    try:
        page.evaluate(CONTEXT_WATCH)
        state = open_desktop_menu(page)
        x, y = state["pressedAt"]

        if not page.evaluate("window.__ctxPrevented"):
            bad.append(
                "context menu: the right-click was not prevented, so Chrome's own menu is "
                "still over the shell's"
            )
        if not state["isOpen"]:
            bad.append("context menu: a popup is in the document but the host says isOpen false")
        if state["popups"] != 1:
            bad.append(f"context menu: {state['popups']} menus are up, want exactly 1")
        if state["role"] != "menu" or state["label"] != "Desktop":
            bad.append(
                f"context menu: the popup is role={state['role']!r} "
                f"aria-label={state['label']!r}, want 'menu' / 'Desktop'"
            )
        # Placed at the pointer, to the pixel. This is the number win32.run gets wrong by
        # declaring the menu's size — `CMFSItem.js:13-14` — instead of measuring it.
        if not near(state["box"]["x"], x, 1) or not near(state["box"]["y"], y, 1):
            bad.append(
                f"context menu: opened at {state['box']['x']:.0f},{state['box']['y']:.0f} "
                f"for a press at {x:.0f},{y:.0f}"
            )
        taskbar_z = page.evaluate(
            "parseInt(getComputedStyle(document.querySelector('.taskbar')).zIndex, 10)")
        if not isinstance(state["z"], int) or state["z"] <= taskbar_z:
            bad.append(
                f"context menu: z-index {state['z']!r} against the taskbar's {taskbar_z} — a "
                "menu under the taskbar is a menu nobody can press"
            )
        # The Luna bevel, from `menu.css`. An earlier arrangement made the popup itself the
        # focus target, and `theme.css`'s `[tabindex]:focus-visible` box-shadow then replaced
        # the whole bevel with a navy halo. Measured, which is why this line exists.
        if "inset" not in str(state["shadow"]):
            bad.append(
                f"context menu: the popup's box-shadow is {state['shadow']!r} and has no "
                "inset edge — something has outranked `.menu-popup`'s bevel"
            )

        got = tuple(row["text"] for row in state["rows"])
        if got != CONTEXT_DESKTOP_ROWS:
            bad.append(f"context menu: rows {got}, want {CONTEXT_DESKTOP_ROWS}")
        if state["groups"] != 3:
            bad.append(
                f"context menu: {state['groups']} groups, want 3 — the grouping is the "
                "separator, so a lost group is a lost rule"
            )
        for row in state["rows"]:
            if row["tabindex"] != -1:
                bad.append(
                    f"context menu: {row['text']!r} has tabindex {row['tabindex']}, want -1 "
                    "so the menu never becomes a tab stop of its own"
                )
            if not row["mnemonic"]:
                bad.append(f"context menu: {row['text']!r} draws no underlined mnemonic")
        # One letter per row, per list. A submenu is its own list, so the top-level rows and
        # the submenu's are counted apart — which is why `&Properties` can sit beside
        # `&Program order`.
        for which, names in (("the desktop menu", CONTEXT_DESKTOP_ROWS[:1]
                              + CONTEXT_DESKTOP_ROWS[3:]),
                             ("Arrange Icons By", CONTEXT_DESKTOP_ROWS[1:3])):
            letters = [str(context_row(state, name)["mnemonic"]).lower() for name in names]
            if len(set(letters)) != len(letters):
                bad.append(
                    f"context menu: {which} answers {letters} — two rows on one letter means "
                    "the second underlines a letter the typeahead never reaches"
                )
        # Focus is in the menu on open, or Escape and the arrows have nowhere to start from.
        if state["activeRow"] != CONTEXT_DESKTOP_ROWS[0]:
            bad.append(
                f"context menu: focus is on {state['active']!r} after opening, want the "
                f"first row {CONTEXT_DESKTOP_ROWS[0]!r}"
            )
        for name in ("Arrange Icons By", "Refresh", "Properties"):
            row = context_row(state, name)
            if not row["visible"] or not row["hitsSelf"]:
                bad.append(
                    f"context menu: {name!r} is visible={row['visible']} and answers a press "
                    f"at ({row['x']:.0f}, {row['y']:.0f}) with hitsSelf={row['hitsSelf']}"
                )

        # Refresh does something, and says so. A row that renders and does nothing is the one
        # thing these menus refuse to ship.
        if not page.evaluate(
            "(() => { window.__ctxIcon = document.querySelector('.desktop-icon');"
            " return !!window.__ctxIcon; })()"
        ):
            raise HarnessError("context menu: no desktop icon to watch across a Refresh")
        row = context_row(state, "Refresh")
        click_at(page, row["x"], row["y"])
        after = context_state(page)
        if after["open"]:
            bad.append("context menu: pressing Refresh left the menu on screen")
        spoken = said(page)
        if "Desktop refreshed" not in spoken:
            bad.append(f"context menu: Refresh announced {spoken!r}, want 'Desktop refreshed'")
        thin = icons_missing(after)
        if thin:
            bad.append(f"context menu: after Refresh, {thin}")
        # A different node, and a node at all. `__ctxIcon !== null` is true, so a Refresh
        # that emptied the strip read as a Refresh that rebuilt it.
        if not page.evaluate(
            "(() => { const now = document.querySelector('.desktop-icon');"
            " return Boolean(now) && now !== window.__ctxIcon; })()"
        ):
            bad.append(
                "context menu: Refresh left the same icon nodes in place, or left none at "
                "all, so it recomputed nothing from the registry the icons are derived from"
            )

        # The submenu, reached and used by the pointer that opened it.
        #
        # This is the regression the hover rule was written for, and it is the whole reason
        # this block exists. The obvious close test — "any row that is not the one the submenu
        # hangs off" — is true of every row *inside* the submenu, so the submenu closed under
        # the pointer on its way in and the press that followed landed on the wallpaper
        # behind. Measured, before `current.rows.includes(row)`: hovering 'Arrange Icons By'
        # then pressing 'Name' left the icons in their old order, the live region empty, and
        # the menu gone.
        state = open_desktop_menu(page)
        parent = context_row(state, "Arrange Icons By")
        hover_at(page, parent["x"], parent["y"])
        time.sleep(0.2)
        state = context_state(page)
        if context_row(state, "Arrange Icons By")["expanded"] != "true":
            bad.append("context menu: the pointer crossing 'Arrange Icons By' opened nothing")
        row = context_row(state, "Name")
        if not row["visible"] or not row["hitsSelf"]:
            bad.append(
                "context menu: hovering 'Arrange Icons By' left 'Name' visible="
                f"{row['visible']} hitsSelf={row['hitsSelf']} — the submenu is not reachable "
                "by the pointer that opened it"
            )
        else:
            click_at(page, row["x"], row["y"])
            after = context_state(page)
            if after["open"]:
                bad.append("context menu: pressing a submenu row left the menu on screen")
            thin = icons_missing(after)
            if thin:
                bad.append(f"context menu: {thin}")
            elif after["icons"] != sorted(after["icons"]):
                bad.append(
                    "context menu: pressing 'Name' with the pointer left the icons as "
                    f"{after['icons']}, not sorted"
                )
            if "arranged by name" not in str(after["live"]):
                bad.append(f"context menu: the pointer path announced {after['live']!r}")
    finally:
        page.evaluate("(() => { delete window.__ctxIcon; return true; })()")
        # Back to the order every other check found the desktop in.
        page.evaluate("window.webxp.arrange('program')")
        stop_watching_live(page)
    return bad


def check_context_keyboard(page) -> list[str]:
    """The menu opens without a pointer, and the arrows, Enter, a mnemonic and Escape all work.

    This is the whole reason the control is written rather than copied: every menu row in
    win32.run is a `div on:click`, so none of its menus has a keyboard route at all.
    """
    bad = []
    watch_live(page)
    try:
        # Shift+F10 from a desktop icon, which is the only focusable thing on the wallpaper.
        on_icon = page.evaluate(
            "(() => { const i = document.querySelector('.desktop-icon');"
            " if (!i) return null; i.focus();"
            " return document.activeElement === i ? i.querySelector('span').textContent"
            "   : null; })()"
        )
        if not on_icon:
            raise HarnessError("context menu: no desktop icon took focus")
        press_key(page, "F10", modifiers=SHIFT)
        state = context_state(page)
        if not state["open"]:
            return ["context menu: Shift+F10 on a desktop icon opened nothing"]
        if state["activeRow"] != CONTEXT_DESKTOP_ROWS[0]:
            bad.append(
                f"context menu: Shift+F10 left focus on {state['active']!r}, want the first row"
            )
        icon_box = page.evaluate(
            "(({left, bottom}) => ({left, bottom}))"
            "(document.querySelector('.desktop-icon').getBoundingClientRect())"
        )
        # Anchored at the box the keyboard is standing on, not at wherever the pointer was
        # last left — which for a keyboard-only visitor is the corner of the screen.
        if not near(state["box"]["x"], icon_box["left"], 2) or not near(
            state["box"]["y"], icon_box["bottom"], 2
        ):
            bad.append(
                f"context menu: Shift+F10 placed the menu at {state['box']['x']:.0f},"
                f"{state['box']['y']:.0f}, want the focused icon's "
                f"{icon_box['left']:.0f},{icon_box['bottom']:.0f}"
            )
        press_key(page, "Escape")
        back = context_state(page)
        if back["open"]:
            bad.append("context menu: Escape left the menu on screen")
        if "desktop-icon" not in str(back["active"]):
            bad.append(
                f"context menu: Escape put focus on {back['active']!r}, want the icon the "
                "menu was opened from"
            )

        # The context-menu key is the other route Windows itself offers.
        page.evaluate("document.querySelector('.desktop-icon').focus()")
        press_key(page, "ContextMenu")
        if not context_state(page)["open"]:
            bad.append("context menu: the ContextMenu key opened nothing")
        press_key(page, "Escape")

        # One-of-N, by keyboard, both ways round — which is also what leaves the desktop in
        # the order the rest of the suite found it in.
        for want, expect_sorted in (("Name", True), ("Program order", False)):
            state = open_desktop_menu(page)
            arrange = context_row(state, "Arrange Icons By")
            if arrange["expanded"] != "false":
                bad.append(
                    "context menu: 'Arrange Icons By' is aria-expanded="
                    f"{arrange['expanded']!r} before it is opened"
                )
            for name in ("Name", "Program order"):
                if context_row(state, name)["visible"]:
                    bad.append(
                        f"context menu: the submenu row {name!r} is on screen while its "
                        "parent row is collapsed"
                    )
            press_key(page, "ArrowRight")
            state = context_state(page)
            arrange = context_row(state, "Arrange Icons By")
            if arrange["expanded"] != "true":
                bad.append(
                    "context menu: ArrowRight opened the submenu with the parent row still "
                    f"aria-expanded={arrange['expanded']!r}"
                )
            if state["activeRow"] != "Name":
                bad.append(
                    f"context menu: ArrowRight left focus on {state['active']!r}, want the "
                    "submenu's first row"
                )
            on = [row["text"] for row in state["rows"] if row["checked"] == "true"]
            if len(on) != 1:
                bad.append(
                    f"context menu: {len(on)} rows report aria-checked=true ({on}) — one-of-N "
                    "means exactly one"
                )
            for name in ("Name", "Program order"):
                got = context_row(state, name)
                if got["role"] != "menuitemradio":
                    bad.append(
                        f"context menu: {name!r} is role={got['role']!r}, want menuitemradio "
                        "— a checkbox announces independent switches where XP had one choice"
                    )
                if got["checked"] == "true" and "•" not in str(got["bullet"]):
                    bad.append(
                        f"context menu: the chosen row {name!r} draws {got['bullet']!r}, want "
                        "a bullet — `menu.css`'s tick is a checkbox's and this is a radio"
                    )
                if got["checked"] != "true" and str(got["bullet"]) not in ("none", '""'):
                    bad.append(
                        f"context menu: the unchosen row {name!r} draws {got['bullet']!r}"
                    )
            # Walk to the row this pass is about, then press Enter.
            for _ in range(len(CONTEXT_DESKTOP_ROWS)):
                if context_state(page)["activeRow"] == want:
                    break
                press_key(page, "ArrowDown")
            if context_state(page)["activeRow"] != want:
                raise HarnessError(
                    f"context menu: the arrows never reached {want!r} in the submenu"
                )
            press_key(page, "Enter")
            time.sleep(0.25)
            state = context_state(page)
            if state["open"]:
                bad.append(f"context menu: Enter on {want!r} left the menu on screen")
            names = state["icons"]
            thin = icons_missing(state)
            if thin:
                bad.append(f"context menu: after {want!r}, {thin}")
            elif expect_sorted and names != sorted(names):
                bad.append(f"context menu: {want!r} left the icons as {names}, not sorted")
            # The announcement, against the key the row carries. `src/desktop.mjs:378`
            # announces `entry.id` and not the row's own label, so the two already disagree
            # for one of the two values: the rows read `&Name` and `&Program order` while the
            # live region says "by name" and "by program". Asserted as the product behaves,
            # and recorded as a finding — one fact with two renderings.
            if expect_sorted and "arranged by name" not in str(state["live"]):
                bad.append(
                    f"context menu: {want!r} announced {state['live']!r} — a state change a "
                    "reader cannot see has to be said out loud"
                )
            if not expect_sorted and "arranged by program" not in str(state["live"]):
                bad.append(f"context menu: {want!r} announced {state['live']!r}")

        # Re-opening reads the order off the desktop rather than off a copy the menu kept.
        state = open_desktop_menu(page)
        press_key(page, "ArrowRight")
        state = context_state(page)
        chosen = [row["text"] for row in state["rows"] if row["checked"] == "true"]
        if chosen != ["Program order"]:
            bad.append(
                f"context menu: the re-opened submenu marks {chosen} as chosen, but the "
                "desktop was last arranged by program order"
            )
        # Escape backs out of the submenu first, and only then closes the menu.
        press_key(page, "Escape")
        state = context_state(page)
        if not state["open"]:
            bad.append("context menu: Escape in a submenu closed the whole menu")
        elif state["activeRow"] != "Arrange Icons By":
            bad.append(
                f"context menu: Escape out of the submenu left focus on {state['active']!r}, "
                "want the row the submenu hangs off"
            )
        elif context_row(state, "Arrange Icons By")["expanded"] != "false":
            bad.append("context menu: the submenu's parent row still reports itself expanded")
        press_key(page, "Escape")
        if context_state(page)["open"]:
            bad.append("context menu: a second Escape left the menu on screen")

        # Home, End, and a bare mnemonic — XP activated a row on its letter with no Enter.
        open_desktop_menu(page)
        press_key(page, "End")
        if context_state(page)["activeRow"] != CONTEXT_DESKTOP_ROWS[-1]:
            bad.append("context menu: End did not reach the last row")
        press_key(page, "Home")
        if context_state(page)["activeRow"] != CONTEXT_DESKTOP_ROWS[0]:
            bad.append("context menu: Home did not reach the first row")
        press_key(page, "r")
        time.sleep(0.25)
        if context_state(page)["open"]:
            bad.append("context menu: the bare mnemonic 'r' left the menu on screen")
        if "Desktop refreshed" not in said(page):
            bad.append(f"context menu: 'r' did not run Refresh — said {said(page)!r}")

        # Tab closes rather than walking out of a floating popup into whatever follows it in
        # the document, which is the root element and not the surface the menu came from.
        open_desktop_menu(page)
        press_key(page, "Tab")
        if context_state(page)["open"]:
            bad.append("context menu: Tab left the menu on screen")
    finally:
        stop_watching_live(page)
    return bad


def check_context_window(page) -> list[str]:
    """The window menu, on a title bar and a taskbar button, with the rows each state allows.

    The negative control is in here rather than beside it: the same right-click inside the
    window's own body must produce no menu *and* must not be prevented, because webxp has no
    clipboard and the browser's Cut, Copy and Paste are the only ones a text field has.
    """
    bad = []
    watch_live(page)
    try:
        page.evaluate(CONTEXT_WATCH)
        expose(page, "readme")
        selector = win_selector(page, "readme")
        bar = rect_of(page, f"{selector} .window-title")
        if bar is None:
            raise HarnessError("context menu: Read me has no laid-out title bar")

        right_click(page, bar["x"] + 60, bar["y"] + bar["h"] / 2)
        state = context_state(page)
        if not state["open"]:
            return ["context menu: a right-click on the title bar opened nothing"]
        if state["label"] != "Read me window":
            bad.append(
                f"context menu: the window menu is named {state['label']!r}, want "
                "'Read me window' — a menu with no name is announced as a list of items"
            )
        got = tuple(row["text"] for row in state["rows"])
        if got != CONTEXT_WINDOW_ROWS:
            bad.append(f"context menu: window rows {got}, want {CONTEXT_WINDOW_ROWS}")
        if state["groups"] != 2:
            bad.append(f"context menu: {state['groups']} groups in the window menu, want 2")
        press_key(page, "Escape")

        # `centre` spelled out rather than called, for the one reason that justifies it:
        # `centre` takes a CSS selector, and every window's button is `.task-button`
        # (`src/desktop.mjs:651`). Nothing in the markup says *which* window's — the one
        # discriminator is `aria-controls`, carrying a generated window id — so singling out
        # the read-me's means going through the model, which is the read below. The other
        # exception is `menu_pick`, which reads its own box to stay inside one window by id.
        task_button = (
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
            " const r = w.taskButton.getBoundingClientRect();"
            " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
        )
        # The disabled table, state by state, on the taskbar button — which is the only
        # surface a minimised window still has.
        for want_state in ("normal", "max", "min"):
            page.evaluate(
                "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
                " const want = %s;"
                " if (want === 'max') { if (w.state === 'min') w.restore(); w.maximize(); }"
                " else if (want === 'min') w.minimize();"
                " else { if (w.state === 'min') w.restore();"
                "        if (w.state === 'max') w.unmaximize(); } return true; })()"
                % json.dumps(want_state)
            )
            time.sleep(SETTLE_S + 0.25)
            reached = page.evaluate(
                "(window.webxp.windows.find((w) => w.programId === 'readme') || {}).state")
            if reached != want_state:
                bad.append(
                    f"context menu: wanted the window {want_state!r} before opening its menu, "
                    f"got {reached!r} — the row table below was not measured in that state"
                )
                continue
            button = page.evaluate(task_button)
            right_click(page, button["x"], button["y"])
            state = context_state(page)
            if not state["open"]:
                bad.append(
                    f"context menu: a right-click on the taskbar button of a {want_state!r} "
                    "window opened nothing"
                )
                continue
            dead = {row["text"] for row in state["rows"] if row["disabled"]}
            if dead != CONTEXT_WINDOW_DISABLED[want_state]:
                bad.append(
                    f"context menu: a {want_state!r} window greys {sorted(dead)}, want "
                    f"{sorted(CONTEXT_WINDOW_DISABLED[want_state])}"
                )
            for row in state["rows"]:
                # Every row stays focusable and announced, greyed or not: the `disabled`
                # attribute would make the arrows step over it and a reader never be told it
                # is there.
                if row["tabindex"] != -1:
                    bad.append(f"context menu: {row['text']!r} has tabindex {row['tabindex']}")
                if not row["hitsSelf"]:
                    bad.append(
                        f"context menu: {row['text']!r} of a {want_state} window cannot be "
                        f"pressed at ({row['x']:.0f}, {row['y']:.0f}) — the taskbar is "
                        "z-index 1000 and this menu has to clear it"
                    )
            # A greyed row refuses activation rather than looking dead and firing anyway.
            for name in sorted(CONTEXT_WINDOW_DISABLED[want_state]):
                row = context_row(state, name)
                click_at(page, row["x"], row["y"])
                after = page.evaluate(
                    "(window.webxp.windows.find((w) => w.programId === 'readme') || {}).state")
                if after != want_state:
                    bad.append(
                        f"context menu: the greyed {name!r} row acted — the window went from "
                        f"{want_state!r} to {after!r}"
                    )
                if not context_state(page)["open"]:
                    bad.append(
                        f"context menu: pressing the greyed {name!r} row closed the menu"
                    )
                    right_click(page, button["x"], button["y"])
            press_key(page, "Escape")

        # Restore, from the taskbar menu of a minimised window: the window comes back and the
        # keyboard comes with it.
        # Restored out of whatever the loop left before being minimised, so the state Restore
        # returns to is the one this block names. Written as `if (max) unmaximize()` first,
        # which is dead: the loop above always ends on 'min', so the test never passed and
        # the window was minimised with 'max' remembered. Measured both ways — Restore hands
        # the keyboard back from either, so this is determinism rather than a defect fixed.
        page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
            " if (w.state === 'min') w.restore();"
            " if (w.state === 'max') w.unmaximize();"
            " w.minimize(); return true; })()"
        )
        time.sleep(SETTLE_S + 0.25)
        button = page.evaluate(task_button)
        right_click(page, button["x"], button["y"])
        row = context_row(context_state(page), "Restore")
        click_at(page, row["x"], row["y"])
        time.sleep(SETTLE_S + 0.4)
        state = probe(page)
        if need(state, "readme")["state"] == "min":
            bad.append("context menu: Restore left the window minimised")
        if state["active"] != "readme":
            bad.append(
                f"context menu: Restore left {state['active']!r} active, want the window it "
                "brought back"
            )
        if not page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'readme');"
            " return w.el.contains(document.activeElement); })()"
        ):
            bad.append("context menu: Restore brought the window back without the keyboard")

        # The negative control: the same gesture inside the window's own body. No menu, and
        # not prevented, so a text field keeps the only Cut, Copy and Paste there are.
        page.evaluate("window.__ctxPrevented = null")
        body = rect_of(page, f"{selector} .window-body")
        if body is None:
            raise HarnessError("context menu: Read me has no laid-out body")
        right_click(page, body["x"] + body["w"] / 2, body["y"] + body["h"] - 20)
        if context_state(page)["open"]:
            bad.append(
                "context menu: a right-click inside the window body opened the shell's menu — "
                "the desktop's binding is supposed to decline anything that is not the "
                "wallpaper or its icons"
            )
        if page.evaluate("window.__ctxPrevented") is not False:
            bad.append(
                "context menu: a right-click in a window body was prevented, which deletes "
                "the browser's Cut, Copy and Paste without replacing them"
            )

        # Close, from the menu, and the window really goes.
        bar = rect_of(page, f"{selector} .window-title")
        right_click(page, bar["x"] + 60, bar["y"] + bar["h"] / 2)
        row = context_row(context_state(page), "Close")
        click_at(page, row["x"], row["y"])
        time.sleep(SETTLE_S + 0.4)
        if window_of(probe(page), "readme") is not None:
            bad.append("context menu: Close left the window in the desktop's list")
        if "Read me closed" not in said(page):
            bad.append(f"context menu: Close announced {said(page)!r}")
    finally:
        stop_watching_live(page)
        # Put back what this check closed, so nothing after it finds a window missing.
        if window_of(probe(page), "readme") is None:
            launch(page, "readme")
    return bad


#: A context menu host on a scratch root, bound to a scratch surface and left for the check.
#:
#: Its own root, not the shell's, so `destroy()` here cannot be confused with the desktop's
#: teardown and the check can be run by itself.
CONTEXT_SCRATCH = """(async () => {
  const module = await import('/src/contextmenu.mjs');
  const root = document.createElement('div');
  root.id = 'ctx-scratch';
  document.body.append(root);
  const surface = document.createElement('div');
  // Over everything, and clear of the desktop's icon strip, so a real right-click lands here
  // and not on whatever the suite has left open.
  surface.style.position = 'fixed';
  surface.style.left = '45%';
  surface.style.top = '40%';
  surface.style.width = '120px';
  surface.style.height = '80px';
  surface.style.zIndex = '3000';
  root.append(surface);
  const host = module.contextMenus({ root });
  host.bind(surface, () => ({ label: 'Scratch',
    groups: [[{ label: '&Only', onSelect: () => { window.__ctxFired = true; } }]] }));
  window.__ctxScratch = { root, host };
  const box = surface.getBoundingClientRect();
  return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
})()"""


def check_context_teardown(page) -> list[str]:
    """`destroy()` makes a host inert, and the bindings outlive it.

    The bindings live on the surfaces — a title bar, a taskbar button, the desktop itself —
    and `destroy` cannot reach them, because the root element belongs to whoever built it.
    Before the host carried a dead flag, a desktop that had already been destroyed still
    answered a right-click with a full menu whose Close and Restore rows called back into it.
    Measured, which is why this check exists: `desktop destroy` covers the resize listener and
    said nothing about this.
    """
    bad = []
    try:
        at = page.evaluate(CONTEXT_SCRATCH, await_promise=True)
        right_click(page, at["x"], at["y"])
        named = page.evaluate(
            "(() => { const p = document.querySelector('#ctx-scratch .context-menu');"
            " return p ? p.getAttribute('aria-label') : null; })()"
        )
        if named != "Scratch":
            return [
                f"context menu: the scratch host put up {named!r} rather than its own menu, so "
                "the teardown below measures nothing"
            ]
        page.evaluate("(() => { window.__ctxScratch.host.destroy(); return true; })()")
        if page.evaluate("!!document.querySelector('#ctx-scratch .context-menu')"):
            bad.append("context menu: destroy() left the open menu on screen")
        right_click(page, at["x"], at["y"])
        if page.evaluate("!!document.querySelector('#ctx-scratch .context-menu')"):
            bad.append(
                "context menu: a destroyed host still answers a right-click, so a replaced "
                "desktop keeps serving a menu whose rows call back into it"
            )
        threw = page.evaluate(
            "(() => { try { window.__ctxScratch.host.bind(document.body, () => null);"
            " return ''; } catch (e) { return e.message; } })()"
        )
        if "destroyed" not in str(threw):
            bad.append(
                f"context menu: bind() on a destroyed host answered {threw!r}, want a throw "
                "naming the host as destroyed"
            )
    finally:
        page.evaluate(
            "(() => { if (window.__ctxScratch) { window.__ctxScratch.host.destroy();"
            " window.__ctxScratch.root.remove(); delete window.__ctxScratch; }"
            " delete window.__ctxFired; return true; })()"
        )
    return bad


def check_context_refusals(page) -> list[str]:
    """Every menu definition that would ship a dead or ambiguous row is refused, by name.

    Driven through a real `make`, because that is where a definition is built: at every open
    rather than once at startup, which is what lets a greyed row be right about the state the
    window is in now.

    The `contextmenu` event here is synthesised, which the rest of this file refuses to do —
    and the reason is narrow. The subject is the validator, not whether a right-click reaches
    the listener; the three checks above drive that with real presses. A real press cannot be
    used anyway, because each case needs a surface of its own and the throw has to arrive from
    inside the listener.

    The throw lands inside an event listener, so it reaches the page as an uncaught exception
    rather than as a rejected call. `drain_expecting` is how the rest of this file reads a
    deliberate error: it requires the message and keeps the end-of-run sweep strict.
    """
    bad = []
    # Clear whatever an earlier check left, so each drain below sees only its own error. Kept
    # rather than discarded — `drain` folds it into the run's failures.
    drain(page)
    build = (
        "(async () => { const m = await import('/src/contextmenu.mjs');"
        " const root = document.createElement('div'); document.body.append(root);"
        " const surface = document.createElement('div'); root.append(surface);"
        " const host = m.contextMenus({ root });"
        " const before = document.querySelectorAll('.context-menu').length;"
        " host.bind(surface, () => (%s));"
        " surface.dispatchEvent(new MouseEvent('contextmenu'));"
        " const after = document.querySelectorAll('.context-menu').length;"
        " host.destroy(); root.remove();"
        " return after - before; })()"
    )
    for spec, wanted in CONTEXT_REFUSALS:
        leaked = page.evaluate(build % spec, await_promise=True)
        # The exception arrives over the websocket, not with the evaluate's reply.
        time.sleep(0.15)
        bad += drain_expecting(page, wanted, "context menu refusal")
        if leaked:
            bad.append(
                f"context menu: refusing {wanted!r} left {leaked} popup(s) in the page — a "
                "half-built menu on screen is worse than none"
            )
    # The control. A well-formed menu of the same shape must put exactly one popup up and say
    # nothing to the console, or "no console error" above is a constant and the thirteen
    # assertions are reading a drain that never sees anything.
    put_up = page.evaluate(build % CONTEXT_WELL_FORMED, await_promise=True)
    time.sleep(0.15)
    noise = console_errors(page)
    if put_up != 1:
        bad.append(
            f"context menu: a well-formed menu put {put_up} popup(s) up, want 1 — the "
            f"{len(CONTEXT_REFUSALS)} refusals above are then measuring a builder that "
            "refuses everything"
        )
    if noise:
        bad.append(f"context menu: a well-formed menu reported {noise}")
    return bad


#: A scratch popup with one checkbox row and one radio row, built by hand in `menu.css`'s own
#: DOM contract.
#:
#: Built rather than found, because the two glyphs are a tie between two sheets and neither
#: control reliably has both roles on screen at once: `widgets.menuBar` renders only
#: `menuitemcheckbox` and `contextmenu.mjs` only `menuitemradio`.
CONTEXT_GLYPHS = """(() => {
  const popup = document.createElement('div');
  popup.className = 'menu-popup context-menu';
  popup.setAttribute('role', 'menu');
  popup.style.left = '0px';
  popup.style.top = '0px';
  const group = document.createElement('div');
  group.className = 'menu-group';
  group.setAttribute('role', 'group');
  const out = {};
  for (const role of ['menuitemcheckbox', 'menuitemradio']) {
    for (const on of ['true', 'false']) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'menu-item';
      button.setAttribute('role', role);
      button.setAttribute('aria-checked', on);
      const gutter = document.createElement('span');
      gutter.className = 'menu-check';
      const label = document.createElement('span');
      label.className = 'menu-label';
      label.textContent = role + ' ' + on;
      button.append(gutter, label);
      group.append(button);
      out[role + ':' + on] = button;
    }
  }
  popup.append(group);
  document.body.append(popup);
  try {
    const read = {};
    for (const [key, button] of Object.entries(out)) {
      read[key] = getComputedStyle(button.querySelector('.menu-check'), '::before').content;
    }
    read.gutter = getComputedStyle(popup).getPropertyValue('--menu-gutter').trim();
    return read;
  } finally {
    popup.remove();
  }
})()"""


def check_context_glyphs(page, *, invert: bool = False) -> list[str]:
    """A tick means a switch that is on; a bullet means the one of several that is chosen.

    `menu.css`'s `.menu-item[aria-checked="true"] .menu-check::before` matches a radio row
    too, because `aria-checked` is the attribute for both roles. `src/contextmenu.css`
    overrides it one class higher, so the decision is won on count and not on which sheet is
    linked last. `css_ties.py` pins that same decision — `OUTRANKS`, (0,4,1) over (0,3,1) —
    by reading the sheets; this reads the glyph a reader sees out of computed style in a live
    page, which is the half no static count can answer: a selector that outranks another
    still draws nothing if the rule it wins is itself overridden at runtime.

    Invertible, and cheaply so: it builds its own popup, reads four computed styles and
    removes it again, so the inverted run costs one page round trip and changes no state.
    """
    glyphs = page.evaluate(CONTEXT_GLYPHS)
    if not isinstance(glyphs, dict) or "gutter" not in glyphs:
        raise HarnessError(f"context menu: the glyph probe answered {glyphs!r}")
    bad = []
    if invert:
        # --prove-failure. The two glyphs are there; asserting that they are not must fail.
        if "✓" in str(glyphs["menuitemcheckbox:true"]) and "•" in str(
            glyphs["menuitemradio:true"]
        ):
            bad.append(
                "context menu glyphs[--prove-failure]: the checkbox row draws "
                f"{glyphs['menuitemcheckbox:true']!r} and the radio row "
                f"{glyphs['menuitemradio:true']!r} as they should — the inverted assertion "
                "failed, so this check can fail"
            )
        return bad
    if "✓" not in str(glyphs["menuitemcheckbox:true"]):
        bad.append(
            f"context menu: a checked checkbox row draws {glyphs['menuitemcheckbox:true']!r}, "
            "want a tick — `contextmenu.css`'s radio rule has reached a checkbox"
        )
    if "•" not in str(glyphs["menuitemradio:true"]):
        bad.append(
            f"context menu: a chosen radio row draws {glyphs['menuitemradio:true']!r}, want a "
            "bullet"
        )
    for key in ("menuitemcheckbox:false", "menuitemradio:false"):
        if str(glyphs[key]) not in ("none", '""'):
            bad.append(f"context menu: an unchecked {key} row draws {glyphs[key]!r}")
    # The gutter the two glyphs are centred in is `menu.css`'s one declaration of that number,
    # and a context menu reading a different one would be a second copy of it.
    if glyphs["gutter"] != "20px":
        bad.append(
            f"context menu: --menu-gutter reads {glyphs['gutter']!r} on a context menu popup, "
            "want menu.css's 20px"
        )
    return bad


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


#: A file in the repository mounted at `/usr/src/webxp` whose extension nothing opens. Real
#: rather than invented: a reader can reach it from the desktop, and a `.gz` is compressed
#: bytes, so Notepad showing them would be the wrong answer loudly instead of quietly.
UNOPENABLE = "/usr/src/webxp/apps/day-02-lines/fixture/activities/202610200000.gpx.gz"


def check_file_associations(page, *, invert: bool = False) -> list[str]:
    """Extension to program: the table, the resolver, and a type with nothing to open it.

    That `.txt` resolves to Notepad is the cheap half. The two halves that cost something:
    an id in the table the desktop never registered is a double-click that does nothing,
    because `Desktop.launch` returns `null` for a program it does not have; and an associated
    program that is a singleton cannot be handed a path at all, which is the trap `webview`
    sets for `.html`. Both are read off the live registry rather than off the manifest, so an
    app that failed to load counts as missing.

    `danglingAssociations([])` is asserted non-empty too. Without that line the dangling
    assertion reads the same when the detector is broken as when the table is right.

    Invertible: one page round trip, no input and no state, so the control costs nothing to
    re-run on every `--prove-failure`.
    """
    result = page.evaluate(
        "(async () => {"
        " const api = await import('/src/apps.mjs');"
        " const ids = [...window.webxp.programs.keys()];"
        " let frozen = 'mutable';"
        " try { api.programsFor('/a.txt').push('x'); } catch (error) { frozen = error.name; }"
        " return {"
        "   table: api.FILE_PROGRAMS,"
        "   txt: [...api.programsFor('/home/user/readme.txt')],"
        "   upper: [...api.programsFor('/home/user/README.TXT')],"
        "   noext: [...api.programsFor('/usr/src/webxp/LICENSE')],"
        "   dotfile: api.extensionOf('/home/user/.bashrc'),"
        "   double: api.extensionOf('/a/b.gpx.gz'),"
        "   miss: [...api.programsFor(MISS)],"
        "   why: api.noProgramFor(MISS),"
        "   dangling: api.danglingAssociations(ids),"
        "   detector: api.danglingAssociations([]).length,"
        "   singletons: ids.filter((id) => (window.webxp.programs.get(id) || {}).singleton),"
        "   frozen,"
        " }; })()".replace("MISS", json.dumps(UNOPENABLE)),
        # Without this the IIFE's promise comes back by value as `{}` and every key below
        # reads missing. It did, first try, and the harness reported a KeyError rather than
        # a pass.
        await_promise=True,
    )
    bad = []
    if invert:
        # --prove-failure. The resolver does answer, so asserting that it does not must
        # fail. The two readings are the ends of the table's job — a type with a program,
        # and a type with none.
        if result["txt"] == ["notepad"] and result["miss"] == []:
            bad.append(
                "file associations[--prove-failure]: .txt resolved to "
                f"{result['txt']} and a .gz to {result['miss']} as they should — the "
                "inverted assertion failed, so this check can fail"
            )
        return bad
    table = result["table"]
    if not table:
        return ["file associations: FILE_PROGRAMS is empty"]

    # The table's own shape. A key without its dot would never match `extensionOf`, and an
    # empty list is a third way of saying "no program" that no caller tests for.
    for extension, programs in table.items():
        if extension and not extension.startswith("."):
            bad.append(f"file associations: key {extension!r} has no leading dot")
        if extension != extension.lower():
            bad.append(f"file associations: key {extension!r} is not lower-case")
        if not programs:
            bad.append(f"file associations: {extension!r} maps to an empty list, not an absence")

    # Resolution: the default opener, case-insensitively, and a name with no extension.
    if result["txt"] != ["notepad"]:
        bad.append(f"file associations: .txt resolved to {result['txt']}, want ['notepad']")
    if result["upper"] != result["txt"]:
        bad.append(
            f"file associations: README.TXT resolved to {result['upper']} but readme.txt to "
            f"{result['txt']} — the lookup is case-sensitive"
        )
    if result["noext"] != ["notepad"]:
        bad.append(f"file associations: LICENSE resolved to {result['noext']}, want ['notepad']")
    if result["dotfile"] != "":
        bad.append(f"file associations: .bashrc's extension read {result['dotfile']!r}, want ''")
    if result["double"] != ".gz":
        bad.append(f"file associations: b.gpx.gz's extension read {result['double']!r}")

    # The miss, and the sentence that has to come with it.
    if result["miss"] != []:
        bad.append(f"file associations: a .gz resolved to {result['miss']}, want nothing")
    why = result["why"]
    if UNOPENABLE not in why or ".gz" not in why:
        bad.append(
            f"file associations: an unopenable file is explained as {why!r} — it must name the "
            "path and the extension"
        )

    # No dangling id, and the detector that says so can still say no.
    if result["dangling"]:
        bad.append(
            f"file associations: {result['dangling']} name programs the desktop has not "
            "registered — launch returns null and a double-click does nothing"
        )
    if result["detector"] < 1:
        bad.append(
            "file associations: danglingAssociations found nothing against an empty registry, "
            "so its clean answer above measured nothing"
        )

    # A singleton cannot be opened on a path: it is mounted once, and `launch` warns and
    # drops the arguments. `webview` is the live example, and `.html` must not point at it.
    associated = {program for programs in table.values() for program in programs}
    singletons = sorted(associated & set(result["singletons"]))
    if singletons:
        bad.append(
            f"file associations: {singletons} are singletons — a second launch discards the "
            "path, so the file silently does not open"
        )

    if result["frozen"] != "TypeError":
        bad.append(
            f"file associations: pushing into a resolved list was {result['frozen']} — a "
            "caller can rewrite the association for everyone after it"
        )
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


#: XP's `SM_CXVSCROLL` at 96 DPI, which `src/scrollbar.css` declares as `--sb-size`.
SCROLLBAR_PX = 17

#: The scroller the skin is measured on, named rather than discovered.
#:
#: `windows.find(w => w.bodyEl.scrollHeight > w.bodyEl.clientHeight)` is what this probe asked
#: first, and it is the wrong question: it answers with whichever window happens to overflow,
#: so the check silently retargeted to the `windows` app and went on reporting ok while Read
#: me — the window the docstring names and the one the skin was tuned against — had stopped
#: overflowing. A check that picks its own subject cannot fail by losing it.
SCROLLBAR_WINDOW = "readme"

SCROLLBAR_PROBE = """
((wanted) => {
  const root = getComputedStyle(document.documentElement);
  const token = (name) => root.getPropertyValue(name).trim();
  const links = [...document.querySelectorAll('link[rel=stylesheet]')]
    .filter((l) => new URL(l.href).pathname === '/src/scrollbar.css');
  const sheet = [...document.styleSheets]
    .find((s) => (s.href || '').endsWith('/src/scrollbar.css'));
  let rules = 0;
  try { rules = sheet ? sheet.cssRules.length : 0; } catch (e) { rules = 'UNREADABLE:' + e.name; }
  const win = (window.webxp.windows || []).find((w) => w.programId === wanted);
  const el = win ? win.bodyEl : null;
  const names = ['--sb-size', '--sb-edge', '--sb-track-edge', '--sb-track-face',
                 '--sb-thumb-face', '--sb-thumb-lit', '--sb-thumb-shade', '--sb-thumb-grip'];
  return {
    links: links.length,
    rules,
    found: Boolean(win),
    overflows: el ? el.scrollHeight > el.clientHeight : null,
    scrollHeight: el ? el.scrollHeight : null,
    clientHeight: el ? el.clientHeight : null,
    reserved: el ? el.offsetWidth - el.clientWidth : null,
    barWidth: el ? getComputedStyle(el, '::-webkit-scrollbar').width : null,
    thumbEdge: el ? getComputedStyle(el, '::-webkit-scrollbar-thumb').borderTopColor : null,
    supportsPseudo: CSS.supports('selector(::-webkit-scrollbar)'),
    tokens: Object.fromEntries(names.map((n) => [n, token(n)])),
  };
})(WANTED)
"""


def check_scrollbar_skin(page, *, invert: bool = False) -> list[str]:
    """The XP scrollbar is linked, reaches a real scroller, and keeps its contrast.

    Nothing else in the tree styles a scrollbar, so `src/scrollbar.css` is the whole skin and
    a dropped `<link>` in `index.html` silently restores the host browser's bar in every
    window with content — the most visible seam available, and invisible to every other check
    here.

    WHAT THIS DOES NOT PROVE
    ------------------------
    It does not prove the bar paints. `scripts/cdp.py` launches Chrome with
    `--hide-scrollbars`, so no scrollbar exists in this run and no screenshot under
    `screenshots/` contains one: the Read me body reserves 0px rather than 17. With that one
    flag stripped the same body reserves 17. The flag is read below rather than assumed, so
    the day it goes the 17px assertion starts running in place of the 0px one and this check
    cannot go on reporting ok over a harness that stopped looking.

    `getComputedStyle(el, '::-webkit-scrollbar').width` resolves the declaration whether or
    not the pseudo-element is live, so it is asserted as "the rule reached this element",
    which is all it is. The thumb's border colour is read the same way and is the value SC
    1.4.11 rests on, so it is the computed colour that goes into the ratio rather than the
    token text.

    Invertible on the two readings that would be zero if the sheet had not arrived: the
    `<link>` count and the width the rule computes to on the element a reader scrolls.
    """
    bad = []
    launch(page, SCROLLBAR_WINDOW)
    got = page.evaluate(SCROLLBAR_PROBE.replace("WANTED", json.dumps(SCROLLBAR_WINDOW)))

    if invert:
        # --prove-failure. The sheet is linked once and its width reached the scroller, so
        # asserting that it did neither must fail.
        if got["links"] == 1 and got["barWidth"] == f"{SCROLLBAR_PX}px":
            bad.append(
                f"scrollbar skin[--prove-failure]: /src/scrollbar.css is linked "
                f"{got['links']} time(s) and computes to {got['barWidth']} on the "
                f"{SCROLLBAR_WINDOW} body as it should — the inverted assertion failed, so "
                "this check can fail"
            )
        return bad

    if got["links"] != 1:
        bad.append(
            f"scrollbar skin: index.html has {got['links']} links to /src/scrollbar.css, want "
            "exactly 1 — nothing else in the tree styles a scrollbar, so at 0 every window "
            "with content shows the host browser's bar"
        )
    if not isinstance(got["rules"], int) or got["rules"] < 1:
        bad.append(
            f"scrollbar skin: src/scrollbar.css contributed {got['rules']!r} rules — the "
            "stylesheet 404'd, was blocked, or parsed to nothing"
        )
    # Named, both times. Either absence is this check losing its subject, and the skin then
    # goes unmeasured — which must be a failure and not a quiet retarget.
    if not got["found"]:
        return bad + [
            f"scrollbar skin: no {SCROLLBAR_WINDOW} window is open, so the skin was measured "
            "on nothing"
        ]
    if not got["overflows"]:
        return bad + [
            f"scrollbar skin: the {SCROLLBAR_WINDOW} window holds {got['scrollHeight']}px of "
            f"content in {got['clientHeight']}px and so has nothing to scroll — this check "
            "needs a real scroller, and silently measuring some other window's would be worse"
        ]
    where = f"the {SCROLLBAR_WINDOW} window body"
    if got["barWidth"] != f"{SCROLLBAR_PX}px":
        bad.append(
            f"scrollbar skin: ::-webkit-scrollbar width on {where} computes to "
            f"{got['barWidth']!r}, want '{SCROLLBAR_PX}px' — the rule did not reach the "
            "element the reader scrolls"
        )

    # The harness's own blind spot, stated rather than papered over.
    hidden = "--hide-scrollbars" in (REPO_ROOT / "scripts" / "cdp.py").read_text(encoding="utf-8")
    if hidden and got["reserved"] != 0:
        bad.append(
            f"scrollbar skin: {where} reserves {got['reserved']}px for a scrollbar while "
            "scripts/cdp.py still passes --hide-scrollbars, which should leave 0 — the two "
            "disagree, so one of them is not what this check thinks it is"
        )
    if not hidden and got["reserved"] != SCROLLBAR_PX:
        bad.append(
            f"scrollbar skin: {where} reserves {got['reserved']}px, want {SCROLLBAR_PX} — "
            "scripts/cdp.py no longer hides scrollbars, so the skin is observable now and it "
            "is not laying out at XP's width"
        )

    tokens = got["tokens"]
    missing = sorted(name for name, value in tokens.items() if not value)
    if missing:
        bad.append(
            f"scrollbar skin: src/scrollbar.css declares none of {', '.join(missing)} on the "
            "root — renamed or moved, and every rule reading them falls back to its initial "
            "value"
        )
    if tokens.get("--sb-size") and tokens["--sb-size"] != f"{SCROLLBAR_PX}px":
        bad.append(
            f"scrollbar skin: --sb-size is {tokens['--sb-size']!r}, want '{SCROLLBAR_PX}px' — "
            "XP's SM_CXVSCROLL at 96 DPI"
        )

    # The correction. Luna's thumb is #c8d6fb inside a white border on a near-white track:
    # 1.44:1, and the only thing on the bar that says where the control is. The border
    # carries it instead, measured on the element rather than read out of the token.
    edge = rgb_stops([got["thumbEdge"]])
    if not edge:
        bad.append(
            f"scrollbar skin: the thumb's border colour on {where} is {got['thumbEdge']!r}, "
            "which did not parse as a colour — the contrast correction went unmeasured"
        )
    for name in ("--sb-track-edge", "--sb-track-face"):
        track = rgb_stops([tokens.get(name)])
        if not edge or not track:
            continue
        ratio = contrast(edge[0], track[0])
        if ratio < AA_NON_TEXT:
            bad.append(
                f"scrollbar skin: the thumb's border on {name} is {ratio:.2f}:1, under SC "
                f"1.4.11's {AA_NON_TEXT}:1 — the thumb's outline is the only thing marking "
                "where the bar is, and Luna's own 1px white border is what this value "
                "replaced"
            )
    thumb = rgb_stops([tokens.get("--sb-thumb-face")])
    if edge and thumb and contrast(edge[0], thumb[0]) < AA_NON_TEXT:
        bad.append(
            f"scrollbar skin: the thumb's border on its own face is "
            f"{contrast(edge[0], thumb[0]):.2f}:1, under {AA_NON_TEXT}:1 — the outline has "
            "merged into the thumb it is outlining"
        )

    source = (REPO_ROOT / "src" / "scrollbar.css").read_text(encoding="utf-8")

    # The one duplication CSS forces: a url() cannot read a custom property, so each arrow
    # glyph spells --sb-edge's value. Pinned so the five cannot drift apart in silence.
    declared = re.search(r"--sb-edge:\s*(#[0-9a-fA-F]{3,8})\s*;", source)
    if not declared:
        bad.append(
            "scrollbar skin: src/scrollbar.css no longer declares --sb-edge as a hex literal, "
            "so the arrow glyphs cannot be compared against it"
        )
    else:
        want = declared.group(1).lower().lstrip("#")
        glyphs = re.findall(r"--sb-arrow-(\w+):\s*url\(\"([^\"]*)\"\)", source)
        if len(glyphs) != 4:
            bad.append(
                f"scrollbar skin: {len(glyphs)} --sb-arrow-* glyphs in src/scrollbar.css, want "
                "4 — one per direction, and a missing one leaves that button blank"
            )
        for direction, uri in glyphs:
            fills = [f.lower() for f in re.findall(r"fill='%23([0-9a-fA-F]{3,8})'", uri)]
            if fills != [want]:
                bad.append(
                    f"scrollbar skin: --sb-arrow-{direction} fills with {fills!r} but "
                    f"--sb-edge is '#{want}' — a url() cannot read a custom property, so "
                    "these two are hand-matched and have come apart"
                )

    # `display: block` is what makes the four arrow buttons exist; a size alone leaves the
    # bar with none. `getComputedStyle` cannot address `:vertical:start:decrement`, so this
    # is a pin on the spelling in the same shape as `scripts/css_ties.py`'s OUTRANKS
    # entries, and it is worth having: dropping the declaration removes all four buttons and
    # changes nothing else any assertion here can see.
    buttons = re.search(
        r"::-webkit-scrollbar-button:vertical:start:decrement,.*?\{(.*?)\}", source, re.S
    )
    if not buttons:
        bad.append(
            "scrollbar skin: src/scrollbar.css has no rule starting "
            "`::-webkit-scrollbar-button:vertical:start:decrement,` — the shared button rule "
            "was renamed or split, so the `display: block` pin asserts nothing"
        )
    elif "display: block" not in buttons.group(1):
        bad.append(
            "scrollbar skin: the shared arrow-button rule does not set `display: block` — a "
            "width and a height alone leave the bar with no buttons at all, and nothing else "
            "in this check would notice"
        )

    # The guard that keeps the standard properties away from Chromium.
    if not re.search(r"@supports\s+not\s+selector\(::-webkit-scrollbar\)", source):
        bad.append(
            "scrollbar skin: src/scrollbar.css has no `@supports not "
            "selector(::-webkit-scrollbar)` guard — an unguarded scrollbar-color switches "
            "Chromium to the standard scrollbar and discards every ::-webkit- rule above it"
        )
    elif not got["supportsPseudo"]:
        bad.append(
            "scrollbar skin: this engine reports no support for selector(::-webkit-scrollbar), "
            "so the Firefox block is live here and the XP skin is not being drawn at all"
        )
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
        press_key(page, "ArrowRight")
    for _ in range(2):
        press_key(page, "ArrowDown")
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
        press_key(page, "ArrowRight", modifiers=SHIFT)
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
    # Notepad and the read-me only: this clicks a computed point, and anything else left open
    # can be over it. Raising Notepad first is not an option — the check is that a click
    # raises it, so it has to start underneath something.
    state = expose(page, "notepad", "readme")
    notepad = need(state, "notepad")
    # The underneath precondition, asserted rather than assumed.
    #
    # `expose` launches whichever of the two is missing, and a launched window lands on top.
    # With Notepad already topmost the click has nothing to raise it above, and the z
    # assertion below then reports a z that did not rise — a sentence about a window manager
    # that did nothing wrong. Minimised and hidden windows do not count: a click cannot be
    # shown to raise anything above a window that is not on screen.
    #
    # A harness MUST fail when it cannot reach what it was pointed at, and MUST say which
    # kind of failure it is. This is the harness's problem, not the window manager's.
    under_something = any(
        w["state"] != "min" and not w["hidden"] and w["z"] > notepad["z"]
        for w in state["wins"]
    )
    if not under_something:
        return [
            f"focus: cannot start Notepad underneath another window — it is already topmost "
            f"at z {notepad['z']}. This is a harness problem, not a product one: the "
            "precondition is a window on screen over Notepad, and the state this check "
            "inherited has none."
        ]

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
        " const tiles = [...w.bodyEl.querySelectorAll('.folder-items .folder-item')];"
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
        " const tile = w && w.bodyEl.querySelector('.folder-items .folder-item');"
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
        drain(page)
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
    drain(page)

    # And the other direction: a window publishes a link to itself as it moves.
    page.evaluate("window.history.replaceState(null, '', window.location.pathname)")
    page.call("Page.reload", ignoreCache=True)
    time.sleep(0.8)
    wait_for_route(page)
    drain(page)
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
        drain(page)
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

#: A string whose Python and JavaScript lengths differ, and the length JavaScript gives it.
#:
#: `🎉` is U+1F389, above U+FFFF, so it is one code point and two UTF-16 code units: `len`
#: says 3 and `String.length` says 4. The literal is the whole point — it is an expectation
#: that does not come from `mount.py`, and the unit defect cannot be caught by any
#: expectation that does.
ASTRAL_FIXTURE = "a\U0001F389b"
ASTRAL_UTF16_UNITS = 4


def check_mount_index(page) -> list[str]:
    """The index agrees with the tracked files, lists nothing it must not, and counts right.

    Three separate claims. The first is the stale-generated-file problem `manifest is
    current` already guards for `apps/`. The second is a privacy rule: `.gitignore` excludes
    `apps/day-*/private/` because it holds real Strava traces, and an index built by walking
    the working tree would mount them into a filesystem with a browsable `ls`. That
    directory is absent from some worktrees and present in others, so the walk produces a
    clean index on one machine and publishes the traces from another — and the clean run is
    the one you would trust. Asserted against the index rather than against the generator's
    intent, because the index is what ships.

    The third is the unit the sizes are counted in, and it is here rather than in
    `mount.py --check` because `--check` structurally cannot see it: it recomputes sizes with
    the same function that wrote them, so a wrong unit is wrong identically on both sides and
    compares equal — and it tolerates size drift by design anyway, printing a note and
    exiting 0. A wrong unit needs an expectation from outside the generator.
    """
    bad = []

    # The unit, against a literal and against the browser that reads the index.
    #
    # No tracked file holds a character above U+FFFF today, so every real row agrees whether
    # the generator counts code points or code units, and this fixture is the only thing that
    # can tell the two apart. Counting code points puts `ls -l` one short per such character
    # until the file is read and `inode.data.length` replaces the figure — the size-changes-
    # on-read defect the column was added to remove.
    units = mount.utf16_units(ASTRAL_FIXTURE)
    if units != ASTRAL_UTF16_UNITS:
        bad.append(
            f"mount index: mount.utf16_units({ASTRAL_FIXTURE!r}) is {units}, not "
            f"{ASTRAL_UTF16_UNITS} — the index is not counting UTF-16 code units, so a file "
            "with an astral character gets a size that changes when it is read"
        )
    in_browser = page.evaluate(f"{json.dumps(ASTRAL_FIXTURE)}.length")
    if in_browser != units:
        bad.append(
            f"mount index: {ASTRAL_FIXTURE!r} is {units} to mount.py and {in_browser} to the "
            "browser holding the filesystem — the writer and the reader of mount.json "
            "disagree on the unit"
        )
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
    signal that a boot's module fetches are done. `loadApps` (`src/apps.mjs`) `Promise.all`s
    one `loadApp` per id in `apps/manifest.mjs`, and the shell's own modules are fetched on
    top of those, so the count rises with every app added.

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


#: How page script picks the Notepad window to measure: the one named, or the newest if none
#: was named. Declares `const win`. `ID` is filled in by `with_window_id`.
#:
#: Every probe that reads a Notepad goes through this, because falling back to *another*
#: Notepad on a miss is how a check that drives its own subject through a close path reports
#: ok on having lost it. `check_notepad_picker` launches its own Notepad while `check_picker`
#: may have left the shared one open, and it drives File > Exit and Cancel — the close path
#: that *is* the subject. With a fallback, a Cancel that closed the window silently moved 18
#: reads onto the other one: "Cancel left the document dirty" read off a window nothing had
#: edited, and the check reported ok. A check that picks its own subject cannot fail by
#: losing it.
NOTEPAD_PICK = (
    "const pads = window.webxp.windows.filter((p) => p.programId === 'notepad');"
    " const win = ID ? pads.find((p) => p.id === ID) : pads[pads.length - 1];"
)

#: What a probe says when the window it was *named* is not there. One message, two probes,
#: because the two disagree on what an unnamed miss means — `null` is "no Notepad is open",
#: which `check_notepad` and `check_open_across_apps` read as a verdict, while a picker
#: cannot be measured on no window at all. Only the choice of window is shared; what a miss
#: means is each probe's own.
NOTEPAD_GONE = json.dumps("the notepad window asked for is gone")

NOTEPAD_PARTS = (
    "(() => { " + NOTEPAD_PICK
    + " if (!win) return ID ? { id: ID, error: " + NOTEPAD_GONE + " } : null;"
    + " const area = win.el.querySelector('.notepad-area');"
    # A missing text area is reported as itself. Returning null for `value` and 0 for
    # the coordinates made a renamed class fail as 'Notepad overwrote an external change
    # silently', which sends the reader after the wrong defect.
    " if (!area) return { id: win.id, error: 'no .notepad-area in the Notepad window' };"
    " const r = area.getBoundingClientRect();"
    " return { id: win.id, title: win.titleText, value: area.value,"
    "          status: win.statusEl ? win.statusEl.textContent : '',"
    "          x: r.left + 20, y: r.top + 10 }; })()"
)


def with_window_id(template: str, win_id: str | None) -> str:
    """`ID` in a page script replaced by a quoted window id, or `null` for "the newest"."""
    return template.replace("ID", json.dumps(win_id) if win_id else "null")


def _notepad_parts(page, win_id: str | None = None) -> dict | None:
    return page.evaluate(with_window_id(NOTEPAD_PARTS, win_id))


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
    # Over `/home`, not `/`. The mounted repository is in the tree but deliberately outside
    # the quota — `stats().used` does not count a mounted inode, because its bytes live on the
    # server and are never written to the image. So `du -s /` legitimately exceeds `df` by the
    # size of the mount, and the dedup this asserts does not need the whole filesystem: the
    # hard-linked inode is under `/home/user`.
    counted = term_run(page, "du -s /home", shell)
    home_used = page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.id === %s);"
        " const fs = w.app.shell.fs; let total = 0; const seen = new Set();"
        " for (const e of fs.walk('/home')) {"
        "   if (e.stat && e.stat.type !== 'dir' && !seen.has(e.stat.ino)) {"
        "     seen.add(e.stat.ino); total += e.stat.size; } }"
        " return total; })()" % json.dumps(shell)
    )
    if f"{home_used}\t" not in counted:
        bad.append(
            f"terminal: du -s /home counted {counted.strip()[:48]!r}, and one charge per "
            f"inode over that tree is {home_used}"
        )

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
    # buffer the end-of-run sweep would attribute it to whatever check runs next. Through
    # `drain_expecting`, because a bare read is destructive: the patch and its restore above
    # can raise a second error, and a read that asks only for the needle consumes that one
    # too and reports clean.
    bad += drain_expecting(page, "cat threw", "terminal")
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
    # The contrast read below goes through `window.__wxColours`, which only exists once this
    # is evaluated. Installed here rather than relied on from an earlier check: `--only
    # explorer` runs this one alone, and a check that works only in company is one reorder
    # from reading `undefined` as a colour.
    page.evaluate(COLOUR_READER)
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
        # Through `__wxColours`, because `rgb_stops` reads its normalised output rather than a
        # raw CSS string. Handed `'rgb(255, 255, 255)'` it iterates the characters, finds no
        # tuple, and reports "could not read" over colours that were right there.
        " const C = window.__wxColours;"
        " return { ink: C(seen.color), fill: C(seen.backgroundColor),"
        "          typeInk: C(getComputedStyle(type).color) }; })()"
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

    #: The buffer and the file at each step, exactly.
    #:
    #: Every assertion below was `in`, and the exact bytes are what a stray keystroke hides
    #: in: `edited-in-notepad\ns` satisfies `"edited-in-notepad" in`, so a Ctrl+S that typed
    #: its own `s` into the document reported a clean save of a corrupted one. The lengths
    #: are derived from these strings rather than spelled in the status line's wording.
    mounted = "bridge-one\n"
    edited = mounted + "edited-in-notepad\n"
    twice_edited = edited + "one-more-edit\n"
    behind = "written-behind-notepad\n"

    def cat(path: str) -> str:
        """What `cat` printed, without the prompt and the echoed line `term_run` returns."""
        printed = term_run(page, f"cat {path}", shell)
        return printed.split("\n", 1)[1] if "\n" in printed else printed

    def buffer_now() -> str:
        return notepad_parts(page, pad["id"])["value"]

    if pad["title"] != "bridge.txt - Notepad":
        bad.append(f"open: Notepad is titled {pad['title']!r}, want 'bridge.txt - Notepad'")
    if pad["value"] != mounted:
        bad.append(f"open: Notepad holds {pad['value']!r}, want the file the shell wrote")

    # Typed into, then saved with Ctrl+S. The caret is moved by script and the text is
    # inserted as real input: `insertText` lands wherever the caret is, and a click lands it
    # wherever the pointer was.
    notepad_caret(page, pad["id"])
    page.type_text("edited-in-notepad\n")
    time.sleep(0.1)

    def ctrl_s() -> None:
        # Notepad's handler asks for `ctrlKey`, and a keypress without it is the browser's
        # own Save dialog rather than the editor's.
        press_key(page, "s", modifiers=CTRL)
        time.sleep(0.3)

    ctrl_s()
    if buffer_now() != edited:
        bad.append(f"open: Ctrl+S left the buffer {buffer_now()!r}, want {edited!r}")
    saved = notepad_parts(page, pad["id"])["status"]
    if saved != f"/tmp/bridge.txt · {len(edited)} characters · saved":
        bad.append(f"open: Notepad's status after Ctrl+S is {saved!r}")

    # A write from the other window, behind this buffer's back. The first Ctrl+S after it has
    # to refuse: Notepad read the file once at mount, and saving over a change it never saw
    # would lose the shell's write while reporting success.
    term_run(page, "echo written-behind-notepad > /tmp/bridge.txt", shell)
    notepad_caret(page, pad["id"])
    page.type_text("one-more-edit\n")
    ctrl_s()
    if buffer_now() != twice_edited:
        bad.append(f"open: the refused Ctrl+S left the buffer {buffer_now()!r}")
    conflict = notepad_parts(page, pad["id"])["status"]
    if conflict != (
        "/tmp/bridge.txt · changed on disk since it was opened — Ctrl+S again to overwrite"
    ):
        bad.append(f"open: Notepad overwrote an external change silently — status {conflict!r}")
    held = cat("/tmp/bridge.txt")
    if held != behind:
        bad.append(f"open: the refused save wrote anyway — the file reads {held!r}")
    # And the second one goes through, because replacing it may well be what was meant.
    raise_window(page, pad["id"])
    ctrl_s()
    forced = cat("/tmp/bridge.txt")
    if forced != twice_edited:
        bad.append(f"open: a second Ctrl+S did not overwrite — the file reads {forced!r}")

    # The two apps read the same bytes, each through its own surface. Asserted against the
    # buffer rather than against the literal again, so this says "one filesystem" and not
    # "the literal above a second time".
    back, held_in_pad = cat("/tmp/bridge.txt"), buffer_now()
    if back != held_in_pad:
        bad.append(
            f"open: the terminal reads {back!r} and Notepad holds {held_in_pad!r} — Notepad "
            "saved into a different filesystem from the one the shell is holding"
        )

    # A chord nothing binds leaves the document alone, asserted on the one window in this
    # suite whose buffer is known to the byte. `q` because Notepad binds only Ctrl+O and
    # Ctrl+S (`apps/notepad/app.mjs:286-288`), so no `preventDefault` stands between the
    # keypress and Blink's editing path.
    #
    # This is the assertion that would have caught `press_key` dispatching `text` for every
    # character, modified or not. Measured with the rule disabled: Alt+Q and Ctrl+Alt+Q each
    # left a `q` in the buffer — Blink reads Ctrl+Alt as AltGr, which composes — while
    # Ctrl+Q alone inserted nothing. `q` and not the Ctrl+S above, because Ctrl alone cannot
    # show the defect at all: it is not a composing modifier, so a plain Ctrl chord carrying
    # `text` inserts nothing even when nothing calls `preventDefault`. Measured on a bare
    # textarea with no shell listener over it: Ctrl+S and Ctrl+Q left the buffer untouched at
    # `defaultPrevented: false`, while Alt+S and Ctrl+Alt+S each typed an `s`. Alt is the
    # only arm that bites, and the loop below holds it with both of its neighbours.
    #
    # Meta is not probed. Cmd+Q is Quit, and dispatching it shut Chrome down mid-check: the
    # run reported "socket is already closed" from the screenshot and nothing about the key.
    notepad_caret(page, pad["id"])
    for chord, bits in (("Alt+Q", ALT), ("Ctrl+Alt+Q", CTRL | ALT), ("Ctrl+Q", CTRL)):
        press_key(page, "q", modifiers=bits)
        if buffer_now() != twice_edited:
            bad.append(
                f"open: {chord} typed into the document — the buffer reads "
                f"{buffer_now()!r}, want {twice_edited!r}. A chord is a command, not a "
                "character."
            )
            break

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


# ---------------------------------------------------------------------------
# The file picker, as a service any window can ask for. `src/dialogs.mjs` holds it.
# ---------------------------------------------------------------------------

#: XP's file dialog footer, by way of win32.run's `Viewer3.svelte` (lines 196-228): two fixed
#: rows with the labels in a gutter. `src/picker.css` declares both as tokens on the dialog;
#: these are the numbers a reader sees, so they are asserted rather than read back off the
#: custom property that sets them.
PICKER_FOOT_H = 35
PICKER_GUTTER = 100

#: Where the picker's checks build their folder. Named here because two checks share it.
PICK_DIR = "/home/user/pick-probe"

PICKER_STATE = "(() => {\n  " + NOTEPAD_PICK + """
  if (!win) return { error: ID ? """ + NOTEPAD_GONE + """
                                : 'no notepad window to put a picker on' };
  const el = (s) => win.el.querySelector(s);
  const dialog = el('.dialog.picker');
  const round = (r) => ({ x: Math.round(r.left), y: Math.round(r.top),
                          w: Math.round(r.width), h: Math.round(r.height) });
  const labels = [...win.el.querySelectorAll('.picker-foot .picker-label')]
    .map((n) => round(n.getBoundingClientRect()));
  const active = document.activeElement;
  return {
    id: win.id,
    hasModal: win.hasModal,
    layers: win.el.querySelectorAll('.window-modal').length,
    picker: Boolean(dialog),
    dialog: dialog ? round(dialog.getBoundingClientRect()) : null,
    body: round(win.bodyEl.getBoundingClientRect()),
    win: round(win.el.getBoundingClientRect()),
    places: [...win.el.querySelectorAll('.picker-place')].map((p) => p.dataset.path),
    where: (el('.picker-where') || {}).textContent ?? null,
    note: (el('.picker-note') || {}).textContent ?? null,
    rows: [...win.el.querySelectorAll('.picker-row')].map((r) => ({
      name: r.dataset.name, kind: r.dataset.kind,
      refused: r.getAttribute('aria-disabled') === 'true',
      selected: r.getAttribute('aria-selected') === 'true',
      stop: r.tabIndex === 0,
      opacity: Number(getComputedStyle(r).opacity),
      role: r.getAttribute('role'),
    })),
    listRole: el('.picker-list') ? el('.picker-list').getAttribute('role') : null,
    name: (el('.picker-name') || {}).value ?? null,
    go: el('.picker-go') ? [el('.picker-go').textContent, el('.picker-go').disabled] : null,
    gutter: labels.length ? labels[0].w : null,
    pitch: labels.length > 1 ? labels[1].y - labels[0].y : null,
    ask: (el('.picker-ask .dialog-text') || {}).textContent || null,
    activeInPicker: Boolean(dialog && dialog.contains(active)),
    activeClass: active ? (active.className || active.tagName) : null,
    answer: window.__picked ?? null,
    title: win.titleText,
    status: win.statusEl ? win.statusEl.textContent : '',
    buffer: (el('.notepad-area') || {}).value ?? null,
    info: win.app && win.app.info ? win.app.info() : null,
    pads: window.webxp.windows.filter((w) => w.programId === 'notepad').length,
  };
})()"""


def picker_state(page, win_id: str | None = None) -> dict:
    """What the picker on a Notepad window is showing, and what it has answered.

    Every field that can be absent is coerced to `null` in the page, not guarded here: a CDP
    result by value drops an `undefined` property entirely, so reading one off a dialog that
    has already closed raised `KeyError` and reported as a harness fault instead of as the
    assertion it was standing in for.
    """
    state = page.evaluate(with_window_id(PICKER_STATE, win_id))
    # A missing window is the harness failing to reach what it was pointed at, not a verdict
    # about the picker. It used to be neither: the probe fell back to any other Notepad and
    # answered about that one instead. `NOTEPAD_PICK` carries the rule now.
    if state.get("error"):
        raise HarnessError(state["error"])
    return state


def start_picker(page, win_id: str, call: str) -> dict:
    """Open a picker on that window and stash its answer where the state probe can read it.

    Through the module's own entry point, because that is the interface an app has. The
    answer is kept on `window.__picked` rather than awaited here: the check has to drive the
    dialog with real input while the promise is still pending, which is the whole point of
    it.
    """
    page.evaluate(
        "(() => { window.__picked = 'pending';"
        " const win = window.webxp.windows.find((w) => w.id === %s);"
        " import('/src/dialogs.mjs').then((d) => d.%s.then("
        "   (value) => { window.__picked = value; },"
        "   (error) => { window.__picked = 'REJECTED: ' + error.message; })); })()"
        % (json.dumps(win_id), call)
    )
    time.sleep(0.9)
    return picker_state(page, win_id)


def type_into_name(page, win_id: str, text: str) -> None:
    """Clear the name box, focus it for real, and type. Enter is pressed by the caller."""
    page.evaluate(
        "(() => { const win = window.webxp.windows.find((w) => w.id === %s);"
        " const box = win.el.querySelector('.picker-name');"
        " box.value = ''; box.focus(); })()" % json.dumps(win_id)
    )
    page.type_text(text)
    time.sleep(0.15)


def ready_shell(page) -> str:
    """A terminal that is actually on screen, and its id.

    `term_run` drives real input: it raises the window and clicks where the input is. A
    minimised terminal cannot take that click, and `term_run` then returns the bare prompt —
    which reads exactly like a command that printed nothing, so every file a check meant to
    create is silently missing and the product is blamed for not finding it. `expose`
    minimises every window but its target, so any check that runs after one needs this.
    Measured: without it, `echo … > /tmp/pad-probe/one.txt` returned the prompt and the file
    was never created.
    """
    shell = terminal_session(page)
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " if (w && w.state === 'min') w.restore(); })()" % json.dumps(shell)
    )
    time.sleep(0.5)
    return shell


def press_in_picker(page, win_id: str, selector: str, answer: str | None = None) -> bool:
    """Press a control inside the picker, and say whether it was there to press.

    A control that is missing is what a control run looks like: the question the picker was
    supposed to ask, deleted. Reported by the assertion that wanted it rather than raised as
    a `TypeError` from page script, which reads as the harness breaking.
    """
    return page.evaluate(
        "(() => { const win = window.webxp.windows.find((w) => w.id === %s);"
        " if (!win) return false;"
        " const found = [...win.el.querySelectorAll(%s)];"
        " const target = %s === null ? found[0]"
        "   : found.find((b) => b.dataset.id === %s);"
        " if (!target) return false; target.click(); return true; })()"
        % (json.dumps(win_id), json.dumps(selector), json.dumps(answer), json.dumps(answer))
    )


#: `src/window.mjs`, read for the selector its modal focus trap cycles.
SOURCE_WINDOW = (REPO_ROOT / "src" / "window.mjs").read_text(encoding="utf-8")


def focusable_selector() -> str:
    """The selector `window.mjs`'s modal trap cycles, read out of the product.

    Read rather than copied, so a probe carrying its own copy cannot measure the copy — and a
    shape it can no longer parse is a failure, not a skip.
    """
    found = re.search(r"const FOCUSABLE = \[(.*?)\]\.join", SOURCE_WINDOW, re.S)
    if not found:
        raise HarnessError("no `const FOCUSABLE = [...]` in src/window.mjs to read")
    return ", ".join(re.findall(r"'([^']+)'", found.group(1)))


def picker_tab_stops(page, win_id: str, bad: list[str]) -> list[str]:
    """One tab stop for the whole file list, asserted against the trap that decides it.

    `row.tabIndex === 0` is a property of the row, not of the cycle: `window.mjs`'s modal
    trap consumes every Tab and walks its own `FOCUSABLE` list, so inside a dialog the trap
    *is* the tab order and the picker's roving tabindex decides nothing. Measured before the
    host excluded `tabindex="-1"` from its type selectors: 14 stops for a 6-file folder, 6 of
    them rows, and Tab crossed every file between the places column and the name box. In
    `/usr/src/webxp/apps`, 41 entries, that was 41 presses.

    The selector is read out of the product rather than copied, so a probe carrying its own
    copy cannot measure the copy — and a shape it can no longer parse is a failure, not a skip.
    """
    selector = focusable_selector()
    trap = page.evaluate(
        "((sel) => { const win = window.webxp.windows.find((w) => w.id === %s);"
        " const layer = win && win.el.querySelector('.window-modal');"
        " if (!layer) return null;"
        " const stops = [...layer.querySelectorAll(sel)];"
        " return { rows: layer.querySelectorAll('.picker-row').length,"
        "   rowStops: stops.filter((s) => s.classList.contains('picker-row')).length,"
        "   notATabStop: stops.filter((s) => s.tabIndex === -1)"
        "     .map((s) => (s.className || s.tagName)) }; })(%s)"
        % (json.dumps(win_id), json.dumps(selector))
    )
    if trap is None:
        return bad + ["picker: no modal layer to read the trap's stop list from"]
    if trap["rows"] < 3:
        # A folder with one or two rows cannot tell one stop from one per row.
        bad.append(f"picker: {trap['rows']} rows in the probe folder — too few to measure a "
                   "roving tab stop against a stop-per-row")
    if trap["rowStops"] != 1:
        bad.append(
            f"picker: the modal trap cycles {trap['rowStops']} of {trap['rows']} file rows, "
            "want 1 — the list is one tab stop walked with the arrows, and a stop per file "
            "puts a folder's whole contents between the places column and the name box"
        )
    if trap["notATabStop"]:
        bad.append(
            f"picker: {trap['notATabStop']} are in the trap's cycle with tabindex=-1 — "
            "focusable by script and deliberately not a tab stop, so the trap and the roving "
            "tabindex disagree about what Tab does"
        )
    return bad


def check_picker(page) -> list[str]:
    """The file picker as a service: a path or null, clipped to the window that asked.

    Every condition is built here from a terminal — `docs/review.md` rule 4 — so a check
    failure names this check's own folder and not a fixture some other app owns.

    What this is for: the picker resolves an **absolute path**, and `null` on a cancel rather
    than rejecting, because `try/catch` around a cancel is the mistake every caller makes. It
    resolves nothing it has not read, so a mounted file cannot reach a caller as an `EAGAIN`
    one window away from the press that opened it.
    """
    bad = []
    # Runnable alone: `--only picker` on a fresh page would otherwise race the boot's module
    # fetches against the single-threaded `http.server`, and lose. `wait_for_apps` says why.
    wait_for_apps(page)
    shell = ready_shell(page)
    term_run(page, f"rm -rf {PICK_DIR}", shell)
    term_run(page, f"mkdir -p {PICK_DIR}/inner", shell)
    term_run(page, f"echo picked-me > {PICK_DIR}/note.txt", shell)
    term_run(page, f"echo wrong-kind > {PICK_DIR}/other.md", shell)
    listing = term_run(page, f"ls {PICK_DIR}", shell)
    if "note.txt" not in listing or "other.md" not in listing:
        # The harness could not build its own condition, and must say so rather than report
        # the picker as unable to list a folder that was never written.
        return bad + [
            f"picker: the harness did not create {PICK_DIR} — the shell listed "
            f"{listing.strip()[:120]!r}"
        ]

    state = expose(page, "notepad")
    pad = need(state, "notepad")
    win_id = pad["id"]
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " w.moveTo(60, 40); w.sizeTo(620, 440); })()" % json.dumps(win_id)
    )
    time.sleep(0.4)

    # 1. The dialog, where it belongs, and the footer XP built.
    up = start_picker(page, win_id, "pickFile(win, { dir: %s })" % json.dumps(PICK_DIR))
    if not up["picker"]:
        # The harness cannot assert anything about a dialog that is not there, and must not
        # report the rest as passing.
        return bad + [f"picker: no .dialog.picker went up on {win_id} — note {up['note']!r}"]
    frame, dialog = up["win"], up["dialog"]
    if (dialog["x"] < frame["x"] or dialog["y"] < frame["y"]
            or dialog["x"] + dialog["w"] > frame["x"] + frame["w"]
            or dialog["y"] + dialog["h"] > frame["y"] + frame["h"]):
        bad.append(
            f"picker: the dialog is at {dialog} outside its window at {frame} — a dialog "
            "belongs to the window that asked, not to the screen"
        )
    if up["gutter"] != PICKER_GUTTER:
        bad.append(f"picker: the footer's label gutter is {up['gutter']}px, want {PICKER_GUTTER}")
    # Two fixed rows: the pitch is the row plus the grid's own gap, so it is asserted with
    # slack rather than as an equality on a number the gap also feeds.
    if up["pitch"] is None or not (PICKER_FOOT_H <= up["pitch"] <= PICKER_FOOT_H + 10):
        bad.append(
            f"picker: the footer's two rows are {up['pitch']}px apart, want {PICKER_FOOT_H}px "
            "plus the row gap"
        )
    if up["listRole"] != "listbox" or any(row["role"] != "option" for row in up["rows"]):
        bad.append(f"picker: the list is {up['listRole']!r} and its rows are "
                   f"{sorted({row['role'] for row in up['rows']})}")

    # 2. The places column is derived, not listed. HOME and `/` are the two the seed
    #    guarantees; the mounted repository is there because the filesystem says so, and a
    #    hardcoded list is what goes stale when a graft moves.
    for wanted in ("/home/user", "/", "/usr/src/webxp"):
        if wanted not in up["places"]:
            bad.append(f"picker: {wanted} is not in the places column {up['places']}")

    # 3. Folders first, then by name, and one tab stop for the whole list.
    order = [row["name"] for row in up["rows"]]
    if order != ["inner", "note.txt", "other.md"]:
        bad.append(f"picker: rows are {order}, want folders first then by name")
    if sum(1 for row in up["rows"] if row["stop"]) != 1:
        bad.append(f"picker: {sum(1 for r in up['rows'] if r['stop'])} rows are tab stops, want 1")
    if not up["activeInPicker"]:
        bad.append(f"picker: focus opened on {up['activeClass']!r}, outside the dialog")
    bad = picker_tab_stops(page, win_id, bad)

    # 4. The keyboard is the whole route for a reader with no mouse: the list moves under the
    #    arrows, selection follows the focus, Enter walks into a folder and Backspace comes
    #    back out. Explorer's own list does none of this, which is why it is asserted here.
    page.evaluate(
        "(() => { const win = window.webxp.windows.find((w) => w.id === %s);"
        " win.el.querySelector('.picker-row').focus(); })()" % json.dumps(win_id)
    )
    press_key(page, "ArrowDown")
    time.sleep(0.25)
    moved = picker_state(page, win_id)
    if [row["name"] for row in moved["rows"] if row["selected"]] != ["note.txt"]:
        bad.append(
            "picker: ArrowDown selected "
            f"{[r['name'] for r in moved['rows'] if r['selected']]}, want ['note.txt'] — the "
            "list cannot be walked from the keyboard"
        )
    if moved["name"] != "note.txt":
        bad.append(f"picker: arrowing onto a row left the name box reading {moved['name']!r}")
    page.evaluate(
        "(() => { const win = window.webxp.windows.find((w) => w.id === %s);"
        " [...win.el.querySelectorAll('.picker-row')]"
        "   .find((r) => r.dataset.name === 'inner').focus(); })()" % json.dumps(win_id)
    )
    page.press("Enter")
    time.sleep(0.4)
    inside = picker_state(page, win_id)
    if inside["where"] != f"{PICK_DIR}/inner":
        bad.append(f"picker: Enter on a folder left it showing {inside['where']!r}")
    press_key(page, "Backspace")
    time.sleep(0.4)
    back = picker_state(page, win_id)
    if back["where"] != PICK_DIR:
        bad.append(f"picker: Backspace in the list left it showing {back['where']!r}")

    # 5. Escape is a cancel, and a cancel is `null` rather than a rejection. A caller that
    #    wrapped this in try/catch would never see it.
    page.press("Escape")
    time.sleep(0.4)
    cancelled = picker_state(page, win_id)
    if cancelled["answer"] is not None:
        bad.append(f"picker: Escape answered {cancelled['answer']!r}, want None")
    if cancelled["hasModal"] or cancelled["layers"]:
        bad.append(f"picker: Escape left {cancelled['layers']} layer(s) up")

    # 6. A typed name resolves an absolute path, and the file is read before it resolves.
    typed = start_picker(page, win_id, "pickFile(win, { dir: %s })" % json.dumps(PICK_DIR))
    if not typed["picker"]:
        return bad + ["picker: the second picker did not go up"]
    type_into_name(page, win_id, "note.txt")
    page.press("Enter")
    time.sleep(0.6)
    answered = picker_state(page, win_id)
    if answered["answer"] != f"{PICK_DIR}/note.txt":
        bad.append(f"picker: a typed name answered {answered['answer']!r}")

    # 7. A file the filesystem refuses is refused *here*, where the press was, instead of
    #    resolving a path the caller cannot read. `/etc/secret.conf` is seeded `chmod 000`.
    refused = start_picker(page, win_id, "pickFile(win, { dir: '/etc' })")
    if not refused["picker"]:
        # A failure, not a skip. The two assertions below are the only ones in this check
        # about refusing an unreadable file where the press was, and the dialog going up is
        # their precondition: seed `/etc` itself mode 000 and this block vanished while the
        # suite still printed `[  ok] picker`. Steps 9 and 10 say so; this one did not.
        bad.append("picker: pickFile did not go up on /etc, so a refused file is unasserted")
    else:
        type_into_name(page, win_id, "secret.conf")
        page.press("Enter")
        time.sleep(0.5)
        denied = picker_state(page, win_id)
        if denied["answer"] != "pending":
            bad.append(
                f"picker: an unreadable file answered {denied['answer']!r} — the caller is "
                "handed a path that throws when it reads it"
            )
        if "Permission denied" not in (denied["note"] or ""):
            bad.append(f"picker: the refusal reads {denied['note']!r}")
        page.press("Escape")
        time.sleep(0.3)

    # 8. A kind the caller does not offer is greyed to 50% and refused selection, never
    #    hidden: a reader can see the file is there and is told why they cannot pick it.
    #    win32.run declares this filter, renders it, and never applies it.
    filtered = start_picker(
        page, win_id,
        "pickFile(win, { dir: %s, extensions: ['.txt'] })" % json.dumps(PICK_DIR),
    )
    other = next((row for row in filtered["rows"] if row["name"] == "other.md"), None)
    if other is None:
        bad.append("picker: a filter hid other.md instead of greying it")
    else:
        if not other["refused"]:
            bad.append("picker: other.md is not marked aria-disabled under a .txt filter")
        if not near(other["opacity"], 0.5, 0.01):
            bad.append(f"picker: a refused row is drawn at opacity {other['opacity']}, want 0.5")
        # `centre` and not a second "middle of a row" helper. One picker is up — a window has
        # one modal slot — so the row's own data attribute addresses it exactly.
        click_at(page, *centre(page, '.picker-row[data-name="other.md"]'))
        time.sleep(0.1)
        pressed = picker_state(page, win_id)
        if any(row["selected"] for row in pressed["rows"]):
            bad.append(
                "picker: pressing a refused row selected "
                f"{[r['name'] for r in pressed['rows'] if r['selected']]}"
            )
        if "not a .txt file" not in (pressed["note"] or ""):
            bad.append(f"picker: pressing a refused row said {pressed['note']!r}")
    page.press("Escape")
    time.sleep(0.3)

    # 9. Save is dead in a folder nothing can be written to, before a name is typed. The
    #    repository mounts read-only, so this is the case a reader meets by opening a file
    #    from it and asking to save it back.
    locked = start_picker(page, win_id, "pickSaveAs(win, { dir: '/usr/src/webxp' })")
    if not locked["picker"]:
        bad.append("picker: pickSaveAs did not go up on the mounted repository")
    else:
        if locked["go"] != ["Save", True]:
            bad.append(
                f"picker: in a read-only folder Save reads {locked['go']!r}, want "
                "['Save', True] — a dead button before a name is typed"
            )
        if "read-only" not in (locked["note"] or ""):
            bad.append(f"picker: the read-only folder said {locked['note']!r}")
        # And out of it again by absolute path: the button follows the folder the box names,
        # not the folder on screen.
        type_into_name(page, win_id, "/tmp/picker-elsewhere.txt")
        time.sleep(0.3)
        freed = picker_state(page, win_id)
        if freed["go"] != ["Save", False]:
            bad.append(
                f"picker: an absolute path out of a read-only folder left Save {freed['go']!r}"
            )
        page.press("Escape")
        time.sleep(0.3)

    # 10. Replacing a file is asked inside the picker's own layer, because a window has one
    #     modal slot and `showModal` refuses a second. One layer, and No keeps the dialog up.
    over = start_picker(page, win_id, "pickSaveAs(win, { dir: %s, name: 'note.txt' })"
                        % json.dumps(PICK_DIR))
    if not over["picker"]:
        bad.append("picker: pickSaveAs did not go up over the probe folder")
    else:
        press_in_picker(page, win_id, ".picker-go")
        time.sleep(0.5)
        asked = picker_state(page, win_id)
        if "already exists" not in (asked["ask"] or ""):
            bad.append(
                f"picker: saving over a file asked {asked['ask']!r} — it replaced a file "
                "without a question"
            )
        if asked["layers"] != 1:
            bad.append(
                f"picker: the replace question put {asked['layers']} layers up — a window has "
                "one modal slot"
            )
        if asked["ask"] and not asked["activeInPicker"]:
            bad.append(f"picker: the replace question left focus on {asked['activeClass']!r}")
        if asked["ask"]:
            if not press_in_picker(page, win_id, ".picker-ask .dialog-buttons button", "no"):
                bad.append("picker: the replace question has no No to press")
            time.sleep(0.5)
            kept = picker_state(page, win_id)
            if kept["answer"] != "pending" or not kept["picker"]:
                bad.append(
                    f"picker: No to the replace question answered {kept['answer']!r} and left "
                    f"the picker {'up' if kept['picker'] else 'down'}"
                )
            press_in_picker(page, win_id, ".picker-go")
            time.sleep(0.4)
            press_in_picker(page, win_id, ".picker-ask .dialog-buttons button", "yes")
            time.sleep(0.5)
        replaced = picker_state(page, win_id)
        if replaced["answer"] != f"{PICK_DIR}/note.txt":
            bad.append(f"picker: Yes to the replace question answered {replaced['answer']!r}")
        # Whatever happened, this window must not be left holding a dialog: the checks after
        # this one open their own.
        page.evaluate(
            "(() => { const win = window.webxp.windows.find((w) => w.id === %s);"
            " if (win) win.closeModal(); })()" % json.dumps(win_id)
        )
        time.sleep(0.3)

    # 11. The caller's own options are refused on the caller's own line, synchronously: a
    #     rejection one microtask later is indistinguishable from a cancel in a `catch`.
    outcomes = page.evaluate(
        """(async () => {
          const d = await import('/src/dialogs.mjs');
          const win = window.webxp.windows.find((w) => w.id === %s);
          const out = {};
          for (const [label, run] of [
            ['no window', () => d.pickFile(null, {})],
            ['unknown option', () => d.pickFile(win, { filter: ['.txt'] })],
            ['extension with no dot', () => d.pickFile(win, { extensions: ['txt'] })],
            ['name is a path', () => d.pickSaveAs(win, { name: '/tmp/x.txt' })],
            ['types is empty', () => d.pickSaveAs(win, { types: [] })],
          ]) {
            try { const value = run(); out[label] = 'no throw'; if (value && value.then) value.catch(() => {}); }
            catch (error) { out[label] = error.message; }
          }
          win.closeModal();
          return out;
        })()""" % json.dumps(win_id),
        await_promise=True,
    )
    for label, wanted in (
        ("no window", "needs the XPWindow"),
        ("unknown option", 'unknown option "filter"'),
        ("extension with no dot", "starts with a dot"),
        ("name is a path", "not a path"),
        ("types is empty", "non-empty array"),
    ):
        if wanted not in outcomes.get(label, ""):
            bad.append(f"picker: {label} answered {outcomes.get(label)!r}, want {wanted!r}")

    # Whatever failed above, no window is left holding a dialog. A check that leaves a modal
    # up takes the next one down with it: every later `showModal` on that window throws, and
    # the failures then name this check's subject in another check's voice.
    page.evaluate("(() => { for (const w of window.webxp.windows) w.closeModal(); })()")
    time.sleep(0.2)
    # The shell again, because `expose` above minimised it. `term_run` on a minimised
    # terminal clicks into nothing and hands back the bare prompt, so without this the two
    # cleanups below would report success and delete nothing.
    shell = ready_shell(page)
    term_run(page, f"rm -rf {PICK_DIR}", shell)
    term_run(page, "rm -f /tmp/picker-elsewhere.txt", shell)
    return bad


def wait_for_pad(page, path: str, timeout: float = 10.0) -> str:
    """The id of the Notepad window holding `path`, once its content is in the document.

    A flat sleep is not enough. `desktop.launch` holds its marquee for `minLoadMs` and the
    text area does not exist until `setContent` has run, so measuring too early reported "no
    .notepad-area in the Notepad window" — a harness fault wearing a product failure's
    clothes. A timeout is a failure, not a skip.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'notepad'"
            "   && x.app && x.app.info && x.app.info().path === %s"
            "   && x.el.querySelector('.notepad-area'));"
            " return w ? w.id : null; })()" % json.dumps(path)
        )
        if found:
            return found
        time.sleep(0.2)
    # What it saw, so a stalled launch cannot be mistaken for a picker that did not open.
    # `desktop.launch` awaits its open animation beside `mount`, and a headless run that
    # produces no frames can leave `Animation.finished` unsettled: the window is there, its
    # `mount` has finished, and `setContent` has not run — the body still reads `is-loading`.
    seen = page.evaluate(
        "window.webxp.windows.filter((w) => w.programId === 'notepad')"
        ".map((w) => [w.id, w.titleText, w.bodyEl.className])"
    )
    raise HarnessError(
        f"no Notepad window finished opening on {path} within {timeout}s — Notepad windows "
        f"are {seen}"
    )


def check_notepad_picker(page) -> list[str]:
    """Notepad's File menu: the picker, and the question before a buffer is thrown away.

    Driven through the menu with a real pointer, because the menu is the route a reader has.
    XP's Notepad overwrote and discarded without a word, and win32.run's still does.
    """
    bad = []
    wait_for_apps(page)
    page.evaluate("(() => { for (const w of window.webxp.windows) w.closeModal(); })()")
    shell = ready_shell(page)
    term_run(page, "rm -rf /tmp/pad-probe", shell)
    term_run(page, "mkdir -p /tmp/pad-probe", shell)
    term_run(page, "echo first-file > /tmp/pad-probe/one.txt", shell)
    term_run(page, "echo second-file > /tmp/pad-probe/two.txt", shell)
    listing = term_run(page, "ls /tmp/pad-probe", shell)
    if "one.txt" not in listing or "two.txt" not in listing:
        return bad + [
            "notepad picker: the harness did not create /tmp/pad-probe — the shell listed "
            f"{listing.strip()[:120]!r}"
        ]

    page.evaluate("window.webxp.launch('notepad', { path: '/tmp/pad-probe/one.txt' })")
    win_id = wait_for_pad(page, "/tmp/pad-probe/one.txt")
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " w.moveTo(60, 40); w.sizeTo(620, 440); })()" % json.dumps(win_id)
    )
    time.sleep(0.4)
    # Measured after the move, never before it: the text area's coordinates are where the
    # clicks below land, and a stale pair of them types into whatever is at the old spot.
    pad = notepad_parts(page, win_id)

    # The menu bar is in the client area and the document still has the keyboard: a window
    # that opens with its File menu focused is a window you cannot type in.
    opened = picker_state(page, win_id)
    if "notepad-area" not in (opened["activeClass"] or ""):
        bad.append(f"notepad picker: a new window gave the keyboard to {opened['activeClass']!r}")
    # The bar and the document fill the client area between them, and nothing scrolls.
    # Asserted as the two heights and not only as "it does not overflow": `.window-body` is a
    # block box and the text area is sized `height: 100%`, so a missing row box gives either
    # an overflowing document or — measured — a two-line one in a 390px window, and only one
    # of those shows up as a scroll.
    layout = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " const h = (s) => { const n = w.el.querySelector(s);"
        "   return n ? Math.round(n.getBoundingClientRect().height) : 0; };"
        " return { bar: h('.menu-bar'), area: h('.notepad-area'),"
        "          body: Math.round(w.bodyEl.getBoundingClientRect().height),"
        "          scroll: w.bodyEl.scrollHeight, client: w.bodyEl.clientHeight }; })()"
        % json.dumps(win_id)
    )
    if not near(layout["bar"] + layout["area"], layout["body"], 2):
        bad.append(
            f"notepad picker: a {layout['bar']}px menu bar and a {layout['area']}px document "
            f"in a {layout['body']}px client area — the two do not fill it between them"
        )
    if layout["scroll"] > layout["client"] + 1:
        bad.append(
            f"notepad picker: the client area scrolls — content {layout['scroll']}px in "
            f"{layout['client']}px, so the foot of the document is out of reach"
        )

    # Type into it, then ask for another file: the question comes before the picker.
    raise_window(page, win_id)
    click_at(page, pad["x"], pad["y"])
    page.type_text("unsaved-edit\n")
    time.sleep(0.3)

    def menu_pick(label: str) -> bool:
        """Press File, then the row whose label starts with `label`. Real presses.

        Measured inside this window by id, not by a document-wide selector: an earlier check
        may have opened a Notepad of its own, and a selector that resolves to the first one
        presses another window's File menu while these assertions read this one. That is the
        one thing `centre` cannot express, which is why this reads its own box.
        """
        raise_window(page, win_id)
        bar = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
            " const t = w && w.el.querySelector('.menu-title'); if (!t) return null;"
            " const r = t.getBoundingClientRect();"
            " return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()"
            % json.dumps(win_id)
        )
        if bar is None:
            return False
        click_at(page, bar["x"], bar["y"])
        # The row, inside this window's own popup, and only if it has a box to press.
        spot = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
            " const row = [...w.el.querySelectorAll('.menu-popup .menu-item')]"
            "   .find((r) => r.textContent.startsWith(%s));"
            " if (!row) return null; const r = row.getBoundingClientRect();"
            " if (!r.width || !r.height) return null;"
            " return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()"
            % (json.dumps(win_id), json.dumps(label))
        )
        if spot is None:
            return False
        click_at(page, spot["x"], spot["y"])
        time.sleep(0.5)
        return True

    if not menu_pick("Open"):
        return bad + ["notepad picker: the File menu has no Open row to press"]
    asked = picker_state(page, win_id)
    answers = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " return [...w.el.querySelectorAll('.dialog-buttons button')].map((b) => b.dataset.id); })()"
        % json.dumps(win_id)
    )
    if answers != ["save", "discard", "cancel"]:
        bad.append(
            f"notepad picker: a dirty buffer offered {answers} — XP asked save, discard or "
            "cancel before it threw a document away"
        )
    if asked["picker"]:
        bad.append("notepad picker: the picker went up over an unsaved buffer with no question")
    if "discard" not in answers:
        return bad + [
            f"notepad picker: no discard answer to press — the window offered {answers} with "
            f"the document at {asked['info']!r}"
        ]
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " [...w.el.querySelectorAll('.dialog-buttons button')]"
        "   .find((b) => b.dataset.id === 'discard').click(); })()" % json.dumps(win_id)
    )
    time.sleep(0.9)
    picking = picker_state(page, win_id)
    if not picking["picker"]:
        return bad + ["notepad picker: Don't Save did not reach the picker"]
    # The picker opens where the document lives, which is the only folder the reader has
    # given this window.
    if picking["where"] != "/tmp/pad-probe":
        bad.append(f"notepad picker: Open started in {picking['where']!r}")

    type_into_name(page, win_id, "two.txt")
    page.press("Enter")
    time.sleep(0.9)
    loaded = picker_state(page, win_id)
    if loaded["info"] != {"path": "/tmp/pad-probe/two.txt", "dirty": False, "writable": True}:
        bad.append(f"notepad picker: after Open the document is {loaded['info']!r}")
    if "second-file" not in (loaded["buffer"] or ""):
        bad.append(f"notepad picker: the buffer holds {loaded['buffer']!r}")
    if loaded["title"] != "two.txt - Notepad":
        bad.append(f"notepad picker: the title reads {loaded['title']!r}")
    # One window: File > Open replaces this document, as XP's Notepad did. A second window
    # would be the terminal's `open`, which is a different command.
    if loaded["pads"] != opened["pads"]:
        bad.append(
            f"notepad picker: Open left {loaded['pads']} Notepad windows, was {opened['pads']}"
        )

    # Save As writes through to the one filesystem, and takes the type's extension when the
    # name typed has none — which is why XP's `notes` saved as `notes.txt`.
    click_at(page, pad["x"], pad["y"])
    page.type_text("saved-by-save-as\n")
    time.sleep(0.2)
    if not menu_pick("Save As"):
        return bad + ["notepad picker: the File menu has no Save As row"]
    type_into_name(page, win_id, "/tmp/pad-probe/three")
    page.press("Enter")
    time.sleep(0.9)
    saved = picker_state(page, win_id)
    if saved["info"] != {"path": "/tmp/pad-probe/three.txt", "dirty": False, "writable": True}:
        bad.append(f"notepad picker: after Save As the document is {saved['info']!r}")
    on_disk = term_run(page, "cat /tmp/pad-probe/three.txt", shell)
    if "saved-by-save-as" not in on_disk:
        bad.append(
            f"notepad picker: the shell reads {on_disk.strip()[:120]!r} — Save As wrote into a "
            "different filesystem from the one the terminal holds"
        )

    # Exit with an unsaved buffer: Cancel keeps the window, and keeps the edit.
    click_at(page, pad["x"], pad["y"])
    page.type_text("last-edit\n")
    time.sleep(0.2)
    if not menu_pick("Exit"):
        return bad + ["notepad picker: the File menu has no Exit row"]
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " [...w.el.querySelectorAll('.dialog-buttons button')]"
        "   .find((b) => b.dataset.id === 'cancel').click(); })()" % json.dumps(win_id)
    )
    time.sleep(0.8)
    alive = page.evaluate(
        "Boolean(window.webxp.windows.find((w) => w.id === %s && !w.closed))"
        % json.dumps(win_id)
    )
    if not alive:
        bad.append(
            "notepad picker: Cancel on the way out closed the window anyway, and the edit it "
            "was asked to keep went with it"
        )
    else:
        stayed = picker_state(page, win_id)
        if not stayed["info"] or not stayed["info"]["dirty"]:
            bad.append(
                f"notepad picker: Cancel on the way out left the document {stayed['info']!r}"
            )
        if "last-edit" not in (stayed["buffer"] or ""):
            bad.append("notepad picker: Cancel on the way out lost the edit it was asked to keep")

    # Closed through the frame's own route, which does not ask: the question hangs off
    # File > Exit and this check has already measured it.
    page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.id === %s);"
        " if (w) w.close(); })()" % json.dumps(win_id)
    )
    time.sleep(0.6)
    term_run(page, "rm -rf /tmp/pad-probe", shell)
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
#: flag must report them as failures. These are a minority of the suite, and the gap is
#: recorded in `docs/review.md` rather than papered over: for every other check, "shown
#: capable of failing" rests on a negative control run at the time it was written, or on the
#: review history, rather than on a control the suite re-runs.
INVERTIBLE = (
    check_drag,
    check_reentrant_gesture,
    check_terminal,
    # The three below are picked because inverting them is free: each is one
    # page read with no input and no state change, so the control re-runs on every
    # `--prove-failure` rather than being a measurement somebody took once.
    check_file_associations,
    check_scrollbar_skin,
    check_context_glyphs,
    # `css_ties.check` carries its own inversion: `invert=True` applies its `REVERTS` to the
    # stylesheet text in memory and asserts against the undone tree. Listing it here runs
    # that control on every `--prove-failure` rather than only when somebody runs the file by
    # hand, which is the state it was in when a five-sheet discovery hole went unnoticed.
    check_css_ties,
)
CHECKS = (
    # Beside the other two that read files rather than the page, and first for the same
    # reason: no browser, no port, and a stylesheet decision the whole desktop is drawn from.
    ("css ties", check_css_ties),
    ("manifest is current", check_manifest_current),
    ("mount index is current", check_mount_index),
    ("boot", check_boot),
    # Before the suite opens anything else: it presses a key nothing consumes, and the frame
    # clock it measures is a precondition of every check below. Its docstring holds why.
    ("stray keypress", check_stray_keypress),
    ("launch all", check_launch_all),
    ("app loading", check_app_loading),
    ("app filetypes", check_app_filetypes),
    # Beside `app filetypes`, which is the other half of the same confusion: that one is about
    # the icon a type draws, this one about the program it opens.
    ("file associations", check_file_associations),
    ("autostart", check_autostart),
    ("app styles", check_app_styles),
    ("scrollbar skin", check_scrollbar_skin),
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
    # After `state announcements`, which also drives the Read me window. This one closes that
    # window to prove a pending question is answered by the close, and relaunches it, so it
    # hands the desktop back the way it found it. Before `picker`: both exercise `showModal`,
    # and a broken host should be named by the check about the host, not by the one about its
    # occupant.
    ("modal and message box", check_modal),
    ("icon tokens", check_icon_tokens),
    ("focus order", check_focus_order),
    ("desktop blur", check_desktop_blur),
    ("close", check_close),
    ("singleton", check_singleton),
    ("link schemes", check_link_schemes),
    # The menu bar. Every one of these builds the fixture and resets the bar first, so the
    # order within the group is free except for two pairs:
    #  - `menu geometry` before `menu edges`, because `edges` parks the fixture window in a
    #    corner and `geometry` needs it where it opened;
    #  - `menu bar handed back` last, because it takes the fixture down.
    ("menu roles", check_menu_roles),
    ("menu keyboard", check_menu_keyboard),
    ("menu pointer", check_menu_pointer),
    ("menu activation", check_menu_activation),
    ("menu geometry", check_menu_geometry),
    ("menu edges", check_menu_edges),
    ("menu style", check_menu_style),
    ("menu refusals", check_menu_refusals),
    ("menu bar handed back", check_menu_handed_back),
    # The context menus after the menu bar, and after its teardown: the bar's fixture parks
    # and re-parks a window in the upper left, and `check_context_desktop` raises if anything
    # covers the lower-right point it right-clicks.
    ("context menu on the desktop", check_context_desktop),
    ("context menu by keyboard", check_context_keyboard),
    ("context menu on a window", check_context_window),
    ("context menu teardown", check_context_teardown),
    ("context menu refusals", check_context_refusals),
    ("context menu glyphs", check_context_glyphs),
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
    # Both open their own windows and both need a terminal, which that group has already
    # proved works.
    ("picker", check_picker),
    ("notepad picker", check_notepad_picker),
    ("launch arguments", check_launch_arguments),
    ("filesystem persists", check_fs_persistence),
    # Last: it writes a folder and reloads the page, so it hands the desktop back fresh.
    ("scaffold a new app", check_scaffold),
)

#: The names `--prove-failure` inverts, derived from `INVERTIBLE` rather than listed beside
#: it. The hand-kept list went stale against three other renderings of the same fact, so
#: `--help` told an operator the flag inverted "the drag and shell assertions" and
#: `--only "file associations" --prove-failure` looked invalid.
INVERTIBLE_NAMES = tuple(name for name, check in CHECKS if check in INVERTIBLE)

#: Every invertible check is in `CHECKS`, or `--prove-failure` silently inverts nothing while
#: `INVERTIBLE` still names it. Asserted at import, because a harness that cannot reach what
#: it was pointed at must fail rather than report.
assert len(INVERTIBLE_NAMES) == len(INVERTIBLE), (
    f"{len(INVERTIBLE) - len(INVERTIBLE_NAMES)} of INVERTIBLE is not in CHECKS"
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
        help="invert the assertions of " + ", ".join(INVERTIBLE_NAMES) + ", so the run must "
        "report those failures and no others",
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
            # The renderer treats the page as focused and active for the whole run.
            #
            # Headless Chrome hides the document when the browser gets a key the renderer
            # reported unhandled — `STRAY_KEYS` carries which keys were measured to do it and
            # which were measured not to — and a hidden document runs no frames.
            # `check_stray_keypress` holds where that stalls a launch; without this line the
            # rest of the run drives windows that never finish starting. Measured on a bare
            # `data:text/html,<title>bare</title><body tabindex=0>hello` with no webxp in it,
            # so it is the browser's own window state and not anything in this tree.
            # `setFocusEmulationEnabled` is devtools-protocol-only, which is the other half of
            # that evidence: no page can reach it, so nothing a page does can be the cause.
            # `check_stray_keypress` asserts the frame clock survives such a key, and fails
            # without this line.
            page.call("Emulation.setFocusEmulationEnabled", enabled=True)
            page.navigate(base + "/")
            # The apps have to finish loading before anything is measured.
            time.sleep(1.0)

            inverted_ran: list[str] = []
            inverted_fired: list[str] = []
            #: What the inverted checks themselves put into `failures`, so the rest of
            #: `failures` can be told from it. Without the difference, `--prove-failure`
            #: reads any failure as the one it asked for.
            inverted_failures: list[str] = []
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
                                inverted_failures += [f"{name}: {m}" for m in found]
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

    # `--prove-failure` inverts the exit code: the run is correct when the inversions fired
    # and nothing else did. Asking only "did the flag run" would print "all checks passed"
    # either way, which leaves the one control standing behind every clean run unable to
    # fail itself. A `--only` filter that excludes every check in `INVERTIBLE_NAMES` is an
    # error below rather than a vacuous pass, so narrowing the run cannot quietly disarm it.
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
        # The inversions fired, and nothing else did. Both halves are the control.
        #
        # Returning 0 on "the inversions fired" alone discarded every other failure the run
        # collected — the other checks', the end-of-run console sweep, and the
        # "abandoned after N of M" line — so the flag could not tell a clean tree from one
        # with twenty console errors in it. Measured: a check leaving one uncaught TypeError
        # on the page gave exit 1 plain and exit 0 here, under a printed `[FAIL] console:`.
        stray = Counter(failures) - Counter(inverted_failures)
        if stray:
            print(
                f"\n--prove-failure: {sum(stray.values())} failure(s) outside the inverted "
                "checks, so this run proves nothing about a clean tree:\n  "
                + "\n  ".join(sorted(stray.elements())),
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
