# Review findings

Findings raised against this extraction, their addresses, and what was done. Five were
defects in the code or in the harness; all five are fixed and four became checks.

A second-agent review was dispatched and had not reported when the work was handed over, so
every finding below is from a self-review. That is the weaker arrangement — an author reads
their own change with the eyes least able to see what is missing — and it is recorded here
rather than left implied.

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
