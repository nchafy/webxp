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

So the day shows two measurements of one run and the gap between them, rather than
asserting a figure. No control simplifies the trace: a tolerance control has nothing to act
on until a run is selected, which is most of the time, and an inert control reads as a
broken one. The ladder of lengths is still computed on both sides, and `scripts/check.py`
asserts that a coarser measurement is shorter.

## Where this code lives now

This folder is the canonical copy. It was ported here from the 30daymapchallenge repo, where
day 02 was a page of its own rather than an app, and that repo still holds its
`days/02-lines/` — so for the moment `gpsnoise.py`, `prepare.py`, `street_segments.py` and
`geo.mjs` exist twice on disk.

Edit them **here**. The other copy is history, not a second implementation, and the two MUST
NOT be hand-synced: if that tree is kept, its `days/02-lines/` should be deleted rather than
maintained.

## The pipeline

Every tool here needs numpy, and reading a real export needs `fitdecode` — Strava's bulk
export hands back whatever the device uploaded, which for most watches is FIT. Both are
declared in `tools/requirements.txt`:

```
.venv/bin/pip install -r apps/day-02-lines/tools/requirements.txt
```

The repository's one venv at `.venv` also serves `scripts/check.py`, which needs
`websocket-client` and none of this. Nothing the site itself serves needs Python: the
browser half of the geometry is `geo.mjs`, which has no dependencies at all.

```
.venv/bin/python apps/day-02-lines/tools/prepare.py ~/Downloads/strava_export
```

Reads GPX, FIT and TCX (gzipped or not), measures each activity, snaps it to the network,
and writes one JSON per activity plus an `index.json` into `apps/day-02-lines/private/`, which
is gitignored. The page looks there first and falls back to the committed
`apps/day-02-lines/data/`, which holds the synthetic fixture.

### The ends are cropped

A quarter mile comes off each end of every run before anything measures it — `--trim-m`,
default 402 m, printed on every run. A trace starts and ends at a front door, and the middle
is the part that answers the question.

Cropped once, at ingestion, so there is a single place the untrimmed ends can reach. Every
output is computed from the cropped track: the per-run documents, the overview, the index
totals, the street segments and the grid. Cropping on the way out instead would leave the
lengths describing geometry that is not in the file, and the noise statistics still measured
over somebody's doorstep.

Two conditions, not one. A quarter mile of recorded path comes off each end, and then the
crop keeps walking inward until the kept end is at least **200 m straight-line** from the
original end.

The second condition is not decoration. Path distance is not displacement: a watch left
running at a door piles up metres without going anywhere, and on this export cropping by path
alone left two finishes **3 m** from where they started, with ten under 50 m. All were hikes,
which sample sub-1.5 m and spend time milling about at a trailhead.

200 m rather than another quarter mile, measured rather than picked. At the point a quarter
mile of path has passed, the straight-line offset is a median of 408 m — but 179 of 401 runs
are under 402 m there and only 7 are under 200 m. A 402 m floor would crop past the stated
quarter mile on 45% of runs; 200 m reaches the 1.7% that path distance leaves next to the
door and changes nothing else.

What it costs and what it buys, on one 401-activity export:

- **397 runs kept.** Four never get 200 m from their own start and are dropped, counted and
  named — not written as stubs. A run that never leaves its block is entirely near home.
- **348.0 km of 4,282.8 km removed**, 8.1% of the distance.
- The start moves a median of **357 m**, the finish **345 m**.
- The nearest kept end of all 794 is exactly **200 m**. Nothing is within 100 m.

It is still not a radius, and that is the limit worth being plain about: only the ends are
cropped, so an out-and-back or a loop that passes its own start mid-run keeps that pass. This
removes the obvious tell, not every tell.

### The crop reduced the noise it was not meant to touch

Cropping the ends was done for privacy. It also made the headline figure smaller, and that
belongs in the same breath, because the obvious question about a crop removing 8% of the data
is whether it was chosen to flatter the answer.

Measured over the same 397 runs, before and after:

| | before | after | change |
| --- | --- | --- | --- |
| aggregate recorded vs map | +4.22% | +4.13% | -0.09 points |
| per-run median | +0.98% | +0.82% | -0.16 points |
| **Runs only (377)** | **+0.90%** | **+0.67%** | **-0.23 points** |

The Runs-only median is the number this day leads with, and the crop took about a quarter off
it. There is a mechanical reason -- the first and last stretch of a recording is where the
receiver is coldest and where somebody stands still, so it is where jitter is worst -- but
that is an explanation after the fact, not why the crop exists. A quarter mile was asked for
to hide a front door; the noise it removed came along with it.

So every inflation figure below is measured on cropped traces, and would be roughly a quarter
higher on whole ones. `index.json` records `crop.trim_m` and `crop.min_end_offset_m` so no
consumer has to guess which it is holding.

`--trim-m 0` disables it, which is right only for the synthetic fixture: `test_pipeline.py`
holds the output against known route lengths, and cropping would invalidate that comparison
without testing anything the unit checks do not already cover.

Writing into `data/` takes an explicit `--out` and is only for regenerating the fixture.
The split is deliberate: `data/` is tracked, so a default that wrote there would mean the
command above overwrites committed files with real traces, and the next `git add -A`
publishes them.

| File | What it is |
| --- | --- |
| `gpsnoise.py` | The measurement library: projection, length, Douglas-Peucker, file readers, map matching. |
| `prepare.py` | The CLI. Walks an export, measures, writes the page's data. |
| `geo.mjs` | The same length and simplification arithmetic, in the browser. The other half of a pair nothing currently holds together. |
| `lines.mjs` | The two line layers, the segment overlay, the detail panel. |
| `app.mjs` | The spec, and the `mount`/`ready` split the map needs. |
| `make_fixture.py` | Builds a synthetic export whose true answer is known. |
| `test_gpsnoise.py` | Unit checks, plus `--negative-control`. |
| `test_matching.py` | Integration checks against the live matcher. |
| `test_pipeline.py` | Holds `prepare.py`'s output against the fixture's truth. |
| `make_summary.py` | Aggregates a measured export into the committed `data/noise-summary.json`. |
| `report.py` | Prints the distribution of one measured directory. Nothing is written. |
| `noise_by_area.py` | Noise per ~220 m cell, speed-controlled. |
| `street_segments.py` | Noise per OSM way, which is what the overlay draws. |

## Measuring in a local plane

Everything works in a tangent plane built once per trace, not on lat/lon. The scale
factors are the WGS84 radii of curvature at the trace's own origin: the meridional radius
for north, the prime-vertical radius times cos(lat) for east. Length, perpendicular
distance and simplification are then plane geometry and agree with each other by
construction.

Over the few km of one run the planar error is of order (L/R)², which is 3.1 mm over 5 km.
Doing it on a sphere of mean radius instead was measured — by sabotaging the projection in
`test_gpsnoise.py --negative-control` — to skew a degree of latitude by +0.15% and a degree
of longitude by −0.25%, in opposite directions. A real run's own inflation is under a
percent, so an error of that size is a fraction of the effect being measured rather than a
rounding detail: the ellipsoid is load-bearing and not fussy.

Two nearby figures are easy to confuse and worth separating. The meridional radius varies by
0.017% over a *degree* of latitude and 0.175% over ten, so taking the frame origin at an edge
rather than the midpoint costs 0.0087% — two orders of magnitude below the effect, which is
why `frame_origin`'s midpoint is tidiness and its *rounding* is the load-bearing half. And M
and N themselves differ by 0.395% at 40°N, which is the error the two separate scale factors
exist to avoid.

The origin is the bounding-box midpoint, rounded to six decimal places by an explicitly
written rule rather than by either language's `round`. Not for accuracy: for
reproducibility. `geo.mjs` recomputes all of this, and Douglas-Peucker compares distances
against a threshold, so two origins differing in the last bits can keep different points.

## Two implementations of one calculation

`geo.mjs` duplicates what `gpsnoise.py` does, which is the thing this repo's
[code-design rules](../README.md) exist to prevent. It earned its place when a continuous
control had to re-simplify in the browser; the control is gone, and the duplication stays
because the agreement between the two implementations is itself the check — a divergence in
either one is caught by holding them against each other.

**That check does not exist.** `prepare.py` writes its own ladder into every activity
document and `geo.mjs` can recompute it — `win.lines.ladder()` against
`win.lines.writtenLadder()` — but nothing calls either, so the duplication is currently
guarded by nobody. `shared/map/README.md` § Not checked carries the gap and the agreement
measured by hand.

What that hand measurement found, over the committed fixture: the two agree to 0.50 mm
across all 72 rungs with identical surviving-point counts, and 0.50 mm is the rounding floor
rather than a disagreement — `prepare.LENGTH_DP` writes lengths to three decimals. That one
is reproducible by anyone, because the fixture is committed.

Over a private export of about four hundred activities the counts also matched everywhere,
and one rung of several thousand came out millimetres apart, because `math.cos` and
`Math.cos` differ by a last bit at some latitudes and that flips a near-threshold
Douglas-Peucker decision. **Provisional:** that measurement predates `--trim-m`, so it
describes a geometry the pipeline no longer produces, and the rung count and the millimetre
figure both need re-measuring. The mechanism does not depend on the dataset.

Either way the counts can be asserted exactly and the lengths cannot: a flat 2 mm bar passes
the fixture and fails a real export, so a tolerance has to carry the 0.5 mm rounding floor
plus a term that scales with the trace.

Two real defects came out of measuring it rather than assuming it. Lengths were being
written at one decimal place, which put a 3e-5 floor under any comparison; they are now
written at three. And it is why `frame_origin` exists at all.

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
  match the prediction. It does, to 0.08% at worst across three noise regimes.
- **Against published values.** A degree of latitude and a degree of longitude at 40°N,
  both to 5e-5 of the NOAA figures ([latitude and longitude
  lengths](https://www.noaa.gov/jetstream/latlong)). The check's own bar is 2e-4.
- **Against a known route.** `test_matching.py` asks Valhalla to route between two points,
  resamples the path, displaces every point by noise, and feeds it back as a recording.
  Two things are measured and they are different sizes, which is worth not blurring:

  - the plane length of Valhalla's own returned shape against the length Valhalla reports
    for it — 0.22% to 0.26% across the three legs, two implementations agreeing about one
    geometry;
  - matching a noisy recording back to the network length — +0.3%, +0.4% and +2.5%. The
    street-grid leg is the +2.5%, because a dense grid gives the matcher parallel
    alternatives. The check's bar is 10%.

  The three legs are a Manhattan street grid, a Central Park path and a Prospect Park path.
- **Against the fixture's truth.** `tools/test_pipeline.py` holds the committed output
  against `fixture/truth.json`: matching recovers the route length to within 0.52% on all
  six activities — worst −0.52%, best −0.07% — and the GPX round trip is lossless to 0.08%.
  Re-run it to reproduce those numbers; it prints one line per check.

  It reads the committed JSON rather than re-running `prepare.py`, so it holds the *artifact*
  to the truth and would not catch a `gpsnoise.py` regression on its own. It also executed
  nothing at all for a while: its `fixture/` and `data/` paths were anchored on `tools/`
  rather than on the app folder, so it printed "run make_fixture.py first" and returned 1
  before reaching a single check, while this bullet cited its output as evidence.
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

Figures below are from one private export of a few hundred activities. The export and the
per-run documents built from it are gitignored, because an activity trace says where
somebody lives — so the aggregates are committed in their place:

```
.venv/bin/python -I apps/day-02-lines/tools/make_summary.py apps/day-02-lines/private
```

That writes `data/noise-summary.json`: counts, percentiles and the headline figures, with no
coordinate, identifier, date or bounding box in it. The file's own header lists what was left
out and why. `report.py` prints the same ground in more detail against a directory you have.

**Every figure here is measured on a cropped export.** A quarter mile comes off each end of
every run before anything measures it, so "recorded" means the middle of a run and not the
run. See *The ends are cropped* above. The committed fixture in `data/` is built with
`--trim-m 0`, so a figure from `data/` and a figure from `private/` are not the same quantity
and must not be compared.

Read the numbers out of `noise-summary.json` rather than from here. This section says what
the shape of the answer is; the file says what it is for a given export, and does not go
stale when the export is re-measured.

**Inflation is about 1%, not the tens of percent the fixture shows.** Median
recorded-over-matched across the runs is under a percent, and so is the running total
(`export.by_sport`). That is what the correlated-noise argument above predicts, and the
agreement is consistent with it rather than proof of it: this measures the size of the
effect, not its mechanism. Satellite geometry and atmospheric delay drifting over tens of
seconds would produce this, and so would other things that make consecutive errors
similar.

**Sample spacing predicts it, strongly.** Binning by median step
(`export.median_inflation_by_spacing`) separates the sub-1.5 m activities, inflated by tens
of percent, from the 3–4 m ones inflated by a fraction of a percent. Every activity in the
top band is a hike. Walking at 1 Hz puts consecutive points under a metre apart — below the
receiver's own error — and the trace becomes a random walk. This is `expected_inflation`'s
step/sigma ratio showing up in real data.

**Matching is not a lower bound.** Just under a third of activities match *longer* than they
recorded -- 122 of 397 (`export.recorded_over_matched_pct.negative`), because the matcher follows network
geometry where the GPS line cut a corner. Any description of matched distance as "the true
minimum" is wrong.

**Where matching fails, it fails big, and it looks like noise.** A handful of activities
match to well under Strava's own figure, all of them hikes on trails OSM does not carry
well, and their recorded-over-matched ratios run into the hundreds of percent — mostly a
lost match rather than a noisy trace. `report.py` separates the two by consulting the
simplification ladder, which never touches a map: if simplifying the recording to a few
metres lands near Strava's independent figure, the extra length was jitter and the matcher
is the thing that is wrong.

**Strava's own figure is not available on a cropped export.** Its CSV distance covers the
whole activity while these lengths cover the cropped middle, so the two are different
quantities and `resolve_distance_unit` refuses rather than resolving a unit against a
mismatched reference. Every section comparing against Strava is therefore absent from a
cropped run, and `report.py` says so instead of leaving blank columns. On an uncropped
export the CSV column resolved close to the *matched* length rather than the raw one, which
is the finding worth keeping: Strava is already smoothing, and the naive trackpoint sum is
not what it reports.

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
   band, speed still explains of the order of a tenth of the variance. So each window is
   compared against the median inflation of other windows *at the same speed* — equal-count
   bins and a median, non-parametric because the relationship is curved — and what gets
   mapped is the residual. After that, under one percent is left.
   `grid.speed_control` in `noise-summary.json` carries both figures and the bin count,
   which follows the number of windows rather than being fixed.

The script prints the variance explained before and after, and warns if over 1% remains.
Judging it on variance rather than on `r` is deliberate: an `r` of −0.065 sounds like a
leftover relationship and is four parts in a thousand.

### What survived

**Most of the between-cell spread survives the speed control** — a spread of a couple of
points across well-sampled cells, and nine tenths of it still there after the residual is
taken (`grid.between_cell_spread_pts`). So the spatial pattern is not mostly speed.

The cell count is smaller than the activity count suggests: only runs inside
`gpsnoise.SPACING_BAND_M` contribute, so an export of 401 activities -- 397 after the end
crop drops four -- hands this script 363 of them, and only cells clearing `MIN_WINDOWS` from `MIN_ACTIVITIES` are
reported at all.

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

The number is a couple of points of spread between the quietest and noisiest places. The way
to *see* it needs no number at all.

Press **Show segments** and every well-sampled stretch of road is coloured by how much it
disagrees with itself — a sequential ramp along the path, which answers *where*. Click a
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
3. **A crossing** — that stretch pulled solid out of the bundle, on its own run, at full
   resolution.

### Choices worth not undoing

**The road is coloured, not the grid square.** The square was only ever the key used to
group repeat passes; a window is 150 m of path. Drawing the squares claimed an extent the
measurement never had, cut across junctions, and looked like blotches. Each segment is now
drawn as the simplified path of the crossing whose own reading is nearest the segment's
median — a real trace through the place, rather than an average of traces, which would be a
path nobody ran.

**Colour and width both ramp, and the ramp is sequential.** `NOISE_RAMP` in `lines.mjs` is
seven steps of dim amber to bright gold. Measured there, step by step, against the casing
each line sits on and against both run inks:

| | |
| --- | --- |
| CIE L\* across the seven steps | 43.9 → 93.1, no reversal |
| each step vs the casing | 3.23:1 at the dimmest, 15.23:1 at the brightest |
| each step vs the recorded ink | ΔE 81 at worst |
| each step vs the matched ink | ΔE 73 at worst |

Sequential rather than diverging, because the mapped value is `inflation_pct` and a
percentage is not signed: it is above zero on almost every street measured, so a neutral
midpoint would sit near the bottom of the data and mean nothing. A diverging blue-to-red
ramp with a grey midpoint was right while the mapped value was `adjusted_pts`, a residual
either side of zero where the middle really did mean "unremarkable".

Amber also keeps the choropleth off the two hues the run lines use. Those are magenta and
cyan, and blue and red were close enough to both that a reader had to remember which scale a
colour belonged to.

**Width** is the second channel: `NOISE_WIDTHS` rises with the class, so a quiet street is a
hairline and a noisy one is heavy. The map reads in greyscale, before anyone finds the
legend, and for a reader who cannot separate the hues. Two channels for one quantity.

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
- `tools/prepare.py` writes to `apps/day-02-lines/private/`, which is gitignored, and prints
  where it is writing on every run.
- It also **asks git** before writing, rather than trusting that path to match a pattern. A
  destination inside a repository and not ignored is refused outright; an ignored one, or one
  outside any repository, proceeds. This exists because the path and the pattern disagreed
  once: the default resolved to `tools/private/`, which `apps/day-*/private/` does not match,
  and the help text claimed it was ignored while `git check-ignore` said otherwise.
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
