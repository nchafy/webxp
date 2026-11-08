# Review findings

Findings raised against this work, their addresses, and what was done. Fifteen defects in the
code or the harness; all fifteen are fixed, and eleven became checks.

Findings 1–5 are from a self-review. Findings 6–12 are from a second agent that reviewed the
window manager without the author's conclusions, driving the live page rather than reading the
diff. Findings 13–15 came out of porting a real project onto the app contract — 13 from
looking at a screenshot after the suite had already reported the app green.
It found more, and worse, than the self-review did — including a case where the fix for
finding 1 had re-introduced the same defect one call later. That is the argument for the
second reader, and it is recorded here rather than left implied.

Every finding below was verified against the code before it was acted on, and every check
named was shown to fail against the defect it describes.

## 1. A resize could become a move — fixed

**Where** `src/window.mjs`, `applyGesture`.

**Scenario** A window at `y = 30`, 300 px tall. Drag its north grip 200 px up. The old code
derived a position and a size separately: `h = start.h - dy` grew to 500 while `y` went
negative and was then clamped to 0 by `clamp()`. The top stopped at the desktop edge and the
**bottom edge slid down 170 px**. Resizing from the top against the screen edge moved the
window.

**Fix** A resize is computed as four edges. The edge under the pointer is the one that stops
at a clamp or at the minimum; the opposite edge is never recomputed.

**Check** `resize n at desktop edge`. Reverting the clamp reproduces the drift and the check
reports `the bottom edge moved 330 -> 500`.

## 2. A contrast assertion that could not fail — fixed

**Where** `scripts/check.py`, `check_active_styling`.

**Scenario** The check read `color` from `.window-title` to get the title text colour. The
white is set on the `h2` inside it, so the bar itself returns the inherited `rgb(0, 0, 0)`.
The check compared black against the gradient and against itself — it passed on any
stylesheet, including one with white text on a white bar.

**Fix** Reads `getComputedStyle` of the `h2`, per state, and sweeps every gradient stop of
the bar and of all three title boxes.

**Check** `active styling`, now demonstrated capable of failing: restoring Luna's
`#6a9ad8` title-box stop reports `2.91:1, below 4.5:1`.

## 3. Three inherited contrast failures — fixed

**Where** `src/window.css` (title boxes), `src/shell.css` (taskbar buttons).

**Scenario** Carried over from the source theme, which corrected only its close box. White
glyphs and white window titles sat on stops measuring 2.91:1, 2.75:1 and 3.77:1 — all below
SC 1.4.3's 4.5:1.

**Fix and measurements** In `windows.md`. The title-box highlight the gradient loses is put
back as an inset box-shadow on the bevel edge, where it is not under the glyph.

**Check** `active styling`, extended to the taskbar buttons.

## 4. The unfocused window stopped being distinguishable — fixed

**Where** `src/window.css`.

**Scenario** The first fix for the unfocused title bar darkened it to `#66778f` → `#46556e`
so it could hold white text. That passes at 4.55:1 and lands on a navy close enough to the
focused bar that, on a desktop of five cascaded windows, which window has focus is not
answerable at a glance. The check passed and the only job the title bar has had quietly
stopped working.

This is the finding worth keeping in mind: a contrast gate is satisfied by a change that
defeats the thing the colour was carrying. The gate cannot see that.

**Fix** The bar keeps Luna's pale wash and the text goes dark — 6.45:1 and 4.67:1. Focused
is vivid, white-on-navy; unfocused is pale, dark-on-pale. The two now differ in lightness,
saturation and text colour at once.

**Check** `active styling` asserts the two gradients differ and that each state's own text
colour clears AA on every stop of its own bar. It does **not** assert they are
*distinguishable*, which is a judgement no ratio encodes — the screenshots under
`screenshots/` are the record.

## 5. A documented clamp with no check — fixed

**Where** `docs/windows.md` promised 72 px of a window stays reachable; nothing asserted it.

**Fix** `offscreen clamp` drags a window hard at all four desktop edges and requires that it
stays grabbable, that its title bar stays between the top edge and the taskbar, and that the
drag did not resize it.

## 6. The size cap re-introduced finding 1, one call later — fixed

**Where** `src/window.mjs`, `clamp()`.

**Scenario** `applyGesture` was rewritten to compute four edges, which fixed finding 1. But
`apply()` then calls `clamp()`, which capped the size to the desktop (`w = min(w, sw)`) while
leaving `x` and `y` alone — so the anchored edge absorbed the cap and drifted anyway. Two
measured cases on a 1280 × 790 desktop:

- A window at `(300, 500)`, 460 × 400, so its bottom edge is at 900, below the desktop. Drag
  the north grip up 480 px: the **bottom edge moves 900 → 810**.
- A window at `(1000, 200)`, 240 × 300, so its right edge is at 1240. Drag the west grip left
  1150 px: the **right edge moves 1240 → 1130**.

`resize n at desktop edge` could not see either, because it starts with the window fully
inside the desktop and the cap never binds.

**Fix** The size cap is gone. It cannot know which edge the pointer is holding, so it can
only ever move the wrong one, and a window larger than the desktop is legitimate — XP allowed
it. The position clamp still keeps the title bar reachable.

**Check** `resize past the desktop`, which sets up both measured cases and asserts the
anchored edge. Restoring the cap reproduces `the bottom edge moved 900 -> 810`.

This is the finding most worth keeping: a fix verified by a check written against the same
mental model as the fix will agree with it. The check passed because it tested the case the
author was thinking about.

## 7. A throwing `mount` stranded the window forever — fixed

**Where** `src/desktop.mjs`, `launch`.

**Scenario** `Promise.all([program.mount(...), zoom(...)])` had no `catch`, and neither the
Start menu nor the desktop-icon caller had one. A program whose `mount` threw left
`.window-body.is-loading` on screen indefinitely, with an `Uncaught (in promise)` in the
console and a window that looked like it was still working. `autostart` awaits each launch in
sequence, so one bad program also silently cancelled every program after it.

**Fix** `launch` catches, and the window shows what failed: the error message, the program
id, and a pointer to the console for the stack. The error is still re-reported to the console
— a swallowed failure is a defect. `autostart` isolates each program.

**Check** `mount failure`, which registers a program that throws and requires the window to
name the error. Removing the `catch` reports `the window is still showing the loading
marquee`.

## 8. A window could be stranded off-screen by resizing the browser — fixed

**Where** `src/desktop.mjs`; there was no `resize` listener at all.

**Scenario** `clamp` ran only when a window moved. A window at `(1000, 600)` on a desktop
shrunk to 520 × 390 stayed at `(1000, 600)`: horizontal overlap with the desktop measured
**−480 px**, so the window and the title bar that is its only move handle were both entirely
off the surface, with no pointer or keyboard route back. Exactly the trap this project claims
to have removed from XP, reachable by dragging the browser's corner.

**Fix** `watchViewport` re-clamps every non-maximised window on resize, coalesced to one
frame.

**Check** `viewport shrink`. Disabling the listener reports `only -480px of the window is
over a 520px desktop`.

## 9. The title-bar icon squashed to 4.2 px — fixed

**Where** `src/window.css` had a `.window-icon` rule; `icons.mjs` sets `class="xp-icon"`.

**Scenario** The rule matched nothing, so the title-bar icon was a flex item with the default
`flex-shrink: 1`. A long title in a 220 px window squashed it to **4.2 px × 16 px**. The
taskbar and Start menu icons were spared only because `shell.css` happened to set
`flex: 0 0 auto` on their `svg` separately.

**Fix** One `.xp-icon` rule in `theme.css` carries `display: block; flex: 0 0 auto`, and the
three duplicate per-context rules are gone. One definition, since every icon wants the same
thing.

## 10. `register()` after construction was invisible — fixed

**Where** `src/desktop.mjs`, `register`.

**Scenario** `renderStartMenu` and `renderDesktopIcons` ran once, in the constructor. A
program registered later was launchable only from code, while the JSDoc presents `register`
as the public way to add one.

**Fix** `register` re-renders when the desktop is past construction; a `ready` flag keeps the
constructor's own loop from rendering once per program.

**Check** `mount failure` registers its program after construction and asserts it appears in
the Start menu.

## 11. `check_close` did not close the active window — fixed

**Where** `scripts/check.py`, `check_close`.

**Scenario** It ran straight after `desktop blur`, which asserts nothing is active. So the
window it closed was never the active one, and its assertion that "nothing took focus after
the active window closed" was satisfied through the non-active path. Closing the active window
— the case the docstring names — was unasserted, as was whether focus lands on the *topmost*
survivor rather than any survivor.

**Fix** The check focuses the window first, computes which survivor is topmost by z, and
asserts focus lands on that one.

## 12. Cell links accepted any URL scheme — fixed

**Where** `src/widgets.mjs`, `fillCell`.

**Scenario** `table` and `group` are documented as the shared renderer for a record, and
`a.href = href` was unguarded. A cell value of `['click me', 'javascript:…']` produced a link
that executes on activation. Every caller today passes a literal from program source, so this
was reachable only by a future caller — which is what a shared renderer invites.

**Fix** An allowlist: `http:`, `https:`, `mailto:`, and relatives resolved against
`document.baseURI`. A refused href degrades to its text and reports to the console.

**Check** `link schemes`, which asserts `javascript:` and `data:` are refused, that the text
survives the refusal, that the script did not run, **and** that https, mailto and relative
hrefs still work — without that last part the check would pass on a renderer that refuses
every link.

## 13. A map that reported healthy and drew nothing — fixed

**Where** `apps/basemap/app.css`.

**Scenario** Found by looking at a screenshot, after the check suite reported the app green.

The original project relied on `.window-body > .map` outranking MapLibre's own
`.maplibregl-map` to keep `position: absolute; inset: 0` on the map container. Scoping both
rules under `.app-basemap` made them **equal** at two classes, so source order decided it —
and the vendored MapLibre block sits at the foot of the file. `position: relative` won,
`inset: 0` stopped applying, and the container collapsed to 0 px high while the canvas inside
kept a stale 758 × 300.

The window drew nothing but its holding colour. Meanwhile MapLibre reported
`isStyleLoaded: true`, `areTilesLoaded: true`, 113 layers, all three sources loaded, a
758 × 300 drawing buffer, and not one console error.

The comment above the rule in the file I wrote describes this exact hazard. The code fell
into it anyway.

**Confirmed by controlled comparison.** The original page, served and screenshotted in the
same headless Chrome, renders a complete map. So this was the port, not the environment —
which is what I would otherwise have assumed, and would have been wrong.

**Fix** `.app-basemap .basemap-root > .map`, three classes, which outranks the vendored rule
regardless of order.

**Check** `basemap app`, which had passed over this and now cannot:
- the canvas is measured against the **window**, not against `.map` — comparing the canvas to
  the element a stylesheet accident collapsed is an assertion that agrees with itself;
- `.map` itself must have a box, and the failure message names the specificity battle;
- the canvas region is screenshotted and its compressed size asserted. Blank measured 14,810
  bytes, drawn 446,833.

All three fire on the defect. The third is the one that would catch a *different* cause of a
blank map.

## 14. `ready` — the hook that was missing

**Where** `src/desktop.mjs`, `src/apps.mjs`.

**Scenario** Not a defect so much as a gap the port exposed. `mount` cannot measure anything:
what it returns is what gets inserted, so during `mount` the nodes have no box. MapLibre
measures its container on creation, so building the map in `mount` sized it to nothing.

The first attempt was for the app to call `win.setContent` itself mid-`mount` — which worked
and was wrong: it set the content twice and tore up the loading-marquee timing for every app.

**Fix** An optional `ready({ win, desktop })`, called after the content is in the document and
two frames have passed. A throwing `ready` is reported like a throwing `mount`.

## 15. A cached rejection poisoned an app for the session — fixed

**Where** `src/apps.mjs`, `addStylesheet`.

**Scenario** Surfaced by the suite as five identical mount failures for Notepad —
`stylesheet failed to load: /apps/notepad/app.css` — for a file that answers 200 on request.

One request was dropped, almost certainly while the Base map app saturated the connection
pool with a 1.1 MB module and a burst of tiles. The rejected promise stayed in the `linked`
map, so every later launch of that app replayed the same rejection and showed the same error
window, forever, with the stylesheet sitting there perfectly available.

**Fix** On error the entry is deleted and the dead `<link>` removed, so reopening the app
retries. A declared stylesheet that genuinely does not exist still fails loudly — that is
worth keeping — but a blip is no longer permanent.

**Residual uncertainty:** the underlying dropped request was observed once and not
reproduced. The fix addresses the amplification, which is the part that turned one blip into a
dead app, not the blip itself.

## Two harness bugs found by running it

Neither was a product defect, and both are worth recording because they are the shape of
mistake that makes a suite report clean over broken behaviour.

- `check_loading_frame` first launched `about`, a singleton. Relaunching a singleton focuses
  the window that is already open and already filled, which is indistinguishable from a
  loading floor that does not work. It now uses a non-singleton program.
- The same check closed the window a later check depended on. It now records window ids
  before it runs and closes only what it opened.

## Not addressed

- **The Start button fails SC 1.4.3 at 2.16:1.** Deliberate, excluded from the sweep, and
  recorded with its number in `windows.md`. It is the most recognisable element of the theme
  and a green dark enough to hold white stops reading as XP. Known defect, not an oversight.
- **No check covers touch.** `pointercancel` is handled in `beginGesture` because a touch
  drag the browser turns into a scroll fires it instead of `pointerup`, but the harness
  dispatches mouse events only. The handling is reasoned, not observed.
- **No check covers `prefers-reduced-motion`.** The zoom is skipped rather than shortened,
  and the marquee has a CSS fallback, but neither path is exercised.
- **App CSS scoping is a convention, not an enforcement.** `docs/apps.md` requires every rule
  to sit under `.app-<id>` and `app styles` checks that one app's class does not land on
  another's window, but nothing stops an app shipping a bare `p { ... }`. `@scope` or a build
  step would enforce it; neither is worth it before there are more apps.
- **`basemap app` needs the network.** It reaches the real tile services, because no fixture
  could tell us a source stopped answering. It reports a network failure as one rather than as
  a defect, but it is the one check that cannot run offline.
