/* Possible future improvement (not done): MapLibre GL JS for the map and GeoLibre tools for the editing functions; this file uses Leaflet. */
'use strict';
/* Community address app - front end (Leaflet + plain JS). The backend does all computing; this file only draws and asks. */

const $ = (s, r = document) => r.querySelector(s);
const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n) ? '-' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));

async function api(path, body, method) {
  const headers = AuthUI.headers();
  const opt = body === undefined && !method ? { headers } : { method: method || 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) };
  const r = await fetch('api' + path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) { if(r.status===401)AuthUI.expired(); const d = data && data.detail; const err = new Error((d && d.message) || (typeof d === 'string' ? d : '') || r.statusText); err.status = r.status; err.detail = d; throw err; }
  return data;
}

const S = { project: null, network: null, addresses: null, roads: null, baseStreets: null, tab: 'ids', mode: null, sel: null,
            idColor: 'id', polling: false, labels: true, planAll: false };
const CLIENT = (() => { try { let c = localStorage.getItem('addr-client'); if (!c) { c = Math.random().toString(36).slice(2, 10); localStorage.setItem('addr-client', c); } return c; } catch (e) { return 'tab' + Math.random().toString(36).slice(2, 8); } })();
const user = () => '';

/* ---------------------------------------------------------------- map */
const map = L.map('map', { preferCanvas: true, maxZoom: 21 }).setView([5.3, 10.2], 13);
map.on('dragstart zoomstart', () => { S.fitted = true; S.fittedRoads = true; });                // the user moved the map: it is theirs from now on
const hybrid = L.tileLayer('https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', { maxZoom: 21, maxNativeZoom: 20, attribution: 'Imagery &copy; Google' }).addTo(map);
const osmTiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 21, maxNativeZoom: 19, attribution: '&copy; OpenStreetMap' });
L.control.layers({ Satellite: hybrid, OpenStreetMap: osmTiles }, {}, { position: 'topleft' }).addTo(map);
/* opacity slider of the background map inside the layer control */
(() => {
  const box = document.querySelector('.leaflet-control-layers-list'); if (!box) return;
  let v = 100; try { v = +(localStorage.getItem('addr-bg-opacity') || 100); } catch (e) { /* no storage */ }
  const apply = (x) => { hybrid.setOpacity(x / 100); osmTiles.setOpacity(x / 100); const lbl = box.querySelector('.bg-op-val'); if (lbl) lbl.textContent = `${x}%`; };
  const row = document.createElement('div'); row.className = 'bg-opacity';
  row.innerHTML = I18n.html`<label>Background opacity <span class="bg-op-val">${v}%</span></label><input type="range" min="0" max="100" step="5" value="${v}">`;
  box.appendChild(row);
  L.DomEvent.disableClickPropagation(row); L.DomEvent.disableScrollPropagation(row);
  row.querySelector('input').addEventListener('input', (e) => { const x = +e.target.value; apply(x); try { localStorage.setItem('addr-bg-opacity', String(x)); } catch (err) { /* ignore */ } });
  apply(v);
})();
const G = {};
['area', 'points', 'ref', 'base', 'net', 'cuts', 'labels', 'hl', 'roadEdges', 'roadNodes', 'houses', 'signs', 'access', 'junc', 'refnodes', 'refhover', 'outside'].forEach((k) => { G[k] = L.layerGroup(); });
const TAB_LAYERS = ['outside', 'junc', 'base', 'net', 'cuts', 'labels', 'roadEdges', 'roadNodes', 'houses', 'signs', 'access'];
const clearTab = () => { TAB_LAYERS.forEach((k) => { G[k].clearLayers(); map.removeLayer(G[k]); }); G.hl.clearLayers(); };
map.createPane('workareas'); map.getPane('workareas').style.zIndex = 350;
const areaLayer = L.layerGroup().addTo(map);
const AREA_LABELS = { unreviewed: 'Not reviewed', in_progress: 'In progress', reviewed: 'Reviewed', needs_review: 'Needs review' };
const AREA_COLORS = { unreviewed: '#78909c', in_progress: '#1976d2', reviewed: '#2e7d32', needs_review: '#ef6c00' };
function activeWorkArea() { return S.project?.work_areas?.features?.find(f => f.properties.id === S.activeArea); }
function inWorkArea(f) { const area = activeWorkArea(); return !area || L.geoJSON(f).getBounds().intersects(L.geoJSON(area).getBounds()); }
function chooseWorkArea(id) {
  S.activeArea = id || null;
  const area = activeWorkArea();
  if (area) map.fitBounds(L.geoJSON(area).getBounds().pad(.1), { maxZoom: 19, animate: false });
  renderWorkAreas(); if (S.tab === 'roads') drawTab();
}
function renderWorkAreas() {
  const box = $('#workarea-box'); if (!box) return;
  areaLayer.clearLayers();
  if (!S.project || S.locked) { box.hidden = true; return; }
  box.hidden = false;
  const cells = S.project.work_areas?.features || [], area = activeWorkArea();
  if (cells.length && S.showAreas !== false) {
    L.geoJSON({ type: 'FeatureCollection', features: cells }, { pane: 'workareas',
      style: f => ({ color: AREA_COLORS[f.properties.status], weight: f.properties.id === S.activeArea ? 4 : 1, fillOpacity: f.properties.id === S.activeArea ? .12 : .025 }),
      onEachFeature: (f, layer) => { layer.bindTooltip(`${f.properties.id}: ${I18n.t(AREA_LABELS[f.properties.status])}`, { sticky: true });
        layer.on('click', e => { if (S.mode || S.draw || S.cut) return; L.DomEvent.stopPropagation(e); chooseWorkArea(f.properties.id); }); }
    }).addTo(areaLayer);
  }
  const counts = Object.entries(AREA_LABELS).map(([s, label]) => `${cells.filter(f => f.properties.status === s).length} ${I18n.t(label.toLowerCase())}`).join(' · ');
  box.innerHTML = I18n.html`<details ${S.areaBoxOpen ? 'open' : ''}><summary>Work areas${area ? ' · ' + area.properties.id : ''}</summary>${cells.length ? I18n.html`
    <p class="muted">${fmt(S.project.work_areas.cell_m)} m grid · ${counts}</p><label><input type="checkbox" id="areas-visible" ${S.showAreas !== false ? 'checked' : ''}> Show grid</label>
    <select id="area-select"><option value="">Whole map</option>${cells.map(f => I18n.html`<option value="${f.properties.id}" ${f.properties.id === S.activeArea ? 'selected' : ''}>${f.properties.id} · ${I18n.t(AREA_LABELS[f.properties.status])}</option>`).join('')}</select>
    ${area ? I18n.html`<label>Progress <select id="area-status">${Object.entries(AREA_LABELS).map(([s, label]) => I18n.html`<option value="${s}" ${area.properties.status === s ? 'selected' : ''}>${I18n.t(label)}</option>`).join('')}</select></label>
      <textarea id="area-note" maxlength="2000" placeholder="What was checked? What is uncertain?">${esc(area.properties.note || '')}</textarea><button id="area-save">Save area progress</button>` : ''}
    <p class="muted">Focus highlights an area and narrows road selection. Roads crossing its boundary stay whole. Progress is your review record, not automatic verification.</p>` : I18n.html`
    <p>Divide this project's area into a review grid.</p><label>Cell size <select id="area-size"><option value="auto">Automatic</option>${[100,250,500,1000,2000].map(m=>I18n.html`<option value="${m}">${m} m</option>`).join('')}</select></label><button id="areas-create">Create work areas</button>`}</details>`;
  box.querySelector('details').ontoggle = e => { S.areaBoxOpen = e.target.open; };
}
$('#workarea-box').addEventListener('change', e => {
  if (e.target.id === 'area-select') chooseWorkArea(e.target.value);
  if (e.target.id === 'areas-visible') { S.showAreas = e.target.checked; renderWorkAreas(); }
});
$('#workarea-box').addEventListener('click', async e => {
  try {
    if (e.target.id === 'areas-create') { await api('/work-areas/grid', { cell_m: $('#area-size').value, revision: S.rev }); S.areaBoxOpen = true; await refreshProject(); }
    if (e.target.id === 'area-save') { await api('/work-areas/status', { id: S.activeArea, status: $('#area-status').value, note: $('#area-note').value, revision: S.rev }); await refreshProject(); toast('Area progress saved'); }
  } catch (error) { toast(error.message, 6000); }
});
const hueRaw = (id) => `hsl(${Math.round(((id * 0.61803398875) % 1) * 360)},70%,${id % 2 ? 45 : 38}%)`;
/* street colours: streets that meet at a node never get the same or a similar colour (greedy colouring over a palette of well separated colours) */
const PALETTE = (() => { const p = []; for (let k = 0; k < 90; k++) p.push({ h: Math.round((k * 137.508) % 360), l: [36, 48, 58][k % 3], s: [82, 66][(k >> 1) % 2] }); return p; })();   // golden-angle hues, 3 lightnesses, 2 saturations
const hue = (id) => (S.colorOf && S.colorOf[id]) || hueRaw(id);
function colorDistance(a, b) { const dh = Math.min(Math.abs(a.h - b.h), 360 - Math.abs(a.h - b.h)) / 180; return dh + 0.6 * Math.abs(a.l - b.l) / 22 + 0.3 * Math.abs((a.s || 0) - (b.s || 0)) / 16; }
function ensureColors() {
  if (!S.network || S.colorsFor === S.network) return;
  const adj = {};
  (S.network.junctions ? S.network.junctions.features : []).forEach((f) => {
    const ids = [...new Set(f.properties.spokes.map((s) => s.id))];
    ids.forEach((a) => { adj[a] = adj[a] || new Set(); ids.forEach((b) => { if (a !== b) adj[a].add(b); }); });
  });
  const all = S.network.streets.features.map((f) => f.properties.id);
  all.sort((a, b) => ((adj[b] ? adj[b].size : 0) - (adj[a] ? adj[a].size : 0)) || a - b);       // the most connected streets choose first
  const pick = {}, color = {}, usage = new Array(PALETTE.length).fill(0);
  all.forEach((id) => {
    const near = [...(adj[id] || [])];
    const used = near.filter((n) => pick[n] !== undefined).map((n) => PALETTE[pick[n]]);
    const two = new Set(); near.forEach((n) => (adj[n] ? adj[n] : []).forEach((m) => { if (m !== id && pick[m] !== undefined) two.add(pick[m]); }));      // streets two steps away
    let best = 0, bs = 1e9;
    PALETTE.forEach((c, i) => {
      const dmin = used.length ? Math.min(...used.map((u) => colorDistance(c, u))) : 9;
      // neighbours must look different; among the colours that do, take the least used one (and avoid the colours of streets two steps away)
      const score = (dmin < 0.5 ? 1000 * (0.5 - dmin) : 0) + usage[i] * 2 + (two.has(i) ? 5 : 0) - dmin * 0.1;
      if (score < bs) { bs = score; best = i; }
    });
    pick[id] = best; usage[best] += 1; const c = PALETTE[best]; color[id] = `hsl(${c.h},${c.s}%,${c.l}%)`;
  });
  S.colorOf = color; S.colorsFor = S.network;
}
const STATUS = { named: '#2e7d32', derived: '#f9a825', derived2: '#ef6c00', planned: '#d50000', uncovered: '#7b1fa2', other: '#9e9e9e' };
const statusColor = (p) => (p.status === 'derived' && p.level === 2 ? STATUS.derived2 : STATUS[p.status] || STATUS.other);

function toast(msg, ms = 2800) { const t = $('#toast'); t.textContent = I18n.t(msg); t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, ms); }
function banner(text, mode) { const b = $('#banner'); if (!text) { b.hidden = true; return; } b.hidden = false; b.className = mode ? 'mode' : ''; b.innerHTML = esc(I18n.t(text)) + (mode && !S.locked ? I18n.htmlText(' <button id="banner-cancel">Cancel</button>') : ''); const c = $('#banner-cancel'); if (c) c.onclick = () => (S.draw ? stopDraw() : S.cut ? ACT['cut-cancel']() : setMode(null)); }

/* ---------------------------------------------------------------- project / header */
function renderSaveState() {
  const st=S.project.status,when=t=>t.replace('T',' ').slice(0,16);
  $('#save-state').textContent=(st.last_edit ? I18n.msg`last edit ${when(st.last_edit.ts)}` : I18n.t('no edits yet'))+' · '+I18n.msg`checkpoint "${st.last_label}" ${when(st.last_saved)}`;
}
async function refreshProject() {
  S.project = await api('/project');
  const p = S.project, st = p.status;
  updateLock();
  $('#project-name').textContent = p.name + (p.version ? ` · v${p.version}` : '');
  renderSaveState();
  if (S.rev !== undefined && S.rev !== st.revision) { S.baseStreets = null; S.refNodes = null; S.edgeData = null; S.rawBuildings = null; S.pointRanks = null; S.outsideNet = null; }      // anything edited: every cached layer is reloaded
  S.rev = st.revision; S.netHash = p.network_hash;
  $('#btn-undo').disabled = !st.can_undo; $('#btn-redo').disabled = !st.can_redo;
  $('#tabs').querySelectorAll('button').forEach((b) => { b.disabled = !p.reference && b.dataset.tab !== 'roads'; b.classList.toggle('active', b.dataset.tab === S.tab); });
  const bits = [];
  if (p.stale && S.polling) bits.push('updating the street ids...'); else if (p.addresses_stale && S.tab === 'addresses') bits.push('updating the addresses...');
  if (!p.reference) bits.push('choose the reference point');
  $('#job').textContent = bits.map(I18n.t).join(' · ');
  autoCompute();
  updateStaleBar();
  updateWarnBadge();
  renderWorkAreas();
  return p;
}
function allWarnings() {
  const out = [];
  const p = S.project;
  return [...out, ...((S.network && S.network.warnings) || []), ...((S.addresses && S.addresses.warnings) || [])];
}
function updateWarnBadge() { const n = allWarnings().length; const c = $('#warn-count'); c.textContent = n; $('#warn-count2').textContent = n; $('#btn-warnings').style.color = n ? 'var(--warn)' : ''; renderWarnings(); }

/* ---------------------------------------------------------------- static layers: area, points, reference */
async function loadStaticLayers() {
  G.area.clearLayers(); G.points.clearLayers();
  const has = S.project.has;
  if (has.area) { const a = await api('/layers/area'); L.geoJSON(a, { style: { color: '#00897b', weight: 2, fill: false, dashArray: '6 5' }, interactive: false }).addTo(G.area); G.area.addTo(map); }
  if (has.points) {
    const pts = await api('/layers/points');
    S.pointsCount = pts.features.length; S.pointsData = pts;
    L.geoJSON(pts, { pointToLayer: (f, ll) => sqMarker(ll),
      onEachFeature: (f, l) => l.bindTooltip(esc(Object.values(f.properties).filter(Boolean).slice(0, 2).join(' · '))) }).addTo(G.points);
    G.points.addTo(map);
  }
}
function drawReference() {
  const r = S.project.reference; if (!r || S.locked) return;
  G.ref.clearLayers();                // while it is being confirmed or chosen, only the node being asked about is drawn
  refMarkers(r).forEach((m) => m.addTo(G.ref));
  G.ref.addTo(map);
}
/* the reference point is always a node of the road network, drawn so it cannot be missed: pulsing rings, a dot exactly on the node, a star and a label;
   `asking` (while it is being chosen or confirmed) makes it much bigger and brighter */
function refPin(r, asking, label) {
  const html = I18n.html`<div class="refpin${asking ? ' asking' : ''}"><span class="ring r1"></span><span class="ring r2"></span><svg class="star" viewBox="-12 -12 24 24"><polygon points="0,-11 3.2,-3.6 11,-3.4 4.8,1.6 7,9.4 0,5 -7,9.4 -4.8,1.6 -11,-3.4 -3.2,-3.6"/></svg>${label === null ? '' : I18n.html`<span class="lbl">${I18n.t(label || 'REFERENCE POINT')}</span>`}</div>`;
  return L.marker([r.lat, r.lon], { icon: L.divIcon({ className: '', html, iconSize: [0, 0] }), keyboard: false, zIndexOffset: 5000 })
    .bindTooltip(`Reference point (node ${r.node}): the numbering starts here`, { direction: 'bottom', offset: [0, 14] });
}
function refMarkers(r, asking) { return [refPin(r, !!asking, asking ? undefined : null)]; }       // the label only while it is being chosen or confirmed

/* ---------------------------------------------------------------- modes (reference / road point / connect) */
function setMode(kind, data) {
  S.mode = kind ? { kind, data } : null;
  map.getContainer().style.cursor = kind ? 'crosshair' : '';
  const text = { reference: 'Move over the map: the highlighted node is where the reference point goes. Click to choose it - the numbering starts from that node.',
                 access: I18n.msg`Click a road next to house ${data && data.id}: its number will be taken from that point.`,
                 connect: 'Click the existing road or node this end must connect to.',
                 cut: 'Click two positions on the selected road. Only the red preview between them will be removed.', draw: '' }[kind];
  banner(kind && kind !== 'reference' ? text : '', !!kind);
  if (kind === 'reference') showRefNodes(); else hideRefNodes();
}
/* while the reference point is chosen: every node of the drivable streets is shown and the nearest one is highlighted; a click takes that node */
async function showRefNodes() {
  if (!S.refNodes) S.refNodes = await api('/base/nodes');
  if (!S.mode || S.mode.kind !== 'reference') return;
  G.refnodes.clearLayers();
  S.refNodes.forEach(([id, lon, lat]) => L.circleMarker([lat, lon], { radius: 3, color: '#4a148c', weight: 1, fillColor: '#fff', fillOpacity: 1, interactive: false }).addTo(G.refnodes));
  G.refnodes.addTo(map);
}
function hideRefNodes() { G.refnodes.clearLayers(); G.refhover.clearLayers(); S.hoverNode = null; }
function nearestNode(ll) {
  if (!S.refNodes) return null;
  const k = Math.cos(ll.lat * Math.PI / 180); let best = null, bd = 1e18;
  for (const n of S.refNodes) { const dx = (n[1] - ll.lng) * k, dy = n[2] - ll.lat, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = n; } }
  return best;
}
map.on('dblclick', (e) => { if (S.draw && S.draw.mode !== 'reshape' && S.draw.mode !== 'route' && S.draw.tool === 'points' && !S.draw.saving && S.draw.pts.length >= 3) { S.draw.pts.pop(); S.draw.snaps.pop(); finishDraw(); } });
map.on('mousemove', (e) => {
  if (!S.mode || S.mode.kind !== 'reference') return;
  const n = nearestNode(e.latlng); S.hoverNode = n; G.refhover.clearLayers();
  if (n) { refPin({ lat: n[2], lon: n[1], node: n[0] }, true, 'CLICK TO CHOOSE THIS NODE').addTo(G.refhover); G.refhover.addTo(map); }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !S.locked) { if (S.draw) stopDraw(); else if (S.cut) ACT['cut-cancel'](); else if (S.join) cancelJoin(); else setMode(null); }
  if (e.key === 'Enter' && S.draw && !(e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT'))) finishDraw();
});
/* nothing can be done until the reference point is set / confirmed in this page load: the whole app is locked except the map (to move it) */
function updateLock() {
  const p = S.project;
  S.locked = !p || !p.reference || !S.refConfirmed;
  document.body.classList.toggle('locked', S.locked);
}
map.getContainer().addEventListener('click', (e) => { if (S.locked && !(S.mode && S.mode.kind === 'reference')) { e.stopPropagation(); e.preventDefault(); } }, true);
/* a click on an empty place of the map ends every special style: the selection, its popup, the highlight and the azimuth arrow / road point of a building */
function clearSelection() {
  if (!S.sel && !S.selPopup && !G.hl.getLayers().length && !G.access.getLayers().length) return;
  S.sel = null; G.hl.clearLayers(); G.access.clearLayers();
  if (S.selPopup) { const p = S.selPopup; S.selPopup = null; map.closePopup(p); }
  renderPanel();
}
map.on('click', async (e) => {
  if (!S.mode) { if (!S.locked) clearSelection(); return; }
  const { kind, data } = S.mode;
  try {
    if (kind === 'reference') {
      await showRefNodes(); const pick = S.hoverNode || nearestNode(e.latlng);
      if (!pick) { toast('No road node found here'); return; }
      tentativeReference(pick);                       // nothing is saved or computed until the editor confirms
    } else if (kind === 'access') {
      const snap = await api('/access/snap', { lat: e.latlng.lat, lon: e.latlng.lng });
      await api('/access', { building_id: data.id, lat: snap.lat, lon: snap.lon, user: user() });
      setMode(null); toast(`Road point set on street ${snap.street || snap.street_id}.`);
      await afterEdit(); drawTab();
    } else if (kind === 'cut') {
      await addCutPoint(e.latlng);
    } else if (kind === 'draw' && S.draw && S.draw.mode !== 'reshape' && S.draw.tool === 'points') {
      await addDrawPoint(e.latlng);
    } else if (kind === 'connect') {
      if (S.stage) {
        const r = await api('/roads/snap', { lat: e.latlng.lat, lon: e.latlng.lng });
        S.stage.connections[data.key] = r.target; setMode(null); await previewStage(false); S.sel = null;
        toast(r.target.type === 'node' ? 'Connected to the node' : 'Connected to the road (it will be split there)'); drawTab(); return;
      }
      const r = await api('/roads/connect', { node: data.key, lat: e.latlng.lat, lon: e.latlng.lng, user: user() });
      setMode(null); toast(r.target.type === 'node' ? 'Connected to the node' : 'Connected to the road (it will be split there)');
      await afterEdit(); await loadRoads(); drawTab();
    }
  } catch (err) { toast(err.message, 4500); }
});

async function afterEdit() { await refreshProject(); }

/* ---------------------------------------------------------------- compute */
/* edits do not recompute the street ids by themselves (it takes a while): a bar offers it, and changing to Street IDs, Street names or Addresses recomputes
   them when something changed; the Addresses tab then recomputes the addresses when it is opened */
function recomputeIfStale() { const p = S.project; if (p && p.reference && S.refConfirmed && !S.polling && p.network_ready && p.stale) startCompute('network', true); }
function updateStaleBar() {
  const p = S.project, bar = $('#stalebar'); if (!bar) return;
  const show = !!(p && p.reference && p.network_ready && p.stale && !S.polling && !S.recomputing);
  bar.hidden = !show;
  if (show) bar.innerHTML = I18n.htmlText('Edits not applied yet <button id="recompute-now" class="primary">Recompute ids</button>');
}
document.addEventListener('click', (e) => { if (e.target && e.target.id === 'recompute-now') { $('#stalebar').hidden = true; recomputeIfStale(); } });      // the bar goes away at once
function autoCompute() {
  if(!AuthUI.can('compute'))return;
  const p = S.project; if (!p || !p.reference || !S.refConfirmed || S.polling) return;
  const atStart = !S.startChecked; S.startChecked = true;       // at the start of the map the ids are recomputed once when edits are waiting
  if (!p.network_ready || (atStart && p.stale)) {                // otherwise edits wait (see recomputeIfStale); one try per page load, never after every edit
    if (!S.autoNet) { S.autoNet = true; startCompute('network', true); }
  } else if (!p.stale && S.tab === 'addresses' && (!p.addresses_ready || p.addresses_stale)) {
    const key = `${p.status.revision}|${p.network_hash}`;
    if (S.autoAddr !== key) { S.autoAddr = key; startCompute('addresses', true); }
  }
}
async function startCompute(kind, quiet = true) {
  if(!AuthUI.can('compute'))return;
  if (kind === 'network') { S.recomputing = true; updateStaleBar(); }          // the bar stays away from the click until the computation ends
  try {
    const r = await api(kind === 'network' ? '/compute' : '/addresses/compute', {});
    if (!r.started && !quiet) toast('A computation is already running');
  } catch (e) { S.recomputing = false; updateStaleBar(); toast(e.message, 4500); return; }
  pollJob(kind);
}
async function pollJob(kind) {
  if (S.polling) return; S.polling = true;
  const tick = async () => {
    const j = await api('/job');
    if (j.state === 'running') { $('#job').textContent = `computing: ${j.step}...`; setTimeout(tick, 600); return; }
    S.polling = false; S.recomputing = false;
    if (j.state === 'error') { toast(`Computation failed: ${j.error}`, 7000); await refreshProject(); return; }
    if (j.state === 'done') { await loadComputed(j.kind || kind); }
  };
  tick();
}
async function loadComputed(kind) {
  try {
    if (kind === 'addresses') S.addresses = await api('/addresses');
    S.network = await api('/network');
    if (kind !== 'addresses') S.addresses = null;                     // the addresses follow the new street ids
    await loadRoads();
  } catch (e) { /* not available yet */ }
  await refreshProject(); if (S.tab === 'addresses') S.addresses = S.addresses || null; drawTab();
  if (kind === 'network' && S.network && S.mergeCheck) {
    const parts = S.network.streets.features.filter((f) => f.properties.original === S.mergeCheck.id).length;
    toast(parts > 1 ? `The street with id ${S.mergeCheck.id} was cut again into ${parts} parts at a minimum of the distance to the reference.` : `The street with id ${S.mergeCheck.id} is kept as one street.`, 6000); S.mergeCheck = null;
  }
  if (kind === 'network' && S.network) { const b = S.network.reference; if (!S.fitted) { map.setView([S.project.reference.lat, S.project.reference.lon], 16); S.fitted = true; } toast(I18n.msg`Computed: ${S.network.stats.streets} streets`); }
}
function fitToNetwork() { try { const b = L.geoJSON(S.network.streets).getBounds(); if (b.isValid()) map.fitBounds(b.pad(0.05)); } catch (e) { /* ignore */ } }

/* ---------------------------------------------------------------- warnings */
function renderWarnings() {
  const list = $('#warn-list'); const ws = allWarnings();
  list.innerHTML = ws.length ? ws.map((original, i) => { const w=I18n.warning(original); return I18n.html`<div class="warn-item ${w.severity}" data-warn="${i}"><strong>${esc(w.title)}</strong><small>${esc(w.message)}</small></div>`; }).join('') : I18n.htmlText('<p class="muted">No warnings.</p>');
}
function highlightIds(ids) {
  G.hl.clearLayers(); if (!S.network) return null; const set = new Set(ids); const bounds = [];
  S.network.streets.features.filter((f) => set.has(f.properties.id)).forEach((f) => {
    const l = L.geoJSON(f, { style: { color: '#d50000', weight: 11, opacity: .5 }, interactive: false }).addTo(G.hl); bounds.push(l.getBounds());
  });
  G.hl.addTo(map); return bounds;
}
function focusWarning(w) {
  let bounds = [];
  if (w.streets && w.streets.length) bounds = highlightIds(w.streets) || [];
  if (w.nodes) { G.hl.clearLayers(); w.nodes.forEach((n) => { L.circleMarker([n.lat, n.lon], { radius: 12, color: '#d50000', weight: 3, fill: false }).addTo(G.hl); bounds.push(L.latLngBounds([n.lat, n.lon], [n.lat, n.lon])); }); G.hl.addTo(map); }
  if (w.buildings && S.addresses) {
    const set = new Set(w.buildings); G.hl.clearLayers();
    S.addresses.houses.features.filter((f) => set.has(f.properties.building_id)).forEach((f) => { const l = L.geoJSON(f, { pointToLayer: (ft, ll) => L.circleMarker(ll, { radius: 10, color: '#d50000', weight: 3, fill: false }), style: { color: '#d50000', weight: 3, fill: false }, interactive: false }).addTo(G.hl); bounds.push(l.getBounds()); });
    G.hl.addTo(map);
  }
  if (bounds.length) { let b = bounds[0]; bounds.forEach((x) => { b = b.extend(x); }); map.fitBounds(b.pad(0.2), { maxZoom: 18 }); }
}

/* ---------------------------------------------------------------- tabs */
async function switchTab(tab) {
  if (S.stage && tab !== 'roads' && !confirm('Cancel the new streets you are previewing?')) return;
  const from = S.tab;
  if (S.draw && S.draw.preview) map.removeLayer(S.draw.preview);
  S.tab = tab; S.sel = null; S.stage = null; S.del = null; S.reshapePick = null; S.cut = null; S.join = null; S.draw = null; map.dragging.enable(); map.doubleClickZoom.enable(); setMode(null); await refreshProject();
  if (tab === 'roads') await loadRoads();
  if ((tab === 'ids' || tab === 'names' || tab === 'addresses') && !S.network) { try { S.network = await api('/network'); } catch (e) { /* not computed */ } }
  if (tab === 'addresses' && !S.addresses) { try { S.addresses = await api('/addresses'); } catch (e) { /* not computed */ } }
  drawTab();
  if (tab !== 'roads' && tab !== from) recomputeIfStale();         // leaving the street editor (or opening names / addresses) applies the edits
  autoCompute();
}
$('#tabs').addEventListener('click', (e) => { const b = e.target.closest('button[data-tab]'); if (b && !b.disabled) switchTab(b.dataset.tab); });

/* the larger road network (outside the area of interest) as a light grey context layer; it is used for the routes to the reference points outside */
async function drawOutside() {
  if (!S.outsideNet) { try { S.outsideNet = await api('/base/outside'); } catch (e) { return; } }
  G.outside.clearLayers();
  L.geoJSON(S.outsideNet, { style: { color: '#b0bec5', weight: 2, opacity: .5 }, interactive: false }).addTo(G.outside); G.outside.addTo(map); G.outside.eachLayer((l) => l.bringToBack && l.bringToBack());
}
function drawTab() {
  S.renderEpoch = (S.renderEpoch || 0) + 1;
  clearTab(); drawReference(); drawOutside();
  if (S.tab !== 'addresses' && S.project.has.points) G.points.addTo(map);
  const p = S.project;
  if (!p.reference) { drawReferencePick(); return; }
  ({ roads: drawRoads, ids: drawNetwork, names: drawNetwork, addresses: drawAddresses }[S.tab])();
  renderPanel();
}
async function drawReferencePick() {
  if (!S.baseStreets) S.baseStreets = await api('/base/streets');
  L.geoJSON(S.baseStreets, { style: { color: '#ffd54f', weight: 2 }, interactive: false }).addTo(G.base); G.base.addTo(map);
  try { const b = L.geoJSON(S.baseStreets).getBounds(); if (b.isValid()) map.fitBounds(b); } catch (e) { /* ignore */ }
  showMoveBox(); renderPanel();
}

/* ---------------------------------------------------------------- network tabs (ids, names) */
function featureMid(f) {
  const g = f.geometry; const line = g.type === 'MultiLineString' ? g.coordinates[0] : g.coordinates; const c = line[Math.floor(line.length / 2)]; return [c[1], c[0]];
}
const RANK_COLOR = ['#b71c1c', '#e65100', '#f9a825', '#2e7d32', '#1565c0', '#6a1b9a'];
const RANK_NAMES = ['0 motorway / trunk', '1 primary', '2 secondary', '3 tertiary', '4 residential / service', '5 footway / path'];
function colourLegend() {
  const m = S.idColor;
  const ring = I18n.htmlText('<span><i class="dot" style="background:#fff;border:2px solid #d50000;box-sizing:border-box"></i>street cut here for the numbering</span>');
  const withRing = (html) => html.replace(/<\/div>$/, ring + I18n.htmlText('</div>'));            // one legend with the colours and the circle
  if (m === 'rank') return withRing(legend(RANK_NAMES.map((t, i) => [RANK_COLOR[i], t]), true));
  if (m === 'drivable') return withRing(legend([['#1565c0', 'drivable'], ['#9e9e9e', 'not drivable (dashed)'], ['#bdbdbd', 'unknown (dotted)']], true));
  if (m === 'paved') return withRing(legend([['#00acc1', 'paved'], ['#ef6c00', 'unpaved'], ['#bdbdbd', 'unknown (dotted)']], true));
  return I18n.html`<div class="legend">${ring}</div>`;
}
function drawNetwork() {
  const net = S.network;
  if (!net) { return; }
  ensureColors();
  const names = S.tab === 'names';
  const maxD = Math.max(1, ...net.streets.features.map((f) => f.properties.d1 || 0));
  const layer = L.geoJSON(net.streets, {
    style: (f) => {
      const p = f.properties;
      if (names) return { color: statusColor(p), weight: p.status === 'planned' ? 7 : p.status === 'named' ? 5 : p.status === 'other' ? 2 : 3.5, opacity: .95 };
      if (S.idColor === 'rank') return { color: RANK_COLOR[p.rank] || '#757575', weight: 4, opacity: .95 };
      if (S.idColor === 'drivable' && p.drivable === null) return { color: '#bdbdbd', weight: 3, opacity: .95, dashArray: '2 5' };
      if (S.idColor === 'paved' && p.paved === null) return { color: '#bdbdbd', weight: 3, opacity: .95, dashArray: '2 5' };
      if (S.idColor === 'drivable') return p.drivable ? { color: '#1565c0', weight: 4, opacity: .95 } : { color: '#9e9e9e', weight: 3, opacity: .95, dashArray: '6 5' };
      if (S.idColor === 'paved') return p.paved ? { color: '#00acc1', weight: 4, opacity: .95 } : { color: '#ef6c00', weight: 4, opacity: .95 };
      if (S.idColor === 'distance') { const t = ((p.d0 || 0) + (p.d1 || 0)) / 2 / maxD; return { color: `hsl(${Math.round(240 - 240 * t)},85%,45%)`, weight: 4, opacity: .95 }; }
      return { color: hue(p.id), weight: 4, opacity: .95 };
    },
    onEachFeature: (f, l) => {
      const p = f.properties;
      l.bindTooltip(`${esc(p.display || '(no name yet)')} · street ${p.id}${p.part ? ` (part ${p.part + 1} of ${p.original})` : ''}`, { sticky: true });
      l.on('click', (e) => { if (S.mode) return; L.DomEvent.stopPropagation(e); selectStreet(p.id, true, e.latlng); });
    },
  }).addTo(G.net);
  G.net.addTo(map);
  if (S.tab === 'ids') { L.geoJSON(net.cuts, { pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 4, color: '#d50000', weight: 2, fill: false }), onEachFeature: (f, l) => l.bindTooltip('a street was cut here') }).addTo(G.cuts); G.cuts.addTo(map); }
  if (S.tab === 'ids' && net.junctions) { drawJunctions(net.junctions); }
  S.labelFeatures = net.streets.features.filter((f) => f.properties.display).map((f) => ({ f, mid: featureMid(f) }));
  updateLabels(); G.labels.addTo(map);
  if (S.sel && S.sel.type === 'street') selectStreet(S.sel.id, true, S.sel.ll);
}
function updateLabels() {
  G.labels.clearLayers();
  if (!S.labels || (S.tab !== 'ids' && S.tab !== 'names') || map.getZoom() < 16 || !S.labelFeatures) return;
  const b = map.getBounds().pad(0.1);
  S.labelFeatures.forEach(({ f, mid }) => {
    if (!b.contains(mid)) return; const p = f.properties;
    L.marker(mid, { interactive: false, icon: L.divIcon({ className: '', html: I18n.html`<div class="slabel ${p.status === 'derived' ? 'derived' : p.status === 'planned' ? 'planned' : ''}">${esc(p.display)}</div>`, iconSize: [0, 0] }) }).addTo(G.labels);
  });
}
map.on('zoomend moveend', updateLabels);

function streetById(id) { return S.network && S.network.streets.features.find((f) => f.properties.id === id); }
/* node popup: every street meeting at a node, as a coloured spoke (like the old street editor) */
function splitForNumbering() {                               // final ids whose street was cut into parts by the numbering
  const n = {}; S.network.streets.features.forEach((f) => { n[f.properties.original] = (n[f.properties.original] || 0) + 1; });
  return new Set(S.network.streets.features.filter((f) => n[f.properties.original] > 1).map((f) => f.properties.id));
}
function junctionHtml(p) {
  ensureColors();
  const size = 300, c = size / 2, r = 78;
  const compass = (b) => ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(b / 45) % 8];
  const ids = [...new Set(p.spokes.map((s) => s.id))];
  const count = {}; p.spokes.forEach((s) => { count[s.id] = (count[s.id] || 0) + 1; });
  const busy = !!S.polling;                                          // selectors are disabled only while a computation is actively running
  const cutIds = splitForNumbering();                          // ids that are parts of a street cut for the numbering: better not to join them
  let svg = I18n.html`<svg width="${size}" height="${size}" class="junction-svg">`, boxes = '';
  p.spokes.forEach((s) => {
    const a = s.bearing * Math.PI / 180, col = hue(s.id), sx = Math.sin(a), sy = -Math.cos(a);
    svg += I18n.html`<line x1="${c}" y1="${c}" x2="${c + r * sx}" y2="${c + r * sy}" stroke="${col}" stroke-width="4" stroke-linecap="round"/>`;
    const lx = c + (r + 30) * sx, ly = c + (r + 16) * sy;      // the selector sits at the end of its road, in the diagram
    const sourceRank = streetById(s.id)?.properties.rank;
    const merge = ids.filter((i) => i !== s.id && (sourceRank === undefined || streetById(i)?.properties.rank === sourceRank)).map((i) => (cutIds.has(i) ? I18n.html`<option value="merge:${i}" style="color:#d50000;font-weight:700">&#9888; id ${i} (cut for the numbering)</option>` : I18n.html`<option value="merge:${i}">id ${i}</option>`)).join('');
    const split = (s.can_split ?? count[s.id] > 1) ? I18n.htmlText('<option value="split">new id (split here)</option>') : '';      // topology checked by the server; count fallback for older cached results
    boxes += merge || split
      ? I18n.html`<select class="id-change" ${busy ? 'disabled' : ''} style="left:${lx}px;top:${ly}px;border-color:${col};color:${col}" title="${busy ? 'updating the street ids...' : 'Change the id of this road'}" data-ref="${esc(s.ref)}" data-node="${p.node}" data-id="${s.id}"><option value="">${s.id}</option>${merge}${split}</select>`
      : I18n.html`<span class="id-plain" style="left:${lx}px;top:${ly}px;color:${col}">${s.id}</span>`;
  });
  svg += I18n.html`<circle cx="${c}" cy="${c}" r="4" fill="#fff"/></svg>`;
  const rankSel = (s) => {                                          // the rank of the street, editable here
    const f = S.network && S.network.streets.features.find((g) => g.properties.id === s.id); if (!f) return '';
    const q = f.properties, parts = S.network.streets.features.filter((g) => g.properties.original === q.original).length;
    return I18n.html`<select class="attr-edit" data-attr="rank" data-orig="${q.original}" ${parts > 1 ? `data-part="${q.part}"` : ''} title="Street rank">${RANK_NAMES.map((t, i) => I18n.html`<option value="${i}" ${q.rank === i ? 'selected' : ''}>${t.split(' ').slice(0, 2).join(' ')}</option>`).join('')}</select>`;
  };
  const rows = p.spokes.map((s) => I18n.html`<tr class="click" data-street="${s.id}"><td><span class="dot" style="background:${hue(s.id)}"></span> <strong>${s.id}</strong> <span class="muted">${compass(s.bearing)}</span></td><td>${esc(s.name || '(no name yet)')}</td><td>${rankSel(s)}</td></tr>`).join('');
  return I18n.html`<div class="junction"><div class="muted">Node ${p.node}${p.d !== null ? ` &middot; ${fmt(p.d)} m from the reference` : ''}</div><div class="jbox" style="width:${size}px;height:${size}px">${svg}${boxes}</div><table>${rows}</table><div class="muted">${busy ? 'Updating the street ids... selectors are off until it ends.' : 'Change an id with the selector at its road.'}</div></div>`;
}
function drawJunctions(fc) {
  L.geoJSON(fc, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 4, color: '#000', weight: 1, fillColor: '#fff', fillOpacity: 1 }),
    onEachFeature: (f, l) => {
      l.on('click', (e) => {
        if (S.mode) return; L.DomEvent.stopPropagation(e);
        L.popup({ maxWidth: 340 }).setLatLng(l.getLatLng()).setContent(junctionHtml(f.properties)).openOn(map);
      });
    },
  }).addTo(G.junc);
  const show = () => { if (map.getZoom() >= 16) G.junc.addTo(map); else map.removeLayer(G.junc); };
  map.off('zoomend', S.juncZoom); S.juncZoom = show; map.on('zoomend', show); show();
}
document.addEventListener('change', async (e) => {                // the rank of a reference point for the street signs
  const pr = e.target.closest && e.target.closest('select.pt-rank');
  if (pr) { try { await api('/points/rank', { id: pr.dataset.id, rank: +pr.value }); toast('Saved'); S.pointRanks = null; await afterEdit(); if (S.tab === 'addresses') loadPointRanks(); } catch (err) { toast(err.message, 5000); } }
});
async function loadPointRanks() { try { S.pointRanks = await api('/points/ranks'); if (S.tab === 'addresses') renderPanel(); } catch (e) { /* no points */ } }
document.addEventListener('change', async (e) => {                // rank / drivable / paved of a street, edited in its popup
  const at = e.target.closest && e.target.closest('.attr-edit');
  if (at) {
    const body = { original: +at.dataset.orig, ...(at.dataset.part !== undefined ? { part: +at.dataset.part } : {}), [at.dataset.attr]: at.dataset.attr === 'rank' ? +at.value : JSON.parse(at.value) };
    try {
      await api('/streets/attrs', body); toast('Saved');
      if (S.network) {                                           // show the change at once; the recomputation that follows confirms it
        const key = at.dataset.attr, val = body[key];
        S.network.streets.features.forEach((f) => { if (f.properties.original === body.original && (body.part === undefined || f.properties.part === body.part)) f.properties[key] = val; });
        if (S.tab === 'ids' || S.tab === 'names') { drawNetwork(); if (S.sel) refreshSel(); }
      }
      await afterEdit();
    } catch (err) { toast(err.message, 5000); }
    return;
  }
});
document.addEventListener('change', async (e) => {
  const sel = e.target.closest && e.target.closest('select.id-change'); if (!sel || !sel.value) return;
  const [mode, target] = sel.value.split(':'); const id = +sel.dataset.id, tid = +target;
  const cut = mode === 'merge' && splitForNumbering().has(tid);
  const what = mode === 'merge' ? (cut ? `Street ${tid} was cut for the numbering, so it may be cut again. Join street ${id} to it anyway and never cut it there again?` : `Give every road of street ${id} the id ${tid}?`) : `Give the branch of street ${id} that leaves node ${sel.dataset.node} on this road a new id?`;
  if (!confirm(what)) { sel.value = ''; return; }
  try {
    const r = await api('/streets/assign', mode === 'merge' ? { ref: sel.dataset.ref, mode, target: tid, force: cut } : { ref: sel.dataset.ref, mode, node: +sel.dataset.node });
    map.closePopup(); S.mergeCheck = cut ? null : { id: r.id, mode };
    toast(`ID change saved for ${r.changed} roads: id ${r.id}${r.name ? ' and the name ' + r.name : ''}. Updating the map...`, 5000);
    await afterEdit(); recomputeIfStale();
  } catch (err) { sel.value = ''; toast(err.message, 7000); }
});
document.addEventListener('click', (e) => { if (e.target.closest && e.target.closest('select.id-change, select.attr-edit')) return; const r = e.target.closest('.junction tr[data-street]'); if (r) { map.closePopup(); selectStreet(+r.dataset.street, true); renderPanel(); } });

function selHtml() {
  if (!S.sel) return '';
  if (S.sel.type === 'street') return streetBox(S.tab === 'names');
  if (S.sel.type === 'house') return houseBox();
  if (S.sel.type === 'node') return nodePanel(!!S.stage);
  return '';
}
function refreshSel() {                                       // open / update / close the selection popup
  if(S.openingPopup)return;
  const html = selHtml();
  if (!html || !S.sel || !S.sel.ll) { if (S.selPopup) { const p = S.selPopup; S.selPopup = null; map.closePopup(p); } return; }
  const content = I18n.html`<div class="selbox">${html}</div>`;
  if (S.selPopup && S.selPopup.isOpen()) { const same = S.selPopup.getLatLng().equals(S.sel.ll); S.selPopup.setContent(content); if (!same) S.selPopup.setLatLng(S.sel.ll); return; }
  // Opening can stop an existing pan and synchronously fire moveend. Assign
  // the popup first and guard re-entry so that handler cannot open a second
  // popup and detach the one Leaflet is still positioning.
  S.openingPopup=true;
  try {
    S.selPopup=L.popup({maxWidth:400,minWidth:280,autoClose:true,closeOnClick:false,className:'sel-popup',autoPanPadding:[30,30]}).setLatLng(S.sel.ll).setContent(content);
    S.selPopup.openOn(map);
    S.selPopup.options.autoPan=false;
  } finally {S.openingPopup=false;}
}
map.on('popupclose', (e) => { if (e.popup === S.selPopup) { S.selPopup = null; S.sel = null; G.hl.clearLayers(); } });
document.addEventListener('click', async (e) => {            // buttons inside the popup
  const b = e.target.closest && e.target.closest('.sel-popup [data-act]'); if (!b) return; e.preventDefault();
  try { await ACT[b.dataset.act](b); } catch (err) { toast(err.message, 5000); }
  refreshSel();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.id === 'name-input') { const b = document.querySelector('.sel-popup [data-act=set-name]'); if (b) b.click(); } });

function nearestVertex(f, c) {                               // the point of a street line that is nearest to a place
  const g = f.geometry; const lines = g.type === 'MultiLineString' ? g.coordinates : [g.coordinates]; let best = null, bd = 1e18;
  lines.forEach((ln) => ln.forEach(([x, y]) => { const d = (x - c.lng) ** 2 + (y - c.lat) ** 2; if (d < bd) { bd = d; best = L.latLng(y, x); } }));
  return best;
}
function selectStreet(id, keepView, ll) {
  const f = streetById(id); if (!f) return;
  G.hl.clearLayers();
  const l = L.geoJSON(f, { style: { color: '#fff', weight: 10, opacity: .8 }, interactive: false }).addTo(G.hl); G.hl.addTo(map);
  if (keepView) {
    S.sel = { type: 'street', id, ll: ll || L.latLng(featureMid(f)) };
    renderPanel(); refreshSel(); return;
  }
  const target = l.getBounds().pad(0.6);                      // chosen from a list: show the street, then open the popup on it where it is in view
  S.sel = { type: 'street', id, ll: nearestVertex(f, target.getCenter()) };
  renderPanel();
  map.once('moveend', () => { if (S.sel && S.sel.id === id) refreshSel(); });
  map.fitBounds(target, { maxZoom: 18 });
  setTimeout(() => { if (S.sel && S.sel.id === id && !S.selPopup) refreshSel(); }, 900);          // the view did not need to move
}

/* ---------------------------------------------------------------- edit streets tab: add streets from a file, delete edges */
async function loadRoads() { S.roads = await api('/roads'); }
const NEWC = (i) => `hsl(${Math.round((i * 137.5) % 360)},85%,52%)`;               // a different colour for every new street
const NODE_STYLE = {
  connected: { radius: 9, color: '#004d40', weight: 4, fillColor: '#00e5ff', fillOpacity: 1 },
  free: { radius: 6, color: '#000', weight: 1.5, fillColor: '#ff6d00', fillOpacity: 1 },
  inner: { radius: 3, color: '#000', weight: 1, fillColor: '#bdbdbd', fillOpacity: 1 },
};
function nodeKind(p) { return p.state === 'connected' ? 'connected' : (p.state === 'free' || p.state === 'pending_edge') && p.degree === 1 ? 'free' : 'inner'; }
function clearStage() { S.stage = null; S.sel = null; setMode(null); }
async function previewStage(first) {
  const st = S.stage;
  const body = { filename: st.filename, connections: st.connections, keep: st.keep };
  if (first) body.content_b64 = st.b64; else body.features = st.features;
  const r = await api('/roads/preview', body); st.features = r.features; st.pv = r; st.b64 = undefined;
  return r;
}
function drawStageLayers() {
  const pv = S.stage.pv;
  const cmap = {}; S.stage.features.forEach((f, i) => { cmap[f.id] = NEWC(i); });
  L.geoJSON(pv.replaced, { style: { color: '#d50000', weight: 7, opacity: .9, dashArray: '2 8' }, interactive: true,
    onEachFeature: (f, l) => l.bindTooltip(`existing road replaced by the new one (${fmt(f.properties.old_length, 0)} m vs ${fmt(f.properties.new_length, 0)} m)`, { sticky: true }) }).addTo(G.roadEdges);
  L.geoJSON(pv.edges, { style: (f) => ({ color: cmap[f.properties.feature] || '#e040fb', weight: 5, opacity: .95 }),
    onEachFeature: (f, l) => l.bindTooltip(`new street ${esc(f.properties.name || '')}`, { sticky: true }) }).addTo(G.roadEdges);
  G.roadEdges.addTo(map);
  const sorted = [...pv.nodes.features].sort((a, b) => (nodeKind(a.properties) === 'connected') - (nodeKind(b.properties) === 'connected'));
  L.geoJSON({ type: 'FeatureCollection', features: sorted }, {
    pointToLayer: (f, ll) => L.circleMarker(ll, NODE_STYLE[nodeKind(f.properties)]),
    onEachFeature: (f, l) => {
      const p = f.properties, k = nodeKind(p);
      l.bindTooltip(k === 'connected' ? `connection node (${p.to}${p.manual ? ', by hand' : ', automatic'}) - click to edit` : k === 'free' ? 'end not connected - click to connect it' : 'node - click to edit');
      l.on('click', (e) => { if (S.mode) return; L.DomEvent.stopPropagation(e); S.sel = { type: 'node', key: p.key, props: p, ll: e.latlng }; renderPanel(); refreshSel(); });
    },
  }).addTo(G.roadNodes); G.roadNodes.addTo(map);
  try { const b = L.geoJSON(pv.edges).getBounds(); if (b.isValid() && S.stage.fit) { map.fitBounds(b.pad(0.2)); S.stage.fit = false; } } catch (e) { /* ignore */ }
}
async function drawDeleteLayers() {
  const epoch = S.renderEpoch;
  const [e, current] = await Promise.all([api('/edges'), api('/roads/geometry')]);
  if (epoch !== S.renderEpoch || !S.del) return;
  S.edgeData = {...e,edges:current.edges}; const sel = S.del.sel;
  const style = (f) => (sel.has(f.properties.ref) ? { color: '#ff1744', weight: 7, opacity: 1 } : { color: '#ffd54f', weight: 3.5, opacity: .9 });
  const tip = (f) => `${esc(f.properties.name || 'edge')} · ${f.properties.street_id !== null && f.properties.street_id !== undefined ? 'street ' + f.properties.street_id + ' · ' : ''}${fmt(f.properties.length_m ?? f.properties.length, 0)} m · click to select`;
  const toggle = (ref) => { if (sel.has(ref)) sel.delete(ref); else sel.add(ref); drawTab(); };
  L.geoJSON(e.removed, { style: { color: '#9e9e9e', weight: 4, dashArray: '4 6', opacity: .9 },
    onEachFeature: (f, l) => { l.bindTooltip('deleted - click to bring it back', { sticky: true });
      l.on('click', async (ev) => { L.DomEvent.stopPropagation(ev); await api('/edges/restore', { edges: [f.properties.ref] }); await afterEdit(); toast('Edge restored'); drawTab(); }); } }).addTo(G.roadEdges);
  L.geoJSON(current.edges, { style, onEachFeature: (f, l) => { l.bindTooltip(tip(f), { sticky: true }); l.on('click', (ev) => { L.DomEvent.stopPropagation(ev); toggle(f.properties.ref); }); } }).addTo(G.base);
  G.base.addTo(map); G.roadEdges.addTo(map);
}
const FILE_COLOR = (i) => `hsl(${Math.round((i * 137.5 + 20) % 360)},80%,48%)`;
const fileIndex = (id) => {                                    // files with the same name (all the roads drawn by hand) share one colour
  const imps = S.project.roads.imports || [], i = imps.findIndex((x) => String(x.id) === String(id));
  return i < 0 ? i : imps.findIndex((x) => x.file === imps[i].file);
};
function roadsLegend() {
  if (S.roadColor !== 'file') return '';
  return legend([['#9e9e9e', 'downloaded network (OSM)'], ...(S.project.roads.imports || []).map((x, i) => [FILE_COLOR(fileIndex(x.id)), esc(x.file), i]).filter(([, t, i], k, a) => a.findIndex((y) => y[1] === t) === k).map(([c, t]) => [c, t])], true);
}
function drawRoads() {
  const epoch = S.renderEpoch;
  if (S.join) { drawJoinLayers(); return; }
  if (S.draw) { drawDrawLayers(); return; }
  if (S.cut) { drawCutLayers(); return; }
  if (S.reshapePick) { drawReshapeChoices(); return; }
  if (S.stage && S.stage.pv) {
    api('/base/streets').then((b) => { S.baseStreets = b; if (S.tab !== 'roads' || epoch !== S.renderEpoch) return; G.base.clearLayers(); L.geoJSON(b, { style: { color: '#ffd54f', weight: 2, opacity: .7 }, interactive: false }).addTo(G.base); G.base.addTo(map); });
    drawStageLayers(); return;
  }
  if (S.del) { drawDeleteLayers(); return; }
  const byFile = S.roadColor === 'file';
  api('/base/streets').then((b) => { S.baseStreets = b; if (S.tab !== 'roads' || epoch !== S.renderEpoch) return; G.base.clearLayers(); L.geoJSON(b, { style: (f) => (byFile ? (f.properties.file ? { color: FILE_COLOR(fileIndex(f.properties.file)), weight: 4, opacity: .95 } : { color: '#9e9e9e', weight: 2, opacity: .85 }) : { color: '#ffd54f', weight: 2, opacity: .85 }), interactive: false }).addTo(G.base); G.base.addTo(map); });
  api('/base/components').then((c) => {                        // the parts cut off from the main network: red dashed
    const had = JSON.stringify((S.components || { parts: [] }).parts), now = JSON.stringify(c.parts);      // (nothing to show: the panel is not redrawn, it may hold a file being chosen)
    S.components = c;
    if (S.tab !== 'roads' || epoch !== S.renderEpoch) return;
    L.geoJSON(c.edges, { style: { color: '#d50000', weight: 7, opacity: .9, dashArray: '2 9', lineCap: 'round' }, onEachFeature: (f, l) => {
      const id = f.properties.part, pt = c.parts.find((x) => x.id === id) || { edges: '?', length_m: 0 };
      l.bindTooltip(`separate part ${id}: not connected to the main network - click for options`);
      l.on('click', (e) => {
        L.DomEvent.stopPropagation(e);
        const box = document.createElement('div');
        box.innerHTML = I18n.html`<strong>Separate part ${id}</strong><br>${pt.edges} edges &middot; ${fmt(pt.length_m)} m<br><span class="muted">Not connected to the main network: draw or prolong a road to connect it, or delete it.</span><div class="row"><button class="danger">Delete this part</button></div>`;
        box.querySelector('button').addEventListener('click', async () => { map.closePopup(); await ACT['del-part']({ dataset: { part: id } }); });
        L.popup().setLatLng(e.latlng).setContent(box).openOn(map);
      });
    } }).addTo(G.roadEdges);
    G.roadEdges.addTo(map);
    if (had !== now && S.tab === 'roads') renderPanel();
  }).catch(() => {});
  const r = S.roads; if (!r) return;
  (r.report?.uncertain_ends || []).forEach(p => L.marker([p.lat, p.lon], { icon: L.divIcon({ className: 'uncertain-end', html: '?', iconSize: [22, 22] }) }).bindTooltip('Path unclear beyond this endpoint — needs review').addTo(G.roadNodes));
  if (!byFile) L.geoJSON(r.edges, { style: { color: '#e040fb', weight: 5, opacity: .95 }, onEachFeature: (f, l) => l.bindTooltip(`new road ${esc(f.properties.name || '')} · street ${f.properties.street_id}`) }).addTo(G.roadEdges);
  G.roadEdges.addTo(map);
  L.geoJSON(r.nodes, {
    pointToLayer: (f, ll) => L.circleMarker(ll, NODE_STYLE[nodeKind(f.properties)]),
    onEachFeature: (f, l) => {
      const p = f.properties;
      l.bindTooltip(p.state === 'connected' ? `connected (${p.to}${p.manual ? ', by hand' : ', automatic'})` : p.degree === 1 ? 'end not connected - click to connect it' : 'node');
      l.on('click', (e) => { L.DomEvent.stopPropagation(e); if (p.degree === 1 || p.state !== 'connected') { S.sel = { type: 'node', key: p.key, props: p, ll: e.latlng }; renderPanel(); refreshSel(); } });
    },
  }).addTo(G.roadNodes); G.roadNodes.addTo(map);
  try { const b = L.geoJSON(r.edges).getBounds(); if (b.isValid() && !S.fittedRoads) { map.fitBounds(b.pad(0.2)); S.fittedRoads = true; } } catch (e) { /* ignore */ }
}

/* ---------------------------------------------------------------- addresses tab */
const HOUSE = { ok: '#2e7d32', pending_name: '#f9a825', no_name: '#d50000', no_street: '#7b1fa2' };
/* the important points of the community (reference points) are squares, buildings are circles */
/* an emoji for every kind of reference point on the Addresses tab: school, health, church, chiefdom, square, shop ... */
const EMOJI_RULES = [[/lyc[ée]e|[ée]cole|school|coll[èe]ge|universit/i, '🏫'], [/sant[ée]|health|h[ôo]pital|clinique|\bcsi\b|\bcma\b|pharmac|healthcare/i, '🏥'],
  [/[ée]glise|church|paroisse|mission|cath[ée]drale|temple|mosqu/i, '⛪'], [/lieu sacr|sacr[ée]|sacred/i, '🛕'], [/place |square|plaza|\bplace\b/i, '🌳'], [/h[ôo]tel/i, '🏨'], [/march[ée]|market|boutique|shop|alimentation|commerce|magasin/i, '🛒'], [/stade|stadium/i, '🏟️'],
  [/eau|water|puits|forage|fontaine/i, '💧'], [/carrefour|crossroad|intersection/i, '🔀'], [/village|hamlet|town|ville/i, '🏘️'],
  [/foyer|salle|mairie|sous-pr[ée]fecture|poste|police|gendarmerie|public|facility/i, '🏛️']];
function pointEmoji(name, kind) {
  if (/^(chefferie|chiefferie|palais)\b|\b(chefferie|chiefferie|chiefdom)\s+(de|du|of)\b|^chiefdom\b/i.test(name || '') && !/[ée]cole|school|lyc[ée]e|coll[èe]ge/i.test(name)) return '🪶';   // only places named as a chiefdom
  const t = `${kind || ''} ${name || ''}`; for (const [re, em] of EMOJI_RULES) if (re.test(t)) return em; return '📍'; }
function emojiMarker(ll, name, kind, border) {
  return L.marker(ll, { icon: L.divIcon({ className: '', html: I18n.html`<div class="emo" style="border-color:${border || '#555'}">${pointEmoji(name, kind)}</div>`, iconSize: [0, 0] }), keyboard: false });
}
function sqMarker(ll, color) {
  color = '#ffffff';                                          // reference points are white squares: no colour of a use or an address status
  return L.marker(ll, { icon: L.divIcon({ className: '', html: I18n.html`<div class="sqpt" style="background:${color}"></div>`, iconSize: [0, 0] }), keyboard: false });
}
/* every surveyed building is visible on the Addresses tab, also before the addresses are computed (coloured by use) */
const USE = { residential: '#43a047', commercial: '#fb8c00', industrial: '#6d4c41', office: '#1e88e5', public: '#8e24aa', unknown: '#9e9e9e' };
async function drawRawBuildings() {
  if (!S.project.has.buildings) return;
  if (!S.rawBuildings) S.rawBuildings = await api('/layers/buildings');
  if (S.tab !== 'addresses' || S.addresses) return;
  renderPanel(); G.houses.clearLayers();
  L.geoJSON(S.rawBuildings, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 4, color: '#000', weight: 1, fillColor: USE[f.properties.use] || USE.unknown, fillOpacity: .95 }),
    onEachFeature: (f, l) => { const p = f.properties; l.bindTooltip(`${[p.use, p.name].filter(Boolean).map(esc).join(' &middot; ') || 'building'}${p.name ? '' : ''}${p.residents ? ' &middot; ' + p.residents + ' residents' : ''}`, { sticky: true }); },
  }).addTo(G.houses);
  if (S.pointsData) L.geoJSON(S.pointsData, { pointToLayer: (f, ll) => emojiMarker(ll, f.properties.name, f.properties.kind),
    onEachFeature: (f, l) => l.bindTooltip(esc(f.properties.name || f.properties.kind || 'reference point'), { sticky: true }) }).addTo(G.houses);
  G.houses.addTo(map);
}
function drawAddresses() {
  map.removeLayer(G.points);                                   // the emoji replace the squares of the reference points here
  if (!S.pointRanks && S.project.has.points) loadPointRanks();
  if (S.network) { L.geoJSON(S.network.streets, { style: { color: '#fff', weight: 2, opacity: .55 }, interactive: false }).addTo(G.base); G.base.addTo(map); }
  const a = S.addresses;
  if (!a) { drawRawBuildings(); return; }
  L.geoJSON(a.houses, {
    pointToLayer: (f, ll) => (f.properties.use === 'reference' ? emojiMarker(ll, f.properties.name, f.properties.info, HOUSE[f.properties.status] || '#999') : L.circleMarker(ll, { radius: 5, color: '#000', weight: 1, fillColor: HOUSE[f.properties.status] || '#999', fillOpacity: .95 })),
    style: (f) => ({ color: HOUSE[f.properties.status] || '#999', weight: 2, fillOpacity: .35 }),
    onEachFeature: (f, l) => { const p = f.properties; l.bindTooltip(esc(p.address || 'no street within reach') + (p.use ? ` &middot; ${esc(p.use)}` : '') + (p.name ? ` &middot; ${esc(p.name)}` : ''), { sticky: true });
      l.on('click', (e) => { if (S.mode) return; L.DomEvent.stopPropagation(e); S.sel = { type: 'house', props: p, ll: e.latlng }; drawAccess(p); renderPanel(); refreshSel(); }); },
  }).addTo(G.houses); G.houses.addTo(map);
  L.geoJSON(a.signs, { pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 6, color: '#fff', weight: 2, fillColor: '#1565c0', fillOpacity: 1 }),
    onEachFeature: (f, l) => l.bindPopup(() => signHtml(f.properties), { maxWidth: 360, minWidth: 260 }) }).addTo(G.signs);
  updateSigns();
}
/* the popup of a street sign: a sketch of the roads at the intersection and the sign boards: a blue square board per street, a green arrow board per reference point */
const kmText = (m) => `${Math.max(0.1, Math.round(m / 100) / 10).toFixed(1)} km`;      // always the number, then km
function signHtml(p) {
  ensureColors();
  const size = 220, c = size / 2, r = 70;
  let svg = I18n.html`<svg width="${size}" height="${size}" class="junction-svg">`;
  const letters = {};                                          // one letter per street, the same on the sketch and on its sign board
  [...(p.spokes || []).map((x) => x.id), ...(p.label_ids || [])].forEach((id) => { if (!(id in letters)) letters[id] = String.fromCharCode(65 + Object.keys(letters).length % 26); });
  const seen = new Set();
  (p.spokes || []).forEach((s) => {
    const a = s.bearing * Math.PI / 180, col = hue(s.id), x = c + r * Math.sin(a), y = c - r * Math.cos(a);
    svg += I18n.html`<line x1="${c}" y1="${c}" x2="${x}" y2="${y}" stroke="${col}" stroke-width="5" stroke-linecap="round"/>`;
    if (!seen.has(s.id + ':' + Math.round(s.bearing / 20))) { seen.add(s.id + ':' + Math.round(s.bearing / 20)); svg += I18n.html`<text x="${c + (r + 14) * Math.sin(a)}" y="${c - (r + 14) * Math.cos(a)}" fill="${col}" font-size="15" font-weight="700" text-anchor="middle" dominant-baseline="middle">${letters[s.id]}</text>`; }
  });
  svg += I18n.html`<circle cx="${c}" cy="${c}" r="4" fill="#fff"/></svg>`;
  const boards = (p.labels || []).map((t, i) => { const id = (p.label_ids || [])[i]; return id === undefined ? I18n.html`<div class="board">${esc(t)}</div>` : I18n.html`<div class="board"><span class="sid" style="background:${hue(id)}">${letters[id]}</span>${esc(t)}</div>`; }).join('');
  const arrows = (p.pois || []).map((q) => I18n.html`<div class="arrow-board" title="rank ${q.rank}"><span class="arr" style="transform:rotate(${q.bearing - 90}deg)">&#10148;</span><span>${esc(q.name)}${q.distance_m ? I18n.html` <small>${kmText(q.distance_m)}</small>` : ''}</span></div>`).join('');
  return I18n.html`<div class="junction signbox"><div class="muted">Intersection of ${p.labels.length} street sign${p.labels.length === 1 ? '' : 's'}${(p.pois || []).length ? ` and ${p.pois.length} reference point${p.pois.length === 1 ? '' : 's'}` : ''}</div><div style="text-align:center">${svg}</div><div class="boards">${boards}${arrows}</div></div>`;
}
function updateSigns() { if (S.tab !== 'addresses') return; if (map.getZoom() >= 16) G.signs.addTo(map); else map.removeLayer(G.signs); }
map.on('zoomend', updateSigns);
function drawAccess(p) {
  G.access.clearLayers();
  if (!p) { G.access.addTo(map); return; }
  const f = S.addresses && S.addresses.houses.features.find((x) => x.properties.building_id === p.building_id);
  if (f) {
    const c = L.geoJSON(f).getBounds().getCenter();
    const rl = p.access_lat ? [p.access_lat, p.access_lon] : p.road_lat ? [p.road_lat, p.road_lon] : null;
    if (rl) {                                                  // the point of the road the number is taken from
      L.polyline([[c.lat, c.lng], rl], { color: '#6a1b9a', dashArray: '5 5', weight: 3 }).addTo(G.access);
      L.circleMarker(rl, { radius: 7, color: '#6a1b9a', weight: 3, fillColor: '#ce93d8', fillOpacity: 1 }).bindTooltip('point of the road used for the number').addTo(G.access);
    }
    if (p.azimuth !== null && p.azimuth !== undefined && p.azimuth === p.azimuth) {      // arrow: the direction from the street to the door, drawn through the building
      const svg = I18n.html`<svg width="80" height="80" viewBox="-40 -40 80 80" style="overflow:visible"><g transform="rotate(${p.azimuth})">
        <line x1="0" y1="18" x2="0" y2="-16" stroke="#fff" stroke-width="8" stroke-linecap="round"/><polygon points="0,-34 -11,-14 11,-14" fill="#fff" stroke="#fff" stroke-width="6" stroke-linejoin="round"/>
        <line x1="0" y1="18" x2="0" y2="-16" stroke="#d50000" stroke-width="4" stroke-linecap="round"/><polygon points="0,-34 -11,-14 11,-14" fill="#d50000" stroke="#d50000" stroke-width="1" stroke-linejoin="round"/>
        <circle cx="0" cy="18" r="4" fill="#d50000" stroke="#fff" stroke-width="2"/></g></svg>`;
      L.marker(c, { icon: L.divIcon({ className: '', html: I18n.html`<div style="transform:translate(-50%,-50%)">${svg}</div>`, iconSize: [0, 0] }), interactive: false }).bindTooltip(`azimuth ${Math.round(p.azimuth)}\u00b0`).addTo(G.access);
    }
  }
  G.access.addTo(map);
}

/* ---------------------------------------------------------------- panels */
const stale = () => (S.polling ? I18n.htmlText('<p><span class="badge warn">updating...</span></p>') : '');
function legend(items, dots) { return I18n.html`<div class="legend">${items.map(([c, t, sq]) => I18n.html`<span><i class="${dots ? 'dot' : ''}${sq ? ' sq' : ''}" style="background:${c}"></i>${t}</span>`).join('')}</div>`; }
function bar(parts) { const tot = parts.reduce((s, p) => s + p[0], 0) || 1; return I18n.html`<div class="bar">${parts.map(([n, c, t]) => I18n.html`<span title="${t}: ${n}" style="width:${100 * n / tot}%;background:${c}"></span>`).join('')}</div>`; }

function renderPanel() {
  const P = $('#panel'), p = S.project;
  const headings = {roads:['Prepare the roads','Follow visible paths, keep junctions connected, and flag uncertain ends.'],ids:['Check street identity','Select a junction to split or group segments into a street.'],names:['Give places a name','Name priority streets so smaller streets can inherit a clear address.'],addresses:['Review house access','Select a building to check its address and choose the correct road access.']};
  const heading=headings[S.tab];
  $('#workspace-title').textContent=I18n.t(heading[0]); $('#workspace-description').textContent=I18n.t(heading[1]);
  if (!p.reference) { P.innerHTML = I18n.htmlText('<h3>Choose the reference point</h3><p>Click the map near the node the numbering starts from (for example a crossroads).</p>'); return; }
  P.innerHTML = { roads: panelRoads, ids: panelIds, names: panelNames, addresses: panelAddresses }[S.tab]();
  AuthUI.applyPermissions();
}

/* what is selected on the map (a street, a house, a node of a new road) is shown and edited in a popup on the map, not in the side panel */
function selectedStreetBox() {
  return I18n.html`<p class="muted">Click a street${S.tab === 'ids' ? ' or a white node' : ''} on the map: a popup opens.</p>`;
}
const tri = (v) => [['null', 'unknown'], ['true', 'yes'], ['false', 'no']].map(([k, t]) => I18n.html`<option value="${k}" ${String(v === undefined ? null : v) === k ? 'selected' : ''}>${I18n.t(t)}</option>`).join('');
function streetBox(editable) {
  if (!S.sel || S.sel.type !== 'street') return '';
  const f = streetById(S.sel.id); if (!f) return '';
  const p = f.properties; const assigned = S.project.names[String(p.original)];
  const parts = S.network.streets.features.filter((g) => g.properties.original === p.original);
  let h = I18n.html`<h3>Street ${p.id} <span class="badge">${esc(p.status)}</span></h3>
    <p><strong>${esc(p.display || '(no name yet)')}</strong></p>
    <table><tr><td>original id</td><td>${p.original}${parts.length > 1 ? ` &middot; part ${p.part + 1} of ${parts.length}` : ''}</td></tr>
    <tr><td>length</td><td>${fmt(p.length)} m &middot; rank ${p.rank} (${esc(p.highway)})</td></tr>
    <tr><td>distance to reference</td><td>${fmt(p.d0)} - ${fmt(p.d1)} m</td></tr>
    ${p.parent ? I18n.html`<tr><td>derived from</td><td>street ${p.parent}${p.root && p.root !== p.parent ? ` (main street ${p.root})` : ''}</td></tr>` : ''}</table>`;
  h += I18n.html`<div class="row attrs"><select class="attr-edit" data-attr="rank" data-orig="${p.original}" ${parts.length > 1 ? `data-part="${p.part}"` : ''} title="Street rank">${RANK_NAMES.map((t, i) => I18n.html`<option value="${i}" ${p.rank === i ? 'selected' : ''}>${I18n.t(t)}</option>`).join('')}</select>
    <label>drivable <select class="attr-edit" data-attr="drivable" data-orig="${p.original}" ${parts.length > 1 ? `data-part="${p.part}"` : ''}>${tri(p.drivable)}</select></label>
    <label>paved <select class="attr-edit" data-attr="paved" data-orig="${p.original}" ${parts.length > 1 ? `data-part="${p.part}"` : ''}>${tri(p.paved)}</select></label></div>${parts.length > 1 ? I18n.htmlText('<p class="muted">Applies to this part only.</p>') : ''}`;
  if (parts.length > 1) h += I18n.html`<p class="muted">Cut parts: ${parts.map((g) => I18n.html`<a href="#" data-act="select" data-id="${g.properties.id}">${g.properties.id}</a>`).join(', ')}</p>`;
  if (editable) {
    const dup = assigned && S.network.streets.features.some((g) => g.properties.original !== p.original && g.properties.name && g.properties.name.toLowerCase() === assigned.toLowerCase());
    h += I18n.html`<h3>Name of street ${p.original}${parts.length > 1 ? ' (all parts)' : ''}</h3>
      <input type="text" id="name-input" placeholder="Street name (empty = no name)" value="${esc((assigned === '0' ? '' : assigned) || (assigned === '0' ? '' : p.status === 'named' ? (parts.find((g) => g.properties.part === 0) || f).properties.name : ''))}">
      <div class="row"><button class="primary" data-act="set-name" data-id="${p.original}">Set name</button>
      ${assigned ? I18n.html`<button data-act="clear-name" data-id="${p.original}">Back to the OSM name</button>` : ''}<button class="danger" data-act="delete-name" data-id="${p.original}">Delete name</button></div>
      ${dup ? I18n.htmlText('<p><span class="badge warn">this name is already used by another street</span></p>') : ''}
      <p class="muted">Saved at once; the street ids update by themselves.</p>`;
  }
  return h;
}

function panelIds() {
  if (!S.network) return I18n.html`<h3>Street ids</h3><p>Computing the street ids...</p>`;
  const st = S.network.stats, sg = S.network.streets.features.map((f) => f.properties);
  const cut = {}; sg.filter((p) => p.part > 0 || sg.some((q) => q.original === p.original && q.part > 0)).forEach((p) => { (cut[p.original] = cut[p.original] || []).push(p); });
  const cutRows = Object.entries(cut).slice(0, 60).map(([o, ps]) => I18n.html`<tr class="click" data-act="select" data-id="${ps[0].id}"><td>${o}</td><td>${ps.map((q) => q.id).join(', ')}</td><td>${esc(ps[0].name || '')}</td></tr>`).join('');
  const s = S.project.settings;
  return I18n.html`<h3>Street ids</h3>${stale()}
  <h3>Colour by</h3><div class="row"><select id="color-mode">${[['id', 'street id'], ['distance', 'distance to the reference'], ['rank', 'street rank'], ['drivable', 'drivable'], ['paved', 'paved']].map(([v, t]) => I18n.html`<option value="${v}" ${S.idColor === v ? 'selected' : ''}>${I18n.t(t)}</option>`).join('')}</select></div>${colourLegend()}
    <table><tr><td>streets</td><td><strong>${st.streets}</strong></td></tr><tr><td>original ids</td><td>${st.original_ids}</td></tr>
    <tr><td>new ids from cutting</td><td>${st.new_ids_from_cutting} (${st.cut_points} cut points)</td></tr></table>
    ${selectedStreetBox(false)}
    <details class="sec"><summary>Streets that were cut (${Object.keys(cut).length})</summary><table><tr><th>orig</th><th>ids</th><th>name</th></tr>${cutRows || I18n.htmlText('<tr><td colspan="3" class="muted">none</td></tr>')}</table></details>
    <details class="sec"><summary>Settings</summary>
      <p>Derivation levels <input type="number" min="0" max="4" id="set-levels" value="${s.max_levels}"></p>
      <p>Second-level names (x-y-name) for drive roads shorter than (m) <input type="number" min="0" id="set-deep" value="${s.second_level_max_drive_m}"> <span class="muted">(non-drive roads always may)</span></p>
      <p>House step (m) <input type="number" step="0.5" id="set-house" value="${s.house_step_m}"></p>
      <p>Sub-street step (m) <input type="number" step="1" id="set-branch" value="${s.branch_step_m}"></p>
      <p>Houses farther than (m) from a road get no address <input type="number" id="set-dist" value="${s.max_house_distance_m}"></p>
      <p><label><input type="checkbox" id="set-conn" ${s.connect_backbone ? 'checked' : ''}> named streets must connect to the reference through named streets</label></p>
      <button data-act="apply-settings">Apply settings</button></details>`;
}

function panelNames() {
  if (!S.network) return I18n.html`<h3>Street names</h3><p>Computing the street ids...</p>`;
  const st = S.network.stats, sg = S.network.streets.features.map((f) => f.properties);
  const c = (k) => sg.filter((p) => p.status === k).length; const d1 = sg.filter((p) => p.status === 'derived' && p.level === 1).length, d2 = sg.filter((p) => p.status === 'derived' && p.level === 2).length;
  const rows = S.network.plan.slice(0, S.planAll ? 1e6 : 25).map((r) => I18n.html`<tr class="click" data-act="goto-plan" data-id="${r.street_ids.split(';')[0]}"><td>${r.order}</td><td>${r.street_id_original}</td><td>${fmt(r.length_m)}</td><td>${r.covers}</td><td>${esc(r.reason)}</td></tr>`).join('');
  return I18n.html`<h3>Street names</h3>${stale()}
        ${bar([[c('named'), STATUS.named, 'named'], [d1, STATUS.derived, 'derived 1'], [d2, STATUS.derived2, 'derived 2'], [c('planned'), STATUS.planned, 'to name'], [c('uncovered'), STATUS.uncovered, 'uncovered']])}
    ${legend([[STATUS.named, `named ${c('named')}`], [STATUS.derived, `x-name ${d1}`], [STATUS.derived2, `x-y-name ${d2}`], [STATUS.planned, `still to name ${st.to_be_named}`], ...(c('uncovered') ? [[STATUS.uncovered, `cannot be named ${c('uncovered')}`]] : []), ...(c('other') ? [[STATUS.other, `not a road ${c('other')}`]] : [])])}
    <p class="muted">${st.to_be_named} ${I18n.t(st.to_be_named === 1 ? 'street needs' : 'streets need')} a name (red). Naming them lets the others derive theirs.</p>
    ${selectedStreetBox(true)}
    <h3>Streets to name, in priority order</h3>
    <table><tr><th>#</th><th>street</th><th>m</th><th>covers</th><th>why</th></tr>${rows || I18n.htmlText('<tr><td colspan="5" class="muted">nothing left to name</td></tr>')}</table>
    ${S.network.plan.length > 25 ? I18n.html`<p><a href="#" data-act="plan-all">${S.planAll ? 'show fewer' : `show all ${S.network.plan.length}`}</a></p>` : ''}`;
}

function panelAddresses() {
  const a = S.addresses; const hasB = S.project.has.buildings;
  if (!hasB) return I18n.htmlText('<h3>Addresses</h3><p>This project has no houses to address (input/buildings.gpkg).</p>');
  let h = I18n.html`<h3>House numbers, addresses and street signs</h3>${S.project.addresses_stale || S.polling ? I18n.htmlText('<p><span class="badge warn">updating...</span></p>') : ''}`;
  if (!a) {
    const fs = S.rawBuildings ? S.rawBuildings.features : [], n = {}; fs.forEach((f) => { const u = f.properties.use || 'unknown'; n[u] = (n[u] || 0) + 1; });
    return h + I18n.html`<p>Computing the addresses... All ${fs.length || ''} surveyed buildings are shown, coloured by use.</p>` + legend([...Object.keys(USE).filter((u) => n[u]).map((u) => [USE[u], `${u} ${n[u]}`]), ...(S.pointsCount ? [['#ffffff', `reference points ${S.pointsCount} (emoji by type)`, true]] : [])], true);
  }
  const s = a.stats;
  h += bar([[s.addressed, HOUSE.ok, 'addressed'], [s.pending_name, HOUSE.pending_name, 'street still to be named'], [s.no_name, HOUSE.no_name, 'no name'], [s.no_street, HOUSE.no_street, 'no road']]);
  h += legend([[HOUSE.ok, `address ${s.addressed}`], [HOUSE.pending_name, `street to be named ${s.pending_name}`], [HOUSE.no_name, `no name ${s.no_name}`], [HOUSE.no_street, `too far from a road ${s.no_street}`], ['#ffffff', `reference points ${a.houses.features.filter((f) => f.properties.use === 'reference').length} (emoji by type)`, true]], true);
  h += I18n.html`<p class="muted">${s.houses} houses &middot; ${s.signs} street signs (blue) &middot; ${s.manual_road_points} road points chosen by hand</p>
    <input type="text" id="addr-search" placeholder="Search an address..."><div id="addr-results"></div>
    <details class="sec"><summary>Reference points on the street signs${S.pointRanks ? ` (${S.pointRanks.length})` : ''}</summary>
      <p class="muted">Rank = how important a point is for the signs.</p>
      <table>${(S.pointRanks || []).map((q) => I18n.html`<tr><td class="click" data-act="goto-point" data-lat="${q.lat}" data-lon="${q.lon}">${esc(q.name || q.info)}</td><td>${ptRankSelect(q.id, q.rank)}</td></tr>`).join('') || I18n.htmlText('<tr><td class="muted">loading...</td></tr>')}</table></details>`;
  h += I18n.htmlText('<p class="muted">Click a building to see its address, side of the road and road point.</p>');
  return h;
}

const PT_RANKS = ['0 not on the signs', '1 landmark', '2 important', '3 local'];
const ptRankSelect = (id, v) => I18n.html`<select class="pt-rank" data-id="${esc(id)}">${PT_RANKS.map((t, i) => I18n.html`<option value="${i}" ${v === i ? 'selected' : ''}>${I18n.t(t)}</option>`).join('')}</select>`;
function houseBox() {
  if (!S.sel || S.sel.type !== 'house') return '';
  const p = S.sel.props, row = (k, v) => (v === null || v === undefined || v === '' ? '' : I18n.html`<tr><td>${k}</td><td>${esc(String(v))}</td></tr>`);
  const az = p.azimuth !== null && p.azimuth !== undefined && p.azimuth === p.azimuth ? `${Math.round(p.azimuth)}\u00b0` : null;
  return I18n.html`<h3>${esc(p.address || 'no address')}</h3>
    <table>${row('number', p.number)}${row('street', p.street ? `${p.street} (id ${p.street_id})` : p.street_id !== null && p.street_id !== undefined ? `id ${p.street_id}, no name yet` : null)}
      ${row('side', p.side ? `${p.side}${p.side_source === 'azimuth' ? ' (from the azimuth)' : ' (from the position)'}` : null)}${row('azimuth', az)}
      ${p.use === 'reference' ? I18n.html`<tr><td>sign rank</td><td>${ptRankSelect(p.building_id, p.sign_rank)}</td></tr>` : ''}${row('use', p.use)}${row('name', p.name)}${row('residents', p.residents)}${row('road point', p.source === 'manual' ? 'chosen by hand' : p.road_lat ? 'nearest road (purple)' : null)}${row('status', p.status)}</table>
    <div class="row"><button data-act="pick-access" data-id="${esc(p.building_id)}">Choose the road point</button>
    ${p.source === 'manual' ? I18n.html`<button class="danger" data-act="clear-access" data-id="${esc(p.building_id)}">Nearest road</button>` : ''}</div>`;
}

function nodePanel(staged) {
  if (!S.sel || S.sel.type !== 'node') return '';
  if (S.sel.props.effective) return I18n.htmlText('<h3>Current road endpoint</h3><p>Right-click its road and choose Connect an endpoint to a road. This works on the current cut and corrected sections.</p>');
  const p = S.sel.props, s = p.suggestion, k = nodeKind(p);
  const what = p.state === 'connected' ? `Connected to ${p.to === 'edge' ? 'a point of an existing road' : 'node ' + p.node} (${p.manual ? 'by hand' : 'automatic'})` : 'Not connected';
  return I18n.html`<h3>Selected node</h3><p>${what}${s ? ` &middot; suggestion: ${s.type === 'node' ? 'node' : 'a point of the road'} ${fmt(s.distance_m, 1)} m away` : ''}</p>
    <div class="row"><button class="primary" data-act="connect-click" data-key="${esc(p.key)}">${k === 'connected' ? 'Connect somewhere else' : 'Connect'}: click a road or node</button>
    ${s ? I18n.html`<button data-act="connect-sug" data-key="${esc(p.key)}">Use the suggestion</button>` : ''}
    <button data-act="leave-node" data-key="${esc(p.key)}">${k === 'connected' ? 'Disconnect' : 'Leave unconnected'}</button><button data-act="reset-node" data-key="${esc(p.key)}">Automatic</button></div>`;
}
function panelStage() {
  const st = S.stage, pv = st.pv, sm = pv.summary;
  const rep = pv.replaced.features.map((f, i) => { const q = f.properties, kept = q.kept.length > 0 && q.removed.length === 0;
    return I18n.html`<tr><td>${f.properties.refs.length} road${f.properties.refs.length > 1 ? 's' : ''}, ${fmt(q.old_length, 0)} m</td><td>${fmt(q.new_length, 0)} m</td><td><label><input type="checkbox" data-keep="${i}" ${kept ? 'checked' : ''}> keep the existing</label></td></tr>`; }).join('');
  return I18n.html`<h3>New streets from ${esc(st.filename)}</h3>
    <table><tr><td>lines in the file</td><td><strong>${sm.lines}</strong></td></tr><tr><td>connection nodes</td><td><strong>${sm.connected}</strong> (${sm.auto} automatic)</td></tr>
    <tr><td>ends not connected</td><td><strong>${sm.not_connected}</strong></td></tr><tr><td>roads not reaching the network</td><td>${sm.isolated}</td></tr>
    <tr><td>existing roads replaced</td><td><strong>${sm.replaced}</strong></td></tr></table>
    ${legend([['#00e5ff', 'connection node (thick)'], ['#ff6d00', 'end not connected'], ['#bdbdbd', 'inner node'], ['#d50000', 'existing road replaced']], true)}
    <p class="muted">Click a node to connect or disconnect it. Nothing is saved until you accept.</p>
    ${rep ? I18n.html`<h3>Replaced roads</h3><p class="muted">Same end nodes and a length within 10 %: the new street wins. Tick the ones that must stay.</p><table><tr><th>existing</th><th>new</th><th></th></tr>${rep}</table>` : ''}
    <div class="row sticky"><button class="primary" data-act="stage-accept">Accept and add ${sm.lines} streets</button><button data-act="stage-connect-suggested">Connect ends with a suggestion</button><button class="danger" data-act="stage-cancel">Cancel</button></div>`;
}
/* ---- basic road editing: draw a new road, or prolong a dead end (the new part joins that street); the ends snap to nodes and roads */
function startDraw(mode) {
  S.reshapePick = null;
  S.stage = null; S.del = null; S.sel = null;
  S.draw = { mode, tool: 'points', snap: true, pending: new Set(), pts: [], snaps: [], start: null, join: null, name: '', rank: 4, drivable: null, paved: null, ends: null };
  S.draw.phase = mode === 'route' ? 'start' : 'trace'; S.draw.strokeHistory = [];
  setMode('draw'); map.doubleClickZoom.disable();
  if (mode === 'prolong') api('/base/ends').then((e) => { if (S.draw) { S.draw.ends = e; drawTab(); } });
  drawTab();
}
function stopDraw() { if (S.draw && S.draw.preview) map.removeLayer(S.draw.preview); S.draw = null; map.dragging.enable(); setMode(null); map.doubleClickZoom.enable(); drawTab(); }
async function pickRoadToReshape(action = 'reshape') {
  S.stage = null; S.del = null; S.sel = null;
  S.reshapePick = await api('/roads/geometry');
  S.reshapePick.action = action;
  drawTab();
}
function drawReshapeChoices() {
  L.geoJSON({ type: 'FeatureCollection', features: S.reshapePick.edges.features.filter(inWorkArea) }, { style: { color: '#00bcd4', weight: 6 }, onEachFeature: (f, layer) => {
    layer.bindTooltip(`Street ${f.properties.street_id ?? '?'}: click to reshape this section`);
    layer.on('click', (e) => {
      L.DomEvent.stopPropagation(e);
      const revision = S.reshapePick.revision;
      if (S.reshapePick.action === 'cut') {
        S.cut = { feature: f, revision, points: [] }; S.reshapePick = null;
        setMode('cut'); drawTab(); return;
      }
      startDraw('reshape');
      const d = S.draw;
      d.ref = f.properties.ref; d.revision = revision; d.snap = false;
      d.pts = f.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
      d.snaps = d.pts.map(() => null);
      d.original = d.pts.map(p => [...p]);
      drawTab();
    });
  } }).addTo(G.roadEdges); G.roadEdges.addTo(map);
}
async function addCutPoint(ll) {
  const c = S.cut; if (!c || c.busy || c.points.length >= 2) return;
  c.points.push([ll.lng, ll.lat]); c.preview = null;
  if (c.points.length === 2) {
    c.busy = true;
    try { c.preview = await api('/roads/cut/preview', { ref: c.feature.properties.ref, points: c.points }); }
    catch (e) { c.points.pop(); toast(e.message, 6000); }
    finally { c.busy = false; }
  }
  if (S.cut === c) drawTab();
}
function drawCutLayers() {
  const c = S.cut;
  L.geoJSON(c.feature, { style: { color: '#f9a825', weight: 7 }, onEachFeature: (f, layer) => layer.on('click', e => { L.DomEvent.stopPropagation(e); addCutPoint(e.latlng); }) }).addTo(G.roadEdges);
  c.points.forEach(([lon, lat], i) => L.circleMarker([lat, lon], { radius: 7, color: '#d50000', fillOpacity: 1 }).bindTooltip(`Cut point ${i+1}`).addTo(G.roadNodes));
  if (c.preview) L.geoJSON(c.preview, { style: { color: '#d50000', weight: 9 }, interactive: false }).addTo(G.roadEdges);
  G.roadEdges.addTo(map); G.roadNodes.addTo(map);
}
function snapMetres(ll) { const mpp = 156543.03 * Math.cos(ll.lat * Math.PI / 180) / Math.pow(2, map.getZoom()); return Math.max(1.5, Math.min(8, 9 * mpp)); }   // about 9 screen pixels, never more than 8 m
/* Always resolve context actions against the effective graph, including cut fragments. */
function nearbyRoads(fc, ll) {
  const p = map.latLngToContainerPoint(ll);
  return fc.features.map(f => {
    let distance = Infinity; const points = f.geometry.coordinates.map(c => map.latLngToContainerPoint([c[1], c[0]]));
    for (let i = 1; i < points.length; i++) {
      const a = points[i-1], b = points[i], dx = b.x-a.x, dy = b.y-a.y;
      const t = Math.max(0, Math.min(1, ((p.x-a.x)*dx+(p.y-a.y)*dy)/(dx*dx+dy*dy || 1)));
      distance = Math.min(distance, Math.hypot(p.x-a.x-t*dx, p.y-a.y-t*dy));
    }
    return { f, distance };
  }).filter(x => x.distance <= 12).sort((a,b) => a.distance-b.distance).map(x => x.f);
}
function cancelJoin() { S.join = null; setMode(null); drawTab(); }
map.on('contextmenu', async e => {
  if (S.locked || S.draw || S.stage) return;
  try {
    const current = await api('/roads/geometry'), choices = nearbyRoads(current.edges, e.latlng);
    if (!choices.length) { toast('No current road section here. Zoom in and right-click closer to the line.'); return; }
    const box = document.createElement('div'); box.className = 'road-context';
    box.innerHTML = I18n.html`<strong>Road editing</strong><select aria-label="Road section">${choices.map((f,i)=>I18n.html`<option value="${i}">${esc(f.properties.ref)} · street ${esc(f.properties.street_id ?? '?')}</option>`).join('')}</select><p class="road-end-info"></p><div>${[['reshape','Reshape this section'],['cut','Delete a portion (two points)'],['whole','Delete this whole section'],['join','Connect an endpoint to a road'],['extend','Extend from a free endpoint'],['ids','Street IDs / group segments'],['names','Street names']].map(([a,t])=>I18n.html`<button data-road-action="${a}" style="display:block;margin:4px 0">${I18n.t(t)}</button>`).join('')}</div>`;
    const selected = () => choices[+box.querySelector('select').value];
    const info = () => { const f=selected(),p=f.properties; box.querySelector('.road-end-info').textContent = `${fmt(p.length_m,1)} m · A: ${I18n.t(p.degree_u === 1 ? 'free end' : 'junction')} · B: ${I18n.t(p.degree_v === 1 ? 'free end' : 'junction')}`; G.hl.clearLayers(); L.geoJSON(f,{style:{color:'#00e5ff',weight:8,opacity:.65},interactive:false}).addTo(G.hl); G.hl.addTo(map); };
    box.querySelector('select').onchange = info; info();
    box.addEventListener('click', async ev => {
      const action = ev.target.dataset.roadAction; if (!action) return;
      try {
        const f=selected(), revision=current.revision;
        map.closePopup(); await switchTab('roads');
        S.cut=null; S.join=null; S.reshapePick=null; S.del=null;
        if (action === 'ids' || action === 'names') { await switchTab(action); return; }
        if (action === 'reshape') {
          startDraw('reshape'); Object.assign(S.draw,{ref:f.properties.ref, revision, snap:false, pts:f.geometry.coordinates.map(([x,y])=>[y,x])});
          S.draw.snaps=S.draw.pts.map(()=>null); S.draw.original=S.draw.pts.map(p=>[...p]); drawTab(); return;
        }
        if (action === 'cut' || action === 'whole') {
          S.cut={feature:f,revision,points:[],whole:action==='whole'};
          if (S.cut.whole) { const c=f.geometry.coordinates; S.cut.points=[c[0],c[c.length-1]]; S.cut.preview=await api('/roads/cut/preview',{ref:f.properties.ref,points:S.cut.points,whole:true}); }
          setMode('cut'); drawTab(); return;
        }
        S.join={feature:f,revision,current,extend:action==='extend'}; setMode('join'); drawTab();
      } catch (err) { toast(err.message,6000); }
    });
    L.popup({maxWidth:350}).setLatLng(e.latlng).setContent(box).openOn(map);
  } catch (err) { toast(err.message,6000); }
});
function drawJoinLayers() {
  const j=S.join, f=j.feature, p=f.properties, coords=f.geometry.coordinates;
  banner(j.source ? 'Click the target road at the junction to connect this endpoint.' : 'Choose endpoint A or B. Orange endpoints are free; a junction already connects roads.',true);
  const cancel=$('#banner-cancel'); if (cancel) cancel.onclick=cancelJoin;
  L.geoJSON(j.current.edges,{style:{color:'#ffd54f',weight:4},onEachFeature:(target,layer)=>layer.on('click',async e=>{
    L.DomEvent.stopPropagation(e); if (!j.source) return;
    const body={ref:p.ref,source_node:j.source.node,target_ref:target.properties.ref,lon:e.latlng.lng,lat:e.latlng.lat,revision:j.revision};
    try {
      const preview=await api('/roads/join/preview',body);
      if (preview.already_connected) { toast('These roads already share a junction. No connection change is needed.'); return; }
      const box=document.createElement('div');
      box.innerHTML=I18n.html`<p>${I18n.msg`Move this endpoint ${fmt(preview.gap_m,1)} m to the target road and create a shared junction? Street IDs are preserved.`}</p><button>Connect here</button>`;
      box.querySelector('button').onclick=async()=>{
        try { await api('/roads/join',body); map.closePopup(); S.join=null; setMode(null); await afterEdit(); await loadRoads(); drawTab(); toast('Roads connected at a shared junction.'); }
        catch(err) { toast(err.message,6000); }
      };
      const c=preview.point.coordinates; L.popup().setLatLng([c[1],c[0]]).setContent(box).openOn(map);
    } catch(err) { toast(err.message,6000); }
  })}).addTo(G.roadEdges); G.roadEdges.addTo(map);
  [[p.u,p.degree_u,coords[0],'A'],[p.v,p.degree_v,coords[coords.length-1],'B']].forEach(([node,degree,c,label])=>{
    L.marker([c[1],c[0]],{icon:L.divIcon({className:'',html:I18n.html`<strong style="background:${degree===1?'#ff9800':'#aaa'};padding:5px;border:2px solid white">${label}</strong>`})}).bindTooltip(degree===1?'Choose this free endpoint':'Already a junction').on('click',e=>{
      L.DomEvent.stopPropagation(e);
      if (degree!==1) { toast('This endpoint already belongs to a junction. Choose a free endpoint.'); return; }
      if (j.extend) {
        startDraw('prolong'); const d=S.draw; S.join=null; d.start={type:'node',node,lon:c[0],lat:c[1]}; d.join=p.street_id; d.pts=[[c[1],c[0]]]; d.snaps=[d.start]; drawTab(); return;
      }
      j.source={node}; drawTab();
    }).addTo(G.roadNodes);
  }); G.roadNodes.addTo(map);
}
async function snapAt(ll) { try { const m = snapMetres(ll); return (await api('/roads/snap', { lat: ll.lat, lon: ll.lng, max_m: m, node_m: m })).target; } catch (e) { return null; } }
async function addDrawPoint(ll) {                            // every point snaps to a node or a road under the mouse (about 9 pixels, at most 8 m), so the new road gets connected there
  const d = S.draw; if (!d || d.saving) return;
  if (d.mode === 'route') {
    if (d.picking || d.reached) return;
    if (d.phase !== 'trace') {
      d.picking = true;
      const t = d.snap ? await snapAt(ll) : null;
      if (S.draw !== d) return;
      const p = t ? [t.lat, t.lon] : [ll.lat, ll.lng];
      if (d.phase === 'start') { d.pts = [p]; d.snaps = [t]; d.start = t; d.phase = 'end'; }
      else { d.target = p; d.endTarget = t; d.phase = 'trace'; }
      d.picking = false; drawTab(); return;
    }
    const reaches = map.latLngToContainerPoint(ll).distanceTo(map.latLngToContainerPoint(d.target)) <= 12;
    d.pts.push(reaches ? [...d.target] : [ll.lat, ll.lng]); d.snaps.push(reaches ? d.endTarget : null); d.reached = reaches;
    drawTab(); return;
  }
  if (d.mode === 'prolong' && !d.start) { toast('Click a dead end (orange dot) first'); return; }
  // Reserve the vertex before the request: rapid clicks must keep their order
  // even if snap responses arrive out of order.
  const point = [ll.lat, ll.lng];
  d.pts.push(point); d.snaps.push(null);
  drawTab();
  if (!d.snap) return;
  const request = snapAt(ll); d.pending.add(request);
  try {
    const t = await request; const i = d.pts.indexOf(point);
    if (S.draw !== d || !d.snap || i < 0) return;
    if (t) { d.pts[i] = [t.lat, t.lon]; d.snaps[i] = t; }
    if (i === 0) d.start = t;
    drawTab();
  } finally { d.pending.delete(request); }
}
let snapTimer = null;
map.on('mousemove', (e) => {                                  // the snap target under the mouse is shown before clicking
  if (!S.draw || !S.draw.snap || S.draw.stroke) return; clearTimeout(snapTimer);
  const d = S.draw;
  snapTimer = setTimeout(async () => {
    if (S.draw !== d || !d.snap) return;
    const t = await snapAt(e.latlng); if (S.draw !== d || !d.snap) return; G.hl.clearLayers(); if (!t) return;
    L.circleMarker([t.lat, t.lon], { radius: t.type === 'node' ? 10 : 7, color: '#00e676', weight: 3, fill: false, interactive: false }).addTo(G.hl); G.hl.addTo(map);
  }, 90);
});
// Freehand strokes remain a draft until Finish. Simplify in screen pixels so
// a long drag does not create thousands of almost identical vertices.
function beginDrawStroke(e) {
  const d = S.draw;
  if (!d || d.tool !== 'freehand' || d.saving || d.reached || d.phase !== 'trace' || e.originalEvent.button !== 0) return;
  if (d.mode === 'prolong' && !d.start) return;
  d.stroke = [e.latlng];
  d.preview = L.polyline([e.latlng], { color: '#00e5ff', weight: 5, interactive: false }).addTo(map);
}
map.on('mousedown', beginDrawStroke);
map.on('mousemove', (e) => {
  const d = S.draw; if (!d || !d.stroke) return;
  const last = map.latLngToContainerPoint(d.stroke[d.stroke.length - 1]);
  if (last.distanceTo(e.containerPoint) < 3) return;
  d.stroke.push(e.latlng); d.preview.setLatLngs(d.stroke);
});
function endDrawStroke() {
  const d = S.draw; if (!d || !d.stroke) return;
  const pts = L.LineUtil.simplify(d.stroke.map((ll) => map.latLngToContainerPoint(ll)), 2);
  map.removeLayer(d.preview); d.stroke = null; d.preview = null;
  if (pts.length < 2) return;
  const stroke = pts.map(p => { const ll = map.containerPointToLatLng(p); return [ll.lat, ll.lng]; });
  if (d.mode !== 'reshape' && d.pts.length) {
    const last = d.pts[d.pts.length-1];
    if (map.latLngToContainerPoint(last).distanceTo(map.latLngToContainerPoint(stroke[0])) > 12) {
      toast('Start the next stroke at the last point; no gap has been joined.', 6000); return;
    }
    stroke[0] = [...last];
  }
  d.strokeHistory.push({ pts: d.pts.map(p => [...p]), snaps: [...d.snaps], reached: d.reached });
  if (d.mode === 'reshape') {
    // Retain both network junctions while replacing the interior trace.
    const first = L.latLng(d.original[0]), last = L.latLng(d.original[d.original.length - 1]);
    if (first.distanceTo(stroke[0]) + last.distanceTo(stroke[stroke.length - 1]) > first.distanceTo(stroke[stroke.length - 1]) + last.distanceTo(stroke[0])) stroke.reverse();
    d.pts = [d.original[0], ...stroke, d.original[d.original.length - 1]];
    d.snaps = d.pts.map(() => null);
  } else {
    const hadPoints = d.pts.length > 0;
    stroke.slice(hadPoints ? 1 : 0).forEach(p => { d.pts.push(p); d.snaps.push(null); });
    if (d.mode === 'route' && map.latLngToContainerPoint(d.pts[d.pts.length-1]).distanceTo(map.latLngToContainerPoint(d.target)) <= 12) {
      d.pts[d.pts.length-1] = [...d.target]; d.snaps[d.snaps.length-1] = d.endTarget; d.reached = true;
    }
  }
  drawTab();
}
document.addEventListener('mouseup', endDrawStroke);
window.addEventListener('blur', endDrawStroke);
async function finishDraw(recompute) {
  endDrawStroke();
  const d = S.draw; if (!d || d.pts.length < 2 || d.saving) return;
  if (d.mode === 'route' && !d.reached && !d.uncertain) { toast('Continue to the planned end, or mark this endpoint as unclear.'); return; }
  d.saving = true;
  try {
    await Promise.all([...d.pending]);
    if (S.draw !== d) return;
    if (d.mode === 'reshape') {
      await api('/roads/geometry', { ref: d.ref, revision: d.revision, coords: d.pts.map(([lat, lon]) => [lon, lat]) });
      stopDraw(); await afterEdit(); await loadRoads(); drawTab();
      toast('Road reshaped. Junctions and street identity retained; recompute to update addresses.');
      if (recompute) recomputeIfStale();
      return;
    }
    const last = d.pts.length - 1;
    if (d.uncertain) d.snaps[last] = null;
    if (d.snap) {
      for (const i of [0, last]) {
      if (i === last && d.uncertain) { d.snaps[i] = null; continue; }
      if (!d.snaps[i]) d.snaps[i] = await snapAt(L.latLng(d.pts[i][0], d.pts[i][1]));
        if (d.snaps[i] && Number.isFinite(d.snaps[i].lat) && Number.isFinite(d.snaps[i].lon)) d.pts[i] = [d.snaps[i].lat, d.snaps[i].lon];
      }
    }
    if (S.draw !== d) return;
    if (d.snaps[last]) d.pts[last] = [d.snaps[last].lat, d.snaps[last].lon];
    const cuts = [0]; for (let i = 1; i < last; i++) if (d.snaps[i]) cuts.push(i); cuts.push(last);          // a snapped point in the middle splits the line there
    const pieces = cuts.slice(1).map((c, k) => { const a = cuts[k]; return { coords: d.pts.slice(a, c + 1).map(([la, lo]) => [lo, la]), start: d.snaps[a] || (d.snap ? null : { type: 'none' }), end: d.snaps[c] || (d.snap ? null : { type: 'none' }) }; });
    const name = ($('#draw-name') && $('#draw-name').value.trim()) || d.name || null;
    await api('/roads/draw', Object.assign({ pieces, name, join: d.join, end_review: d.uncertain ? 'uncertain' : undefined }, d.mode !== 'prolong' ? { rank: d.rank, drivable: d.drivable, paved: d.paved } : {}));
    const mode = d.mode; stopDraw(); await afterEdit(); await loadRoads(); toast(`Road added (${d.snaps.filter(Boolean).length} connections)`);
    drawTab();
    if (recompute) recomputeIfStale();
  } finally { d.saving = false; }
}
function drawDrawLayers() {
  const epoch = S.renderEpoch;
  const d = S.draw;
  if (d.target) L.marker(d.target, { icon: L.divIcon({ className: 'road-target', html: 'End', iconSize: [30, 22] }) }).bindTooltip('Planned endpoint — no line is added until you trace to it').on('click', e => { L.DomEvent.stopPropagation(e); if (d.tool === 'points') addDrawPoint(L.latLng(d.target)); }).addTo(G.roadNodes);
  G.roadNodes.addTo(map);
  api('/base/streets').then((b) => { S.baseStreets = b; if (S.tab !== 'roads' || epoch !== S.renderEpoch) return; G.base.clearLayers(); L.geoJSON(b, { style: { color: '#ffd54f', weight: 2, opacity: .7 }, interactive: false }).addTo(G.base); G.base.addTo(map); });
  if (d.mode === 'prolong' && !d.start && d.ends) {
    d.ends.forEach(([node, lon, lat, sid]) => L.circleMarker([lat, lon], { radius: 7, color: '#000', weight: 1.5, fillColor: '#ff6d00', fillOpacity: 1 })
      .bindTooltip(`dead end of street ${sid === null ? '?' : sid} - click to prolong it`)
      .on('click', (e) => { L.DomEvent.stopPropagation(e); d.start = { type: 'node', node }; d.join = sid; d.pts = [[lat, lon]]; d.snaps = [d.start]; drawTab(); }).addTo(G.roadNodes));
    G.roadNodes.addTo(map);
  }
  if (d.pts.length) {
    if (d.original) L.polyline(d.original, { color: '#f57c00', weight: 4, dashArray: '3 6', interactive: false }).addTo(G.roadEdges);
    const line = L.polyline(d.pts, { color: '#00e5ff', weight: 5, dashArray: '8 6', interactive: false }).addTo(G.roadEdges);
    d.pts.forEach((p, i) => {
      // The fixed start of a prolongation must stay on its original node.
      const fixed = ((d.mode === 'prolong' || d.mode === 'route') && i === 0) || (d.mode === 'route' && d.reached && i === d.pts.length-1) || (d.mode === 'reshape' && (i === 0 || i === d.pts.length - 1));
      const marker = L.marker(p, { draggable: !fixed && !d.saving && d.tool !== 'freehand', icon: L.divIcon({ className: `road-vertex${fixed ? i === 0 ? ' road-start' : ' road-end' : ''}`, iconSize: [12, 12], iconAnchor: [6, 6] }) }).addTo(G.roadEdges);
      marker.bindTooltip(fixed ? i === 0 ? 'Fixed start junction (green)' : 'Fixed end junction (red)' : 'Drag to reshape; right-click to remove');
      marker.on('drag', () => { const ll = marker.getLatLng(); d.pts[i] = [ll.lat, ll.lng]; d.snaps[i] = null; line.setLatLngs(d.pts); });
      marker.on('dragend', async () => {
        if (S.draw !== d) return;
        const t = d.snap ? await snapAt(marker.getLatLng()) : null;
        if (S.draw !== d) return;
        if (t) { d.pts[i] = [t.lat, t.lon]; d.snaps[i] = t; }
        drawTab();
      });
      marker.on('contextmenu', (e) => { L.DomEvent.stop(e.originalEvent); if (fixed) return; d.pts.splice(i, 1); d.snaps.splice(i, 1); drawTab(); });
      marker.on('mousedown click dblclick', (e) => L.DomEvent.stopPropagation(e));
      marker.on('mousedown', e => { if (d.tool === 'freehand' && i === d.pts.length-1) beginDrawStroke({ latlng: marker.getLatLng(), originalEvent: e.originalEvent }); });
      if (i > 0) {
        const a = d.pts[i - 1];
        L.circleMarker([(a[0] + p[0]) / 2, (a[1] + p[1]) / 2], { radius: 4, color: '#004d40', fillColor: '#fff', fillOpacity: .7 })
          .bindTooltip('Click to insert a bend').on('click', (e) => { L.DomEvent.stopPropagation(e); d.pts.splice(i, 0, [e.latlng.lat, e.latlng.lng]); d.snaps.splice(i, 0, null); drawTab(); }).addTo(G.roadEdges);
      }
    });
    G.roadEdges.addTo(map);
  }
}
function panelDraw() {
  const d = S.draw, n = d.pts.length;
  const msg = d.mode === 'prolong' && !d.start ? 'Click a dead end (orange dot) of the road to prolong.' : d.tool === 'freehand' ? 'Hold the left mouse button and trace the road. Release to adjust the points; Finish saves it.' : d.mode === 'reshape' ? 'Drag the white points to follow the visible road. Click a small midpoint to insert a bend; right-click a white point to remove it.' : 'Click along the road to add bends. Drag a point to reshape; click a small midpoint to insert a bend; right-click a point to remove it.';
  const sel = (id, opts, cur) => I18n.html`<select id="${id}">${opts.map(([v, t]) => I18n.html`<option value="${v}" ${String(cur) === String(v) ? 'selected' : ''}>${I18n.t(t)}</option>`).join('')}</select>`;
  const triOpts = [['null', 'unknown'], ['true', 'yes'], ['false', 'no']];
  return I18n.html`<h3>${I18n.t(d.mode === 'route' ? 'Trace a connected route' : d.mode === 'reshape' ? 'Reshape existing road' : d.mode === 'new' ? 'Draw a new road' : 'Prolong a road end')}</h3><p>${d.mode === 'route' && d.phase !== 'trace' ? d.phase === 'start' ? I18n.t('Click the starting point.') : I18n.t('Click the planned ending point. This does not draw a connecting line.') : I18n.t(msg)}</p>
    ${d.mode === 'route' && d.phase === 'trace' ? I18n.htmlText('<p>Each stroke must start at the last point. The planned end remains separate until reached. Stop and flag uncertainty where the visible path disappears.</p>') : ''}
    ${d.mode === 'reshape' ? I18n.htmlText('<p>Move the white points or insert bends between the fixed junctions. Orange shows the previous shape. Freehand replaces the interior; trace from the first endpoint toward the last. Saving does not add another road. A crossing does not create a new junction.</p>') : ''}
    <p><label>Drawing <select id="draw-tool" ${d.phase !== 'trace' ? 'disabled' : ''}><option value="points" ${d.tool === 'points' ? 'selected' : ''}>Click points</option><option value="freehand" ${d.tool === 'freehand' ? 'selected' : ''}>Freehand trace</option></select></label></p>
    ${d.mode === 'reshape' ? '' : I18n.html`<p><label><input type="checkbox" id="draw-snap" ${d.snap ? 'checked' : ''}> Snap to existing roads</label></p>`}
    <p class="muted">${n} points. Freehand mode uses dragging to draw; switch to Click points to pan the map.${d.mode === 'reshape' ? ' Existing junctions stay fixed.' : ' With snapping off, new ends stay unconnected.'}</p>
    ${d.mode === 'prolong' && d.join !== null ? I18n.html`<p class="muted">The new part becomes part of street ${d.join}.</p>` : ''}
    ${d.mode === 'reshape' ? '' : I18n.html`<p><input type="text" id="draw-name" placeholder="Name (optional)" value="${esc(d.name)}"></p>`}
    ${d.mode === 'new' || d.mode === 'route' ? I18n.html`<table class="form"><tr><td>Rank</td><td>${sel('draw-rank', RANK_NAMES.map((t, i) => [i, t]), d.rank)}</td></tr>
      <tr><td>Drivable</td><td>${sel('draw-drv', triOpts, d.drivable)}</td></tr><tr><td>Paved</td><td>${sel('draw-paved', triOpts, d.paved)}</td></tr></table>` : ''}
    ${d.mode !== 'reshape' ? I18n.html`<p><label><input type="checkbox" id="draw-uncertain" ${d.uncertain ? 'checked' : ''}> Stop here: path beyond this point is unclear</label></p>` : ''}
    <div class="row"><button class="primary" data-act="draw-finish" ${n < 2 ? 'disabled' : ''}>Finish</button><button data-act="draw-again" ${n < 2 ? 'disabled' : ''}>Finish and recompute ids</button><button class="danger" data-act="draw-cancel">Cancel</button></div>
    ${d.strokeHistory.length ? I18n.htmlText('<button data-act="draw-undo-stroke">Undo last stroke</button>') : ''}
    <div class="row">${d.mode === 'reshape' ? I18n.htmlText('<button data-act="reshape-reset">Reset draft</button>') : I18n.html`<button data-act="draw-undo" ${n < 1 || (d.mode === 'prolong' && n < 2) ? 'disabled' : ''}>Remove last point</button>`}<span class="muted">Enter finishes &middot; Esc cancels</span></div>`;
}
function panelDelete() {
  const d = S.del, n = d.sel.size, gone = S.edgeData ? S.edgeData.removed.features.length : 0;
  return I18n.html`<h3>Delete street edges</h3><p>Click edges to select them (red). Click a grey dashed edge to restore it.</p>
    <p><strong>${n}</strong> selected &middot; ${gone} deleted so far</p>
    <div class="row"><button class="danger" data-act="del-apply" ${n ? '' : 'disabled'}>Delete the ${n} selected</button><button data-act="del-clear" ${n ? '' : 'disabled'}>Clear selection</button><button data-act="del-done">Done</button></div>
    <p class="muted">One undo step; the ids update by themselves.</p>`;
}
function panelRoads() {
  if (S.join) return I18n.htmlText('<h3>Connect roads</h3><p>Choose a free endpoint A or B, then click the target road. A shared junction connects routing; street ID grouping is separate.</p><button data-act=join-cancel>Cancel connection</button>');
  if (S.reshapePick) return I18n.html`<h3>${S.reshapePick.action === 'cut' ? 'Remove part of a road' : 'Reshape an existing road'}</h3><p>Click a cyan road section${S.reshapePick.action === 'cut' ? ', then choose two cut points.' : ' to adjust its bends. Junction endpoints stay fixed.'}</p><button data-act="reshape-cancel">Cancel</button>`;
  if (S.cut) return I18n.html`<h3>Remove part of a road</h3><p>Choose two positions along this section. The portion between them appears in red; the rest stays.</p>
    ${S.cut.preview ? I18n.html`<p><strong>${fmt(S.cut.preview.properties.removed_m, 1)} m</strong> to remove; ${S.cut.preview.properties.retained_parts} outside parts retained. A gap can disconnect the network.</p>` : I18n.html`<p>${S.cut.points.length} of 2 cut points chosen.</p>`}
    <button class="danger" data-act="cut-save" ${!S.cut.preview || S.cut.busy ? 'disabled' : ''}>Delete red portion</button><button data-act="cut-reset">Reset cut points</button><button data-act="cut-cancel">Cancel</button>`;
  if (S.draw) return panelDraw();
  if (S.stage && S.stage.pv) return panelStage();
  if (S.del) return panelDelete();
  const r = S.roads || { imports: [], report: {} }, rep = r.report || {};
  const grouped = []; (r.imports || []).forEach((i) => { const g = grouped.find((x) => x.file === i.file); if (g) { g.lines += i.lines; g.ids.push(i.id); } else grouped.push({ file: i.file, lines: i.lines, ids: [i.id] }); });   // files with one name (all the roads drawn by hand) are one file
  const imports = grouped.map((i) => I18n.html`<tr><td>${esc(i.file)}</td><td>${i.lines} lines</td><td><button data-act="conn-open">Edit connections</button> <button class="danger" data-act="remove-roads" data-id="${i.ids.join(',')}">Remove</button></td></tr>`).join('');
  const dangling = (r.nodes ? r.nodes.features : []).filter((f) => rep.dangling && rep.dangling.includes(f.properties.key));
  const rows = dangling.map((f, i) => { const s = f.properties.suggestion; return I18n.html`<tr class="click" data-act="select-node" data-key="${esc(f.properties.key)}"><td>${i + 1}</td><td>${s ? `${s.type === 'node' ? 'node' : 'road'} ${fmt(s.distance_m, 1)} m away` : 'nothing near'}</td></tr>`; }).join('');
  const comp = S.components && S.components.parts.length ? I18n.html`<div class="warnbox"><strong>The network is not connected</strong>: ${S.components.parts.length} part${S.components.parts.length > 1 ? 's are' : ' is'} cut off from the main network (red dashed on the map). Connect ${S.components.parts.length > 1 ? 'them' : 'it'} by drawing or prolonging a road.<table>${S.components.parts.map((p) => I18n.html`<tr><td>part ${p.id}</td><td>${p.edges} edges &middot; ${fmt(p.length_m)} m</td><td>street${p.streets.length > 1 ? 's' : ''} ${p.streets.join(', ')}</td><td><a href="#" data-act="zoom-part" data-part="${p.id}">show</a></td></tr>`).join('')}</table></div>` : '';
  const sec = (key, title, body, open) => I18n.html`<details class="sec" data-sec="${key}" ${(S.openSec ? S.openSec[key] : open) ? 'open' : ''}><summary>${I18n.t(title)}</summary>${body}</details>`;
  if (S.connView && rep.features) {
    return I18n.html`<div class="row"><button data-act="conn-back">&larr; Back to edit streets</button></div><h3>Connection of the added streets</h3><table><tr><td>streets</td><td>${rep.features}</td></tr><tr><td>ends connected automatically</td><td>${rep.auto_connected}</td></tr>
      <tr><td>roads split to connect</td><td>${rep.split_edges}</td></tr><tr><td>ends not connected</td><td><strong>${(rep.dangling || []).length}</strong></td></tr>
      <tr><td>roads not reaching the network</td><td>${(rep.isolated || []).length}</td></tr></table>
      ${legend([['#00e5ff', 'connection node'], ['#ff6d00', 'end not connected'], ['#bdbdbd', 'inner node']], true)}
      ${r.nodes?.features.some(f=>f.properties.effective) ? I18n.htmlText('<p>Right-click a current road to connect its endpoint.</p>') : I18n.htmlText('<div class="row"><button data-act="connect-suggested">Connect the ends with a suggestion (&le; 25 m)</button></div>')}
      <p class="muted">The street ids update by themselves. Click a node of a new road to edit its connection.</p>
      <h3>Ends not connected</h3><table><tr><th>#</th><th>nearest existing road</th></tr>${rows || I18n.htmlText('<tr><td colspan="2" class="muted">all ends are connected</td></tr>')}</table>`;
  }
  let h = I18n.html`<div class="workspace-hint"><div><strong>Right-click a road to edit</strong><small>Your edits are saved automatically. Use checkpoints before a larger change.</small></div></div>${comp}<div class="row"><label>Colour by <select id="roads-color"><option value="plain" ${S.roadColor !== 'file' ? 'selected' : ''}>one colour</option><option value="file" ${S.roadColor === 'file' ? 'selected' : ''}>file</option></select></label></div>${roadsLegend()}`;
  h += sec('draw', 'Draw, prolong or reshape a road', I18n.htmlText('<div class="row"><button data-act="draw-route">Trace route: start → end</button><button data-act="draw-new">Draw a new road</button><button data-act="draw-prolong">Prolong a road end</button><button data-act="reshape-pick">Reshape an existing road</button></div>'), false);
  h += sec('add', 'Add streets from a file', I18n.htmlText('<p class="muted">KML, GeoJSON, GeoPackage or zipped shapefile</p><div class="row"><input type="file" id="roads-file" accept=".kml,.geojson,.json,.gpkg,.zip,.shp"></div>'), true);
  h += sec('del', 'Delete edges', I18n.htmlText('<div class="row"><button data-act="cut-pick">Remove part of a road...</button><button data-act="del-start">Select whole edges to delete...</button></div>'), false);
  h += sec('data', 'Street data and Google Earth', I18n.htmlText('<div class="row"><a class="btn" href="api/export/streets.csv" download="streets.csv">&#11015; Street data CSV</a><a class="btn" href="api/export/edges.kml" download>&#11015; Streets KML</a></div><p class="muted">Upload an edited street data CSV (rank, drivable, paved):</p><div class="row"><input type="file" id="attrs-file" accept=".csv"></div>'), false);
  if (imports) h += I18n.html`<h3>Files already added</h3><table>${imports}</table>`;
  return h;
}

/* ---------------------------------------------------------------- panel actions (event delegation) */
const ACT = {
  compute: () => startCompute('network'),
  'compute-addresses': () => startCompute('addresses'),
  select: (b) => selectStreet(+b.dataset.id),
  'goto-plan': (b) => selectStreet(+b.dataset.id),
  'plan-all': () => { S.planAll = !S.planAll; renderPanel(); },
  'set-name': async (b) => { const typed = $('#name-input').value.trim(), name = typed || '0'; /* an empty box deletes the name */ const old = (S.project.names || {})[b.dataset.id] || null; try { await api('/names', { street_id: +b.dataset.id, name, expected: old, user: user() }); } catch (err) { if (err.status !== 409) throw err; const cur = err.detail.current; if (!confirm(`Someone just changed this street to "${cur || '(no name)'}". Replace it with "${name}"?`)) { await refreshProject(); renderPanel(); return; } await api('/names', { street_id: +b.dataset.id, name, expected: cur, user: user() }); } await afterEdit(); toast('Name saved.'); renderPanel(); },
  'delete-name': async (b) => { await api('/names', { street_id: +b.dataset.id, name: '0', user: user() }); await afterEdit(); toast('Name deleted'); renderPanel(); },
  'clear-name': async (b) => { await api('/names', { street_id: +b.dataset.id, name: '', user: user() }); await afterEdit(); toast('Name removed'); renderPanel(); },
  'apply-settings': async () => { await api('/settings', { max_levels: +$('#set-levels').value, second_level_max_drive_m: +$('#set-deep').value, house_step_m: +$('#set-house').value, branch_step_m: +$('#set-branch').value, max_house_distance_m: +$('#set-dist').value, connect_backbone: $('#set-conn').checked, user: user() }); await afterEdit(); toast('Settings changed. Compute to apply.'); renderPanel(); },
  'pick-access': (b) => setMode('access', { id: b.dataset.id }),
  'clear-access': async (b) => { await api('/access', { building_id: b.dataset.id, clear: true, user: user() }); await afterEdit(); toast('Back to the nearest road.'); renderPanel(); },
  'remove-roads': async (b) => { const ids = b.dataset.id.split(','); if (ids.length > 1 && !confirm(`Remove all ${ids.length} parts of this file?`)) return; for (const id of ids) await api('/roads/remove', { id: +id, user: user() }); await afterEdit(); await loadRoads(); drawTab(); },
  'zoom-part': (b) => { const fs = S.components.edges.features.filter((f) => f.properties.part === +b.dataset.part); if (fs.length) map.fitBounds(L.geoJSON(fs).getBounds().pad(0.3)); },
  'del-part': async (b) => { if (!confirm(`Delete the separate part ${b.dataset.part} completely? You can undo this.`)) return; await api('/base/components/delete', { part: +b.dataset.part, user: user() }); S.components = null; await afterEdit(); await loadRoads(); toast('Part deleted.', 4000); drawTab(); },
  'conn-open': () => { S.connView = true; renderPanel(); },
  'conn-back': () => { S.connView = false; renderPanel(); },
  'connect-suggested': async () => { const r = await api('/roads/connect-suggested', { max_m: 25, user: user() }); toast(`${r.connected} ends connected`); await afterEdit(); await loadRoads(); drawTab(); },
  'select-node': (b) => { const f = S.roads.nodes.features.find((x) => x.properties.key === b.dataset.key); if (f) { S.sel = { type: 'node', key: f.properties.key, props: f.properties, ll: L.latLng(f.geometry.coordinates[1], f.geometry.coordinates[0]) }; map.setView(S.sel.ll, Math.max(map.getZoom(), 18)); renderPanel(); refreshSel(); } },
  'connect-click': (b) => setMode('connect', { key: b.dataset.key }),
  'stage-cancel': () => { clearStage(); drawTab(); },
  'stage-accept': async () => {
    const st = S.stage, sm = st.pv.summary;
    if (!confirm(`Add ${sm.lines} new streets to the project?\n${sm.connected} connection nodes, ${sm.not_connected} ends not connected, ${sm.replaced} existing roads replaced by the new ones.`)) return;
    const r = await api('/roads/import', { filename: st.filename, features: st.features, connections: st.connections, keep: st.keep, user: user() });
    clearStage(); S.fittedRoads = false; await afterEdit(); await loadRoads(); S.connView = true; toast(`${r.lines} streets added.`, 5000); drawTab();
  },
  'stage-connect-suggested': async () => {
    const st = S.stage; let n = 0;
    st.pv.nodes.features.forEach((f) => { const p = f.properties; if (p.suggestion && st.pv.dangling.includes(p.key) && p.suggestion.distance_m <= 25) { st.connections[p.key] = p.suggestion; n += 1; } });
    await previewStage(false); toast(`${n} ends connected`); drawTab();
  },
  'draw-new': () => startDraw('new'),
  'draw-route': () => startDraw('route'),
  'draw-undo-stroke': () => { const d = S.draw, previous = d.strokeHistory.pop(); if (previous) { d.pts = previous.pts; d.snaps = previous.snaps; d.reached = previous.reached; drawTab(); } },
  'reshape-pick': () => pickRoadToReshape(),
  'cut-pick': () => pickRoadToReshape('cut'),
  'join-cancel': cancelJoin,
  'cut-cancel': () => { S.cut = null; setMode(null); drawTab(); },
  'cut-reset': () => { if (S.cut && !S.cut.busy) { S.cut.points = []; S.cut.preview = null; S.cut.whole = false; drawTab(); } },
  'cut-save': async () => {
    const c = S.cut; if (!c || !c.preview || c.busy) return;
    if (!confirm(I18n.msg`Delete only the red ${fmt(c.preview.properties.removed_m, 1)} m? This may create a network gap. You can Undo.`)) return;
    c.busy = true;
    try { await api('/roads/cut', { ref: c.feature.properties.ref, points: c.points, whole: !!c.whole, revision: c.revision }); S.cut = null; setMode(null); await afterEdit(); await loadRoads(); drawTab(); toast(c.whole ? 'Selected section removed.' : 'Selected portion removed; outside pieces retained.'); }
    finally { c.busy = false; }
  },
  'reshape-cancel': () => { S.reshapePick = null; drawTab(); },
  'reshape-reset': () => { const d = S.draw; d.pts = d.original.map(p => [...p]); d.snaps = d.pts.map(() => null); drawTab(); },
  'draw-prolong': () => startDraw('prolong'),
  'draw-finish': () => finishDraw(),
  'draw-again': () => finishDraw(true),
  'draw-undo': () => { if (S.draw && S.draw.pts.length && !(S.draw.mode === 'route' && S.draw.pts.length === 1)) { S.draw.pts.pop(); S.draw.snaps.pop(); S.draw.reached = false; if (!S.draw.pts.length && S.draw.mode === 'prolong') { S.draw.start = null; S.draw.join = null; } drawTab(); } },
  'draw-cancel': () => stopDraw(),
  'del-start': () => { S.del = { sel: new Set() }; drawTab(); },
  'del-clear': () => { S.del.sel.clear(); drawTab(); },
  'del-done': () => { S.del = null; drawTab(); },
  'del-apply': async () => {
    const sel = [...S.del.sel]; if (!sel.length) return;
    if (!confirm(`Delete ${sel.length} street edge${sel.length > 1 ? 's' : ''}? You can undo this.`)) return;
    await api('/edges/remove', { edges: sel.filter((x) => !x.startsWith('f:')), features: sel.filter((x) => x.startsWith('f:')).map((x) => x.slice(2)), user: user() });
    S.del.sel.clear(); await afterEdit(); await loadRoads(); toast(`${sel.length} edge${sel.length > 1 ? 's' : ''} deleted.`, 5000); drawTab();
  },
  'connect-sug': async (b) => { if (S.stage) { S.stage.connections[b.dataset.key] = S.sel.props.suggestion; await previewStage(false); S.sel = null; drawTab(); return; } const s = S.sel.props.suggestion; await api('/roads/connect', { node: b.dataset.key, target: s, user: user() }); await afterEdit(); await loadRoads(); S.sel = null; drawTab(); },
  'leave-node': async (b) => { if (S.stage) { S.stage.connections[b.dataset.key] = { type: 'none' }; await previewStage(false); S.sel = null; drawTab(); return; } await api('/roads/connect', { node: b.dataset.key, none: true, user: user() }); await afterEdit(); await loadRoads(); S.sel = null; drawTab(); },
  'reset-node': async (b) => { if (S.stage) { delete S.stage.connections[b.dataset.key]; await previewStage(false); S.sel = null; drawTab(); return; } await api('/roads/connect', { node: b.dataset.key, clear: true, user: user() }); await afterEdit(); await loadRoads(); S.sel = null; drawTab(); },
};
$('#panel').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]'); if (!b) return; e.preventDefault();
  try { await ACT[b.dataset.act](b); } catch (err) { toast(err.message, 5000); }
});
$('#panel').addEventListener('change', (e) => {
  if (S.draw && e.target.id === 'draw-uncertain') S.draw.uncertain = e.target.checked;
  if (S.draw && e.target.id === 'draw-tool') { endDrawStroke(); S.draw.tool = e.target.value; if (S.draw.tool === 'freehand') map.dragging.disable(); else map.dragging.enable(); drawTab(); }
  if (S.draw && e.target.id === 'draw-snap') { S.draw.snap = e.target.checked; if (!S.draw.snap) S.draw.snaps = S.draw.snaps.map((t, i) => S.draw.mode === 'prolong' && i === 0 ? t : null); G.hl.clearLayers(); drawTab(); }
  if (S.draw && ['draw-rank', 'draw-drv', 'draw-paved'].includes(e.target.id)) { const v = e.target.value; if (e.target.id === 'draw-rank') S.draw.rank = +v; else S.draw[e.target.id === 'draw-drv' ? 'drivable' : 'paved'] = JSON.parse(v); }
  if (e.target.id === 'roads-color') { S.roadColor = e.target.value; drawTab(); }
  if (e.target.id === 'color-mode') { S.idColor = e.target.value; drawTab(); }
  if (e.target.dataset && e.target.dataset.keep !== undefined && S.stage) {
    const f = S.stage.pv.replaced.features[+e.target.dataset.keep].properties, all = f.refs;
    S.stage.keep = e.target.checked ? [...new Set([...S.stage.keep, ...all])] : S.stage.keep.filter((r) => !all.includes(r));
    previewStage(false).then(() => drawTab()).catch((err) => toast(err.message, 5000));
  }
  if (e.target.id === 'attrs-file' && e.target.files[0]) {
    const rd = new FileReader();
    rd.onload = async () => { try { const r = await api('/streets/attrs/import', { content_b64: rd.result.split(',')[1] }); toast(`${r.changed} street${r.changed === 1 ? '' : 's'} changed`, 4000); await afterEdit(); } catch (err) { toast(err.message, 6000); } e.target.value = ''; };
    rd.readAsDataURL(e.target.files[0]); return;
  }
  if (e.target.id === 'roads-file' && e.target.files[0]) {
    const file = e.target.files[0]; const rd = new FileReader();
    rd.onload = async () => { try { const b64 = rd.result.split(',')[1]; S.del = null; S.stage = { filename: file.name, b64, features: null, connections: {}, keep: [], pv: null, fit: true }; await previewStage(true); S.sel = null; drawTab(); toast(`${S.stage.pv.summary.lines} lines read: check the merge, then accept or cancel`, 5000); } catch (err) { S.stage = null; toast(err.message, 6000); } };
    rd.readAsDataURL(file);
  }
});
$('#panel').addEventListener('toggle', (e) => { const d = e.target; if (d.dataset && d.dataset.sec) { S.openSec = S.openSec || {}; S.openSec[d.dataset.sec] = d.open; } }, true);
$('#panel').addEventListener('input', (e) => {
  if (e.target.id === 'draw-name' && S.draw) { S.draw.name = e.target.value; return; }
  if (e.target.id !== 'addr-search' || !S.addresses) return;
  const q = e.target.value.trim().toLowerCase(); const out = $('#addr-results');
  if (q.length < 2) { out.innerHTML = ''; return; }
  const hits = S.addresses.houses.features.filter((f) => ((f.properties.address || '') + ' ' + f.properties.building_id).toLowerCase().includes(q)).slice(0, 12);
  out.innerHTML = I18n.html`<table>${hits.map((f) => I18n.html`<tr class="click" data-act="goto-house" data-id="${esc(f.properties.building_id)}"><td>${esc(f.properties.address || f.properties.building_id)}</td></tr>`).join('')}</table>`;
});
ACT['goto-point'] = (b) => { map.setView([+b.dataset.lat, +b.dataset.lon], 18); };
ACT['goto-house'] = (b) => { const f = S.addresses.houses.features.find((x) => x.properties.building_id === b.dataset.id); if (!f) return; const c = L.geoJSON(f).getBounds().getCenter(); map.setView(c, 19); S.sel = { type: 'house', props: f.properties, ll: c }; drawAccess(f.properties); renderPanel(); refreshSel(); };

/* ---------------------------------------------------------------- downloads (clear message when something has to be computed first) */
document.querySelector('.menu-body').addEventListener('click', async (e) => {
  const a = e.target.closest('a[data-dl]'); if (!a) return; e.preventDefault();
  try {
    const r = await fetch(a.getAttribute('href'));
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (x) { /* not json */ } toast(m, 6000); return; }
    const url = URL.createObjectURL(await r.blob()); const l = document.createElement('a'); l.href = url; l.download = a.dataset.dl; document.body.appendChild(l); l.click(); l.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
    const st = r.headers.get('X-Osm-Stats'); if (st) { const s = JSON.parse(st); toast(`OSM file: ${s.new_ways} new ways, ${s.modified_ways} modified ways (${s.renamed_ways} renamed), ${s.new_nodes} new nodes. Open it in JOSM and check before uploading.`, 9000); }
  } catch (err) { toast(err.message, 6000); }
});

/* ---------------------------------------------------------------- header buttons */
$('#btn-undo').onclick = async () => { await api('/undo', {}); await afterEdit(); if (S.tab === 'roads') await loadRoads(); drawTab(); };
$('#btn-redo').onclick = async () => { await api('/redo', {}); await afterEdit(); if (S.tab === 'roads') await loadRoads(); drawTab(); };
async function computeAndWait() {
  await api('/compute', {}).catch(() => null);
  for (;;) { const j = await api('/job'); $('#job').textContent = `computing: ${j.step || ''}...`; if (j.state !== 'running') { if (j.state === 'error') throw new Error(j.error); return; } await new Promise((r) => setTimeout(r, 700)); }
}
$('#btn-save').onclick = async () => {
  const label = prompt('Describe this checkpoint (optional):', ''); if (label === null) return;
  try {
    if (S.project.reference && (S.project.stale || !S.project.network_ready)) { toast('Computing before saving, so the street ids are stored too...', 6000); await computeAndWait(); S.network = await api('/network'); }
  } catch (e) { toast(`Could not compute: ${e.message}`, 6000); }
  const r = await api('/save', { label, user: user() }); await refreshProject(); drawTab();
  toast(r.ids_saved ? 'Saved: names and street ids are in the project database' : 'Saved (compute first to store the street ids too)');
};
const toggleWarnings = () => { const box=$('#warnings-box'); if(matchMedia('(max-width:900px)').matches) { box.classList.remove('collapsed'); box.classList.toggle('mobile-expanded'); } else box.classList.toggle('collapsed'); };
$('#btn-warnings').onclick = toggleWarnings; $('#warn-close').onclick = toggleWarnings;
$('#warn-list').addEventListener('click', (e) => { const w = e.target.closest('[data-warn]'); if (w) focusWarning(allWarnings()[+w.dataset.warn]); });
$('#btn-checkpoints').onclick = async () => {
  const cps = await api('/checkpoints'); const box = $('#modal'); $('#modal-title').textContent = 'Checkpoints (newest first)';
  $('#modal-content').innerHTML = I18n.html`<p class="muted">Edits are saved at once. Restoring adds a checkpoint (undoable).</p><table>${cps.map((c) => I18n.html`<tr><td>#${c.id}</td><td>${esc(c.ts.replace('T', ' ').slice(0, 16))}</td><td>${esc(c.label)}${c.user ? ' · ' + esc(c.user) : ''}</td><td>${c.named_streets} names${c.has_ids ? ' + ids' : ''}</td><td><button data-restore="${c.id}">Restore</button></td></tr>`).join('')}</table>`;
  box.hidden = false;
};
$('#modal-close').onclick = () => { $('#modal').hidden = true; };
$('#modal-content').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-restore]'); if (!b) return;
  if (!confirm(`Go back to checkpoint #${b.dataset.restore}? This changes the project for all editors.`)) return;
  const reason=AuthUI.state.enabled?prompt(AuthUI.t('Restore reason (required, at least 5 characters):')):'';
  if(AuthUI.state.enabled&&(!reason||reason.trim().length<5))return;
  await api('/restore', { id: +b.dataset.restore, revision:S.rev, reason, user: user() }); $('#modal').hidden = true; S.network = null; S.addresses = null;
  await refreshProject(); await loadStaticLayers(); drawTab(); if (S.project.reference) startCompute('network');
});

/* ---------------------------------------------------------------- other editors: poll the revision */
setInterval(async () => {
  if (!S.project || document.hidden) return;
  try {
    const r = await api('/revision');
    if (r.status.revision !== S.rev) {
      await refreshProject(); if (S.tab === 'roads') await loadRoads();
      toast('The project was changed by another editor'); drawTab();
    }
    if (r.fresh && r.network_hash && r.network_hash !== S.netHash && r.job.state !== 'running') { S.network = await api('/network'); S.addresses = null; await refreshProject(); drawTab(); toast('The network was recomputed'); }
  } catch (e) { /* offline or login expired: try again later */ }
}, 4000);

/* ---------------------------------------------------------------- start */
/* the reference point is confirmed (or moved) by every editor who did not set it: the default point, or one set by somebody else */
const refBox = () => $('#refbox');
function showRefOnly(r) {                              // only the node itself is drawn, never the point that was clicked or the previous one
  G.ref.clearLayers(); refMarkers(r, true).forEach((m) => m.addTo(G.ref)); G.ref.addTo(map);
}
function showMoveBox() {                               // choosing: no old point on the map, the nearest node follows the mouse
  S.referenceView={kind:'move'};
  const has = S.project && S.project.reference;
  G.ref.clearLayers();
  refBox().hidden = false;
  refBox().innerHTML = I18n.html`<img class="reference-logo" src="static/brand/community-addressing-cameroon.png" alt="Community Addressing Cameroon"><div class="refbox-title">Choose the reference point</div><p>Move over the map: the highlighted node is the one you get. Click to select it.</p>${has ? I18n.htmlText('<div class="row"><button id="ref-back">Keep the current point</button></div>') : ''}`;
  setMode('reference');
  if (has) $('#ref-back').onclick = () => { setMode(null); askReference(true); };
}
function tentativeReference(pick) {                    // a node was clicked: show it and ask before anything is saved
  S.referenceView={kind:'tentative',pick};
  const r = { lat: pick[2], lon: pick[1], node: pick[0] };
  setMode(null); hideRefNodes(); showRefOnly(r); map.panTo([r.lat, r.lon]);
  refBox().hidden = false;
  refBox().innerHTML = I18n.html`<img class="reference-logo" src="static/brand/community-addressing-cameroon.png" alt="Community Addressing Cameroon"><div class="refbox-title">Reference point</div><p>${I18n.msg`Use this node (${r.node}) as the reference point? The numbering of every street and house starts here.`}</p>
    <div class="row"><button class="primary" id="ref-yes">Yes, use it</button><button id="ref-other">No, choose another</button></div>`;
  $('#ref-yes').onclick = async () => {
    await api('/reference', { lat: r.lat, lon: r.lon, client: CLIENT, user: user() }); S.refConfirmed = true; refBox().hidden = true;
    toast(`Reference point set on node ${r.node}`); await afterEdit(); startCompute('network'); switchTab('ids');
  };
  $('#ref-other').onclick = () => { showMoveBox(); };
}
function askReference(force) {
  const r = S.project.reference; if (!r || (S.refConfirmed && !force)) return;
  S.referenceView={kind:'ask'};
  const what = r.name ? I18n.html`<strong>${esc(r.name)}</strong> (node ${r.node})` : `node ${r.node}`;
  showRefOnly(r);
  map.setView([r.lat, r.lon], 17);
  refBox().hidden = false;
  refBox().innerHTML = I18n.html`<img class="reference-logo" src="static/brand/community-addressing-cameroon.png" alt="Community Addressing Cameroon"><div class="refbox-title">Reference point</div><p>${I18n.msg`The numbering of every street and house starts at the star on the map: ${what}, ${I18n.t(r.default ? 'the default point of this project' : 'set by another editor')}.`}</p>
    <p><strong>Is this the right place?</strong></p><div class="row"><button class="primary" id="ref-confirm">Yes, confirm it</button><button id="ref-move">No, I will move it</button></div>`;
  $('#ref-confirm').onclick = async () => {
    await api('/reference/confirm', { client: CLIENT }); S.refConfirmed = true; refBox().hidden = true; await refreshProject(); drawTab(); toast('Reference point confirmed');
  };
  $('#ref-move').onclick = () => showMoveBox();
}

window.addEventListener('languagechange', () => {
  if (!S.project) return;
  const controls=[...document.querySelectorAll('input[id]:not([type=file]),select[id],textarea[id]')]
    .filter(el=>el.id!=='language-select').map(el=>({id:el.id,value:el.value,checked:el.checked}));
  const center=map.getCenter(), zoom=map.getZoom(), view=S.referenceView;
  if (S.mode) setMode(S.mode.kind,S.mode.data);
  drawTab(); renderWorkAreas(); updateStaleBar(); renderWarnings();
  renderSaveState();
  if (!refBox().hidden) {
    if (view?.kind==='tentative') tentativeReference(view.pick);
    else if (view?.kind==='move') showMoveBox();
    else askReference(true);
  }
  if (S.sel) refreshSel();
  // A language change is a local preference, not a project edit or a reset.
  controls.forEach(({id,value,checked})=>{const el=document.getElementById(id);if(el){el.value=value;if(el.type==='checkbox')el.checked=checked;}});
  map.setView(center,zoom,{animate:false});
});

async function initApp() {
  if(S.initializing||S.ready)return;S.initializing=true;
  await refreshProject();
  await loadStaticLayers();
  askReference();
  if (!S.project.reference) { S.tab = 'ids'; $('#tabs').querySelectorAll('button').forEach((b) => b.classList.toggle('active', false)); drawTab(); S.ready = true; return; }
  const j = await api('/job');
  try { S.network = await api('/network'); } catch (e) { /* computing or not yet */ }
  let issues = false;                                        // graph problems (cut-off parts, roads not connected): start on the street editor
  try { issues = (await api('/base/components')).parts.length > 0 || ((S.network && S.network.warnings) || []).some((w) => ['disconnected', 'roads_isolated'].includes(w.code)); } catch (e) { /* no network yet */ }
  await switchTab(issues ? 'roads' : 'ids');
  S.fitted = true;                                           // the view is set once at the start; later recomputations never move it
  if (S.network) map.setView([S.project.reference.lat, S.project.reference.lon], 16); else if (j.state === 'running') pollJob('network'); else startCompute('network');
  if (j.state === 'running') pollJob(j.kind || 'network');
  S.ready = true;
}
AuthUI.ready.then(()=>{if(AuthUI.canView())initApp();});
window.addEventListener('authchange',async()=>{
  if(!S.ready) {await initApp();return;}
  await refreshProject();drawTab();
});
