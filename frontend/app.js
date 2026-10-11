const API = `${window.SENTINEL_CONFIG?.apiBaseUrl || ''}/api`;
const $ = (s) => document.querySelector(s);
const state = { mode: 'live', date: '', spots: [], assessments: [], proposal: null, map: null, markers: [], lang: 'english' };
const riskColor = (level) => ({ green: '#54d6a0', amber: '#f2b94b', red: '#ff6570' }[level] || '#8093a1');
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  let body; try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}
function toast(message) { const el = $('#toast'); el.textContent = message; el.style.display = 'block'; clearTimeout(toast.timer); toast.timer = setTimeout(() => el.style.display = 'none', 5000); }
function setMode(mode) { state.mode = mode; $('#liveBtn').classList.toggle('active', mode === 'live'); $('#replayBtn').classList.toggle('active', mode === 'replay'); $('#dateInput').hidden = mode !== 'replay'; $('#replayBanner').hidden = mode !== 'replay'; $('#dateInput').value ||= new Date(Date.now() - 86400000).toISOString().slice(0, 10); state.date = $('#dateInput').value; }
function initMap() {
  if (!window.L) { $('#sourceStatus').textContent = 'MAP UNAVAILABLE'; return; }
  state.map = L.map('map', { zoomControl: false, scrollWheelZoom: false }).setView([28.625, 77.225], 11);
  L.control.zoom({ position: 'bottomright' }).addTo(state.map);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19 }).addTo(state.map);
}
function markerFor(spot, assessment) {
  const cls = assessment?.data_available ? assessment.risk_level : assessment ? 'unavailable' : 'green';
  const icon = L.divIcon({ className: '', html: `<div class="marker ${cls}" aria-label="${esc(cls)} risk"></div>`, iconSize: [18,18], iconAnchor: [9,9] });
  const marker = L.marker([spot.lat, spot.lon], { icon }).addTo(state.map);
  const details = assessment ? `<br>Peak: ${assessment.data_available ? `${assessment.peak_precipitation_mm_hr} mm/hr` : 'unavailable'}<br>Risk ratio: ${assessment.data_available ? assessment.risk_ratio : '—'}<br>Source: ${esc(assessment.data_source)}` : '<br>Awaiting assessment';
  let chart = '';
  if (assessment?.data_available && assessment.hourly_series?.length) {
    const vals = assessment.hourly_series, max = Math.max(spot.threshold_mm_hr, ...vals, 1), x = i => 8 + i * (184 / Math.max(1, vals.length - 1)), y = v => 58 - v / max * 48;
    const points = vals.map((v, i) => `${x(i)},${y(v)}`).join(' '), thresholdY = y(spot.threshold_mm_hr);
    chart = `<svg width="200" height="70" viewBox="0 0 200 70" role="img" aria-label="Hourly precipitation chart; dashed line is estimated threshold"><line x1="5" y1="${thresholdY}" x2="195" y2="${thresholdY}" stroke="#f2b94b" stroke-dasharray="4 3"/><polyline points="${points}" fill="none" stroke="${riskColor(assessment.risk_level)}" stroke-width="2"/><text x="5" y="68" fill="#91a4b0" font-size="8">HOURLY MM/HR · DASHED = THRESHOLD</text></svg>`;
  }
  marker.bindPopup(`<b>${esc(spot.name)}</b><br>Estimated threshold: ${spot.threshold_mm_hr} mm/hr${details}<br><small>Coordinates and threshold unverified</small>${chart}`);
  return marker;
}
function renderSpots() {
  const byId = Object.fromEntries(state.assessments.map(a => [a.spot_id, a]));
  $('#spotCount').textContent = state.spots.length;
  $('#spotGrid').innerHTML = state.spots.map(s => {
    const a = byId[s.id], level = a?.risk_level || '—', label = a ? (a.data_available ? level.toUpperCase() : 'UNAVAILABLE') : 'AWAITING';
    return `<article class="spot-card" tabindex="0" data-id="${esc(s.id)}"><div class="spot-top"><strong>${esc(s.name)}</strong><span class="risk-pill ${a?.data_available ? a.risk_level : ''}">${label}</span></div><div class="spot-metrics">THRESHOLD ${s.threshold_mm_hr} MM/HR &nbsp;·&nbsp; PEAK ${a?.data_available ? `${a.peak_precipitation_mm_hr} MM/HR` : '—'}<br>${a ? `SOURCE ${esc(a.data_source)}` : 'NOT ASSESSED'}</div></article>`;
  }).join('') || '<div class="loading">No locations available.</div>';
  document.querySelectorAll('.spot-card').forEach(card => {
    const open = () => { const m = state.markers.find(x => x.spotId === card.dataset.id); if (m) { state.map.setView(m.getLatLng(), 14); m.openPopup(); } };
    card.addEventListener('click', open); card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });
  if (state.map) { state.markers.forEach(m => state.map.removeLayer(m)); state.markers = state.spots.map(s => { const m = markerFor(s, byId[s.id]); m.spotId = s.id; return m; }); }
}
function setSteps(proposal) {
  const values = ['Forecast','Assess','Propose','Approve','Act','Verify'];
  let completed = proposal ? 3 : state.assessments.length ? 2 : 0;
  if (proposal?.status === 'approved') completed = 4;
  if (proposal?.status === 'published') completed = 5;
  if (proposal?.verified) completed = 6;
  $('#steps').innerHTML = values.map((v,i) => `<span class="${i < completed ? 'done' : i === completed ? 'current' : ''}">${String(i+1).padStart(2,'0')} ${v}</span>`).join('');
}
function proposalMarkup(p) {
  const copy = p[state.lang === 'english' ? 'english_text' : 'hindi_text'];
  const flagged = (p.flagged_spots || []).map(s => `<div class="flag-row"><b>${esc(state.spots.find(x => x.id === s.spot_id)?.name || s.spot_id)} <span class="risk-text ${esc(s.risk_level)}">${esc(s.risk_level.toUpperCase())}</span></b><small>Peak ${esc(s.peak_mm_hr)} mm/hr at ${esc(s.peak_time)}<br>${esc(s.recommended_action)}</small></div>`).join('') || '<div class="flag-row"><small>No locations crossed the estimated watch threshold.</small></div>';
  const status = p.status.toUpperCase().replaceAll('_',' ');
  return `<div class="proposal-card"><div class="proposal-status"><span class="badge">${esc(status)}</span><span class="badge ${p.generated_by === 'fallback' ? 'fallback' : ''}">${p.generated_by === 'fallback' ? 'TEMPLATE FALLBACK' : 'STRANDS AGENT'}</span></div><h2>Dispatcher proposal</h2><div class="lang-tabs"><button class="${state.lang === 'english' ? 'active' : ''}" data-lang="english">ENGLISH</button><button class="${state.lang === 'hindi' ? 'active' : ''}" data-lang="hindi">हिन्दी</button></div><div class="proposal-copy">${esc(copy)}</div><div class="reasoning">${esc(p.reasoning)}<br><br>Window: ${esc(p.window_start)} – ${esc(p.window_end)}</div><h3 class="eyebrow">FLAGGED LOCATIONS & ACTIONS</h3>${flagged}<div class="proposal-actions"><button class="approve" id="approveBtn" ${p.status !== 'pending' ? 'disabled' : ''}>Approve & publish</button><button class="reject" id="rejectBtn" ${p.status !== 'pending' ? 'disabled' : ''}>Reject</button>${p.status === 'published' ? `<button id="verifyBtn" ${state.mode !== 'replay' ? 'disabled title="Replay historical assessments only"' : ''}>Verify rainfall</button>` : ''}</div>${p.agent_trace ? `<div class="delivery">Drafted by ${esc(p.agent_trace.framework)} · ${esc(p.agent_trace.provider)} / ${esc(p.agent_trace.model)}<br>Tools called: ${esc((p.agent_trace.tools_called || []).join(' → '))}<br>Numbers, risk levels and Hindi text come from the deterministic engine, not the model.</div>` : ''}${p.fallback_reason ? `<div class="delivery">Fallback reason: ${esc(p.fallback_reason)}</div>` : ''}${p.delivery_state ? `<div class="delivery">Delivery: ${esc(p.delivery_state)}${p.delivery_error ? ` · ${esc(p.delivery_error)}` : ''}</div>` : ''}${p.verified ? '<div class="delivery">Rainfall verification recorded. This does not confirm flooding.</div>' : ''}</div>`;
}
function renderProposal() {
  if (!state.proposal) return;
  $('#proposalArea').innerHTML = proposalMarkup(state.proposal);
  setSteps(state.proposal);
  document.querySelectorAll('[data-lang]').forEach(b => b.onclick = () => { state.lang = b.dataset.lang; renderProposal(); });
  $('#approveBtn')?.addEventListener('click', () => transition('approve'));
  $('#rejectBtn')?.addEventListener('click', () => transition('reject'));
  $('#verifyBtn')?.addEventListener('click', () => transition('verify'));
}
async function transition(action) {
  const id = encodeURIComponent(state.proposal.id);
  try {
    const result = await request(`/proposals/${id}/${action}`, { method: 'POST' });
    if (action === 'verify') { state.proposal.verified = true; toast('Rainfall comparison recorded; flood status remains unverified.'); await loadHistory(); }
    else { Object.assign(state.proposal, result); toast(result.message || `Proposal ${action} complete.`); }
    renderProposal();
  } catch (e) { toast(e.message); }
}
async function runAssessment(synthetic = false) {
  const button = $('#runBtn'); button.disabled = true; button.textContent = 'Assessing…';
  try {
    const result = await request('/proposals', { method: 'POST', body: JSON.stringify({ mode: state.mode, date: state.mode === 'replay' ? $('#dateInput').value : undefined, synthetic }) });
    state.proposal = result;
    state.assessments = result.assessments || [];
    const available = state.assessments.filter(x => x.data_available).length;
    $('#windowLabel').textContent = `${result.window_start} — ${result.window_end} · ${result.provider_used}`;
    $('#sourceStatus').innerHTML = `<i></i> ${synthetic ? 'SYNTHETIC FIXTURE' : esc(result.provider_used).toUpperCase()}`;
    renderSpots(); setSteps(null);
    if (!available) {
      $('#proposalArea').innerHTML = `<div class="empty"><h2>Rainfall data unavailable</h2><p>${esc(result.error || 'No usable values were returned. Missing data is not counted as zero rainfall.')}</p><button id="fixtureBtn" class="primary">Run labelled synthetic demo</button></div>`;
      state.proposal = null;
      $('#fixtureBtn').onclick = () => runAssessment(true); return;
    }
    renderProposal();
    toast(synthetic ? 'Synthetic fixture assessment complete.' : 'Assessment and dispatcher proposal ready.');
  } catch (e) { $('#proposalArea').innerHTML = `<div class="empty"><h2>Assessment failed</h2><p>${esc(e.message)}</p><button id="fixtureBtn" class="primary">Run labelled synthetic demo</button></div>`; $('#fixtureBtn').onclick = () => runAssessment(true); toast(e.message); }
  finally { button.disabled = false; button.innerHTML = 'Run assessment <span>↗</span>'; }
}
async function loadHistory() {
  try {
    const result = await request('/history'); const records = result.records || [];
    $('#historyCount').textContent = records.length;
    $('#historyList').innerHTML = records.length ? records.map(r => `<article class="history-record"><strong>${esc(r.spot_name)} · ${esc(r.outcome.toUpperCase())}</strong><p>Forecast ${esc(r.forecast_peak_mm_hr)} mm/hr (${esc(r.forecast_source)}) → observed ${r.observed_peak_mm_hr == null ? 'unavailable' : `${esc(r.observed_peak_mm_hr)} mm/hr`} (${esc(r.observation_source)})<br>Estimated threshold ${esc(r.threshold_before)} → ${esc(r.threshold_after)} mm/hr<br>Rainfall based · no flood observations</p></article>`).join('') : '<div class="empty-small">No completed verifications yet.</div>';
  } catch (e) { $('#historyList').textContent = e.message; }
}
async function subscribe() {
  const email = $('#emailInput').value.trim(); if (!email) return toast('Enter an email address first.');
  $('#subscribeMsg').textContent = 'Submitting…';
  try { const result = await request('/subscribe', { method: 'POST', body: JSON.stringify({ email }) }); $('#subscribeMsg').textContent = result.message; toast(result.message); }
  catch (e) { $('#subscribeMsg').textContent = e.message; }
}
async function start() {
  initMap(); setMode('live'); $('#dateInput').addEventListener('change', e => state.date = e.target.value);
  $('#liveBtn').onclick = () => setMode('live'); $('#replayBtn').onclick = () => setMode('replay');
  $('#runBtn').onclick = () => runAssessment(); $('#emptyRun').onclick = () => runAssessment(); $('#subscribeBtn').onclick = subscribe;
  document.querySelectorAll('.tab').forEach(tab => tab.onclick = () => { document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === tab)); $('#dispatchView').hidden = tab.dataset.tab !== 'dispatch'; $('#historyView').hidden = tab.dataset.tab !== 'history'; if (tab.dataset.tab === 'history') loadHistory(); });
  try { const [health, spots] = await Promise.all([request('/health'), request('/spots')]); state.spots = spots.spots || []; $('#sourceStatus').innerHTML = `<i></i> ${health.storage_mode.toUpperCase()} STORAGE · ${health.openmeteo_reachable ? 'OPEN-METEO READY' : 'OPEN-METEO OFFLINE'}${health.agent_provider ? ` · STRANDS AGENT (${esc(health.agent_provider.toUpperCase())})` : ''}`; renderSpots(); }
  catch (e) { $('#sourceStatus').textContent = 'API UNAVAILABLE'; $('#spotGrid').innerHTML = `<div class="loading">Backend unavailable: ${esc(e.message)}. Start the local API, then reload.</div>`; }
}
start();
