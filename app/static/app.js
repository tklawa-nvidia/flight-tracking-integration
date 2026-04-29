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
  // @deck.gl/extensions UMD bundle merges into the same `deck` global. The
  // extension is optional — if the script tag fails to load we render solid
  // ATS lines instead of dashed.
  PathStyleExtension,
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
  // When the agent issues `/api/map/track`, the server broadcasts both a
  // {type:'highlight'} and a {type:'view'} over the bus. The view always
  // arrives because it's pure camera math, but the highlight needs the
  // target plane to be in `state.flights` (which is bbox-scoped to the
  // current viewport). If the agent picks a plane that's just outside
  // the current bbox, the highlight no-ops — the camera moves but the
  // detail card never opens and trails never light up. We stash the
  // intent here and re-try it from `ingestFlights` once the post-flyTo
  // poll loads the plane in. {target: lowercase id-or-callsign, expiresAt}.
  pendingHighlight: null,
  layerVisibility: {
    flights: true,
    airports: true,
    arcs: false,
    trails: false,
    paths: false,        // breadcrumb paths shown automatically when zoomed in (off by default — they clutter the chart)
    weather: false,
    sua: false,          // FAA Special Use Airspace
    classes: false,      // Class B/C/D shells
    tfrs: true,          // Temporary Flight Restrictions (default on — they're rare and important)
    runways: false,      // FAA AM_Runway polygons (auto-load: small dataset)
    taxiways: false,     // FAA AM_Taxiway polygons (zoom-gated, bbox-fetched)
    obstacles: false,    // FAA Digital Obstacle File points (zoom-gated, bbox-fetched)
    ats: false,          // FAA ATS_Route lines (zoom-gated, bbox-fetched)
    // ── 30-min demo layers ────────────────────────────────────────────
    // METAR observations are coloured by VFR/MVFR/IFR/LIFR category and
    // refetched per-bbox on moveend, like taxiways/obstacles.
    metar: false,
    // NAS Status airport advisories (Ground Stops, GDPs, closures…) are
    // a single nationwide feed; rendered as airport-anchored severity
    // dots that throb when on.
    nas: false,
    // ARTCC (Air Route Traffic Control Center) sectorisation — globally
    // cached GeoJSON FeatureCollection of low-altitude ARTCC polygons.
    artcc: false,
    // NAVAIDs (VOR/VORTAC/DME/TACAN/NDB/ILS components) — bbox-fetched
    // points; the spine of any IFR procedure when you don't have the
    // SID/STAR linework itself.
    navaids: false,
  },
  // 3D airspace mode: extrudes Class B/C/D + SUA + TFR volumes from the
  // ground up to their published ceiling so the user can tilt the camera
  // and see the actual airspace box. `airspaceVScale` multiplies all
  // extruded heights — useful at low zoom (e.g. continental view) where
  // a 60,000 ft column is still only a few pixels tall, or at very high
  // zoom (single-airport) where the same column overflows the screen.
  // Plane icons + history trails also lift to their real altitudes when
  // this flag is on, so a plane at FL350 sits visually inside its
  // restricted-area column instead of disappearing under it.
  airspace3D: false,
  airspaceVScale: 1.0,
  // Cached map pitch (degrees). Updated by a 'pitchend' handler so the
  // IconLayer can decide whether to billboard or to lay flat on the
  // world plane — billboards look great straight-down but their
  // headings appear "pointed down" when the camera is tilted, since
  // the icon is forced to face the camera. Above ~25° we drop the
  // billboarding so the heading reads correctly from any angle.
  pitch: 0,
  airspace: {
    // Globally cached — loaded once when the toggle flips on.
    sua: null,
    classes: null,
    tfrs: null,
    runways: null,
    artcc: null,
    fetchedAt: { sua: 0, classes: 0, tfrs: 0, runways: 0, artcc: 0 },
    // Bbox-scoped — refetched on moveend when the layer is on. Stored as
    // a single {bboxKey, data} per dataset so we don't accumulate.
    bbox: { taxiways: null, obstacles: null, ats: null, navaids: null },
    bboxKey: { taxiways: null, obstacles: null, ats: null, navaids: null },
    fetchingBbox: { taxiways: false, obstacles: false, ats: false, navaids: false },
  },
  // METAR (weather-station) overlay: bbox-scoped like the bbox-airspace
  // layers above, but on a separate state slot because the upstream
  // (aviationweather.gov) is independent of FAA AIS and has its own TTL.
  metar: {
    bbox: null,            // { stations: [...], count, bbox }
    bboxKey: null,         // last bbox fingerprint we fetched for
    fetching: false,
  },
  // NAS Status — one nationwide list of airport advisories, refreshed
  // every 60 s while the layer is on. Stored as the array `events` so
  // both the chart layer and the per-airport drawer can read from it.
  nas: {
    events: null,          // [{airport, lat, lon, severity, events: [...]}, ...]
    fetchedAt: 0,
    fetching: false,
    nextRefresh: 0,        // performance.now() — driven by nasTick()
  },
  // Current map zoom (cached so layers can size themselves without
  // calling map.getZoom() repeatedly each animation tick).
  zoom: 3.4,
  // 0..1 sine-driven phase used to throb hazardous-airspace fills and the
  // ATS-route halo. Updated every animate() tick; layers consume it via
  // their `opacity` prop, which is cheap (no per-feature accessor recompute).
  pulsePhase: 0,
  // Faster strobe phase for high-severity NAS badges (closures, ground
  // stops). We feed the raw radian value into Math.sin in the layer
  // builder; ~1.6 Hz reads as "alarm" without being seizure-strobe.
  pulsePhaseFast: 0,
  // Selected aircraft details — populated asynchronously from
  // /api/flight/{icao24} and /api/flight/{icao24}/track when the user
  // clicks a plane. `selectedFlightInfo` carries origin/destination/
  // callsign metadata; `selectedTrack` is an array of pre-coloured
  // path segments rendered as a cyan→deep-blue gradient on the map.
  // `_flightDetailsToken` is a monotonically increasing race-guard so
  // a slow lookup that finishes after the user has clicked a *different*
  // aircraft doesn't paint the wrong drawer.
  selectedFlightInfo: null,
  selectedTrack: null,
  selectedRegistry: null,        // adsbdb / hexdb registry lookup result
  _flightDetailsToken: 0,
  // Active aircraft colour scheme. One of:
  //   'phase'    — Phase of flight (default; takeoff yellow → cruise
  //                orange → landing red, gray on ground)
  //   'altitude' — Light → bright green by flight level
  //   'vrate'    — Magenta climb / violet descent (diverging)
  //   'squawk'   — Emergency squawk highlight (7500/7600/7700)
  // Persisted in localStorage so a user's pick survives reloads.
  colorMode: 'phase',
  // METAR colour mode — same accordion-radio pattern as `colorMode` above
  // but applied to the weather-station body (the wind arrow always shows
  // wind direction/speed regardless of mode). Persisted in localStorage.
  //   'flt_cat'    — VFR / MVFR / IFR / LIFR (FAA flight categories)
  //   'wind'       — calm grey → gale red as wind speed climbs
  //   'temp'       — cold blue → hot red on the surface temperature
  //   'visibility' — fog red → clear green
  metarColorMode: 'flt_cat',
  // Per-color-mode bucket filter for the IconLayer. Only the categorical
  // color modes (`phase`, `squawk`) participate — the continuous modes
  // (altitude, vrate) don't make sense to bucket. The user toggles
  // individual buckets via the chip legend underneath each color-mode
  // accordion row; clicking a chip removes its bucket from the active
  // set, which immediately removes those planes from the map.
  //
  // Default: every bucket selected (== nothing filtered out, identical
  // behaviour to before the chip UI shipped). Persisted in localStorage
  // as `flightops:flightFilter` so a configured filter survives reloads.
  flightFilter: {
    phase:  new Set(['climb', 'level-slow', 'level-fast', 'descend', 'ground']),
    squawk: new Set(['7500', '7600', '7700', 'normal', 'ground']),
  },
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
// Don't extrapolate further than this since the last fix. We poll OpenSky
// every ~10s, so a flight we haven't heard from in 30s is either out of
// our bbox or has dropped off ADS-B. Capping at 30s prevents the icon
// from "running far ahead" on stale extrapolation only to be yanked back
// when a delayed fix finally arrives — at high zoom that yank is the
// most visible jitter.
const DEAD_RECKON_MAX_S = 30;
// When a new fix lands, the icon is currently at its dead-reckoned visual
// position. The new fix tells us where the plane actually is. Snapping to
// the new position would jitter, so we keep a decaying offset that smoothly
// pulls the icon onto the new dead-reckoning line over this many seconds.
// The same time-constant is used for *heading* glide — when the new fix
// reports a different heading than the old anchor, the icon's drawn
// orientation eases shortest-arc toward the new heading instead of
// snapping. Without that, a real-world ~5° course adjustment looks like
// a sudden swerve at high zoom.
const CORRECTION_DECAY_S = 3;
// Show breadcrumb paths for every visible flight at this zoom or above.
const TRAIL_ZOOM_THRESHOLD = 6;
// Visibility thresholds for layers that only make sense up-close. Below
// these, the layer is hidden even if its toggle is on so the chart stays
// legible at country zoom.
const ATS_MIN_ZOOM = 6;        // ATS routes — semi-transparent green lines
const RUNWAY_MIN_ZOOM = 9;     // Runway polygons — visible alongside taxiways
const TAXIWAY_MIN_ZOOM = 12;   // Taxiway polygons — only at airport-detail zoom
const OBSTACLE_MIN_ZOOM = 9;   // Obstacle points — towers, cranes, chimneys
// 30-min demo layers — visibility floors. METAR is legible from a
// continental zoom (one dot per major airport), NAVAIDs need a closer
// in to read at all without overplotting the icons.
// METAR_MIN_ZOOM was previously 4, which sat above the app's default
// startup zoom (~3.4). That meant a user toggling METAR at startup
// silently got nothing because the fetch gated on zoom too. Lowered
// to 3 so the layer "just works" out of the box at any reasonable
// continental-overview zoom.
const METAR_MIN_ZOOM = 3;
const NAVAID_MIN_ZOOM = 7;
// NAS status nationwide feed — refresh cadence while the layer is on.
const NAS_REFRESH_MS = 60 * 1000;
// RainViewer publishes a JSON manifest of available radar/satellite frames.
// No key required, ~5min refresh on their side.
const RAINVIEWER_MANIFEST = 'https://api.rainviewer.com/public/weather-maps.json';
const WEATHER_REFRESH_MS = 5 * 60 * 1000;

// ── DOM refs ─────────────────────────────────────────────────────────────

// Header status pill was retired — the live-data status now lives in the
// bottom live-bar. We keep these as optional refs so older deploys with
// the pill still in the DOM keep working.
const elStatusPill = document.getElementById('status-pill');
const elStatusText = document.getElementById('status-text');
const elLiveBarStatus = document.getElementById('live-bar-status');
const elLiveBarStatusState = document.getElementById('live-bar-status-state');
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
// Hover popup (METAR / NAS / ARTCC / NAVAID hover cards) — created
// lazily on first hover so a session that never enables those layers
// pays nothing. Anchored to the cursor in screen-pixel space rather
// than to a deck.gl `getTooltip` because the latter only renders text.
let elMapPopup = null;
function getMapPopup() {
  if (elMapPopup) return elMapPopup;
  elMapPopup = document.createElement('div');
  elMapPopup.className = 'map-popup';
  document.querySelector('.map-stage')?.appendChild(elMapPopup);
  return elMapPopup;
}
function hidePopup() {
  if (elMapPopup) elMapPopup.classList.remove('show');
}
function showPopup(html, x, y) {
  const el = getMapPopup();
  el.innerHTML = html;
  el.style.left = `${Math.round(x)}px`;
  el.style.top = `${Math.round(y)}px`;
  el.classList.add('show');
}

// ── NAS status — DOM overlay for the rounded glowing pills ──────────────
// We render NAS badges as DOM nodes (rather than a deck.gl TextLayer) so
// we can use border-radius, backdrop-filter, and CSS keyframe animations
// for the colour-tinted glow. The overlay container is positioned over
// the maplibre map and we project lat/lon → pixel on every move/render
// tick. Cardinality is small (typically <30 active events nationwide),
// so the DOM cost is negligible compared to a full deck.gl rebuild.
let elNasOverlay = null;
const _nasMarkers = new Map(); // airport code → element
function getNasOverlay() {
  if (elNasOverlay) return elNasOverlay;
  elNasOverlay = document.createElement('div');
  elNasOverlay.className = 'nas-overlay';
  document.querySelector('.map-stage')?.appendChild(elNasOverlay);
  return elNasOverlay;
}
function clearNasOverlay() {
  for (const el of _nasMarkers.values()) el.remove();
  _nasMarkers.clear();
}
function renderNasOverlay() {
  const root = getNasOverlay();
  const visible =
    state.layerVisibility.nas &&
    state.nas.events &&
    state.map; // map needed for projection
  if (!visible) {
    if (_nasMarkers.size) clearNasOverlay();
    root.classList.remove('show');
    return;
  }
  root.classList.add('show');

  const positioned = state.nas.events.filter(
    (e) => Number.isFinite(e.lat) && Number.isFinite(e.lon)
  );
  const seen = new Set();
  for (const e of positioned) {
    const key = e.airport || `${e.lat},${e.lon}`;
    seen.add(key);
    let el = _nasMarkers.get(key);
    if (!el) {
      el = document.createElement('button');
      el.type = 'button';
      el.className = 'nas-tag';
      el.addEventListener('click', (ev) => {
        ev.stopPropagation();
        selectNas(e);
      });
      el.addEventListener('mouseenter', (ev) => {
        const rect = root.getBoundingClientRect();
        showPopup(
          nasPopupHtml(e),
          ev.clientX - rect.left,
          ev.clientY - rect.top
        );
      });
      el.addEventListener('mouseleave', hidePopup);
      root.appendChild(el);
      _nasMarkers.set(key, el);
    }
    // Refresh content + class even on existing element so a severity
    // upgrade (advisory → ground_stop) flips the colour mid-session.
    const sev = e.severity || 'info';
    const tag = NAS_TAG[sev] || NAS_TAG.info;
    el.dataset.sev = sev;
    el.className = `nas-tag sev-${sev}`;
    el.innerHTML =
      `<span class="nas-tag-dot"></span>` +
      `<span class="nas-tag-text">${tag}</span>` +
      `<span class="nas-tag-code">${(e.airport || '').slice(0, 4)}</span>`;
    // Project to pixel space. The calc()-based percentages are resolved
    // against the element's own bounding box, so we get pill-centred
    // horizontally on the airport and lifted ~24 px above it regardless
    // of pill width or device DPI. -22px adds a small visual gap above
    // the airport dot so the colour-tinted glow doesn't smother it.
    const p = state.map.project([e.lon, e.lat]);
    el.style.transform =
      `translate(calc(${Math.round(p.x)}px - 50%),` +
      ` calc(${Math.round(p.y)}px - 100% - 22px))`;
  }
  // Garbage-collect markers whose airports are gone from the latest poll.
  for (const [key, el] of _nasMarkers) {
    if (!seen.has(key)) {
      el.remove();
      _nasMarkers.delete(key);
    }
  }
}

// ── Plane icon (data URI so we don't need an asset file) ────────────────
// NOTE: explicit width/height on the <svg> root are *required* for deck.gl's
// icon manager to rasterize correctly. Without them some browsers compute a
// 0×0 layout and the icon appears invisible even though everything else looks
// healthy. We also use the per-feature getIcon callback rather than
// iconAtlas + iconMapping — that path is more reliable for data URIs.

// IMPORTANT: this is an *alpha mask* icon (mask: true). The path is filled
// solid white and shape is carried entirely by the alpha channel. deck.gl
// then multiplies the per-feature `getColor` against the mask, so phase-of-
// flight tinting (climb/level/descend) actually shows up. If you switch the
// fill to a real colour or set mask:false, `getColor` is ignored and every
// plane renders identically.
const PLANE_ICON_SVG = encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
     <path d="M32 4 L36 26 L60 36 L60 42 L36 36 L34 54 L42 58 L42 60 L32 58 L22 60 L22 58 L30 54 L28 36 L4 42 L4 36 L28 26 Z"
           fill="#ffffff"/>
   </svg>`
);
const PLANE_ICON_URL = `data:image/svg+xml;utf8,${PLANE_ICON_SVG}`;

const PLANE_ICON_DEF = {
  url: PLANE_ICON_URL,
  width: 64,
  height: 64,
  anchorX: 32,
  anchorY: 32,
  mask: true,
};

// ── Wind vane icon for METAR stations ───────────────────────────────────
// A tapered shaft with a chevron tip, points UP by default. Anchored at
// (32, 60) — i.e. a few pixels above the base of the shaft — so when the
// icon is placed at a station's lat/lon, the arrow extends *outward from*
// the station and rotates around that point. We compute deck.gl rotation
// as `-(wind_dir + 180)` so the arrow points downwind (the direction the
// wind is going), matching how every modern weather UI draws wind arrows.
// The path is filled white; the IconLayer's `mask: true` lets us tint
// it via getColor (driven by metarArrowColor).
const WIND_ICON_SVG = encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
     <path d="M32 4 L48 26 L36 22 L36 56 L28 56 L28 22 L16 26 Z" fill="#ffffff"/>
   </svg>`
);
const WIND_ICON_URL = `data:image/svg+xml;utf8,${WIND_ICON_SVG}`;
const WIND_ICON_DEF = {
  url: WIND_ICON_URL,
  width: 64,
  height: 64,
  anchorX: 32,
  anchorY: 56,
  mask: true,
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
    // MapLibre's default maxPitch is 60°. 85° is the upper safe limit
    // (anything beyond starts mathematically degenerate ray casts at
    // the horizon). 85° is enough to feel almost cockpit-level when
    // 3D airspace volumes are extruded, so we open it up here.
    maxPitch: 85,
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
    onHover: (info) => handleDeckHover(info),
  });
  state.map.addControl(state.deckOverlay);

  state.zoom = state.map.getZoom();

  // Re-fetch flights + bbox-scoped airspace whenever the user pans/zooms.
  state.map.on('moveend', () => {
    state.zoom = state.map.getZoom();
    schedulePump({ force: true });
    refreshBboxAirspace();
    refreshBboxMetar();
    refreshLayers();
    renderNasOverlay();
    // The "Aircraft in view" / "Airports in view" HUD counts must
    // refresh on every pan/zoom regardless of whether the live pump
    // ran this turn. They describe what's currently visible, not the
    // freshness of the data — so they need to update even when the
    // feed is paused or set to manual cadence (cases where
    // schedulePump short-circuits and never calls pump).
    updateInViewHud();
  });
  // Update NAS pill positions continuously through the pan/zoom gesture
  // so they don't lag the underlying canvas. Cheap: we only re-project a
  // handful of points per frame, no DOM creation.
  state.map.on('move', () => {
    if (state.layerVisibility.nas) renderNasOverlay();
  });
  // Plane size is zoom-driven, so we need to refresh on zoom-in-progress
  // too, otherwise icons jump in size only after the gesture ends.
  state.map.on('zoom', () => {
    state.zoom = state.map.getZoom();
  });
  // Track pitch so the IconLayer can decide whether to billboard. We
  // listen to 'pitchend' (fires once per gesture) instead of 'pitch'
  // (fires every frame) — flipping `billboard` recompiles the layer's
  // shader pipeline, so per-frame churn would be wasteful and the
  // visual difference between "during the drag" and "after the drag"
  // is imperceptible.
  state.map.on('pitchend', () => {
    state.pitch = state.map.getPitch();
    refreshLayers();
  });
  // Silently swallow tile-load errors so MapLibre doesn't surface
  // "no data at this zoom level" style messages on raster overlays
  // that legitimately have no native tile at high zoom.
  state.map.on('error', (e) => {
    const msg = (e && e.error && e.error.message) || '';
    if (/tile|raster|404/i.test(msg)) return;
    console.debug('maplibre error', msg);
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

// Returns the heading the icon should be drawn at, in degrees CW from
// north (i.e. the OpenSky convention). When a fresh fix arrives with a
// different heading than the previous anchor, we glide shortest-arc from
// the old heading to the new one over CORRECTION_DECAY_S — same
// time-constant the position correction uses, so the icon's orientation
// and track stay coherent during the glide. f.headingCorrection is the
// signed shortest-arc delta in degrees that we still owe (old − new).
function renderHeading(f, nowMs) {
  const base = f.anchor?.heading ?? f.heading ?? 0;
  if (!f.headingCorrection || f.correctionTs == null) return base;
  const age = (nowMs - f.correctionTs) / 1000;
  const decay = Math.exp(-age / CORRECTION_DECAY_S);
  if (decay < 0.01) {
    f.headingCorrection = 0;
    return base;
  }
  // Wrap to (−180, 180] so the rendered angle stays in a sane range
  // and we never accidentally take the long way round a discontinuity.
  let h = base + f.headingCorrection * decay;
  h = ((h % 360) + 360) % 360;
  return h;
}

// Shortest signed delta from a → b in degrees, range (−180, 180].
function shortestArcDeg(a, b) {
  let d = (b - a) % 360;
  if (d > 180) d -= 360;
  if (d <= -180) d += 360;
  return d;
}

// When a new state vector arrives the icon is currently at its dead-
// reckoned position based on the *previous* anchor. Naïvely setting the
// correction to (oldRender − newAnchor) and decaying it to zero produces
// a visible *backward* slide whenever the previous extrapolation over-
// shot — the icon walks back along its heading until the correction
// runs out. This is what users see at high zoom as "the plane jumps
// back a bit before going forward".
//
// Real flight trackers avoid this by decomposing the offset into
// along-track (parallel to heading) and cross-track (perpendicular)
// components, gliding only the cross-track to zero, and clamping the
// along-track so the icon's instantaneous along-track velocity never
// goes negative. Geometry:
//
//   v_along(t) = vel − along₀ / τ · decay(t)
//
// requiring v_along ≥ 0 at all t (worst case decay=1) gives along₀ ≤
// vel·τ. We use 0.85·vel·τ as a margin so the icon always has a small
// positive along-track velocity rather than momentarily appearing to
// hover. If the previous extrapolation under-shot (along₀ < 0), we
// keep the correction as-is — that just makes the icon glide forward,
// which looks fine.
function clampedCorrection(oldRender, newAnchor) {
  const dLon = oldRender[0] - newAnchor.lon;
  const dLat = oldRender[1] - newAnchor.lat;
  // Stationary / no-heading aircraft: nothing to project against, so
  // glide directly. (A plane sitting on the ground can't move
  // backward — it can't move at all.)
  const vel = newAnchor.vel || 0;
  if (newAnchor.on_ground || vel <= 0 || newAnchor.heading == null) {
    return [dLon, dLat];
  }
  const lat0 = (newAnchor.lat * Math.PI) / 180;
  const cosLat = Math.cos(lat0);
  // Local east-north metres so along/cross decomposition is metric and
  // the comparisons against vel·τ make dimensional sense.
  const vN = dLat * 111_320;
  const vE = dLon * 111_320 * cosLat;
  const hdg = (newAnchor.heading * Math.PI) / 180;
  const aN = Math.cos(hdg);
  const aE = Math.sin(hdg);
  let along = vN * aN + vE * aE;          // metres ahead along heading
  const cross = vN * aE - vE * aN;        // metres right of heading
  const maxAlong = vel * CORRECTION_DECAY_S * 0.85;
  if (along > maxAlong) along = maxAlong;
  // Reconstruct east/north metres → lon/lat degrees.
  const vN2 = along * aN + cross * aE;
  const vE2 = along * aE - cross * aN;
  return [vE2 / (111_320 * cosLat), vN2 / 111_320];
}

// ── Airspace + airfield styling ──────────────────────────────────────────
// Outline-free, fill-only translucent polygons. Color separation alone is
// enough on the dark basemap and the result reads as a modern "hint tint"
// rather than busy chart symbology.
//
// SUA TYPE_CODE values (per FAA AIS docs):
//   P = Prohibited   R = Restricted   W = Warning   A = Alert
//   M = MOA          N = National Security Area     C = CFA
// Per-feature alphas are intentionally low so overlapping polygons (e.g. a
// MOA stacked over warning + alert + restricted areas) don't pile up into
// an opaque blob. The deck.gl GeoJsonLayer uses additive-ish source-over
// blending, so two ~75-alpha fills that overlap read as ~110 — still
// distinguishable from either parent. Each layer additionally has a
// container-level `opacity` (see buildLayers) that further dampens
// stacked density when 3D extrusion is on.
const SUA_FILL = {
  P: [255,  64,  76,  90],  // prohibited — saturated red
  R: [255, 124,  64,  75],  // restricted — orange
  W: [255, 196,  64,  60],  // warning   — amber
  A: [120, 220, 140,  50],  // alert     — green
  M: [240, 220,  88,  55],  // MOA       — yellow
  N: [200, 100, 255,  65],  // NSA       — purple
  C: [180, 220, 255,  45],  // CFA       — pale blue
  _: [180, 200, 220,  45],
};

// CLASS airspace shells. Class B = magenta, C = blue, D = teal, MODE-C = neon.
// FAA AIS encodes the actual class in `properties.CLASS` (B/C/D/E) and the
// shell type in `TYPE_CODE` ("CLASS" or "MODE-C"). We key off CLASS first so
// the colour matches the chart symbology pilots already know.
const CLASS_FILL = {
  B: [233, 100, 200,  50],
  C: [120, 170, 255,  45],
  D: [120, 230, 220,  40],
  E: [180, 200, 220,  30],
  'MODE-C': [255, 180, 100, 35],
  _: [180, 200, 220,  30],
};

// TFRs are dynamic + hazardous, so they get a saturated red fill that
// throbs (driven by layer opacity, not per-feature alpha). Slightly less
// opaque than before so a TFR sitting on top of an SUA still shows the
// underlying volume through it.
const TFR_FILL = [255, 64, 96, 165];

// ── Airspace altitude → metres helper ────────────────────────────────────
// FAA AIS encodes ceilings as a tuple of (UPPER_VAL, UPPER_UOM, UPPER_CODE).
// UOM is "FL" (flight level → ×100 ft), "FT" (feet), "M" (metres), or empty.
// CODE is "MSL" / "AGL" / "BY NOTAM" — for visualisation we treat them all
// as MSL-equivalent since we're not modelling terrain. Returns metres or
// 0 if no usable ceiling is on file.
const FT_PER_M = 3.28084;
function airspaceCeilingMetres(props) {
  if (!props) return 0;
  const raw = props.UPPER_VAL ?? props.UPPER ?? props.upper_val;
  const val = parseFloat(raw);
  if (!Number.isFinite(val) || val <= 0) return 0;
  // GP — "GP" sometimes appears as a sentinel for "ground plus" with the
  // value in feet AGL. Treat it like FT.
  const uom = (props.UPPER_UOM || props.UPPER_CODE || 'FT').toString().trim().toUpperCase();
  let ft;
  if (uom === 'FL' || uom.startsWith('FL')) ft = val * 100;
  else if (uom === 'M' || uom === 'M-AMSL' || uom === 'METRES') ft = val * FT_PER_M;
  else ft = val; // default to feet — covers FT, AGL, MSL, GP, blank, etc.
  // Cap at 60 000 ft so a "999 999 unlimited" sentinel doesn't blow the
  // scene through the ceiling.
  if (ft > 60000) ft = 60000;
  return ft / FT_PER_M;
}

function airspaceElevationFor(feature) {
  // Flat metres value, multiplied by the user's vertical-scale slider.
  return airspaceCeilingMetres(feature?.properties) * state.airspaceVScale;
}

// ── Plane altitude → world-Z helper ──────────────────────────────────────
// OpenSky `baro_altitude` is metres MSL (we store it on the flight as
// `alt_m`). When 3D airspace mode is active we use it as the icon's Z
// coordinate, multiplied by the same vertical-scale slider as the
// extruded volumes — that's what keeps a plane inside (or above) its
// restricted area when the user pulls the slider, instead of drifting
// out of frame.
function flightAltMetres(f) {
  const m = Number.isFinite(f?.alt_m) ? f.alt_m : 0;
  if (m <= 0) return 0;
  if (m > 18500) return 18500;   // ~FL600 — clamp absurd reports
  return m;
}

function flightRenderAltMetres(f) {
  if (!state.airspace3D) return 0;
  return flightAltMetres(f) * state.airspaceVScale;
}

function suaFillFor(feature) {
  const code = (feature?.properties?.TYPE_CODE || '').trim().toUpperCase();
  return SUA_FILL[code] || SUA_FILL._;
}

function classFillFor(feature) {
  const p = feature?.properties || {};
  const t = (p.TYPE_CODE || '').trim().toUpperCase();
  if (t === 'MODE-C') return CLASS_FILL['MODE-C'];
  const c = (p.CLASS || '').trim().toUpperCase();
  return CLASS_FILL[c] || CLASS_FILL._;
}

// Operational status palettes — FAA codes encode runway/taxiway state.
// 2 = open, 7 = closed, 1 = closed indefinitely, 3 = under construction,
// 4 = repurposed as taxiway, 5 = unknown.
//
// Runways and taxiways are nearly always operational, so when they
// share an "open = green" hue the airport reads as one undifferentiated
// green slab — you can't tell pavement role apart at a glance. We
// split the open hue: runways take a pink/magenta fill (matches the
// legend swatch) so they pop out of the green taxiway grid, which
// matches how every published AIP airport diagram colour-codes them.
// Anomalous states (closed, construction, repurposed) override with
// the same shared danger palette in both layers since "this pavement
// is unsafe" trumps the role distinction.
const _OPER_NONOPEN = {
  '7': [255,  90, 110, 200],   // closed — red
  '1': [255,  90, 110, 200],
  '3': [255, 170,  80, 190],   // under construction — orange
  '4': [255, 220, 100, 180],   // repurposed — yellow
  '5': [180, 200, 220, 140],   // unknown — neutral
  _:   [200, 215, 230, 160],
};
const RWY_OPEN_FILL = [255, 130, 195, 185];   // open runway — magenta/pink
const TWY_OPEN_FILL = [120, 230, 150, 170];   // open taxiway — green

function rwyFillFor(feature) {
  const code = String(feature?.properties?.RWY_OPER ?? '').trim();
  if (code === '2' || code === '') return RWY_OPEN_FILL;
  return _OPER_NONOPEN[code] || _OPER_NONOPEN._;
}
function twyFillFor(feature) {
  const code = String(feature?.properties?.TWY_OPER ?? '').trim();
  if (code === '2' || code === '') return TWY_OPEN_FILL;
  return _OPER_NONOPEN[code] || _OPER_NONOPEN._;
}

// Obstacle color ramps on AGL height — taller obstacles get hotter colors
// so the eye is drawn to the towers that matter. AGL is in feet.
function obstacleColor(feature) {
  const agl = Number(feature?.properties?.AGL) || 0;
  if (agl >= 1000) return [255, 80, 110, 230];   // 1000+ ft — red
  if (agl >=  500) return [255, 150,  90, 220];  //  500+ ft — orange
  if (agl >=  300) return [255, 210, 110, 210];  //  300+ ft — amber
  return [200, 220, 240, 190];                   //  base   — pale
}

// Neon green for ATS routes. Rendered as two stacked PathLayers — a
// thin soft halo and a slightly-brighter solid core — so the airways
// read as glowing electrical traces. Both are static (no per-frame
// updates) since animating them across the full route set was too
// expensive on the main thread.
const ATS_HALO_COLOR = [ 80, 255, 160,  70];   // thin + soft glow
const ATS_CORE_COLOR = [150, 255, 195, 235];   // bright but not overpowering

// ── METAR / NAS / NAVAID colour helpers ──────────────────────────────────
// METAR rendering is a small composite "weather instrument": a body
// circle coloured by the active mode + a wind-vane arrow that always
// reads wind direction and is sized by speed. Four colour modes —
// flt_cat, wind, temp, visibility — are user-selectable from the layer
// panel (see wireMetarColorMode), with the chosen value persisted in
// localStorage.
const METAR_FILL = {
  VFR:  [ 80, 220, 130, 235],
  MVFR: [ 80, 160, 240, 235],
  IFR:  [255, 100, 100, 240],
  LIFR: [220, 100, 220, 240],
  _:    [180, 200, 220, 220],
};

// Linear interpolate two RGBA colour stops at t∈[0,1]. Used by the wind/
// temp/visibility colour ramps below so the dot blends smoothly through
// the transition zones (e.g. between a 12 kt breeze and a 22 kt blow)
// rather than snapping between buckets.
function _lerpRGBA(a, b, t) {
  return [
    Math.round(a[0] + (b[0] - a[0]) * t),
    Math.round(a[1] + (b[1] - a[1]) * t),
    Math.round(a[2] + (b[2] - a[2]) * t),
    Math.round((a[3] ?? 235) + ((b[3] ?? 235) - (a[3] ?? 235)) * t),
  ];
}
// Walk a piecewise ramp [(stop, color), ...] and return the colour at v.
function _rampColor(stops, v) {
  if (!Number.isFinite(v)) return stops[0][1];
  if (v <= stops[0][0]) return stops[0][1];
  if (v >= stops[stops.length - 1][0]) return stops[stops.length - 1][1];
  for (let i = 1; i < stops.length; i++) {
    const [s1, c1] = stops[i];
    if (v <= s1) {
      const [s0, c0] = stops[i - 1];
      const t = (v - s0) / Math.max(s1 - s0, 1e-6);
      return _lerpRGBA(c0, c1, t);
    }
  }
  return stops[stops.length - 1][1];
}

// Wind speed ramp (kt). Calm slate → light cyan → green at flying-day
// speeds → yellow/orange at a stiff breeze → red at gale.
const METAR_WIND_RAMP = [
  [  0, [140, 160, 180, 220]],
  [  3, [120, 200, 235, 230]],
  [ 10, [ 90, 220, 150, 235]],
  [ 18, [240, 220,  90, 240]],
  [ 28, [255, 150,  70, 245]],
  [ 40, [255,  90,  90, 250]],
];
// Temperature ramp (°C). NWS-style: cold purple/blue → green/yellow
// near 20 °C → red over 35 °C.
const METAR_TEMP_RAMP = [
  [-25, [120, 130, 220, 230]],
  [-10, [ 90, 180, 235, 230]],
  [  0, [120, 220, 240, 235]],
  [ 10, [120, 230, 170, 235]],
  [ 20, [220, 230, 120, 240]],
  [ 30, [255, 170,  90, 245]],
  [ 40, [255,  90,  80, 250]],
];
// Visibility ramp (statute miles). Below 1 sm = LIFR red; 10+ sm = clear.
const METAR_VIS_RAMP = [
  [ 0.0, [220, 100, 220, 245]],
  [ 1.0, [255, 100, 100, 245]],
  [ 3.0, [255, 180,  90, 240]],
  [ 5.0, [255, 220, 110, 235]],
  [10.0, [120, 220, 150, 230]],
];

function _parseVisibility(m) {
  // visib_sm comes back as a number, a string ("10+", "1 1/2"), or null.
  const v = m.visib_sm;
  if (typeof v === 'number') return v;
  if (typeof v !== 'string') return null;
  const s = v.trim();
  if (!s) return null;
  if (s.includes('+')) return parseFloat(s) || 10;
  if (s.includes('/')) {
    const m2 = s.match(/(?:(\d+)\s+)?(\d+)\/(\d+)/);
    if (m2) {
      const whole = parseInt(m2[1] || '0', 10);
      return whole + parseInt(m2[2], 10) / parseInt(m2[3], 10);
    }
  }
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : null;
}

function metarColor(m) {
  const mode = state.metarColorMode || 'flt_cat';
  if (mode === 'wind') {
    const w = Number.isFinite(m.wind_kt) ? m.wind_kt : 0;
    // Gusts read as "stronger than steady" so we bias the colour toward
    // the gust value when it's much higher than the sustained wind.
    const eff = Number.isFinite(m.wind_gust_kt)
      ? Math.max(w, m.wind_gust_kt * 0.85 + w * 0.15)
      : w;
    return _rampColor(METAR_WIND_RAMP, eff);
  }
  if (mode === 'temp') {
    return _rampColor(METAR_TEMP_RAMP, m.temp_c);
  }
  if (mode === 'visibility') {
    return _rampColor(METAR_VIS_RAMP, _parseVisibility(m));
  }
  return METAR_FILL[m.flt_cat] || METAR_FILL._;
}

// Wind arrow size (px). We map sustained wind speed to a length so a
// glance across the chart reads pressure-gradient-tight regions vs
// calm pockets without any colour decoding.
function metarArrowSize(m) {
  const w = Number.isFinite(m.wind_kt) ? m.wind_kt : 0;
  if (w < 3) return 0;            // calm — no arrow drawn
  if (w < 10) return 22;
  if (w < 20) return 30;
  if (w < 30) return 38;
  if (w < 45) return 46;
  return 54;
}

// Wind arrow color. The arrow always matches the body so each station
// reads as a single instrument: pick "flight category" and both the
// dot and the arrow turn green/blue/red together; pick "temperature"
// and they ride the temperature ramp together; etc. Gust information
// is conveyed by the arrow's *size* (see metarArrowSize), not its
// hue, so the color encoding stays one-dimensional and unambiguous.
function metarArrowColor(m) {
  return metarColor(m);
}

function metarHasWind(m) {
  return Number.isFinite(m.wind_kt) && m.wind_kt >= 3 && Number.isFinite(m.wind_dir);
}

// NAS Status severity ramp. Anything `info`/`advisory` is muted — those
// are usually deicing notices or runway-config messages. `delay` and
// `ground_stop` light up in amber/red so the user's eye is drawn to
// the airports actually losing throughput. The pulse is applied at
// the layer level (see buildLayers), not per-feature.
const NAS_FILL = {
  info:        [120, 170, 255, 200],
  advisory:    [120, 170, 255, 220],
  delay:       [255, 196,  64, 235],
  ground_stop: [255,  80, 110, 245],
  closure:     [255,  64,  76, 250],
};
// Compact tag rendered inside the NAS badge — kept to ≤4 glyphs so the
// pill stays small enough that it doesn't smother the airport dot below.
const NAS_TAG = {
  info:        'NOTE',
  advisory:    'ADV',
  delay:       'DLY',
  ground_stop: 'STOP',
  closure:     'CLSD',
};
// Hard events (closures + ground stops) get a fast strobe so they steal
// the eye in a screen full of advisories. Soft events get a gentle 1.4 s
// throb that matches the rest of the chart.
const NAS_HARD_SEV = new Set(['ground_stop', 'closure']);
function nasFillFor(e) {
  return NAS_FILL[e.severity] || NAS_FILL.info;
}
function nasTagFor(e) {
  return NAS_TAG[e.severity] || NAS_TAG.info;
}

// NAVAID class symbology — yellow for the VOR family (the radio aids
// pilots build mental maps around), pale blue for everything else
// (NDB / DME-only / TACAN / ILS components / approach fixes). The
// CLASS_TXT field varies wildly across record types so we sniff for
// the prefix rather than enumerating every code.
function navaidColor(f) {
  const cls = (f.properties?.CLASS_TXT || '').toUpperCase();
  if (cls.includes('VOR')) return [255, 240, 130, 230];   // VOR / VORTAC / VOR-DME
  return [180, 230, 255, 215];                            // NDB / DME / TACAN / ILS
}

// ARTCC label centroids — computed once per FeatureCollection because
// the boundary polygons don't move and a 21-feature centroid pass is
// trivially cheap to memoise. Cached on the FC object itself so a
// reload (e.g. TTL refetch) automatically invalidates with the data.
function ensureArtccLabels(fc) {
  if (!fc || !fc.features) return null;
  if (fc.__labels) return fc.__labels;
  const labels = [];
  for (const feat of fc.features) {
    const c = polygonCentroid(feat.geometry);
    if (!c) continue;
    const ident = feat.properties?.IDENT || '';
    if (!ident) continue;
    labels.push({ position: c, text: ident });
  }
  fc.__labels = labels;
  return labels;
}

// Light-weight area-weighted polygon centroid that handles both
// Polygon and MultiPolygon geometries. Imperfect for crazy shapes but
// the ARTCC polygons are convex-ish and the label only needs to land
// somewhere inside the boundary. We weight by signed area so a
// MultiPolygon's biggest piece dominates (e.g. ZSE picks the mainland
// chunk over the small pieces at the AK panhandle).
function polygonCentroid(geom) {
  if (!geom) return null;
  let polys = [];
  if (geom.type === 'Polygon') polys = [geom.coordinates];
  else if (geom.type === 'MultiPolygon') polys = geom.coordinates;
  else return null;
  let totalArea = 0;
  let cx = 0, cy = 0;
  for (const poly of polys) {
    const ring = poly[0];                // outer ring only
    if (!ring || ring.length < 3) continue;
    let a = 0, x = 0, y = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      const [x0, y0] = ring[i];
      const [x1, y1] = ring[i + 1];
      const f = x0 * y1 - x1 * y0;
      a += f;
      x += (x0 + x1) * f;
      y += (y0 + y1) * f;
    }
    a *= 0.5;
    if (a === 0) continue;
    const ax = x / (6 * a);
    const ay = y / (6 * a);
    const w = Math.abs(a);
    cx += ax * w;
    cy += ay * w;
    totalArea += w;
  }
  if (totalArea === 0) return null;
  return [cx / totalArea, cy / totalArea];
}

// ── Plane color coding ───────────────────────────────────────────────────
// One warm ramp covers the whole flight envelope so the chart reads as a
// single hue family instead of three competing palettes:
//
//   on the ground           near-black
//   landing  (descending)   muted brick red (not full red)
//   level    (cruise)       orange — light when slow, deep when fast
//   takeoff  (climbing)     warm yellow
//
// Transitions between bands use smoothstep blends rather than hard
// thresholds, so a plane easing through level-off doesn't snap from
// yellow to orange — it eases.
//
// 500 fpm ≈ 2.54 m/s. OpenSky publishes vertical rate in m/s. We give
// the ramp a wider tail (≈ ±700 fpm to fully saturate) so brief vertical
// jitter near level flight doesn't paint the icon yellow or red.
const VRATE_CLIMB_FULL_MPS    =  3.6;   // ≥ this → fully takeoff yellow
const VRATE_DESCEND_FULL_MPS  = -3.6;   // ≤ this → fully landing red

// Speed ramp for the orange cruise band. Slow traffic (~50 m/s GA) reads
// as a light, warm orange; fast cruisers (~250 m/s) deepen toward burnt
// orange so they're still distinguishable from the climb yellow above.
const LEVEL_SPEED_LO_MPS = 50;
const LEVEL_SPEED_HI_MPS = 250;

const GROUND_COLOR   = [195, 200, 210, 215];  // light gray — on the ground, "out of play"
const LANDING_COLOR  = [200,  78,  62, 235];  // muted brick red
const ORANGE_SLOW    = [255, 178,  82, 235];  // light, warm orange
const ORANGE_FAST    = [232, 118,  40, 235];  // deep burnt orange
const TAKEOFF_COLOR  = [255, 214,  72, 235];  // warm yellow

// (Kept exported so a previous "color planes by airline" wiring still
// works; phase-of-flight is the default colour now.)
const CLIMB_COLOR   = TAKEOFF_COLOR;
const DESCEND_COLOR = LANDING_COLOR;
const LEVEL_COLOR_LIGHT = ORANGE_SLOW;
const LEVEL_COLOR_DARK  = ORANGE_FAST;

// Linear interpolate two RGB triplets and tag with a fixed alpha. Keeps
// the gradient simple — perceptually-uniform colour spaces are overkill
// for what's effectively a "cooler / hotter" cue.
function lerpRgba(a, b, t, alpha = 235) {
  const u = Math.max(0, Math.min(1, t));
  return [
    Math.round(a[0] + (b[0] - a[0]) * u),
    Math.round(a[1] + (b[1] - a[1]) * u),
    Math.round(a[2] + (b[2] - a[2]) * u),
    alpha,
  ];
}

// Cubic Hermite ease — same shape as GLSL smoothstep. Used to soften
// the band-to-band transitions so colour changes feel like a fade, not
// a step, as a plane levels off / starts a descent.
function smoothstep01(t) {
  const u = Math.max(0, Math.min(1, t));
  return u * u * (3 - 2 * u);
}

// ── Selected-flight track gradient ───────────────────────────────────────
// When the user clicks an aircraft we render its historical waypoint
// path (from OpenSky's /tracks/all, exposed via /api/flight/.../track)
// as a cyan→deep-blue gradient PathLayer. Older waypoints sit at the
// deep-blue end so the eye naturally reads "where it came from"
// fading into "where it is now".
//
// PathLayer's `getColor` is per-feature, not per-vertex, so we explode
// the waypoint list into N-1 short segments and colour each one.
// O(N) to build, ~120 waypoints typical → trivial.
const TRACK_GRADIENT_OLD = [ 40,  85, 180];   // deep blue — start of the trail
const TRACK_GRADIENT_NEW = [120, 230, 255];   // bright cyan — current position

function trackGradient(t) {
  const u = Math.max(0, Math.min(1, t));
  return [
    Math.round(TRACK_GRADIENT_OLD[0] + (TRACK_GRADIENT_NEW[0] - TRACK_GRADIENT_OLD[0]) * u),
    Math.round(TRACK_GRADIENT_OLD[1] + (TRACK_GRADIENT_NEW[1] - TRACK_GRADIENT_OLD[1]) * u),
    Math.round(TRACK_GRADIENT_OLD[2] + (TRACK_GRADIENT_NEW[2] - TRACK_GRADIENT_OLD[2]) * u),
    230,
  ];
}

function buildSelectedTrackSegments(waypoints) {
  if (!Array.isArray(waypoints) || waypoints.length < 2) return [];
  const segs = [];
  const last = waypoints.length - 1;
  for (let i = 0; i < last; i++) {
    const a = waypoints[i];
    const b = waypoints[i + 1];
    if (a == null || b == null) continue;
    if (a.lat == null || a.lon == null || b.lat == null || b.lon == null) continue;
    // Color the segment by its *end* index — that way the very last
    // segment (joining the second-to-last waypoint to "now") is the
    // bright cyan end of the gradient. We store both endpoints' MSL
    // altitude in metres so the PathLayer can lift the segment to its
    // recorded altitude when 3D airspace mode is on.
    const t = (i + 1) / last;
    const aAltM = Number.isFinite(a.alt_ft) ? a.alt_ft / FT_PER_M : 0;
    const bAltM = Number.isFinite(b.alt_ft) ? b.alt_ft / FT_PER_M : 0;
    segs.push({
      a: { lon: a.lon, lat: a.lat, alt_m: aAltM },
      b: { lon: b.lon, lat: b.lat, alt_m: bAltM },
      color: trackGradient(t),
    });
  }
  return segs;
}

// (Unused at the moment but kept in the file so a chat command like
// "color planes by airline" can be re-wired without re-deriving it.)
const AIRLINE_PALETTE = {
  UAL: [ 12, 109, 219, 235],   // United — UA blue
  AAL: [220,  62,  92, 235],   // American — AA red
  DAL: [184,  38,  64, 235],   // Delta — Delta widget red
  SWA: [255, 153,   0, 235],   // Southwest — orange
  ASA: [  0, 116, 197, 235],   // Alaska
  JBU: [ 56, 138, 203, 235],   // JetBlue
  NKS: [255, 199,  44, 235],   // Spirit — yellow
  FFT: [ 90, 188,  86, 235],   // Frontier — green
  HAL: [180,  60, 140, 235],   // Hawaiian
  SKW: [110, 175, 220, 235],   // SkyWest
  ENY: [200,  70,  86, 235],   // Envoy
  RPA: [ 96, 116, 188, 235],   // Republic
  EJA: [180, 180, 195, 235],   // NetJets
  ACA: [220,  35,  60, 235],   // Air Canada
  WJA: [  0, 110, 180, 235],   // WestJet
  BAW: [ 26,  54, 110, 235],   // British Airways
  DLH: [254, 213,   0, 235],   // Lufthansa
  AFR: [  0,  35, 102, 235],   // Air France
  KLM: [  0, 168, 226, 235],   // KLM
  IBE: [220, 100,  60, 235],   // Iberia
  RYR: [ 14,  61, 154, 235],   // Ryanair
  EZY: [255, 102,   0, 235],   // easyJet
  UAE: [200,  10,  62, 235],   // Emirates
  QTR: [110,  20,  56, 235],   // Qatar
  SIA: [ 19,  41, 110, 235],   // Singapore
  ANA: [  0,  86, 180, 235],   // ANA
  JAL: [200,  16,  46, 235],   // Japan Airlines
  // Cargo big-three deserve their own bucket; they show up everywhere.
  FDX: [255, 102,   0, 235],   // FedEx
  UPS: [101,  62,  16, 235],   // UPS — brown
  GTI: [102, 153, 102, 235],   // Atlas
};

// Convert a 3-letter ICAO airline prefix to a deterministic color when
// we don't have a curated palette entry. Uses a tiny string hash mapped
// onto the HSL colour wheel — saturated + high lightness so it pops on
// the dark basemap without clashing with the curated brand colours.
function paletteFromHash(prefix) {
  let h = 5381;
  for (let i = 0; i < prefix.length; i++) {
    h = ((h << 5) + h + prefix.charCodeAt(i)) >>> 0;
  }
  const hue = h % 360;
  // hsl(hue, 80%, 65%) → rgb. Inline the simple conversion.
  const s = 0.78;
  const l = 0.62;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = l - c / 2;
  let r1, g1, b1;
  if      (hue <  60) [r1, g1, b1] = [c, x, 0];
  else if (hue < 120) [r1, g1, b1] = [x, c, 0];
  else if (hue < 180) [r1, g1, b1] = [0, c, x];
  else if (hue < 240) [r1, g1, b1] = [0, x, c];
  else if (hue < 300) [r1, g1, b1] = [x, 0, c];
  else                [r1, g1, b1] = [c, 0, x];
  return [
    Math.round((r1 + m) * 255),
    Math.round((g1 + m) * 255),
    Math.round((b1 + m) * 255),
    230,
  ];
}

function airlineKey(flight) {
  // Callsigns are space-padded by OpenSky; first three alphabetic chars
  // are the ICAO airline code. Skip if it doesn't look like one.
  const cs = (flight?.callsign || '').trim().toUpperCase();
  if (cs.length < 4) return null;
  const prefix = cs.slice(0, 3);
  if (!/^[A-Z]{3}$/.test(prefix)) return null;
  return prefix;
}

// ── Aircraft colour schemes ──────────────────────────────────────────────
// Four named schemes, all converging on `planeColor(flight)`. The active
// scheme is `state.colorMode` and is changed via the radio group in the
// Layers tab. Adding another scheme is a matter of writing one more
// `planeColor*` function and registering it in `COLOR_SCHEMES` below.
//
// Convention shared by all schemes:
//   - `state.selectedFlightId` always wins (warm orange highlight).
//   - On-ground aircraft always render as light gray. Phase/altitude/
//     vrate are not meaningful while parked at a gate.
//   - Selected/ground are short-circuited at the dispatch level so
//     individual schemes only have to worry about the airborne case.

// 1. Phase of flight — takeoff yellow → cruise orange (depth = speed)
//    → landing red. The original FlightOps preset; keeps its name as
//    the default. The same logic the legend's swatches encode.
function planeColorPhase(flight) {
  const speed = Number(flight.vel_mps) || 0;
  const sT = smoothstep01(
    (speed - LEVEL_SPEED_LO_MPS) / (LEVEL_SPEED_HI_MPS - LEVEL_SPEED_LO_MPS)
  );
  const cruise = lerpRgba(ORANGE_SLOW, ORANGE_FAST, sT);
  const v = Number(flight.vrate_mps);
  if (!Number.isFinite(v)) return cruise;
  if (v >= 0) {
    const t = smoothstep01(v / VRATE_CLIMB_FULL_MPS);
    return lerpRgba(cruise, TAKEOFF_COLOR, t);
  }
  const t = smoothstep01(-v / -VRATE_DESCEND_FULL_MPS);
  return lerpRgba(cruise, LANDING_COLOR, t);
}

// 2. Altitude — light pastel green at low altitudes, deep saturated
//    green up at FL400+. Useful for spotting traffic stacks (e.g. who
//    is at FL340 vs who is descending through FL220).
const ALT_COLOR_LOW  = [220, 250, 225];   // ~SFC pale mint
const ALT_COLOR_MID  = [ 80, 220, 130];   // ~FL250 vibrant green
const ALT_COLOR_HIGH = [ 30, 160,  72];   // ≥FL450 deep green
const ALT_FT_TOP = 45000;
const ALT_FT_MID = 25000;

function planeColorAltitude(flight) {
  const altM = Number(flight.alt_m);
  if (!Number.isFinite(altM)) return [...ALT_COLOR_MID, 230];
  const ft = altM * 3.28084;
  if (ft <= ALT_FT_MID) {
    const t = smoothstep01(ft / ALT_FT_MID);
    return lerpRgba(ALT_COLOR_LOW, ALT_COLOR_MID, t, 235);
  }
  const t = smoothstep01((ft - ALT_FT_MID) / (ALT_FT_TOP - ALT_FT_MID));
  return lerpRgba(ALT_COLOR_MID, ALT_COLOR_HIGH, t, 235);
}

// 3. Vertical rate — diverging palette through purple. Strong descent
//    pulls toward deep violet, level traffic sits in pale lilac, strong
//    climb pushes toward bright magenta-purple. Useful for at-a-glance
//    "who is changing altitude" reads.
const VRATE_DESC_DARK  = [ 80,  40, 140];   // -3000 fpm: deep violet
const VRATE_NEUTRAL    = [225, 220, 235];   //   level: pale lilac
const VRATE_CLIMB_DARK = [210,  60, 230];   // +3000 fpm: vivid magenta
const VRATE_FULL_MPS = 15.24;  // ≈ ±3000 fpm

function planeColorVrate(flight) {
  const v = Number(flight.vrate_mps);
  if (!Number.isFinite(v)) return [...VRATE_NEUTRAL, 220];
  if (v >= 0) {
    const t = smoothstep01(v / VRATE_FULL_MPS);
    return lerpRgba(VRATE_NEUTRAL, VRATE_CLIMB_DARK, t, 235);
  }
  const t = smoothstep01(-v / VRATE_FULL_MPS);
  return lerpRgba(VRATE_NEUTRAL, VRATE_DESC_DARK, t, 235);
}

// 4. Emergency squawk — operations-focused. The three internationally
//    reserved squawks are highlighted in saturated red/amber, and
//    everything else is muted gray so the eye snaps to anything that
//    is *not* normal. 7500 = unlawful interference, 7600 = comms
//    failure, 7700 = general emergency.
const SQK_NORMAL = [150, 165, 185, 180];
const SQK_7500   = [255,  70,  90, 255];
const SQK_7600   = [255, 170,  60, 255];
const SQK_7700   = [255,  60,  60, 255];

function planeColorSquawk(flight) {
  const code = String(flight.squawk || '').padStart(4, '0');
  if (code === '7500') return SQK_7500;
  if (code === '7600') return SQK_7600;
  if (code === '7700') return SQK_7700;
  return SQK_NORMAL;
}

const COLOR_SCHEMES = {
  phase:    planeColorPhase,
  altitude: planeColorAltitude,
  vrate:    planeColorVrate,
  squawk:   planeColorSquawk,
};

// ── Categorical bucket classification (drives the chip filter) ──────────
// Each categorical color mode (phase, squawk) partitions every flight
// into one of a fixed set of buckets. The chip legend lets the user
// toggle individual buckets on/off; flights whose bucket is "off" are
// removed from the IconLayer's data array, but the underlying state
// vector still ticks in `state.flights` so re-arming the chip restores
// the plane instantly without a refetch.
//
// IMPORTANT: keep the bucket ids here in lockstep with the
// `data-bucket="…"` attributes on the chip buttons in index.html, and
// with `state.flightFilter.{phase,squawk}` defaults at the top of this
// file — the chip "armed" state is read off the DOM at startup so a
// missing bucket id silently disappears from the filter set.

const PHASE_BUCKETS  = ['climb', 'level-slow', 'level-fast', 'descend', 'ground'];
const SQUAWK_BUCKETS = ['7500', '7600', '7700', 'normal', 'ground'];

// Vertical-rate threshold for "actively climbing/descending" vs "level".
// 1.5 m/s ≈ ±300 fpm — IFR-cruise tolerance; anything inside that band
// is treated as cruise even if the raw value reads a hair off zero.
const PHASE_LEVEL_VRATE_BAND_MPS = 1.5;
// Speed midpoint between the cruise-slow → cruise-fast color crossfade.
// Used here to decide which of the two cruise buckets a level flight
// belongs to. Pulled from LEVEL_SPEED_LO/HI rather than hardcoded so the
// chip filter stays consistent with the color gradient if those constants
// are ever retuned.
const PHASE_CRUISE_SPEED_MID_MPS =
  (LEVEL_SPEED_LO_MPS + LEVEL_SPEED_HI_MPS) / 2;

function flightPhaseBucket(f) {
  if (f.on_ground) return 'ground';
  const v = Number(f.vrate_mps);
  if (Number.isFinite(v)) {
    if (v >=  PHASE_LEVEL_VRATE_BAND_MPS) return 'climb';
    if (v <= -PHASE_LEVEL_VRATE_BAND_MPS) return 'descend';
  }
  const s = Number(f.vel_mps) || 0;
  return s >= PHASE_CRUISE_SPEED_MID_MPS ? 'level-fast' : 'level-slow';
}

function flightSquawkBucket(f) {
  // Ground gets its own bucket regardless of squawk so users can hide
  // parked traffic without losing the emergency-squawk highlights.
  if (f.on_ground) return 'ground';
  const code = String(f.squawk || '').padStart(4, '0');
  if (code === '7500' || code === '7600' || code === '7700') return code;
  return 'normal';
}

// Should this flight be rendered, given the active filter? The selected
// flight is always exempt — operator clicked it explicitly, hiding it
// underneath them would be confusing — and color modes without buckets
// (altitude, vrate) pass through unconditionally.
function flightPassesFilter(f) {
  if (f.id === state.selectedFlightId) return true;
  const mode = state.colorMode;
  if (mode === 'phase') {
    return state.flightFilter.phase.has(flightPhaseBucket(f));
  }
  if (mode === 'squawk') {
    return state.flightFilter.squawk.has(flightSquawkBucket(f));
  }
  return true;
}

function planeColor(flight) {
  // Selected aircraft always wins — operator picked this one, keep it warm.
  if (flight.id === state.selectedFlightId) return [255, 184, 107, 255];
  // On the ground: light gray. Altitude/phase/vrate aren't meaningful
  // for parked traffic, so every scheme defers to the same swatch.
  if (flight.on_ground) return GROUND_COLOR;
  const fn = COLOR_SCHEMES[state.colorMode] || planeColorPhase;
  return fn(flight);
}

// Plane size in pixels grows with zoom. At country view (z<=4) we want
// small icons (~12 px) so the chart isn't a sea of arrows; at airport
// zoom (z>=10) we want big icons (~32 px) that read as aircraft.
function planeSize(flight) {
  const z = state.zoom || 3;
  // Linear ramp z=4→14, z=11→30, clamped on both ends.
  const base = Math.min(34, Math.max(13, 4 + 2.4 * (z - 2)));
  return flight.id === state.selectedFlightId ? base + 8 : base;
}

// ── Layer assembly ───────────────────────────────────────────────────────

function buildLayers() {
  const layers = [];
  const flights = Array.from(state.flights.values());
  const zoom = state.zoom;

  // pulsePhase rides 0..1 each ~1.4 s; we want a slow, calm throb on
  // hazardous-airspace fills (not strobing).
  const pulse = state.pulsePhase;

  // ── Airspace polygons (fill-only — no outlines for a modern look) ──────
  // Class B/C/D fills are gentle and steady — they're a reference, not a
  // warning. SUA fills throb subtly so prohibited zones catch the eye.
  // 3D extrusion is gated on a separate state flag and re-uses the
  // ceiling-from-properties helper. We disable extrusion shading via
  // `material: false` so the side faces inherit the same translucent
  // colour as the top — otherwise deck.gl tints them with a default
  // light source and the extruded box reads as opaque grey from the
  // side.
  const extrude3D = state.airspace3D;
  const extrudeProps = extrude3D
    ? { extruded: true, getElevation: airspaceElevationFor, material: false, wireframe: false }
    : { extruded: false };
  // When the user pulls a strong vertical exaggeration we drop the layer
  // alpha a bit so the side walls stay readable through each other.
  const extrudeOpacityScale = extrude3D ? 0.78 : 1.0;

  if (state.layerVisibility.classes && state.airspace.classes) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-classes',
        data: state.airspace.classes,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: classFillFor,
        opacity: 1.0 * extrudeOpacityScale,
        ...extrudeProps,
        updateTriggers: {
          getElevation: [state.airspace3D, state.airspaceVScale],
        },
      })
    );
  }

  if (state.layerVisibility.sua && state.airspace.sua) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-sua',
        data: state.airspace.sua,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: suaFillFor,
        opacity: 0.9 * extrudeOpacityScale,
        ...extrudeProps,
        updateTriggers: {
          getElevation: [state.airspace3D, state.airspaceVScale],
        },
      })
    );
  }

  // TFRs throb harder — they're transient, dangerous, and the user wants to
  // see them as alive on the chart. Layer-level opacity is cheap to animate
  // (no per-feature accessor recompute, no GPU buffer churn).
  if (state.layerVisibility.tfrs && state.airspace.tfrs) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-tfrs',
        data: state.airspace.tfrs,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: TFR_FILL,
        opacity: (0.55 + 0.45 * pulse) * extrudeOpacityScale,
        ...extrudeProps,
        updateTriggers: {
          getElevation: [state.airspace3D, state.airspaceVScale],
        },
      })
    );
  }

  // ── ATS routes — neon green airways, drawn as a thin soft halo + a
  // slim dashed core. Both layers are static (no per-frame opacity or
  // dash-offset updates) — the previous animated TripsLayer version
  // ran a full GPU re-prop every tick across the whole route set and
  // tanked frame rate. The halo is now narrow enough that even at
  // its static alpha it reads as a glow, not a fat green band.
  if (
    state.layerVisibility.ats &&
    state.airspace.bbox.ats &&
    zoom >= ATS_MIN_ZOOM
  ) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-ats-halo',
        data: state.airspace.bbox.ats,
        stroked: true,
        filled: false,
        pickable: false,
        getLineColor: ATS_HALO_COLOR,
        getLineWidth: 2.4,                  // slimmer than the old 5
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 1.5,
        lineWidthMaxPixels: 4,
        opacity: 0.4,                       // static (was a pulse)
      })
    );
    // Dashed core, only if PathStyleExtension loaded. Falls back to a
    // solid bright line otherwise — still readable, just less fancy.
    const dashExt = PathStyleExtension
      ? [new PathStyleExtension({ dash: true, highPrecisionDash: true })]
      : [];
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-ats',
        data: state.airspace.bbox.ats,
        stroked: true,
        filled: false,
        pickable: true,
        getLineColor: ATS_CORE_COLOR,
        getLineWidth: 1.2,                  // slimmer than the old 1.4
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 1,
        lineWidthMaxPixels: 2,
        getDashArray: [6, 4],
        dashJustified: true,
        dashGapPickable: true,
        extensions: dashExt,
      })
    );
  }

  // ── Runways + taxiways — operational-status coloured fills ─────────────
  // Order matters: runways below taxiways so taxiway lines don't get hidden
  // when paved areas overlap.
  if (state.layerVisibility.runways && state.airspace.runways && zoom >= RUNWAY_MIN_ZOOM) {
    layers.push(
      new GeoJsonLayer({
        id: 'airfield-runways',
        data: state.airspace.runways,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: rwyFillFor,
      })
    );
  }
  if (state.layerVisibility.taxiways && state.airspace.bbox.taxiways && zoom >= TAXIWAY_MIN_ZOOM) {
    layers.push(
      new GeoJsonLayer({
        id: 'airfield-taxiways',
        data: state.airspace.bbox.taxiways,
        stroked: false,
        filled: true,
        pickable: true,
        getFillColor: twyFillFor,
      })
    );
  }

  // ── Obstacles — point layer, color-ramped on AGL height ────────────────
  if (state.layerVisibility.obstacles && state.airspace.bbox.obstacles && zoom >= OBSTACLE_MIN_ZOOM) {
    layers.push(
      new ScatterplotLayer({
        id: 'airfield-obstacles',
        data: state.airspace.bbox.obstacles.features || [],
        getPosition: (f) => f.geometry?.coordinates || [0, 0],
        // Bigger dot for taller obstacles, but everything stays small —
        // the colour does the heavy lifting, the radius is just a hint.
        getRadius: (f) => {
          const agl = Number(f.properties?.AGL) || 0;
          if (agl >= 1000) return 80;
          if (agl >=  500) return 55;
          if (agl >=  300) return 40;
          return 28;
        },
        radiusUnits: 'meters',
        radiusMinPixels: 3,
        radiusMaxPixels: 9,
        getFillColor: obstacleColor,
        stroked: false,
        pickable: true,
      })
    );
  }

  // ── ARTCC boundaries — outline-only polygons + labelled centre IDs ───
  // Layered *under* the airports/flights so the boundary lines are
  // never on top of the operational data. We render the outline as a
  // GeoJsonLayer (stroked, unfilled) plus a TextLayer that labels each
  // polygon at its computed centroid with the 3-letter ARTCC ident
  // (ZID, ZNY, ZAB…). Centroids are computed once per featurecollection
  // and cached on the FC itself so we don't recompute every frame.
  if (state.layerVisibility.artcc && state.airspace.artcc) {
    layers.push(
      new GeoJsonLayer({
        id: 'airspace-artcc',
        data: state.airspace.artcc,
        stroked: true,
        filled: false,
        pickable: true,
        getLineColor: [120, 200, 255, 175],
        getLineWidth: 1.6,
        lineWidthUnits: 'pixels',
        lineWidthMinPixels: 1,
        lineWidthMaxPixels: 2.5,
      })
    );
    const labels = ensureArtccLabels(state.airspace.artcc);
    if (labels && labels.length) {
      layers.push(
        new TextLayer({
          id: 'airspace-artcc-labels',
          data: labels,
          getPosition: (d) => d.position,
          getText: (d) => d.text,
          getSize: 13,
          sizeUnits: 'pixels',
          getColor: [200, 230, 255, 235],
          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          fontWeight: 700,
          outlineWidth: 3,
          outlineColor: [4, 8, 18, 230],
          billboard: true,
        })
      );
    }
  }

  // ── NAVAIDs (VOR / VORTAC / DME / TACAN / NDB / ILS components) ──
  // Drawn as a small dot ringed by a brighter halo. VOR family gets
  // a yellow tint (matching the chart symbology pilots already know);
  // every other class is muted blue so the ground-based aids don't
  // compete with the airports for attention.
  if (
    state.layerVisibility.navaids &&
    state.airspace.bbox.navaids &&
    zoom >= NAVAID_MIN_ZOOM
  ) {
    layers.push(
      new ScatterplotLayer({
        id: 'airspace-navaids',
        data: state.airspace.bbox.navaids.features || [],
        getPosition: (f) => f.geometry?.coordinates || [0, 0],
        getRadius: () => 700,
        radiusUnits: 'meters',
        radiusMinPixels: 3,
        radiusMaxPixels: 7,
        getFillColor: navaidColor,
        getLineColor: [255, 255, 255, 180],
        lineWidthMinPixels: 0.5,
        stroked: true,
        pickable: true,
      })
    );
    if (zoom >= NAVAID_MIN_ZOOM + 1) {
      layers.push(
        new TextLayer({
          id: 'airspace-navaids-labels',
          data: state.airspace.bbox.navaids.features || [],
          getPosition: (f) => f.geometry?.coordinates || [0, 0],
          getText: (f) => f.properties?.IDENT || '',
          getSize: 10,
          sizeUnits: 'pixels',
          getColor: [255, 240, 200, 200],
          getPixelOffset: [0, -12],
          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          fontWeight: 700,
          outlineWidth: 2,
          outlineColor: [4, 8, 18, 220],
          billboard: true,
        })
      );
    }
  }

  // ── METAR observations — sleek "weather instrument" composite ──────
  // Each station is rendered as:
  //   (a) a wind vane arrow (IconLayer) rotated to point downwind and
  //       sized by sustained wind speed (suppressed when calm),
  //   (b) a body ring (ScatterplotLayer) whose colour reflects the
  //       active METAR colour mode — flight category, wind speed,
  //       temperature, or visibility.
  // Refetched per-bbox on moveend (see refreshBboxMetar). Hover popup
  // shows the raw METAR. Picking is on the body ring; the arrow is
  // pickable too so stiff-wind stations are easy to hit.
  if (
    state.layerVisibility.metar &&
    state.metar.bbox &&
    state.metar.bbox.stations?.length &&
    zoom >= METAR_MIN_ZOOM
  ) {
    const stations = state.metar.bbox.stations;
    const windy = stations.filter(metarHasWind);
    if (windy.length) {
      layers.push(
        new IconLayer({
          id: 'metar-wind-arrows',
          data: windy,
          // Per-feature getIcon returning a single shared definition is
          // the most reliable path for data URIs — same approach as the
          // plane IconLayer above.
          getIcon: () => WIND_ICON_DEF,
          getPosition: (m) => [m.lon, m.lat],
          getSize: metarArrowSize,
          sizeUnits: 'pixels',
          // Wind direction in METAR is the bearing the wind comes from
          // (compass clockwise). The arrow should point in the *opposite*
          // direction (where the wind is going). deck.gl rotates counter-
          // clockwise, so we negate the resulting bearing.
          getAngle: (m) => -((m.wind_dir + 180) % 360),
          getColor: metarArrowColor,
          billboard: true,
          pickable: true,
          updateTriggers: {
            getColor: [state.metar.bboxKey, state.metarColorMode],
          },
        })
      );
    }
    layers.push(
      new ScatterplotLayer({
        id: 'metar-stations',
        data: stations,
        getPosition: (m) => [m.lon, m.lat],
        // Constant pixel radius. Previously this was a 4.5km meter-based
        // disc capped at 9px max — fine zoomed out, but the disc grew
        // to fill its cap zoomed in and dominated the airport-detail
        // view. With pixel units the dot stays a small anchor at every
        // zoom and the wind arrow does the visual work.
        getRadius: () => 4.5,
        radiusUnits: 'pixels',
        getFillColor: (m) => {
          const c = metarColor(m);
          // Body fill is slightly translucent so the underlying base map
          // (and the arrow's halo, where they overlap) remains legible.
          return [c[0], c[1], c[2], Math.round((c[3] ?? 220) * 0.78)];
        },
        getLineColor: (m) => {
          const c = metarColor(m);
          return [
            Math.min(255, c[0] + 30),
            Math.min(255, c[1] + 30),
            Math.min(255, c[2] + 30),
            255,
          ];
        },
        stroked: true,
        lineWidthMinPixels: 1.4,
        lineWidthMaxPixels: 2,
        pickable: true,
        updateTriggers: {
          getFillColor: [state.metar.bboxKey, state.metarColorMode],
          getLineColor: [state.metar.bboxKey, state.metarColorMode],
        },
      })
    );
  }

  // ── NAS status — small anchor dot at the airport ──────────────────
  // The actual badge (rounded pill with frosted backdrop and a colour-
  // tinted glow) is a DOM overlay updated by renderNasOverlay() — see
  // below. Keeping the anchor dot in deck.gl is cheap and preserves a
  // visual link from pill to airport when the user pans.
  if (state.layerVisibility.nas && state.nas.events) {
    const positioned = state.nas.events.filter(
      (e) => Number.isFinite(e.lat) && Number.isFinite(e.lon)
    );
    if (positioned.length) {
      layers.push(
        new ScatterplotLayer({
          id: 'nas-status-anchor',
          data: positioned,
          getPosition: (e) => [e.lon, e.lat],
          getRadius: 1,
          radiusUnits: 'pixels',
          radiusMinPixels: 3,
          radiusMaxPixels: 4,
          getFillColor: (e) => {
            const c = nasFillFor(e);
            return [c[0], c[1], c[2], 220];
          },
          stroked: true,
          getLineColor: [255, 255, 255, 230],
          lineWidthMinPixels: 1.2,
          pickable: false,
          updateTriggers: { getFillColor: state.nas.fetchedAt },
        })
      );
    }
  }

  if (state.layerVisibility.airports && state.airportsInView.length) {
    // Per-type pixel size + alpha so a CONUS-wide view reads as
    // "hubs first, regionals second, GA strips as a faint background"
    // rather than a uniform polka-dot field.
    const radiusPxByType = { large_airport: 5, medium_airport: 3.5, small_airport: 2 };
    const alphaByType    = { large_airport: 220, medium_airport: 180, small_airport: 130 };
    layers.push(
      new ScatterplotLayer({
        id: 'airports',
        data: state.airportsInView,
        getPosition: (a) => [a.lon, a.lat],
        getRadius: (a) => radiusPxByType[a.type] ?? 2,
        radiusUnits: 'pixels',
        radiusMinPixels: 1.5,
        radiusMaxPixels: 7,
        getFillColor: (a) => {
          if (state.selectedAirport && a.code === state.selectedAirport.code) {
            return [255, 184, 107, 235];
          }
          return [94, 226, 255, alphaByType[a.type] ?? 160];
        },
        getLineColor: [255, 255, 255, 80],
        lineWidthMinPixels: 0.5,
        stroked: true,
        pickable: true,
        updateTriggers: { getFillColor: state.selectedAirport?.code },
      })
    );
    // Labels: keep the canvas readable. Always label large airports.
    // At zoom ≥7 also label medium ones (regional hubs visible but not
    // crowded). Small_airport stays unlabelled — clicking on the dot
    // still shows the popup.
    const z = state.map.getZoom();
    const labelData = state.airportsInView.filter((a) => {
      if (a.type === 'large_airport') return true;
      if (a.type === 'medium_airport' && z >= 7) return true;
      return false;
    });
    if (labelData.length) {
      layers.push(
        new TextLayer({
          id: 'airport-labels',
          data: labelData,
          getPosition: (a) => [a.lon, a.lat],
          getText: (a) => a.code,
          getSize: (a) => (a.type === 'large_airport' ? 12 : 10),
          getColor: (a) =>
            a.type === 'large_airport' ? [200, 230, 250, 220] : [170, 200, 220, 170],
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

  // Selected-aircraft historical track — cyan→deep-blue gradient. One
  // path-segment per pair of waypoints, so PathLayer's per-feature
  // `getColor` produces a vertex-style gradient. This layer sits above
  // the generic `flight-paths` breadcrumbs so the gradient visually wins
  // for the highlighted aircraft.
  //
  // Source-of-truth is OpenSky's /tracks/all (richer, goes back ~30
  // minutes), populated into state.selectedTrack by
  // fetchSelectedFlightDetails. When that call fails (OpenSky's tracks
  // endpoint is aggressively rate-limited from cloud egress IPs and
  // routinely 403s anonymously), we fall back to the locally
  // accumulated state.flightHistory[icao24] — every state-vector poll
  // appends a fix, so we always have *some* trail to draw, and it
  // auto-extends each refresh as new fixes arrive.
  if (state.selectedFlightId) {
    let trackSegs = state.selectedTrack;
    if (!trackSegs || trackSegs.length < 1) {
      const hist = state.flightHistory.get(state.selectedFlightId);
      if (hist && hist.length >= 2) {
        trackSegs = buildSelectedTrackSegments(
          hist.map((p) => ({
            lat: p.lat,
            lon: p.lon,
            alt_ft: (p.alt_m || 0) * FT_PER_M,
          }))
        );
      }
    }
    // Extend the trail's tail to the icon's live, dead-reckoned
    // position so the cyan stroke is always glued to the plane's
    // nose. Without this the trail ends at the last received state-
    // vector fix while the icon has already moved forward via
    // interpolation, producing the "the plane isn't at the end of
    // the cyan line" effect at high zoom — and the inverse "the
    // line keeps going past the plane" right after a new fix lands
    // before the next animate tick rebuilds. We synthesise one extra
    // segment per frame at the brightest gradient stop so it visually
    // continues the colour ramp.
    const selFlight = state.flights.get(state.selectedFlightId);
    if (selFlight && trackSegs && trackSegs.length >= 1) {
      const live = renderPos(selFlight, performance.now());
      const lastSeg = trackSegs[trackSegs.length - 1];
      // Skip the connector when the live position is already on top
      // of the last waypoint (within ~5 m at the equator) — adding
      // a zero-length segment makes deck.gl draw a tiny round cap
      // dot that flickers as the plane catches up.
      const dLon = live[0] - lastSeg.b.lon;
      const dLat = live[1] - lastSeg.b.lat;
      if (dLon * dLon + dLat * dLat > 1e-9) {
        const liveAltM = Number.isFinite(selFlight.alt_m) ? selFlight.alt_m : (lastSeg.b.alt_m || 0);
        trackSegs = trackSegs.concat([{
          a: { lon: lastSeg.b.lon, lat: lastSeg.b.lat, alt_m: lastSeg.b.alt_m },
          b: { lon: live[0],       lat: live[1],       alt_m: liveAltM },
          color: trackGradient(1.0),
        }]);
      }
    }
    if (trackSegs && trackSegs.length >= 1) {
      layers.push(
        new PathLayer({
          id: 'selected-flight-track',
          data: trackSegs,
          getPath: (d) => {
            if (state.airspace3D) {
              const s = state.airspaceVScale;
              return [
                [d.a.lon, d.a.lat, d.a.alt_m * s],
                [d.b.lon, d.b.lat, d.b.alt_m * s],
              ];
            }
            return [[d.a.lon, d.a.lat], [d.b.lon, d.b.lat]];
          },
          getColor: (d) => d.color,
          getWidth: 2.6,
          widthUnits: 'pixels',
          widthMinPixels: 1.5,
          widthMaxPixels: 4,
          jointRounded: true,
          capRounded: true,
          pickable: false,
          updateTriggers: {
            // Rebuild the geometry buffer when the source flips between
            // upstream-OpenSky and local-fallback (state.selectedTrack
            // arriving fills the path with denser waypoints), or when
            // the user toggles 3D mode / vscale.
            getPath: [
              state.airspace3D,
              state.airspaceVScale,
              state.selectedTrack ? 'upstream' : 'local',
            ],
          },
        })
      );
    }
  }

  // Breadcrumb paths for every visible flight when the user zooms in.
  // Uses the actual fix history (no interpolation jitter), so the
  // polylines stay clean while the planes themselves glide smoothly.
  if (state.layerVisibility.paths && zoom >= TRAIL_ZOOM_THRESHOLD) {
    const pathData = [];
    for (const [id, history] of state.flightHistory) {
      if (history.length < 2) continue;
      // Lift each breadcrumb to the altitude it was logged at when 3D
      // airspace mode is on, so the trail floats with the plane instead
      // of dragging on the ground. In 2D mode we drop Z and stay flat.
      const lift = state.airspace3D;
      pathData.push({
        id,
        path: history.map((p) =>
          lift ? [p.lon, p.lat, (p.alt_m || 0) * state.airspaceVScale] : [p.lon, p.lat]
        ),
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
          updateTriggers: {
            getColor: state.selectedFlightId,
            getWidth: state.selectedFlightId,
            getPath: [state.airspace3D, state.airspaceVScale],
          },
        })
      );
    }
  }

  if (state.layerVisibility.trails && state.selectedFlightId) {
    const history = state.flightHistory.get(state.selectedFlightId) || [];
    if (history.length >= 2) {
      const lift = state.airspace3D;
      const tripPath = history.map((p) =>
        lift ? [p.lon, p.lat, (p.alt_m || 0) * state.airspaceVScale] : [p.lon, p.lat]
      );
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

  // Apply the chip-legend bucket filter for the current color mode.
  // For non-categorical modes (altitude, vrate) flightPassesFilter()
  // returns true unconditionally so this is a no-op. We compute it
  // once per buildLayers() pass rather than per accessor call so deck.gl
  // can size its attribute buffers exactly.
  const visibleFlights = flights.filter(flightPassesFilter);
  // Bucket-filter signature for updateTriggers. Sets are reference-stable
  // unless wireFlightFilters() rebuilds them on chip-toggle, so a fresh
  // signature string is the cheapest way to make deck.gl notice the
  // change without thrashing on every animation frame.
  const filterSig =
    state.colorMode === 'phase'
      ? `phase:${[...state.flightFilter.phase].sort().join(',')}`
      : state.colorMode === 'squawk'
      ? `squawk:${[...state.flightFilter.squawk].sort().join(',')}`
      : 'none';

  if (state.layerVisibility.flights && visibleFlights.length) {
    // Lay the icon flat on the world plane (instead of billboarding to
    // face the camera) any time the camera is tilted enough that a
    // billboarded heading would look "pointed downwards". This is a
    // hard switch — when toggled, deck.gl recompiles the layer's
    // vertex shader pipeline, so we only swap on `pitchend` (handled
    // by the map listener).
    const planeFlat = state.airspace3D || state.pitch > 25;
    layers.push(
      new IconLayer({
        id: 'flights',
        data: visibleFlights,
        // Per-feature getIcon is the most reliable path for data URIs in
        // deck.gl — it avoids the iconAtlas autopack step entirely. The
        // returned object identity is stable (PLANE_ICON_DEF is a const)
        // so deck.gl reuses the cached atlas across frame rebuilds.
        getIcon: () => PLANE_ICON_DEF,
        // Position is computed every frame from the per-flight anchor +
        // a decaying correction offset, so the icon traces the plane's
        // *real* heading at its *real* ground speed between fixes. The
        // optional Z component lifts the icon to the plane's reported
        // altitude when 3D airspace mode is on — otherwise it stays
        // glued to the basemap (z=0) for the classic flat view.
        getPosition: (f) => {
          const p = renderPos(f, performance.now());
          return [p[0], p[1], flightRenderAltMetres(f)];
        },
        getSize: planeSize,
        // Plane SVG points UP (0° = north). OpenSky heading is degrees CW
        // from north. deck.gl getAngle is CCW degrees, so we negate.
        // renderHeading eases shortest-arc from the old anchor's heading
        // to the new one over CORRECTION_DECAY_S, matching the position
        // glide so the icon's orientation and track stay in sync during
        // a course adjustment instead of snapping to the new heading.
        getAngle: (f) => -renderHeading(f, performance.now()),
        getColor: planeColor,
        sizeUnits: 'pixels',
        sizeMinPixels: 10,
        sizeMaxPixels: 44,
        billboard: !planeFlat,
        pickable: true,
        updateTriggers: {
          // Re-evaluate size + colour when zoom changes (smaller far out,
          // bigger zoomed in) and when selection changes (selected plane
          // gets the warm orange highlight). state.lastFetchedAt forces a
          // colour recompute on every fresh OpenSky pull so phase-of-flight
          // (climb/level/descend) and the speed gradient track new fixes —
          // without it deck.gl 9 caches the color attribute buffer between
          // frames even though the data array reference changes.
          getSize: [state.zoom, state.selectedFlightId],
          getColor: [state.selectedFlightId, state.lastFetchedAt, state.colorMode, filterSig],
          // Re-run the position accessor when 3D mode or vertical scale
          // toggle so the Z component snaps to the new value instantly
          // (without waiting for the next animate-loop refreshLayers).
          getPosition: [state.airspace3D, state.airspaceVScale],
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

function animate(nowTs) {
  if (nowTs - _lastRenderTs >= RENDER_INTERVAL_MS) {
    // 0..1 sine wave. Period chosen to feel ambient (~1.4 s) — fast enough
    // that the chart never looks frozen, slow enough that it doesn't strobe.
    state.pulsePhase = 0.5 + 0.5 * Math.sin(nowTs / 700);
    // Fast phase used by hard-severity NAS strobes (~1.6 Hz). We hand
    // the radian value through; the layer takes |sin| so the dim phase
    // is brief and the bright phase lingers.
    state.pulsePhaseFast = nowTs / 100;
    const lv = state.layerVisibility;
    const needRefresh =
      (lv.flights && state.flights.size > 0) ||
      (lv.trails && state.selectedFlightId) ||
      // Only TFRs animate now — SUA fills and ATS routes are static
      // (animating them across hundreds of features per frame was the
      // perf hit). TFRs are usually 0–10 features so their throb is
      // cheap to keep.
      (lv.tfrs && state.airspace.tfrs) ||
      // NAS Status airport dots throb while the layer is on so
      // ground stops/closures pop on the chart.
      (lv.nas && state.nas.events?.length);
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
  } else if (lid === 'metar-stations' || lid === 'metar-wind-arrows') {
    selectMetar(info.object);
  } else if (lid === 'nas-status' || lid === 'nas-status-hard') {
    selectNas(info.object);
  } else if (lid.startsWith('airspace-') || lid.startsWith('airfield-')) {
    // The ATS-route halo is unpickable in normal use, but if anyone ever
    // makes it pickable, route the click through to the same handler.
    const routeId = lid === 'airspace-ats-halo' ? 'airspace-ats' : lid;
    selectAirspace(info.object, routeId);
  }
}

// Hover handler — only the lightweight overlays (METAR, NAS, ARTCC,
// NAVAIDs) get a popup; clicks on these still open the side drawer
// with the full detail view. Aircraft and airports already have
// drawers and the popup would just compete with them.
function handleDeckHover(info) {
  if (!info || !info.object || info.x == null) {
    hidePopup();
    return;
  }
  const lid = info.layer?.id || '';
  let html = null;
  if (lid === 'metar-stations' || lid === 'metar-wind-arrows') html = metarPopupHtml(info.object);
  else if (lid === 'nas-status' || lid === 'nas-status-hard') html = nasPopupHtml(info.object);
  else if (lid === 'airspace-artcc' || lid === 'airspace-artcc-labels') html = artccPopupHtml(info.object);
  else if (lid === 'airspace-navaids' || lid === 'airspace-navaids-labels') html = navaidPopupHtml(info.object);
  if (html) showPopup(html, info.x, info.y);
  else hidePopup();
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function metarPopupHtml(m) {
  const cat = (m.flt_cat || '').toLowerCase();
  const station = m.station || '—';
  const name = m.name ? `<div class="row"><span class="k">Station</span><span>${escapeHtml(m.name)}</span></div>` : '';
  const wind = m.wind_kt != null
    ? `<div class="row"><span class="k">Wind</span><span>${m.wind_dir != null ? Math.round(m.wind_dir) + '°' : 'VRB'} @ ${Math.round(m.wind_kt)} kt${m.wind_gust_kt ? ` (G ${Math.round(m.wind_gust_kt)})` : ''}</span></div>`
    : '';
  const visib = m.visib_sm != null ? `<div class="row"><span class="k">Visibility</span><span>${escapeHtml(m.visib_sm)} SM</span></div>` : '';
  const temp = m.temp_c != null ? `<div class="row"><span class="k">Temp / Dew</span><span>${Math.round(m.temp_c)}°C / ${m.dewp_c != null ? Math.round(m.dewp_c) + '°C' : '—'}</span></div>` : '';
  const altim = m.altim_hpa != null ? `<div class="row"><span class="k">Altimeter</span><span>${(m.altim_hpa / 33.8639).toFixed(2)} inHg</span></div>` : '';
  const raw = m.raw ? `<div class="pop-raw">${escapeHtml(m.raw)}</div>` : '';
  return `
    <div class="pop-title">${escapeHtml(station)}<span class="pop-cat ${cat}">${escapeHtml(m.flt_cat || '—')}</span></div>
    <div class="pop-rows">${name}${wind}${visib}${temp}${altim}</div>
    ${raw}
  `;
}

function nasPopupHtml(e) {
  const headline = `${escapeHtml(e.airport)} · ${escapeHtml(e.severity.replace('_', ' '))}`;
  const ev = (e.events || []).slice(0, 3).map((evt) => {
    const cls = ['ground_stop', 'closure'].includes(evt.kind) ? 'closure'
              : (evt.kind || '').includes('delay') ? 'delay' : 'advisory';
    const reason = evt.reason ? `<div>${escapeHtml(evt.reason)}</div>` : '';
    const end = evt.end_time ? `<div class="k">until ${escapeHtml(evt.end_time)}</div>` : '';
    let extra = '';
    if (evt.avg_delay_min != null || evt.max_delay_min != null) {
      extra = `<div>avg ${escapeHtml(evt.avg_delay_min ?? '—')} / max ${escapeHtml(evt.max_delay_min ?? '—')} min</div>`;
    }
    return `<div class="pop-event ${cls}"><span class="kind">${escapeHtml((evt.kind || '').replace('_', ' '))}</span>${reason}${extra}${end}</div>`;
  }).join('');
  const name = e.name ? `<div style="margin-top:2px;color:var(--text-faint);">${escapeHtml(e.name)}</div>` : '';
  return `<div class="pop-title">${headline}</div>${name}${ev}`;
}

function artccPopupHtml(feature) {
  const p = feature.properties || feature || {};
  return `
    <div class="pop-title">${escapeHtml(p.IDENT || 'ARTCC')}</div>
    <div class="pop-rows">
      <div class="row"><span class="k">Center</span><span>${escapeHtml(p.NAME || '—')}</span></div>
      <div class="row"><span class="k">Type</span><span>${escapeHtml(p.LOCAL_TYPE || p.TYPE_CODE || '—')}</span></div>
    </div>
  `;
}

function navaidPopupHtml(feature) {
  const p = feature.properties || feature || {};
  return `
    <div class="pop-title">${escapeHtml(p.IDENT || '—')} · ${escapeHtml(p.CLASS_TXT || '')}</div>
    <div class="pop-rows">
      <div class="row"><span class="k">Name</span><span>${escapeHtml(p.NAME_TXT || '—')}</span></div>
      <div class="row"><span class="k">Channel</span><span>${escapeHtml(p.CHANNEL || '—')}</span></div>
      <div class="row"><span class="k">Status</span><span>${escapeHtml(p.STATUS || '—')}</span></div>
      <div class="row"><span class="k">Location</span><span>${escapeHtml([p.CITY, p.STATE].filter(Boolean).join(', ') || '—')}</span></div>
    </div>
  `;
}

// FAA coded values translated into human strings so the drawer reads
// like a chart legend instead of raw integers.
const RWY_OPER_LABEL = {
  '1': 'closed indefinitely', '2': 'open', '3': 'under construction',
  '4': 'repurposed as taxiway', '5': 'unknown', '7': 'closed',
};
const TWY_OPER_LABEL = { '2': 'open', '5': 'unknown', '7': 'closed' };
const SURFACE_LABEL = {
  '1': 'hard / paved', '2': 'metal', '5': 'other than hard surface',
};

function selectMetar(m) {
  state.selectedFlightId = null;
  state.selectedAirport = null;
  const grid = [
    ['Station', m.station || '—'],
    ['Name', m.name || '—'],
    ['Category', m.flt_cat || '—'],
    ['Observed', m.obs_time ? new Date(m.obs_time * 1000).toUTCString() : '—'],
    ['Temp / Dew', m.temp_c != null ? `${Math.round(m.temp_c)}°C / ${m.dewp_c != null ? Math.round(m.dewp_c) + '°C' : '—'}` : '—'],
    ['Wind', m.wind_kt != null ? `${m.wind_dir != null ? Math.round(m.wind_dir) + '°' : 'VRB'} @ ${Math.round(m.wind_kt)} kt${m.wind_gust_kt ? ` (G ${Math.round(m.wind_gust_kt)})` : ''}` : '—'],
    ['Visibility', m.visib_sm != null ? `${m.visib_sm} SM` : '—'],
    ['Altimeter', m.altim_hpa != null ? `${(m.altim_hpa / 33.8639).toFixed(2)} inHg` : '—'],
    ['Raw', m.raw ? `<code style="font-size:11px;">${escapeHtml(m.raw)}</code>` : '—'],
  ];
  openDrawer({
    eyebrow: 'METAR',
    title: m.station || 'METAR',
    grid,
  });
  refreshLayers();
}

function selectNas(e) {
  state.selectedFlightId = null;
  state.selectedAirport = null;
  const grid = [
    ['Airport', e.airport || '—'],
    ['Name', e.name || '—'],
    ['Severity', (e.severity || 'info').replace('_', ' ')],
  ];
  for (const evt of (e.events || []).slice(0, 6)) {
    const kind = (evt.kind || '').replace('_', ' ');
    const bits = [];
    if (evt.reason) bits.push(evt.reason);
    if (evt.avg_delay_min != null) bits.push(`avg ${evt.avg_delay_min} min`);
    if (evt.max_delay_min != null) bits.push(`max ${evt.max_delay_min} min`);
    if (evt.end_time) bits.push(`ends ${evt.end_time}`);
    grid.push([kind, bits.join(' · ') || '—']);
  }
  openDrawer({ eyebrow: 'NAS Status', title: e.airport || 'Advisory', grid, kind: 'airport' });
}

function selectAirspace(feature, layerId) {
  const p = feature?.properties || {};
  const fmtAlt = (val, uom, code) =>
    val != null ? `${val} ${uom || 'FT'} ${code || ''}`.trim() : '—';
  let eyebrow = 'Airspace';
  let title = p.NAME || p.TITLE || p.IDENT || 'Feature';
  const grid = [];

  if (layerId === 'airspace-tfrs') {
    eyebrow = 'TFR';
    title = p.TITLE || p.NOTAM_KEY || 'TFR';
    grid.push(['Title', p.TITLE || '—']);
    grid.push(['NOTAM', p.NOTAM_KEY || '—']);
    grid.push(['State', p.STATE || '—']);
    if (p.LAST_MODIFICATION_DATETIME) grid.push(['Updated', p.LAST_MODIFICATION_DATETIME]);
  } else if (layerId === 'airspace-classes') {
    eyebrow = 'Class Airspace';
    title = p.NAME || p.IDENT || 'Class airspace';
    if (p.CLASS) grid.push(['Class', p.CLASS]);
    if (p.TYPE_CODE) grid.push(['Type', p.TYPE_CODE]);
    grid.push(['Ceiling', fmtAlt(p.UPPER_VAL, p.UPPER_UOM, p.UPPER_CODE)]);
    grid.push(['Floor', fmtAlt(p.LOWER_VAL, p.LOWER_UOM, p.LOWER_CODE)]);
  } else if (layerId === 'airspace-sua') {
    eyebrow = 'Special Use Airspace';
    title = p.NAME || p.IDENT || 'SUA';
    if (p.LOCAL_TYPE || p.TYPE_CODE) grid.push(['Type', p.LOCAL_TYPE || p.TYPE_CODE]);
    grid.push(['Ceiling', fmtAlt(p.UPPER_VAL, p.UPPER_UOM, p.UPPER_CODE)]);
    grid.push(['Floor', fmtAlt(p.LOWER_VAL, p.LOWER_UOM, p.LOWER_CODE)]);
    if (p.CITY || p.STATE) grid.push(['Location', [p.CITY, p.STATE].filter(Boolean).join(', ')]);
    if (p.TIMESOFUSE) grid.push(['Times of use', p.TIMESOFUSE]);
    if (p.CONT_AGENT) grid.push(['Authority', p.CONT_AGENT]);
  } else if (layerId === 'airspace-ats') {
    eyebrow = 'ATS Route';
    title = p.IDENT || 'ATS route';
    if (p.TYPE_CODE) grid.push(['Type', p.TYPE_CODE]);
    if (p.LEVEL_) grid.push(['Level', p.LEVEL_]);
    if (p.MAA_VAL) grid.push(['MAA', `${p.MAA_VAL} ${p.MAA_UOM || 'FT'}`]);
    if (p.MEA_E_VAL) grid.push(['MEA E', `${p.MEA_E_VAL} ${p.MEA_E_UOM || 'FT'}`]);
    if (p.MEA_W_VAL) grid.push(['MEA W', `${p.MEA_W_VAL} ${p.MEA_W_UOM || 'FT'}`]);
    if (p.WKHR_CODE) grid.push(['Hours', p.WKHR_CODE]);
  } else if (layerId === 'airfield-runways') {
    eyebrow = 'Runway';
    title = `${p.ICAO_ID || p.FAA_ID || ''} · ${p.DESIGNATOR || p.RWY_ID || ''}`.trim();
    grid.push(['Airport', p.ICAO_ID || p.FAA_ID || '—']);
    grid.push(['Runway', p.DESIGNATOR || p.RWY_ID || '—']);
    grid.push(['Surface', SURFACE_LABEL[String(p.SURFACE)] || p.SURFACE || '—']);
    grid.push(['Status', RWY_OPER_LABEL[String(p.RWY_OPER)] || p.RWY_OPER || '—']);
  } else if (layerId === 'airfield-taxiways') {
    eyebrow = 'Taxiway';
    title = `${p.ICAO_ID || p.FAA_ID || ''} · ${p.DESIGNATOR || ''}`.trim();
    grid.push(['Airport', p.ICAO_ID || p.FAA_ID || '—']);
    grid.push(['Taxiway', p.DESIGNATOR || '—']);
    grid.push(['Surface', SURFACE_LABEL[String(p.SURFACE)] || p.SURFACE || '—']);
    grid.push(['Status', TWY_OPER_LABEL[String(p.TWY_OPER)] || p.TWY_OPER || '—']);
  } else if (layerId === 'airfield-obstacles') {
    eyebrow = 'Obstacle';
    title = p.Type_Code || 'Obstacle';
    if (p.AGL != null) grid.push(['AGL', `${p.AGL} ft`]);
    if (p.AMSL != null) grid.push(['MSL', `${p.AMSL} ft`]);
    if (p.Lighting) grid.push(['Lighting', p.Lighting]);
    if (p.Verified) grid.push(['Verified', p.Verified]);
    if (p.City || p.State) grid.push(['Location', [p.City, p.State].filter(Boolean).join(', ')]);
    if (p.OAS_Number) grid.push(['OAS', p.OAS_Number]);
  } else if (layerId === 'airspace-artcc' || layerId === 'airspace-artcc-labels') {
    eyebrow = 'ARTCC';
    title = `${p.IDENT || ''} · ${p.NAME || 'Center'}`.trim();
    grid.push(['Center', p.NAME || '—']);
    grid.push(['Ident', p.IDENT || '—']);
    grid.push(['Type', p.LOCAL_TYPE || p.TYPE_CODE || '—']);
    if (p.UPPER_VAL || p.LOWER_VAL) {
      grid.push(['Ceiling', fmtAlt(p.UPPER_VAL, p.UPPER_UOM, p.UPPER_CODE)]);
      grid.push(['Floor', fmtAlt(p.LOWER_VAL, p.LOWER_UOM, p.LOWER_CODE)]);
    }
  } else if (layerId === 'airspace-navaids' || layerId === 'airspace-navaids-labels') {
    eyebrow = 'NAVAID';
    title = `${p.IDENT || ''} · ${p.CLASS_TXT || ''}`.trim();
    grid.push(['Name', p.NAME_TXT || '—']);
    grid.push(['Class', p.CLASS_TXT || '—']);
    grid.push(['Channel / Freq', p.CHANNEL || '—']);
    grid.push(['Status', p.STATUS || '—']);
    if (p.CITY || p.STATE) grid.push(['Location', [p.CITY, p.STATE].filter(Boolean).join(', ')]);
  }

  openDrawer({
    eyebrow,
    title,
    grid: grid.length ? grid : [['Properties', JSON.stringify(p).slice(0, 200)]],
  });
}

function selectFlight(f) {
  state.selectedFlightId = f.id;
  state.selectedAirport = null;
  // Clear stale data from a previous selection so the drawer doesn't
  // briefly show another aircraft's origin/destination while the new
  // lookup is in flight.
  state.selectedFlightInfo = null;
  state.selectedTrack = null;
  state.selectedRegistry = null;
  renderFlightDrawer(f);
  refreshLayers();
  // Kick off the async lookups; renderFlightDrawer will be re-called
  // when they land.
  fetchSelectedFlightDetails(f.id, f.callsign).catch((err) =>
    console.warn('flight details fetch failed', err)
  );
}

// Re-paint the drawer for the currently-selected flight, blending
// whatever live state vector we have (`f`) with whatever metadata the
// async backend lookup has produced (`state.selectedFlightInfo`).
// Called both immediately on click *and* after the lookup resolves.
function renderFlightDrawer(f) {
  const info = state.selectedFlightInfo;
  const latest = info?.latest || null;
  const reg = state.selectedRegistry;
  const grid = [
    ['ICAO24', f.id.toUpperCase()],
    ['Callsign', f.callsign || latest?.callsign || '—'],
    ['Country', f.country || '—'],
    ['Altitude', formatAltitude(f.alt_m)],
    ['Speed', formatSpeed(f.vel_mps)],
    ['Heading', f.heading != null ? `${Math.round(f.heading)}°` : '—'],
    ['V/Rate', formatVrate(f.vrate_mps)],
  ];
  if (reg) {
    if (reg.registration) grid.push(['Registration', reg.registration]);
    const typeLine = [reg.manufacturer, reg.type].filter(Boolean).join(' ');
    if (typeLine) grid.push(['Aircraft', `${typeLine}${reg.icao_type ? ` (${reg.icao_type})` : ''}`]);
    if (reg.owner) grid.push(['Operator', reg.owner]);
  }
  if (info?.airline?.name) {
    grid.push(['Airline', info.airline.name]);
  }
  if (latest) {
    if (latest.departure) {
      grid.push(['From', formatAirportRef(latest.departure)]);
    }
    if (latest.arrival) {
      grid.push(['To', formatAirportRef(latest.arrival)]);
    }
    if (latest.first_seen) {
      grid.push(['Departed', formatUnixUtc(latest.first_seen)]);
    }
    if (latest.last_seen && latest.arrival) {
      grid.push(['Last seen', formatUnixUtc(latest.last_seen)]);
    }
  } else if (info) {
    // Lookup landed but OpenSky has no recent flight summary. Surface
    // that explicitly so the user knows the field isn't just blank.
    grid.push(['From / To', 'no recent flight on file']);
  }
  if (state.selectedTrack && state.selectedTrack.length) {
    grid.push(['Track', `${state.selectedTrack.length + 1} waypoints (cyan = current)`]);
  }
  // Registry card (photo + tag + manufacturer/type) renders below the
  // grid so the existing two-column drawer layout stays clean.
  let extraHtml = '';
  if (reg) {
    const photo = reg.photo_thumb_url || reg.photo_url;
    const photoEl = photo
      ? `<img class="reg-photo" src="${escapeHtml(photo)}" alt="${escapeHtml(reg.registration || 'aircraft photo')}" loading="lazy" />`
      : '';
    const typeLine = [reg.manufacturer, reg.type].filter(Boolean).join(' ');
    extraHtml = `
      <div class="drawer-registry">
        <div class="reg-head">
          <span class="reg-tag">${escapeHtml(reg.registration || '—')}</span>
          <span style="color:var(--text-faint);font-size:11px;">${escapeHtml(reg.icao_type || '')} · via ${escapeHtml(reg.source || '')}</span>
        </div>
        ${typeLine ? `<div class="reg-rows"><div class="row"><span class="k">Aircraft</span><span>${escapeHtml(typeLine)}</span></div></div>` : ''}
        ${reg.owner ? `<div class="reg-rows"><div class="row"><span class="k">Operator</span><span>${escapeHtml(reg.owner)}</span></div></div>` : ''}
        ${reg.owner_country ? `<div class="reg-rows"><div class="row"><span class="k">Country</span><span>${escapeHtml(reg.owner_country)}</span></div></div>` : ''}
        ${photoEl}
      </div>
    `;
  }
  openDrawer({
    eyebrow: 'Aircraft',
    title: f.callsign || latest?.callsign || reg?.registration || f.id.toUpperCase(),
    grid,
    extraHtml,
    kind: 'aircraft',
  });
}

function formatAirportRef(a) {
  if (!a) return '—';
  const code = a.iata || a.icao || '';
  const place = [a.city, a.country].filter(Boolean).join(', ');
  if (a.name && place) return `${code ? code + ' · ' : ''}${a.name} (${place})`;
  if (a.name) return `${code ? code + ' · ' : ''}${a.name}`;
  if (place) return `${code ? code + ' · ' : ''}${place}`;
  return code || '—';
}

function formatUnixUtc(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  // Format like "2026-04-26 14:32 UTC" — readable in both the drawer
  // and chat, no surprises across timezones.
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
       + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

async function fetchSelectedFlightDetails(icao24, callsign) {
  if (!icao24) return;
  const token = ++state._flightDetailsToken;
  // Fire info + track + registry (+ optional route) in parallel. They're
  // independent endpoints and the registry lookup in particular is
  // routinely the slowest of the four — we don't want it to gate the
  // info/track rendering.
  const cs = (callsign || '').trim();
  const promises = [
    fetch(`/api/flight/${encodeURIComponent(icao24)}`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null),
    fetch(`/api/flight/${encodeURIComponent(icao24)}/track?time=0`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null),
    fetch(`/api/registry/${encodeURIComponent(icao24)}`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null),
    cs
      ? fetch(`/api/route/${encodeURIComponent(cs)}`)
          .then((r) => (r.ok ? r.json() : null))
          .catch(() => null)
      : Promise.resolve(null),
  ];
  const [info, track, registry, route] = await Promise.all(promises);
  // Race guard — discard if the user has clicked another aircraft (or
  // closed the drawer) while we were waiting for the upstream.
  if (token !== state._flightDetailsToken) return;
  if (state.selectedFlightId !== icao24) return;
  // Merge an adsbdb route lookup into the flight info if OpenSky didn't
  // return one — the adsbdb origin/destination is callsign-keyed and
  // is often available before OpenSky has linked an in-progress flight.
  let mergedInfo = info || null;
  if (route && route.found && (!mergedInfo || !mergedInfo.latest)) {
    mergedInfo = {
      ...(mergedInfo || {}),
      latest: {
        ...(mergedInfo?.latest || {}),
        callsign: cs || null,
        departure: route.origin || null,
        arrival: route.destination || null,
      },
      route_source: 'adsbdb',
      airline: route.airline || null,
    };
  } else if (route && route.found && mergedInfo) {
    mergedInfo.airline = mergedInfo.airline || route.airline || null;
  }
  state.selectedFlightInfo = mergedInfo;
  state.selectedTrack =
    track && track.available && Array.isArray(track.waypoints)
      ? buildSelectedTrackSegments(track.waypoints)
      : null;
  state.selectedRegistry = registry && registry.found ? registry : null;
  const f = state.flights.get(icao24);
  if (f) renderFlightDrawer(f);
  refreshLayers();
}

function selectAirport(a) {
  state.selectedAirport = a;
  state.selectedFlightId = null;
  renderAirportDrawer(a, /* nasEvent */ null);
  refreshLayers();
  // Always check NAS Status for the clicked airport — even if the layer
  // toggle is off — because users routinely click an airport to ask
  // "is this one delayed?". The lookup is cached server-side so it's
  // cheap, and a missing airport just renders the drawer without the
  // advisory chip.
  fetchAirportNas(a).catch((err) => console.warn('NAS lookup failed', err));
}

function renderAirportDrawer(a, nasEvent) {
  const grid = [
    ['Name', a.name],
    ['City', a.city || '—'],
    ['Country', a.country || '—'],
    ['Elevation', a.elevation_ft != null ? `${a.elevation_ft} ft` : '—'],
    ['Type', (a.type || '').replace('_', ' ')],
    ['Position', `${a.lat.toFixed(3)}, ${a.lon.toFixed(3)}`],
  ];
  let extraHtml = '';
  if (nasEvent && (nasEvent.events || []).length) {
    const sev = nasEvent.severity || 'info';
    const items = (nasEvent.events || []).slice(0, 4).map((evt) => {
      const kind = (evt.kind || '').replace('_', ' ');
      const reason = evt.reason ? ` · ${escapeHtml(evt.reason)}` : '';
      const end = evt.end_time ? ` · until ${escapeHtml(evt.end_time)}` : '';
      let extra = '';
      if (evt.avg_delay_min != null || evt.max_delay_min != null) {
        extra = ` · avg ${escapeHtml(evt.avg_delay_min ?? '—')}/max ${escapeHtml(evt.max_delay_min ?? '—')} min`;
      }
      return `<div><span class="nas-kind">${escapeHtml(kind)}</span>${reason}${extra}${end}</div>`;
    }).join('');
    extraHtml = `<div class="drawer-nas severity-${escapeHtml(sev)}">${items}</div>`;
  } else if (nasEvent) {
    extraHtml = `<div class="drawer-nas severity-info"><span class="nas-kind">NAS Status</span> · no active advisories</div>`;
  }
  openDrawer({
    eyebrow: 'Airport',
    title: `${a.code} · ${a.icao || ''}`.trim(),
    grid,
    extraHtml,
    kind: 'airport',
  });
}

async function fetchAirportNas(a) {
  const code = a.code || a.icao;
  if (!code) return;
  const r = await fetch(`/api/nas/airport/${encodeURIComponent(code)}`);
  if (!r.ok) return;
  const ev = await r.json();
  // Only render if the airport is still selected (user may have clicked
  // away during the in-flight request).
  if (state.selectedAirport && state.selectedAirport.code === a.code) {
    renderAirportDrawer(a, ev);
  }
}

function openDrawer({ eyebrow, title, grid, extraHtml = '', kind = 'other' }) {
  elDrawerEyebrow.textContent = eyebrow;
  elDrawerTitle.textContent = title;
  elDrawerGrid.innerHTML = grid
    .map(([k, v]) => `<div class="drawer-cell"><div class="k">${k}</div><div class="v">${v}</div></div>`)
    .join('') + (extraHtml || '');
  // Drives the CSS that conditionally shows the "Show trail" / "Arcs to
  // nearest airport" buttons. Only aircraft drawers should ever expose
  // those — airports / METAR / airspace features get a clean info card.
  elDrawer.dataset.kind = kind;
  elDrawer.classList.remove('hidden');
  elDrawer.setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  elDrawer.classList.add('hidden');
  elDrawer.setAttribute('aria-hidden', 'true');
  state.selectedFlightId = null;
  state.selectedAirport = null;
  // Drop the per-flight cache so the gradient track disappears with the
  // drawer, and so a stale lookup that resolves after this point can't
  // briefly repaint the panel.
  state.selectedFlightInfo = null;
  state.selectedTrack = null;
  state.selectedRegistry = null;
  state._flightDetailsToken += 1;
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

// Tier the airport request by zoom so a CONUS-wide view doesn't drown the
// canvas in 6,000 small_airport dots. The dataset is ~11k entries, server
// pre-sorts large→medium→small, so a tighter `types=` filter at low zoom
// keeps the visible set legible while a wider one at high zoom surfaces
// every public-use field around the user's airport. Limits are picked so
// the truncation `+` indicator only shows at extreme metro density.
function airportsQueryForZoom(zoom) {
  if (zoom < 5)  return 'types=large&limit=2000';
  if (zoom < 7)  return 'types=large,medium&limit=4000';
  if (zoom < 9)  return 'types=large,medium&limit=6000';
  return 'limit=12000';
}

// ── In-view HUD counters ─────────────────────────────────────────────────
// Recomputes the "Aircraft in view" / "Airports in view" pills any time the
// camera moves. These counts describe what's currently visible (so they're
// bbox-derived, not pump-derived) and have to stay accurate even when the
// live pump is paused or in manual cadence — otherwise zooming feels broken.
//
// Aircraft count is purely client-side: filter the existing snapshot by
// the new bbox. Airports are a cheap static lookup, so we re-fetch them.
//
// IMPORTANT: pump() also calls updateAircraftInViewCount() rather than
// writing state.flights.size — otherwise we'd race ourselves. The flight
// store accumulates aircraft across pumps (a CONUS-wide view leaves ~5k
// in state.flights even after the user zooms to a single airport), so
// state.flights.size != "in the current viewport" except by coincidence.
function updateAircraftInViewCount() {
  if (!state.map || !elHudFlights) return;
  const b = state.map.getBounds();
  const w = b.getWest(),  e = b.getEast();
  const s = b.getSouth(), n = b.getNorth();
  let aircraft = 0;
  if (state.flights && typeof state.flights.values === 'function') {
    for (const f of state.flights.values()) {
      const lat = f && f.lat;
      const lon = f && f.lon;
      if (lat == null || lon == null) continue;
      if (lat >= s && lat <= n && lon >= w && lon <= e) aircraft++;
    }
  }
  elHudFlights.textContent = aircraft.toLocaleString();
}

let _hudInflight = false;
let _hudPending  = false;
async function updateInViewHud() {
  if (!state.map) return;
  updateAircraftInViewCount();

  // Airports: fast static lookup. Coalesce overlapping calls so a fast
  // pinch-zoom doesn't fire 4 fetches in a row.
  if (_hudInflight) { _hudPending = true; return; }
  _hudInflight = true;
  try {
    const apQuery = airportsQueryForZoom(state.map.getZoom());
    const bboxStr = currentBbox();
    const res = await fetchOrThrow(`/api/airports?bbox=${bboxStr}&${apQuery}`);
    state.airportsInView   = res.airports || [];
    state.airportsTruncated = Boolean(res.truncated);
    if (elHudAirports) {
      const c = state.airportsInView.length.toLocaleString();
      elHudAirports.textContent = state.airportsTruncated ? `${c}+` : c;
    }
    refreshLayers();
  } catch (err) {
    // Don't toast — this is a background refresh that races with the
    // pump and the user's zoom gestures. Silent failure is fine; the
    // counts just stay at whatever the last successful fetch returned.
    console.debug('updateInViewHud: airports fetch failed', err);
  } finally {
    _hudInflight = false;
    if (_hudPending) {
      _hudPending = false;
      // Schedule the trailing refresh after the current frame so a long
      // pinch-zoom collapses to a single fetch when it stops.
      setTimeout(updateInViewHud, 50);
    }
  }
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
    const airportQuery = airportsQueryForZoom(state.map.getZoom());
    const [flightsRes, airportsRes] = await Promise.all([
      fetchOrThrow(`/api/flights?bbox=${bboxStr}`),
      fetchOrThrow(`/api/airports?bbox=${bboxStr}&${airportQuery}`),
    ]);

    ingestFlights(flightsRes.flights || []);
    state.airportsInView = airportsRes.airports || [];
    state.airportsTruncated = Boolean(airportsRes.truncated);

    // Aircraft pill must be bbox-filtered, not state.flights.size — the
    // flight store accumulates planes across pumps, so its size lags far
    // behind a zoom-in. Same helper updateInViewHud uses on moveend.
    updateAircraftInViewCount();
    const apCount = state.airportsInView.length.toLocaleString();
    elHudAirports.textContent = state.airportsTruncated ? `${apCount}+` : apCount;
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
  // The dedicated "Refresh" HUD cell was retired — the bottom live-bar
  // status pill now carries cadence / paused / rate-limit info. We keep
  // this function for back-compat with any deploy that still has the
  // element in the DOM, but otherwise no-op gracefully.
  if (!elHudRefresh) return;
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
    let headingCorrection = 0;
    if (prev && prev.anchor) {
      const oldRender = renderPos(prev, now);
      const dLon = oldRender[0] - newAnchor.lon;
      const dLat = oldRender[1] - newAnchor.lat;
      // Sanity guard: if the gap is huge (>~5 degrees, e.g. icao24 reuse
      // or a fix from the other side of the world), don't try to glide;
      // just snap.
      if (dLon * dLon + dLat * dLat < 25) {
        correction = clampedCorrection(oldRender, newAnchor);
        // Capture heading delta so the icon's orientation eases to the
        // new heading instead of snapping. The "owed" delta is the
        // shortest signed arc from new → old, so renderHeading adds
        // (old − new) * decay back onto the new base heading.
        const oldHeading = renderHeading(prev, now);
        if (newAnchor.heading != null && Number.isFinite(oldHeading)) {
          headingCorrection = shortestArcDeg(newAnchor.heading, oldHeading);
        }
      }
    }
    f.anchor = newAnchor;
    f.correction = correction;
    f.headingCorrection = headingCorrection;
    f.correctionTs = now;

    state.flights.set(f.id, f);
    let history = state.flightHistory.get(f.id);
    if (!history) {
      history = [];
      state.flightHistory.set(f.id, history);
    }
    history.push({
      lat: f.lat,
      lon: f.lon,
      alt_m: Number.isFinite(f.alt_m) ? f.alt_m : 0,
      t: (now - state.animationStart) / 1000,
    });
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
  // The agent's track flow can land a {highlight} bus message before
  // the next /api/flights poll pulls the target plane into the
  // client-side index — see `state.pendingHighlight` above. Now that
  // a fresh batch has been ingested, retry the lookup.
  resolvePendingHighlight();
}

// ── Status pill ──────────────────────────────────────────────────────────

function setStatus(kind, text) {
  // Legacy header pill — kept for back-compat but optional now.
  if (elStatusPill) {
    elStatusPill.classList.remove('is-stale', 'is-error');
    if (kind === 'stale') elStatusPill.classList.add('is-stale');
    if (kind === 'error') elStatusPill.classList.add('is-error');
  }
  if (elStatusText) elStatusText.textContent = text;

  // Drive the bottom live-bar status pill. We expand `kind` (which only
  // knows ok|stale|error|connecting) to the richer state machine via
  // state.live.lastStatus so the chip can also surface paused / manual /
  // hidden / rate-limit. The rate-limit case gets visual priority — red,
  // pulsing — so the rate-limit signal is co-located with the cadence
  // controls that throttle it.
  if (elLiveBarStatus && elLiveBarStatusState) {
    // Map the legacy `kind` param (live|ok|stale|error|connecting) to the
    // new chip data-status. Note that `kind === 'live'` is what a healthy
    // pump emits — both that and the legacy `'ok'` mean "streaming green".
    let dataStatus =
        (kind === 'ok' || kind === 'live') ? 'ok'
      :  kind === 'stale'                   ? 'stale'
      :  kind === 'error'                   ? 'error'
      : /* 'connecting' / unknown */          'connecting';

    // The richer state.live.lastStatus may upgrade ok → manual / paused /
    // hidden, or override anything → rate-limit. rate-limit and error
    // should never silently revert to ok just because of a transient
    // lastStatus race.
    const live = state && state.live && state.live.lastStatus;
    if (kind === 'error' && text === 'rate limited') {
      dataStatus = 'rate-limit';
    } else if (live === 'rate-limit' && kind !== 'live') {
      dataStatus = 'rate-limit';
    } else if (live === 'paused')  dataStatus = 'paused';
    else if (live === 'hidden')    dataStatus = 'hidden';
    else if (live === 'manual' && (kind === 'live' || kind === 'ok')) {
      dataStatus = 'manual';
    }

    elLiveBarStatus.dataset.status = dataStatus;
    elLiveBarStatusState.textContent = text || dataStatus;
  }
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
  // METAR · VFR/IFR — bbox-fetched on moveend like the FAA bbox layers,
  // but the toast shows VFR/IFR counts so the user knows whether the
  // current view is "all green" or hides instrument minima.
  const elMetar = document.getElementById('layer-metar');
  if (elMetar) {
    elMetar.addEventListener('change', async (e) => {
      state.layerVisibility.metar = e.target.checked;
      if (e.target.checked) {
        try {
          // If the user is zoomed out past the render floor, tell them
          // explicitly rather than silently doing nothing — the fetch
          // would no-op anyway. Subsequent zoom-in fires moveend → fetch.
          if (state.zoom < METAR_MIN_ZOOM - 0.5) {
            toast(`METAR: zoom in to load (z ≥ ${METAR_MIN_ZOOM})`, 3500);
          } else {
            await fetchBboxMetar(/* force */ true);
            const stations = state.metar.bbox?.stations || [];
            const bad = stations.filter((s) =>
              s.flt_cat === 'IFR' || s.flt_cat === 'LIFR'
            ).length;
            if (!stations.length) {
              toast('METAR: no observations in this view', 3500);
            } else {
              toast(`METAR: ${stations.length} stations · ${bad} IFR/LIFR`);
            }
          }
        } catch (err) {
          console.warn('METAR fetch failed', err);
          toast('METAR: failed to load');
          e.target.checked = false;
          state.layerVisibility.metar = false;
        }
      } else {
        clearMetar();
      }
      refreshLayers();
    });
  }
  // NAS Status — single nationwide pull, refreshed every minute while
  // the layer is on. Toast surfaces the worst severity in the feed
  // because that's almost always the question the user is about to ask.
  const elNas = document.getElementById('layer-nas');
  if (elNas) {
    elNas.addEventListener('change', async (e) => {
      state.layerVisibility.nas = e.target.checked;
      if (e.target.checked) {
        try {
          await ensureNasStatus(/* force */ true);
          summariseNasToToast();
        } catch (err) {
          console.warn('NAS status fetch failed', err);
          toast('NAS Status: failed to load');
          e.target.checked = false;
          state.layerVisibility.nas = false;
        }
      } else {
        clearNas();
      }
      refreshLayers();
    });
  }
  // Globally cached airspace toggles (sua / classes / tfrs / runways /
  // artcc). ARTCC is tiny (21 features) and globally cached identically
  // to the rest, so we just append it to the loop.
  for (const [key, label] of [
    ['sua', 'Restricted & SUA'],
    ['classes', 'Class B/C/D'],
    ['tfrs', 'Active TFRs'],
    ['runways', 'Runways'],
    ['artcc', 'ARTCC boundaries'],
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
  // Bbox-only airspace toggles. These layers are too large to ship globally
  // so we tell the user when they're zoomed too far out for the layer to
  // make sense, and refetch on moveend.
  for (const [key, label, minZoom] of [
    ['taxiways', 'Taxiways', TAXIWAY_MIN_ZOOM],
    ['obstacles', 'Obstacles', OBSTACLE_MIN_ZOOM],
    ['ats', 'ATS routes', ATS_MIN_ZOOM],
    ['navaids', 'NAVAIDs', NAVAID_MIN_ZOOM],
  ]) {
    const el = document.getElementById(`layer-${key}`);
    if (!el) continue;
    el.addEventListener('change', async (e) => {
      state.layerVisibility[key] = e.target.checked;
      if (e.target.checked) {
        if (state.zoom < minZoom) {
          toast(`${label}: zoom in to z≥${minZoom} to load this layer`, 3500);
        } else {
          try {
            await fetchBboxAirspace(key, /* force */ true);
            const n = state.airspace.bbox[key]?.features?.length ?? 0;
            if (n === 0) {
              toast(`${label}: nothing in this view — try a busier area`, 3500);
            } else {
              toast(`${label}: ${n} feature${n === 1 ? '' : 's'} in view`);
            }
          } catch (err) {
            console.warn(`bbox airspace ${key} failed`, err);
            toast(`${label}: failed to load`);
          }
        }
      }
      refreshLayers();
    });
  }

  // ── 3D extrude toggle + vertical-scale slider ─────────────────────────
  // The toggle flips `state.airspace3D`; the slider scales every extruded
  // ceiling so a continental-scale view doesn't read as a flat sheet and
  // a single-airport view doesn't punch through the camera. Both feed
  // into `getElevation` via the layer's updateTriggers, which forces a
  // GPU buffer recompute when either value changes.
  const elExtrude = document.getElementById('airspace-3d');
  const elVScale = document.getElementById('airspace-vscale');
  const elVScaleOut = document.getElementById('airspace-vscale-out');
  const vscaleRow = document.getElementById('airspace-vscale-row');
  const refreshVScaleVisibility = () => {
    if (!vscaleRow) return;
    vscaleRow.classList.toggle('disabled', !state.airspace3D);
  };
  if (elExtrude) {
    elExtrude.checked = state.airspace3D;
    elExtrude.addEventListener('change', (e) => {
      state.airspace3D = e.target.checked;
      refreshVScaleVisibility();
      // Auto-tilt the camera so the user sees the 3D effect immediately.
      // We don't *force* it back to flat when toggling off — preserves
      // whatever pitch the user already set.
      if (state.airspace3D && state.map && state.map.getPitch() < 30) {
        state.map.easeTo({ pitch: 55, duration: 600 });
      }
      refreshLayers();
      toast(state.airspace3D ? '3D airspace on — tilt the map (right-click + drag) to see the columns' : '3D airspace off');
    });
  }
  if (elVScale && elVScaleOut) {
    const writeOut = () => { elVScaleOut.textContent = `${state.airspaceVScale.toFixed(1)}×`; };
    writeOut();
    elVScale.addEventListener('input', (e) => {
      const v = parseFloat(e.target.value);
      if (!Number.isFinite(v)) return;
      state.airspaceVScale = v;
      writeOut();
      refreshLayers();
    });
  }
  refreshVScaleVisibility();
}

// ── Aircraft colour-mode picker ──────────────────────────────────────────
// The picker is an accordion in the Layers tab: each row contains a
// hidden radio + a header + a legend body. Selecting the radio drives
// both the highlight and the body expand purely in CSS via :has().
// JS only has to listen for `change` events to mirror state and ask
// deck.gl to recompute the colour buffer.
//
// `setColorMode(mode, opts)` is the single source of truth. It's also
// what the WebSocket bus calls when the chat agent posts to
// /api/map/color, so the user can say "color the planes by altitude"
// and have the picker animate to the right row.
function wireColorMode() {
  const radios = document.querySelectorAll('input[name="color-mode"]');
  if (!radios.length) return;
  // Restore persisted choice (best-effort — invalid values fall back to
  // the default that was set in state initialisation).
  let saved = null;
  try { saved = localStorage.getItem('flightops:colorMode'); } catch { /* ignored */ }
  if (saved && Object.prototype.hasOwnProperty.call(COLOR_SCHEMES, saved)) {
    state.colorMode = saved;
  }
  // Sync the DOM with whatever we just settled on (without persisting
  // again or re-firing change events).
  setColorMode(state.colorMode, { fromUI: true, persist: false });
  radios.forEach((r) => {
    r.addEventListener('change', () => {
      if (!r.checked) return;
      setColorMode(r.value, { fromUI: true });
    });
  });
}

function setColorMode(mode, { fromUI = false, persist = true } = {}) {
  if (!Object.prototype.hasOwnProperty.call(COLOR_SCHEMES, mode)) return false;
  state.colorMode = mode;
  if (persist) {
    try { localStorage.setItem('flightops:colorMode', mode); } catch { /* ignored */ }
  }
  // Mirror the radios — important when the change came from outside
  // the UI (chat / WebSocket) so the accordion row visibly opens.
  const radios = document.querySelectorAll('input[name="color-mode"]');
  radios.forEach((r) => { r.checked = r.value === mode; });
  refreshLayers();
  if (!fromUI) {
    const labels = {
      phase: 'phase of flight',
      altitude: 'altitude',
      vrate: 'vertical rate',
      squawk: 'emergency squawk',
    };
    toast(`Colouring planes by ${labels[mode] || mode}`);
  }
  return true;
}

// METAR colour mode — same accordion-radio pattern as the aircraft
// colour mode above, but applied to the weather-station body. Persisted
// in localStorage as `flightops:metarColorMode`.
const METAR_COLOR_MODES = new Set(['flt_cat', 'wind', 'temp', 'visibility']);

function wireMetarColorMode() {
  const radios = document.querySelectorAll('input[name="metar-color-mode"]');
  if (!radios.length) return;
  let saved = null;
  try { saved = localStorage.getItem('flightops:metarColorMode'); } catch { /* ignored */ }
  if (saved && METAR_COLOR_MODES.has(saved)) {
    state.metarColorMode = saved;
  }
  setMetarColorMode(state.metarColorMode, { fromUI: true, persist: false });
  radios.forEach((r) => {
    r.addEventListener('change', () => {
      if (!r.checked) return;
      setMetarColorMode(r.value, { fromUI: true });
    });
  });
}

function setMetarColorMode(mode, { fromUI = false, persist = true } = {}) {
  if (!METAR_COLOR_MODES.has(mode)) return false;
  state.metarColorMode = mode;
  if (persist) {
    try { localStorage.setItem('flightops:metarColorMode', mode); } catch { /* ignored */ }
  }
  const radios = document.querySelectorAll('input[name="metar-color-mode"]');
  radios.forEach((r) => { r.checked = r.value === mode; });
  refreshLayers();
  if (!fromUI) {
    const labels = {
      flt_cat: 'flight category',
      wind: 'wind speed',
      temp: 'temperature',
      visibility: 'visibility',
    };
    toast(`Colouring METAR by ${labels[mode] || mode}`);
  }
  return true;
}

// ── Chip-legend bucket filters (phase + squawk color modes) ─────────────
// The phase-of-flight and emergency-squawk legends double as multi-select
// filters: each chip represents one bucket of aircraft, and clicking the
// chip toggles whether that bucket is rendered on the map. By default
// every chip is "armed" so all flights show; click "Cruise · slow" off
// and every level cruiser disappears, click "On ground" off and gates
// clear out. The same scheme works for squawk (kill normal traffic to
// see only emergency squawks, or vice-versa).
//
// State of truth:
//   - `state.flightFilter.{phase,squawk}` — Sets of bucket ids that are
//     currently armed.
//   - The chip's own `data-active` DOM attribute mirrors this so CSS can
//     render the green outline / dim states without consulting JS.
//   - localStorage persists the configured filter across reloads as
//     `flightops:flightFilter` (a tiny JSON object of arrays).
//
// Why we preventDefault() the click: the chip lives inside a wrapping
// <label class="cm-row"> for the color-mode radio. A bare click would
// bubble to the label and re-check the radio (a no-op when this color
// mode is already active, but a confusing flicker if it ever weren't).
// preventDefault on the button click suppresses the label's default
// "activate the input" action.
const FLIGHT_FILTER_BUCKETS = {
  phase:  PHASE_BUCKETS,
  squawk: SQUAWK_BUCKETS,
};

function _loadFlightFilterFromStorage() {
  let raw = null;
  try { raw = localStorage.getItem('flightops:flightFilter'); } catch { /* ignored */ }
  if (!raw) return;
  let parsed;
  try { parsed = JSON.parse(raw); } catch { return; }
  for (const mode of Object.keys(FLIGHT_FILTER_BUCKETS)) {
    const allowed = FLIGHT_FILTER_BUCKETS[mode];
    const persisted = Array.isArray(parsed?.[mode]) ? parsed[mode] : null;
    if (!persisted) continue;
    // Intersect with the known bucket list — guards against a stored
    // bucket id that's been renamed in a later release.
    state.flightFilter[mode] = new Set(persisted.filter((b) => allowed.includes(b)));
  }
}

function _saveFlightFilterToStorage() {
  const out = {};
  for (const mode of Object.keys(FLIGHT_FILTER_BUCKETS)) {
    out[mode] = [...state.flightFilter[mode]];
  }
  try { localStorage.setItem('flightops:flightFilter', JSON.stringify(out)); }
  catch { /* ignored */ }
}

function _syncChipDom() {
  document.querySelectorAll('.legend-chip').forEach((chip) => {
    const mode   = chip.dataset.mode;
    const bucket = chip.dataset.bucket;
    const set    = state.flightFilter[mode];
    if (!set) return;
    chip.dataset.active = set.has(bucket) ? 'true' : 'false';
    chip.setAttribute('aria-pressed', set.has(bucket) ? 'true' : 'false');
  });
}

function wireFlightFilters() {
  _loadFlightFilterFromStorage();
  _syncChipDom();

  const chips = document.querySelectorAll('.legend-chip');
  chips.forEach((chip) => {
    chip.addEventListener('click', (ev) => {
      // Critical: the chip is nested inside <label class="cm-row"> which
      // owns the color-mode radio. preventDefault here stops the label
      // from re-activating the radio (which would close any other
      // accordion row with no perceptible benefit). stopPropagation
      // belt-and-braces against any other delegated handler upstream.
      ev.preventDefault();
      ev.stopPropagation();

      const mode   = chip.dataset.mode;
      const bucket = chip.dataset.bucket;
      const set    = state.flightFilter[mode];
      if (!set) return;
      if (set.has(bucket)) {
        // Refuse to disarm the *last* armed chip — that would leave the
        // map empty with no obvious way to recover. Instead, clicking
        // the last armed chip "inverts" the filter: re-arm all buckets.
        // This matches the muscle memory of "I want to reset the filter"
        // without needing a separate reset button.
        if (set.size === 1) {
          for (const b of FLIGHT_FILTER_BUCKETS[mode]) set.add(b);
        } else {
          set.delete(bucket);
        }
      } else {
        set.add(bucket);
      }
      _syncChipDom();
      _saveFlightFilterToStorage();
      refreshLayers();
    });

    // Keyboard a11y — Space/Enter on a focused chip should toggle it
    // exactly like the click handler above. The browser will fire the
    // synthetic click for Enter automatically; Space we have to handle.
    chip.addEventListener('keydown', (ev) => {
      if (ev.key === ' ' || ev.code === 'Space') {
        ev.preventDefault();
        chip.click();
      }
    });
    // Initialise aria-pressed for screen readers that don't wait for
    // the first sync above.
    chip.setAttribute('role', 'switch');
  });
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
// Two flavours of fetch:
//   1. Globally cached (sua/classes/tfrs/runways) — fetched once via
//      /api/airspace/{name} when the toggle flips on, then refreshed on a
//      schedule (TFRs every 5 min, the rest hourly).
//   2. Bbox-scoped (taxiways/obstacles/ats) — refetched on every moveend
//      because the upstream layers are too large to ship globally. The
//      backend also caches per-bbox so successive pans inside the same
//      city don't burn upstream calls.
const AIRSPACE_TTL_MS = {
  sua: 3600 * 1000,
  classes: 3600 * 1000,
  tfrs: 5 * 60 * 1000,
  runways: 24 * 3600 * 1000,
  artcc: 24 * 3600 * 1000,
};
const BBOX_DATASETS = ['taxiways', 'obstacles', 'ats', 'navaids'];
// Layers below this zoom are too coarse to bother fetching; spares the
// backend a wave of useless calls when the user is at country scale.
const BBOX_MIN_ZOOM = {
  taxiways: TAXIWAY_MIN_ZOOM - 1,
  obstacles: OBSTACLE_MIN_ZOOM - 1,
  ats: ATS_MIN_ZOOM - 1,
  navaids: NAVAID_MIN_ZOOM - 1,
};

async function ensureAirspace(name) {
  const now = Date.now();
  const fetched = state.airspace.fetchedAt[name] || 0;
  if (state.airspace[name] && (now - fetched) < AIRSPACE_TTL_MS[name]) return;
  const r = await fetch(`/api/airspace/${name}`);
  if (!r.ok) throw new Error(`airspace ${name}: HTTP ${r.status}`);
  state.airspace[name] = await r.json();
  state.airspace.fetchedAt[name] = now;
}

// Round bbox to 2 decimals so a tiny pan inside the same city doesn't
// trigger an upstream call. The backend has its own bbox cache too.
function bboxKey(bb) {
  return `${bb.getWest().toFixed(2)},${bb.getSouth().toFixed(2)},${bb.getEast().toFixed(2)},${bb.getNorth().toFixed(2)}`;
}

async function fetchBboxAirspace(name, force = false) {
  if (!state.layerVisibility[name]) return;
  if (state.airspace.fetchingBbox[name]) return;
  if (state.zoom < BBOX_MIN_ZOOM[name]) return;
  const bb = state.map.getBounds();
  const key = bboxKey(bb);
  if (!force && state.airspace.bboxKey[name] === key) return;
  state.airspace.fetchingBbox[name] = true;
  try {
    const url = `/api/airspace/${name}?bbox=${key}`;
    const r = await fetch(url);
    if (!r.ok) {
      console.warn(`bbox airspace ${name}: HTTP ${r.status}`);
      return;
    }
    state.airspace.bbox[name] = await r.json();
    state.airspace.bboxKey[name] = key;
    refreshLayers();
  } catch (err) {
    console.warn(`bbox airspace ${name} fetch failed`, err);
  } finally {
    state.airspace.fetchingBbox[name] = false;
  }
}

function refreshBboxAirspace() {
  for (const name of BBOX_DATASETS) {
    if (state.layerVisibility[name]) fetchBboxAirspace(name);
  }
}

// ── METAR (aviationweather.gov) ───────────────────────────────────────────
// Same bbox-driven cadence as taxiways/obstacles/ATS, but routed through
// our own /api/weather/metar proxy so the upstream sees one user-agent
// per server rather than one per browser tab.

async function fetchBboxMetar(force = false) {
  if (!state.layerVisibility.metar) return;
  if (state.metar.fetching) return;
  if (state.zoom < METAR_MIN_ZOOM - 0.5) return;
  const bb = state.map.getBounds();
  // Stations are ~50 km apart in CONUS, so a 2-decimal bbox key (~1 km
  // grid) is over-fine; we round to whole degrees here too because
  // the server itself rounds to 1° before caching.
  const w = Math.round(bb.getWest());
  const s = Math.round(bb.getSouth());
  const e = Math.round(bb.getEast());
  const n = Math.round(bb.getNorth());
  const key = `${w},${s},${e},${n}`;
  if (!force && state.metar.bboxKey === key) return;
  state.metar.fetching = true;
  try {
    const r = await fetch(`/api/weather/metar?bbox=${key}`);
    if (!r.ok) {
      console.warn(`METAR: HTTP ${r.status}`);
      return;
    }
    state.metar.bbox = await r.json();
    state.metar.bboxKey = key;
    refreshLayers();
  } catch (err) {
    console.warn('METAR fetch failed', err);
  } finally {
    state.metar.fetching = false;
  }
}

function refreshBboxMetar() {
  if (state.layerVisibility.metar) fetchBboxMetar();
}

function clearMetar() {
  state.metar.bbox = null;
  state.metar.bboxKey = null;
}

// ── NAS Status (nasstatus.faa.gov) ────────────────────────────────────────
// Single nationwide pull. Cached server-side for 30 s and refreshed
// client-side on a 60 s tick while the layer is on.

async function ensureNasStatus(force = false) {
  if (state.nas.fetching) return;
  state.nas.fetching = true;
  try {
    const r = await fetch('/api/nas/status');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const payload = await r.json();
    state.nas.events = payload.events || [];
    state.nas.fetchedAt = Date.now();
    state.nas.nextRefresh = performance.now() + NAS_REFRESH_MS;
    renderNasOverlay();
  } finally {
    state.nas.fetching = false;
  }
}

function clearNas() {
  state.nas.events = null;
  state.nas.fetchedAt = 0;
  state.nas.nextRefresh = 0;
  renderNasOverlay();
}

function summariseNasToToast() {
  const events = state.nas.events || [];
  if (!events.length) {
    toast('NAS Status: no active advisories', 3500);
    return;
  }
  const counts = { ground_stop: 0, closure: 0, delay: 0, advisory: 0, info: 0 };
  for (const e of events) counts[e.severity] = (counts[e.severity] || 0) + 1;
  const parts = [];
  if (counts.ground_stop) parts.push(`${counts.ground_stop} ground stop${counts.ground_stop === 1 ? '' : 's'}`);
  if (counts.closure) parts.push(`${counts.closure} closure${counts.closure === 1 ? '' : 's'}`);
  if (counts.delay) parts.push(`${counts.delay} delay${counts.delay === 1 ? '' : 's'}`);
  if (counts.advisory) parts.push(`${counts.advisory} advisory${counts.advisory === 1 ? '' : 's'}`);
  toast(parts.length ? `NAS: ${parts.join(' · ')}` : `NAS: ${events.length} active`);
}

async function nasTick() {
  if (state.layerVisibility.nas) {
    try {
      await ensureNasStatus(/* force */ true);
      refreshLayers();
    } catch (err) {
      console.warn('NAS status refresh failed', err);
    }
  }
  setTimeout(nasTick, NAS_REFRESH_MS);
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
      // pitch/bearing are optional — when absent flyTo() preserves the
      // current camera pose, so a chat-issued goto that's only meant to
      // pan doesn't yank a manually-tilted view back to flat.
      flyTo(msg.lon, msg.lat, msg.zoom || 9, {
        pitch:   msg.pitch,
        bearing: msg.bearing,
      });
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
    case 'view': {
      // Free-form camera control. Any field left undefined preserves
      // the current value via flyTo's no-op branches above.
      const m = state.map;
      if (!m) break;
      const cur = m.getCenter();
      const lat  = Number.isFinite(msg.lat) ? msg.lat : cur.lat;
      const lon  = Number.isFinite(msg.lon) ? msg.lon : cur.lng;
      const zoom = Number.isFinite(msg.zoom) ? msg.zoom : m.getZoom();
      flyTo(lon, lat, zoom, { pitch: msg.pitch, bearing: msg.bearing });
      const bits = [];
      if (Number.isFinite(msg.pitch))   bits.push(`pitch ${Math.round(msg.pitch)}°`);
      if (Number.isFinite(msg.bearing)) bits.push(`bearing ${Math.round(msg.bearing)}°`);
      if (Number.isFinite(msg.zoom))    bits.push(`zoom ${msg.zoom.toFixed(1)}`);
      toast(bits.length ? `View: ${bits.join(' · ')}` : 'View updated');
      break;
    }
    case 'filter': {
      // Chip-legend bucket filter, driven from chat. Mirrors
      // wireFlightFilters' click handler logic but accepts four shapes
      // (buckets / include / exclude / reset) so the agent can pass the
      // user's natural phrasing through.
      const mode = msg.mode;
      if (mode !== 'phase' && mode !== 'squawk') {
        console.warn('filter: unknown mode', mode);
        break;
      }
      const valid = mode === 'phase' ? PHASE_BUCKETS : SQUAWK_BUCKETS;
      const set = state.flightFilter[mode];
      if (msg.reset) {
        for (const b of valid) set.add(b);
      } else if (Array.isArray(msg.buckets)) {
        set.clear();
        for (const b of msg.buckets) if (valid.includes(b)) set.add(b);
        // Refuse to leave the set completely empty — that hides every
        // plane and confuses the user. If the agent passed in nothing
        // valid, fall back to "everything armed".
        if (set.size === 0) for (const b of valid) set.add(b);
      } else {
        if (Array.isArray(msg.include)) {
          for (const b of msg.include) if (valid.includes(b)) set.add(b);
        }
        if (Array.isArray(msg.exclude)) {
          for (const b of msg.exclude) if (valid.includes(b)) set.delete(b);
        }
        if (set.size === 0) for (const b of valid) set.add(b);
      }
      _syncChipDom();
      _saveFlightFilterToStorage();
      // If chat is filtering by phase/squawk, switch the colour mode to
      // match — otherwise the filter is invisibly inert. The browser
      // already reads `state.colorMode` to decide which filter set
      // applies, so swapping here is the natural side effect.
      if (state.colorMode !== mode) setColorMode(mode, { fromUI: false });
      else refreshLayers();
      toast(`Filter (${mode}): ${[...set].sort().join(', ') || '∅'}`);
      break;
    }
    case 'metar-color': {
      const ok = setMetarColorMode(msg.mode, { fromUI: false });
      if (!ok) toast(`Unknown METAR color mode: ${msg.mode}`);
      break;
    }
    case 'airspace3d': {
      const next = !!msg.enabled;
      state.airspace3D = next;
      const cb = document.getElementById('layer-airspace3d');
      if (cb) cb.checked = next;
      // The 3D extrusion subsystem reads state.airspace3D every frame
      // via updateTriggers, so we just need to refresh once for the
      // change to flow through the GeoJsonLayers and the IconLayer
      // (planes lift to their reported altitude in 3D mode).
      refreshLayers();
      // The vertical-scale slider is only meaningful in 3D mode.
      if (typeof refreshVScaleVisibility === 'function') refreshVScaleVisibility();
      toast(`3D airspace: ${next ? 'on' : 'off'}`);
      break;
    }
    case 'layer': {
      if (msg.layer in state.layerVisibility) {
        state.layerVisibility[msg.layer] = !!msg.visible;
        const cb = document.getElementById(`layer-${msg.layer}`);
        if (cb) cb.checked = !!msg.visible;
        // Globally cached airspace layers need their GeoJSON loaded before
        // they can render; bbox-only layers fetch from current bounds.
        if (msg.visible && ['sua', 'classes', 'tfrs', 'runways', 'artcc'].includes(msg.layer)) {
          ensureAirspace(msg.layer)
            .then(refreshLayers)
            .catch((err) => console.warn(`agent-toggled airspace ${msg.layer} failed`, err));
        } else if (msg.visible && BBOX_DATASETS.includes(msg.layer)) {
          fetchBboxAirspace(msg.layer, /* force */ true)
            .then(refreshLayers)
            .catch((err) => console.warn(`agent-toggled bbox ${msg.layer} failed`, err));
        } else if (msg.visible && msg.layer === 'metar') {
          fetchBboxMetar(/* force */ true)
            .then(refreshLayers)
            .catch((err) => console.warn('agent-toggled metar failed', err));
        } else if (msg.visible && msg.layer === 'nas') {
          ensureNasStatus(/* force */ true)
            .then(refreshLayers)
            .catch((err) => console.warn('agent-toggled nas failed', err));
        } else if (!msg.visible && msg.layer === 'metar') {
          clearMetar();
          refreshLayers();
        } else if (!msg.visible && msg.layer === 'nas') {
          clearNas();
          refreshLayers();
        } else {
          refreshLayers();
        }
      }
      break;
    }
    case 'color': {
      // The chat skill flips the aircraft colour scheme by POST'ing to
      // /api/map/color, which broadcasts {type:'color', mode:'<key>'}.
      // setColorMode() validates against COLOR_SCHEMES, persists, ticks
      // the radio, and triggers a deck.gl colour-buffer rebuild.
      const ok = setColorMode(msg.mode, { fromUI: false });
      if (!ok) toast(`Unknown color mode: ${msg.mode}`);
      break;
    }
    case 'highlight': {
      const target = (msg.flight || '').toLowerCase();
      if (tryHighlightTarget(target)) break;
      // The plane isn't in the client-side index yet — almost always
      // because the agent's track flow has just sent us a {view} pulling
      // the camera to a previously off-screen plane, and the next
      // /api/flights poll for the new bbox hasn't landed. Stash it and
      // resolve in `ingestFlights` when the plane shows up.
      state.pendingHighlight = {
        target,
        expiresAt: performance.now() + 20_000,
      };
      break;
    }
    default:
      console.debug('unhandled bus msg', msg);
  }
}

// Shared lookup-and-highlight side-effects: set selection, force trails
// on, fly the camera, open the detail drawer. Used by the {highlight}
// bus handler on its first try AND by `resolvePendingHighlight()` once
// a delayed /api/flights poll loads the target. Returns true if the
// plane was found, false if it still isn't in `state.flights`.
function tryHighlightTarget(target) {
  if (!target) return false;
  let matched = state.flights.get(target);
  if (!matched) {
    for (const f of state.flights.values()) {
      if ((f.callsign || '').trim().toLowerCase() === target) {
        matched = f;
        break;
      }
    }
  }
  if (!matched) return false;
  state.selectedFlightId = matched.id;
  state.layerVisibility.trails = true;
  const trailsBox = document.getElementById('layer-trails');
  if (trailsBox) trailsBox.checked = true;
  flyTo(matched.lon, matched.lat, 9);
  selectFlight(matched);
  return true;
}

// Drain any pending highlight intent (set by the {highlight} bus
// handler when the target wasn't yet in state.flights). Called from
// `ingestFlights` after each poll lands. If the intent has expired
// without a match, surface the original "no live contact" toast so
// the user (and agent, watching `delivered`) gets the same signal as
// before — just delayed by up to 20s instead of fired prematurely on
// the very first poll.
function resolvePendingHighlight() {
  const ph = state.pendingHighlight;
  if (!ph) return;
  if (tryHighlightTarget(ph.target)) {
    state.pendingHighlight = null;
    return;
  }
  if (performance.now() > ph.expiresAt) {
    toast(`No live contact for ${ph.target}`);
    state.pendingHighlight = null;
  }
}

function flyTo(lon, lat, zoom = 9, opts = {}) {
  if (!state.map) return;
  // pitch/bearing are optional — when not supplied we keep whatever the
  // user (or a previous bus message) last set, so a "highlight UAL123"
  // mid-tilted-arc-view doesn't yank the camera flat.
  const args = {
    center: [lon, lat],
    zoom,
    speed: 1.4,
    curve: 1.42,
    essential: true,
  };
  if (Number.isFinite(opts.pitch))   args.pitch   = clamp(opts.pitch, 0, 70);
  if (Number.isFinite(opts.bearing)) args.bearing = ((opts.bearing % 360) + 360) % 360;
  state.map.flyTo(args);
}

function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

// ── Chat ────────────────────────────────────────────────────────────────
// Chat is a thin wrapper over `openclaw agent --json`. OpenClaw owns the
// conversation state on its side (keyed by session_id), so we only post the
// latest user message and the session id we got back from the previous turn.

let openclawSessionId = null;

// Tiny markdown renderer used by the bot replies. The agent emits
// `**bold**`, `### headings`, `*  bullets`, `1.  numbered`, `inline code`,
// triple-backtick fences, and `[text](url)` style links — none of
// which the previous textContent path rendered, so the user saw raw
// asterisks and hashes in the chat. We don't pull in a full markdown
// lib (no extra CDN, no npm) — this is a focused implementation
// covering exactly what the agent emits, and it HTML-escapes first
// (via the existing escapeHtml() helper) so a malicious-looking string
// from a tool reply can't inject DOM.
function renderMarkdown(text) {
  // 1. Pull fenced code blocks out so the inline rules don't clobber
  //    the contents (a `**` inside example code shouldn't bold).
  const fences = [];
  let src = String(text || '').replace(/```([\s\S]*?)```/g, (_m, body) => {
    const idx = fences.length;
    fences.push(body.replace(/^\n/, ''));
    return `\u0000FENCE${idx}\u0000`;
  });

  // 2. Walk the source line by line, classifying each line so a
  //    block of "heading + bullet + bullet" parses correctly even
  //    without a blank line between them. This is the shape the
  //    agent actually emits, so a paragraph-block parser misses it.
  const rawLines = src.split('\n');
  const out = [];
  let listKind = null;       // 'ul' | 'ol' | null
  let listItems = [];        // collected <li> contents (already inline-rendered)
  let paraLines = [];        // collected paragraph lines (already inline-rendered)

  const flushList = () => {
    if (listKind && listItems.length) {
      out.push(`<${listKind}>${listItems.map((it) => `<li>${it}</li>`).join('')}</${listKind}>`);
    }
    listKind = null;
    listItems = [];
  };
  const flushPara = () => {
    if (paraLines.length) {
      out.push(`<p>${paraLines.join('<br>')}</p>`);
    }
    paraLines = [];
  };
  const flushAll = () => { flushList(); flushPara(); };

  // Render inline syntax (code, links, bold, italic) on one stretch
  // of already-HTML-escaped text. Bold runs before italic so `**a**`
  // doesn't half-italicise. The italic rule explicitly skips a `*`
  // followed by whitespace so a leading bullet marker (`*   foo`)
  // doesn't open an italic span.
  const inline = (s) => {
    let t = escapeHtml(s);
    t = t.replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`);
    t = t.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_m, label, url) =>
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
    );
    t = t.replace(/\*\*([^*\n][^*]*?)\*\*/g, '<strong>$1</strong>');
    // Italic: a `*` adjacent to whitespace on either side isn't an
    // emphasis open/close (it's a bullet marker or stray asterisk).
    t = t.replace(/(^|[\s(])\*(\S(?:[^*\n]*?\S)?)\*(?=[\s),.;:!?]|$)/g,
                  '$1<em>$2</em>');
    return t;
  };

  for (const raw of rawLines) {
    // Pass through fenced-code placeholders untouched; flush whatever
    // open block we were building.
    if (/^\u0000FENCE\d+\u0000$/.test(raw)) {
      flushAll();
      out.push(raw);
      continue;
    }

    // Blank line ends both lists and paragraphs.
    if (raw.trim() === '') {
      flushAll();
      continue;
    }

    // Heading "### foo" → <h4>. We compress h1/h2 to h3 and h3+ to h4
    // because we render inside the chat panel; h1 would blow up the
    // visual hierarchy.
    const head = raw.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (head) {
      flushAll();
      const level = head[1].length;
      const tag = level <= 2 ? 'h3' : 'h4';
      out.push(`<${tag}>${inline(head[2])}</${tag}>`);
      continue;
    }

    // Bullet list (- / * / +). Accept any leading whitespace so the
    // agent's nested "    *   foo" continuations still register as
    // bullets — we render them at one level since the chat panel is
    // narrow and a deep tree isn't useful.
    const ul = raw.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      flushPara();
      if (listKind && listKind !== 'ul') flushList();
      listKind = 'ul';
      listItems.push(inline(ul[1]));
      continue;
    }

    // Numbered list (1. / 12. …).
    const ol = raw.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) {
      flushPara();
      if (listKind && listKind !== 'ol') flushList();
      listKind = 'ol';
      listItems.push(inline(ol[1]));
      continue;
    }

    // Anything else: paragraph line. Indented continuation of a list
    // item is a common shape ("    Average delay: 52 min" right under
    // a bullet); attach it to the previous <li> with a <br> rather
    // than starting a new paragraph that visually detaches it.
    if (listKind && /^\s+\S/.test(raw)) {
      const last = listItems.length - 1;
      if (last >= 0) {
        listItems[last] = `${listItems[last]}<br>${inline(raw.trim())}`;
        continue;
      }
    }
    flushList();
    paraLines.push(inline(raw));
  }
  flushAll();

  // 3. Re-substitute fenced code blocks. Their contents are escaped
  //    against HTML-injection — the agent has, on rare occasions,
  //    emitted HTML examples in code fences.
  return out.join('').replace(/\u0000FENCE(\d+)\u0000/g, (_m, i) =>
    `<pre><code>${escapeHtml(fences[Number(i)])}</code></pre>`
  );
}

function appendMessage(role, content, { thinking = false, markdown = false } = {}) {
  const wrap = document.createElement('div');
  wrap.className = `msg msg-${role}${thinking ? ' msg-thinking' : ''}`;
  const body = document.createElement('div');
  body.className = 'msg-content';
  if (markdown) {
    body.innerHTML = renderMarkdown(content);
  } else {
    body.textContent = content;
  }
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

// ── Collapsible prompt suggestions ──────────────────────────────────────
//
// Default state: chips are visible so the user has a clear menu of
// starter prompts. After the first user submit we auto-collapse them
// to a single chevron-button — that way the chat log gets all the
// vertical room. The user can click the chevron at any time to
// expand the chips back.
const elSuggestWrap   = document.getElementById('chat-suggestions-wrap');
const elSuggestToggle = document.getElementById('chat-suggestions-toggle');

function setSuggestionsCollapsed(collapsed) {
  if (!elSuggestWrap || !elSuggestToggle) return;
  elSuggestWrap.dataset.collapsed = collapsed ? 'true' : 'false';
  elSuggestToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}
elSuggestToggle?.addEventListener('click', () => {
  const isCollapsed = elSuggestWrap?.dataset.collapsed === 'true';
  setSuggestionsCollapsed(!isCollapsed);
});

// ── Chat trace toggle ───────────────────────────────────────────────────
//
// One pill toggle below the input lets the user opt into seeing the
// agent's tool calls + tool results streamed live under each reply.
// Defaults to OFF (cleanest demo) and persists to localStorage so the
// choice survives refreshes.
//
// We DO NOT surface "thinking" here because the inference model
// OpenClaw uses (Nemotron-3 Super v3) doesn't expose intermediate
// reasoning to the agent runtime — even with `--thinking high
// --verbose on` the JSONL only contains tool_call / tool_result /
// final-text records, no separate thought stream. If a future model
// exposes reasoning we'll add it back.

const elChatToggleTools = document.getElementById('chat-toggle-tools');
const CHAT_TRACE_LS_KEY = 'flightops.chatTrace';

const chatTrace = (() => {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(CHAT_TRACE_LS_KEY) || '{}'); }
  catch (_e) { saved = {}; }
  return { showTools: saved.showTools === true };
})();

function syncChatToggleUI() {
  if (elChatToggleTools)
    elChatToggleTools.setAttribute('aria-pressed', chatTrace.showTools ? 'true' : 'false');
}
function persistChatToggles() {
  try { localStorage.setItem(CHAT_TRACE_LS_KEY, JSON.stringify(chatTrace)); }
  catch (_e) { /* private mode etc. — non-fatal */ }
}
elChatToggleTools?.addEventListener('click', () => {
  chatTrace.showTools = !chatTrace.showTools;
  syncChatToggleUI();
  persistChatToggles();
});
syncChatToggleUI();

// Build a one-line summary for the collapsed header of a tool-call /
// tool-result / thought card. We try to extract the most useful
// fragment (the URL hit by curl, the python file body's first line,
// etc.) so the user can scan the trace without expanding everything.
function summariseTraceItem(item) {
  if (item.kind === 'tool_call') {
    const cmd = (item.command || '').trim();
    if (cmd) {
      const url = cmd.match(/https?:\/\/[^\s'"]+/);
      if (url) return `${item.tool || 'exec'} · ${url[0]}`;
      return `${item.tool || 'exec'} · ${cmd.split('\n')[0].slice(0, 90)}`;
    }
    if (item.args && typeof item.args === 'object') {
      const keys = Object.keys(item.args).slice(0, 3).join(', ');
      return `${item.tool || 'tool'} (${keys})`;
    }
    return item.tool || 'tool';
  }
  if (item.kind === 'tool_result') {
    const text = (item.text || '').trim();
    if (!text) return item.is_error ? 'error (empty)' : 'result (empty)';
    const first = text.split('\n').find((ln) => ln.trim()) || text;
    return first.slice(0, 110);
  }
  if (item.kind === 'planning' || item.kind === 'thought' || item.kind === 'text') {
    const t = (item.text || '').trim().split('\n').find((ln) => ln.trim()) || '';
    return t.slice(0, 120);
  }
  return '';
}

// Build a single trace card and append it to a wrapper. Returns the
// new card so the caller can later replace its contents (e.g. when
// the matching tool_result arrives for a tool_call already shown).
function appendTraceCard(wrap, e) {
  let cls = 'trace-item';
  let tag = '';
  let body = '';
  if (e.kind === 'tool_call') {
    cls += ' trace-tool';     tag = 'tool';
    body = e.command ? e.command : (e.args ? JSON.stringify(e.args, null, 2) : '');
  } else if (e.kind === 'tool_result') {
    cls += e.is_error ? ' trace-result-err' : ' trace-result';
    tag = e.is_error ? 'error' : 'result';
    body = e.text || '';
  } else if (e.kind === 'planning') {
    cls += ' trace-planning';  tag = 'plan';
    body = e.text || '';
  } else if (e.kind === 'thought') {
    cls += ' trace-thought';  tag = 'thinking';
    body = e.text || '';
  } else {
    return null;
  }
  const det = document.createElement('details');
  det.className = cls;
  // Planning + thinking start expanded so the user can see the
  // narrative; tool calls/results stay collapsed by default since
  // their bodies (raw JSON) are bulky.
  if (e.kind === 'planning' || e.kind === 'thought') det.open = true;
  const summary = document.createElement('summary');
  const tagEl = document.createElement('span');
  tagEl.className = 'trace-tag';
  tagEl.textContent = tag;
  const sumEl = document.createElement('span');
  sumEl.className = 'trace-summary';
  sumEl.textContent = summariseTraceItem(e);
  summary.appendChild(tagEl);
  summary.appendChild(sumEl);
  det.appendChild(summary);
  if (body) {
    const pre = document.createElement('pre');
    pre.textContent = body;
    det.appendChild(pre);
  }
  wrap.appendChild(det);
  return det;
}

// Decide whether an incoming streamed event should be rendered given
// the user's current toggle state. Returning false drops it on the
// floor — events still arrive over the wire so toggling on mid-turn
// can't retro-render past events; that's an acceptable trade-off.
function shouldShowEvent(e) {
  if (!e) return false;
  // Planning narration is part of the agent's "decide what to call"
  // step, so it lives under the same Tool calls toggle. Surfacing
  // them together gives the user a coherent "plan → call → result"
  // story for each step.
  if (e.kind === 'tool_call' || e.kind === 'tool_result' || e.kind === 'planning') {
    return chatTrace.showTools;
  }
  return false;
}

elChatForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = elChatInput.value.trim();
  if (!text) return;
  elChatInput.value = '';
  // Auto-collapse the prompt-suggestions drawer the moment the user
  // sends their first message so the chat log gets the full panel.
  setSuggestionsCollapsed(true);
  appendMessage('user', text);

  // Bot bubble layout while the turn is running:
  //
  //   ┌── .msg ──────────────────────────────┐
  //   │  .msg-trace        (streamed cards)  │   ← grows top-down
  //   │  .msg-content      (Agent working…)  │   ← stays at bottom
  //   └──────────────────────────────────────┘
  //
  // We mount the trace wrapper BEFORE the content node so each new
  // tool_call / tool_result card lands above the spinner — the
  // spinner therefore stays visually anchored at the bottom of the
  // bubble as a live "still going" tail. When the final reply
  // arrives, the spinner row is replaced in-place by the markdown
  // and the trace cards remain stacked above it as the audit trail.
  const bubble = appendMessage('bot', '', { thinking: true });
  const content = bubble.querySelector('.msg-content');
  const traceWrap = document.createElement('div');
  traceWrap.className = 'msg-trace';
  bubble.insertBefore(traceWrap, content);
  content.innerHTML =
    'Agent working <span class="thinking-dots"><span></span><span></span><span></span></span>';

  // Track the latest tool_call card by its call_id so when the
  // matching tool_result arrives we can dock it visually beneath
  // the call. Today we just append both to the same flow — that
  // keeps the code simple and reads naturally top-down.
  const toolCardById = new Map();

  // OpenClaw turns can take a while when the agent decides to use
  // tools, so disable the input until the reply lands.
  elChatInput.disabled = true;

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message:    text,
        session_id: openclawSessionId,
      }),
    });
    if (!res.ok) {
      const errBody = await res.text();
      bubble.remove();
      appendMessage('bot', `Agent call failed (${res.status}). ${errBody.slice(0, 320)}`);
      return;
    }

    // NDJSON reader: pull from the body stream, split on newlines,
    // parse each line, dispatch by event type. This is the same
    // pattern the OpenAI streaming client and `kubectl logs -f` use.
    const reader  = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let leftover  = '';
    let finalDone = null;
    let finalErr  = null;

    streamLoop: while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      leftover += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = leftover.indexOf('\n')) >= 0) {
        const line = leftover.slice(0, idx).trim();
        leftover = leftover.slice(idx + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); }
        catch (_e) { continue; }
        if (msg.type === 'event') {
          if (shouldShowEvent(msg)) {
            const card = appendTraceCard(traceWrap, msg);
            if (msg.kind === 'tool_call' && msg.call_id && card) {
              toolCardById.set(msg.call_id, card);
            }
            elChatLog.scrollTop = elChatLog.scrollHeight;
          }
        } else if (msg.type === 'done') {
          finalDone = msg;
          break streamLoop;
        } else if (msg.type === 'error') {
          finalErr = msg.error || 'Agent error';
          break streamLoop;
        }
      }
    }

    if (finalErr) {
      content.textContent = `Agent error: ${finalErr.slice(0, 320)}`;
      return;
    }
    if (!finalDone) {
      content.textContent = 'Agent stream ended without a reply.';
      return;
    }
    if (finalDone.session_id) openclawSessionId = finalDone.session_id;
    content.innerHTML = renderMarkdown(finalDone.reply || '(no reply)');
    // Drop the `msg-thinking` class so .msg-bot.msg-content gets the
    // bright body-text color rule meant for finished replies.
    bubble.classList.remove('msg-thinking');
    if (!traceWrap.childNodes.length) traceWrap.remove();
    elChatLog.scrollTop = elChatLog.scrollHeight;
  } catch (err) {
    bubble.remove();
    appendMessage('bot', `Network error talking to OpenClaw: ${err.message}`);
  } finally {
    elChatInput.disabled = false;
    elChatInput.focus();
  }
});

// ── Resizable rail ──────────────────────────────────────────────────────
//
// The left panel width is driven by the `--rail-w` CSS variable on
// :root. Drag the right-edge handle to resize, double-click to reset
// to the default 340px. Width is clamped so the rail can't get
// unusably narrow or eat the whole map. The chosen width is persisted
// to localStorage so it survives refreshes.
const RAIL_MIN_PX = 280;
const RAIL_MAX_PX = 720;
const RAIL_DEFAULT_PX = 340;
const RAIL_LS_KEY = 'flightops.railWidth';

function applyRailWidth(px) {
  const clamped = Math.max(RAIL_MIN_PX, Math.min(RAIL_MAX_PX, Math.round(px)));
  document.documentElement.style.setProperty('--rail-w', `${clamped}px`);
  // MapLibre measures its container on resize, so let it know the
  // map stage just changed width. Otherwise the canvas can stay
  // pinned to the pre-drag width until the next window resize.
  if (state && state.map && typeof state.map.resize === 'function') {
    state.map.resize();
  }
  return clamped;
}

(function initRailWidth() {
  let saved = NaN;
  try { saved = parseInt(localStorage.getItem(RAIL_LS_KEY) || '', 10); }
  catch (_e) { /* ignore */ }
  if (Number.isFinite(saved) && saved >= RAIL_MIN_PX && saved <= RAIL_MAX_PX) {
    document.documentElement.style.setProperty('--rail-w', `${saved}px`);
  }
})();

const elRailResizer = document.getElementById('rail-resizer');
if (elRailResizer) {
  let dragging = false;
  const onMove = (clientX) => {
    if (!dragging) return;
    const rect = document.body.getBoundingClientRect();
    // The rail is the leftmost grid track, so the cursor's distance
    // from the left edge of the viewport IS the desired width.
    applyRailWidth(clientX - rect.left);
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('rail-resizing');
    // Persist the final width.
    try {
      const cur = getComputedStyle(document.documentElement)
        .getPropertyValue('--rail-w').trim();
      const px = parseInt(cur, 10);
      if (Number.isFinite(px)) localStorage.setItem(RAIL_LS_KEY, String(px));
    } catch (_e) { /* ignore */ }
    window.removeEventListener('mousemove', onMouseMove);
    window.removeEventListener('mouseup',   stop);
    window.removeEventListener('touchmove', onTouchMove);
    window.removeEventListener('touchend',  stop);
  };
  const onMouseMove = (ev) => onMove(ev.clientX);
  const onTouchMove = (ev) => {
    if (ev.touches && ev.touches[0]) onMove(ev.touches[0].clientX);
  };
  elRailResizer.addEventListener('mousedown', (ev) => {
    ev.preventDefault();
    dragging = true;
    document.body.classList.add('rail-resizing');
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup',   stop);
  });
  elRailResizer.addEventListener('touchstart', (ev) => {
    if (ev.touches && ev.touches[0]) {
      dragging = true;
      document.body.classList.add('rail-resizing');
      window.addEventListener('touchmove', onTouchMove, { passive: true });
      window.addEventListener('touchend',  stop);
    }
  }, { passive: true });
  elRailResizer.addEventListener('dblclick', () => {
    applyRailWidth(RAIL_DEFAULT_PX);
    try { localStorage.setItem(RAIL_LS_KEY, String(RAIL_DEFAULT_PX)); }
    catch (_e) { /* ignore */ }
  });
  // Keyboard: ←/→ adjust by 16px when the handle is focused, makes
  // it usable without a mouse.
  elRailResizer.addEventListener('keydown', (ev) => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    const cur = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue('--rail-w'),
      10,
    ) || RAIL_DEFAULT_PX;
    const step = ev.shiftKey ? 48 : 16;
    const next = applyRailWidth(cur + (ev.key === 'ArrowRight' ? step : -step));
    try { localStorage.setItem(RAIL_LS_KEY, String(next)); }
    catch (_e) { /* ignore */ }
  });
}

// ── Boot ─────────────────────────────────────────────────────────────────

(async function boot() {
  initMap();
  await new Promise((resolve) => state.map.once('load', resolve));
  wireTabs();
  wireLayerToggles();
  wireLiveControls();
  wireColorMode();
  wireMetarColorMode();
  wireFlightFilters();
  connectWs();
  schedulePump({ force: true });
  requestAnimationFrame(animate);
  // Pre-warm the RainViewer manifest so the first weather toggle is instant.
  ensureWeatherManifest();
  setTimeout(weatherTick, WEATHER_REFRESH_MS);
  // NAS Status refresh tick — refetches the nationwide feed every 60 s
  // while the layer is on. No-op when the layer is off, so the cost
  // is just a setTimeout chain.
  setTimeout(nasTick, NAS_REFRESH_MS);
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
