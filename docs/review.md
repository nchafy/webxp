# Review findings

Findings raised against this work, their addresses, and what was done. Every numbered finding
below is fixed, and each entry names the check that guards it where one does. What was not
fixed is under "Not addressed" at the end.

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

**Check** `viewport shrink`, asserted on `placed` — the clamp's own output — and naming which
of two defects it saw: a placement that has not moved since the shrink is a listener that
never fired or a clamp that does nothing, and a placement that moved and still left the window
off the surface is the clamp's arithmetic. Reading `box` reports the −480 px above either way,
because `apply()` never writes the clamp back into it.

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

**Fix** An optional `ready({ win, desktop, args })`, called after the content is in the
document and two frames have passed. A throwing `ready` is reported like a throwing `mount`.

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

## The pattern worth naming: a check that cannot fail

The same defect wearing different clothes, every time. The check is **silent and reports
success**, which is strictly worse than no check: a missing check is a known gap, while this
one answers "is that covered?" with yes. One entry below had no check on it yet — the helper
it names guaranteed that whatever was written on it would pass.

More than one was introduced *by the fix for an earlier one*, which is the part worth dwelling
on. A check and the code it guards are edited by the same person holding the same mental
model, so a change that invalidates the check's premise rarely looks like one. The panel sweep
is the clearest case: the fix that gave it its own long footer still left nothing overflowing.

| What died | How | Found by |
| --- | --- | --- |
| The title-bar contrast assertion | Read `color` off the bar, not the heading that carries the white text. Compared black to black. | Self-review (finding 2) |
| The canvas-fills-window assertion | Guarded on `if state["root"] and …`, where `.basemap-root` stopped existing when the map moved to `shared/`. The guard went false; the line printed ok. | The other session, probing a live window: `{"basemapRoot": false, "mapRoot": true}` |
| `scaffold a new app` | Read a stale `apps/manifest.mjs` over HTTP and blamed the generator. Intermittent, so a retry "fixed" it. | A re-run that passed |
| `--only <substring>` | A filter matching nothing ran zero checks and exited 0. | The other session, on writing it |
| The noise-ramp probe | `scripts/inspect_noise.py` queried `[data-role=noise-legend] .key-row`. `.key-row` exists nowhere in the repo, so the script written to inspect a new ramp printed an empty legend and no error. | The adversarial review |
| `day_02_panel_sweep` | Its window sizes were chosen because a long panel footer pushed content over the status bar. The copy pass emptied all three footers and `.detail-foot:empty` drops the element, so the sizes stressed nothing and the check passed without exercising the overflow. | The adversarial review |
| `viewport shrink` | Measured `box`, the position the setup asked for, which `apply()` never writes the clamp back into. It printed the −480 px of finding 8 — the signature of the listener being *absent* — whether the listener ran or not. | The audit |
| `day 02 segment spans` | It walks the overlay files sitting beside the activity documents. A checkout with none made every loop skip, and the check reported ok having read nothing. | The audit |
| `day_02_panel_sweep`, again | The footer it fills is not what overflows the panel. `max-height: 50%` with `min-height: 0` lets flex shrink `.detail-body` and `.detail-foot` to zero content height, so what overflows is the head and its two paddings: `panelWanted` measured 91 px and `footGiven` 14 px, both flat from 8 to 1024 repeats of the probe text. Before that the fill was erased outright — `panel.open` ends by writing the footer, and the open promise was discarded, so its fetches landed after the stress. | The audit |
| `double_click`'s two presses | `mouse()` pinned `clickCount` at 1. Two presses that both say 1 are two single clicks, the browser never synthesises `dblclick`, and the `dblclick` listener `src/window.mjs` has carried since it was written had never run. A check built on that helper reports what an app that ignores double clicks would. | The audit |
| `--prove-failure` | It inverted its assertions, never asked whether they had produced a failure, and printed "all checks passed" either way. The one control standing behind every clean run could not itself fail. | The audit |
| `active styling`'s surface sweep | An empty stop list ran the contrast loop zero times and returned clean over white text on a near-white bar, measured at 1.04:1. | The audit |
| `active styling`'s transparent skip | The skip for a transparent black tested a substring of `background-image` and `background-color` joined, so a genuine black gradient stop beside a transparent fill was skipped — and a black glyph on it is 1:1. | The audit |

The rules that come out of it are cheap:

1. **A guard on a selector is a guard on a spelling.** `if element:` turns a renamed class
   into a skipped assertion. A missing precondition MUST be a failure, not a branch that
   quietly does nothing. `basemap app` now says `no .map-area — nothing to measure the canvas
   against`.
2. **Assert against the thing, not against a sibling of the thing.** Comparing the canvas to
   `.map` — the element a stylesheet accident collapses — is an assertion that agrees with
   itself. Measure against the room the content was given.
3. **A passing retry is a finding, not a fix.** The stale-manifest flake passed on re-run and
   would have been shrugged off; forcing the window deliberately
   (`scripts/inspect_manifest_cache.py`) turned a guess into 2 stale reads in 5 rounds with
   the cache on and 0 in 5 with it off.

4. **A check must create the condition it tests, and then measure that it did.**
   `day_02_panel_sweep` depended on the app's copy happening to be long, so it was given its
   own long footer — and the footer's length turned out not to move the panel's overflow at
   all. Writing the setup is not the same as creating the condition. It now re-fills at every
   size it sweeps and asserts the panel's content exceeds its box before any hit test, so a
   panel that is not being clipped is a failure rather than a clean sweep. The same reasoning
   applies to any check whose setup is "the app happens to be in the right state".

5. **The control for a helper belongs inside the check that uses it.** `title bar double
   click` first asserts that two single clicks change nothing. Without that, "the double click
   maximised" is satisfied by any pair of clicks that happens to toggle, and the helper's click
   count goes unasserted. `--prove-failure` is the same rule one level up: a control nobody
   asks about is not a control, so a run with the flag now fails when an inverted check reports
   no failure.

The general form: **a check whose precondition can go false without failing is not a check.**
Every assertion added since is written so that the absence of what it measures is itself a
failure, and shown failing against the defect it describes before it is believed.

## The mirror image: a check that is always red

Review feedback named the counterpart, and it is the same defect approached from the other
side. A check that goes red on every edit trains the reader to regenerate past it. The failure
lands in the human rather than in the assertion: a result that never changes carries no
information, so it stops being read, and the first true failure arrives inside noise already
learned as something to clear. **A check nobody reads is not a check**, and which direction it
broke in makes no difference — one never speaks, the other never stops.

## The third shape: a guard that only some paths reach

**A permission enforced in one function is enforced nowhere.** Two instances in this
repository, and the second is the first one over data rather than over a class.

`VFS.touch` in `shared/fs/vfs.mjs` has two branches. The create branch delegates to
`writeFile` and inherits its `#need(inode, W, path)`. The existing-file branch stamped the
inode directly and checked nothing. So `chmod 444 f` followed by `touch f` succeeded: it moved
the mtime 3 ms and fired a change event, and that event is what the debounced save in
`shared/fs/fs.mjs` acts on — so a read-only file's new timestamp was written out. Every other
`#stamp` call site in the class demands the write bit on the correct object first, and
`symlink` a few lines below does it correctly. The guard was in the file the whole time.
`touch` never reached it.

The cropping transform in `apps/day-02-lines/tools/` is the same miss. `prepare.py` crops
402 m off each end of every run. `street_segments.py` and `noise_by_area.py` read the *export*
directly rather than `prepare.py`'s output, so neither cropped. 1,220 of 11,384 crossings in
`street-noise.json` indexed past the end of their run and 91 named a run that no longer
existed; the dangerous ones were in range but shifted, addressing a different stretch of road
by up to 87%. The app drew those confidently. Three tools over one dataset, two of them
bypassing the transform the third applies.

This is what makes it a family with the two above: in all three the check or guard is
**present and looks correct at the place you read it**. What is wrong is the set of paths that
reach it, which is not visible from there. So reading the guard does not tell you whether it
holds — the routes have to be enumerated. The guard belongs at each entry point, where the
path and the intent are, not in a shared setter that has neither. What that costs differs:
`#need` at each public mutation is a line per method, while three tools over one dataset
agreeing on `--trim-m` is a convention and not an enforcement.

So the second instance is enforced at the consumer instead. `spanFits` in
`apps/day-02-lines/lines.mjs` drops a crossing whose span runs past the end of the run it
names, and the status line says how many and why; `day 02 segment spans` fails the suite on
the same condition before a browser renders anything. One direction only — a span that
shifted but still fits addresses a different stretch of road, and nothing in the file can
tell.

## The fourth shape: a harness that does not know what it measured

The three above are defects in a *check*. This one leaves the assertions intact and lies about
what they were pointed at.

- **The port decided the subject.** `http.server` exits with `Address already in use` rather
  than failing where the harness can see it, and the readiness loop was satisfied by anything
  answering 200 on the port. So a second checkout serving on that port silently became what
  every check measured. One run was never explained at the time: `placed` came back `{}`,
  `cascadeSlot` undefined, `Desktop.destroy` not a function and no `var()` fills in the icon
  set — four symptoms of a page built from older modules, while every file on disk was newer
  than the failure and the next run was clean. `serving_this_tree` now fetches the page and the
  modules every geometry assertion indexes into and compares the served bytes to the bytes on
  disk. Asking whether the subprocess is alive was tried and is not enough: the other server
  answers the first 200 while ours is still on its way to exiting, so the inference is wrong
  and the run goes on to blame the page for a server mix-up.
- **The probe handed back a hole.** `JSON.stringify` drops a key whose value is `undefined`
  rather than sending `null`, so a field read before it was set arrives with the key *absent*.
  That is how `KeyError: 'w'` presented — at a subscript hundreds of lines from the early read,
  as a product verdict rather than a harness defect. `probe` now separates three outcomes,
  because they send a reader to three different places: *not yet* is this harness reading
  early, *absent* is a renamed field or a window that never finished being built, and *wrong*
  is a key holding something that is not a number. None of the three is a pass. The page tests
  for the field with `in`, never with a truth test, because `0` is a legitimate coordinate.
- **The run reported a prefix of itself.** The check loop had no `try`, so one raise took the
  run out through a traceback with nothing in it naming the check: the log still read 14 passes
  and the 26 checks behind it were unmeasured and unnamed. A raise is now a failed check under
  that check's name, and every check the loop reached is recorded and compared to the list, so
  "this check never ran" is stated rather than inferred from a gap in the log.
- **The control graded half of its own run.** `--prove-failure` inverts the checks in
  `INVERTIBLE` and requires each to fail. Its exit-code branch returned on that result alone,
  so everything else the run collected — the other checks' failures, the end-of-run console
  sweep, the "abandoned after N of M" line — was discarded by the one flag that exists to
  prove the suite can fail. A planted `TypeError` gave exit 1 on a plain run and **exit 0**
  here, printing its `[FAIL]` line and then `failed as required`. The break was sound; the
  verdict over it was not, which is why this sits here and not under the fifth shape. The exit
  path now subtracts the inverted checks' own failures from the run's and fails on the
  remainder, naming each one, so a clean tree is the only thing the flag can report clean.
  `scripts/check.py --prove-failure` is the check — the control for every other run, and now
  for itself.

What makes it a shape of its own: a check written perfectly against the first pattern still
dies to every one of these. The assertions were right and the preconditions were asserted; the
instrument was reading another tree, another field, two thirds of the suite, or only the part
of its own run it had asked about. The remedy is not a stronger assertion. The instrument has
to prove its own subject before it measures, and state its own coverage afterwards.

The harness bugs recorded above are this shape caught earlier. Relaunching a singleton is
indistinguishable from a loading floor that does not work, and a check that closes a window a
later check depends on moves the failure onto the innocent one. Neither is a weak assertion
either.

## The fifth shape: a control that breaks the checking, not the thing checked

The four above are defects in a check or in the harness around it. This one is a defect in the
**control** — the deliberate break that is supposed to show a check capable of failing. It
leaves the check unproven and reports it proven, which puts it in the same family.

Three attempts at one control, in `scripts/check_styles_isolated.py` (its docstring records
all three). The check opens one app alone in a fresh document and requires every stylesheet that
app declares to be in the document. The control has to make that fail.

1. **Skip the assertion for that app.** A skipped assertion cannot fail.
2. **Remove the declaration.** The check derives what an app needs from what it declares, so
   removing the declaration removed the expectation with it.
3. **Misspell the path.** The declaration is still there, still expected, and now wrong — the
   only one of the three that perturbs the subject rather than the instrument.

The first two share one tell: both **reduced what was asserted**, and reducing what is
asserted always passes. So **if the control makes the check ask less, it is not a control.**

That is what the first pattern does not give. "Show it capable of failing" says a control is
owed; it does not say how a control goes wrong, and both empty ones were written by someone
following it. Recorded off two instances in one day rather than waiting for a third, because
the tell costs one question per control.

`scripts/css_ties.py` is the same shape done right, and worth reading beside those three.
`--prove-failure` applies `REVERTS` to the stylesheet text in memory, never to disk
(`_apply_reverts`) — the subject is the CSS and the control edits the CSS. It then
audits its own break: a revert whose text no longer matches the file, or a pin that passed
with its fix reverted, is reported as `control: …` rather than counted as the
required failure. A stale control is one that quietly stopped breaking anything, which is the
same defect reached from the other side. The isolation check refuses in the same spirit: with
no sheet declared by two apps it exits 2 saying it has no subject, rather than planting a
break that cannot bite.

`scripts/check.py`'s `--prove-failure` is a different instrument, not a weaker one. It inverts
the assertion of every check in `INVERTIBLE`, and fails the run if an inverted check reports no
failure. The tuple is the list: `grep -n "^INVERTIBLE" scripts/check.py`. It is named rather
than counted here on purpose — a count of it, and a line number into a file of ten thousand
lines, both go stale without anything failing. Inverting asks *more*, so it clears the tell —
but what it proves is that the measurement responds to the page and that a failure reaches the
log, not that the un-inverted assertion catches the defect it was written for. Both questions
are worth asking. Only the second is answered by breaking the subject.

## The rule that came with it, narrowed: what one link per href hides

`src/apps.mjs` links a stylesheet once per session, keyed by resolved href (`linked`, `:172`;
`addStylesheet`, `:181`; each declared path resolved against the app's own folder, `:316`).
That is deliberate — a second app declaring the same sheet should not get a second `<link>`.
**One mechanism, two defects, two instruments**, and running the two together is how the first
version of this rule was wrong.

- A **wrong** declaration is invisible whenever another app declares the same sheet correctly.
  Misspell the path in one app and the sheet is still in the document, still applying, put
  there by the other app. The loader sees nothing wrong; nor does a suite that opens several
  apps, because the app that declares it correctly links it for everything after. One app
  alone in a fresh document is the instrument — `scripts/check_styles_isolated.py`, which is
  what it does.
- A **missing** declaration is hidden by the same single link and is *not* found by that
  sweep. The sweep derives what an app needs from what the app declares, so an absent line is
  something nothing expects. The reasoning is circular and no control rescues it: the break
  for an absent declaration is an absent declaration. It is found by asserting a **styling
  property** per app — the border the app's own sheet removes, the grid it sets. One assertion
  per app, not a sweep. That is what found `apps/showup/`, which rendered its folder correctly
  for an afternoon with no `src/folder.css` entry because `apps/map-challenge/` had linked it.

The broad version offered first — "dedupe hides a missing declaration and isolation finds
it" — is half true, and the false half is the half that would have been acted on.

**The masking needs one resolved href declared by two apps, and on `main` there is exactly
one:** `/shared/map/map.css`, by `apps/basemap/app.mjs:66` and `apps/day-02-lines/app.mjs:39`.
No `app.css` declaration is shared, because `new URL(path, appBase)` gives each one its own
app's folder, so no `app.css` can mask another. `feat/showup-apps` carries three
shared sheets (`shared/map/map.css`, `src/folder.css`, `shared/showup/links.css`), which is
why it bit there first. Re-read with `grep -h "styles:" apps/*/app.mjs`, resolving each path
against its app folder.

`scripts/check_styles_isolated.py` is absent from `main` and referenced by no suite on either
branch. The rule is recorded here so it outlives the branch.

## Not addressed

- **The Start button fails SC 1.4.3 at 2.16:1.** Deliberate, excluded from the sweep, and
  recorded with its number in `windows.md`. It is the most recognisable element of the theme
  and a green dark enough to hold white stops reading as XP. Known defect, not an oversight.
- **`pointercancel` is reasoned, not observed.** `re-entrant gesture` drives a real second
  pointer through touch emulation — the only check in the suite that uses touch — so a touch
  down, move and release are exercised. The branch that is not is the one `beginGesture`
  handles `pointercancel` for: a touch drag the browser turns into a scroll, which fires it
  instead of `pointerup`.
- **Only the checks in `INVERTIBLE` can be inverted.** `--prove-failure` asserts the opposite
  of each one's usual claim and fails the run if an inverted check reports no failure — or if
  an `--only` filter excluded every one of them. `scripts/css_ties.py` carries its own
  inversion over the specificity decisions it pins. For every other check, "shown capable of
  failing" rests on a negative control run when it was written, or on the history above, rather
  than on a control the suite re-runs.
- **No check covers `prefers-reduced-motion`.** The zoom is skipped rather than shortened,
  and the marquee has a CSS fallback, but neither path is exercised.
- **`scripts/css_ties.py` is a regression guard, not a tie detector.** It pins the decisions
  listed in its `OUTRANKS`, `DISJOINT`, `ORDERED` and `GONE` tables, so undoing one of them
  fails. A new
  tie of finding 13's shape, between two other selectors, is not caught. The general case needs
  ancestor reachability — which class sets co-occur, and which ancestor chains are possible —
  and without it a sweep over the stylesheets reports a few hundred candidate ties that can
  never meet on one element.

  Its subject is discovered rather than listed: `sheets()` walks the tree for `*.css` and
  raises on an empty result, so a sheet cannot land outside the sweep and leave `css ties: ok`
  quoted for a file the instrument never opened. What stays hand-written is the pins, so the
  gap is one of attention and not of reach — a new sheet is read, and nothing in it is
  asserted until somebody writes a pin naming one of its selectors.
- **App CSS scoping is a convention, not an enforcement.** `docs/apps.md` requires every rule
  to sit under `.app-<id>` and `app styles` checks that one app's class does not land on
  another's window, but nothing stops an app shipping a bare `p { ... }`. `@scope` or a build
  step would enforce it; neither is worth it before there are more apps.
- **`basemap app` needs the network.** It reaches the real tile services, because no fixture
  could tell us a source stopped answering. It reports a network failure as one rather than as
  a defect, but it is the one check that cannot run offline.
