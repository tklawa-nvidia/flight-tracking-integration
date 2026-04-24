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
  FlyToInterpolator,
  TextLayer,
} = deck;

// Loud sanity check — if deck.gl's UMD bundle ever drops one of these
// exports we'll know immediately rather than silently failing to render.
for (const [name, ref] of Object.entries({
  MapboxOverlay, ScatterplotLayer, IconLayer, ArcLayer, TripsLayer, TextLayer,
})) {
  if (!ref) console.error(`deck.gl export missing: ${name}`);
}

// ── State ────────────────────────────────────────────────────────────────

const state = {
  flights: new Map(),         // id -> {id, lat, lon, heading, alt_m, vel_mps, callsign, ...}
  flightHistory: new Map(),   // id -> [{lat, lon, t}]   (capped per-id)
  airportsInView: [],         // last airports response
  selectedFlightId: null,
  selectedAirport: null,
  arcs: [],                   // {from:[lon,lat], to:[lon,lat]}
  arcsAirportCode: null,
  layerVisibility: {
    flights: true,
    airports: true,
    arcs: false,
    trails: false,
  },
  bbox: null,                 // last fetched bbox key
  lastFetchedAt: 0,
  inFlight: false,
  ws: null,
  deckOverlay: null,
  map: null,
  animationStart: performance.now(),
};

const FETCH_INTERVAL_MS = 9_000;       // keep us under OpenSky's anonymous 10s rate
const TRAIL_WINDOW_MS = 90_000;        // 90s trails
const FLIGHT_STALE_MS = 60_000;        // drop flights we haven't seen in 60s
const MAX_HISTORY = 60;

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

// ── Layer assembly ───────────────────────────────────────────────────────

function buildLayers() {
  const layers = [];
  const now = performance.now();
  const flights = Array.from(state.flights.values());

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
        getPosition: (f) => [f.lon, f.lat],
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

// ── Animation loop (just to keep the trail current time advancing) ───────

function animate() {
  if (state.layerVisibility.trails && state.selectedFlightId) {
    refreshLayers();
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
  if (force) {
    pumpTimer = setTimeout(pump, 250);
  } else {
    pumpTimer = setTimeout(pump, FETCH_INTERVAL_MS);
  }
}

async function pump() {
  if (state.inFlight) {
    pendingPump = true;
    return;
  }
  state.inFlight = true;
  try {
    setStatus('live', 'streaming');
    const bboxStr = currentBbox();
    const [flightsRes, airportsRes] = await Promise.all([
      fetch(`/api/flights?bbox=${bboxStr}`).then((r) => r.json()),
      fetch(`/api/airports?bbox=${bboxStr}&limit=400`).then((r) => r.json()),
    ]);

    ingestFlights(flightsRes.flights || []);
    state.airportsInView = airportsRes.airports || [];

    elHudFlights.textContent = state.flights.size.toLocaleString();
    elHudAirports.textContent = state.airportsInView.length.toLocaleString();
    elHudRefresh.textContent = `${Math.round(FETCH_INTERVAL_MS / 1000)}s`;
    state.lastFetchedAt = Date.now();

    refreshLayers();
  } catch (err) {
    console.error('pump failed', err);
    setStatus('error', 'offline');
    toast('Live data fetch failed — retrying…');
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

function ingestFlights(flights) {
  const now = performance.now();
  const seen = new Set();
  for (const f of flights) {
    seen.add(f.id);
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

const chatHistory = [];

function appendMessage(role, content, { tools = [], thinking = false } = {}) {
  const wrap = document.createElement('div');
  wrap.className = `msg msg-${role}${thinking ? ' msg-thinking' : ''}`;
  const body = document.createElement('div');
  body.className = 'msg-content';
  body.textContent = content;
  wrap.appendChild(body);
  if (tools.length) {
    const t = document.createElement('div');
    t.className = 'msg-tools';
    tools.forEach((name) => {
      const c = document.createElement('span');
      c.className = 'tool-chip';
      c.textContent = name;
      t.appendChild(c);
    });
    wrap.appendChild(t);
  }
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
  chatHistory.push({ role: 'user', content: text });

  const thinking = appendMessage('bot', '', { thinking: true });
  thinking.querySelector('.msg-content').innerHTML =
    'thinking <span class="thinking-dots"><span></span><span></span><span></span></span>';

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: chatHistory.slice(-8) }),
    });
    if (!res.ok) {
      const errBody = await res.text();
      thinking.remove();
      appendMessage('bot', `LLM call failed (${res.status}). ${errBody.slice(0, 240)}`);
      return;
    }
    const data = await res.json();
    thinking.remove();
    const tools = (data.actions || []).map((a) => a.tool);
    appendMessage('bot', data.reply || '(no reply)', { tools });
    if (data.reply) chatHistory.push({ role: 'assistant', content: data.reply });
  } catch (err) {
    thinking.remove();
    appendMessage('bot', `Network error talking to copilot: ${err.message}`);
  }
});

// ── Boot ─────────────────────────────────────────────────────────────────

(async function boot() {
  initMap();
  await new Promise((resolve) => state.map.once('load', resolve));
  wireLayerToggles();
  connectWs();
  schedulePump({ force: true });
  requestAnimationFrame(animate);
})();
