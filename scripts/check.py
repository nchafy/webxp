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
    "about": ("About webxp", False),
}

#: Settling time after an animated state change. `--zoom-ms` is 160; this is comfortably
#: past it plus a frame, without being long enough to hide a hang.
SETTLE_S = 0.45

#: Console messages that are noise rather than a defect. Empty on purpose: this page makes
#: no network requests beyond its own modules, so there is nothing legitimate to filter and
#: a blanket filter is how a real error gets ignored for a week.
IGNORED: tuple[str, ...] = ()


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
    live: (document.querySelector('[aria-live]') || {}).textContent || '',
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
    """One autostarted window, one taskbar button, and the frame actually laid out."""
    bad = []
    state = probe(page)
    if len(state["wins"]) != 1:
        bad.append(f"boot: {len(state['wins'])} windows open, want 1 (Read me autostarts)")
    if state["taskButtons"] != len(state["wins"]):
        bad.append(
            f"boot: {state['taskButtons']} taskbar buttons for {len(state['wins'])} windows"
        )
    win = window_of(state, "readme")
    if win is None:
        return bad + ["boot: Read me did not autostart"]
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
    for program in ("notepad", "display", "windows", "about"):
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
    state = probe(page)
    notepad = need(state, "notepad")

    body = rect_of(page, f"{win_selector(page, 'notepad')} .window-title")
    if body is None:
        return ["focus: Notepad has no laid-out title bar"]
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

    # Notepad is not a singleton, so the same move must open a second one. Without this the
    # check above would also pass on a desktop that refuses to open anything twice.
    launch(page, "notepad")
    after = probe(page)
    notepads = sum(1 for w in after["wins"] if w["program"] == "notepad")
    if notepads != 2:
        bad.append(f"singleton control: {notepads} Notepad windows after a relaunch, want 2")
    return bad


CHECKS = (
    ("boot", check_boot),
    ("launch all", check_launch_all),
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
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--out", default="screenshots")
    parser.add_argument(
        "--prove-failure",
        action="store_true",
        help="invert the drag assertion, so the run must report a failure",
    )
    args = parser.parse_args(argv)

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"

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
            page.call(
                "Emulation.setDeviceMetricsOverride",
                width=width,
                height=height,
                deviceScaleFactor=1,
                mobile=False,
            )
            page.navigate(base + "/")
            # The autostarted window has to finish its zoom and its mount before anything
            # is measured.
            time.sleep(1.0)

            for name, check in CHECKS:
                if check is check_drag and args.prove_failure:
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
