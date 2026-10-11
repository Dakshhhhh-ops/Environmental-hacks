const API = `${window.SENTINEL_CONFIG?.apiBaseUrl || ''}/api`;
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const state = {
  mode: 'live', synthetic: false, spots: [], places: [], assessments: [], proposal: null, history: [],
  map: null, markers: {}, routeLayers: [], routes: null, selectedRoute: 0, lang: 'english', filter: 'all',
};
const COLORS = { green: '#54d6a0', amber: '#f2b94b', red: '#ff6570', idle: '#5c6e7c', blue: '#66b9ea' };
const LABELS = { green: 'LOW', amber: 'WATCH', red: 'HIGH' };
const RANK = { red: 0, amber: 1, green: 2 };
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hhmm = (iso) => (iso ? String(iso).slice(11, 16) : '—');
const niceDate = (iso) => { const d = new Date(`${String(iso).slice(0, 10)}T00:00:00`); return isNaN(d) ? iso : d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }); };

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  let body; try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`);
  return body;
}
function toast(message) { const el = $('#toast'); el.textContent = message; el.style.display = 'block'; clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.style.display = 'none'; }, 5000); }
function busy(title, notes = []) {
  clearInterval(busy.timer);
  if (!title) { $('#busy').hidden = true; return; }
  $('#busy').hidden = false; $('#busyTitle').textContent = title;
  let i = 0; $('#busyNote').textContent = notes[0] || '';
  busy.timer = setInterval(() => { i = Math.min(i + 1, notes.length - 1); $('#busyNote').textContent = notes[i] || ''; }, 4500);
}
const byId = () => Object.fromEntries(state.assessments.map((a) => [a.spot_id, a]));

/* ---------- map ---------- */
function initMap() {
  if (!window.L) { $('#sourceStatus').textContent = 'MAP LIBRARY UNAVAILABLE'; return; }
  state.map = L.map('map', { zoomControl: false }).setView([28.625, 77.19], 11);
  L.control.zoom({ position: 'bottomright' }).addTo(state.map);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap contributors', maxZoom: 18 }).addTo(state.map);
}
function sparkline(a, spot, w = 150, h = 34) {
  const vals = a?.hourly_series || [];
  if (!a?.data_available || !vals.length) return '<span class="muted">—</span>';
  const max = Math.max(spot.threshold_mm_hr * 1.1, ...vals, 1), bw = w / vals.length;
  const bars = vals.map((v, i) => { const bh = Math.max(1, (v / max) * (h - 2)); const lvl = v >= spot.threshold_mm_hr ? 'red' : v >= spot.threshold_mm_hr * 0.6 ? 'amber' : 'green'; return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" rx="1.5" fill="${COLORS[lvl]}" opacity="${v > 0 ? 0.95 : 0.25}"/>`; }).join('');
  const ty = (h - (spot.threshold_mm_hr / max) * (h - 2)).toFixed(1);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Hourly rainfall, dashed line is the threshold">${bars}<line x1="0" x2="${w}" y1="${ty}" y2="${ty}" stroke="#e6edf2" stroke-dasharray="3 3" stroke-width="1" opacity=".6"/></svg>`;
}
function renderMarkers() {
  if (!state.map) return;
  Object.values(state.markers).forEach((m) => state.map.removeLayer(m));
  state.markers = {};
  const lookup = byId();
  state.spots.forEach((spot) => {
    const a = lookup[spot.id];
    const cls = a ? (a.data_available ? a.risk_level : 'idle') : 'idle';
    const size = cls === 'red' ? 22 : cls === 'amber' ? 18 : 14;
    const icon = L.divIcon({ className: '', html: `<div class="marker ${cls}"></div>`, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
    const marker = L.marker([spot.lat, spot.lon], { icon, zIndexOffset: cls === 'red' ? 600 : cls === 'amber' ? 300 : 0 }).addTo(state.map);
    const detail = a?.data_available
      ? `<span class="risk-text ${a.risk_level}"><b>${LABELS[a.risk_level]}</b></span> · peak ${a.peak_precipitation_mm_hr} mm/hr at ${hhmm(a.peak_time)}<br>${Math.round(a.risk_ratio * 100)}% of the ${spot.threshold_mm_hr} mm/hr estimated threshold<br>${sparkline(a, spot, 190, 44)}`
      : `Estimated threshold ${spot.threshold_mm_hr} mm/hr<br><span class="muted">${a ? 'No rainfall data for this spot' : 'Not assessed yet'}</span>`;
    marker.bindPopup(`<b>${esc(spot.name)}</b><br><span class="muted">${esc(spot.zone || '')} Delhi</span><br>${detail}`);
    state.markers[spot.id] = marker;
  });
}
function focusSpot(id) { const m = state.markers[id]; if (m) { state.map.setView(m.getLatLng(), 14); m.openPopup(); $('#map').scrollIntoView({ behavior: 'smooth', block: 'center' }); } }

/* ---------- KPIs and table ---------- */
function renderKpis() {
  const avail = state.assessments.filter((a) => a.data_available);
  $('#kpiSpots').textContent = state.spots.length || '—';
  const zones = new Set(state.spots.map((s) => s.zone).filter(Boolean));
  $('#kpiZones').textContent = zones.size ? `across ${zones.size} zones of Delhi` : 'across Delhi';
  $('#kpiRed').textContent = avail.length ? avail.filter((a) => a.risk_level === 'red').length : '—';
  $('#kpiAmber').textContent = avail.length ? avail.filter((a) => a.risk_level === 'amber').length : '—';
  if (avail.length) {
    const top = avail.reduce((m, a) => (a.peak_precipitation_mm_hr > m.peak_precipitation_mm_hr ? a : m));
    $('#kpiPeak').textContent = `${top.peak_precipitation_mm_hr} mm/hr`;
    $('#kpiPeakWhere').textContent = `${top.spot_name}, ${hhmm(top.peak_time)}`;
  } else { $('#kpiPeak').textContent = '—'; $('#kpiPeakWhere').textContent = 'run an assessment'; }
  const judged = state.history.filter((r) => ['true_alarm', 'false_alarm', 'missed_event'].includes(r.outcome));
  const good = judged.filter((r) => r.outcome === 'true_alarm').length;
  $('#kpiAccuracy').textContent = judged.length ? `${good}/${judged.length}` : '—';
  $('#kpiAccuracyNote').textContent = judged.length ? 'alerts matched by recorded rain' : 'no verified alerts yet';
}
function renderTable() {
  const lookup = byId();
  let rows = state.spots.map((s) => ({ s, a: lookup[s.id] }));
  rows.sort((x, y) => (RANK[x.a?.data_available ? x.a.risk_level : 'green'] - RANK[y.a?.data_available ? y.a.risk_level : 'green']) || ((y.a?.risk_ratio || 0) - (x.a?.risk_ratio || 0)) || x.s.name.localeCompare(y.s.name));
  if (state.filter === 'flagged') rows = rows.filter(({ a }) => a?.data_available && a.risk_level !== 'green');
  $('#spotCount').textContent = `${rows.length} of ${state.spots.length}`;
  $('#spotRows').innerHTML = rows.map(({ s, a }) => {
    const ok = a?.data_available;
    const pct = ok ? Math.min(150, Math.round(a.risk_ratio * 100)) : 0;
    const meter = ok
      ? `<span class="meter"><i style="width:${(pct / 150) * 100}%;background:${COLORS[a.risk_level]}"></i><u style="left:${(100 / 150) * 100}%"></u></span><span class="meter-label">${a.peak_precipitation_mm_hr} / ${s.threshold_mm_hr} mm/hr</span>`
      : `<span class="muted">threshold ${s.threshold_mm_hr} mm/hr</span>`;
    const pill = ok ? `<span class="pill ${a.risk_level}">${LABELS[a.risk_level]}</span>` : `<span class="pill">${a ? 'NO DATA' : 'NOT ASSESSED'}</span>`;
    return `<tr data-id="${esc(s.id)}" tabindex="0"><td><b>${esc(s.name)}</b></td><td class="muted">${esc(s.zone || '—')}</td><td>${pill}</td><td>${meter}</td><td>${sparkline(a, s)}</td><td class="muted">${ok ? hhmm(a.peak_time) : '—'}</td></tr>`;
  }).join('') || `<tr><td colspan="6" class="loading">${state.filter === 'flagged' ? 'No flagged locations in this assessment.' : 'No locations available.'}</td></tr>`;
  $$('#spotRows tr[data-id]').forEach((tr) => { tr.onclick = () => focusSpot(tr.dataset.id); tr.onkeydown = (e) => { if (e.key === 'Enter') focusSpot(tr.dataset.id); }; });
}
function renderAll() { renderMarkers(); renderKpis(); renderTable(); }

/* ---------- briefing ---------- */
function setSteps() {
  const p = state.proposal;
  let done = p ? 3 : 0;
  if (p?.status === 'published') done = 5;
  if (p?.status === 'rejected') done = 4;
  if (p?.verified) done = 6;
  $('#steps').innerHTML = ['Forecast', 'Assess', 'Propose', 'Approve', 'Act', 'Verify'].map((v, i) => `<span class="${i < done ? 'done' : i === done ? 'current' : ''}">${i < done ? '✓' : String(i + 1).padStart(2, '0')} ${v}</span>`).join('');
}
function emptyBriefing(title, text, extra = '') {
  $('#proposalArea').innerHTML = `<div class="empty"><h2>${esc(title)}</h2><p>${esc(text)}</p><button class="primary" id="emptyRun">Run assessment</button>${extra}</div>`;
  $('#emptyRun').onclick = () => runAssessment();
}
function renderProposal() {
  setSteps();
  const p = state.proposal;
  if (!p) return emptyBriefing('No assessment yet', 'Run an assessment to compare the rain forecast with each low point and get a draft alert to review.');
  const names = Object.fromEntries(state.spots.map((s) => [s.id, s.name]));
  const flagged = (p.flagged_spots || []).slice().sort((a, b) => RANK[a.risk_level] - RANK[b.risk_level]).map((s) => `<div class="flag-row ${esc(s.risk_level)}"><b>${esc(names[s.spot_id] || s.spot_id)} <span class="risk-text ${esc(s.risk_level)}">${LABELS[s.risk_level] || ''}</span></b><small>Peak ${esc(s.peak_mm_hr)} mm/hr at ${hhmm(s.peak_time)} · ${esc(s.recommended_action)}</small></div>`).join('') || '<div class="flag-row"><small>No location reached the watch level in this window.</small></div>';
  const agent = p.generated_by !== 'fallback';
  const noFlags = !(p.flagged_spots || []).length;
  const trace = p.agent_trace
    ? `<div class="trace"><b>Drafted by ${esc(p.agent_trace.framework)}</b> · ${esc(p.agent_trace.provider)} / ${esc(p.agent_trace.model)}<br>Tools the agent called: ${(p.agent_trace.tools_called || []).map((t) => `<code>${esc(t)}</code>`).join('')}<br>Rain values, risk levels and the Hindi text come from the rule engine, never from the model.</div>`
    : `<div class="trace">${noFlags ? 'Nothing was flagged, so no AI draft was needed. This is the standard all-clear text.' : `Standard template used. ${esc(p.fallback_reason || '')}`}</div>`;
  const delivery = p.delivery_state ? `<div class="delivery">${p.delivery_state === 'local-skipped' ? 'Approved and recorded. No alert channel is connected in local mode, so nothing was sent.' : `Delivery: ${esc(p.delivery_state)}`}${p.delivery_error ? ` · ${esc(p.delivery_error)}` : ''}</div>` : '';
  const canVerify = p.status === 'published' && p.mode === 'replay' && !state.synthetic;
  $('#proposalArea').innerHTML = `<div class="proposal-card">
    <div class="proposal-status"><span class="badge ${esc(p.status)}">${esc(p.status === 'pending' ? 'AWAITING YOUR DECISION' : p.status.toUpperCase())}</span><span class="badge ${agent ? 'agent' : 'fallback'}">${agent ? 'AI AGENT DRAFT' : noFlags ? 'ALL CLEAR' : 'TEMPLATE'}</span></div>
    <h2>Draft alert for ${esc(niceDate(p.window_start))}, ${hhmm(p.window_start)}–${hhmm(p.window_end)}</h2>
    <div class="lang-tabs"><button class="${state.lang === 'english' ? 'active' : ''}" data-lang="english">English</button><button class="${state.lang === 'hindi' ? 'active' : ''}" data-lang="hindi">हिन्दी</button></div>
    <div class="proposal-copy">${esc(p[state.lang === 'english' ? 'english_text' : 'hindi_text'])}</div>
    <div class="reasoning">${esc(p.reasoning)}</div>
    <span class="eyebrow">FLAGGED LOCATIONS AND ACTIONS</span>${flagged}
    <div class="proposal-actions"><button class="approve" id="approveBtn" ${p.status !== 'pending' ? 'disabled' : ''}>Approve and send</button><button class="reject" id="rejectBtn" ${p.status !== 'pending' ? 'disabled' : ''}>Reject</button>${canVerify ? `<button id="verifyBtn" ${p.verified ? 'disabled' : ''}>${p.verified ? 'Checked against recorded rain' : 'Check against recorded rain'}</button>` : ''}<button id="toRouteBtn">Check a route</button></div>
    ${delivery}${trace}</div>`;
  $$('[data-lang]').forEach((b) => { b.onclick = () => { state.lang = b.dataset.lang; renderProposal(); }; });
  $('#approveBtn').onclick = () => transition('approve');
  $('#rejectBtn').onclick = () => transition('reject');
  if ($('#verifyBtn')) $('#verifyBtn').onclick = () => transition('verify');
  $('#toRouteBtn').onclick = () => showTab('route');
}
async function transition(action) {
  try {
    const result = await request(`/proposals/${encodeURIComponent(state.proposal.id)}/${action}`, { method: 'POST' });
    if (action === 'verify') { state.proposal.verified = true; await loadHistory(); toast('Compared with recorded rain. See Track record.'); }
    else { Object.assign(state.proposal, result); toast(action === 'approve' ? 'Alert approved.' : 'Draft rejected. Nothing was sent.'); }
    renderProposal();
  } catch (e) { toast(e.message); }
}
async function runAssessment() {
  const btn = $('#runBtn'); btn.disabled = true;
  const date = $('#dateInput').value;
  busy(state.synthetic ? 'Running storm drill…' : state.mode === 'live' ? 'Reading the live forecast…' : `Replaying ${niceDate(date)}…`, [
    `Fetching hourly rain for ${state.spots.length} low points`, 'Comparing each peak with its threshold', 'AI agent is reading the assessment and past alert outcomes', 'Drafting the alert for your review', 'Still working. A small local model takes a little longer.']);
  try {
    const body = { mode: state.synthetic ? 'replay' : state.mode, synthetic: state.synthetic };
    if (body.mode === 'replay') body.date = date;
    const result = await request('/proposals', { method: 'POST', body: JSON.stringify(body) });
    state.proposal = result; state.assessments = result.assessments || []; state.routes = null; clearRoutes(); $('#routeResult').innerHTML = '';
    const flaggedCount = state.assessments.filter((a) => a.data_available && a.risk_level !== 'green').length;
    $('#mapTitle').textContent = flaggedCount ? `${flaggedCount} low point${flaggedCount > 1 ? 's' : ''} need attention` : 'No low point reached watch level';
    $('#windowLabel').textContent = `${state.synthetic ? 'Synthetic storm drill, not real weather' : state.mode === 'live' ? 'Live forecast' : 'Historical forecast replay'} · ${niceDate(result.window_start)} ${hhmm(result.window_start)}–${hhmm(result.window_end)} IST`;
    renderAll(); renderProposal(); showTab('brief');
    const flagged = state.assessments.filter((a) => a.data_available && a.risk_level !== 'green').map((a) => state.markers[a.spot_id]?.getLatLng()).filter(Boolean);
    if (flagged.length > 1) state.map.fitBounds(L.latLngBounds(flagged).pad(0.25));
  } catch (e) {
    state.proposal = null; setSteps();
    $('#proposalArea').innerHTML = `<div class="empty"><h2>Assessment could not run</h2><p>${esc(e.message)}</p><button class="primary" id="drillBtn">Run a storm drill instead</button></div>`;
    $('#drillBtn').onclick = () => { setMode('replay', true); runAssessment(); };
    toast(e.message);
  } finally { busy(null); btn.disabled = false; }
}

/* ---------- route check ---------- */
function clearRoutes() { state.routeLayers.forEach((l) => state.map.removeLayer(l)); state.routeLayers = []; }
function pin(latlng, letter) { return L.marker(latlng, { icon: L.divIcon({ className: '', html: `<div class="pin"><b>${letter}</b></div>`, iconSize: [26, 26], iconAnchor: [13, 26] }), zIndexOffset: 900 }); }
function drawRoutes() {
  clearRoutes();
  const r = state.routes; if (!r || !state.map) return;
  r.routes.forEach((route, i) => {
    if (i === state.selectedRoute) return;
    state.routeLayers.push(L.polyline(route.path, { color: route.score ? COLORS.red : '#8a9cab', weight: 4, opacity: 0.55, dashArray: '6 8' }).addTo(state.map));
  });
  const sel = r.routes[state.selectedRoute];
  state.routeLayers.push(L.polyline(sel.path, { color: '#07121d', weight: 9, opacity: 0.9 }).addTo(state.map));
  const line = L.polyline(sel.path, { color: sel.score ? COLORS.amber : COLORS.blue, weight: 5 }).addTo(state.map);
  state.routeLayers.push(line, pin(sel.path[0], 'A').addTo(state.map), pin(sel.path[sel.path.length - 1], 'B').addTo(state.map));
  state.map.fitBounds(line.getBounds().pad(0.15));
}
function renderRoutes() {
  const r = state.routes; if (!r) return;
  const rec = r.routes[r.recommended_index], fast = r.routes[r.fastest_index];
  const assessed = state.assessments.length > 0;
  let verdict;
  if (!assessed) verdict = '<div class="verdict warn">No assessment has been run<small>Routes are shown, but nothing is flagged yet. Run an assessment first.</small></div>';
  else if (r.all_routes_flagged) verdict = `<div class="verdict bad">Every route passes a flagged low point<small>Hold this trip or confirm conditions at ${esc(rec.hits.map((h) => h.name).join(', '))} before dispatch.</small></div>`;
  else if (fast.score > 0) verdict = `<div class="verdict warn">Use the suggested route, not the fastest one<small>The fastest route passes ${fast.hits.length} flagged spot${fast.hits.length > 1 ? 's' : ''}. The suggested route avoids them and takes ${r.extra_minutes > 0 ? `${r.extra_minutes} min longer` : 'no longer'}.</small></div>`;
  else verdict = '<div class="verdict ok">The fastest route is clear of flagged spots<small>Clear of flagged spots is not a guarantee the road is dry. Drivers should still report conditions.</small></div>';
  const cards = r.routes.map((route, i) => {
    const tags = [i === r.recommended_index ? '<span class="pill green">SUGGESTED</span>' : '', i === r.fastest_index ? '<span class="pill">FASTEST</span>' : ''].join(' ');
    const hits = route.hits.length ? `<ul>${route.hits.map((h) => `<li><span class="risk-text ${h.risk_level}">${LABELS[h.risk_level]}</span> ${esc(h.name)} <span class="muted">(${h.distance_m} m from route)</span></li>`).join('')}</ul>` : `<div class="clear">${assessed ? 'Clear of flagged spots' : 'Nothing flagged yet'}</div>`;
    return `<article class="route-card ${i === state.selectedRoute ? 'selected' : ''}" data-route="${i}" tabindex="0"><header><b>${route.duration_min} min · ${route.distance_km} km</b><span>${tags}</span></header>${hits}</article>`;
  }).join('');
  $('#routeResult').innerHTML = `${verdict}${cards}<p class="muted" style="font-size:12px">Driving routes from OpenStreetMap (OSRM). A spot counts if it lies within ${r.proximity_m} m of the route. Times ignore traffic.</p>`;
  $$('.route-card').forEach((c) => { c.onclick = () => { state.selectedRoute = Number(c.dataset.route); renderRoutes(); drawRoutes(); }; });
}
async function checkRoute() {
  const from = state.places.find((p) => p.id === $('#fromSel').value), to = state.places.find((p) => p.id === $('#toSel').value);
  if (!from || !to || from.id === to.id) return toast('Choose two different places.');
  const btn = $('#routeBtn'); btn.disabled = true; btn.textContent = 'Checking routes…';
  try {
    const risk = Object.fromEntries(state.assessments.filter((a) => a.data_available).map((a) => [a.spot_id, a.risk_level]));
    state.routes = await request('/route-check', { method: 'POST', body: JSON.stringify({ origin: { lat: from.lat, lon: from.lon }, destination: { lat: to.lat, lon: to.lon }, risk }) });
    state.selectedRoute = state.routes.recommended_index;
    renderRoutes(); drawRoutes();
  } catch (e) { $('#routeResult').innerHTML = `<div class="verdict bad">Route check failed<small>${esc(e.message)}</small></div>`; }
  finally { btn.disabled = false; btn.textContent = 'Check route'; }
}

/* ---------- history ---------- */
async function loadHistory() {
  try { state.history = (await request('/history')).records || []; } catch { state.history = []; }
  const count = (o) => state.history.filter((r) => r.outcome === o).length;
  $('#historyCount').textContent = state.history.length;
  $('#historyStats').innerHTML = [['true_alarm', 'Alert, rain came'], ['false_alarm', 'Alert, no rain'], ['missed_event', 'Missed'], ['no_event', 'Quiet, correct']].map(([k, label]) => `<div class="stat"><b class="outcome ${k}">${count(k)}</b><span>${label}</span></div>`).join('');
  const words = { true_alarm: 'ALERT CONFIRMED', false_alarm: 'FALSE ALARM', missed_event: 'MISSED', no_event: 'QUIET', no_data: 'NO DATA' };
  $('#historyList').innerHTML = state.history.length ? state.history.slice().reverse().slice(0, 80).map((r) => `<article class="history-record"><strong><span>${esc(r.spot_name)}</span><span class="outcome ${esc(r.outcome)}">${words[r.outcome] || esc(r.outcome)}</span></strong><p>${esc(niceDate(r.assessment_window_start))} · forecast ${esc(r.forecast_peak_mm_hr)} mm/hr, recorded ${r.observed_peak_mm_hr == null ? 'unavailable' : `${esc(r.observed_peak_mm_hr)} mm/hr`}${r.threshold_adjusted ? ` · threshold ${esc(r.threshold_before)} → ${esc(r.threshold_after)} mm/hr` : ''}</p></article>`).join('') : '<div class="empty-small">Nothing verified yet. Replay a past day, approve the alert, then check it against recorded rain.</div>';
  renderKpis();
}

/* ---------- wiring ---------- */
function showTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $('#briefView').hidden = name !== 'brief'; $('#routeView').hidden = name !== 'route'; $('#historyView').hidden = name !== 'history';
  if (name === 'history') loadHistory();
}
function setMode(mode, synthetic = false) {
  state.mode = mode; state.synthetic = synthetic;
  $('#liveBtn').classList.toggle('active', mode === 'live'); $('#replayBtn').classList.toggle('active', mode === 'replay');
  $('#replayControls').hidden = mode !== 'replay';
  if (!$('#dateInput').value) $('#dateInput').value = '2023-07-09';
  $$('.replay-controls .chip').forEach((c) => c.classList.toggle('active', synthetic ? !!c.dataset.synthetic : c.dataset.date === $('#dateInput').value));
}
async function subscribe() {
  const email = $('#emailInput').value.trim(); if (!email) return toast('Enter an email address first.');
  try { const result = await request('/subscribe', { method: 'POST', body: JSON.stringify({ email }) }); $('#subscribeMsg').textContent = result.message; }
  catch (e) { $('#subscribeMsg').textContent = e.message; }
}
async function start() {
  initMap(); setMode('live'); renderProposal();
  $('#dateInput').max = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  $('#dateInput').onchange = () => setMode('replay');
  $('#liveBtn').onclick = () => setMode('live'); $('#replayBtn').onclick = () => setMode('replay');
  $$('.replay-controls .chip').forEach((c) => { c.onclick = () => { if (c.dataset.date) $('#dateInput').value = c.dataset.date; setMode('replay', !!c.dataset.synthetic); runAssessment(); }; });
  $('#runBtn').onclick = () => runAssessment(); $('#subscribeBtn').onclick = subscribe; $('#routeBtn').onclick = checkRoute;
  $('#swapBtn').onclick = () => { const a = $('#fromSel').value; $('#fromSel').value = $('#toSel').value; $('#toSel').value = a; };
  $$('.tab').forEach((t) => { t.onclick = () => showTab(t.dataset.tab); });
  $$('[data-filter]').forEach((c) => { c.onclick = () => { state.filter = c.dataset.filter; $$('[data-filter]').forEach((x) => x.classList.toggle('active', x === c)); renderTable(); }; });
  try {
    const [health, spots, places] = await Promise.all([request('/health'), request('/spots'), request('/places')]);
    state.spots = spots.spots || []; state.places = places.places || [];
    $('#sourceStatus').innerHTML = `<i style="background:${health.openmeteo_reachable ? COLORS.green : COLORS.red}"></i> ${health.openmeteo_reachable ? 'WEATHER ONLINE' : 'WEATHER OFFLINE'} · STRANDS AGENT · ${esc(String(health.agent_provider || '').toUpperCase())}`;
    const opts = state.places.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    $('#fromSel').innerHTML = opts; $('#toSel').innerHTML = opts; $('#fromSel').value = 'nzm'; $('#toSel').value = 'du_north';
    renderAll(); await loadHistory();
  } catch (e) {
    $('#sourceStatus').innerHTML = `<i style="background:${COLORS.red}"></i> BACKEND UNAVAILABLE`;
    $('#spotRows').innerHTML = `<tr><td colspan="6" class="loading">Cannot reach the backend: ${esc(e.message)}. Start the API, then reload.</td></tr>`;
    return;
  }
  const q = new URLSearchParams(location.search);
  if (q.get('replay')) { if (q.get('replay') === 'drill') setMode('replay', true); else { $('#dateInput').value = q.get('replay'); setMode('replay'); } await runAssessment(); if (q.get('route')) { showTab('route'); await checkRoute(); } }
}
start();
