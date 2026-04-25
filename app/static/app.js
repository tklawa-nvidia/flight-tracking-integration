// FlightOps frontend — MapLibre basemap + deck.gl overlays.
//
// The deck.gl global ships every layer/util we need on a single namespace
// (`deck`). We use `MapboxOverlay` (which is also the supported MapLibre
// integration) so we can keep MapLibre's native interaction handling and
// just add GPU layers on top.

const {
  MapboxOverlay,
  ScatterplotLayer,
  IconLayer,
  ArcLayer,
  TripsLayer,
  PathLayer,
  GeoJsonLayer,
  FlyToInterpolator,
  TextLayer,
} = deck;

// Loud sanity check — if deck.gl's UMD bundle ever drops one of these
// exports we'll know immediately rather than silently failing to render.
for (const [name, ref] of Object.entries({
  MapboxOverlay, ScatterplotLayer, IconLayer, ArcLayer, TripsLayer, PathLayer,
  GeoJsonLayer, TextLayer,
})) {
  if (!ref) console.error(`deck.gl export missing: ${name}`);
}

// ── State ────────────────────────────────────────────────────────────────

const state = {
  // id -> {id, lat, lon, heading, alt_m, vel_mps, callsign, fixTs, ...}
  // `fixTs` is performance.now() when we received this fix; used as the t0
  // for dead-reckoning animation between OpenSky polls.
  flights: new Map(),
  // id -> [{lat, lon, t}]   (capped per-id) — actual fixes only, no jitter
  flightHistory: new Map(),
  airportsInView: [],
  selectedFlightId: null,
  selectedAirport: null,
  arcs: [],
  arcsAirportCode: null,
  layerVisibility: {
    flights: true,
    airports: true,
    arcs: false,
    trails: false,
    paths: true,         // breadcrumb paths shown automatically when zoomed in
    weather: false,
    sua: false,          // FAA Special Use Airspace
    classes: false,      // Class B/C/D shells
    tfrs: true,          // Temporary Flight Restrictions (default on — they're rare and important)
  },
  airspace: {
    sua: null,           // GeoJSON FeatureCollection or null until fetched
    classes: null,
    tfrs: null,
    fetchedAt: { sua: 0, classes: 0, tfrs: 0 },
  },
  // pulsePhase advances 0..2π in the animate() loop and drives the
  // breathing alpha/width on prohibited/restricted/active airspace strokes.
  pulsePhase: 0,
  bbox: null,
  lastFetchedAt: 0,
  inFlight: false,
  ws: null,
  deckOverlay: null,
  map: null,
  animationStart: performance.now(),
  weather: {
    manifest: null,
    layerIds: [],        // MapLibre layer/source ids we own
    nextRefresh: 0,
  },
  // OpenSky's anonymous quota is small (~400 credits/day with bbox costs of
  // 1-4 each) so we let the user dial cadence, pause the live feed, or
  // switch to manual one-shot fetches. Any 429 from the server pushes
  // nextAllowedAt out so we don't just bash the upstream until midnight UTC.
  live: {
    paused: false,
    // 0 == manual mode (no auto-refresh; user clicks Snapshot)
    intervalMs: 30_000,
    nextAllowedAt: 0,             // performance.now() floor; raised on 429
    backoffMs: 60_000,             // current 429 backoff window
    lastStatus: 'ok',              // 'ok' | 'rate-limit' | 'error' | 'paused' | 'hidden' | 'manual'
  },
};

// Anonymous OpenSky budget — 400 credits/day. Each bbox call costs roughly
// 2 credits in the size range we typically use, so the practical anonymous
// budget is ~200 calls/day. Used to drive the budget hint text.
const OPENSKY_DAILY_CREDITS = 400;
const APPROX_CREDITS_PER_CALL = 2;

const FLIGHT_STALE_MS = 60_000;        // drop flights we haven't seen in 60s
const MAX_HISTORY = 60;
// Don't extrapolate further than this since the last fix — an aircraft we
// haven't heard from in 60s is more likely lost than still on its old
// vector. Beyond this we just freeze the icon at the last known position.
const DEAD_RECKON_MAX_S = 60;
// When a new fix lands, the icon is currently at its dead-reckoned visual
// position. The new fix tells us where the plane actually is. Snapping to
// the new position would jitter, so we keep a decaying offset that smoothly
// pulls the icon onto the new dead-reckoning line over this many seconds.
const CORRECTION_DECAY_S = 3;
// Show breadcrumb paths for every visible flight at this zoom or above.
const TRAIL_ZOOM_THRESHOLD = 6;
// RainViewer publishes a JSON manifest of available radar/satellite frames.
// No key required, ~5min refresh on their side.
const RAINVIEWER_MANIFEST = 'https://api.rainviewer.com/public/weather-maps.json';
const WEATHER_REFRESH_MS = 5 * 60 * 1000;

// ── DOM refs ─────────────────────────────────────────────────────────────

const elStatusPill = document.getElementById('status-pill');
const elStatusText = document.getElementById('status-text');
const elHudFlights = document.getElementById('hud-flights');
const elHudAirports = document.getElementById('hud-airports');
const elHudRefresh = document.getElementById('hud-refresh');
const elChatLog = document.getElementById('chat-log');
const elChatForm = document.getElementById('chat-form');
const elChatInput = document.getElementById('chat-text');
const elDrawer = document.getElementById('detail-drawer');
const elDrawerEyebrow = document.getElementById('drawer-eyebrow');
const elDrawerTitle = document.getElementById('drawer-title');
const elDrawerGrid = document.getElementById('drawer-grid');
const elDrawerClose = document.getElementById('drawer-close');
const elDrawerTrail = document.getElementById('drawer-trail');
const elDrawerArcs = document.getElementById('drawer-arcs');
const elToast = document.getElementById('toast');

// ── Plane icon (data URI so we don't need an asset file) ────────────────
// NOTE: explicit width/height on the <svg> root are *required* for deck.gl's
// icon manager to rasterize correctly. Without them some browsers compute a
// 0×0 layout and the icon appears invisible even though everything else looks
// healthy. We also use the per-feature getIcon callback rather than
// iconAtlas + iconMapping — that path is more reliable for data URIs.

const PLANE_ICON_SVG = encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
     <defs>
       <linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
         <stop offset="0" stop-color="#8ef5ff"/>
         <stop offset="1" stop-color="#1c8aab"/>
       </linearGradient>
     </defs>
     <path d="M32 4 L36 26 L60 36 L60 42 L36 36 L34 54 L42 58 L42 60 L32 58 L22 60 L22 58 L30 54 L28 36 L4 42 L4 36 L28 26 Z"
           fill="url(#g)" stroke="#001722" stroke-width="1.5" stroke-linejoin="round"/>
   </svg>`
);
const PLANE_ICON_URL = `data:image/svg+xml;utf8,${PLANE_ICON_SVG}`;

const PLANE_ICON_DEF = {
  url: PLANE_ICON_URL,
  width: 64,
  height: 64,
  anchorX: 32,
  anchorY: 32,
  mask: false,
};

// ── Map setup ────────────────────────────────────────────────────────────

const BASE_STYLE = 'https://tiles.openfreemap.org/styles/dark';

function initMap() {
  state.map = new maplibregl.Map({
    container: 'map',
    style: BASE_STYLE,
    center: [-95, 38],
    zoom: 3.4,
    pitch: 0,
    bearing: 0,
    attributionControl: false,
    antialias: true,
  });

  state.map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');
  state.map.touchZoomRotate.disableRotation();

  // Overlay mode (interleaved: false) puts deck.gl on its own canvas above
  // MapLibre. More tolerant of WebGL edge cases than interleaved mode and
  // we don't need to interleave with vector tile layers for this app.
  state.deckOverlay = new MapboxOverlay({
    interleaved: false,
    layers: buildLayers(),
    onClick: (info) => handleDeckClick(info),
  });
  state.map.addControl(state.deckOverlay);

  // Re-fetch flights whenever the user pans far enough.
  state.map.on('moveend', () => {
    schedulePump({ force: true });
  });
}

// ── Smooth motion (continuous dead-reckoning) ────────────────────────────
// OpenSky publishes every ~10–30s — a "snap to new fix" looks awful. The
// previous version of this file linearly interpolated between two
// predicted future positions, which produced an unnatural zig-zag whenever
// heading or speed changed between fixes.
//
// New approach (what real flight trackers do):
//   1. Each fix is an "anchor": (lat, lon, heading, ground_speed, ts).
//   2. Each render frame, the icon's drawn position = dead-reckoned
//      position from the latest anchor based on (now - ts). That makes
//      the icon move forward along its true heading at its true speed.
//   3. When a new fix arrives, the dead-reckoned guess is rarely exactly
//      where the new anchor says the plane is. Instead of teleporting,
//      we record a "correction offset" = (old_render_pos - new_anchor)
//      and decay it to zero over a few seconds. The icon glides onto the
//      new dead-reckoning line without ever leaving the heading.

function deadReckon(anchor, nowMs) {
  // Returns [lon, lat] = where this anchor's plane should be at `nowMs`,
  // assuming it has continued on its last known heading/speed.
  if (!anchor) return null;
  if (anchor.on_ground || !anchor.vel || anchor.vel <= 0 || anchor.heading == null) {
    return [anchor.lon, anchor.lat];
  }
  const dt = Math.min(DEAD_RECKON_MAX_S, Math.max(0, (nowMs - anchor.ts) / 1000));
  const hdg = (anchor.heading * Math.PI) / 180;
  const dN = anchor.vel * Math.cos(hdg) * dt;   // metres north
  const dE = anchor.vel * Math.sin(hdg) * dt;   // metres east
  const dLat = dN / 111_320;
  const dLon = dE / (111_320 * Math.cos((anchor.lat * Math.PI) / 180));
  return [anchor.lon + dLon, anchor.lat + dLat];
}

function renderPos(f, nowMs) {
  // Drawn position = dead-reckoning + decaying correction offset.
  if (!f.anchor) return [f.lon, f.lat];
  const dr = deadReckon(f.anchor, nowMs);
  if (!f.correction) return dr;
  const age = (nowMs - f.correctionTs) / 1000;
  const decay = Math.exp(-age / CORRECTION_DECAY_S);
  if (decay < 0.01) {
    f.correction = null;
    return dr;
  }
  return [dr[0] + f.correction[0] * decay, dr[1] + f.correction[1] * decay];
}

// ── Airspace styling ─────────────────────────────────────────────────────
// Color palette tuned for the dark basemap. We keep fills very low alpha so
// they read as "hint" tint and rely on bright strokes for legibility. Types
// in PULSING_TYPES get a breathing stroke alpha + width modulated by
// state.pulsePhase to draw the eye to active hazards.
//
// SUA TYPE_CODE values (per FAA AIS docs):
//   P = Prohibited   R = Restricted   W = Warning   A = Alert
//   M = MOA          N = National Security Area     C = CFA
const SUA_STYLE = {
  P: { fill: [255,  64,  76, 30], stroke: [255,  96, 110, 230], pulse: true },
  R: { fill: [255, 124,  64, 25], stroke: [255, 156,  88, 220], pulse: true },
  W: { fill: [255, 196,  64, 22], stroke: [255, 208, 110, 200], pulse: false },
  A: { fill: [120, 220, 140, 18], stroke: [160, 240, 180, 200], pulse: false },
  M: { fill: [240, 220,  88, 20], stroke: [255, 235, 110, 200], pulse: false },
  N: { fill: [200, 100, 255, 22], stroke: [220, 140, 255, 220], pulse: true },
  C: { fill: [180, 220, 255, 18], stroke: [200, 230, 255, 180], pulse: false },
  _: { fill: [180, 200, 220, 16], stroke: [200, 220, 240, 180], pulse: false },
};

// CLASS airspace shells. Class B = magenta, C = blue, D = teal, MODE-C = neon.
// FAA AIS encodes the actual class in `properties.CLASS` (B/C/D/E) and the
// shell type in `TYPE_CODE` ("CLASS" or "MODE-C"). We key off CLASS first so
// the colour matches the chart symbology pilots already know.
const CLASS_STYLE = {
  B: { fill: [233, 100, 200, 14], stroke: [255, 124, 220, 200] },
  C: { fill: [120, 170, 255, 14], stroke: [148, 188, 255, 200] },
  D: { fill: [120, 230, 220, 14], stroke: [150, 240, 230, 200] },
  E: { fill: [180, 200, 220, 10], stroke: [200, 220, 240, 140] },
  'MODE-C': { fill: [255, 180, 100,  8], stroke: [255, 210, 130, 140] },
  _: { fill: [180, 200, 220,  8], stroke: [200, 220, 240, 130] },
};

// TFRs always pulse — they're hazardous and dynamic by definition.
const TFR_STYLE = {
  fill: [255, 64, 96, 50],
  stroke: [255, 100, 130, 240],
  pulse: true,
};

function suaStyleFor(feature) {
  const code = (feature?.properties?.TYPE_CODE || '').trim().toUpperCase();
  return SUA_STYLE[code] || SUA_STYLE._;
}

function classStyleFor(feature) {
  const p = feature?.properties || {};
  const t = (p.TYPE_CODE || '').trim().toUpperCase();
  if (t === 'MODE-C') return CLASS_STYLE['MODE-C'];
  const c = (p.CLASS || '').trim().toUpperCase();
  return CLASS_STYLE[c] || CLASS_STYLE._;
}

// Pulse intensity in [0..1], driven by state.pulsePhase. We map it to two
// channels: a stroke-width multiplier (1.0..1.8) and an alpha multiplier
// (0.65..1.0). Both are floor-clamped so non-pulsing styles stay legible.
function pulseWidth(base) { return base * (1 + 0.4 * (1 + Math.sin(state.pulsePhase)) / 2); }
function pulseAlpha(a)    { return Math.round(a * (0.65 + 0.35 * (1 + Math.sin(state.pulsePhase)) / 2)); }

// ── Layer assembly ───────────────────────────────────────────────────────

function buildLayers() {
  const layers = [];
  const flights = Array.from(state.flights.values());
  const zoom = state.map ? state.map.getZoom() : 3;

  // ── Airspace (rendered first so it sits below flights/airports) ────────
  if (state.layerVisibility.classes && state.airspace.classes) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-classes',
        data: state.airspace.classes,
        stroked: true,
        filled: true,
        pickable: true,
        getFillColor: (f) => classStyleFor(f).fill,
        getLineColor: (f) => classStyleFor(f).stroke,
        getLineWidth: 1.5,
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 1,
      })
    );
  }

  if (state.layerVisibility.sua && state.airspace.sua) {
    // Two passes so the static fills don't churn while the strokes pulse:
    //   1) filled polygons, never re-evaluated per frame
    //   2) animated stroke on the same data, with pulsePhase as the only
    //      updateTrigger so deck.gl recomputes line attrs only.
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-sua-fill',
        data: state.airspace.sua,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: (f) => suaStyleFor(f).fill,
      })
    );
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-sua-stroke',
        data: state.airspace.sua,
        stroked: true,
        filled: false,
        pickable: false,
        getLineColor: (f) => {
          const s = suaStyleFor(f);
          if (!s.pulse) return s.stroke;
          const [r, g, b, a] = s.stroke;
          return [r, g, b, pulseAlpha(a)];
        },
        getLineWidth: (f) => (suaStyleFor(f).pulse ? pulseWidth(1.6) : 1.4),
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 1,
        updateTriggers: {
          getLineColor: state.pulsePhase,
          getLineWidth: state.pulsePhase,
        },
      })
    );
  }

  if (state.layerVisibility.tfrs && state.airspace.tfrs) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-tfrs-fill',
        data: state.airspace.tfrs,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: TFR_STYLE.fill,
      })
    );
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-tfrs-stroke',
        data: state.airspace.tfrs,
        stroked: true,
        filled: false,
        pickable: false,
        getLineColor: () => {
          const [r, g, b, a] = TFR_STYLE.stroke;
          return [r, g, b, pulseAlpha(a)];
        },
        getLineWidth: () => pulseWidth(2.0),
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 1.5,
        updateTriggers: {
          getLineColor: state.pulsePhase,
          getLineWidth: state.pulsePhase,
        },
      })
    );
  }

  if (state.layerVisibility.airports && state.airportsInView.length) {
    layers.push(
      new ScatterplotLayer({
        id: 'airports',
        data: state.airportsInView,
        getPosition: (a) => [a.lon, a.lat],
        getRadius: (a) => (a.type === 'large_airport' ? 4500 : 2200),
        radiusUnits: 'meters',
        radiusMinPixels: 3,
        radiusMaxPixels: 9,
        getFillColor: (a) =>
          state.selectedAirport && a.code === state.selectedAirport.code
            ? [255, 184, 107, 230]
            : [94, 226, 255, 180],
        getLineColor: [255, 255, 255, 80],
        lineWidthMinPixels: 0.5,
        stroked: true,
        pickable: true,
        updateTriggers: { getFillColor: state.selectedAirport?.code },
      })
    );
    layers.push(
      new TextLayer({
        id: 'airport-labels',
        data: state.airportsInView.filter((a) => a.type === 'large_airport'),
        getPosition: (a) => [a.lon, a.lat],
        getText: (a) => a.code,
        getSize: 12,
        getColor: [180, 215, 240, 200],
        getPixelOffset: [0, -14],
        sizeUnits: 'pixels',
        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
        fontWeight: 700,
        outlineWidth: 2,
        outlineColor: [0, 0, 0, 220],
        billboard: true,
      })
    );
  }

  if (state.layerVisibility.arcs && state.arcs.length) {
    layers.push(
      new ArcLayer({
        id: 'inbound-arcs',
        data: state.arcs,
        getSourcePosition: (d) => d.from,
        getTargetPosition: (d) => d.to,
        getSourceColor: [94, 226, 255, 180],
        getTargetColor: [255, 184, 107, 230],
        getWidth: 1.5,
        widthMinPixels: 1,
        greatCircle: true,
      })
    );
  }

  // Breadcrumb paths for every visible flight when the user zooms in.
  // Uses the actual fix history (no interpolation jitter), so the
  // polylines stay clean while the planes themselves glide smoothly.
  if (state.layerVisibility.paths && zoom >= TRAIL_ZOOM_THRESHOLD) {
    const pathData = [];
    for (const [id, history] of state.flightHistory) {
      if (history.length < 2) continue;
      pathData.push({
        id,
        path: history.map((p) => [p.lon, p.lat]),
        selected: id === state.selectedFlightId,
      });
    }
    if (pathData.length) {
      layers.push(
        new PathLayer({
          id: 'flight-paths',
          data: pathData,
          getPath: (d) => d.path,
          getColor: (d) =>
            d.selected ? [255, 184, 107, 220] : [180, 230, 255, 90],
          getWidth: (d) => (d.selected ? 2.5 : 1.5),
          widthMinPixels: 1,
          widthMaxPixels: 3,
          jointRounded: true,
          capRounded: true,
          updateTriggers: { getColor: state.selectedFlightId, getWidth: state.selectedFlightId },
        })
      );
    }
  }

  if (state.layerVisibility.trails && state.selectedFlightId) {
    const history = state.flightHistory.get(state.selectedFlightId) || [];
    if (history.length >= 2) {
      const tripPath = history.map((p) => [p.lon, p.lat]);
      const tripTimes = history.map((p) => p.t);
      const elapsed = (performance.now() - state.animationStart) / 1000;
      layers.push(
        new TripsLayer({
          id: 'flight-trail',
          data: [{ path: tripPath, timestamps: tripTimes }],
          getPath: (d) => d.path,
          getTimestamps: (d) => d.timestamps,
          getColor: [255, 184, 107, 240],
          widthMinPixels: 3,
          rounded: true,
          fadeTrail: true,
          trailLength: 60,
          currentTime: elapsed,
        })
      );
    }
  }

  if (state.layerVisibility.flights && flights.length) {
    layers.push(
      new IconLayer({
        id: 'flights',
        data: flights,
        // Per-feature getIcon is the most reliable path for data URIs in
        // deck.gl — it avoids the iconAtlas autopack step entirely. The
        // returned object identity is stable (PLANE_ICON_DEF is a const)
        // so deck.gl reuses the cached atlas across frame rebuilds.
        getIcon: () => PLANE_ICON_DEF,
        // Position is computed every frame from the per-flight anchor +
        // a decaying correction offset, so the icon traces the plane's
        // *real* heading at its *real* ground speed between fixes — no
        // bouncing along straight chords between predicted endpoints.
        getPosition: (f) => renderPos(f, performance.now()),
        getSize: (f) => (f.id === state.selectedFlightId ? 32 : 20),
        // Plane SVG points UP (0° = north). OpenSky heading is degrees CW
        // from north. deck.gl getAngle is CCW degrees, so we negate.
        getAngle: (f) => -(f.anchor?.heading ?? f.heading ?? 0),
        getColor: (f) =>
          f.id === state.selectedFlightId
            ? [255, 184, 107, 255]
            : f.on_ground
            ? [110, 130, 160, 200]
            : [255, 255, 255, 230],
        sizeUnits: 'pixels',
        sizeMinPixels: 12,
        sizeMaxPixels: 36,
        billboard: true,
        pickable: true,
        updateTriggers: {
          getSize: state.selectedFlightId,
          getColor: state.selectedFlightId,
        },
      })
    );
  }

  return layers;
}

function refreshLayers() {
  if (!state.deckOverlay) return;
  state.deckOverlay.setProps({ layers: buildLayers() });
}

// ── Animation loop ───────────────────────────────────────────────────────
// We drive plane motion ourselves (continuous dead-reckoning), so this
// loop has to refresh the layers at a steady cadence to keep the icons
// moving forward between fixes. ~20 fps is buttery and cheap: each
// rebuild is O(N) sin/cos for ~5k flights, well under 1 ms on the main
// thread, with similar GPU buffer churn.
//
// The TripsLayer used by the *selected* flight's trail also needs the
// loop tick to advance its currentTime, so the same rebuild covers both.

let _lastRenderTs = 0;
const RENDER_INTERVAL_MS = 50; // ~20 fps
const PULSE_PERIOD_MS = 2400;  // breathing cycle for restricted/active airspace

function animate(nowTs) {
  if (nowTs - _lastRenderTs >= RENDER_INTERVAL_MS) {
    state.pulsePhase = (nowTs / PULSE_PERIOD_MS) * Math.PI * 2;
    const lv = state.layerVisibility;
    const needRefresh =
      (lv.flights && state.flights.size > 0) ||
      (lv.trails && state.selectedFlightId) ||
      (lv.sua && state.airspace.sua) ||
      (lv.tfrs && state.airspace.tfrs);
    if (needRefresh) refreshLayers();
    _lastRenderTs = nowTs;
  }
  requestAnimationFrame(animate);
}

// ── Picking ──────────────────────────────────────────────────────────────

function handleDeckClick(info) {
  if (!info || !info.object) {
    closeDrawer();
    return;
  }
  const lid = info.layer?.id || '';
  if (lid === 'flights') {
    selectFlight(info.object);
  } else if (lid === 'airports' || lid === 'airport-labels') {
    selectAirport(info.object);
  } else if (lid.startsWith('airspace-')) {
    selectAirspace(info.object, lid);
  }
}

function selectAirspace(feature, layerId) {
  const p = feature?.properties || {};
  const isSua = layerId.startsWith('airspace-sua');
  const isClass = layerId === 'airspace-classes';
  const isTfr = layerId.startsWith('airspace-tfrs');
  const eyebrow = isTfr ? 'TFR' : isClass ? 'Class Airspace' : 'Special Use Airspace';
  const fmtAlt = (val, uom, code) =>
    val != null ? `${val} ${uom || 'FT'} ${code || ''}`.trim() : '—';
  const grid = [];
  if (isTfr) {
    grid.push(['Title', p.TITLE || '—']);
    grid.push(['NOTAM', p.NOTAM_KEY || '—']);
    grid.push(['State', p.STATE || '—']);
    if (p.LAST_MODIFICATION_DATETIME) grid.push(['Updated', p.LAST_MODIFICATION_DATETIME]);
  } else {
    grid.push(['Designator', p.NAME || p.IDENT || '—']);
    if (p.LOCAL_TYPE || p.TYPE_CODE) grid.push(['Type', p.LOCAL_TYPE || p.TYPE_CODE]);
    if (p.CLASS) grid.push(['Class', p.CLASS]);
    grid.push(['Ceiling', fmtAlt(p.UPPER_VAL, p.UPPER_UOM, p.UPPER_CODE)]);
    grid.push(['Floor', fmtAlt(p.LOWER_VAL, p.LOWER_UOM, p.LOWER_CODE)]);
    if (p.CITY || p.STATE) grid.push(['Location', [p.CITY, p.STATE].filter(Boolean).join(', ')]);
    if (p.TIMESOFUSE) grid.push(['Times of use', p.TIMESOFUSE]);
    if (p.CONT_AGENT) grid.push(['Authority', p.CONT_AGENT]);
  }
  openDrawer({
    eyebrow,
    title: p.NAME || p.TITLE || p.IDENT || 'Airspace',
    grid: grid.length ? grid : [['Properties', JSON.stringify(p).slice(0, 200)]],
  });
}

function selectFlight(f) {
  state.selectedFlightId = f.id;
  state.selectedAirport = null;
  openDrawer({
    eyebrow: 'Aircraft',
    title: f.callsign || f.id.toUpperCase(),
    grid: [
      ['ICAO24', f.id.toUpperCase()],
      ['Country', f.country || '—'],
      ['Altitude', formatAltitude(f.alt_m)],
      ['Speed', formatSpeed(f.vel_mps)],
      ['Heading', f.heading != null ? `${Math.round(f.heading)}°` : '—'],
      ['V/Rate', formatVrate(f.vrate_mps)],
    ],
  });
  refreshLayers();
}

function selectAirport(a) {
  state.selectedAirport = a;
  state.selectedFlightId = null;
  openDrawer({
    eyebrow: 'Airport',
    title: `${a.code} · ${a.icao || ''}`.trim(),
    grid: [
      ['Name', a.name],
      ['City', a.city || '—'],
      ['Country', a.country || '—'],
      ['Elevation', a.elevation_ft != null ? `${a.elevation_ft} ft` : '—'],
      ['Type', (a.type || '').replace('_', ' ')],
      ['Position', `${a.lat.toFixed(3)}, ${a.lon.toFixed(3)}`],
    ],
  });
  refreshLayers();
}

function openDrawer({ eyebrow, title, grid }) {
  elDrawerEyebrow.textContent = eyebrow;
  elDrawerTitle.textContent = title;
  elDrawerGrid.innerHTML = grid
    .map(([k, v]) => `<div class="drawer-cell"><div class="k">${k}</div><div class="v">${v}</div></div>`)
    .join('');
  elDrawer.classList.remove('hidden');
  elDrawer.setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  elDrawer.classList.add('hidden');
  elDrawer.setAttribute('aria-hidden', 'true');
  state.selectedFlightId = null;
  state.selectedAirport = null;
  refreshLayers();
}

elDrawerClose.addEventListener('click', closeDrawer);

elDrawerTrail.addEventListener('click', () => {
  if (!state.selectedFlightId) return;
  document.getElementById('layer-trails').checked = true;
  state.layerVisibility.trails = true;
  refreshLayers();
  toast('Trail enabled — moves will animate as new positions arrive');
});

elDrawerArcs.addEventListener('click', async () => {
  // Find the nearest airport in the current bbox to the selected flight.
  const f = state.flights.get(state.selectedFlightId);
  if (!f || !state.airportsInView.length) return;
  let closest = null;
  let dmin = Infinity;
  for (const a of state.airportsInView) {
    const d = haversineKm(f.lat, f.lon, a.lat, a.lon);
    if (d < dmin) { dmin = d; closest = a; }
  }
  if (!closest) return;
  toast(`Drawing arcs to ${closest.code}…`);
  await fetch('/api/map/arcs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ airport: closest.code, radius_km: Math.max(40, Math.ceil(dmin * 2)) }),
  });
});

// ── Helpers ──────────────────────────────────────────────────────────────

function formatAltitude(m) {
  if (m == null) return '—';
  const ft = m * 3.28084;
  return `${Math.round(ft).toLocaleString()} ft`;
}

function formatSpeed(mps) {
  if (mps == null) return '—';
  const knots = mps * 1.94384;
  return `${Math.round(knots)} kt`;
}

function formatVrate(mps) {
  if (mps == null) return '—';
  const fpm = Math.round(mps * 196.85);
  if (Math.abs(fpm) < 100) return 'level';
  return `${fpm > 0 ? '+' : ''}${fpm} fpm`;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function currentBbox() {
  const b = state.map.getBounds();
  // Returned as west,south,east,north for the API.
  return `${b.getWest().toFixed(3)},${b.getSouth().toFixed(3)},${b.getEast().toFixed(3)},${b.getNorth().toFixed(3)}`;
}

// ── Data pump ────────────────────────────────────────────────────────────

let pumpTimer = null;
let pendingPump = false;

function schedulePump({ force = false } = {}) {
  if (pumpTimer) clearTimeout(pumpTimer);
  // Respect pause / manual / visibility — none of these schedule a fetch.
  if (state.live.paused) {
    state.live.lastStatus = 'paused';
    updateRefreshHud();
    return;
  }
  if (state.live.intervalMs === 0) {
    // Manual mode: no auto refresh. The user fires Snapshot to refetch.
    state.live.lastStatus = 'manual';
    updateRefreshHud();
    return;
  }
  if (document.hidden) {
    state.live.lastStatus = 'hidden';
    updateRefreshHud();
    return;
  }
  const now = performance.now();
  let delay;
  if (force) {
    delay = Math.max(250, state.live.nextAllowedAt - now);
  } else {
    delay = Math.max(state.live.intervalMs, state.live.nextAllowedAt - now);
  }
  pumpTimer = setTimeout(pump, delay);
  updateRefreshHud(delay);
}

async function pump({ snapshot = false } = {}) {
  if (state.inFlight) {
    pendingPump = true;
    return;
  }
  // A manual snapshot bypasses pause / hidden-tab / manual-mode gates but
  // still respects the 429 backoff floor.
  if (!snapshot && (state.live.paused || document.hidden)) {
    schedulePump();
    return;
  }
  if (snapshot && state.live.nextAllowedAt > performance.now()) {
    const wait = Math.round((state.live.nextAllowedAt - performance.now()) / 1000);
    toast(`Backoff active — try again in ${wait}s`);
    return;
  }
  state.inFlight = true;
  try {
    setStatus('live', 'streaming');
    const bboxStr = currentBbox();
    const [flightsRes, airportsRes] = await Promise.all([
      fetchOrThrow(`/api/flights?bbox=${bboxStr}`),
      fetchOrThrow(`/api/airports?bbox=${bboxStr}&limit=400`),
    ]);

    ingestFlights(flightsRes.flights || []);
    state.airportsInView = airportsRes.airports || [];

    elHudFlights.textContent = state.flights.size.toLocaleString();
    elHudAirports.textContent = state.airportsInView.length.toLocaleString();
    state.lastFetchedAt = Date.now();
    state.live.lastStatus = 'ok';
    state.live.backoffMs = 60_000;  // recovered → reset backoff window

    refreshLayers();
  } catch (err) {
    if (err && err.status === 429) {
      // OpenSky rate-limited us. Push nextAllowedAt out by an exponentially
      // growing window (capped) so we don't just bash the upstream every
      // cadence tick. This reset whenever a successful fetch lands.
      const wait = Math.min(state.live.backoffMs, 5 * 60_000);
      state.live.nextAllowedAt = performance.now() + wait;
      state.live.backoffMs = Math.min(state.live.backoffMs * 2, 5 * 60_000);
      state.live.lastStatus = 'rate-limit';
      setStatus('error', 'rate limited');
      toast(`OpenSky rate limit — backing off ${Math.round(wait / 1000)}s`, 4000);
    } else {
      console.error('pump failed', err);
      state.live.lastStatus = 'error';
      setStatus('error', 'offline');
      toast('Live data fetch failed — retrying…');
    }
  } finally {
    state.inFlight = false;
    // In snapshot/manual mode we don't reschedule a follow-up; just leave
    // the planes frozen at their last fix until the user asks again.
    if (snapshot || state.live.intervalMs === 0) {
      state.live.lastStatus = state.live.lastStatus === 'rate-limit' ? 'rate-limit' : 'manual';
      updateRefreshHud();
      return;
    }
    if (pendingPump) {
      pendingPump = false;
      schedulePump({ force: true });
    } else {
      schedulePump();
    }
  }
}

// Tiny fetch wrapper that turns non-2xx into a real Error with a `.status`
// so the pump's catch can distinguish a 429 from a generic network error.
async function fetchOrThrow(url) {
  const r = await fetch(url);
  if (!r.ok) {
    const err = new Error(`HTTP ${r.status}`);
    err.status = r.status;
    err.body = await r.text().catch(() => '');
    throw err;
  }
  return r.json();
}

function updateRefreshHud(delayMs) {
  // Show cadence + a hint about the current state. Driven from the pump so
  // it stays accurate when the user changes the cadence dropdown or pauses.
  const sec = Math.round(state.live.intervalMs / 1000);
  switch (state.live.lastStatus) {
    case 'paused':
      elHudRefresh.textContent = 'paused';
      break;
    case 'manual':
      elHudRefresh.textContent = 'manual';
      break;
    case 'hidden':
      elHudRefresh.textContent = 'tab hidden';
      break;
    case 'rate-limit': {
      const remaining = Math.max(0, Math.round((state.live.nextAllowedAt - performance.now()) / 1000));
      elHudRefresh.textContent = `429 · ${remaining}s`;
      break;
    }
    case 'error':
      elHudRefresh.textContent = 'retrying';
      break;
    default:
      elHudRefresh.textContent = sec >= 60 ? `${Math.round(sec / 60)} min` : `${sec}s`;
  }
}

// Update the credit-budget hint shown below the cadence selector.
// At intervalMs cadence, calls/hour ≈ 3600/(intervalMs/1000).
// Anonymous budget is OPENSKY_DAILY_CREDITS / APPROX_CREDITS_PER_CALL calls
// per day. We surface the practical "hours of continuous viewing" number
// so the user can pick a cadence that won't burn the daily quota.
function updateBudgetHint() {
  const elBudget = document.getElementById('live-budget');
  const elHours = document.getElementById('live-budget-hours');
  if (!elBudget || !elHours) return;
  if (state.live.intervalMs === 0) {
    elBudget.textContent = '0';
    elHours.textContent = 'unlimited';
    return;
  }
  const callsPerHour = 3600 / (state.live.intervalMs / 1000);
  const creditsPerHour = Math.round(callsPerHour * APPROX_CREDITS_PER_CALL);
  const dailyCalls = OPENSKY_DAILY_CREDITS / APPROX_CREDITS_PER_CALL;
  const hours = dailyCalls / callsPerHour;
  elBudget.textContent = `~${creditsPerHour}`;
  elHours.textContent = hours >= 24
    ? 'all day'
    : hours >= 1
    ? `~${hours.toFixed(hours < 5 ? 1 : 0)} hr`
    : `~${Math.round(hours * 60)} min`;
}

function ingestFlights(flights) {
  const now = performance.now();
  const seen = new Set();
  for (const f of flights) {
    seen.add(f.id);
    f.fixTs = now;

    // New anchor from the freshly received state vector.
    const newAnchor = {
      lon: f.lon, lat: f.lat,
      heading: f.heading, vel: f.vel_mps,
      ts: now, on_ground: f.on_ground,
    };

    // If we already had a fix for this aircraft, the icon is currently
    // dead-reckoned along the *old* anchor. Capture that visual position
    // and turn the delta against the new anchor into a decaying offset
    // — the icon will glide onto the new heading line over a few seconds
    // instead of teleporting.
    const prev = state.flights.get(f.id);
    let correction = null;
    if (prev && prev.anchor) {
      const oldRender = renderPos(prev, now);
      const dLon = oldRender[0] - newAnchor.lon;
      const dLat = oldRender[1] - newAnchor.lat;
      // Sanity guard: if the gap is huge (>~5 degrees, e.g. icao24 reuse
      // or a fix from the other side of the world), don't try to glide;
      // just snap.
      if (dLon * dLon + dLat * dLat < 25) {
        correction = [dLon, dLat];
      }
    }
    f.anchor = newAnchor;
    f.correction = correction;
    f.correctionTs = now;

    state.flights.set(f.id, f);
    let history = state.flightHistory.get(f.id);
    if (!history) {
      history = [];
      state.flightHistory.set(f.id, history);
    }
    history.push({ lat: f.lat, lon: f.lon, t: (now - state.animationStart) / 1000 });
    if (history.length > MAX_HISTORY) history.shift();
  }
  // Drop stale flights so the icon layer doesn't accumulate ghosts.
  const staleCutoff = Date.now() / 1000 - FLIGHT_STALE_MS / 1000;
  for (const [id, f] of state.flights) {
    if (!seen.has(id) && f.last_seen && f.last_seen < staleCutoff) {
      state.flights.delete(id);
      state.flightHistory.delete(id);
    }
  }
}

// ── Status pill ──────────────────────────────────────────────────────────

function setStatus(kind, text) {
  elStatusPill.classList.remove('is-stale', 'is-error');
  if (kind === 'stale') elStatusPill.classList.add('is-stale');
  if (kind === 'error') elStatusPill.classList.add('is-error');
  elStatusText.textContent = text;
}

function toast(text, ms = 2500) {
  elToast.textContent = text;
  elToast.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => elToast.classList.remove('show'), ms);
}

// ── Live data controls ───────────────────────────────────────────────────

function wireLiveControls() {
  const elPause = document.getElementById('live-pause');
  const elSnapshot = document.getElementById('live-snapshot');
  const elCadence = document.getElementById('live-cadence');

  if (elPause) {
    elPause.addEventListener('click', () => {
      state.live.paused = !state.live.paused;
      elPause.classList.toggle('is-active', state.live.paused);
      elPause.textContent = state.live.paused ? 'Resume' : 'Pause';
      if (state.live.paused) {
        if (pumpTimer) clearTimeout(pumpTimer);
        state.live.lastStatus = 'paused';
        updateRefreshHud();
        toast('Live feed paused — planes are frozen at last fix');
      } else {
        state.live.lastStatus = state.live.intervalMs === 0 ? 'manual' : 'ok';
        toast('Live feed resumed');
        if (state.live.intervalMs > 0) schedulePump({ force: true });
      }
    });
  }

  // Snapshot: a single one-shot fetch. Useful in manual mode, or when you
  // just want fresh data without committing to a recurring poll. Bypasses
  // the pause / hidden-tab gates but still respects the 429 backoff.
  if (elSnapshot) {
    elSnapshot.addEventListener('click', async () => {
      elSnapshot.disabled = true;
      elSnapshot.classList.add('is-active');
      toast('Fetching one snapshot…', 1500);
      try {
        await pump({ snapshot: true });
      } finally {
        elSnapshot.disabled = false;
        elSnapshot.classList.remove('is-active');
      }
    });
  }

  if (elCadence) {
    elCadence.value = String(state.live.intervalMs);
    elCadence.addEventListener('change', () => {
      const ms = parseInt(elCadence.value, 10);
      if (!Number.isFinite(ms)) return;
      state.live.intervalMs = ms;
      updateBudgetHint();
      if (ms === 0) {
        if (pumpTimer) clearTimeout(pumpTimer);
        state.live.lastStatus = 'manual';
        updateRefreshHud();
        toast('Manual mode — use Snapshot to fetch when you need it');
      } else {
        // Re-schedule from now so the new cadence applies immediately.
        state.live.lastStatus = 'ok';
        schedulePump();
      }
    });
  }

  updateBudgetHint();

  // Auto-pause when the tab is backgrounded — saves a *lot* of credits over
  // the course of a day. Resume immediately when it comes back.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (pumpTimer) clearTimeout(pumpTimer);
      state.live.lastStatus = 'hidden';
      updateRefreshHud();
    } else if (!state.live.paused && state.live.intervalMs > 0) {
      schedulePump({ force: true });
    }
  });
}

// ── Layer toggles ────────────────────────────────────────────────────────

function wireLayerToggles() {
  document.getElementById('layer-flights').addEventListener('change', (e) => {
    state.layerVisibility.flights = e.target.checked;
    refreshLayers();
  });
  document.getElementById('layer-airports').addEventListener('change', (e) => {
    state.layerVisibility.airports = e.target.checked;
    refreshLayers();
  });
  document.getElementById('layer-arcs').addEventListener('change', (e) => {
    state.layerVisibility.arcs = e.target.checked;
    refreshLayers();
  });
  document.getElementById('layer-trails').addEventListener('change', (e) => {
    state.layerVisibility.trails = e.target.checked;
    refreshLayers();
  });
  const elPaths = document.getElementById('layer-paths');
  if (elPaths) {
    elPaths.addEventListener('change', (e) => {
      state.layerVisibility.paths = e.target.checked;
      refreshLayers();
    });
  }
  const elWeather = document.getElementById('layer-weather');
  if (elWeather) {
    elWeather.addEventListener('change', async (e) => {
      state.layerVisibility.weather = e.target.checked;
      if (e.target.checked) {
        await ensureWeatherManifest();
        applyWeather();
        toast('Weather: precipitation radar + cloud IR');
      } else {
        clearWeather();
      }
    });
  }
  // Airspace toggles
  for (const [key, label] of [
    ['sua', 'Restricted airspace'],
    ['classes', 'Class B/C/D'],
    ['tfrs', 'Active TFRs'],
  ]) {
    const el = document.getElementById(`layer-${key}`);
    if (!el) continue;
    el.addEventListener('change', async (e) => {
      state.layerVisibility[key] = e.target.checked;
      if (e.target.checked) {
        try {
          await ensureAirspace(key);
          const fc = state.airspace[key];
          const n = fc?.features?.length ?? 0;
          toast(`${label}: ${n} feature${n === 1 ? '' : 's'} loaded`);
        } catch (err) {
          console.warn(`failed to load airspace ${key}`, err);
          toast(`${label}: failed to load`);
          e.target.checked = false;
          state.layerVisibility[key] = false;
        }
      }
      refreshLayers();
    });
  }
}

// ── Tab switcher ─────────────────────────────────────────────────────────
function wireTabs() {
  const buttons = document.querySelectorAll('.tab-btn');
  const panels = {
    layers: document.getElementById('tab-layers'),
    nemoclaw: document.getElementById('tab-nemoclaw'),
    airspace: document.getElementById('tab-airspace'),
  };
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      buttons.forEach((b) => {
        const active = b.dataset.tab === target;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      for (const [k, panel] of Object.entries(panels)) {
        if (!panel) continue;
        const active = k === target;
        panel.classList.toggle('active', active);
        if (active) panel.removeAttribute('hidden');
        else panel.setAttribute('hidden', '');
      }
      // Chat needs its log scrolled to bottom every time it becomes visible
      // (otherwise long answers received while the tab was hidden look cut off).
      if (target === 'nemoclaw') {
        const log = document.getElementById('chat-log');
        if (log) log.scrollTop = log.scrollHeight;
      }
    });
  });
}

// ── Airspace (FAA AIS) ───────────────────────────────────────────────────
// We pull GeoJSON via the local /api/airspace/* proxies which cache the
// upstream FAA endpoints. SUA + Class refresh hourly client-side; TFRs are
// dynamic so we re-fetch every five minutes.
const AIRSPACE_TTL_MS = { sua: 3600 * 1000, classes: 3600 * 1000, tfrs: 5 * 60 * 1000 };

async function ensureAirspace(name) {
  const now = Date.now();
  const fetched = state.airspace.fetchedAt[name] || 0;
  if (state.airspace[name] && (now - fetched) < AIRSPACE_TTL_MS[name]) return;
  const r = await fetch(`/api/airspace/${name}`);
  if (!r.ok) throw new Error(`airspace ${name}: HTTP ${r.status}`);
  state.airspace[name] = await r.json();
  state.airspace.fetchedAt[name] = now;
}

// ── Weather (RainViewer) ─────────────────────────────────────────────────
// Two raster overlays from the same provider:
//   - infrared satellite  (cloud cover, latest frame)
//   - precipitation radar (latest past frame; nowcast frames are predicted)
// Both are free, anonymous, global, and CORS-enabled.

async function ensureWeatherManifest() {
  const now = Date.now();
  if (state.weather.manifest && now < state.weather.nextRefresh) return;
  try {
    const r = await fetch(RAINVIEWER_MANIFEST, { cache: 'no-store' });
    if (!r.ok) throw new Error(`status ${r.status}`);
    state.weather.manifest = await r.json();
    state.weather.nextRefresh = now + WEATHER_REFRESH_MS;
  } catch (err) {
    console.warn('weather manifest fetch failed', err);
    toast('Weather feed unreachable');
  }
}

function applyWeather() {
  const m = state.weather.manifest;
  if (!m || !state.map) return;
  clearWeather();
  // RainViewer tile URL grammar:
  //   {host}{path}/{size}/{z}/{x}/{y}/{color}/{options}.png
  // For radar:     color 4 (universal blue), options 1_1 (smooth + snow)
  // For satellite: color 0 (b/w IR), options 0_0
  //
  // Native RainViewer tile coverage:
  //   - radar:     up to z=12
  //   - satellite: up to z=8
  // We mark `maxzoom` on the source so MapLibre overzooms (resamples the
  // last available level) instead of hammering 404s when the user zooms
  // past those native levels — this is what keeps the overlay visible at
  // city-level zoom. We then taper opacity with zoom so the inevitable
  // blur is subtle rather than washed-out.
  const size = 256;

  const sat = (m.satellite?.infrared || []).slice(-1)[0];
  if (sat) {
    const id = 'wx-clouds';
    state.map.addSource(id, {
      type: 'raster',
      tiles: [`${m.host}${sat.path}/${size}/{z}/{x}/{y}/0/0_0.png`],
      tileSize: size,
      maxzoom: 8,
      attribution: 'Clouds: RainViewer',
    });
    state.map.addLayer({
      id, type: 'raster', source: id,
      paint: {
        'raster-opacity': [
          'interpolate', ['linear'], ['zoom'],
          0,  0.30,
          5,  0.28,
          8,  0.22,
          12, 0.16,
          16, 0.12,
        ],
        'raster-resampling': 'linear',
        'raster-fade-duration': 200,
      },
    });
    state.weather.layerIds.push(id);
  }

  const radar = (m.radar?.past || []).slice(-1)[0];
  if (radar) {
    const id = 'wx-radar';
    state.map.addSource(id, {
      type: 'raster',
      tiles: [`${m.host}${radar.path}/${size}/{z}/{x}/{y}/4/1_1.png`],
      tileSize: size,
      maxzoom: 12,
      attribution: 'Radar: RainViewer',
    });
    state.map.addLayer({
      id, type: 'raster', source: id,
      paint: {
        'raster-opacity': [
          'interpolate', ['linear'], ['zoom'],
          0,  0.45,
          5,  0.42,
          9,  0.38,
          12, 0.32,
          15, 0.26,
          18, 0.22,
        ],
        'raster-resampling': 'linear',
        'raster-fade-duration': 200,
      },
    });
    state.weather.layerIds.push(id);
  }
}

function clearWeather() {
  if (!state.map) return;
  for (const id of state.weather.layerIds) {
    if (state.map.getLayer(id)) state.map.removeLayer(id);
    if (state.map.getSource(id)) state.map.removeSource(id);
  }
  state.weather.layerIds = [];
}

// Periodically swap to the newest RainViewer frame so the radar doesn't go
// stale during long sessions. Only kicks in when weather is on.
async function weatherTick() {
  if (state.layerVisibility.weather) {
    state.weather.nextRefresh = 0;       // force a refresh
    await ensureWeatherManifest();
    applyWeather();
  }
  setTimeout(weatherTick, WEATHER_REFRESH_MS);
}

// ── WebSocket bus (commands from the backend / external skill) ──────────

function connectWs() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/map`);
  state.ws = ws;
  ws.onopen = () => setStatus('live', 'streaming');
  ws.onclose = () => {
    setStatus('stale', 'reconnecting');
    setTimeout(connectWs, 2000);
  };
  ws.onerror = () => setStatus('error', 'ws error');
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      handleBusMessage(msg);
    } catch (err) {
      console.error('bad ws message', err);
    }
  };
}

function handleBusMessage(msg) {
  switch (msg.type) {
    case 'goto': {
      flyTo(msg.lon, msg.lat, msg.zoom || 9);
      toast(`Flying to ${msg.label || `${msg.lat}, ${msg.lon}`}`);
      // After the camera lands, refresh the data window for the new bbox.
      setTimeout(() => schedulePump({ force: true }), 1100);
      break;
    }
    case 'arcs': {
      state.arcs = msg.arcs || [];
      state.arcsAirportCode = msg.airport;
      state.layerVisibility.arcs = true;
      document.getElementById('layer-arcs').checked = true;
      refreshLayers();
      toast(`Drew ${state.arcs.length} arcs into ${msg.airport}`);
      break;
    }
    case 'layer': {
      if (msg.layer in state.layerVisibility) {
        state.layerVisibility[msg.layer] = !!msg.visible;
        const cb = document.getElementById(`layer-${msg.layer}`);
        if (cb) cb.checked = !!msg.visible;
        // Airspace layers need their GeoJSON loaded before they can render.
        if (msg.visible && ['sua', 'classes', 'tfrs'].includes(msg.layer)) {
          ensureAirspace(msg.layer)
            .then(refreshLayers)
            .catch((err) => console.warn(`agent-toggled airspace ${msg.layer} failed`, err));
        } else {
          refreshLayers();
        }
      }
      break;
    }
    case 'highlight': {
      const target = (msg.flight || '').toLowerCase();
      // Try ICAO24 hex match first, then callsign.
      let matched = state.flights.get(target);
      if (!matched) {
        for (const f of state.flights.values()) {
          if ((f.callsign || '').trim().toLowerCase() === target) {
            matched = f;
            break;
          }
        }
      }
      if (matched) {
        state.selectedFlightId = matched.id;
        state.layerVisibility.trails = true;
        document.getElementById('layer-trails').checked = true;
        flyTo(matched.lon, matched.lat, 9);
        selectFlight(matched);
      } else {
        toast(`No live contact for ${msg.flight}`);
      }
      break;
    }
    default:
      console.debug('unhandled bus msg', msg);
  }
}

function flyTo(lon, lat, zoom = 9) {
  if (!state.map) return;
  state.map.flyTo({
    center: [lon, lat],
    zoom,
    speed: 1.4,
    curve: 1.42,
    essential: true,
  });
}

// ── Chat ────────────────────────────────────────────────────────────────
// Chat is a thin wrapper over `openclaw agent --json`. OpenClaw owns the
// conversation state on its side (keyed by session_id), so we only post the
// latest user message and the session id we got back from the previous turn.

let openclawSessionId = null;

function appendMessage(role, content, { thinking = false } = {}) {
  const wrap = document.createElement('div');
  wrap.className = `msg msg-${role}${thinking ? ' msg-thinking' : ''}`;
  const body = document.createElement('div');
  body.className = 'msg-content';
  body.textContent = content;
  wrap.appendChild(body);
  elChatLog.appendChild(wrap);
  elChatLog.scrollTop = elChatLog.scrollHeight;
  return wrap;
}

document.querySelectorAll('.chip').forEach((btn) => {
  btn.addEventListener('click', () => {
    elChatInput.value = btn.dataset.prompt;
    elChatForm.requestSubmit();
  });
});

elChatForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = elChatInput.value.trim();
  if (!text) return;
  elChatInput.value = '';
  appendMessage('user', text);

  const thinking = appendMessage('bot', '', { thinking: true });
  thinking.querySelector('.msg-content').innerHTML =
    'thinking <span class="thinking-dots"><span></span><span></span><span></span></span>';

  // OpenClaw turns can take a while when the agent decides to use tools,
  // so disable the input until the reply lands.
  elChatInput.disabled = true;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: openclawSessionId }),
    });
    if (!res.ok) {
      const errBody = await res.text();
      thinking.remove();
      appendMessage('bot', `Agent call failed (${res.status}). ${errBody.slice(0, 320)}`);
      return;
    }
    const data = await res.json();
    thinking.remove();
    if (data.session_id) openclawSessionId = data.session_id;
    appendMessage('bot', data.reply || '(no reply)');
  } catch (err) {
    thinking.remove();
    appendMessage('bot', `Network error talking to OpenClaw: ${err.message}`);
  } finally {
    elChatInput.disabled = false;
    elChatInput.focus();
  }
});

// ── Boot ─────────────────────────────────────────────────────────────────

(async function boot() {
  initMap();
  await new Promise((resolve) => state.map.once('load', resolve));
  wireTabs();
  wireLayerToggles();
  wireLiveControls();
  connectWs();
  schedulePump({ force: true });
  requestAnimationFrame(animate);
  // Pre-warm the RainViewer manifest so the first weather toggle is instant.
  ensureWeatherManifest();
  setTimeout(weatherTick, WEATHER_REFRESH_MS);
  // TFRs default to ON (rare + important). SUA + Class are off until the
  // user toggles them — they're chatty and visually heavy at country zoom.
  if (state.layerVisibility.tfrs) {
    ensureAirspace('tfrs')
      .then(() => {
        const n = state.airspace.tfrs?.features?.length ?? 0;
        if (n > 0) toast(`${n} active TFR${n === 1 ? '' : 's'} loaded`);
        refreshLayers();
      })
      .catch((err) => console.warn('TFR preload failed', err));
  }
  // Refresh TFRs every 5 min while the page is open.
  setInterval(() => {
    if (state.layerVisibility.tfrs) {
      ensureAirspace('tfrs').then(refreshLayers).catch(() => {});
    }
  }, 5 * 60 * 1000);
})();
