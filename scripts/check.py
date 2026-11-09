#!/usr/bin/env python3
"""Drive the demo desktop in headless Chrome and assert the window behaviour.

A window manager cannot be checked by screenshot. An inert frame and a working one are the
same picture; what separates them is whether the geometry changed after a drag. So every
check here dispatches real input through CDP and then reads the numbers back off the
window's own state.

    .venv/bin/python scripts/check.py [--port 8099] [--out screenshots]

Two flags exist to keep the harness honest:

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
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp import Browser, CDPError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

#: One desktop size. A window manager's behaviour is not viewport-dependent the way a
#: stylesheet's is, and the narrow case that does matter — content inside a small window —
#: is exercised by resizing a window rather than by shrinking the screen.
VIEWPORT = (1280, 820)

#: The minimums `src/window.mjs` exports. Duplicated here because a check that imports the
#: value it is checking cannot catch the value changing.
MIN_W = 220
MIN_H = 120

#: Every program `demo/programs.mjs` registers, and whether it is resizable. The names are
#: the window titles at launch.
PROGRAMS = {
    "readme": ("Read me", True),
    "notepad": ("Untitled - Notepad", True),
    "display": ("Display Properties", True),
    "windows": ("Window list", True),
    "webview": ("Webview", True),
    "basemap": ("Base map", True),
    "map-challenge": ("30DaysOfMaps", True),
    "terminal": ("Terminal", True),
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
#: console. Geometry is read from `w.box`, which is what the window actually believes, and
#: from `getBoundingClientRect`, which is what the browser laid out. Both, because a box
#: the code updated and the layout ignored is exactly the bug a screenshot misses.
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
        box: { ...w.box },
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


def mouse(page, event_type: str, x: float, y: float, *, buttons: int = 0) -> None:
    page.call(
        "Input.dispatchMouseEvent",
        type=event_type,
        x=float(x),
        y=float(y),
        button="left",
        buttons=buttons,
        clickCount=1,
    )


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


def probe(page) -> dict:
    state = page.evaluate(PROBE)
    if state.get("error"):
        raise CDPError(f"probe failed: {state['error']}")
    return state


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
    """Open a program through the Start menu, so the menu is covered too."""
    page.evaluate("document.getElementById('start').click()")
    time.sleep(0.05)
    clicked = page.evaluate(
        "(() => { const items = [...document.querySelectorAll('.start-item')];"
        " const want = %s;"
        " const hit = items.find((b) => b.textContent.includes(want));"
        " if (!hit) return false; hit.click(); return true; })()"
        % json.dumps(PROGRAMS[program][0])
    )
    if not clicked:
        raise CDPError(f"no Start menu item matching {program!r}")
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


# ---------------------------------------------------------------------------
# Contrast. The stylesheets claim specific ratios in their comments, as the reason for
# departing from Luna's own values; these turn the claim into something that fails.
# ---------------------------------------------------------------------------

RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")

#: WCAG 2.2 SC 1.4.3 for text below 18.66px bold / 24px regular.
AA_NORMAL = 4.5


def rgb_stops(css: str) -> list[tuple[int, int, int]]:
    """Every colour in a resolved `background-image` or `background-color`."""
    return [(int(r), int(g), int(b)) for r, g, b in RGB.findall(css or "")]


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
                    "terminal", "about"):
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
    origins = {(w["box"]["x"], w["box"]["y"]) for w in state["wins"]}
    if len(origins) != len(state["wins"]):
        bad.append(f"launch: {len(state['wins'])} windows at {len(origins)} distinct origins")

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
    bars = page.evaluate(
        "(() => { const d = window.webxp;"
        " const pick = (want) => {"
        "   const w = d.windows.find((w) => (d.active === w) === want && w.state !== 'min');"
        "   if (!w) return null;"
        "   const bar = w.el.querySelector('.window-title');"
        "   const heading = bar.querySelector('h2');"
        "   const boxes = [...w.el.querySelectorAll('.title-box')].map((b) => {"
        "     const s = getComputedStyle(b);"
        "     return { label: b.getAttribute('aria-label'), colour: s.color,"
        "              fill: s.backgroundImage + ' ' + s.backgroundColor }; });"
        "   return { program: w.programId,"
        "            bar: getComputedStyle(bar).backgroundImage,"
        # The white text lives on the heading, not on the bar. Reading `color` off the bar
        # returns the inherited black and compares black to black, which is a check that
        # cannot fail — the first version of this did exactly that.
        "            textColour: getComputedStyle(heading).color,"
        "            boxes }; };"
        " return { active: pick(true), idle: pick(false) }; })()"
    )
    if not bars.get("active") or not bars.get("idle"):
        return ["active styling: need one focused and one unfocused window to compare"]

    bad = []
    if bars["active"]["bar"] == bars["idle"]["bar"]:
        bad.append(
            "active styling: the focused and unfocused title bars resolve to the same "
            f"gradient ({bars['active']['bar'][:60]}…)"
        )

    # Luna's own unfocused bar kept near-white text on a pale blue and measured about
    # 2.3:1. Both of these carry white text, so every stop of both gradients has to hold it
    # — including the stop in the middle, which is where a four-stop gradient is lightest.
    for state in ("active", "idle"):
        text = rgb_stops(bars[state]["textColour"])
        if not text:
            bad.append(f"contrast: could not read the {state} title colour")
            continue
        for stop in rgb_stops(bars[state]["bar"]):
            ratio = contrast(text[0], stop)
            if ratio < AA_NORMAL:
                bad.append(
                    f"contrast: {state} title text {text[0]} on bar stop {stop} is "
                    f"{ratio:.2f}:1, below {AA_NORMAL}:1"
                )
        for box in bars[state]["boxes"]:
            glyph = rgb_stops(box["colour"])
            if not glyph:
                continue
            for stop in rgb_stops(box["fill"]):
                # A fully transparent background resolves to rgba(0,0,0,0) and is not a
                # surface the glyph sits on.
                if stop == (0, 0, 0) and "rgba(0, 0, 0, 0)" in box["fill"]:
                    continue
                ratio = contrast(glyph[0], stop)
                if ratio < AA_NORMAL:
                    bad.append(
                        f"contrast: {state} {box['label']!r} glyph {glyph[0]} on {stop} is "
                        f"{ratio:.2f}:1, below {AA_NORMAL}:1"
                    )

    # The taskbar buttons carry window titles, which is text a reader has to pick a window
    # out of. Both states, because the pressed one is a different gradient.
    #
    # The Start button is deliberately not in this sweep. Its white italic on Luna green
    # measures 2.16:1 and is the one place the imitation is kept over the correction;
    # `docs/windows.md` records that with the number.
    buttons = page.evaluate(
        "[...document.querySelectorAll('.task-button')].map((b) => {"
        " const s = getComputedStyle(b);"
        " return { pressed: b.getAttribute('aria-pressed'), colour: s.color,"
        "          fill: s.backgroundImage + ' ' + s.backgroundColor }; })"
    )
    for button in buttons:
        text = rgb_stops(button["colour"])
        if not text:
            continue
        for stop in rgb_stops(button["fill"]):
            if stop == (0, 0, 0) and "rgba(0, 0, 0, 0)" in button["fill"]:
                continue
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
    want_x = before["box"]["x"] + dx
    want_y = before["box"]["y"] + dy
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
    drag(page, grip["x"] + grip["w"] / 2, grip["y"] + grip["h"] / 2, dx, dy)

    after = need(probe(page), "readme")
    if not near(after["box"]["w"], before["box"]["w"] + dx):
        bad.append(f"resize se: width {before['box']['w']} -> {after['box']['w']}, want +{dx}")
    if not near(after["box"]["h"], before["box"]["h"] + dy):
        bad.append(f"resize se: height {before['box']['h']} -> {after['box']['h']}, want +{dy}")
    if after["box"]["x"] != before["box"]["x"] or after["box"]["y"] != before["box"]["y"]:
        bad.append("resize se: the origin moved, so the window slid instead of growing")
    # The layout has to follow the state.
    if not near(after["rect"]["w"], after["box"]["w"]) or not near(after["rect"]["h"], after["box"]["h"]):
        bad.append(
            f"resize se: believed {after['box']['w']}x{after['box']['h']} but laid out "
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
    if not near(after["box"]["x"], before["box"]["x"] + dx):
        bad.append(f"resize w: left edge {before['box']['x']} -> {after['box']['x']}, want +{dx}")
    if not near(after["box"]["w"], before["box"]["w"] - dx):
        bad.append(f"resize w: width {before['box']['w']} -> {after['box']['w']}, want -{dx}")
    right_before = before["box"]["x"] + before["box"]["w"]
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
    if after["box"]["y"] != 0:
        bad.append(f"resize n: top is {after['box']['y']}, want 0 at the desktop edge")
    bottom_before = before["box"]["y"] + before["box"]["h"]
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
        box = win["box"]

        # Horizontally: some of the window has to remain over the desktop.
        visible = min(box["x"] + box["w"], surface["w"]) - max(box["x"], 0)
        if visible < 60:
            bad.append(
                f"clamp {label}: only {round(visible)}px of the window is over the desktop"
            )
        if box["y"] < 0:
            bad.append(f"clamp {label}: title bar is at y={box['y']}, above the desktop")
        # And the title bar has to be on the desktop, not under the taskbar.
        if box["y"] > surface["h"] - 20:
            bad.append(
                f"clamp {label}: title bar at y={box['y']} is below the desktop's "
                f"{round(surface['h'])}px"
            )
        # The drag must not have resized anything.
        if box["w"] != 400 or box["h"] != 260:
            bad.append(f"clamp {label}: the window resized to {box['w']}x{box['h']}")
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
    bottom_before = before["box"]["y"] + before["box"]["h"]
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
    right_before = before["box"]["x"] + before["box"]["w"]
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
    width, height = VIEWPORT
    try:
        page.call(
            "Emulation.setDeviceMetricsOverride",
            width=520, height=420, deviceScaleFactor=1, mobile=False,
        )
        # Past the coalescing frame.
        time.sleep(0.4)
        state = probe(page)
        win = need(state, "readme")
        surface = state["surface"]
        visible = min(win["box"]["x"] + win["box"]["w"], surface["w"]) - max(win["box"]["x"], 0)
        if visible < 60:
            bad.append(
                f"viewport shrink: only {round(visible)}px of the window is over a "
                f"{round(surface['w'])}px desktop — it cannot be dragged back"
            )
        if win["box"]["y"] > surface["h"] - 20:
            bad.append(
                f"viewport shrink: title bar at y={win['box']['y']} is below the "
                f"{round(surface['h'])}px desktop"
            )
    finally:
        page.call(
            "Emulation.setDeviceMetricsOverride",
            width=width, height=height, deviceScaleFactor=1, mobile=False,
        )
        time.sleep(0.4)
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
        " const { iconFor, FILETYPES, FILETYPE_NAMES } = await import('/src/icons.mjs');"
        " return {"
        "   byFiletype: iconFor({ filetype: 'map' }),"
        "   override: iconFor({ filetype: 'map', icon: 'check' }),"
        "   neither: iconFor({}),"
        "   filetypes: FILETYPE_NAMES,"
        "   mapIcon: FILETYPES.map.icon,"
        "   badFiletype: validate({ id: 'x', name: 'X', mount(){}, filetype: 'nope' }, 'x'),"
        "   goodFiletype: validate({ id: 'x', name: 'X', mount(){}, filetype: 'map' }, 'x'),"
        "   registered: [...window.webxp.programs.entries()]"
        "     .map(([id, p]) => [id, p.filetype || null, p.icon]) }; })()",
        await_promise=True,
    )
    bad = []
    if result["byFiletype"] != result["mapIcon"]:
        bad.append(f"app filetypes: filetype 'map' gave {result['byFiletype']!r}, want the map icon")
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
            # What sits on top of the credit's own midpoint. If anything but the credit is
            # there, it is covered — which is a licence problem, not a layout nit.
            " const onCredit = tr && tr.width"
            "   ? (document.elementFromPoint(tr.left + tr.width / 2, tr.top + tr.height / 2)"
            "      || {}).className || ''"
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

    page.evaluate(
        "(() => { const w = window.webxp.windows.find((w) => w.programId === 'basemap');"
        " w.bodyEl.querySelector('[data-basemap=\"satellite\"]').click(); })()"
    )
    deadline = time.monotonic() + 45
    switched = None
    while time.monotonic() < deadline:
        switched = page.evaluate(
            "(() => { const w = window.webxp.windows.find((w) => w.programId === 'basemap');"
            " const map = w.base.map;"
            " return { basemap: w.base.basemap, loaded: map.loaded(),"
            "   layers: map.getStyle() ? map.getStyle().layers.map((l) => l.id) : [] }; })()"
        )
        if switched["basemap"] == "satellite" and switched["loaded"]:
            break
        time.sleep(0.5)

    if not switched or switched["basemap"] != "satellite":
        bad.append(f"basemap app: the satellite switch did not take — {switched}")
    else:
        missing = overlay - set(switched["layers"])
        if missing:
            bad.append(
                f"basemap app: overlay layers {sorted(missing)} were discarded by the basemap "
                "switch — draw() was not re-run"
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
    if after["box"]["w"] != MIN_W:
        bad.append(f"min size: width clamped to {after['box']['w']}, want {MIN_W}")
    if after["box"]["h"] != MIN_H:
        bad.append(f"min size: height clamped to {after['box']['h']}, want {MIN_H}")
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
    if not near(after["box"]["x"], before["box"]["x"] + 30) or not near(
        after["box"]["y"], before["box"]["y"] + 20
    ):
        bad.append(
            f"keyboard move: {before['box']['x']},{before['box']['y']} -> "
            f"{after['box']['x']},{after['box']['y']}, want +30,+20"
        )

    sized = need(probe(page), "readme")
    for _ in range(3):
        arrow(page, "ArrowRight", shift=True)
    grown = need(probe(page), "readme")
    if not near(grown["box"]["w"], sized["box"]["w"] + 30):
        bad.append(
            f"keyboard resize: width {sized['box']['w']} -> {grown['box']['w']}, want +30"
        )
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
PANEL_SIZES = ((420, 140), (240, 200), (820, 200))


def day_02_panel_sweep(page) -> list[str]:
    """Open the drill-down panel and check it covers neither pane of the status bar.

    The rest of `check_day_02` tests one window size with the panel shut, which is exactly
    the gap this closes: the panel is `position: absolute; z-index: 5` and `.map-bar` is not
    positioned, so anything overflowing the panel paints over the credit and the live region.
    """
    bad = []
    opened = page.evaluate(
        "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
        " if (!w || !w.lines) return 'no day 02 window';"
        " const cell = w.lines.firstCellWithPasses();"
        " if (!cell) return 'no segment with crossings in the loaded data';"
        " w.lines.openSegment(cell); return ''; })()"
    )
    if opened:
        # Not a product failure and not silence: the committed synthetic data carries no
        # street file, so this cannot run on a clean clone. Say which it was.
        return [f"day 02 panel: skipped — {opened}. Not a layout result either way."]
    time.sleep(1.0)

    for width, height in PANEL_SIZES:
        page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " if (w.state === 'max') w.toggleMaximize();"
            " w.el.style.left = '10px'; w.el.style.top = '10px';"
            " w.el.style.width = %d + 'px'; w.el.style.height = %d + 'px'; })()"
            % (width, height)
        )
        time.sleep(0.15)
        hit = page.evaluate(
            "(() => { const w = window.webxp.windows.find((x) => x.programId === 'day-02-lines');"
            " const at = (sel) => { const e = w.bodyEl.querySelector(sel);"
            "   if (!e) return 'missing';"
            "   const r = e.getBoundingClientRect();"
            "   if (!r.width || !r.height) return 'zero';"
            "   const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
            "   return top ? String(top.className || top.tagName) : 'nothing'; };"
            " return { status: at('.map-status'), credit: at('.map-attrib'),"
            "   panelShut: !!w.bodyEl.querySelector('.detail').hidden }; })()"
        )
        if hit["panelShut"]:
            bad.append(f"day 02 panel: the panel closed itself at {width}x{height}")
            continue
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
        console_errors(page)
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
  const { VFS, MAX_FILE_SIZE, normalize } = await import('/shared/fs/vfs.mjs');
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

  // A batch notifies once, at the end, with the tree whole. Un-batched, the first mutation
  // of a reseed told every watcher the filesystem was empty.
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
    # check runs next.
    console_errors(page)


def check_filesystem(page) -> list[str]:
    """The filesystem model: resolution, symlinks, links, modes, serialisation."""
    fs_reset(page)
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
    return page.evaluate(
        "(() => { const pads = window.webxp.windows.filter((w) => w.programId === 'notepad');"
        " const w = %s ? pads.find((p) => p.id === %s) : pads[pads.length - 1];"
        " if (!w) return null;"
        " const area = w.el.querySelector('.notepad-area');"
        " const r = area ? area.getBoundingClientRect() : null;"
        " return { id: w.id, title: w.titleText, value: area ? area.value : null,"
        "          status: w.statusEl ? w.statusEl.textContent : '',"
        "          x: r ? r.left + 20 : 0, y: r ? r.top + 10 : 0 }; })()"
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
    ("cd ..", [], []),
    ("pwd", ["/home/user/links"], ["relative-dir"]),
    ("cd ~", [], []),
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
    ("echo appended >> /tmp/probe.txt", [], []),
    ("wc -l < /tmp/probe.txt", ["2"], []),
    ("ls /etc | wc -l", ["4"], []),
    ("mkdir -p /tmp/a/b/c", [], []),
    ("find /tmp -type d", ["/tmp/a/b/c"], []),
    ("rm -r /tmp/a", [], []),
    ("find /tmp -type d", [], ["/tmp/a"]),
    # A directory copied into its own subtree never terminated: the copy created a child,
    # the walk reached it, and it copied that too. Both edges, because a guard that refuses
    # everything is also a guard.
    ("mkdir -p /tmp/src/inner", [], []),
    ("cp -r /tmp/src /tmp/src/copy", ["into itself"], []),
    ("cp -r /tmp/src /tmp/elsewhere", [], ["into itself"]),
    ("ls /tmp/elsewhere", ["inner"], []),
    ("ln -s /etc/hosts /tmp/h", [], []),
    ("readlink /tmp/h", ["/etc/hosts"], []),
    ("realpath /tmp/h", ["/etc/hosts"], []),
    ("rm /tmp/h", [], []),
    ("cat /etc/hosts", ["localhost"], []),
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


def check_open_across_apps(page) -> list[str]:
    """`open` hands another app a path, and both apps turn out to hold one filesystem.

    This is the claim the single mount in `shared/fs/fs.mjs` makes, and it cannot be checked
    inside either app: the terminal writes, Notepad reads, Notepad writes, the terminal
    reads. Anything less is two apps that each work.
    """
    bad = []
    shell = term_parts(page)["id"]
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
    page.evaluate("window.webxp.launch('terminal', { cwd: '/etc' })")
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
    console_errors(page)
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
    page.evaluate("window.webxp.launch('terminal', { cwd: '/home/user/notes' })")
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


CHECKS = (
    ("manifest is current", check_manifest_current),
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
    ("minimum size", check_min_size),
    ("container query", check_container_query),
    ("minimise and restore", check_minimize_restore),
    ("taskbar toggle", check_taskbar_toggles),
    ("maximise", check_maximize),
    ("keyboard move and resize", check_keyboard),
    ("focus order", check_focus_order),
    ("desktop blur", check_desktop_blur),
    ("close", check_close),
    ("singleton", check_singleton),
    ("link schemes", check_link_schemes),
    # Last, because it registers a program and provokes a console error. Its cleanup puts
    # the desktop back, but a check that deliberately breaks something belongs at the end.
    ("mount failure", check_mount_failure),
    # The filesystem group. After the window-manager checks because the first of these
    # reloads the page to clear the stored tree, and a reload closes every window the
    # earlier checks opened.
    ("filesystem", check_filesystem),
    ("terminal", check_terminal),
    ("open across apps", check_open_across_apps),
    ("launch arguments", check_launch_arguments),
    ("filesystem persists", check_fs_persistence),
    # Last: it writes a folder and reloads the page, so it hands the desktop back fresh.
    ("scaffold a new app", check_scaffold),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
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
    base = f"http://127.0.0.1:{args.port}"
    global BASE_URL
    BASE_URL = base

    # Loopback only. Nothing here is meant to be reachable from another machine.
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    failures: list[str] = []
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/", timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        else:
            print("server did not come up", file=sys.stderr)
            return 1

        width, height = VIEWPORT
        with Browser() as browser:
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

            for name, check in checks:
                if args.prove_failure and check in (check_drag, check_terminal):
                    found = check(page, invert=True)
                else:
                    found = check(page)
                failures += [f"{name}: {message}" for message in found]
                mark = "FAIL" if found else "ok"
                print(f"  [{mark:>4}] {name}")
                for message in found:
                    print(f"         {message}")
                shoot(page, out_dir / f"{name.replace(' ', '-').replace('(', '').replace(')', '')}.png")

            errors = console_errors(page)
            failures += [f"console: {message}" for message in errors]
            for message in errors:
                print(f"  [FAIL] console: {message}")

            size = shoot(page, out_dir / "desktop.png")
            print(f"\n  desktop.png  {size // 1024} KB")
    finally:
        server.terminate()
        server.wait(timeout=10)

    if failures:
        print(f"\n{len(failures)} failure(s)", file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
