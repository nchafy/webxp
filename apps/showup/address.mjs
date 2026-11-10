/* The address box: which of the 51 council districts am I in?
 *
 * Restored from the landing page this project was ported from — `council_access_nyc`,
 * `src/showup/assets/address.js` at commit `85d70fc^`. Four kinds of input, and only one of
 * them leaves the machine.
 *
 * ## The privacy property, which is the whole point
 *
 * A ZIP code, a neighbourhood name and a bare district number are answered from
 * `lookup.json`, which the desktop already fetched. Nothing is sent for them. Only a street
 * address reaches the City's own geocoder, on submit and never on a keystroke, with
 * `private=true` — the parameter that asks NYC Planning Labs not to log the query. That is
 * `docs/phase-1-scope.md` §4.4 in the source project, which carried a build-time grep
 * failing any call site that omitted it.
 *
 * The inverse is the failure worth naming, because it has happened: the original shipped a
 * CSP whose `connect-src` omitted `'self'`, so every same-origin fetch was refused and ZIP,
 * neighbourhood and district-number input fell through to the geocoder — a policy written to
 * protect the reader's address sent more of it. So the local index is a precondition rather
 * than an optimisation. The form stays hidden until the index is in hand, and a street
 * address is refused by name if our own geometry will not load. Neither path degrades into
 * sending more.
 *
 * ## Where a resolution goes
 *
 * To a program, via `openSurface`. A destination is carried as one string — the
 * `/district/35/` form that `preview.json` uses and `internalTarget` resolves — so the
 * address box and every other cross-link in Show Up agree about where a district is without
 * a second table saying so.
 */

import { group, table } from '../../src/widgets.mjs';
import {
  LOOKUP,
  internalTarget,
  load,
  openSurface,
  usableTable,
} from '../../shared/showup/data.mjs';

const GEOCODER = 'https://geosearch.planninglabs.nyc/v2/search';

//: Candidates asked of the geocoder, and matches offered for a name. Both 8, as the
//: original had them: enough that an ambiguous street number in three boroughs is all there,
//: few enough to read.
const RESULT_SIZE = 8;
const MATCH_LIMIT = 8;

/* The district polygons.
 *
 * Not beside the other artifact paths in `shared/showup/data.mjs` because that file is not
 * this change's to edit. Fetched only on the street-address path: the geocoder answers with
 * a coordinate and not with a district, so the district is computed here — and a reader who
 * types a ZIP never pays the 149 KB.
 */
const DISTRICTS_GEO = '/data/showup/districts.geo.json';

//: `docs/phase-1-scope.md` §6A / R40c, as measured there. The geocoder leg is the City's
//: and cannot be promised, so it is narrated instead: slow at 1.2 s, abandoned at 5 s.
const SLOW_MS = 1200;
const ABANDON_MS = 5000;

const HEADING = 'Which council district am I in?';
const PROMPT = 'Your address, ZIP code, neighbourhood, or council district number';

/* `lookup.json`'s `zips` carries one key that is not a ZIP code.
 *
 * `99999` maps to districts 6, 15, 51, 39, 31, 32, 11 and 42 — eight districts across five
 * boroughs, which no deliverable ZIP does — and `zip_zcta` echoes `99999` back. It is the
 * upstream ETL's catch-all bucket, and offering it as an answer would be inventing a place.
 * What it means in the ETL is a question for `council_access_nyc`; here it is excluded by
 * name, because a rule inferred from "spans too many boroughs" would be a threshold nobody
 * measured.
 */
const NOT_A_ZIP = '99999';

//: Localities the geocoder may legitimately place a New York City address in. Used only on
//: the fallback branch of `outOfArea`, where the parse carries no region at all.
const NYC = /manhattan|brooklyn|queens|bronx|staten island|new york/i;

//: One per page, not one per window: 51 polygons parsed into rings are immutable derived
//: data, and a second window asking for them should get the first window's copy.
let shapesPromise = null;

//: Makes `label for` unique even though this program is a singleton, so a second window
//: could never point a label at the first one's input.
let sequence = 0;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* The polygons, flattened to rings with a bounding box each.
 *
 * The geometry type is asserted rather than assumed: `coordinates.flat()` is the rings of a
 * MultiPolygon, and the same expression over a Polygon yields coordinate pairs, which this
 * would then hit-test as if they were rings and answer wrongly in silence.
 */
function index(collection) {
  return (collection.features || []).map((feature) => {
    if (feature.geometry.type !== 'MultiPolygon') {
      throw new Error(`a ${feature.geometry.type} feature where a MultiPolygon was expected`);
    }
    const rings = feature.geometry.coordinates.flat();
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const ring of rings) {
      for (const [x, y] of ring) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
    return { n: Number(feature.properties.coundist), rings, minX, maxX, minY, maxY };
  });
}

function shapes() {
  if (!shapesPromise) {
    shapesPromise = load(DISTRICTS_GEO)
      .then(index)
      .catch((error) => {
        // Dropped so the next street address retries, rather than one bad fetch disabling
        // the path for the session.
        shapesPromise = null;
        throw new Error(`${DISTRICTS_GEO}: ${error.message}`);
      });
  }
  return shapesPromise;
}

/* Even-odd ray casting, which is the rule the build's own Python used, so the browser and
 * the committed crosswalks agree about which district a point is in. Holes are rings like
 * any other and the parity handles them.
 */
function contains(shape, x, y) {
  if (x < shape.minX || x > shape.maxX || y < shape.minY || y > shape.maxY) return false;
  let inside = false;
  for (const ring of shape.rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if ((yi > y) !== (yj > y) && x < xi + ((y - yi) * (xj - xi)) / (yj - yi)) inside = !inside;
    }
  }
  return inside;
}

function districtAt(all, lon, lat) {
  const hit = all.find((shape) => contains(shape, lon, lat));
  return hit ? hit.n : null;
}

/* One destination: what to call it, where it goes, and what to say beside it.
 *
 * `null` when `lookup.json` does not carry the district, so a caller drops the row instead
 * of offering a button that opens nothing.
 */
function districtChoice(lookup, n) {
  const entry = (lookup.districts || []).find((district) => district.n === Number(n));
  if (!entry) return null;
  return { short: `Council District ${entry.n}`, href: `/district/${entry.n}/`, hoods: entry.hoods || '' };
}

function boardChoice(board) {
  return { short: board.label, href: `/board/${board.code}/`, hoods: board.hoods || '' };
}

/* Everything answerable without a request.
 *
 * Returning `null` is what sends a query to the geocoder, so this function is the privacy
 * boundary and every branch of it is a request that does not happen.
 */
function resolveLocally(lookup, query) {
  const needle = query.toLowerCase();

  const asNumber = /^(?:council\s+)?(?:district\s*)?#?(\d{1,2})$/i.exec(query);
  if (asNumber) {
    const choice = districtChoice(lookup, asNumber[1]);
    if (choice) return { choices: [choice] };
    /* A bare number is a district number or it is nothing — it is never a street address, so
     * `52`, `0` and `99` are refused here rather than forwarded. The bounds are read off the
     * index, because a literal "1 to 51" in a sentence is a figure that goes stale silently.
     */
    const numbers = (lookup.districts || []).map((district) => district.n);
    return {
      refusal: `There is no council district ${Number(asNumber[1])}. The districts are `
        + `numbered ${Math.min(...numbers)} to ${Math.max(...numbers)}.`,
    };
  }

  const asZip = /^(\d{5})(?:-\d{4})?$/.exec(query);
  if (asZip) {
    const zip = asZip[1];
    const numbers = zip === NOT_A_ZIP ? [] : (lookup.zips || {})[zip] || [];
    const choices = numbers.map((n) => districtChoice(lookup, n)).filter(Boolean);
    if (!choices.length) {
      /* Two different facts, and this box can only tell them apart for the ZIPs it has.
       *
       * The index holds 215 ZIPs and New York City has more: `11249` (Williamsburg) and
       * `10281` (Battery Park City) are both real and both absent. Saying "that is not in New
       * York City" would tell a Williamsburg reader their home is outside the city they live
       * in. Testing the digits against a ZIP range instead would mean inventing the range.
       */
      return {
        refusal: `ZIP ${zip} is not in this program's list of New York City ZIP codes. Either `
          + 'it is outside the city, or the list is missing it — a street address or a '
          + 'neighbourhood name answers either way.',
      };
    }
    // A ZIP straddling districts is the norm rather than the exception, so the reader is
    // offered the choice instead of being given a guess.
    return { heading: `ZIP ${zip} covers ${choices.length} council districts. Pick one:`, choices };
  }

  /* The labels this box prints back at the reader all carry a digit — every community board
   * is "… Community Board 2" — and the digit gate below would hand one straight to the
   * geocoder, which answers a board's name with a street. An exact match on a label we
   * rendered ourselves is never an address. District labels need no entry here: "Council
   * District 35" is already a district number to the pattern above.
   */
  const labelled = (lookup.boards || []).find((board) => board.label.toLowerCase() === needle);
  if (labelled) return { choices: [boardChoice(labelled)] };

  // A substring match answers only when the query carries no digit, so a street address still
  // goes to the geocoder.
  if (/\d/.test(query)) return null;

  const found = [];
  for (const district of lookup.districts || []) {
    if ((district.hoods || '').toLowerCase().includes(needle)) {
      found.push(districtChoice(lookup, district.n));
    }
  }
  for (const board of lookup.boards || []) {
    if (`${board.label} ${board.hoods || ''}`.toLowerCase().includes(needle)) {
      found.push(boardChoice(board));
    }
  }
  if (!found.length) return null;
  return {
    heading: found.length > MATCH_LIMIT
      ? `${found.length} matches for “${query}”. The first ${MATCH_LIMIT}:`
      : `${found.length} matches for “${query}”. Pick one:`,
    choices: found.slice(0, MATCH_LIMIT),
  };
}

/* One address comes back several times — multiple entrances, and aliases of one building —
 * so duplicates are collapsed on a rounded coordinate rather than on the label, which
 * differs between them.
 */
function dedupe(features) {
  const seen = new Set();
  return features.filter((feature) => {
    const coords = feature.geometry && feature.geometry.coordinates;
    if (!coords) return false;
    const key = `${coords[0].toFixed(5)},${coords[1].toFixed(5)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/* Refusing an address the reader did not mean, from what the geocoder says it *parsed*
 * rather than from where it landed.
 *
 * The parse is at `geocoding.query.parsed_text` on the response, not on a feature:
 * `features[].properties.parsed_text` is `null` in every response measured against the live
 * service on 2026-11-10. It has to be the parse and not the result, because a New Jersey
 * query lands in New York City anyway — "744 Broad Street, Newark, NJ" returns 109 BROAD
 * STREET, Manhattan, as its best match, so a check on the returned borough sees nothing
 * wrong and silently tells a Newark reader they are in District 1.
 */
function outOfArea(response, properties) {
  const parsed = ((response.geocoding || {}).query || {}).parsed_text || {};
  const region = String(parsed.region || '').toUpperCase();
  if (region && region !== 'NY' && region !== 'NEW YORK') {
    return parsed.admin || parsed.locality || parsed.region;
  }
  const locality = properties.locality || '';
  if (!properties.borough && locality && !NYC.test(locality)) return locality;
  return null;
}

/**
 * The address box, as a group box to drop into a window.
 *
 * @param {object} desktop  the `Desktop`, for `openSurface`
 * @returns {{node: HTMLElement, ready: () => Promise<boolean>, dispose: () => void}}
 *          `ready` fetches the index and reveals the form, answering whether it can work.
 *          `dispose` belongs on `win.onClose`.
 */
export function addressBox(desktop) {
  sequence += 1;
  const inputId = `showup-address-${sequence}`;

  const node = group(HEADING, []);
  const form = el('form', 'address-form');
  form.hidden = true;
  const label = el('label', 'address-label', PROMPT);
  label.htmlFor = inputId;
  const input = el('input', 'address-input');
  input.type = 'search';
  input.id = inputId;
  // The typed address is never written to a URL, to storage or to a log. Form history is
  // storage, and the browser's own saved-value dropdown is how it would get there.
  input.autocomplete = 'off';
  const go = el('button', 'address-go', 'Find');
  go.type = 'submit';
  form.append(label, input, go);

  const status = el('p', 'address-status', 'Loading the district index…');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const results = el('div', 'address-results');
  node.append(form, status, results);

  let lookup = null;
  //: Bumped by every submit and by `dispose`, so a late answer cannot write over a newer
  //: one and nothing writes into a closed window.
  let generation = 0;
  let timers = [];
  let controller = null;

  function say(message) {
    status.textContent = message;
  }

  /* The last word on an async path.
   *
   * Everything a reader can cause — a slow geocoder, a blocked fetch, a malformed answer —
   * is reported where it happens. Anything reaching here is a defect in this file, and
   * without this the only trace would be an unhandled rejection and a status line stuck on
   * "Looking up…" forever.
   */
  function report(promise, what) {
    promise.catch((error) => {
      say(`${what} failed unexpectedly (${error.message}). That is a fault here.`);
      console.error(`showup/address: ${what}: ${error.stack || error.message}`);
    });
  }

  function clearTimers() {
    for (const timer of timers) clearTimeout(timer);
    timers = [];
  }

  function reset() {
    clearTimers();
    if (controller) controller.abort();
    controller = null;
    results.replaceChildren();
  }

  /* Open a resolution, and let the status line carry the answer.
   *
   * Every failure says so. `openSurface` announces its own reason — an unregistered program,
   * a row the list does not hold — but it announces into the desktop's live region, and a
   * status line still reading "opening it now" would contradict it.
   */
  async function openChoice(choice, prefix = '') {
    const target = internalTarget(choice.href);
    if (!target) {
      say(`${prefix}${choice.short} could not be opened: ${choice.href} names no program.`);
      return;
    }
    say(`${prefix}${choice.short} — opening it now.`);
    const reached = await openSurface(desktop, target);
    if (!reached) say(`${prefix}${choice.short} could not be opened.`);
  }

  function showChoices(heading, choices) {
    say(heading);
    // `usableTable` is what turns each `/district/35/` into the same cross-link button the
    // councils and boards programs render, so these rows behave like every other one.
    results.replaceChildren(table(usableTable({
      columns: ['Where', 'Neighbourhoods'],
      rows: choices.map((choice) => [{ text: choice.short, href: choice.href }, choice.hoods]),
    }, desktop)));
  }

  function geocoded(query, response, all) {
    const features = dedupe(response.features || []);
    if (!features.length) {
      say(`No match for “${query}”. Check the spelling, or add the borough.`);
      return null;
    }

    const foreign = outOfArea(response, features[0].properties || {});
    if (foreign) {
      say(`That looks like ${foreign}, which is outside New York City. `
        + 'Show Up NYC only covers the five boroughs.');
      return null;
    }

    // The geocoder returns its candidates best first, so the best one is the answer, and the
    // status names the address it chose — which is what makes an ambiguous street number in
    // three boroughs visible rather than arbitrary.
    for (const feature of features) {
      const [lon, lat] = feature.geometry.coordinates;
      const n = districtAt(all, lon, lat);
      if (!n) continue;
      const choice = districtChoice(lookup, n);
      if (choice) return { choice, label: feature.properties.label || query };
      // The geometry and the index disagreeing is our own data being inconsistent, and the
      // reader is told that rather than watching the submit do nothing.
      say(`That address is in council district ${n}, which this program's own index does `
        + 'not carry. That is a fault here, not with the City.');
      return null;
    }

    say('We found that address but could not place it in a council district. '
      + 'It may be just outside the city boundary.');
    return null;
  }

  function search(query, signal) {
    // `private=true` asks NYC Planning Labs not to log the query. It is the one parameter
    // this call cannot lose. No custom header and no credentials, so it stays a CORS-simple
    // GET: the service answers `OPTIONS` with 403, and anything triggering a preflight works
    // in curl and fails in a browser.
    const url = `${GEOCODER}?text=${encodeURIComponent(query)}&size=${RESULT_SIZE}&private=true`;
    return fetch(url, { credentials: 'omit', referrerPolicy: 'no-referrer', signal }).then(
      (response) => {
        if (!response.ok) throw new Error(`the geocoder answered ${response.status}`);
        return response.json();
      },
    );
  }

  async function street(query, mine) {
    say('Looking up…');

    /* Our own geometry first, and the address is not sent until it is in hand.
     *
     * The geocoder answers with a coordinate and not with a district, so without these
     * polygons there is no answer to give — and an address sent for a question we then cannot
     * answer is an address sent for nothing. Fetching both at once was a request the reader
     * paid for and got nothing back from. Only the first street lookup of a session pays for
     * this; `shapes()` holds the promise.
     */
    let all;
    try {
      all = await shapes();
    } catch (error) {
      if (mine !== generation) return;
      say('This program could not load its own district geometry, so a street address cannot '
        + `be placed (${error.message}). Nothing was sent to the City — that is a fault here.`);
      return;
    }
    if (mine !== generation) return;

    controller = new AbortController();
    const { signal } = controller;
    let abandoned = false;

    timers.push(setTimeout(() => {
      if (mine === generation) {
        say("Still looking up — the City's address lookup (geosearch.planninglabs.nyc) "
          + 'is slow right now.');
      }
    }, SLOW_MS));
    timers.push(setTimeout(() => {
      if (mine !== generation) return;
      abandoned = true;
      controller.abort();
      say("Giving up on the City's address lookup — it did not answer in 5 seconds.");
    }, ABANDON_MS));

    let response;
    try {
      response = await search(query, signal);
    } catch (error) {
      if (mine !== generation || abandoned) return;
      clearTimers();
      say(`The City's address lookup did not answer (${error.message}). Nothing was saved — `
        + 'try again, or type a ZIP code, a neighbourhood or a district number, which are '
        + 'answered here.');
      return;
    }

    if (mine !== generation) return;
    clearTimers();
    const answer = geocoded(query, response, all);
    // `geocoded` has already said why when it has no answer.
    if (!answer) return;
    await openChoice(answer.choice, `${answer.label} is in `);
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    // Bumped before the abort, not after: a superseded run checks the generation to decide
    // whether to report, and aborting it while it still looks current makes the new
    // submit's own cancellation render as "the City did not answer".
    generation += 1;
    const mine = generation;
    reset();

    const query = input.value.trim();
    if (!query) {
      say('Type an address, a ZIP code, a neighbourhood, or a district number.');
      return;
    }

    // `lookup` is non-null whenever this listener can run: the form ships hidden and only
    // `ready` reveals it, and only once the index is in hand.
    const local = resolveLocally(lookup, query);
    if (local && local.refusal) {
      say(local.refusal);
      return;
    }
    if (local && local.choices.length === 1) {
      report(openChoice(local.choices[0]), 'opening that district');
      return;
    }
    if (local) {
      showChoices(local.heading, local.choices);
      return;
    }

    report(street(query, mine), 'the address lookup');
  });

  async function ready() {
    const mine = generation;
    let index;
    try {
      index = await load(LOOKUP);
    } catch (error) {
      say(`The district index could not be loaded (${error.message}), so this box cannot `
        + 'answer anything. That is a fault here, not with the City.');
      return false;
    }
    if (mine !== generation) return false;
    if (!(index.districts || []).length) {
      say('The district index loaded but carries no districts, so this box cannot answer '
        + 'anything. That is a fault here, not with the City.');
      return false;
    }
    lookup = index;
    form.hidden = false;
    say('');
    return true;
  }

  function dispose() {
    generation += 1;
    reset();
  }

  return { node, ready, dispose };
}
