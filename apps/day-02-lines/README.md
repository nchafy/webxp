# Day 2: how long was the run?

Two lengths for the same activity, neither of them the answer.

- **As recorded.** Sum the distances between consecutive trackpoints. Receiver jitter is
  indistinguishable from movement, so every wobble is counted as ground covered. This
  overestimates.
- **Snapped to the map.** Fit the trace to the OpenStreetMap network and measure the path
  the matcher fitted. Matching follows way centrelines, so it cuts corners and never sees
  a weave around a dog. This underestimates.

The gap between them is not an error bar on a known value. It is the width of the
question. A polyline's length depends on the resolution it is measured at, which is the
coastline paradox ([Mandelbrot
1967](https://doi.org/10.1126/science.156.3775.636)) happening on a 5 km run.

So the day measures the whole curve rather than its two ends. The slider simplifies the
recorded trace at a tolerance the reader picks, and the total falls as the wobble comes
out. There is no setting at which the number is right.

## Where this code lives now

This folder is the canonical copy. It was ported here from the 30daymapchallenge repo, where
day 02 was a page of its own rather than an app, and that repo still holds its
`days/02-lines/` — so for the moment `gpsnoise.py`, `prepare.py`, `street_segments.py` and
`geo.mjs` exist twice on disk.

Edit them **here**. The other copy is history, not a second implementation, and the two MUST
NOT be hand-synced: if that tree is kept, its `days/02-lines/` should be deleted rather than
maintained.

## The pipeline

```
.venv/bin/python apps/day-02-lines/tools/prepare.py ~/Downloads/strava_export
```

Reads GPX, FIT and TCX (gzipped or not), measures each activity, snaps it to the network,
and writes one JSON per activity plus an `index.json` into `apps/day-02-lines/private/`, which
is gitignored. The page looks there first and falls back to the committed
`apps/day-02-lines/data/`, which holds the synthetic fixture.

Writing into `data/` takes an explicit `--out` and is only for regenerating the fixture.
The split is deliberate: `data/` is tracked, so a default that wrote there would mean the
command above overwrites committed files with real traces, and the next `git add -A`
publishes them.

| File | What it is |
| --- | --- |
| `gpsnoise.py` | The measurement library: projection, length, Douglas-Peucker, file readers, map matching. |
| `prepare.py` | The CLI. Walks an export, measures, writes the page's data. |
| `geo.mjs` | The same length and simplification arithmetic, in the browser, so the slider can be continuous. |
| `lines.mjs` | The two line layers, the slider, the segment overlay, the detail panel. |
| `app.mjs` | The spec, and the `mount`/`ready` split the map needs. |
| `make_fixture.py` | Builds a synthetic export whose true answer is known. |
| `test_gpsnoise.py` | Unit checks, plus `--negative-control`. |
| `test_matching.py` | Integration checks against the live matcher. |
| `test_pipeline.py` | Holds `prepare.py`'s output against the fixture's truth. |

## Measuring in a local plane

Everything works in a tangent plane built once per trace, not on lat/lon. The scale
factors are the WGS84 radii of curvature at the trace's own origin: the meridional radius
for north, the prime-vertical radius times cos(lat) for east. Length, perpendicular
distance and simplification are then plane geometry and agree with each other by
construction.

Over the few km of one run the planar error is of order (L/R)², about 3 mm over 5 km.
Doing it on a sphere of mean radius instead was measured — by sabotaging the projection in
`test_gpsnoise.py --negative-control` — to skew a degree of latitude by +0.14% and a
degree of longitude by −0.25%. That is the same size as the inflation on a clean trace, so
the ellipsoid is load-bearing rather than fussy.

The origin is the bounding-box midpoint, rounded to six decimal places by an explicitly
written rule rather than by either language's `round`. Not for accuracy: for
reproducibility. `geo.mjs` recomputes all of this, and Douglas-Peucker compares distances
against a threshold, so two origins differing in the last bits can keep different points.

## Two implementations of one calculation

`geo.mjs` duplicates what `gpsnoise.py` does, which is the thing this repo's
[code-design rules](../README.md) exist to prevent. The reason is the slider: a continuous
control has to re-simplify in the browser, and shipping a geometry per rung would make it
stepped and the payload several times larger.

The guard is that `prepare.py` writes its own ladder into every activity document and
`scripts/check.py` drives the browser's implementation over the same geometry and requires
agreement — on the integer count of surviving points, which carries no rounding, and on
the lengths to 2 mm. They currently agree to 0.43 mm across twelve rungs with identical
point sets at every one.

That check found two real defects. Lengths were being written at one decimal place, which
put a 3e-5 floor under any comparison; they are now written at three. And it is why
`frame_origin` exists at all.

## What was verified, and how

The hard part of measuring GPS noise is that a wrong pipeline still produces a plausible
number. "About 2% inflation" looks right whether or not the projection, the variance, or
the polyline precision is correct. So nothing here is checked against another measurement
of itself.

- **Against a closed form.** For a straight path sampled every `d` metres with
  independent per-point noise of sigma σ, the measured step is Rice-distributed and its
  mean is known in closed form ([Rice
  1945](https://doi.org/10.1002/j.1538-7305.1945.tb00453.x)). `test_gpsnoise.py` builds a
  trace whose true length is exact by construction and requires the measured inflation to
  match the prediction. It does, to better than 0.2% across three noise regimes.
- **Against published values.** A degree of latitude and a degree of longitude at 40°N,
  to 2e-4.
- **Against a known route.** `test_matching.py` asks Valhalla to route between two points,
  resamples the path, displaces every point by noise, and feeds it back as a recording.
  Matching recovers the length Valhalla itself reported to within 0.5% on street grid,
  park path and waterfront.
- **Against the fixture's truth.** `test_pipeline.py` holds the whole CLI against
  `fixture/truth.json`: matching recovered the route length to within 0.51% on all six
  activities, and the GPX round trip is lossless to 0.08%.
- **Negative controls.** `test_gpsnoise.py --negative-control` sabotages the projection,
  the noise variance and the polyline precision in turn and requires each to produce a
  failure. The pixel check in `scripts/check.py` has its threshold set from the measured
  gap between a healthy render and a sabotaged one, not from a guess.

### The white-noise caveat

The Rice model assumes independent per-point error, and real receiver error is not
independent. It is dominated by satellite geometry and atmospheric delay, which drift over
tens of seconds, so consecutive points share most of their error and their difference
carries far less than 2σ². That is why a real trace is inflated by a few percent while the
fixture, built with white noise, is inflated by 20% to 270%.

The model is the right reference for checking the arithmetic against a known answer and
the wrong one for predicting a real activity. The fixture is a test, not a claim.

## What a real history turned out to look like

Figures below are from one 401-activity export. They are not in this repo — the export and
everything built from it are gitignored, so these are quoted as a measurement, not shipped
as data. Reproduce with `report.py` against your own.

**Inflation is about 1%, not the tens of percent the fixture shows.** Median recorded-over-
matched across 381 runs was +0.9%, and the running total was +0.8%. That is the correlated-
noise argument above, confirmed: receiver error drifts over tens of seconds, so consecutive
points share most of it and their difference carries far less than 2σ².

**Sample spacing predicts it, strongly.** Binning the same export by median step:

| median step | n | median inflation |
| --- | --- | --- |
| under 1.5 m | 19 | +61% |
| 3.0–3.5 m | 210 | +1.4% |
| 3.5–4.0 m | 158 | +0.2% |

Every activity in the top band was a hike. Walking at 1 Hz puts consecutive points under a
metre apart — below the receiver's own error — and the trace becomes a random walk. This is
`expected_inflation`'s step/sigma ratio showing up in real data.

**Matching is not a lower bound.** 107 of 401 activities matched *longer* than they
recorded, because the matcher follows network geometry where the GPS line cut a corner. Any
description of matched distance as "the true minimum" is wrong.

**Where matching fails, it fails big, and it looks like noise.** 11 activities matched to
under 80% of Strava's own figure, all of them hikes on trails OSM does not carry well. Their
recorded-over-matched ratios reach +265%, which is mostly a lost match rather than a noisy
trace. `report.py` separates these two by consulting the simplification ladder, which never
touches a map: if simplifying the recording to a few metres lands near Strava's independent
figure, the extra length was jitter and the matcher is the thing that is wrong.

**Strava's reported distance is close to the matched one, not the raw one.** Resolved
against the matched length the CSV column sat at a median ratio of 1.003 across 401
activities, where the raw sum was +2.6% above Strava's figure. Strava is already smoothing;
the naive trackpoint sum is not what it reports.

## Does it depend on where you are?

Yes, and `noise_by_area.py` measures it. Run it against an export:

```
.venv/bin/python -I apps/day-02-lines/tools/noise_by_area.py ~/path/to/strava_export
```

Per activity this question cannot be answered — spacing dominates, so comparing places
would really be comparing how fast you were moving. So it works per **window**: walk each
trace in 150 m lengths of *simplified* path, measure how much each window is inflated, and
bin the windows into a ~220 m grid. A cell needs 12 windows from 4+ activities before it
is reported, because ten passes from one activity is one day's satellite geometry, not a
property of the place.

### The confound that would otherwise invent a finding

**Standing still looks exactly like bad reception.** Stop at a traffic light with the watch
running and you get dozens of points inside a four-metre circle: enormous local inflation,
nothing to do with the sky. Bin that spatially and every junction in a city lights up as a
GPS black spot — a confident, reproducible, wrong answer.

Two defences, in order:

1. A window only counts when its mean speed is 2.0–5.0 m/s. Windows outside are dropped
   and counted, so the cost of the filter is visible.
2. That is not enough on its own, and the script says so rather than assuming. Inside the
   band, speed still explained **12.6%** of the variance in one export. So each window is
   compared against the median inflation of other windows *at the same speed* — 40
   equal-count bins and a median, non-parametric because the relationship is curved — and
   what gets mapped is the residual. After that, speed explains **0.4%**.

The script prints the variance explained before and after, and warns if over 1% remains.
Judging it on variance rather than on `r` is deliberate: `r = −0.065` sounds like a
leftover relationship and is four parts in a thousand.

### What survived

In one 368-activity export, **91% of the between-cell spread survived the speed control** —
a 2.35-point spread across well-sampled cells, against 2.59 before. So the spatial pattern
is not mostly speed.

The shape of it: dense, low-rise-to-mid-rise urban grid came out roughly 1.5 to 2.5 points
noisier than the same runner's quiet residential and suburban cells, with a city's downtown
core and a brownstone street grid at the top. A large city park, surrounded by the noisiest
cells in its own borough, sat among the quietest — which points at building geometry rather
than tree cover, since foliage would predict the opposite.

Treat the mechanism as unestablished. This measures that reception is worse in some places;
it cannot separate multipath from blocked sky view, and the cells are coordinates rather
than geocoded descriptions, so naming what is at one is an inference. The grid is written to
`area-noise.json` beside the measured data — gitignored, because a map of where somebody
runs is not public information.

## Showing it: the crossing overlay

The number is 2.3 points of spread between the quietest and noisiest places. The way to
*see* it needs no number at all.

Press **Show segments** and every well-sampled stretch of road is coloured by how much it
disagrees with itself — a diverging ramp along the path, which answers *where*. Click a
segment and the map draws **every crossing of that stretch on top of itself**: one faint
line per run that went through it. Where the receiver agreed with
itself the lines lie on each other; where it did not, they fan apart. The width of that
bundle is the thing this whole page measures as a percentage.

It is also the era's own failure mode. An XP window dragged faster than the compositor
could repaint left a smear of copies trailing behind it, and that is exactly what a noisy
segment looks like. `shoot_worm.py` in the 30daymapchallenge repo captures the two extremes of a grid at one
zoom, which is the comparison worth looking at: a quiet segment is a single thread, a noisy
one is a street-width spray of threads over the same pavement.

Three levels, and a run is the *destination* rather than the entry point:

1. **The grid** — which places disagree with themselves.
2. **A segment** — how much, across how many runs, and the best and worst crossings of it.
3. **A crossing** — that stretch pulled solid out of the bundle, on its own run, with the
   slider live on the full-resolution trace.

### Choices worth not undoing

**The road is coloured, not the grid square.** The square was only ever the key used to
group repeat passes; a window is 150 m of path. Drawing the squares claimed an extent the
measurement never had, cut across junctions, and looked like blotches. Each segment is now
drawn as the simplified path of the crossing whose own reading is nearest the segment's
median — a real trace through the place, rather than an average of traces, which would be a
path nobody ran.

**Colour and width both ramp.** Diverging blue to red, neutral middle, measured against the
casing each line sits on:

| | |
| --- | --- |
| each arm as an ordinal ramp | monotone lightness, step gaps ≥ 0.06 |
| innermost steps vs the casing | 4.10:1 blue, 4.12:1 red |
| across the midpoint, CVD | worst pair ΔE 8.6 deutan, 10.5 tritan (≥8 target) |
| across the midpoint, normal vision | worst pair ΔE 17.4 (≥15 floor) |

The midpoint is `#7f7f77`, not the palette's darker `#383835`. A dark neutral is right for
an *area* fill, where "unremarkable" should recede toward the surface; on a thin *line* it
recedes to invisible, and an invisible segment is indistinguishable from one with too few
passes to report. Lightening it costs separation from the red arm, so it was walked up only
until the CVD gate passed again: `#5c5c56` and `#6e6e67` fail at ΔE 5.8 and 5.7, `#7f7f77`
clears at 8.6. That leaves the midpoint about as light as the innermost steps, so **width**
carries "near zero is unremarkable" instead — a hairline at the middle, heavy at the
extremes. Two channels for one quantity, which also survives greyscale.

**A casing under every segment, and the scrim above the whole basemap.** These are two
different jobs and conflating them was the bug. The casing is the ramp's actual surface, so
it is what the ramp is validated against. The scrim only stops an olive-and-yellow satnav
basemap from competing, so its alpha is a legibility choice — street names still have to
read through it to say *which* street is noisy.

The scrim's **position** is the part that was wrong. The base map hands a day `ctx.beforeId`
so that place labels stay above a thin data overlay, which is right for the traces. A scrim
inserted there sat below fifty basemap layers — bridges, one-way arrows, every label —
which painted over it at full brightness. `probe_order.py` in the 30daymapchallenge repo prints the live order;
everything that must cover the basemap is now appended above all of it, and the probe
reports zero basemap layers above.

**Opaque marks.** Painting a validated ramp at partial alpha composites every step toward
whatever is behind it. At 0.85 one step landed about 32 per channel from the colour that was
checked, so the colours on screen were never the colours validated.

## Two things the page cannot tell you

**Strava's own figure is not the raw sum.** The bulk export hands back the file the device
uploaded, and Strava's reported distance is computed by its own smoothing — and for a
device with a footpod or wheel sensor may not come from GPS at all. `prepare.py` reads the
reported figure from `activities.csv` and shows it beside the others rather than claiming
to reproduce it.

**The CSV's units are worked out by measurement.** Strava's export CSV is not a stable
interface: the column set has changed between exports and two differently-scaled
`Distance` columns have appeared in one file. So `resolve_distance_unit` infers the scale
from the median ratio to the matched length and **refuses** when the answer is ambiguous.
Kilometres and miles are only a factor of 1.609 apart, so this only works against an
accurate reference — against the inflated recorded length the two are indistinguishable,
and an early version read a kilometres column as miles.

## Privacy and licensing

An activity trace records where someone lives and when they are not home.

- Real exports are gitignored (`strava-export/`, `strava_export*/`).
- `prepare.py` writes to `apps/day-02-lines/private/`, which is gitignored, and prints where
  it is writing on every run.
- `apps/day-02-lines/fixture/` and the `data/` built from it are synthetic and are committed,
  so the page renders for someone with no export.

The private output path is not a convenience. The first version defaulted to `data/`,
which is tracked — so following the documented command and committing would have published
real home locations to a public repo, while this document claimed the opposite. It was
caught by review, not by a check, which is why `prepare.py` now also says the destination
out loud.

Map matching uses the [FOSSGIS](https://www.fossgis.de/) public Valhalla instance at
`valhalla1.openstreetmap.de`, which is a volunteer-run service. `prepare.py` identifies
itself in a `User-Agent` and pauses a second between calls. A whole-history export is
thousands of requests; for that, run Valhalla locally in Docker against an OSM extract
rather than leaning on the public instance.

Matched geometry derives from OpenStreetMap and is therefore © OpenStreetMap contributors
under the [ODbL](https://www.openstreetmap.org/copyright).
