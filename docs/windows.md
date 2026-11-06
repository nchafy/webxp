# The window model

What a window is, what it guarantees, and where it departs from Windows XP.

Every claim about behaviour here is asserted by `scripts/check.py`, and the check name is
given beside it. A claim with no check beside it is a claim nobody has measured.

## States

A window is in exactly one of three states, recorded in `XPWindow.state`:

| State    | Meaning                                                    |
| -------- | ---------------------------------------------------------- |
| `normal` | Positioned and sized from `XPWindow.box`.                  |
| `min`    | Out of the layout and out of the tab order. Button remains. |
| `max`    | Filling the desktop. `box` is untouched, which is what makes a restore exact. |

`box` is the single source of truth for geometry. It is never written while the window is
maximised, so un-maximising needs no second copy of the numbers and cannot disagree with
one (`maximise`).

Geometry reaches the element as four custom properties — `--win-x`, `--win-y`, `--win-w`,
`--win-h` — written with `style.setProperty`. A `style` attribute would need `unsafe-inline`
in a `style-src` policy; the CSSOM does not. `transform` is deliberately left free of
geometry, because the open and minimise animations use it.

## Gestures

| Gesture                      | Does                                | Check |
| ---------------------------- | ----------------------------------- | ----- |
| Drag the title bar           | Moves the window                    | `drag` |
| Drag any of eight edges      | Resizes from that edge              | `resize se`, `resize w`, `resize n at desktop edge` |
| Double-click the title bar   | Maximise, or restore                | `maximise` |
| Click a title box            | Minimise, maximise, close           | `minimise and restore`, `maximise`, `close` |
| Click a taskbar button       | Raise it, or put the front one away | `taskbar toggle` |
| Click anywhere in a window   | Raises it                           | `focus order` |
| Click bare desktop           | Clears the focus                    | `desktop blur` |

The title bar is the only move handle. `drag is title-bar only` is the negative control
for that: it performs the identical drag starting inside the window body and requires that
nothing moves. Without it, a passing `drag` would not rule out a probe that reports movement
whatever you do.

### Keyboard

A drag is a pointer gesture, which SC 2.1.1 requires an equivalent for. The title bar is in
the tab order, and once focused:

| Key                   | Does                          |
| --------------------- | ----------------------------- |
| Arrow keys            | Move ten pixels               |
| Shift and arrow keys  | Resize ten pixels             |
| Enter or Space        | Maximise, or restore          |

Asserted by `keyboard move and resize`, which also asserts that a shifted arrow sizes the
window without moving its origin.

## Clamps

- Minimum size is 220 × 120 (`MIN_W`, `MIN_H` in `src/window.mjs`). Dragging a corner past
  it stops at it rather than inverting the window (`minimum size`).
- At least 72 px of a window stays horizontally on the desktop, and the title bar stays
  between the top edge and the taskbar (`offscreen clamp`, which drags a window hard at all
  four edges and requires it to remain grabbable and unresized). See the departures below.
- A resize is computed as four edges, not as a position plus a size. The edge under the
  pointer is the one that stops at a clamp or at the minimum; the opposite edge does not
  move. Deriving position and size separately lets the `y` clamp at the desktop top fire
  while the height keeps growing, which slides the bottom edge down — a resize from the
  north edge quietly becoming a move. `resize n at desktop edge` fails by 170 px if that
  formulation comes back.
- A new window cascades 26 px down and right of the last, starting clear of the desktop icon
  column. Two windows never open at one origin (`launch all`).

## The open sequence

This is the "pop up load feel", and it is four steps in `Desktop.launch`:

1. The frame is created with its body showing a marquee progress bar.
2. The taskbar button is created, and the window takes focus.
3. The frame zooms up from that button's rect, while the program's `mount` runs alongside it.
4. The content replaces the marquee, and keyboard focus moves into it.

`MIN_LOAD_MS` (120 ms, in `src/desktop.mjs`) is a floor on step 3. It is deliberate latency,
which is normally a defect. It is here because it is the thing being imitated: XP gave you
the frame first and filled it afterwards, and a window whose content is already present when
the frame lands reads as a div appearing rather than as a program starting. Set
`minLoadMs: 0` on the desktop or on a single program to drop it.

`loading frame` asserts the frame is at full size while the body is still the marquee, and
that no window is ever left stuck on it.

The zoom itself is `zoom()` in `src/window.mjs`: a Web Animations transform from the taskbar
button's rect to the window's own, reversed for minimise and for close. Under
`prefers-reduced-motion: reduce` it is skipped outright rather than shortened, per SC 2.3.3.

## Windows size their own content

`.window-body` declares `container: window / inline-size`, so content can respond to the
window instead of the screen. A window 300 px wide on a 1600 px desktop has exactly the
problem a 300 px phone has, and `@media` cannot see it. `theme.css` uses this for
`.group-table`, which restacks to labelled rows when its window gets narrow.

`container query` asserts it by resizing a window to the 220 px minimum on a 1280 px desktop
and requiring the table header to be clipped.

## Departures from XP

Recorded here because each one is a deliberate choice against fidelity, and a reader meeting
the code deserves the reason.

| XP did                                             | webxp does                                        | Why |
| -------------------------------------------------- | ------------------------------------------------- | --- |
| Let a window be dragged until its own title bar was unreachable | Keeps 72 px on screen and the title bar below the top edge | The title bar is the only way to move a window. XP's version is a trap with no way out. |
| Opened a desktop icon on a double click            | Opens on a single activation                      | A double click has no keyboard and no touch equivalent. The Start menu lists the same programs, so neither route is the only one. |
| Buried its own desktop icons under the first cascaded window | Starts the cascade clear of the icon column | Costs nothing, and leaves both reachable. |
| Drew minimise and maximise as 21 px boxes          | 24 px                                             | SC 2.5.8 (WCAG 2.2 AA) wants a 24 × 24 CSS px target, and the three boxes have to match. |
| Drew focus as a 1 px dotted line                   | A white inset ring with a dark halo               | The dotted line fails SC 1.4.11, and has to read on both bisque chrome and blue title bars. |
| Did not animate a close                            | Zooms out, like a minimise                        | A window that vanishes on click reads as a crash. |

### Contrast

Luna put white text on several surfaces too light to hold it. Every correction below is
measured, and `active styling` fails if any of them is undone — demonstrated by restoring
Luna's title-box stop, which the check reports at 2.91:1.

| Surface                   | Luna     | Measured | webxp    | Measured |
| ------------------------- | -------- | -------- | -------- | -------- |
| Close box, lightest stop  | `#e8785a` | 2.89:1  | `#c8341a` | 5.30:1  |
| Title box, lightest stop  | `#6a9ad8` | 2.91:1  | `#4574ad` | 4.82:1  |
| Taskbar button, top stop  | `#3c81ee` | 3.77:1  | `#2f6fd8` | 4.79:1  |
| Body text                 | grey      | fails AA | `#3a3a3a` | passes  |

The light stop that the title-box gradient loses is put back as an inset box-shadow on the
bevel's edge, where it is not under the glyph.

### The unfocused title bar

Luna washed the unfocused bar pale and kept near-white text on it, at roughly 2.3:1.

The obvious fix is to darken the bar until it holds white. That was the first attempt, and
it was wrong: `#66778f` → `#46556e` passes at 4.55:1 and lands on a navy close enough to the
focused bar that "which window am I typing into" stops being answerable at a glance. The
check passed and the thing the title bar is for had quietly stopped working.

So the bar keeps Luna's pale wash and the **text** goes dark instead:

| Surface                          | Colour                  | With                | Measured |
| -------------------------------- | ----------------------- | ------------------- | -------- |
| Unfocused bar, top stop          | `#b4c3da`               | text `#2b3a4f`      | 6.45:1   |
| Unfocused bar, bottom stop       | `#93a6c4`               | text `#2b3a4f`      | 4.67:1   |
| Unfocused title box, darkest     | `#aebed6`               | glyph `#2b3a4f`     | 6.10:1   |
| Unfocused close box, darkest     | `#d79c92`               | glyph `#5e2415`     | 5.21:1   |

The focused bar is vivid and white-on-navy; the unfocused one is pale and dark-on-pale. The
two are now distinguishable by lightness, saturation and text colour at once, which is what
XP was communicating. `active styling` reads each state's own text colour rather than
assuming both are white — an earlier version read `color` off the bar instead of the heading,
got the inherited black for both, and compared black to black. That assertion could not fail.

**One departure is kept uncorrected.** The Start button is white bold italic on Luna's green
and measures **2.16:1**, which fails SC 1.4.3. It is deliberately outside the contrast sweep
in `check.py`: it is the single most recognisable element of the theme, and a green dark
enough to hold white stops reading as XP. Its `text-shadow` helps a sighted reader and is
not modelled by the WCAG formula, so the number above is the pessimistic one. This is a
known defect, not an oversight — if the Start button ever has to carry information rather
than being a label everyone already knows, it must be corrected.

## What a window does not do

Not implemented, and not pretended:

- No window cycling by keyboard. Alt+Tab belongs to the operating system and a browser never
  sees it. Minimised windows stay reachable because the taskbar buttons are focusable.
- No snapping, no edge docking. Those are Windows 7, not XP.
- No modal dialogs, and no window groups in the taskbar.
- Windows are not persisted. A reload is a reboot.
