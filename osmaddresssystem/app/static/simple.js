/* The simple page: street names and addresses only. Same server, same project; every change is saved at once and the rest updates by itself. */
const $ = (s) => document.querySelector(s);
const esc = (t) => String(t == null ? '' : t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, body) => {
  const r = await fetch(P + 'api' + path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let d = null; try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) { const e = new Error((d && d.detail && (d.detail.message || d.detail)) || r.statusText); e.status = r.status; e.detail = d && d.detail; throw e; }
  return d;
};
const COLOR = { named: '#2e7d32', derived: '#f9a825', planned: '#d50000', uncovered: '#d50000', other: '#9e9e9e' };
const NEEDS = (p) => p.status === 'planned' || p.status === 'uncovered';
const map = L.map('map', { zoomControl: true });
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap' }).addTo(map);
const layer = L.layerGroup().addTo(map);
const S = { tab: 'names', network: null, addresses: null, lines: {}, busy: false };
const state = (t) => { $('#state').textContent = t || ''; };

async function wait(kind) {                                             // run a computation and wait for it
  await api(kind === 'network' ? '/compute' : '/addresses/compute', {});
  for (;;) {
    const j = await api('/job');
    if (j.state !== 'running') return;
    state('Updating… ' + (j.step || ''));
    await new Promise((r) => setTimeout(r, 700));
  }
}
async function ensureNetwork() {
  const p = await api('/project');
  if (!p.reference) { location.replace(P || './'); return false; }
  if (!p.network_ready || p.stale) await wait('network');
  S.network = await api('/network');
  const comp = await api('/base/components');                          // serious problems with the roads: the simple page is not for that
  if (comp.parts.length || (S.network.warnings || []).some((w) => ['disconnected', 'roads_isolated'].includes(w.code))) {
    location.replace(P || './');
    return false;
  }
  return true;
}
async function ensureAddresses() {
  const p = await api('/project');
  if (!p.addresses_ready || p.addresses_stale) await wait('addresses');
  S.addresses = await api('/addresses');
}

/* ---------------------------------------------------------------- street names */
function drawNames(fit) {
  layer.clearLayers(); S.lines = {};
  const fc = S.network.streets;
  const g = L.geoJSON(fc, {
    style: (f) => ({ color: COLOR[f.properties.status] || COLOR.other, weight: NEEDS(f.properties) ? 6 : 4, opacity: .95 }),
    onEachFeature: (f, l) => { S.lines[f.properties.original] = S.lines[f.properties.original] || l; l.on('click', (e) => { L.DomEvent.stopPropagation(e); openStreet(f.properties.original, e.latlng); }); l.bindTooltip(f.properties.display || '(no name yet)', { sticky: true }); },
  }).addTo(layer);
  if (fit) { try { map.fitBounds(g.getBounds().pad(0.05)); } catch (e) { /* empty */ } }
  const need = [], seen = new Set();
  fc.features.forEach((f) => { const p = f.properties; if (NEEDS(p) && !seen.has(p.original)) { seen.add(p.original); need.push(p); } });
  need.sort((a, b) => b.length - a.length);
  const st = S.network.stats || {};
  $('#side').innerHTML = `<h2>Streets that need a name</h2><div class="big" style="color:${need.length ? COLOR.planned : COLOR.named}">${need.length}</div>
    <p class="muted">${need.length ? 'Click a red street on the map or in this list, type its name and press Enter.' : 'Every street has a name. Thank you!'}</p>
    <p class="muted"><span class="dot" style="background:${COLOR.named}"></span>has a name &nbsp; <span class="dot" style="background:${COLOR.derived}"></span>name follows from another street &nbsp; <span class="dot" style="background:${COLOR.planned}"></span>needs a name</p>
    ${need.map((p) => `<button class="item" data-id="${p.original}"><span class="dot" style="background:${COLOR.planned}"></span>street ${p.original} &middot; ${Math.round(p.length)} m</button>`).join('')}
    <p><a class="dl" href="${P}api/export/streets.csv" download>&#11015; Street names (CSV)</a></p>`;
  document.querySelectorAll('#side .item').forEach((b) => b.addEventListener('click', () => openStreet(+b.dataset.id, null)));
}
function openStreet(original, ll) {
  const parts = S.network.streets.features.filter((f) => f.properties.original === original);
  if (!parts.length) return;
  const p = (parts.find((f) => f.properties.part === 0) || parts[0]).properties;
  const own = (S.network.streets.features.map((f) => f.properties).find((q) => q.original === original && q.name) || {}).name || '';
  const box = document.createElement('div');
  box.style.minWidth = '230px';
  box.innerHTML = `<strong>${esc(p.display || '(no name yet)')}</strong><br><span class="muted">street ${original} &middot; ${Math.round(parts.reduce((a, f) => a + f.properties.length, 0))} m</span>
    <p style="margin:8px 0 2px">Name of this street</p><input type="text" id="nm" value="${esc(own)}" placeholder="type the name">
    <button class="go" id="ok">Save</button> <span class="muted">(empty = no name)</span>`;
  const f0 = parts[0], c = L.geoJSON(f0).getBounds().getCenter();
  const pop = L.popup({ maxWidth: 300 }).setLatLng(ll || c).setContent(box).openOn(map);
  if (!ll) map.setView(c, Math.max(map.getZoom(), 17));
  const input = box.querySelector('#nm'); setTimeout(() => input.focus(), 50);
  const save = async () => {
    const name = input.value.trim();
    map.closePopup(pop);
    state('Saving…');
    try { await api('/names', { street_id: original, name: name || '0', user: '' }); await wait('network'); S.network = await api('/network'); drawNames(false); state('Saved'); }
    catch (e) { state(''); alert(e.message); }
  };
  box.querySelector('#ok').addEventListener('click', save);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
}

/* ---------------------------------------------------------------- addresses */
function drawAddresses() {
  layer.clearLayers();
  const hs = S.addresses.houses;
  const g = L.geoJSON(hs, {
    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5, color: '#000', weight: 1, fillColor: f.properties.status === 'ok' ? COLOR.named : COLOR.planned, fillOpacity: .95 }),
    style: (f) => ({ color: '#555', weight: 1, fillColor: f.properties.status === 'ok' ? '#a5d6a7' : '#ef9a9a', fillOpacity: .8 }),
    onEachFeature: (f, l) => { const p = f.properties; const t = p.status === 'ok' ? esc(p.address) : 'no address yet: the street has no name'; l.bindPopup(`<strong>${t}</strong>${p.name ? '<br>' + esc(p.name) : ''}`); l.bindTooltip(p.status === 'ok' ? esc(p.address) : 'no address yet', { sticky: true }); },
  }).addTo(layer);
  try { map.fitBounds(g.getBounds().pad(0.05)); } catch (e) { /* empty */ }
  const rows = hs.features.map((f) => f.properties), ok = rows.filter((p) => p.status === 'ok').length;
  $('#side').innerHTML = `<h2>Addresses</h2><div class="big">${ok} <span class="muted">of ${rows.length} buildings</span></div>
    <p class="muted">Click a building to see its address. Buildings in red have no address yet because their street has no name: give it a name in <em>Street names</em>.</p>
    <input type="text" id="q" placeholder="search an address or a name">
    <div id="res"></div>
    <p><a class="dl" href="${P}api/export/buildings.csv" download>&#11015; Addresses (CSV)</a></p>`;
  const find = () => {
    const q = $('#q').value.trim().toLowerCase();
    const hits = q ? hs.features.filter((f) => `${f.properties.address || ''} ${f.properties.name || ''}`.toLowerCase().includes(q)).slice(0, 30) : [];
    $('#res').innerHTML = hits.map((f, i) => `<button class="item" data-i="${i}">${esc(f.properties.address || '(no address)')}${f.properties.name ? ' &middot; ' + esc(f.properties.name) : ''}</button>`).join('');
    $('#res').querySelectorAll('.item').forEach((b) => b.addEventListener('click', () => { const f = hits[+b.dataset.i]; const c = L.geoJSON(f).getBounds().getCenter(); map.setView(c, 19); L.popup().setLatLng(c).setContent(`<strong>${esc(f.properties.address || 'no address yet')}</strong>`).openOn(map); }));
  };
  $('#q').addEventListener('input', find);
}

/* ---------------------------------------------------------------- tabs */
async function show(tab) {
  S.tab = tab; document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  map.closePopup(); state('Loading…');
  try {
    if (tab === 'names') { if (!(await ensureNetwork())) return; drawNames(true); }
    else { if (!(await ensureNetwork())) return; await ensureAddresses(); drawAddresses(); }
    state('');
  } catch (e) { state(''); $('#side').innerHTML = `<h2>Something went wrong</h2><p>${esc(e.message)}</p>`; }
}
document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
show('names');
