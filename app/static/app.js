/* Ukraine Frontline Aggregator — vanilla SPA (PLAN §16). No build step, no websockets:
   the client polls with ETag-aware fetches and only re-renders on a 200. */
'use strict';

/* Diverging scale for clustered markers: which side's reporting dominates a cluster.
   Poles are the same hues the individual markers use, so a cluster reads as "more of these".
   The midpoint is a true neutral grey — a diverging scale never puts a hue in the middle, and a
   balanced cluster must not look like a third category. Greys were chosen against the actual OSM
   tile colours rather than a chart background: the usual near-white midpoint scores 1.0:1 on map
   tiles and vanishes. #666c75 keeps >= 3:1 on land, forest, water and urban fills while giving
   the widest colourblind separation from both poles (12.2 ΔE protan, 16.2 normal). */
const LEAN_UA = '#0057B7';
const LEAN_MID = '#666c75';
const LEAN_RU = '#D52B1E';

const State = {
  config: null,
  perspectives: new Set(),
  types: new Set(),
  rangeHours: 72,
  etags: {},            // url -> last seen ETag
  events: {type: 'FeatureCollection', features: []},
  feedCursor: null,
  feedLoading: false,
  feedExhausted: false,
  // --- news rail (general, unplaced news beside the map) ---
  railCursor: null,
  railLoading: false,
  railExhausted: false,
  railTier: 1,          // max reliability tier shown; 1 = most reliable only (the default)
  timer: null,
  // --- time travel ---
  timeline: null,       // {from, to, snapshots: [ISO], events_per_day: [...]}
  at: null,             // instant being shown; null = live
  playing: false,
  playTimer: null,
  // Monotonic request tokens. Responses are applied only if no newer request of the same kind
  // has been issued since — otherwise a slow earlier response can overwrite a newer one
  // (panning the map while scrubbing raced a live-window response over a historical one).
  seq: {events: 0, frontline: 0},
  // 'perspective' = whose reporting it is; 'beneficiary' = who the event favours.
  colorMode: 'perspective',
  savedOnly: false,
  feedSavedOnly: false,
};

/* Beneficiary colours reuse the diverging poles: a Ukrainian gain is blue, a Russian gain red,
   and anything with no assessment is the same neutral grey a balanced cluster gets. */
const BENEFICIARY_COLORS = {ua: LEAN_UA, ru: LEAN_RU};

/* ------------------------------------------------------------------ saved items
   A reader's own shortlist. Kept entirely in this browser — never sent to the server, so it is
   private and survives reloads but not a different device. Keyed by news item id, so saving from
   the map and from the feed refer to the same story. */
const SAVED_KEY = 'ukraine-aggregator:saved:v1';
const PANELS_KEY = 'ukraine-aggregator:panels:v1';

const Saved = {
  ids: new Set(),
  meta: {},

  load() {
    try {
      const raw = JSON.parse(localStorage.getItem(SAVED_KEY) || '{}');
      this.meta = raw.items || {};
      this.ids = new Set(Object.keys(this.meta).map(Number));
    } catch {
      this.meta = {};
      this.ids = new Set();
    }
  },
  persist() {
    try {
      localStorage.setItem(SAVED_KEY, JSON.stringify({version: 1, items: this.meta}));
    } catch {
      /* private mode or quota: the shortlist just does not survive this session */
    }
  },
  has(id) { return this.ids.has(Number(id)); },
  toggle(id, title) {
    const key = Number(id);
    if (this.ids.has(key)) {
      this.ids.delete(key);
      delete this.meta[key];
    } else {
      this.ids.add(key);
      this.meta[key] = {title: (title || '').slice(0, 160), at: new Date().toISOString()};
    }
    this.persist();
    return this.ids.has(key);
  },
  get size() { return this.ids.size; },
};

const SAVED_COLOR = '#F2B705';

const EVENT_LABELS = {
  frontline_advance: 'Reported advance',
  frontline_claim: 'Fighting reported',
  deep_strike: 'Deep strike',
  shelling: 'Shelling',
  geolocation_proof: 'Geolocated proof',
  debunk: 'Debunk',
  other: 'Other',
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ marker shapes
   Shape = event type (plans/MAP_SYMBOLS.md); colour stays perspective/beneficiary and opacity
   stays confidence. Geometry is defined once, as SVG paths in a 64×64 box (~8px padding for the
   halo and hit area): the map rasterises them into SDF sprites so `icon-color` can keep reusing
   pointColorExpression(), and the legend/filters render the same paths as inline SVG. The unicode
   glyphs stay the *textual* representation (popups, feed chips). */
const SHAPE_STROKE = 7;   // mask-space stroke for hollow shapes; thinner rounds away in the SDF

const EVENT_SHAPES = {
  frontline_advance: {path: 'M32 9 L57 55 L7 55 Z'},                                    // ▲
  frontline_claim: {path: 'M32 12 L54 52 L10 52 Z', hollow: true},                      // △
  deep_strike: {path: 'M32 6 L38 26 L58 32 L38 38 L32 58 L26 38 L6 32 L26 26 Z'},       // ✸
  shelling: {path: 'M32 11 A21 21 0 1 0 32 53 A21 21 0 1 0 32 11 Z'},                   // ●
  geolocation_proof: {path: 'M32 12 A20 20 0 1 0 32 52 A20 20 0 1 0 32 12 Z', hollow: true}, // ◎
  debunk: {path: 'M32 12 A20 20 0 1 0 32 52 A20 20 0 1 0 32 12 Z M18 46 L46 18', hollow: true}, // ⊘
  other: {path: 'M23 23 H41 V41 H23 Z'},                                                // ·
};

const shapeImageId = (type) => `shape-${type}`;

/* SDF rasterisation, the tinysdf approach (Felzenszwalb/Huttenlocher EDT) inlined: the base style
   has no glyph server and the app ships no sprite assets. radius/cutoff follow the GL glyph
   convention (edge at alpha 0.75) so fill and halo render at the right thresholds. */
const SDF_SIZE = 64;
const SDF_RADIUS = 8;
const SDF_CUTOFF = 0.25;
const EDT_INF = 1e20;

function edt1d(grid, offset, stride, length, f, v, z) {
  v[0] = 0;
  z[0] = -EDT_INF;
  z[1] = EDT_INF;
  f[0] = grid[offset];
  for (let q = 1, k = 0, s = 0; q < length; q++) {
    f[q] = grid[offset + q * stride];
    const q2 = q * q;
    do {
      const r = v[k];
      s = (f[q] - f[r] + q2 - r * r) / (q - r) / 2;
    } while (s <= z[k] && --k > -1);
    k++;
    v[k] = q;
    z[k] = s;
    z[k + 1] = EDT_INF;
  }
  for (let q = 0, k = 0; q < length; q++) {
    while (z[k + 1] < q) k++;
    const r = v[k];
    const qr = q - r;
    grid[offset + q * stride] = f[r] + qr * qr;
  }
}

function edt2d(grid, size, f, v, z) {
  for (let x = 0; x < size; x++) edt1d(grid, x, size, size, f, v, z);
  for (let y = 0; y < size; y++) edt1d(grid, y * size, 1, size, f, v, z);
}

function renderShapeSDF(ctx, spec) {
  const size = SDF_SIZE;
  ctx.clearRect(0, 0, size, size);
  const path = new Path2D(spec.path);
  if (spec.hollow) {
    ctx.lineWidth = SHAPE_STROKE;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#fff';
    ctx.stroke(path);
  } else {
    ctx.fillStyle = '#fff';
    ctx.fill(path);
  }
  const alpha = ctx.getImageData(0, 0, size, size).data;
  const n = size * size;
  const outer = new Float64Array(n);
  const inner = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const a = alpha[i * 4 + 3] / 255;
    outer[i] = a === 1 ? 0 : a === 0 ? EDT_INF : Math.max(0, 0.5 - a) ** 2;
    inner[i] = a === 1 ? EDT_INF : a === 0 ? 0 : Math.max(0, a - 0.5) ** 2;
  }
  const f = new Float64Array(size);
  const v = new Uint16Array(size);
  const z = new Float64Array(size + 1);
  edt2d(outer, size, f, v, z);
  edt2d(inner, size, f, v, z);
  const data = new Uint8ClampedArray(n * 4);
  for (let i = 0; i < n; i++) {
    const d = Math.sqrt(outer[i]) - Math.sqrt(inner[i]);
    const value = Math.round(255 - 255 * (d / SDF_RADIUS + SDF_CUTOFF));
    data[i * 4] = data[i * 4 + 1] = data[i * 4 + 2] = value;
    data[i * 4 + 3] = value;
  }
  return {width: size, height: size, data};
}

/* Build the seven sprites; false = no 2D canvas or addImage refused, and the caller falls back
   to the circle layer — a shapeless map beats an empty one. */
function installEventImages() {
  let ctx = null;
  try {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = SDF_SIZE;
    ctx = canvas.getContext('2d', {willReadFrequently: true});
  } catch { /* handled below */ }
  if (!ctx) return false;
  try {
    for (const [type, spec] of Object.entries(EVENT_SHAPES)) {
      const id = shapeImageId(type);
      if (!map.hasImage(id)) {
        map.addImage(id, renderShapeSDF(ctx, spec), {sdf: true, pixelRatio: 2});
      }
    }
    return true;
  } catch (err) {
    console.error('marker shapes failed, falling back to circles:', err);
    return false;
  }
}

/* Legend/filter twin of the map sprites: same path, same fill-vs-stroke, colour from the text. */
function shapeIconHTML(type) {
  const spec = EVENT_SHAPES[type];
  if (!spec) return `<span class="glyph">${(State.config?.glyphs || {})[type] || '·'}</span>`;
  const paint = spec.hollow
    ? `fill="none" stroke="currentColor" stroke-width="${SHAPE_STROKE}"
       stroke-linecap="round" stroke-linejoin="round"`
    : 'fill="currentColor"';
  return `<svg class="shape-icon" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <path d="${spec.path}" ${paint}/></svg>`;
}

/* ------------------------------------------------------------------ fetch helpers */
async function getJSON(url, {useEtag = false} = {}) {
  const headers = {};
  if (useEtag && State.etags[url]) headers['If-None-Match'] = State.etags[url];
  const resp = await fetch(url, {headers});
  if (resp.status === 304) return {unchanged: true};
  if (!resp.ok) throw new Error(`${url} → HTTP ${resp.status}`);
  const etag = resp.headers.get('ETag');
  if (etag) State.etags[url] = etag;
  return {data: await resp.json()};
}

function setStatus(text, isError) {
  const el = $('status');
  el.textContent = text;
  el.style.color = isError ? 'var(--ru)' : 'var(--muted)';
}

function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
}

/* ------------------------------------------------------------------ map */
let map;
/* Which unclustered-events layer is live: 'events-icons' (shapes), or 'events-circles' when
   sprite generation failed and the map fell back (plans/MAP_SYMBOLS.md §2.4). */
let eventLayerId = 'events-icons';
const PERSPECTIVE_ORDER = ['ukrainian', 'russian', 'western', 'neutral'];

function baseStyle() {
  return {
    version: 8,
    sources: {
      osm: {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        maxzoom: 19,
        attribution: '© OpenStreetMap contributors',
      },
    },
    layers: [{id: 'osm', type: 'raster', source: 'osm'}],
  };
}

function webglAvailable() {
  try {
    const canvas = document.createElement('canvas');
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}

function showMapUnavailable(reason) {
  const el = $('map');
  el.innerHTML = `<div class="map-fallback">
      <h2>The map needs WebGL</h2>
      <p>${escapeHTML(reason)}</p>
      <p>Every report is still available in the <strong>Feed</strong> tab, including which
         perspective it comes from and where it was placed.</p>
      <button id="fallback-feed">Open the feed</button>
    </div>`;
  $('fallback-feed')?.addEventListener('click', () => switchTab('feed'));
  setStatus('map unavailable — feed still works', true);
}

function initMap() {
  if (!webglAvailable()) {
    showMapUnavailable('This browser could not create a WebGL context, so the map cannot render. '
      + 'Enabling hardware acceleration usually fixes it.');
    return;
  }

  const {center, zoom} = State.config.map_defaults;
  map = new maplibregl.Map({
    container: 'map', style: baseStyle(), center, zoom,
    attributionControl: {compact: true},
  });
  map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
  map.addControl(new maplibregl.ScaleControl({maxWidth: 110, unit: 'metric'}), 'bottom-right');

  // Without this, a style or tile failure leaves a black rectangle and no explanation at all.
  map.on('error', (e) => {
    const message = e?.error?.message || 'unknown map error';
    console.error('maplibre:', message, e?.error);
    setStatus(`map: ${message}`.slice(0, 90), true);
  });

  // Overlays are installed as soon as the *style* is ready, not on 'load'. `load` also waits for
  // the base raster tiles, so a blocked or down tile server would otherwise mean the frontline and
  // events never render at all — the layers that matter most and that come from our own API.
  let overlaysInstalled = false;
  const installOverlays = () => {
    if (overlaysInstalled || !map.style || !map.isStyleLoaded()) return;
    overlaysInstalled = true;
    // Grey zone first, then RU control, then icons — the order the spec requires.
    map.addSource('grey', {type: 'geojson', data: emptyGeometry()});
    map.addLayer({
      id: 'grey-fill', type: 'fill', source: 'grey',
      paint: {'fill-color': '#9aa0a6', 'fill-opacity': 0.35},
    });
    map.addLayer({
      id: 'grey-line', type: 'line', source: 'grey',
      paint: {'line-color': '#7c8288', 'line-width': 0.8, 'line-dasharray': [3, 2]},
    });

    map.addSource('ru', {type: 'geojson', data: emptyGeometry()});
    map.addLayer({
      id: 'ru-fill', type: 'fill', source: 'ru',
      paint: {'fill-color': '#D52B1E', 'fill-opacity': 0.25},
    });
    map.addLayer({
      id: 'ru-line', type: 'line', source: 'ru',
      paint: {'line-color': '#8e1d14', 'line-width': 1.5},
    });

    map.addSource('events', {type: 'geojson', data: State.events});
    // Shape = event type (plans/MAP_SYMBOLS.md). SDF sprites keep the colour data-driven with
    // the same expression the circles used; if sprite generation is impossible (2D canvas
    // denied where WebGL works), the old circle layer is the fallback.
    if (installEventImages()) {
      eventLayerId = 'events-icons';
      map.addLayer({
        id: eventLayerId, type: 'symbol', source: 'events',
        filter: ['!', ['has', 'cluster']],
        layout: {
          'icon-image': ['match', ['get', 'event_type'],
            ...Object.keys(EVENT_SHAPES).flatMap((t) => [t, shapeImageId(t)]),
            shapeImageId('other')],
          // Sprites are 64px at pixelRatio 2 (32px base). A touch larger than the old circle
          // diameters (10/16/22px): a silhouette needs more pixels than a disc to read.
          'icon-size': ['interpolate', ['linear'], ['zoom'], 4, 0.5, 8, 0.75, 12, 1],
          // Never collision-cull a marker — every circle always rendered, so must every shape.
          'icon-allow-overlap': true,
          'icon-ignore-placement': true,
        },
        paint: {
          'icon-color': pointColorExpression(),
          'icon-halo-color': '#ffffff',
          'icon-halo-width': 1.2,
          // Low-confidence extractions render faintly and never count as evidence (PLAN §21).
          'icon-opacity': lowConfidenceOpacity(),
        },
      });
      // Custom images do not survive a setStyle(); rebuild rather than lose the markers.
      map.on('styleimagemissing', (e) => {
        if (e.id.startsWith('shape-')) installEventImages();
      });
    } else {
      eventLayerId = 'events-circles';
      map.addLayer({
        id: eventLayerId, type: 'circle', source: 'events',
        filter: ['!', ['has', 'cluster']],
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 5, 8, 8, 12, 11],
          'circle-color': pointColorExpression(),
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1.2,
          // Low-confidence extractions render faintly and never count as evidence (PLAN §21).
          'circle-opacity': lowConfidenceOpacity(),
          'circle-stroke-opacity': lowConfidenceOpacity(),
        },
      });
      setStatus('marker shapes unavailable — showing circles', true);
    }
    map.addLayer({
      id: 'clusters', type: 'circle', source: 'events',
      filter: ['has', 'cluster'],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['get', 'count'], 1, 10, 50, 18, 500, 28],
        // Interpolated in Lab so the ramp is perceptually even and does not muddy near the middle.
        'circle-color': clusterColorExpression(),
        'circle-opacity': 0.85,
        'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1.5,
      },
    });

    map.on('click', eventLayerId, onEventClick);
    map.on('click', 'clusters', onClusterClick);
    for (const layer of [eventLayerId, 'clusters']) {
      map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
    }
    map.on('mousemove', 'clusters', onClusterHover);
    map.on('mouseleave', 'clusters', hideClusterTooltip);
    map.on('moveend', () => refreshEvents());

    refreshFrontline();
    refreshEvents();
  };

  map.on('load', installOverlays);
  map.on('styledata', installOverlays);
  // Belt and braces: if neither event ever reports a ready style (a wedged tile source can stop
  // 'load' firing at all), poll briefly rather than leaving the user with an empty map.
  let attempts = 0;
  const watchdog = setInterval(() => {
    attempts += 1;
    installOverlays();
    if (overlaysInstalled || attempts > 40) {
      clearInterval(watchdog);
      if (!overlaysInstalled) setStatus('map layers failed to initialise', true);
    }
  }, 250);
}

function pointColorExpression() {
  // A saved marker is yellow whatever the colour mode — the reader's own flag outranks the
  // editorial encoding, and it is what makes a shortlisted spot findable at a glance.
  const base = pointColorBase();
  return ['case', ['==', ['get', 'saved'], true], SAVED_COLOR, base];
}

function lowConfidenceOpacity() {
  return ['case',
    ['<', ['coalesce', ['get', 'confidence'], 1], State.config.low_confidence_floor], 0.45, 0.95];
}

function pointColorBase() {
  if (State.colorMode === 'beneficiary') {
    return ['match', ['coalesce', ['get', 'beneficiary'], 'none'],
      'ua', BENEFICIARY_COLORS.ua,
      'ru', BENEFICIARY_COLORS.ru,
      LEAN_MID];   // unassessed — mostly verification records, which claim no advantage
  }
  return ['match', ['get', 'perspective'],
    'ukrainian', '#0057B7', 'russian', '#D52B1E',
    'western', '#2E7D32', 'neutral', '#757575', '#757575'];
}

/* The saved flag lives on the features rather than in a MapLibre filter, so it can be recomputed
   locally the instant the shortlist changes without refetching. */
function decorateAndFilter(collection) {
  const features = (collection.features || []).map((feature) => {
    if (feature.properties?.cluster) return feature;
    const saved = Saved.has(feature.properties.news_item_id);
    return {...feature, properties: {...feature.properties, saved}};
  });
  const shown = State.savedOnly
    ? features.filter((f) => !f.properties.cluster && f.properties.saved)
    : features;
  return {...collection, features: shown};
}

function repaintSaved() {
  if (map && map.getSource('events')) {
    map.getSource('events').setData(decorateAndFilter(State.events));
  }
  const count = Saved.size;
  const toggle = $('saved-only');
  if (toggle) {
    toggle.textContent = State.savedOnly ? `★ Saved only (${count})` : `☆ Saved (${count})`;
    toggle.classList.toggle('active', State.savedOnly);
    toggle.disabled = count === 0 && !State.savedOnly;
  }
  renderFeedSavedState();
}

function clusterColorExpression() {
  const field = State.colorMode === 'beneficiary' ? 'lean_beneficiary' : 'lean';
  return ['interpolate-lab', ['linear'], ['get', field],
    0, LEAN_UA, 0.5, LEAN_MID, 1, LEAN_RU];
}

function applyColorMode() {
  const beneficiary = State.colorMode === 'beneficiary';
  $('mode-perspective').classList.toggle('active', !beneficiary);
  $('mode-beneficiary').classList.toggle('active', beneficiary);
  $('mode-perspective').setAttribute('aria-checked', String(!beneficiary));
  $('mode-beneficiary').setAttribute('aria-checked', String(beneficiary));
  $('mode-help').textContent = beneficiary
    ? 'Who the event favours, regardless of who reported it.'
    : 'Who is reporting it.';

  if (map && map.getLayer(eventLayerId)) {
    const colorProp = eventLayerId === 'events-icons' ? 'icon-color' : 'circle-color';
    map.setPaintProperty(eventLayerId, colorProp, pointColorExpression());
    map.setPaintProperty('clusters', 'circle-color', clusterColorExpression());
  }
  buildLegend();
  hideClusterTooltip();
}

function setColorMode(mode) {
  if (State.colorMode === mode) return;
  State.colorMode = mode;
  // Both axes ship in every response, so switching needs no refetch.
  applyColorMode();
}

function emptyGeometry() {
  return {type: 'Feature', properties: {}, geometry: {type: 'MultiPolygon', coordinates: []}};
}

function detailForZoom() {
  const zoom = map ? map.getZoom() : 6;
  if (zoom < 6) return 'low';
  if (zoom < 9) return 'mid';
  return 'high';
}

async function refreshFrontline() {
  const params = new URLSearchParams({detail: detailForZoom()});
  if (isHistorical()) params.set('at', State.at.toISOString());
  const url = `/api/frontline?${params}`;
  const token = ++State.seq.frontline;
  try {
    const {data, unchanged} = await getJSON(url, {useEtag: true});
    if (token !== State.seq.frontline) return;   // superseded while in flight
    if (unchanged || !data) return;
    for (const layer of ['ru', 'grey']) {
      const geometry = data.layers?.[layer] ?? {type: 'MultiPolygon', coordinates: []};
      map.getSource(layer)?.setData({type: 'Feature', properties: {}, geometry});
    }
    const meta = data.meta || {};
    const parts = [];
    if (data.valid_at) {
      parts.push(isHistorical()
        ? `Frontline as of ${formatInstant(new Date(data.valid_at))}`
        : `Frontline built ${relativeTime(data.valid_at)}`);
    }
    if (meta.deepstate_id) parts.push('DeepStateMap');
    if (meta.isw_editdate) parts.push('ISW');
    if (meta.degraded) parts.push(`⚠ ${meta.degraded}`);
    $('frontline-meta').textContent = parts.join(' · ');
  } catch (err) {
    if (token === State.seq.frontline) setStatus(`frontline unavailable (${err.message})`, true);
  }
}

function currentBBox() {
  if (!map) return null;
  const b = map.getBounds();
  // Pad by ~15% so panning does not immediately blank the icons.
  const padX = (b.getEast() - b.getWest()) * 0.15;
  const padY = (b.getNorth() - b.getSouth()) * 0.15;
  return [
    (b.getWest() - padX).toFixed(4), (b.getSouth() - padY).toFixed(4),
    (b.getEast() + padX).toFixed(4), (b.getNorth() + padY).toFixed(4),
  ].join(',');
}

/* The instant the map is showing: an explicit scrub position, or now. */
function currentInstant() {
  return State.at ?? new Date();
}

function isHistorical() {
  return State.at !== null;
}

function eventsURL() {
  const params = new URLSearchParams();
  params.set('bbox', currentBBox());
  const end = currentInstant();
  params.set('from', new Date(end.getTime() - State.rangeHours * 3600e3).toISOString());
  // A scrubbed view is a window ending at the chosen instant, so nothing "from the future"
  // of that moment leaks onto the map.
  if (isHistorical()) params.set('to', end.toISOString());
  params.set('zoom', String(Math.round(map ? map.getZoom() : 6)));
  if (State.types.size && State.types.size !== State.config.event_types.length) {
    params.set('types', [...State.types].join(','));
  }
  if (State.perspectives.size && State.perspectives.size !== State.config.perspectives.length) {
    params.set('perspectives', [...State.perspectives].join(','));
  }
  // Clusters cannot be filtered down to a shortlist client-side, so ask for plain points.
  if (State.savedOnly) params.set('cluster', 'off');
  return `/api/events?${params}`;
}

async function refreshEvents() {
  if (!map || !map.getSource('events')) return;
  const url = eventsURL();
  const token = ++State.seq.events;
  try {
    const {data, unchanged} = await getJSON(url, {useEtag: true});
    if (token !== State.seq.events) return;      // superseded while in flight
    if (unchanged || !data) return;
    State.events = data;
    const shown = decorateAndFilter(data);
    map.getSource('events').setData(shown);
    const meta = data.meta || {};
    if (State.savedOnly) {
      setStatus(`${shown.features.length} saved of ${meta.total} events here`);
    } else {
      setStatus(meta.clustered
        ? `${meta.total} events (clustered)`
        : `${meta.returned} of ${meta.total} events`);
    }
  } catch (err) {
    if (token === State.seq.events) setStatus(`events unavailable (${err.message})`, true);
  }
}

function popupHTML(p, pager = null) {
  const glyph = State.config.glyphs[p.event_type] || '·';
  const label = EVENT_LABELS[p.event_type] || p.event_type;
  const title = p.title ? escapeHTML(p.title) : '(untitled report)';
  const heading = p.url
    ? `<a href="${escapeHTML(p.url)}" target="_blank" rel="noopener noreferrer">${title}</a>`
    : title;
  const confidence = p.confidence == null ? '—' : Number(p.confidence).toFixed(2);
  const claim = p.claim_id
    ? `<div class="row"><span class="chip">claim #${p.claim_id} · ${escapeHTML(p.claim_status || '')}</span></div>`
    : '';
  const favours = p.beneficiary === 'ua' ? 'Favours Ukraine'
    : p.beneficiary === 'ru' ? 'Favours Russia'
    : 'Beneficiary not assessed';
  const favoursColor = p.beneficiary === 'ua' ? LEAN_UA
    : p.beneficiary === 'ru' ? LEAN_RU : LEAN_MID;
  const coords = p.coord_source === 'explicit_coords' ? 'explicit coordinates' : 'gazetteer match';
  const saved = Saved.has(p.news_item_id);
  const star = p.news_item_id
    ? `<button class="star ${saved ? 'on' : ''}" type="button" data-pop="star"
         data-item="${p.news_item_id}" aria-pressed="${saved}"
         title="Save to my list">${saved ? '★' : '☆'}</button>`
    : '';
  const origBtn = p.translated
    ? `<button class="orig-btn" type="button" data-pop="orig">original</button>` : '';
  // English summary from the pipeline: focused on this marker's location when the item names it,
  // the item's general summary otherwise. Absent until the summary task has run.
  const summary = p.summary
    ? `<p class="summary">${escapeHTML(p.summary)}</p>` : '';
  // Several reports can share one spot (same settlement, stacked icons); the pager lets the
  // reader step through the pile instead of only ever reaching the top marker.
  const pagerRow = pager && pager.total > 1
    ? `<div class="pager">
        <button class="pager-btn" type="button" data-pop="prev" aria-label="Previous report here">‹</button>
        <span class="pager-count">${pager.index + 1}/${pager.total}</span>
        <button class="pager-btn" type="button" data-pop="next" aria-label="Next report here">›</button>
        <span class="pager-note">reports at this spot</span>
      </div>`
    : '';

  return `<div class="popup" data-title-en="${escapeHTML(p.title || '')}"
       data-title-orig="${escapeHTML(p.title_original || '')}">
    ${pagerRow}
    <div class="row">
      ${star}
      <span class="pill p-${escapeHTML(p.perspective)}">${escapeHTML(p.perspective)}</span>
      <span class="source">${escapeHTML(p.source_name)}</span>
    </div>
    <h3>${heading}${origBtn}</h3>
    ${summary}
    <div class="row"><span class="glyph">${glyph}</span> ${escapeHTML(label)}</div>
    <div class="row"><span class="dot" style="background:${favoursColor}"></span>
      <span>${favours}</span></div>
    ${claim}
    <p class="meta">${relativeTime(p.published_at)} · confidence ${confidence} · ${coords}
      · source tier ${p.reliability_tier ?? '—'}</p>
  </div>`;
}

function onEventClick(e) {
  // Every marker under the click, not just the topmost: co-located events stack their icons,
  // and the ones underneath would otherwise be unreachable. GeoJSON sources are tiled
  // internally, so the same feature can be reported more than once — dedupe by event id.
  const seen = new Set();
  const group = (e.features || [])
    .filter((f) => {
      const key = f.properties.id ?? `${f.properties.news_item_id}:${f.properties.event_type}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((a, b) => String(b.properties.published_at).localeCompare(String(a.properties.published_at)));
  if (group.length) openEventPopup(group);
}

/* One popup paging through a stack of markers with ‹ ›, newest first. A single feature renders
   exactly the old popup — no pager row. */
function openEventPopup(features, startIndex = 0) {
  const popup = new maplibregl.Popup({closeButton: true, maxWidth: '300px'});
  const render = (i) => {
    const index = ((i % features.length) + features.length) % features.length;
    const feature = features[index];
    // The stack is "under one click", not necessarily one exact point — follow the current item.
    popup.setLngLat(feature.geometry.coordinates);
    popup.setHTML(popupHTML(feature.properties, {index, total: features.length}));
    if (!popup.isOpen()) popup.addTo(map);
    wirePopup(popup.getElement(), feature.properties,
      {index, total: features.length, onPage: render});
  };
  render(startIndex);
  return popup;
}

/* Popup markup is a string, so its controls are wired after it lands in the DOM. */
function wirePopup(root, props, pager = null) {
  if (!root) return;
  if (pager && pager.onPage) {
    root.querySelector('[data-pop=prev]')
      ?.addEventListener('click', () => pager.onPage(pager.index - 1));
    root.querySelector('[data-pop=next]')
      ?.addEventListener('click', () => pager.onPage(pager.index + 1));
  }
  const star = root.querySelector('[data-pop=star]');
  if (star) {
    star.addEventListener('click', () => {
      const nowSaved = Saved.toggle(props.news_item_id, props.title);
      star.textContent = nowSaved ? '★' : '☆';
      star.classList.toggle('on', nowSaved);
      star.setAttribute('aria-pressed', String(nowSaved));
      repaintSaved();
    });
  }
  const orig = root.querySelector('[data-pop=orig]');
  if (orig) {
    const box = root.querySelector('.popup');
    let showingOriginal = false;
    orig.addEventListener('click', () => {
      showingOriginal = !showingOriginal;
      const target = root.querySelector('h3 a') || root.querySelector('h3');
      const text = showingOriginal ? box.dataset.titleOrig : box.dataset.titleEn;
      if (target.tagName === 'A') target.textContent = text;
      else target.firstChild.textContent = text;
      orig.textContent = showingOriginal ? 'English' : 'original';
    });
  }
}

/* The cluster's colour states a lean; the tooltip states the numbers behind it, so the encoding
   is never colour-alone. */
let clusterTooltip = null;

function clusterCounts(props, key = 'counts') {
  let counts = props[key];
  if (typeof counts === 'string') {
    try { counts = JSON.parse(counts); } catch { counts = null; }
  }
  return counts || {};
}

function onClusterHover(e) {
  const feature = e.features?.[0];
  if (!feature) return;
  const props = feature.properties;
  const beneficiary = State.colorMode === 'beneficiary';

  let rows;
  let verdict;
  if (beneficiary) {
    const counts = clusterCounts(props, 'beneficiary_counts');
    rows = [
      [LEAN_UA, counts.ukrainian || 0, 'favour Ukraine'],
      [LEAN_RU, counts.russian || 0, 'favour Russia'],
      [LEAN_MID, counts.unassessed || 0, 'not assessed'],
    ]
      .filter(([, n]) => n > 0)
      .map(([color, n, label]) => `<div class="row">
        <span class="dot" style="background:${color}"></span><span>${n} ${label}</span></div>`)
      .join('');
    const assessed = Number(props.assessed) || 0;
    const lean = Number(props.lean_beneficiary);
    if (assessed === 0) verdict = 'nothing here has an assessed beneficiary';
    else if (lean > 0.58) verdict = 'mostly favours Russia';
    else if (lean < 0.42) verdict = 'mostly favours Ukraine';
    else verdict = 'favours both sides about equally';
  } else {
    const counts = clusterCounts(props);
    rows = PERSPECTIVE_ORDER
      .filter((p) => (counts[p] || 0) > 0)
      .map((p) => `<div class="row"><span class="dot" style="background:${State.config.colors[p]}"></span>
          <span>${counts[p]} ${p}</span></div>`)
      .join('');
    const partisan = Number(props.partisan) || 0;
    const lean = Number(props.lean);
    if (partisan === 0) verdict = 'no Ukrainian or Russian sources here';
    else if (lean > 0.58) verdict = 'mostly Russian-perspective reporting';
    else if (lean < 0.42) verdict = 'mostly Ukrainian-perspective reporting';
    else verdict = 'balanced Ukrainian / Russian reporting';
  }

  const html = `<div class="cluster-tip"><strong>${props.count} reports</strong>
      <div class="verdict">${verdict}</div>${rows}
      <div class="hint">click to zoom in</div></div>`;

  if (!clusterTooltip) {
    clusterTooltip = new maplibregl.Popup({
      closeButton: false, closeOnClick: false, offset: 14, className: 'tip-popup',
    });
  }
  clusterTooltip.setLngLat(feature.geometry.coordinates).setHTML(html).addTo(map);
}

function hideClusterTooltip() {
  clusterTooltip?.remove();
}

function onClusterClick(e) {
  hideClusterTooltip();
  const feature = e.features?.[0];
  if (!feature) return;
  let box = feature.properties.expansion_bbox;
  if (typeof box === 'string') { try { box = JSON.parse(box); } catch { box = null; } }
  if (Array.isArray(box) && box.length === 4) {
    map.fitBounds([[box[0], box[1]], [box[2], box[3]]], {padding: 60, maxZoom: 12});
  } else {
    map.easeTo({center: feature.geometry.coordinates, zoom: map.getZoom() + 2});
  }
}

/* ------------------------------------------------------------------ filters + legend */
function buildFilters() {
  const perspectiveBox = $('perspective-filters');
  for (const perspective of PERSPECTIVE_ORDER) {
    if (!State.config.perspectives.includes(perspective)) continue;
    State.perspectives.add(perspective);
    perspectiveBox.append(checkbox(perspective, true, (on) => {
      on ? State.perspectives.add(perspective) : State.perspectives.delete(perspective);
      refreshEvents();
    }, `<span class="pill p-${perspective}">${perspective}</span>`));
  }

  const typeBox = $('type-filters');
  for (const type of State.config.event_types) {
    State.types.add(type);
    typeBox.append(checkbox(type, true, (on) => {
      on ? State.types.add(type) : State.types.delete(type);
      refreshEvents();
    }, `${shapeIconHTML(type)} ${EVENT_LABELS[type] || type}`));
  }

  $('range').addEventListener('change', (e) => {
    State.rangeHours = Number(e.target.value);
    updateTimeReadout();
    refreshEvents();
  });
  $('layer-ru').addEventListener('change', (e) => toggleLayers(['ru-fill', 'ru-line'], e.target.checked));
  $('layer-grey').addEventListener('change', (e) => toggleLayers(['grey-fill', 'grey-line'], e.target.checked));

  // Panels start collapsed; each one remembers its last open/closed choice in this browser.
  let panelState = {};
  try { panelState = JSON.parse(localStorage.getItem(PANELS_KEY) || '{}'); } catch { /* stay collapsed */ }
  for (const [id, button, body] of [['filters', 'filters-toggle', 'filters-body'],
                                    ['legend', 'legend-toggle', 'legend-body'],
                                    ['news-rail', 'news-rail-toggle', 'news-rail-body']]) {
    const setExpanded = (expanded) => {
      $(button).setAttribute('aria-expanded', String(expanded));
      $(body).hidden = !expanded;
    };
    setExpanded(panelState[id] === true);
    $(button).addEventListener('click', () => {
      const expanded = $(button).getAttribute('aria-expanded') !== 'true';
      setExpanded(expanded);
      panelState[id] = expanded;
      try { localStorage.setItem(PANELS_KEY, JSON.stringify(panelState)); } catch { /* private mode */ }
    });
  }
}

function toggleLayers(ids, visible) {
  for (const id of ids) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
  }
}

function checkbox(value, checked, onChange, labelHTML) {
  const label = document.createElement('label');
  label.className = 'check';
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.checked = checked;
  input.value = value;
  input.addEventListener('change', () => onChange(input.checked));
  label.append(input);
  const span = document.createElement('span');
  span.innerHTML = labelHTML;
  label.append(span);
  return label;
}

function buildLegend() {
  const beneficiary = State.colorMode === 'beneficiary';
  const perspectiveList = $('legend-perspectives');
  perspectiveList.innerHTML = '';

  const intro = $('legend-intro');
  if (intro) {
    intro.textContent = beneficiary
      ? 'Colour = who the event favours. Shape = what happened. Both are also written out in '
        + 'every popup and tooltip, so colour is never the only cue.'
      : 'Colour = whose reporting it is. Shape = what happened. Both are also written out in '
        + 'every popup and tooltip, so colour is never the only cue.';
  }

  if (beneficiary) {
    const rows = [
      [LEAN_UA, 'Favours Ukraine', 'a Ukrainian gain, or a Russian loss'],
      [LEAN_RU, 'Favours Russia', 'a Russian gain, or a Ukrainian loss'],
      [LEAN_MID, 'Neither, or not assessed',
        'verified geolocations carry no judgement of advantage'],
    ];
    for (const [color, label, note] of rows) {
      const li = document.createElement('li');
      li.innerHTML = `<span class="dot" style="background:${color}"></span>
        <span><em>${label}</em> — ${note}</span>`;
      perspectiveList.append(li);
    }
  } else {
    const descriptions = {
      ukrainian: 'Ukrainian sources', russian: 'Russian sources',
      western: 'Western sources', neutral: 'Neutral / international sources',
    };
    for (const perspective of PERSPECTIVE_ORDER) {
      const li = document.createElement('li');
      li.innerHTML = `<span class="dot" style="background:${State.config.colors[perspective]}"></span>
        <span>${descriptions[perspective]} — <em>${perspective}</em></span>`;
      perspectiveList.append(li);
    }
  }
  const leanList = $('legend-lean');
  if (leanList) {
    leanList.innerHTML = `
      <li><span>Zoomed out, reports group into circles: <em>size</em> = how many,
        <em>colour</em> = ${beneficiary ? 'which side the events favour' : 'whose reporting dominates'}.
        Hover one for the counts.</span></li>
      <li><span class="ramp" aria-hidden="true"></span></li>
      <li class="ramp-ends"><span>${beneficiary ? 'all favour Ukraine' : 'all Ukrainian'}</span>
        <span>balanced</span>
        <span>${beneficiary ? 'all favour Russia' : 'all Russian'}</span></li>`;
  }

  const typeList = $('legend-types');
  typeList.innerHTML = '';
  for (const type of State.config.event_types) {
    const li = document.createElement('li');
    li.innerHTML = `${shapeIconHTML(type)}
      <span>${EVENT_LABELS[type] || type}</span>`;
    typeList.append(li);
  }
}

/* ------------------------------------------------------------------ feed */
function feedURL(cursor) {
  const params = new URLSearchParams({limit: '30'});
  if (cursor) params.set('cursor', cursor);
  const perspective = $('feed-perspective').value;
  if (perspective) params.set('perspective', perspective);
  const query = $('feed-q').value.trim();
  if (query) params.set('q', query);
  return `/api/news?${params}`;
}

async function loadFeed({reset = false} = {}) {
  if (State.feedSavedOnly) { renderSavedFeed(); return; }
  if (State.feedLoading) return;
  if (reset) {
    State.feedCursor = null;
    State.feedExhausted = false;
    $('feed').innerHTML = '';
  }
  if (State.feedExhausted) return;
  State.feedLoading = true;
  $('feed-end').textContent = 'Loading…';
  try {
    const {data} = await getJSON(feedURL(State.feedCursor));
    for (const item of data.items || []) $('feed').append(feedRow(item));
    State.feedCursor = data.next_cursor;
    State.feedExhausted = !data.next_cursor;
    $('feed-end').textContent = $('feed').children.length === 0
      ? 'Nothing ingested yet for this filter.'
      : (State.feedExhausted ? 'End of feed.' : 'Scroll for more…');
  } catch (err) {
    $('feed-end').textContent = `Feed unavailable (${err.message})`;
  } finally {
    State.feedLoading = false;
  }
}

function feedRow(item, {rail = false} = {}) {
  const li = document.createElement('li');
  li.dataset.itemId = item.id;
  li.dataset.publishedAt = item.published_at || '';
  const placed = (item.events || []).find((e) => e.placed);
  const chips = (item.events || [])
    // In the rail everything is unplaced by definition, so the suffix would be noise.
    .map((e) => `<span class="chip">${State.config.glyphs[e.event_type] || '·'}
      ${EVENT_LABELS[e.event_type] || e.event_type}${e.placed || rail ? '' : ' (unplaced)'}</span>`)
    .join('');
  const saved = Saved.has(item.id);
  li.classList.toggle('saved', saved);

  const headline = escapeHTML(item.title || '(untitled)');
  const titleHTML = item.url
    ? `<a href="${escapeHTML(item.url)}" target="_blank" rel="noopener noreferrer">${headline}</a>`
    : headline;
  const original = item.translated
    ? `<button class="orig-btn" type="button" data-role="orig">original</button>`
    : '';
  // Same faithful-labelling policy as the popups: confidence comes from the extraction,
  // reliability from the source record — never inferred.
  let railMeta = '';
  if (rail) {
    const confidences = (item.events || []).map((e) => e.confidence).filter((c) => c != null);
    const parts = confidences.length ? [`confidence ${Math.max(...confidences).toFixed(2)}`] : [];
    if (item.source.reliability_tier != null) parts.push(`source tier ${item.source.reliability_tier}`);
    if (parts.length) railMeta = `<span class="when">· ${parts.join(' · ')}</span>`;
  }

  li.innerHTML = `
    <div class="head">
      <button class="star ${saved ? 'on' : ''}" type="button" data-role="star"
              aria-pressed="${saved}" title="Save to my list">${saved ? '★' : '☆'}</button>
      <span class="pill p-${escapeHTML(item.source.perspective)}">${escapeHTML(item.source.perspective)}</span>
      <span class="source">${escapeHTML(item.source.name)}</span>
      <span class="when">· ${relativeTime(item.published_at)}</span>${railMeta}
    </div>
    <h3>${titleHTML}${original}</h3>
    ${item.snippet ? `<p class="snippet">${escapeHTML(item.snippet)}</p>` : ''}
    ${chips ? `<div class="chips">${chips}</div>` : ''}`;

  li.querySelector('[data-role=star]').addEventListener('click', () => {
    const nowSaved = Saved.toggle(item.id, item.title);
    li.classList.toggle('saved', nowSaved);
    const star = li.querySelector('[data-role=star]');
    star.textContent = nowSaved ? '★' : '☆';
    star.classList.toggle('on', nowSaved);
    star.setAttribute('aria-pressed', String(nowSaved));
    repaintSaved();          // also repaints the same story's row in the other list
    if (!rail && State.feedSavedOnly && !nowSaved) li.remove();
  });

  const origButton = li.querySelector('[data-role=orig]');
  if (origButton) {
    let showingOriginal = false;
    origButton.addEventListener('click', () => {
      showingOriginal = !showingOriginal;
      const text = showingOriginal ? (item.title_original || '') : (item.title || '');
      const anchor = li.querySelector('h3 a');
      if (anchor) anchor.textContent = text; else li.querySelector('h3').firstChild.textContent = text;
      const snippet = li.querySelector('.snippet');
      if (snippet) {
        snippet.textContent = showingOriginal
          ? (item.snippet_original || '') : (item.snippet || '');
      }
      origButton.textContent = showingOriginal ? 'English' : 'original';
    });
  }

  if (placed) {
    const button = document.createElement('button');
    button.className = 'show-map';
    button.textContent = 'Show on map';
    button.addEventListener('click', () => showOnMap(placed, item));
    li.append(button);
  }
  return li;
}

function renderFeedSavedState() {
  for (const li of document.querySelectorAll('#feed li, #rail-feed li')) {
    const saved = Saved.has(li.dataset.itemId);
    li.classList.toggle('saved', saved);
    const star = li.querySelector('[data-role=star]');
    if (star) {
      star.textContent = saved ? '★' : '☆';
      star.classList.toggle('on', saved);
      star.setAttribute('aria-pressed', String(saved));
    }
  }
}

/* The shortlist is local, so it can be listed without the server — including items whose
   original request has long scrolled away. */
function renderSavedFeed() {
  const feed = $('feed');
  feed.innerHTML = '';
  const entries = Object.entries(Saved.meta)
    .sort((a, b) => String(b[1].at).localeCompare(String(a[1].at)));
  if (!entries.length) {
    $('feed-end').textContent = 'Nothing saved yet — click ☆ on a report to shortlist it.';
    return;
  }
  // Fetch the current version of each saved item so titles and events stay accurate.
  Promise.all(entries.map(([id]) =>
    fetch(`/api/news?limit=1&cursor=${Number(id) + 1}`).then((r) => r.json())
      .then((d) => (d.items || []).find((i) => String(i.id) === String(id)))
      .catch(() => null)))
    .then((items) => {
      feed.innerHTML = '';
      let shown = 0;
      items.forEach((item, i) => {
        if (item) { feed.append(feedRow(item)); shown += 1; return; }
        // Still list it from the local note, so a saved item never silently vanishes.
        const [id, meta] = entries[i];
        const li = document.createElement('li');
        li.dataset.itemId = id;
        li.className = 'saved';
        li.innerHTML = `<div class="head"><span class="when">saved
            ${relativeTime(meta.at)}</span></div>
          <h3>${escapeHTML(meta.title || `item #${id}`)}</h3>
          <p class="snippet">No longer served by the API — it may have been removed.</p>`;
        feed.append(li);
        shown += 1;
      });
      $('feed-end').textContent = `${shown} saved item${shown === 1 ? '' : 's'}.`;
    });
}

/* ------------------------------------------------------------------ news rail
   General news beside the map (plans/NEWS_RAIL.md): items with no clear location — nothing the
   gazetteer could resolve and no explicit coordinates — so they never get a marker. Same rows
   as the Feed tab, same shortlist, filtered server-side with placement=unplaced. */
const RAIL_PAGE = 20;

function railURL(cursor) {
  // order=published: latest *reporting* first, not latest ingest — backfilled history has new
  // ids with old dates and must not float to the top. The cursor is opaque (issued by the API).
  const params = new URLSearchParams({
    placement: 'unplaced',
    order: 'published',
    max_tier: String(State.railTier),
    limit: String(RAIL_PAGE),
  });
  if (cursor) params.set('cursor', cursor);
  // The rail follows the time scrubber: past news over a past map, same window as the markers.
  if (isHistorical()) {
    params.set('from', new Date(State.at.getTime() - State.rangeHours * HOUR_MS).toISOString());
    params.set('to', State.at.toISOString());
  }
  return `/api/news?${params}`;
}

async function loadRail({reset = false} = {}) {
  if (State.railLoading) return;
  if (reset) {
    State.railCursor = null;
    State.railExhausted = false;
    $('rail-feed').innerHTML = '';
  }
  if (State.railExhausted) return;
  State.railLoading = true;
  $('rail-end').textContent = 'Loading…';
  try {
    const {data} = await getJSON(railURL(State.railCursor));
    for (const item of data.items || []) $('rail-feed').append(feedRow(item, {rail: true}));
    State.railCursor = data.next_cursor;
    State.railExhausted = !data.next_cursor;
    $('rail-end').textContent = $('rail-feed').children.length === 0
      ? 'Nothing at this source quality for this window.'
      : (State.railExhausted ? 'End.' : 'Scroll for more…');
  } catch (err) {
    // Keep the panel with the error line rather than hiding it — the next poll retries.
    $('rail-end').textContent = `Unavailable (${err.message})`;
  } finally {
    State.railLoading = false;
  }
}

/* Poll tick: prepend only unseen items, never reset — the reader's scroll position must not be
   yanked. A row whose item got geocoded since it was rendered simply stays until the next full
   reload; it stops appearing in new pages. */
async function refreshRail() {
  if (State.railLoading) return;
  const list = $('rail-feed');
  const top = list.firstElementChild;
  if (!top) { loadRail({reset: true}); return; }
  // The list is in published order, so "new" = newer than the top row's timestamp (ids won't
  // do: backfill hands out large ids with old dates). The id set guards against duplicating a
  // tie on the exact same timestamp.
  const topTime = new Date(top.dataset.publishedAt || 0).getTime();
  try {
    const {data, unchanged} = await getJSON(railURL(null), {useEtag: true});
    if (unchanged || !data) return;
    const have = new Set(Array.from(list.children, (li) => li.dataset.itemId));
    const fresh = (data.items || []).filter((item) =>
      !have.has(String(item.id)) && new Date(item.published_at).getTime() >= topTime);
    for (const item of fresh.reverse()) list.prepend(feedRow(item, {rail: true}));
  } catch { /* transient; the next tick retries */ }
}

function showOnMap(event, item) {
  switchTab('map');
  const jump = () => {
    map.flyTo({center: [event.lon, event.lat], zoom: 11, duration: 900});
    // A single pseudo-feature: same popup as a map click (wired star/original included),
    // just without a pager.
    openEventPopup([{
      geometry: {type: 'Point', coordinates: [event.lon, event.lat]},
      properties: {
        event_type: event.event_type,
        news_item_id: item.id,
        perspective: item.source.perspective,
        source_name: item.source.name,
        title: item.title,
        title_original: item.title_original,
        translated: item.translated,
        summary: event.summary || item.summary,
        url: item.url,
        published_at: item.published_at,
        confidence: event.confidence,
        reliability_tier: item.source.reliability_tier,
        coord_source: 'gazetteer_match',
      },
    }]);
  };
  map.loaded() ? jump() : map.once('load', jump);
}


/* ------------------------------------------------------------------ time scrubber */
const HOUR_MS = 3600e3;

/* Always rendered in UTC: every upstream in this system publishes UTC, and frontline reporting
   is quoted in UTC, so showing the viewer's local time would silently shift dates. */
function formatInstant(date) {
  const iso = date.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

function timelineBounds() {
  const tl = State.timeline;
  if (!tl || !tl.from) return null;
  const from = new Date(tl.from).getTime();
  // The scrubber always reaches "now", so the live end of the track is the present.
  const to = Math.max(new Date(tl.to || Date.now()).getTime(), Date.now());
  return to > from ? {from, to} : null;
}

function sliderToInstant(value) {
  const bounds = timelineBounds();
  if (!bounds) return null;
  const ratio = Number(value) / 1000;
  return new Date(bounds.from + ratio * (bounds.to - bounds.from));
}

function instantToSlider(date) {
  const bounds = timelineBounds();
  if (!bounds) return 1000;
  const clamped = Math.min(Math.max(date.getTime(), bounds.from), bounds.to);
  return Math.round(((clamped - bounds.from) / (bounds.to - bounds.from)) * 1000);
}

/* Snap to the nearest frontline snapshot at or before the instant, so the geometry shown is
   real rather than an arbitrary point between two published snapshots. */
function nearestSnapshotBefore(date) {
  const snaps = State.timeline?.snapshots;
  if (!snaps || !snaps.length) return null;
  let best = null;
  for (const iso of snaps) {
    const t = new Date(iso);
    if (t <= date) best = t; else break;
  }
  return best;
}

function updateTimeReadout() {
  const live = !isHistorical();
  $('time-live').classList.toggle('active', live);
  $('time-play').textContent = State.playing ? '❚❚' : '▶';
  document.body.classList.toggle('historical', !live);

  if (live) {
    $('time-label').textContent = 'Live';
    $('time-sub').textContent = `showing the last ${State.rangeHours} h`;
    return;
  }
  $('time-label').textContent = formatInstant(State.at);
  const snap = nearestSnapshotBefore(State.at);
  $('time-sub').textContent = snap
    ? `frontline of ${formatInstant(snap)} · ${State.rangeHours} h of reports`
    : `no frontline snapshot this far back · ${State.rangeHours} h of reports`;
}

function setInstant(date, {fromSlider = false} = {}) {
  const bounds = timelineBounds();
  if (date && bounds && date.getTime() >= bounds.to - 60e3) date = null;  // snapped to live
  State.at = date;
  if (!fromSlider) {
    $('time-slider').value = date ? instantToSlider(date) : 1000;
  }
  updateTimeReadout();
  refreshFrontline();
  refreshEvents();
  loadRail({reset: true});
}

function stepInstant(hours) {
  const bounds = timelineBounds();
  if (!bounds) return;
  const base = currentInstant().getTime();
  const next = new Date(Math.min(Math.max(base + hours * HOUR_MS, bounds.from), bounds.to));
  setInstant(next);
}

function togglePlay(force) {
  const next = force ?? !State.playing;
  State.playing = next;
  clearInterval(State.playTimer);
  if (next) {
    const bounds = timelineBounds();
    // Starting playback from the live end would have nowhere to run, so rewind a little first.
    if (!isHistorical() && bounds) {
      setInstant(new Date(Math.max(bounds.from, bounds.to - 14 * 24 * HOUR_MS)));
    }
    State.playTimer = setInterval(() => {
      const step = Number($('time-step').value) || 24;
      const bounds2 = timelineBounds();
      if (!bounds2) return;
      const next2 = currentInstant().getTime() + step * HOUR_MS;
      if (next2 >= bounds2.to) { setInstant(null); togglePlay(false); return; }
      setInstant(new Date(next2));
    }, 1400);
  }
  updateTimeReadout();
}

function drawHistogram() {
  const canvas = $('time-histogram');
  const bounds = timelineBounds();
  const days = State.timeline?.events_per_day || [];
  if (!canvas || !bounds) return;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(width * ratio));
  canvas.height = Math.floor(26 * ratio);
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, 26);
  if (!days.length) return;
  const peak = Math.max(...days.map((d) => d.events), 1);
  ctx.fillStyle = getComputedStyle(document.documentElement)
    .getPropertyValue('--accent').trim() || '#4c8dff';
  for (const day of days) {
    const t = new Date(day.day + 'T12:00:00Z').getTime();
    if (t < bounds.from || t > bounds.to) continue;
    const x = ((t - bounds.from) / (bounds.to - bounds.from)) * width;
    const h = Math.max(1, (day.events / peak) * 24);
    ctx.fillRect(x, 26 - h, Math.max(1, width / Math.max(days.length, 1) - 0.5), h);
  }
}

async function loadTimeline() {
  try {
    const {data} = await getJSON('/api/timeline');
    if (!data || !data.from) return;
    State.timeline = data;
    const bounds = timelineBounds();
    if (!bounds) return;
    $('timebar').hidden = false;
    $('time-start').textContent = formatInstant(new Date(bounds.from));
    $('time-end').textContent = 'now';
    drawHistogram();
    updateTimeReadout();
  } catch {
    /* the scrubber is an enhancement; the live map works without it */
  }
}

function wireScrubber() {
  const slider = $('time-slider');
  slider.addEventListener('input', () => {
    togglePlay(false);
    const instant = sliderToInstant(slider.value);
    if (!instant) return;
    State.at = Number(slider.value) >= 999 ? null : instant;
    updateTimeReadout();
  });
  // Only fetch when the drag ends, so scrubbing does not fire a request per pixel.
  const commit = () => setInstant(State.at, {fromSlider: true});
  slider.addEventListener('change', commit);

  $('time-live').addEventListener('click', () => { togglePlay(false); setInstant(null); });
  $('time-play').addEventListener('click', () => togglePlay());
  $('time-back').addEventListener('click', () => {
    togglePlay(false);
    stepInstant(-(Number($('time-step').value) || 24));
  });
  $('time-fwd').addEventListener('click', () => {
    togglePlay(false);
    stepInstant(Number($('time-step').value) || 24);
  });
  window.addEventListener('resize', drawHistogram);

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea')) return;
    const step = Number($('time-step').value) || 24;
    if (e.key === 'ArrowLeft') { togglePlay(false); stepInstant(-step); }
    else if (e.key === 'ArrowRight') { togglePlay(false); stepInstant(step); }
    else if (e.key === ' ') { e.preventDefault(); togglePlay(); }
  });
}

/* ------------------------------------------------------------------ notifications + tabs */
async function refreshNotifications() {
  try {
    const {data, unchanged} = await getJSON('/api/notifications', {useEtag: true});
    if (unchanged || !data) return;
    const box = $('notifications');
    box.innerHTML = '';
    for (const note of data.items || []) {
      const div = document.createElement('div');
      div.className = `note note-${note.level}`;
      div.innerHTML = `<strong>${escapeHTML(note.title)}</strong>${escapeHTML(note.body || '')}`;
      box.append(div);
    }
    box.hidden = (data.items || []).length === 0;
  } catch { /* notifications are cosmetic; never block the map on them */ }
}

function switchTab(which) {
  const isMap = which === 'map';
  $('tab-map').classList.toggle('active', isMap);
  $('tab-feed').classList.toggle('active', !isMap);
  $('tab-map').setAttribute('aria-selected', String(isMap));
  $('tab-feed').setAttribute('aria-selected', String(!isMap));
  $('view-map').classList.toggle('active', isMap);
  $('view-feed').classList.toggle('active', !isMap);
  $('view-map').hidden = !isMap;
  $('view-feed').hidden = isMap;
  if (isMap && map) map.resize();
  if (!isMap && $('feed').children.length === 0) loadFeed({reset: true});
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  try {
    const {data} = await getJSON('/api/config');
    State.config = data;
  } catch (err) {
    setStatus(`cannot reach the API (${err.message})`, true);
    return;
  }

  Saved.load();
  buildFilters();
  buildLegend();
  initMap();
  repaintSaved();

  loadRail({reset: true});
  $('news-rail-body').addEventListener('scroll', () => {
    const el = $('news-rail-body');
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) loadRail();
  });
  $('rail-tier').addEventListener('change', (e) => {
    State.railTier = Number(e.target.value) || 1;
    loadRail({reset: true});
  });

  const feedPerspective = $('feed-perspective');
  for (const perspective of PERSPECTIVE_ORDER) {
    if (!State.config.perspectives.includes(perspective)) continue;
    const option = document.createElement('option');
    option.value = perspective;
    option.textContent = perspective;
    feedPerspective.append(option);
  }

  $('saved-only').addEventListener('click', () => {
    State.savedOnly = !State.savedOnly;
    repaintSaved();
    refreshEvents();       // saved-only needs unclustered points from the server
  });
  $('feed-saved-only').addEventListener('click', () => {
    State.feedSavedOnly = !State.feedSavedOnly;
    const button = $('feed-saved-only');
    button.classList.toggle('active', State.feedSavedOnly);
    button.textContent = State.feedSavedOnly ? `★ Saved (${Saved.size})` : '☆ Saved';
    loadFeed({reset: true});
  });

  $('mode-perspective').addEventListener('click', () => setColorMode('perspective'));
  $('mode-beneficiary').addEventListener('click', () => setColorMode('beneficiary'));

  $('tab-map').addEventListener('click', () => switchTab('map'));
  $('tab-feed').addEventListener('click', () => switchTab('feed'));
  $('feed-reload').addEventListener('click', () => loadFeed({reset: true}));
  feedPerspective.addEventListener('change', () => loadFeed({reset: true}));
  let searchTimer;
  $('feed-q').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadFeed({reset: true}), 350);
  });
  $('view-feed').addEventListener('scroll', () => {
    if (State.feedSavedOnly) return;
    const el = $('view-feed');
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) loadFeed();
  });

  wireScrubber();
  loadTimeline();

  refreshNotifications();
  const period = (State.config.poll_seconds || 90) * 1000;
  State.timer = setInterval(() => {
    if (document.hidden) return;      // no polling for a backgrounded tab
    if (isHistorical()) return;       // a pinned point in time must not be refreshed out from under
    refreshFrontline();
    refreshEvents();
    refreshRail();
    refreshNotifications();
  }, period);
  // Keep the scrubber's bounds current as new data arrives.
  setInterval(() => { if (!document.hidden && !isHistorical()) loadTimeline(); }, 10 * 60 * 1000);
}

boot();
