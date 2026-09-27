// Admin-Oberflaeche. Bewusst ohne Framework und ohne Build-Schritt: das Image
// bleibt klein und die Dateien sind das, was ausgeliefert wird.

import { get, post, patch, put, del, toast, auslagern, fmtDate, fmtTime, daysPill, eventName, eventDetails, esc, liveConnect, tagEditor } from './api.js';

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
  toast(`Design: ${{ auto: 'wie das System', light: 'hell', dark: 'dunkel' }[next]}`);
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
    ['Eingelagert (30 Tage)', stats.added_30d, ''],
    ['Ausgelagert (30 Tage)', stats.removed_30d, ''],
  ].map(([label, value, cls]) =>
    `<div class="card stat ${cls}"><div class="value">${value}</div><div class="label">${label}</div></div>`
  ).join('');

  $('#dash-expiring').innerHTML = expiring.length
    ? expiring.map((i) => `<tr>
        <td class="name">${esc(i.display_name || i.name)}</td>
        <td>${fmtDate(i.expiry_date)} ${daysPill(i.days_left)}</td>
        <td>${esc(i.location)}</td>
        <td class="right"><button class="sm" data-remove="${esc(i.label)}">Auslagern</button></td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="muted">Nichts läuft demnächst ab.</td></tr>';

  $('#dash-events').innerHTML = events.map((e) => `<tr>
      <td class="muted">${fmtTime(e.ts)}</td><td>${esc(eventName(e.type))}</td>
      <td class="name">${esc(e.name || e.label || e.barcode)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="muted">Noch nichts passiert.</td></tr>';

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
        ? `<button class="sm" data-remove="${esc(i.label)}">Auslagern</button>`
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
      btn.disabled = true;
      await auslagern(btn.dataset.remove, 'web', refreshActive);
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
  const [roll, queue, settings] = await Promise.all([
    get('/api/labels/roll'), get('/api/labels/queue?limit=25'), get('/api/settings'),
  ]);
  state.settings = settings;
  fillSettings(settings);
  updateRotateWarning();
  loadLayouts().catch((e) => toast(e.message, 'error'));
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
// Kalibrierdruck: ein Messstreifen, kein Etikett. Die Anleitung kommt vom
// Server und nicht aus dieser Datei - sonst beschreibt sie irgendwann einen
// Streifen, der so gar nicht mehr gedruckt wird.
$('#calibrate').addEventListener('click', async () => {
  const box = $('#calibration-guide');
  try {
    const r = await post('/api/labels/calibrate');
    box.innerHTML = `
      <p class="muted" style="margin:0 0 8px">
        Streifen gesendet (${r.laenge_mm} mm Papier). Mit einem Lineal
        nachmessen und die Werte unten eintragen.</p>
      <ol style="margin:0;padding-left:20px">${r.anleitung.map((a) => `
        <li style="margin-bottom:8px">
          <b>${esc(a.titel)}</b><br>
          <span>${esc(a.messen)}</span><br>
          <span class="muted" style="font-size:12px">${esc(a.bedeutet)}</span>
        </li>`).join('')}</ol>`;
    box.hidden = false;
    toast('Kalibrierdruck gesendet', 'success');
  } catch (e) { toast(e.message, 'error'); }
});
// Update-Pruefung. Fragt beim Ursprung nach und sagt, wie weit dieser Server
// zurueck ist - aktualisiert aber nichts: ein Dienst, der sich selbst
// ersetzt, braucht einen Rueckweg, und den gibt es noch nicht.
$('#update-check').addEventListener('click', async () => {
  const box = $('#update-state');
  box.textContent = 'Wird geprüft …';
  try {
    const r = await get('/api/system/update?force=true');
    if (r.fehler) {
      box.innerHTML = `<span style="color:var(--warn,#cc9218)">${esc(r.fehler)}</span>`;
      return;
    }
    const teile = [];
    if (r.update_verfuegbar) {
      const n = r.neueste || {};
      teile.push(`<b>Update verfügbar:</b> ${esc(n.version || '')}`
        + (n.commit_kurz ? ` · ${esc(n.commit_kurz)}` : '')
        + (r.rueckstand ? ` · ${r.rueckstand} Commit(s) zurück` : ''));
      if (n.titel) teile.push(`<span class="muted">${esc(n.titel)}</span>`);
    } else {
      teile.push('<b>Aktuell.</b>');
    }
    if (r.hinweis) teile.push(`<span class="muted">${esc(r.hinweis)}</span>`);
    // Was zu tun ist, in Klicks - nicht "das Abbild neu ziehen".
    if (r.anleitung?.length) {
      teile.push(`<ol style="margin:6px 0 0;padding-left:20px">${
        r.anleitung.map((x) => `<li>${esc(x)}</li>`).join('')}</ol>`);
    }
    if (r.abbild) teile.push(`<code class="mono">${esc(r.abbild)}</code>`);
    teile.push(`<a href="${esc(r.url)}" target="_blank" rel="noopener">Änderungen ansehen</a>`);

    // Den Knopf nur zeigen, wenn es etwas zu tun gibt *und* jemand da ist,
    // der den Container ersetzen darf. Sonst statt eines toten Knopfes die
    // Erklärung, was dafür fehlt.
    const anstoss = r.anstoss || {};
    $('#update-apply').hidden = !(r.update_verfuegbar && anstoss.moeglich);
    if (r.update_verfuegbar && !anstoss.moeglich) {
      teile.push(`<span class="muted">${esc(anstoss.hinweis || '')}</span>`);
    }
    box.innerHTML = teile.join('<br>');
  } catch (e) {
    box.innerHTML = `<span style="color:var(--danger,#f04640)">${esc(e.message)}</span>`;
  }
});
// Aktualisieren. Der Server lädt dabei nichts und führt nichts aus - er
// bittet den eingerichteten Dienst, das neue Abbild zu ziehen und diesen
// Container zu ersetzen. Wir verlieren dabei die Verbindung; statt auf eine
// Antwort zu warten, warten wir auf das Wiederkommen.
$('#update-apply').addEventListener('click', async () => {
  if (!confirm('Server aktualisieren?\n\nDer Container wird dabei ersetzt. '
      + 'Das Terminal verliert kurz die Verbindung und verbindet sich von '
      + 'selbst wieder. Die Daten im Volume bleiben unberührt.')) return;

  const box = $('#update-state');
  const knopf = $('#update-apply');
  knopf.disabled = true;
  try {
    const r = await post('/api/system/update/apply');
    box.innerHTML = `<b>${esc(r.hinweis)}</b><br>`
      + '<span class="muted">Warte auf den Neustart …</span>';
    await warteAufServer(box);
  } catch (e) {
    box.innerHTML = `<span style="color:var(--danger,#f04640)">${esc(e.message)}</span>`;
    knopf.disabled = false;
  }
});

// Nach dem Ersetzen antwortet der Server erst nicht und dann wieder. Genau
// das ist das Signal - eine Rückmeldung vom alten Container kann es nicht
// geben, der ist ja weg.
async function warteAufServer(box) {
  const bis = Date.now() + 180000;
  let warWeg = false;
  while (Date.now() < bis) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const h = await fetch('/api/health', { cache: 'no-store' });
      if (!h.ok) throw new Error('nicht bereit');
      if (warWeg) {
        box.innerHTML = '<b>Fertig.</b> <span class="muted">Der Server ist '
          + 'wieder da. Die Seite wird neu geladen …</span>';
        setTimeout(() => location.reload(), 1500);
        return;
      }
    } catch {
      warWeg = true;   // jetzt wird ersetzt
      box.innerHTML = '<b>Der Server wird ersetzt …</b>';
    }
  }
  box.innerHTML = '<span class="muted">Der Server ist nach drei Minuten nicht '
    + 'zurückgekommen. Im TrueNAS nachsehen, ob die App läuft.</span>';
}
$('#queue-reload').addEventListener('click', () => loadLabels());
$('#queue-clear').addEventListener('click', async () => {
  if (!confirm('Alle wartenden Druckaufträge verwerfen?\n\nDie Artikel bleiben im Bestand; '
    + 'ihre Etiketten lassen sich im Inventar einzeln nachdrucken.')) return;
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

// Noch kein Terminal: statt einer leeren Zeile die Schritte, mit denen eins
// dazukommt - samt der Adresse, die es braucht. Die steht sonst nirgends in
// der Oberflaeche, und das Portal am Terminal fragt genau danach.
function terminalEinrichten(eingebettet = false) {
  const sicher = location.protocol === 'https:';
  const port = location.port || (sicher ? '443' : '80');
  return `<div class="${eingebettet ? '' : 'card'}" style="grid-column:1/-1">
    ${eingebettet ? '' : '<h2>Noch kein Terminal verbunden</h2>'}
    <p class="muted" style="margin:0">So kommt eins dazu:</p>
    <ol class="steps">
      <li>Terminal einschalten. Findet es kein bekanntes WLAN, öffnet es ein eigenes:
        <code>Lebensmittel-Terminal</code>, Passwort <code>12345678</code>.</li>
      <li>Mit dem Handy in dieses WLAN gehen und <code>192.168.4.1</code> öffnen
        (meist öffnet sich die Seite von selbst).</li>
      <li>Heim-WLAN auswählen und dazu eintragen:
        Server <code>${esc(location.hostname)}</code>,
        Port <code>${esc(port)}</code>${sicher ? ', Verschlüsselung <code>HTTPS / WSS</code>' : ''},
        Geräte-Token = der Wert von <code>DEVICE_TOKEN</code> aus der Container-Konfiguration.</li>
      <li>Speichern. Das Terminal startet neu und erscheint hier nach wenigen Sekunden.</li>
    </ol>
    <p class="hint">Zeigt das Terminal „Kein Server“, stimmt meist das Token nicht –
      es muss Zeichen für Zeichen gleich sein.</p>
  </div>`;
}

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

// WLAN-Empfang in Worten; -67 dBm sagt nur jemandem etwas, der es schon weiss.
function wlanText(rssi) {
  if (rssi === undefined || rssi === null) return '–';
  const wort = rssi >= -60 ? 'gut' : rssi >= -72 ? 'mittel' : 'schwach';
  return `${wort} <span class="muted">(${rssi} dBm)</span>`;
}

function deviceCard(d) {
  const t = d.telemetry || {};
  const scanner = t.scanner || {};
  const kopf = `<div class="row"><h2 style="margin:0;flex:1">
        <span class="dot ${d.online ? 'on' : ''}"></span>${esc(d.name || d.device_id)}</h2>
        ${d.firmware ? `<span class="pill">Firmware ${esc(d.firmware)}</span>` : ''}</div>
      <div class="muted">${esc(d.device_id)}${d.ip ? ` · ${esc(d.ip)}` : ''}</div>`;

  // Ein getrenntes Terminal zeigte dieselbe Karte wie ein verbundenes, nur mit
  // Strichen in jedem Feld - und Knoepfen, die ins Leere liefen.
  if (!d.online) {
    return `<div class="card">${kopf}
      <div class="notice" style="margin-top:12px"><span><b>Nicht verbunden</b> – zuletzt gesehen
        ${fmtTime(d.last_seen) || 'nie'}. Meist ist das Terminal aus oder hat kein WLAN.
        Zeigt es „Kein Server“, stimmt das Geräte-Token nicht.</span></div>
      <div class="row" style="margin-top:12px">
        <button class="sm ghost" data-dev-forget="${esc(d.device_id)}">Aus der Liste entfernen</button>
      </div></div>`;
  }
  return `<div class="card">${kopf}
      <div class="telemetry">
        <div><b>${scanner.connected ? 'verbunden' : 'nicht verbunden'}</b><span>Handscanner</span></div>
        <div><b>${scanner.battery >= 0 ? scanner.battery + ' %' : '–'}</b><span>Scanner-Akku</span></div>
        <div><b>${wlanText(t.rssi)}</b><span>WLAN</span></div>
        <div><b>${t.uptime ? Math.floor(t.uptime / 3600) + ' h' : '–'}</b><span>Läuft seit</span></div>
        <div><b>${t.heap ? Math.round(t.heap / 1024) + ' K' : '–'}</b><span>Speicher frei</span></div>
        <div><b>${t.min_heap ? Math.round(t.min_heap / 1024) + ' K' : '–'}</b><span>Speicher min.</span></div>
      </div>
      <label class="field" style="margin-top:12px">Lagert ein in
        <select data-dev-loc="${esc(d.device_id)}">
          ${state.locations.map((l) => `<option ${l.name === d.active_location ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
        </select></label>
      <div class="row" style="margin-top:10px">
        <button class="sm" data-dev-beep="${esc(d.device_id)}">Ton</button>
        <button class="sm" data-dev-update="${esc(d.device_id)}"${updateHint(d)}>Firmware-Update</button>
        <button class="sm" data-dev-reboot="${esc(d.device_id)}">Neustart</button>
      </div></div>`;
}

async function loadDevices() {
  state.devices = await get('/api/devices');
  // Die Anleitung bleibt erreichbar, wenn schon Terminals da sind - fuer das
  // zweite oder ein ersetztes Geraet.
  $('#devices-list').innerHTML = state.devices.length
    ? state.devices.map(deviceCard).join('')
      + `<details class="card" style="grid-column:1/-1"><summary><h2 style="display:inline">Weiteres Terminal einrichten</h2></summary>
          ${terminalEinrichten(true)}</details>`
    : terminalEinrichten();

  const sim = $('#sim-card');
  if (sim) sim.hidden = !state.devices.some((d) => d.online);
  $('#sim-device').innerHTML = state.devices.filter((d) => d.online).map((d) =>
    `<option value="${esc(d.device_id)}">${esc(d.name || d.device_id)}</option>`).join('');
}

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button[data-dev-beep],button[data-dev-reboot],button[data-dev-update],button[data-dev-forget]');
  if (!b) return;
  try {
    if (b.dataset.devForget) {
      if (!confirm('Terminal aus der Liste entfernen?\n\nMeldet es sich wieder, erscheint es von selbst neu.')) return;
      await del(`/api/devices/${encodeURIComponent(b.dataset.devForget)}`);
      await loadDevices();
    } else if (b.dataset.devBeep) {
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

// Ein Feld fuellen, ohne dass ein fehlendes den Rest mitreisst.
//
// `$('#weg').value = x` wirft, wenn es das Element nicht gibt - und riss damit
// alles mit, was danach kam. Genau das ist passiert, als das Feld "Nachschub"
// entfernt wurde: ein Browser mit noch altem Skript und schon neuem Aufbau
// brach mitten im Fuellen ab, und die halbe Etikettenmaske blieb leer. Ohne
// Fehlermeldung, ohne Hinweis - man sah nur leere Felder und hielt das
// Update fuer kaputt.
//
// Aufbau und Skript werden hier nie gleichzeitig ausgetauscht (der Browser
// hat das eine schon und das andere noch nicht), also darf ein Unterschied
// zwischen beiden nicht mehr kosten als das eine Feld.
function setzen(auswahl, wert) {
  const el = $(auswahl);
  if (!el) {
    console.warn(`Feld ${auswahl} gibt es nicht mehr - uebersprungen`);
    return;
  }
  if (el.type === 'checkbox') el.checked = Boolean(wert);
  else el.value = wert;
}

async function loadSystem() {
  const [info, settings, events] = await Promise.all([
    get('/api/system'), get('/api/settings'), get('/api/events?limit=60'),
  ]);
  state.settings = settings;

  // Fassung lesbar statt als 40-Zeichen-Hash: Zweig bzw. Tag, kurzer Commit,
  // Baudatum. Ohne das war die Frage "laeuft hier der aktuelle Stand?" vom
  // Panel aus nicht zu beantworten.
  const stand = info.aus_abbild ? [esc(info.version)] : ['Entwicklungsstand (nicht aus einem Abbild)'];
  if (info.commit_kurz) stand.push(`<span class="mono">${esc(info.commit_kurz)}</span>`);
  if (info.gebaut) stand.push(`gebaut ${fmtTime(info.gebaut)}`);
  const kanaele = Object.entries(info.notify || {}).filter(([, v]) => v).map(([k]) => (
    { ntfy: 'ntfy', telegram: 'Telegram', mqtt: 'MQTT' }[k] || k));
  const laufzeit = info.uptime >= 86400
    ? `${Math.floor(info.uptime / 86400)} Tage ${Math.floor((info.uptime % 86400) / 3600)} h`
    : `${Math.floor(info.uptime / 3600)} h ${Math.floor((info.uptime % 3600) / 60)} min`;
  $('#sys-info').innerHTML = [
    ['Fassung', stand.join(' · ')],
    ['Terminals verbunden', String(info.devices.count)],
    ['Benachrichtigungen', kanaele.length ? esc(kanaele.join(', ')) : 'keine eingerichtet'],
    ['Passwortschutz', info.passwortschutz ? 'an' : 'aus'],
    ['Datenbank', esc(info.database.startsWith('sqlite') ? 'SQLite' : info.database)],
    ['Zeitzone', esc(info.timezone)],
    ['Läuft seit', laufzeit],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

  // Was bisher nur im Container-Protokoll stand, wo es niemand liest.
  const warnungen = [];
  if (info.token_standard) {
    warnungen.push(`<div class="notice warn"><span><b>Das Geräte-Token steht noch auf dem
      Standardwert.</b> Jeder im Netz könnte sich als Terminal ausgeben. In der
      Container-Konfiguration <code>DEVICE_TOKEN</code> auf einen eigenen Wert setzen
      (z. B. <code>openssl rand -hex 24</code>) und denselben Wert im WLAN-Portal des
      Terminals eintragen.</span></div>`);
  }
  $('#sys-warnings').innerHTML = warnungen.join('');
  const hint = $('#update-knopf-hint');
  if (hint) hint.hidden = Boolean(info.update_knopf);

  fillSettings(settings);

  $('#sys-events').innerHTML = events.map((e) => `<tr>
    <td class="muted">${fmtTime(e.ts)}</td><td>${esc(eventName(e.type))}</td>
    <td class="mono">${esc(e.label || e.barcode)}</td><td class="name">${esc(e.name)}</td>
    <td>${esc(e.device)}</td>
    <td class="name muted" title="${esc(JSON.stringify(e.payload || {}))}">${esc(eventDetails(e))}</td></tr>`).join('');
}

// ---------------------------------------------------------------- Einstellungen
//
// Jedes Feld mit data-setting="bereich.schluessel" speichert sich beim Aendern
// selbst, und die Karte sagt, ob es geklappt hat.
//
// Vorher gab es auf derselben Seite zwei Arten: Oberflaeche, Terminal und
// Drucker brauchten "Speichern", der Etikettenteil speicherte von allein. Wer
// das nicht wusste, verlor Aenderungen still beim Wechsel des Reiters - oder
// suchte beim Etikett einen Knopf, den es nicht gab.
function settingValue(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.dataset.art === 'int') return parseInt(el.value, 10);
  if (el.dataset.art === 'float') return parseFloat(String(el.value).replace(',', '.'));
  return el.value;
}

function fillSettings(settings) {
  $$('[data-setting]').forEach((el) => {
    // Ein Feld, in dem gerade jemand tippt, nicht unter den Fingern
    // wegziehen - die Live-Aktualisierung eines anderen Browsers kaeme sonst
    // mitten in die Eingabe.
    if (el === document.activeElement) return;
    const [bereich, schluessel] = el.dataset.setting.split('.');
    const wert = settings?.[bereich]?.[schluessel];
    if (wert === undefined || wert === null) return;
    if (el.type === 'checkbox') el.checked = Boolean(wert);
    else el.value = wert;
  });
}

function saveState(el, text, level = '') {
  const box = el?.closest('.card')?.querySelector('[data-save-state]');
  if (!box) { if (level === 'error') toast(text, 'error'); return; }
  box.textContent = text;
  box.className = `save-state ${level}`;
  clearTimeout(box._timer);
  if (level === 'ok') box._timer = setTimeout(() => { box.textContent = ''; }, 2500);
}

async function saveSetting(el, bereich, body) {
  saveState(el, 'Speichert …');
  try {
    state.settings[bereich] = await patch(`/api/settings/${bereich}`, body);
    saveState(el, 'Gespeichert ✓', 'ok');
    return true;
  } catch (e) {
    saveState(el, `Nicht gespeichert: ${e.message}`, 'error');
    return false;
  }
}

document.addEventListener('change', async (ev) => {
  const el = ev.target.closest('[data-setting]');
  if (!el) return;
  const [bereich, schluessel] = el.dataset.setting.split('.');
  const wert = settingValue(el);

  // Ausserhalb der Grenzen nicht still irgendetwas speichern, sondern sagen,
  // was erlaubt ist. Die Schrittweite zaehlt nicht - sie ist nur fuer die
  // Pfeiltasten da.
  const v = el.validity;
  if (typeof wert === 'number' && (!Number.isFinite(wert) || v.rangeUnderflow || v.rangeOverflow || v.badInput)) {
    const grenzen = el.min !== '' && el.max !== '' ? ` zwischen ${el.min} und ${el.max}` : '';
    saveState(el, `Bitte eine Zahl${grenzen} eingeben`, 'error');
    return;
  }

  const ok = await saveSetting(el, bereich, { [schluessel]: wert });
  if (ok && bereich === 'printer') {
    updateRotateWarning();
    await loadLayouts().catch((e) => toast(e.message, 'error'));
  }
});

// Gedrehter Text mit flachem Strichcode ist erlaubt, aber selten gewollt: der
// Code steht dann quer zur Schrift. Frueher stand das nur im Hilfetext unter
// den Feldern - also dort, wo es niemand liest, nachdem er die beiden
// Einstellungen schon getroffen hat.
function updateRotateWarning() {
  const p = state.settings?.printer || {};
  const box = $('#rotate-warning');
  if (box) box.hidden = !(p.label_rotate && p.label_code === 'code128');
}

$('#rotate-fix')?.addEventListener('click', () => {
  const sel = $('[data-setting="printer.label_code"]');
  if (!sel) return;
  sel.value = 'auto';
  sel.dispatchEvent(new Event('change', { bubbles: true }));
});

// Verweise zwischen den Reitern ("steht unter Etiketten").
document.addEventListener('click', (ev) => {
  const a = ev.target.closest('a[data-goto]');
  if (!a) return;
  ev.preventDefault();
  showPage(a.dataset.goto);
  window.scrollTo(0, 0);
});

// -------------------------------------------------------- Etikettenlayouts
// Die Vorschauen kommen fertig vom Server und entstehen aus genau dem
// Payload, den auch der Drucker bekommt. Ein zweites Layout im Browser waere
// die naechste Stelle, an der Bildschirm und Papier auseinanderlaufen.
async function loadLayouts() {
  const box = $('#layout-picker');
  if (!box) return;
  const rows = await get('/api/labels/layouts');
  const chosen = state.settings?.printer?.label_layout || 'standard';

  box.innerHTML = rows.map((l) => `<div class="layout ${l.name === chosen ? 'active' : ''} ${l.passt ? '' : 'zu-gross'}"
      data-layout="${esc(l.name)}" role="button" tabindex="0" aria-pressed="${l.name === chosen}">
    <div class="paper">${l.svg}</div>
    <div class="who">${esc(l.title || l.name)}${l.name === chosen ? ' <span class="pill ok">gewählt</span>' : ''}</div>
    <div class="why">${esc(l.description)}</div>
    <div class="fill">${l.passt
      ? `passt · braucht ${fmtMm(l.mm)} mm`
      : `<b>passt nicht</b> · braucht ${fmtMm(l.mm)} mm`}${l.rotate
      ? '<br>Vorschau ungedreht – der Drucker dreht die Schrift' : ''}</div>
  </div>`).join('');
}

function fmtMm(mm) {
  return Number(mm).toLocaleString('de-DE', { maximumFractionDigits: 1 });
}

async function chooseLayout(el) {
  const ok = await saveSetting(el, 'printer', { label_layout: el.dataset.layout });
  if (ok) await loadLayouts();
}
$('#layout-picker')?.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-layout]');
  if (el) chooseLayout(el);
});
$('#layout-picker')?.addEventListener('keydown', (ev) => {
  const el = ev.target.closest('[data-layout]');
  if (el && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); chooseLayout(el); }
});

$('#notify-test').addEventListener('click', async () => {
  try {
    const r = await post('/api/notify/test');
    const ok = Object.keys(r).filter((k) => r[k]);
    const fehl = Object.keys(r).filter((k) => !r[k]);
    toast(`Gesendet über ${ok.join(', ')}${fehl.length ? ` – fehlgeschlagen: ${fehl.join(', ')}` : ''}`,
      fehl.length ? 'warn' : 'success');
  } catch (e) { toast(e.message, 'error'); }
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
      settings: ['system', 'labels'],
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
