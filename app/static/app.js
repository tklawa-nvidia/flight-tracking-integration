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
  FlyToInterpolator,
  TextLayer,
} = deck;

// Loud sanity check — if deck.gl's UMD bundle ever drops one of these
// exports we'll know immediately rather than silently failing to render.
for (const [name, ref] of Object.entries({
  MapboxOverlay, ScatterplotLayer, IconLayer, ArcLayer, TripsLayer, PathLayer, TextLayer,
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
  },
  bbox: null,
  lastFetchedAt: 0,
  inFlight: false,
  ws: null,
  deckOverlay: null,
  map: null,
  animationStart: performance.now(),
  frameTs: 0,            // bumped every render frame; used as updateTrigger
  weather: {
    manifest: null,
    layerIds: [],        // MapLibre layer/source ids we own
    nextRefresh: 0,
  },
  // OpenSky's anonymous quota is small (~400 credits/day with bbox costs of
  // 1-4 each) so we let the user dial cadence and pause the live feed
  // entirely. Any 429 from the server pushes nextAllowedAt out so we don't
  // just bash the upstream until midnight UTC.
  live: {
    paused: false,
    intervalMs: 10_000,           // user-selectable cadence
    nextAllowedAt: 0,             // performance.now() floor; raised on 429
    backoffMs: 60_000,            // current 429 backoff window
    lastStatus: 'ok',             // 'ok' | 'rate-limit' | 'error' | 'paused' | 'hidden'
  },
};

const FLIGHT_STALE_MS = 60_000;        // drop flights we haven't seen in 60s
const MAX_HISTORY = 60;
// Don't extrapolate further than this since the last fix — an aircraft we
// haven't heard from in 30s is more likely lost than still on its old
// vector. Beyond this we just freeze the icon at the last fix.
const DEAD_RECKON_MAX_S = 30;
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

// ── Dead-reckoning ───────────────────────────────────────────────────────
// OpenSky returns lat/lon/heading/velocity at most every ~10s. To make the
// planes glide between updates we forward-project the last fix using the
// reported ground speed and heading. This is just kinematic guessing; we
// clamp the dt so that when a flight goes silent the icon doesn't sail off
// the map. The actual fix history (used for breadcrumb paths) is *not*
// touched — only the rendered position.

function interpolatedLonLat(f) {
  if (f == null || f.lat == null || f.lon == null) return [f?.lon ?? 0, f?.lat ?? 0];
  if (f.on_ground) return [f.lon, f.lat];
  const v = f.vel_mps;
  if (!v || v <= 0 || f.heading == null || f.fixTs == null) return [f.lon, f.lat];
  const dt = Math.min(DEAD_RECKON_MAX_S, (performance.now() - f.fixTs) / 1000);
  if (dt <= 0) return [f.lon, f.lat];
  const hdg = (f.heading * Math.PI) / 180;
  // Compass: 0° = N, 90° = E. North component cos, east component sin.
  const dN = v * Math.cos(hdg) * dt;   // metres north
  const dE = v * Math.sin(hdg) * dt;   // metres east
  const dLat = dN / 111320;
  const dLon = dE / (111320 * Math.cos((f.lat * Math.PI) / 180));
  return [f.lon + dLon, f.lat + dLat];
}

// ── Layer assembly ───────────────────────────────────────────────────────

function buildLayers() {
  const layers = [];
  const flights = Array.from(state.flights.values());
  const zoom = state.map ? state.map.getZoom() : 3;

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
        // deck.gl — it avoids the iconAtlas autopack step entirely.
        getIcon: () => PLANE_ICON_DEF,
        // Interpolated position based on heading/velocity for smooth motion
        // between OpenSky polls. updateTriggers.frameTs forces deck.gl to
        // recompute the accessor on every animation frame.
        getPosition: (f) => interpolatedLonLat(f),
        getSize: (f) => (f.id === state.selectedFlightId ? 32 : 20),
        // Plane SVG points UP (0° = north). OpenSky heading is degrees CW
        // from north. deck.gl getAngle is CCW degrees, so we negate.
        getAngle: (f) => -(f.heading || 0),
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
          getPosition: state.frameTs,
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
// Runs every frame the tab is foregrounded. Bumps state.frameTs which is
// fed into the IconLayer's updateTriggers so deck.gl re-runs the
// dead-reckoning accessor for each plane on every frame. We throttle the
// expensive layer rebuild to ~30 fps — that's smooth enough for aircraft
// (which move ~250 m/s) and halves the work on weaker GPUs / iGPUs.

let _lastRenderTs = 0;
const RENDER_INTERVAL_MS = 33;  // ~30 fps

function animate(nowTs) {
  if (nowTs - _lastRenderTs >= RENDER_INTERVAL_MS) {
    state.frameTs = nowTs;
    if (state.flights.size > 0 || state.layerVisibility.trails) {
      refreshLayers();
    }
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
  if (info.layer && info.layer.id === 'flights') {
    selectFlight(info.object);
  } else if (info.layer && (info.layer.id === 'airports' || info.layer.id === 'airport-labels')) {
    selectAirport(info.object);
  }
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
  // Respect pause + visibility — neither schedules a fetch.
  if (state.live.paused) {
    state.live.lastStatus = 'paused';
    updateRefreshHud();
    return;
  }
  if (document.hidden) {
    state.live.lastStatus = 'hidden';
    updateRefreshHud();
    // We *do* still re-schedule a tick when the tab returns; that's wired
    // up via the visibilitychange listener in wireLiveControls().
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

async function pump() {
  if (state.inFlight) {
    pendingPump = true;
    return;
  }
  if (state.live.paused || document.hidden) {
    schedulePump();
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
      elHudRefresh.textContent = `${sec}s`;
  }
}

function ingestFlights(flights) {
  const now = performance.now();
  const seen = new Set();
  for (const f of flights) {
    seen.add(f.id);
    // Stamp the fix time so the animation loop knows how far to project.
    f.fixTs = now;
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
        state.live.lastStatus = 'ok';
        toast('Live feed resumed');
        schedulePump({ force: true });
      }
    });
  }

  if (elCadence) {
    elCadence.value = String(state.live.intervalMs);
    elCadence.addEventListener('change', () => {
      const ms = parseInt(elCadence.value, 10);
      if (!Number.isFinite(ms)) return;
      state.live.intervalMs = ms;
      updateRefreshHud();
      // Re-schedule from now so the new cadence applies immediately.
      schedulePump();
    });
  }

  // Auto-pause when the tab is backgrounded — saves a *lot* of credits over
  // the course of a day. Resume immediately when it comes back.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (pumpTimer) clearTimeout(pumpTimer);
      state.live.lastStatus = 'hidden';
      updateRefreshHud();
    } else if (!state.live.paused) {
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
  const size = 256;

  const sat = (m.satellite?.infrared || []).slice(-1)[0];
  if (sat) {
    const id = 'wx-clouds';
    state.map.addSource(id, {
      type: 'raster',
      tiles: [`${m.host}${sat.path}/${size}/{z}/{x}/{y}/0/0_0.png`],
      tileSize: size,
      attribution: 'Clouds: RainViewer',
    });
    state.map.addLayer({
      id, type: 'raster', source: id,
      paint: { 'raster-opacity': 0.45 },
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
      attribution: 'Radar: RainViewer',
    });
    state.map.addLayer({
      id, type: 'raster', source: id,
      paint: { 'raster-opacity': 0.7 },
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
        refreshLayers();
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
  wireLayerToggles();
  wireLiveControls();
  connectWs();
  schedulePump({ force: true });
  requestAnimationFrame(animate);
  // Pre-warm the RainViewer manifest so the first weather toggle is instant.
  ensureWeatherManifest();
  setTimeout(weatherTick, WEATHER_REFRESH_MS);
})();
