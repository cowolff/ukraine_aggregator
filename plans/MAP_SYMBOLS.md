# Map Symbols — per-type marker shapes

Status: **implemented** (2026-08-25). Final `icon-size` stops and the `styleimagemissing`
strategy are logged in `plans/DEVIATIONS.md` §37.

## 1. Goal

Every event on the map is currently the same circle; only its **colour** varies, and colour is
already spoken for (perspective / beneficiary, per the colour-mode toggle). A reader scanning the
map cannot tell a deep strike from a frontline advance from a geolocation proof without clicking
each marker. Meanwhile the system already *has* a per-type visual language — the glyphs in
`EVENT_GLYPHS` (`app/models/events.py:38`) — but it only appears in popups, the legend, the filter
checkboxes and feed chips, never on the map itself.

Give each event type its own **marker shape**, so the map reads at a glance:

| Type | Shape on the map | Existing glyph |
|---|---|---|
| `frontline_advance` | filled triangle (points up) | ▲ |
| `frontline_claim` | hollow triangle | △ |
| `deep_strike` | 4-point starburst | ✸ |
| `shelling` | filled circle (today's marker) | ● |
| `geolocation_proof` | ring (donut) | ◎ |
| `debunk` | circle with a diagonal slash | ⊘ |
| `other` | small filled square | · |

The shapes deliberately mirror the unicode glyphs already shown everywhere else, so the map,
legend, filters, popups and feed chips all speak one language. Filled vs hollow triangle keeps
the advance/claim distinction the glyphs already draw (confirmed vs claimed).

### Channel budget — what stays where

Nothing else moves. The encoding becomes:

- **Shape** = event type *(new)*
- **Colour** = perspective or beneficiary, per the existing mode toggle; saved = yellow, always
- **Opacity** = confidence (low-confidence extractions stay faint, PLAN §21)
- **Clusters** stay circles — a cluster aggregates mixed types, so no shape applies; size = count,
  colour = lean, exactly as today

Shape is a colour-independent channel, so this also improves the colourblind story: type no
longer requires reading the popup, and the legend's "colour is never the only cue" promise now
holds on the map surface itself.

## 2. Rendering approach — SDF icons, generated at boot

Constraints that rule the options in or out:

- **No build step, no external assets** (PLAN §16): the SPA is vanilla JS served statically.
- The base style is a **raster style with no `glyphs` property** (`baseStyle()`, `app.js:145`),
  so a MapLibre *symbol layer with `text-field`* would need a fontstack PBF server — a new
  external dependency (the app self-hosts everything but OSM tiles). Rejected.
- Colour must stay **data-driven**: `pointColorExpression()` already encodes
  saved > beneficiary/perspective in one expression, and `applyColorMode()` swaps it live
  without refetching. Baking colour into per-(type × colour) bitmap icons would mean ~40 images
  and a second copy of the colour logic in image-key space. Rejected.

That leaves **SDF icons**: `map.addImage(name, image, {sdf: true})` gives one single-channel
image per *shape*, and the symbol layer recolours it per feature with `icon-color` — which
accepts the existing `pointColorExpression()` verbatim. Halo (`icon-halo-*`) replaces the
current white `circle-stroke` for contrast against OSM tiles.

### 2.1 One source of truth for geometry

Define the seven shapes once, as SVG path strings, in a new `app.js` constant:

```js
const EVENT_SHAPES = {
  frontline_advance: {path: 'M32 8 L58 54 L6 54 Z'},                    // filled triangle
  frontline_claim:   {path: '…', hollow: true},                         // stroked triangle
  deep_strike:       {path: 'M32 2 L38 26 L62 32 L38 38 L32 62 L26 38 L2 32 L26 26 Z'},
  shelling:          {path: '<circle>'},
  geolocation_proof: {path: '<circle>', hollow: true},                  // ring
  debunk:            {path: '<circle + diagonal bar>', hollow: true},
  other:             {path: '<small square>'},
};
```

At boot, for each shape: draw the path (`Path2D`) onto an offscreen 64×64 canvas as an alpha
mask (hollow shapes are stroked, not filled), run a small distance transform over the mask
(the tinysdf/Felzenszwalb approach, ~60 lines, inlined — no dependency), and `map.addImage`
the result with `{sdf: true, pixelRatio: 2}`. Rendering from `Path2D` rather than hand-coded
per-shape SDF math means the *same path strings* also power the legend and filters (§4) —
geometry is defined exactly once.

Draw shapes with **8px of empty padding** inside the 64px box: SDF halos need headroom, and the
padded box is also the click target (§3.3).

### 2.2 Layer swap

In `installOverlays()` (`app.js:209`), replace the `events-circles` circle layer with a symbol
layer (same source, same position in the layer order — below `clusters`):

```js
map.addLayer({
  id: 'events-icons', type: 'symbol', source: 'events',
  filter: ['!', ['has', 'cluster']],
  layout: {
    'icon-image': ['match', ['get', 'event_type'],
      'frontline_advance', 'shape-frontline_advance',
      /* … one arm per type … */ 'shape-other'],
    'icon-size': ['interpolate', ['linear'], ['zoom'], 4, 0.30, 8, 0.42, 12, 0.56],
    'icon-allow-overlap': true,       // markers must never be collision-culled —
    'icon-ignore-placement': true,    // today's circles always render, so must these
  },
  paint: {
    'icon-color': pointColorExpression(),          // unchanged expression, new property
    'icon-halo-color': '#ffffff',
    'icon-halo-width': 1.2,
    'icon-opacity': /* same low-confidence case expression as today */,
  },
});
```

`icon-size` stops (on a 64px/`pixelRatio:2` icon) are chosen so the rendered marker is a touch
*larger* than today's 5–11px circle radii: a silhouette needs more pixels than a disc to read.
Tune visually; record the final stops in DEVIATIONS.

The `events` GeoJSON already carries `event_type` on every unclustered feature (the popup reads
`p.event_type`), so **no API change** is needed.

### 2.3 Colour-mode toggle and saved repaint

- `applyColorMode()` (`app.js:343`): `setPaintProperty('events-icons', 'icon-color',
  pointColorExpression())` instead of the `circle-color` call. Cluster line unchanged.
- `repaintSaved()` / `decorateAndFilter()`: untouched — they mutate feature properties and the
  expression picks the change up, same as today.

### 2.4 Fallback — keep the circle layer as plan B

Icon generation is pure JS + canvas, but a canvas 2D context can be denied (some hardened
browsers) even where WebGL works. Structure boot so failure degrades to today's map instead of
an empty one:

- Build all seven images **before** adding the layer. If any `addImage` throws or a context is
  unavailable, add the existing circle layer under its old `events-circles` id instead, and set
  a one-line status note. All interaction wiring (§3) takes the layer id from a variable so
  both paths share the handlers.
- Also listen for `styleimagemissing` and re-add images there: MapLibre drops custom images on
  `setStyle`, and while the app never calls `setStyle` today, this makes the icons survive if a
  future change does.

## 3. Interactions

### 3.1 Click / hover rewiring

`onEventClick`, the cursor handlers and the `mousemove` registrations (`app.js:261-267`) switch
from `'events-circles'` to the active layer id. Popup content is unchanged — it already names
the type with glyph + label.

### 3.2 Cluster behaviour

None of `clusters`, `onClusterHover`, `onClusterClick` changes. The cluster tooltip already
reports perspective/beneficiary counts; extending it with per-type counts is out of scope here
(it would need the API to aggregate `type_counts` per cluster — noted in §8 as a follow-up).

### 3.3 Hit targets

Symbol layers hit-test the icon's rendered box. The 64px padded box at the §2.2 sizes yields a
click target at least as large as today's circles, including for thin shapes (hollow triangle,
ring). Verify on touch during review; if thin shapes feel fiddly, raise the padding, not the
shape weight.

## 4. Legend, filters, feed — one visual language

The legend and filter rows currently show the **unicode glyph** (`.glyph` span). Once the map
draws real shapes, a `✸` next to "Deep strike" no longer exactly matches the starburst on the
map. Replace the glyph span in *map-adjacent* UI with an inline SVG rendered from the same
`EVENT_SHAPES` path (a `shapeIconHTML(type)` helper, `currentColor` fill so it inherits text
colour):

- `buildLegend()` type list (`app.js:747`) — shapes, since it explains the map.
- `buildFilters()` type checkboxes (`app.js:648`) — shapes, same reason.
- Feed chips, popup type row, news rail (`feedRow()`, `popupHTML()`) — **keep the unicode
  glyphs**: they are inline text in prose-like contexts, and `EVENT_GLYPHS` stays the textual
  representation everywhere (including `/api/config`; no server change).

Legend intro copy (`legend-intro`) already says "Glyph = what happened" — reword to "Shape =
what happened" in both branches.

## 5. What does **not** change

- **No API, model or pipeline change.** `EVENT_GLYPHS`, `/api/config`, the events GeoJSON shape,
  clustering, ETag caching: all untouched. This is a pure frontend change to `app.js` +
  `style.css` (+ `index.html` only if the legend markup needs a class).
- Colour semantics, confidence opacity, saved-yellow, savedOnly filtering, time travel, the
  frontline layers: untouched.
- The WebGL-unavailable fallback path (`showMapUnavailable`) is orthogonal and unchanged.

## 6. Tests

Frontend is not unit-tested in this repo (no build step); backend has nothing to test since
nothing server-side changes. One cheap guard is worth adding to `tests/test_api.py`'s config
test: assert `EVENT_GLYPHS` keys `== set(EVENT_TYPES)` (if not already asserted), since the
frontend's `EVENT_SHAPES` must mirror the same key set and a new event type added server-side
should fail loudly somewhere.

Manual checklist for the PR:

1. Each of the seven types renders its distinct shape; screenshot at zoom 6 and zoom 11.
2. Colour-mode toggle recolours icons live (no refetch), saved markers go yellow in both modes.
3. Low-confidence events render faint (icon + halo both).
4. Filters: unchecking a type removes exactly that shape; legend rows match map shapes.
5. Click each shape → popup; hover cursor changes; cluster hover/click unchanged.
6. Time-scrub back and forth — icons persist (no `styleimagemissing` console spam).
7. Canvas-blocked profile (or simulated throw) → circle fallback renders, status note shown.
8. Zoom 4–5 with clusters off-pattern: shapes still distinguishable at minimum size, halo keeps
   them visible over water/forest/urban tiles.

## 7. Documentation

- README feature list: "Per-type marker shapes — deep strikes, shelling, frontline movement,
  geolocations and debunks each read at a glance."
- `plans/DEVIATIONS.md`: final `icon-size` stops, any hit-target padding change, and the §2.4
  fallback decision if it deviates.

## 8. Build order

Each step ends green and deployable:

1. **Shape sprites + layer swap** — §2.1/2.2, interactions rewired (§3.1), circle fallback
   (§2.4). The map works end to end with shapes.
2. **Colour-mode + polish** — §2.3, size/halo tuning, hit-target check.
3. **Legend/filter shapes + copy** — §4, README, DEVIATIONS.

Follow-up (separate plan if wanted): per-type counts in the cluster tooltip, and a "types in
this cluster" mini-row of shapes — needs an API aggregation change, deliberately excluded here.

## 9. Risks

- **Legibility at small sizes**: a starburst at 10px can smear into a blob. Mitigated by the
  slightly-larger-than-circles size curve (§2.2) and by choosing shapes with distinct
  *silhouettes* (filled vs hollow vs slashed) rather than distinct fine detail. If zoom-4 singles
  still read badly, clamp the minimum `icon-size` up — clusters dominate at those zooms anyway.
- **SDF quality**: a 64px mask with an 8px spread is plenty for flat shapes, but stroked shapes
  (ring, slash) need stroke width ≥ 6px in mask space or the SDF rounds them away. Verify the
  hollow triangle and debunk slash first — they are the thinnest.
- **Perf**: seven 64×64 SDFs are trivial; `icon-allow-overlap` disables the collision pass, so
  symbol layout cost stays comparable to the circle layer even with thousands of points.
- **Shape–glyph drift**: two representations of one taxonomy (SVG paths on the map, unicode in
  text). Accepted: the §4 split is deliberate, both key off the same type strings, and the §6
  key-set assertion catches a type added to one side only.
