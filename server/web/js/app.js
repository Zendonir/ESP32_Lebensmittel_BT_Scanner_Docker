// Admin-Oberflaeche. Bewusst ohne Framework und ohne Build-Schritt: das Image
// bleibt klein und die Dateien sind das, was ausgeliefert wird.

import { get, post, patch, put, del, toast, fmtDate, fmtTime, daysPill, esc, liveConnect, tagEditor } from './api.js';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = {
  categories: [],
  locations: [],
  templates: [],
  devices: [],
  firmware: [],
  settings: {},
};

// ------------------------------------------------------------------- Navigation
function showPage(name) {
  $$('.page').forEach((p) => p.classList.toggle('active', p.id === `page-${name}`));
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
  location.hash = name;
  const loader = PAGE_LOADERS[name];
  if (loader) loader().catch((e) => toast(e.message, 'error'));
}

$('#tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-page]');
  if (btn) showPage(btn.dataset.page);
});

// ----------------------------------------------------------------------- Design
const THEME_KEY = 'ls-theme';
function applyTheme(value) {
  document.documentElement.dataset.theme = value === 'auto' ? '' : value;
  localStorage.setItem(THEME_KEY, value);
}
$('#theme-toggle').addEventListener('click', () => {
  const order = ['auto', 'light', 'dark'];
  const next = order[(order.indexOf(localStorage.getItem(THEME_KEY) || 'auto') + 1) % 3];
  applyTheme(next);
  toast(`Design: ${next}`);
});
applyTheme(localStorage.getItem(THEME_KEY) || 'auto');

// ------------------------------------------------------------------ Stammdaten
async function loadCatalogData() {
  const [categories, locations] = await Promise.all([get('/api/categories'), get('/api/locations')]);
  state.categories = categories;
  state.locations = locations;

  fillSelect($('#inv-category'), categories.map((c) => c.name), 'Alle Kategorien');
  fillSelect($('#inv-location'), locations.map((l) => l.name), 'Alle Orte');
  $$('[data-fill="categories"]').forEach((el) => fillSelect(el, categories.map((c) => c.name), '–'));
  $$('[data-fill="locations"]').forEach((el) => fillSelect(el, locations.map((l) => l.name), '–'));
}

function fillSelect(el, values, placeholder) {
  if (!el) return;
  const current = el.value;
  el.innerHTML = `<option value="">${esc(placeholder)}</option>` +
    values.map((v) => `<option>${esc(v)}</option>`).join('');
  if (values.includes(current)) el.value = current;
}

// ------------------------------------------------------------------- Übersicht
async function loadDashboard() {
  const [stats, expiring, events] = await Promise.all([
    get('/api/inventory/stats'),
    get('/api/inventory?expiring=true&limit=12'),
    get('/api/events?limit=12'),
  ]);

  $('#stats').innerHTML = [
    ['Im Bestand', stats.total, 'ok'],
    ['Läuft ab', stats.expiring, 'warn'],
    ['Abgelaufen', stats.expired, 'danger'],
    ['Eingelagert 30 T', stats.added_30d, ''],
    ['Verbraucht 30 T', stats.removed_30d, ''],
  ].map(([label, value, cls]) =>
    `<div class="card stat ${cls}"><div class="value">${value}</div><div class="label">${label}</div></div>`
  ).join('');

  $('#dash-expiring').innerHTML = expiring.length
    ? expiring.map((i) => `<tr>
        <td class="name">${esc(i.name)}</td>
        <td>${fmtDate(i.expiry_date)} ${daysPill(i.days_left)}</td>
        <td>${esc(i.location)}</td>
        <td class="right"><button class="sm" data-remove="${esc(i.label)}">Auslagern</button></td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="muted">Nichts läuft demnächst ab.</td></tr>';

  $('#dash-events').innerHTML = events.map((e) => `<tr>
      <td class="mono">${fmtTime(e.ts)}</td><td>${esc(e.type)}</td>
      <td class="name">${esc(e.name || e.label || e.barcode)}</td></tr>`).join('');

  $('#dash-cat').innerHTML = barList(stats.by_category, state.categories);
  $('#dash-loc').innerHTML = barList(stats.by_location, []);
}

function barList(map, categories) {
  const entries = Object.entries(map).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return '<p class="muted">Keine Daten.</p>';
  const max = Math.max(...entries.map((e) => e[1]));
  return entries.map(([name, count]) => {
    const color = categories.find((c) => c.name === name)?.color || 'var(--primary)';
    return `<div style="margin:6px 0">
      <div class="row" style="justify-content:space-between"><span>${esc(name)}</span><b>${count}</b></div>
      <div style="height:6px;border-radius:3px;background:var(--surface-2)">
        <div style="height:6px;border-radius:3px;width:${(count / max) * 100}%;background:${esc(color)}"></div>
      </div></div>`;
  }).join('');
}

// --------------------------------------------------------------------- Inventar
async function loadInventory() {
  const params = new URLSearchParams({
    status: $('#inv-status').value,
    sort: $('#inv-sort').value,
    limit: '500',
  });
  if ($('#inv-q').value.trim()) params.set('q', $('#inv-q').value.trim());
  if ($('#inv-category').value) params.set('category', $('#inv-category').value);
  if ($('#inv-location').value) params.set('location', $('#inv-location').value);

  const rows = await get(`/api/inventory?${params}`);
  $('#inv-empty').hidden = rows.length > 0;
  $('#inv-body').innerHTML = rows.map((i) => `<tr>
    <td class="mono">${esc(i.label)}</td>
    <td class="name"><b>${esc(i.display_name || i.name)}</b>${i.brand ? `<br><span class="muted">${esc(i.brand)}</span>` : ''}</td>
    <td>${esc(i.category)}</td>
    <td>${fmtDate(i.expiry_date)}</td>
    <td>${daysPill(i.days_left)}</td>
    <td>${i.quantity % 1 === 0 ? i.quantity : i.quantity.toFixed(1)} ${esc(i.unit)}</td>
    <td>${esc(i.location)}</td>
    <td class="right">
      <button class="sm" data-edit="${esc(i.label)}">Ändern</button>
      <button class="sm" data-reprint="${esc(i.label)}">Druck</button>
      ${i.status === 'active'
        ? `<button class="sm danger" data-remove="${esc(i.label)}">Auslagern</button>`
        : `<button class="sm" data-restore="${esc(i.label)}">Zurück</button>`}
    </td></tr>`).join('');
}

['#inv-q', '#inv-category', '#inv-location', '#inv-status', '#inv-sort'].forEach((sel) => {
  const el = $(sel);
  const handler = debounce(() => loadInventory().catch((e) => toast(e.message, 'error')), 250);
  el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', handler);
});
$('#inv-reload').addEventListener('click', () => loadInventory());

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// Aktionen aus allen Tabellen zentral abfangen.
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-remove],button[data-restore],button[data-edit],button[data-reprint]');
  if (!btn) return;
  try {
    if (btn.dataset.remove) {
      await post('/api/inventory/remove', { label: btn.dataset.remove, reason: 'web' });
      toast('Ausgelagert', 'success');
    } else if (btn.dataset.restore) {
      await post('/api/inventory/restore', { label: btn.dataset.restore });
      toast('Zurückgebucht', 'success');
    } else if (btn.dataset.reprint) {
      const res = await post(`/api/labels/reprint/${btn.dataset.reprint}`);
      toast(res.sent ? 'Druckauftrag gesendet' : 'Eingereiht (kein Terminal online)', res.sent ? 'success' : 'warn');
    } else if (btn.dataset.edit) {
      return editItem(btn.dataset.edit);
    }
    await refreshActive();
  } catch (e) { toast(e.message, 'error'); }
});

async function editItem(label) {
  const item = await get(`/api/inventory/${label}`);
  const values = await modal(`Etikett ${label}`, [
    { name: 'name', label: 'Name', value: item.name },
    { name: 'brand', label: 'Marke', value: item.brand },
    { name: 'category', label: 'Kategorie', value: item.category, options: state.categories.map((c) => c.name) },
    { name: 'subcategory', label: 'Sorte', value: item.subcategory },
    { name: 'expiry_date', label: 'MHD', value: item.expiry_date, type: 'date' },
    { name: 'quantity', label: 'Menge', value: item.quantity, type: 'number' },
    { name: 'unit', label: 'Einheit', value: item.unit },
    { name: 'location', label: 'Ort', value: item.location, options: state.locations.map((l) => l.name) },
    { name: 'note', label: 'Notiz', value: item.note },
  ]);
  if (!values) return;
  values.quantity = parseFloat(values.quantity) || 1;
  await patch(`/api/inventory/${label}`, values);
  toast('Gespeichert', 'success');
  await loadInventory();
}

// -------------------------------------------------------------------- Etiketten
$('#label-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = new FormData(ev.target);
  const body = Object.fromEntries(form.entries());
  body.quantity = parseFloat(body.quantity) || 1;
  body.count = parseInt(body.count, 10) || 1;
  body.print = form.get('print') === 'on';
  try {
    const res = await post('/api/labels', body);
    toast(`${res.labels.length} Etikett(en): ${res.labels.join(', ')}`, 'success');
    ev.target.reset();
    await Promise.all([loadLabels(), loadDashboard()]);
  } catch (e) { toast(e.message, 'error'); }
});

async function loadLabels() {
  const [roll, queue] = await Promise.all([get('/api/labels/roll'), get('/api/labels/queue?limit=25')]);
  $('#roll-state').textContent = roll.size
    ? `${roll.remaining} von ${roll.size} Etiketten übrig (${roll.used} verbraucht)`
    : 'Keine Rollengröße hinterlegt.';
  $('#print-queue').innerHTML = queue.length
    ? queue.map((j) => `<tr><td class="mono">${j.id}</td><td class="mono">${esc(j.label)}</td>
        <td><span class="pill ${{ done: 'ok', failed: 'danger', queued: 'warn' }[j.status] || ''}">${j.status}</span></td>
        <td>${j.attempts}${j.error ? ` <span class="muted">${esc(j.error)}</span>` : ''}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">Warteschlange leer.</td></tr>';
}

$('#roll-set').addEventListener('click', async () => {
  const size = parseInt($('#roll-size').value, 10);
  if (!Number.isFinite(size)) return toast('Bitte Rollengröße angeben', 'warn');
  await post(`/api/labels/roll?size=${size}`);
  toast('Rolle zurückgesetzt', 'success');
  await loadLabels();
});
$('#test-print').addEventListener('click', async () => {
  try { await post('/api/labels/test-print'); toast('Testdruck gesendet', 'success'); }
  catch (e) { toast(e.message, 'error'); }
});
$('#queue-reload').addEventListener('click', () => loadLabels());
$('#queue-clear').addEventListener('click', async () => {
  await del('/api/labels/queue');
  toast('Warteschlange geleert');
  await loadLabels();
});

// --------------------------------------------------------------------- Vorlagen
async function loadTemplates() {
  state.templates = await get('/api/templates');
  $('#tpl-body').innerHTML = state.templates.map((t) => `<tr>
    <td class="name"><b>${esc(t.name)}</b></td>
    <td>${esc(t.category)}</td><td>${t.shelf_days}</td><td>${esc(t.unit)}</td>
    <td>${esc((t.brands || []).join(', '))}</td>
    <td>${esc((t.sorten || []).join(', '))}</td>
    <td class="right">
      <button class="sm" data-tpl-edit="${t.id}">Ändern</button>
      <button class="sm danger" data-tpl-del="${t.id}">Löschen</button>
    </td></tr>`).join('');
}

document.addEventListener('click', async (ev) => {
  const editBtn = ev.target.closest('button[data-tpl-edit]');
  const delBtn = ev.target.closest('button[data-tpl-del]');
  try {
    if (delBtn) {
      if (!confirm('Vorlage wirklich löschen?')) return;
      await del(`/api/templates/${delBtn.dataset.tplDel}`);
      toast('Gelöscht'); await loadTemplates();
    } else if (editBtn) {
      const tpl = state.templates.find((t) => t.id === +editBtn.dataset.tplEdit);
      await templateDialog(tpl);
    }
  } catch (e) { toast(e.message, 'error'); }
});

$('#tpl-new').addEventListener('click', () => templateDialog(null));

async function templateDialog(tpl) {
  const values = await modal(tpl ? `Vorlage ${tpl.name}` : 'Neue Vorlage', [
    { name: 'name', label: 'Name', value: tpl?.name || '' },
    { name: 'category', label: 'Kategorie', value: tpl?.category || '', options: state.categories.map((c) => c.name) },
    { name: 'shelf_days', label: 'MHD-Tage', value: tpl?.shelf_days ?? 7, type: 'number' },
    { name: 'unit', label: 'Einheit', value: tpl?.unit || '', options: ['', 'St.', 'g', 'kg', 'ml', 'l'] },
    { name: 'brands', label: 'Marken', type: 'tags', value: tpl?.brands || [], placeholder: 'Marke hinzufügen …' },
    { name: 'sorten', label: 'Sorten', type: 'tags', value: tpl?.sorten || [], placeholder: 'Sorte hinzufügen …' },
  ], (body, editors) => {
    // Hat die Kategorie feste Sorten (Fleisch & Fisch: Schwein, Geflügel, …),
    // werden sie angeboten, solange die Vorlage noch keine eigenen hat. Wer
    // andere will, entfernt die Blasen einfach wieder.
    const select = body.querySelector('[name="category"]');
    const offer = () => {
      const found = state.categories.find((c) => c.name === select.value);
      if (editors.sorten.isEmpty() && found?.subcategories?.length) {
        editors.sorten.set(found.subcategories);
      }
    };
    select.addEventListener('change', offer);
    offer();
  });
  if (!values) return;

  const body = {
    name: values.name,
    category: values.category,
    shelf_days: parseInt(values.shelf_days, 10) || 0,
    unit: values.unit,
    brands: values.brands,
    sorten: values.sorten,
    use_sorten: values.sorten.length > 0,
    sort_order: tpl?.sort_order ?? 0,
  };
  if (tpl) await put(`/api/templates/${tpl.id}`, body);
  else await post('/api/templates', body);
  toast('Gespeichert', 'success');
  await loadTemplates();
}

// ------------------------------------------------------------------ Stammdaten
async function loadCatalogPage() {
  await loadCatalogData();
  $('#cat-body').innerHTML = state.categories.map((c) => `<tr>
    <td><span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}
      ${(c.subcategories || []).length ? `<div class="tag-list" style="margin-top:6px">${
        c.subcategories.map((s) => `<span class="tag">${esc(s)}</span>`).join('')}</div>` : ''}</td>
    <td class="right">
      <button class="sm" data-cat-edit="${c.id}">Ändern</button>
      <button class="sm danger" data-cat-del="${c.id}">Löschen</button></td></tr>`).join('');

  $('#loc-body').innerHTML = state.locations.map((l) => `<tr>
    <td>${esc(l.name)} ${l.is_default ? '<span class="pill ok">Standard</span>' : ''}</td>
    <td class="right">
      <button class="sm" data-loc-edit="${l.id}">Ändern</button>
      <button class="sm danger" data-loc-del="${l.id}">Löschen</button></td></tr>`).join('');

  await loadProducts();
}

async function loadProducts() {
  const q = $('#prod-q').value.trim();
  const rows = await get(`/api/products?limit=200${q ? `&q=${encodeURIComponent(q)}` : ''}`);
  $('#prod-body').innerHTML = rows.length ? rows.map((p) => `<tr>
    <td class="mono">${esc(p.barcode)}</td><td class="name">${esc(p.name) || '<span class="muted">unbekannt</span>'}</td>
    <td>${esc(p.brand)}</td><td>${esc(p.category)}</td><td>${esc(p.amount)}</td>
    <td><span class="pill">${esc(p.source)}</span></td>
    <td class="right"><button class="sm danger" data-prod-del="${esc(p.barcode)}">Löschen</button></td>
  </tr>`).join('') : '<tr><td colspan="7" class="muted">Noch nichts im Cache.</td></tr>';
}

$('#prod-q').addEventListener('input', debounce(() => loadProducts(), 250));

document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('button');
  if (!t) return;
  try {
    if (t.dataset.catDel) {
      if (!confirm('Kategorie löschen?')) return;
      await del(`/api/categories/${t.dataset.catDel}`); await loadCatalogPage();
    } else if (t.dataset.catEdit) {
      const cat = state.categories.find((c) => c.id === +t.dataset.catEdit);
      const v = await modal('Kategorie', [
        { name: 'name', label: 'Name', value: cat.name },
        { name: 'color', label: 'Farbe', value: cat.color, type: 'color' },
      ]);
      if (v) { await put(`/api/categories/${cat.id}`, { ...cat, ...v }); await loadCatalogPage(); }
    } else if (t.dataset.locDel) {
      if (!confirm('Lagerort löschen?')) return;
      await del(`/api/locations/${t.dataset.locDel}`); await loadCatalogPage();
    } else if (t.dataset.locEdit) {
      const loc = state.locations.find((l) => l.id === +t.dataset.locEdit);
      const v = await modal('Lagerort', [
        { name: 'name', label: 'Name', value: loc.name },
        { name: 'is_default', label: 'Standard', value: loc.is_default, type: 'checkbox' },
      ]);
      if (v) { await put(`/api/locations/${loc.id}`, { ...loc, ...v, is_default: !!v.is_default }); await loadCatalogPage(); }
    } else if (t.dataset.prodDel) {
      await del(`/api/products/${t.dataset.prodDel}`); await loadProducts();
    }
  } catch (e) { toast(e.message, 'error'); }
});

$('#cat-new').addEventListener('click', async () => {
  const v = await modal('Neue Kategorie', [
    { name: 'name', label: 'Name', value: '' },
    { name: 'color', label: 'Farbe', value: '#1e88e5', type: 'color' },
  ]);
  if (v) { await post('/api/categories', { ...v, icon: '', sort_order: state.categories.length }); await loadCatalogPage(); }
});

$('#loc-new').addEventListener('click', async () => {
  const v = await modal('Neuer Lagerort', [
    { name: 'name', label: 'Name', value: '' },
    { name: 'is_default', label: 'Standard', value: false, type: 'checkbox' },
  ]);
  if (v) { await post('/api/locations', { ...v, is_default: !!v.is_default, sort_order: state.locations.length }); await loadCatalogPage(); }
});

// --------------------------------------------------------------------- Einkauf
async function loadShopping() {
  const rows = await get('/api/shopping');
  $('#shop-body').innerHTML = rows.length ? rows.map((s) => `<tr>
    <td><input type="checkbox" data-shop-done="${s.id}" ${s.done ? 'checked' : ''}></td>
    <td class="name" style="${s.done ? 'text-decoration:line-through;opacity:.55' : ''}">
      ${esc(s.name)} <span class="muted">${s.quantity % 1 === 0 ? s.quantity : s.quantity} ${esc(s.unit)}</span></td>
    <td class="right"><button class="sm danger" data-shop-del="${s.id}">Löschen</button></td>
  </tr>`).join('') : '<tr><td class="muted">Liste ist leer.</td></tr>';
}

$('#shop-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  await post('/api/shopping', {
    name: f.get('name'), quantity: parseFloat(f.get('quantity')) || 1,
    unit: f.get('unit') || '', note: '', done: false,
  });
  ev.target.reset();
  await loadShopping();
});

document.addEventListener('change', async (ev) => {
  const cb = ev.target.closest('input[data-shop-done]');
  if (!cb) return;
  const rows = await get('/api/shopping');
  const item = rows.find((r) => r.id === +cb.dataset.shopDone);
  await put(`/api/shopping/${item.id}`, { ...item, done: cb.checked });
  await loadShopping();
});

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button[data-shop-del]');
  if (!b) return;
  await del(`/api/shopping/${b.dataset.shopDel}`);
  await loadShopping();
});

// ------------------------------------------------------------------- Terminals

// Der Update-Knopf bleibt sichtbar, sagt aber beim Darueberfahren, warum er
// gerade nichts bringt - ein verschwindender Knopf laesst einen nur raten.
function updateHint(d) {
  if (!d.online) return ' disabled title="Terminal ist nicht verbunden"';
  const board = (d.telemetry || {}).board;
  const image = state.firmware.find((f) => f.board === board);
  if (!board) return ' disabled title="Boardvariante noch unbekannt – Terminal einmal neu verbinden lassen"';
  if (!image) return ` disabled title="Kein Abbild für Variante ${board} hinterlegt"`;
  if (image.version === d.firmware) return ` disabled title="Bereits auf ${image.version}"`;
  return ` title="Auf ${image.version} aktualisieren"`;
}

async function loadFirmware() {
  try {
    state.firmware = (await get('/api/firmware')).images;
  } catch { state.firmware = []; }

  const box = $('#fw-list');
  if (!box) return;
  box.innerHTML = state.firmware.length
    ? state.firmware.map((f) =>
        `<div>Variante <b>${esc(f.board)}</b> · ${esc(f.version)} ·
         ${Math.round(f.size / 1024)} KB · ${esc(f.source)}</div>`).join('')
    : '<span class="muted">Noch kein Abbild hinterlegt.</span>';
}

async function loadDevices() {
  state.devices = await get('/api/devices');
  $('#devices-list').innerHTML = state.devices.length ? state.devices.map((d) => {
    const t = d.telemetry || {};
    const scanner = t.scanner || {};
    return `<div class="card">
      <div class="row"><h2 style="margin:0;flex:1">
        <span class="dot ${d.online ? 'on' : ''}"></span>${esc(d.name || d.device_id)}</h2>
        <span class="pill">${esc(d.firmware || '?')}</span></div>
      <div class="muted mono">${esc(d.device_id)} · ${esc(d.ip)} · zuletzt ${fmtTime(d.last_seen)}</div>
      <div class="telemetry">
        <div><b>${t.heap ? Math.round(t.heap / 1024) + ' K' : '–'}</b><span>Heap frei</span></div>
        <div><b>${t.min_heap ? Math.round(t.min_heap / 1024) + ' K' : '–'}</b><span>Heap min</span></div>
        <div><b>${t.rssi ?? '–'}</b><span>WLAN dBm</span></div>
        <div><b>${t.uptime ? Math.floor(t.uptime / 3600) + ' h' : '–'}</b><span>Laufzeit</span></div>
        <div><b>${scanner.connected ? 'ja' : 'nein'}</b><span>Scanner</span></div>
        <div><b>${scanner.battery >= 0 ? scanner.battery + ' %' : '–'}</b><span>Scanner-Akku</span></div>
      </div>
      <div class="row" style="margin-top:12px">
        <select data-dev-loc="${esc(d.device_id)}">
          ${state.locations.map((l) => `<option ${l.name === d.active_location ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
        </select>
        <button class="sm" data-dev-beep="${esc(d.device_id)}">Ton</button>
        <button class="sm" data-dev-update="${esc(d.device_id)}"${updateHint(d)}>Update</button>
        <button class="sm danger" data-dev-reboot="${esc(d.device_id)}">Neustart</button>
      </div></div>`;
  }).join('') : '<div class="card empty">Noch kein Terminal verbunden.</div>';

  $('#sim-device').innerHTML = state.devices.map((d) =>
    `<option value="${esc(d.device_id)}">${esc(d.name || d.device_id)}</option>`).join('');
}

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button[data-dev-beep],button[data-dev-reboot],button[data-dev-update]');
  if (!b) return;
  try {
    if (b.dataset.devBeep) {
      await post(`/api/devices/${b.dataset.devBeep}/beep`); toast('Ton gesendet');
    } else if (b.dataset.devUpdate) {
      if (!confirm('Firmware jetzt aufspielen? Das Terminal startet danach neu.')) return;
      const res = await post(`/api/firmware/push/${b.dataset.devUpdate}`);
      toast(`Update auf ${res.version} gestartet`, 'success');
    } else {
      if (!confirm('Terminal neu starten?')) return;
      await post(`/api/devices/${b.dataset.devReboot}/reboot`); toast('Neustart ausgelöst');
    }
  } catch (e) { toast(e.message, 'error'); }
});

$('#fw-fetch')?.addEventListener('click', async (ev) => {
  const button = ev.currentTarget;
  button.disabled = true;
  button.textContent = 'Wird geholt…';
  try {
    const res = await post('/api/firmware/fetch');
    toast(`${res.images.length} Abbild(er) geholt: ${res.images[0].version}`, 'success');
    await loadFirmware();
    await loadDevices();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Aus GitHub-Release holen';
  }
});

$('#fw-upload-form')?.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const file = $('#fw-file').files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  try {
    const query = new URLSearchParams({
      board: $('#fw-board').value,
      version: $('#fw-version').value.trim(),
    });
    const res = await fetch(`/api/firmware/upload?${query}`, { method: 'POST', body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload fehlgeschlagen');
    toast(`${data.version} hinterlegt (Variante ${data.board})`, 'success');
    $('#fw-file').value = '';
    $('#fw-version').value = '';
    await loadFirmware();
    await loadDevices();
  } catch (e) { toast(e.message, 'error'); }
});

document.addEventListener('change', async (ev) => {
  const sel = ev.target.closest('select[data-dev-loc]');
  if (!sel) return;
  await patch(`/api/devices/${sel.dataset.devLoc}`, { active_location: sel.value });
  toast('Lagerort gesetzt', 'success');
});

$('#sim-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const code = $('#sim-code').value.trim();
  const device = $('#sim-device').value;
  if (!code || !device) return;
  try {
    await post(`/api/devices/${device}/scan?code=${encodeURIComponent(code)}`);
    toast('Scan gesendet', 'success');
    $('#sim-code').value = '';
  } catch (e) { toast(e.message, 'error'); }
});

// ---------------------------------------------------------------------- System
async function loadSystem() {
  const [info, settings, events] = await Promise.all([
    get('/api/system'), get('/api/settings'), get('/api/events?limit=60'),
  ]);
  state.settings = settings;

  $('#sys-info').innerHTML = `
    Version ${esc(info.version)}<br>Python ${esc(info.python)}<br>
    Datenbank ${esc(info.database)}<br>Zeitzone ${esc(info.timezone)}<br>
    Laufzeit ${Math.floor(info.uptime / 3600)} h ${Math.floor((info.uptime % 3600) / 60)} min<br>
    Terminals online ${info.devices.count}<br>
    Kanäle ${Object.entries(info.notify).filter(([, v]) => v).map(([k]) => k).join(', ') || 'keine'}`;

  $('#set-expiring-days').value = settings.ui?.expiring_days ?? 7;
  $('#set-brightness').value = settings.device_ui?.brightness ?? 80;
  $('#set-idle').value = settings.device_ui?.idle_seconds ?? 60;
  $('#set-beep').checked = settings.device_ui?.beep ?? true;
  $('#set-print-enabled').checked = settings.printer?.enabled ?? true;
  $('#set-paper').value = settings.printer?.paper_chars ?? 32;
  $('#set-feed').value = settings.printer?.post_feed_dots ?? 86;
  $('#set-qr').checked = settings.printer?.qr ?? true;
  $('#set-c128').checked = settings.printer?.code128 ?? true;
  $('#set-lw').value = settings.printer?.label_width_mm ?? 50;
  $('#set-lh').value = settings.printer?.label_height_mm ?? 30;
  $('#set-orient').value = settings.printer?.label_orientation ?? 'quer';
  await loadLayouts();

  $('#sys-events').innerHTML = events.map((e) => `<tr>
    <td class="mono">${fmtTime(e.ts)}</td><td>${esc(e.type)}</td>
    <td class="mono">${esc(e.label)}</td><td class="name">${esc(e.name)}</td>
    <td>${esc(e.device)}</td>
    <td class="mono muted">${esc(JSON.stringify(e.payload || {}).slice(0, 60))}</td></tr>`).join('');
}

$('#set-ui-save').addEventListener('click', () => saveSetting('ui', {
  expiring_days: parseInt($('#set-expiring-days').value, 10) || 7,
}));
$('#set-device-save').addEventListener('click', () => saveSetting('device_ui', {
  brightness: parseInt($('#set-brightness').value, 10) || 80,
  idle_seconds: parseInt($('#set-idle').value, 10) || 0,
  beep: $('#set-beep').checked,
}));
$('#set-printer-save').addEventListener('click', () => saveSetting('printer', {
  enabled: $('#set-print-enabled').checked,
  paper_chars: parseInt($('#set-paper').value, 10) || 32,
  post_feed_dots: parseInt($('#set-feed').value, 10) || 0,
  qr: $('#set-qr').checked,
  code128: $('#set-c128').checked,
}));

async function saveSetting(key, body) {
  try { await patch(`/api/settings/${key}`, body); toast('Gespeichert', 'success'); }
  catch (e) { toast(e.message, 'error'); }
}

// -------------------------------------------------------- Etikettenlayouts
// Die Vorschauen kommen fertig vom Server und entstehen aus genau dem
// Payload, den auch der Drucker bekommt. Ein zweites Layout im Browser waere
// die naechste Stelle, an der Bildschirm und Papier auseinanderlaufen.
async function loadLayouts() {
  const box = $('#layout-picker');
  if (!box) return;
  const rows = await get('/api/labels/layouts');
  const chosen = state.settings?.printer?.label_layout || 'standard';

  box.innerHTML = rows.map((l) => `<div class="layout ${l.name === chosen ? 'active' : ''}"
      data-layout="${esc(l.name)}" role="button" tabindex="0">
    <div class="paper">${l.svg}</div>
    <div class="who">${esc(l.title || l.name)}</div>
    <div class="why">${esc(l.description)}</div>
    <div class="fill">${esc(l.code)} · ${l.dots} von ${l.height_dots} Punkten</div>
  </div>`).join('');

  box.querySelectorAll('[data-layout]').forEach((el) => el.addEventListener('click', async () => {
    await saveSetting('printer', { label_layout: el.dataset.layout });
    state.settings.printer = { ...state.settings.printer, label_layout: el.dataset.layout };
    box.querySelectorAll('[data-layout]').forEach((o) => o.classList.toggle('active', o === el));
  }));
}

// Masse und Ausrichtung aendern jede Vorschau - deshalb neu zeichnen, nicht
// nur speichern.
async function saveLabelGeometry() {
  const body = {
    label_width_mm: parseFloat($('#set-lw').value) || 50,
    label_height_mm: parseFloat($('#set-lh').value) || 30,
    label_orientation: $('#set-orient').value,
  };
  await saveSetting('printer', body);
  state.settings.printer = { ...state.settings.printer, ...body };
  await loadLayouts();
}

['#set-lw', '#set-lh', '#set-orient'].forEach((sel) => {
  const el = $(sel);
  if (el) el.addEventListener('change', () => saveLabelGeometry().catch((e) => toast(e.message, 'error')));
});

$('#notify-test').addEventListener('click', async () => {
  try { const r = await post('/api/notify/test'); toast(JSON.stringify(r), 'success'); }
  catch (e) { toast(e.message, 'error'); }
});

$('#import-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const file = $('#import-file').files[0];
  if (!file) return;
  const dryRun = $('#import-dry-run').checked;

  const body = new FormData();
  body.append('file', file);
  const box = $('#import-result');
  box.innerHTML = '<span class="muted">Wird verarbeitet…</span>';

  try {
    const res = await fetch(`/api/import/v1?dry_run=${dryRun}`, { method: 'POST', body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Fehler');

    box.innerHTML = `
      <div class="row">
        <span class="pill ${dryRun ? '' : 'ok'}">${dryRun ? 'Prüflauf' : 'Importiert'}</span>
        <span>${data.imported} von ${data.total_rows} Zeilen</span>
        ${data.skipped_duplicate ? `<span class="pill">${data.skipped_duplicate} Duplikate übersprungen</span>` : ''}
        ${data.skipped_invalid ? `<span class="pill warn">${data.skipped_invalid} ungültig</span>` : ''}
      </div>
      ${data.errors.length ? `<ul class="muted" style="margin:8px 0 0;padding-left:18px">${
        data.errors.map((e) => `<li>${esc(e)}</li>`).join('')
      }</ul>` : ''}`;

    if (!dryRun && data.imported) {
      toast(`${data.imported} Artikel übernommen`, 'success');
      await Promise.all([loadInventory().catch(() => {}), loadDashboard().catch(() => {})]);
    }
  } catch (e) {
    box.innerHTML = '';
    toast(e.message, 'error');
  }
});

// ------------------------------------------------------------------- Dialoge
function modal(title, fields, onReady) {
  const dlg = $('#modal');
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = fields.map((f) => {
    if (f.options) {
      return `<label class="field">${esc(f.label)}<select name="${f.name}">
        ${f.options.map((o) => `<option ${o === f.value ? 'selected' : ''}>${esc(o)}</option>`).join('')}
        ${f.options.includes(f.value) ? '' : `<option selected>${esc(f.value ?? '')}</option>`}
      </select></label>`;
    }
    if (f.type === 'checkbox') {
      return `<label class="row tight"><input type="checkbox" name="${f.name}" ${f.value ? 'checked' : ''}> ${esc(f.label)}</label>`;
    }
    if (f.type === 'tags') {
      // Kein <label>: der Blaseneditor enthaelt mehrere Bedienelemente, ein
      // umschliessendes Label wuerde jeden Klick auf das erste umlenken.
      return `<div class="field"><span>${esc(f.label)}</span><div data-tags="${f.name}"></div></div>`;
    }
    return `<label class="field">${esc(f.label)}
      <input name="${f.name}" type="${f.type || 'text'}" value="${esc(f.value ?? '')}"></label>`;
  }).join('');

  const editors = {};
  fields.filter((f) => f.type === 'tags').forEach((f) => {
    editors[f.name] = tagEditor($(`#modal-body [data-tags="${f.name}"]`), f.value || [], f.placeholder);
  });

  if (onReady) onReady($('#modal-body'), editors);

  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => {
      if (dlg.returnValue !== 'ok') return resolve(null);
      const out = {};
      fields.forEach((f) => {
        if (f.type === 'tags') { out[f.name] = editors[f.name].values(); return; }
        const el = $(`#modal-body [name="${f.name}"]`);
        out[f.name] = f.type === 'checkbox' ? el.checked : el.value;
      });
      resolve(out);
    }, { once: true });
  });
}

// ---------------------------------------------------------------- Live-Updates
const PAGE_LOADERS = {
  dashboard: loadDashboard,
  inventory: loadInventory,
  labels: loadLabels,
  templates: loadTemplates,
  catalog: loadCatalogPage,
  shopping: loadShopping,
  // Erst die Abbilder, dann die Geraete: updateHint() vergleicht die laufende
  // Version mit der hinterlegten und braucht state.firmware bereits gefuellt.
  devices: async () => { await loadFirmware(); await loadDevices(); },
  system: loadSystem,
};

function activePage() {
  return document.querySelector('.page.active')?.id.replace('page-', '') || 'dashboard';
}

async function refreshActive() {
  const loader = PAGE_LOADERS[activePage()];
  if (loader) await loader();
}

liveConnect(
  (msg) => {
    // Nur neu laden, wenn die offene Seite das Ereignis auch anzeigt.
    const relevant = {
      inventory: ['dashboard', 'inventory', 'labels'],
      catalog: ['catalog', 'templates', 'labels'],
      devices: ['devices', 'dashboard'],
      shopping: ['shopping'],
      settings: ['system'],
    }[msg.event] || [];
    if (relevant.includes(activePage())) refreshActive().catch(() => {});
  },
  (online) => {
    $('#conn-dot').classList.toggle('on', online);
    $('#conn-text').textContent = online ? 'live' : 'getrennt';
  },
);

// ------------------------------------------------------------------- Startlauf
(async function start() {
  try {
    await loadCatalogData();
  } catch (e) {
    toast(`Server nicht erreichbar: ${e.message}`, 'error');
  }
  showPage((location.hash || '#dashboard').slice(1));
})();
