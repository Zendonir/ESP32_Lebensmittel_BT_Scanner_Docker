// Admin-Oberflaeche. Bewusst ohne Framework und ohne Build-Schritt: das Image
// bleibt klein und die Dateien sind das, was ausgeliefert wird.

import { get, post, patch, put, del, toast, auslagern, fmtDate, fmtTime, daysPill, eventName, eventDetails, esc, gruppieren, mengeText, EINHEITEN, liveConnect, tagEditor, HAUSHALT, fremdHinweis, zielOptionen, zielAuswerten, zielVorauswahl } from './api.js';
import { mhdKamera } from './mhdkamera.js';
import { zipBlob, base64ZuBytes } from './zip.js';

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

  let fremdBox = $('#dash-fremd');
  if (!fremdBox && HAUSHALT) {
    fremdBox = document.createElement('div');
    fremdBox.id = 'dash-fremd';
    $('#stats').before(fremdBox);
  }
  fremdHinweis(fremdBox).catch(() => {});

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
        <td>${ortMitFarbe(i.location)}</td>
        <td class="right"><button class="sm" data-remove="${esc(i.label)}">Auslagern</button></td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="muted">Nichts läuft demnächst ab.</td></tr>';

  $('#dash-events').innerHTML = events.map((e) => `<tr>
      <td class="muted">${fmtTime(e.ts)}</td><td>${esc(eventName(e.type))}${e.payload?.fremd
        ? ` <span class="pill warn">von ${esc(e.payload.fremd)}</span>` : ''}</td>
      <td class="name">${esc(e.name || e.label || e.barcode)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="muted">Noch nichts passiert.</td></tr>';

  $('#dash-cat').innerHTML = barList(stats.by_category, state.categories);
  $('#dash-loc').innerHTML = barList(stats.by_location, []);
}

// Lagerort mit seinem Farbpunkt (Stammdaten -> Lagerorte).
function ortMitFarbe(name) {
  if (!name) return '';
  const farbe = state.locations.find((l) => l.name === name)?.color;
  return `${farbe ? `<span class="swatch" style="background:${esc(farbe)}"></span>` : ''}${esc(name)}`;
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
  if ($('#inv-alle')?.checked) params.set('alle', 'true');

  const rows = await get(`/api/inventory?${params}`);
  $('#inv-empty').hidden = rows.length > 0;
  const fremd = (i) => (HAUSHALT && i.household !== HAUSHALT.name
    ? ` <span class="pill warn" title="gehört einem anderen Haushalt – Auslagern ist eine Fremdentnahme">🏠 ${esc(i.household)}</span>` : '');
  const menge = (q, unit) => esc(mengeText(q, unit) || String(q));
  const zeile = (i, gruppe = '') => `<tr${gruppe ? ` class="inv-kind" data-gruppe="${esc(gruppe)}"${offeneGruppen.has(gruppe) ? '' : ' hidden'}` : ''}>
    <td class="mono">${esc(i.label)}</td>
    <td class="name">${gruppe ? '<span class="muted">└</span>' : `<b>${esc(i.display_name || i.name)}</b>${fremd(i)}${i.brand ? `<br><span class="muted">${esc(i.brand)}</span>` : ''}`}</td>
    <td>${gruppe ? '' : esc(i.category)}</td>
    <td>${fmtDate(i.expiry_date)}</td>
    <td>${daysPill(i.days_left)}</td>
    <td>${menge(i.quantity, i.unit)}</td>
    <td>${ortMitFarbe(i.location)}</td>
    <td class="right">
      <button class="sm" data-edit="${esc(i.label)}">Ändern</button>
      <button class="sm" data-reprint="${esc(i.label)}">Druck</button>
      ${i.status === 'active'
        ? `<button class="sm" data-remove="${esc(i.label)}">Auslagern</button>`
        : `<button class="sm" data-restore="${esc(i.label)}">Zurück</button>`}
    </td></tr>`;
  // Gleiche Produkte als eine aufklappbare Zeile (Summe, fruehestes MHD).
  const kopf = (g) => {
    const e = g.erster, offen = offeneGruppen.has(g.key);
    return `<tr class="inv-gruppe" data-gruppe="${esc(g.key)}" aria-expanded="${offen}">
    <td class="mono muted">${offen ? '▾' : '▸'} ${g.items.length}×</td>
    <td class="name"><b>${esc(e.display_name || e.name)}</b>${fremd(e)}${e.brand ? `<br><span class="muted">${esc(e.brand)}</span>` : ''}</td>
    <td>${esc(e.category)}</td>
    <td>${e.expiry_date ? `ab ${fmtDate(e.expiry_date)}` : ''}</td>
    <td>${daysPill(e.days_left)}</td>
    <td>${menge(g.menge, e.unit)}</td>
    <td>${g.orte.map(ortMitFarbe).join('<br>')}</td>
    <td class="right muted">${offen ? 'zuklappen' : 'aufklappen'}</td></tr>`;
  };
  $('#inv-body').innerHTML = gruppieren(rows).map((g) => (g.items.length > 1
    ? kopf(g) + g.items.map((i) => zeile(i, g.key)).join('')
    : zeile(g.items[0]))).join('');
}

// Aufgeklappte Gruppen bleiben offen, wenn die Tabelle neu geladen wird.
const offeneGruppen = new Set();
$('#inv-body').addEventListener('click', (ev) => {
  const kopf = ev.target.closest('tr.inv-gruppe');
  if (!kopf) return;
  const key = kopf.dataset.gruppe;
  const offen = !offeneGruppen.has(key);
  if (offen) offeneGruppen.add(key); else offeneGruppen.delete(key);
  kopf.setAttribute('aria-expanded', String(offen));
  kopf.cells[0].textContent = `${offen ? '▾' : '▸'} ${kopf.cells[0].textContent.split(' ')[1]}`;
  kopf.cells[7].textContent = offen ? 'zuklappen' : 'aufklappen';
  document.querySelectorAll('#inv-body tr.inv-kind').forEach((tr) => { if (tr.dataset.gruppe === key) tr.hidden = !offen; });
});

if (HAUSHALT) {
  const lbl = document.createElement('label');
  lbl.className = 'row tight';
  lbl.innerHTML = '<input type="checkbox" id="inv-alle"> auch in anderen Haushalten';
  lbl.title = 'Nur zusammen mit einem Suchbegriff. Wer dort auslagert, macht eine Fremdentnahme.';
  $('#inv-reload').before(lbl);
}
['#inv-q', '#inv-category', '#inv-location', '#inv-status', '#inv-sort', '#inv-alle'].forEach((sel) => {
  const el = $(sel);
  if (!el) return;
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
    { name: 'unit', label: 'Einheit (kg/l werden zu g/ml)', value: item.unit, options: EINHEITEN.includes(item.unit) ? EINHEITEN : [...EINHEITEN, item.unit] },
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
// "Drucken an" (zielOptionen/zielAuswerten in api.js, auch fuers Handy).
// Vorausgewaehlt ist das Standardziel; eine eigene Wahl bleibt beim
// Neuladen der Seite stehen.
async function loadZiele() {
  const ziele = await get('/api/labels/ziele');
  const sel = $('#label-ziel');
  const vorher = sel.dataset.gewaehlt;
  const optionen = zielOptionen(ziele);
  sel.innerHTML = optionen.map(([w, t]) => `<option value="${esc(w)}">${esc(t)}</option>`).join('');
  sel.value = zielVorauswahl(optionen, ziele.standard, vorher);
}
$('#label-ziel').addEventListener('change', (ev) => { ev.target.dataset.gewaehlt = ev.target.value; });

$('#label-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = new FormData(ev.target);
  const body = Object.fromEntries(form.entries());
  body.quantity = parseFloat(body.quantity) || 1;
  body.count = parseInt(body.count, 10) || 1;
  body.print = form.get('print') === 'on';
  delete body.ziel_wahl;
  zielAuswerten(form.get('ziel_wahl'), body);
  try {
    const res = await post('/api/labels', body);
    toast(`${res.labels.length} Etikett(en): ${res.labels.join(', ')}`, 'success');
    const ziel = $('#label-ziel').value;
    ev.target.reset();
    $('#label-ziel').value = ziel;
    await Promise.all([loadLabels(), loadDashboard()]);
  } catch (e) { toast(e.message, 'error'); }
});

async function loadLabels() {
  const [roll, queue, settings] = await Promise.all([
    get('/api/labels/roll'), get('/api/labels/queue?limit=25'), get('/api/settings'),
  ]);
  state.settings = settings;
  fillSettings(settings);
  loadLayouts().catch((e) => toast(e.message, 'error'));
  hochformatHinweise().catch(() => {});
  loadBrother().catch((e) => { $('#brother-status').textContent = e.message; });
  loadZiele().catch(() => {});
  $('#roll-state').textContent = roll.size
    ? `${roll.remaining} von ${roll.size} Etiketten übrig (${roll.used} verbraucht)`
    : 'Keine Rollengröße hinterlegt.';
  $('#print-queue').innerHTML = queue.length
    ? queue.map((j) => `<tr><td class="mono">${j.id}</td><td class="mono">${esc(j.label)}${j.ziel === 'brother' ? ' <span class="pill">Brother</span>' : ''}</td>
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
// Brother am WLAN-Printserver (services/brother.py). Die Felder speichern sich
// selbst (data-setting); hier nur Zustand, Testdruck und Treiberdatei.
// Haushalt des Brother: ein Haushalt oder alle - dieselbe Auswahl wie bei
// den Terminals (Karte unter Terminals und Feld unter Etiketten).
function brotherHaushaltOptionen(haushalte, wert) {
  return [['', 'alle Haushalte'], ...haushalte.map((h) => [h.name, h.name])]
    .map(([w, t]) => `<option value="${esc(w)}" ${w === wert ? 'selected' : ''}>${esc(t)}</option>`).join('');
}

async function loadBrother() {
  const [z, haushalte] = await Promise.all([get('/api/brother'), get('/api/households')]);
  // Gehoert er einem anderen Haushalt, ist er in dieser Ansicht nicht da.
  $('#brother-setup').hidden = !z.sichtbar;
  if (!z.sichtbar) return;
  if ($('#brother-haushalt') !== document.activeElement) {
    $('#brother-haushalt').innerHTML = brotherHaushaltOptionen(haushalte, z.einstellungen.haushalt || '');
  }
  if ($('#brother-etikett') !== document.activeElement) {
    const wahl = z.etikett_wahl || z.einstellungen.etikett || '';
    $('#brother-etikett').innerHTML = (z.etikettensorten || []).map((e) =>
      `<option value="${esc(e.key)}" ${e.key === wahl ? 'selected' : ''}>${esc(e.name)}</option>`).join('');
  }
  brotherSorteZeigen($('#brother-etikett').value, z.profil);
  const ps = z.printserver;
  const medium = ps.medium || {};
  $('#brother-status').innerHTML = ps.erreichbar
    ? `<span>Printserver ${esc(ps.ip || '')} · Firmware ${esc(ps.firmware)} ·
        Drucker ${ps.drucker_verbunden ? esc(ps.drucker || 'verbunden') : '<b>nicht angeschlossen</b>'}
        ${medium.media_width_mm ? ` · Medium ${medium.media_width_mm}${medium.media_length_mm ? ` × ${medium.media_length_mm}` : ''} mm` : ''}
        ${ps.job_key ? '' : '<br><b>Printserver-Firmware zu alt</b> – ab 0.4.1 werden wiederholte Aufträge nicht doppelt gedruckt.'}</span>`
    : `<span>${esc(ps.fehler || 'nicht erreichbar')}</span>`;
  $('#brother-status').className = `notice ${ps.erreichbar ? '' : 'warn'}`;
  const p = z.profil;
  $('#brother-profil').innerHTML = `<span class="muted">${p.quelle === 'treiber'
    ? `Aus ${esc(p.datei || 'Treiberdatei')}: ${esc(p.medium || '')} ${p.breite_mm} × ${p.laenge_mm || '–'} mm,
       ${p.zeilen} Zeilen zu ${p.bytes} Bytes, Befehl ${esc(p.zeilenbefehl)}${p.tiff ? ', gepackt' : ''}.`
    : p.quelle === 'vorlage'
      ? `Etikettensorte ${esc(p.name)}: Befehle und Medieninformation aus dem Brother-Treiber,
         ${p.zeilen} Zeilen, ${p.druckbreite_pins} Punkte breit ab Punkt ${p.rand_links_pins}.`
      : 'Ohne Vorlage: Befehle nach der Brother-Rasterreferenz TD-2020.'}
    Gedruckt wird ${z.masse_mm.breite} × ${z.masse_mm.laenge} mm.
    ${z.sensor ? 'Gestanzte Etiketten: der Drucker setzt jedes Etikett selbst an der Lücke an.'
      : 'Endlosband: ohne Lückensensor, Länge wie eingestellt.'}</span>
    ${z.medium_hinweis ? `<div class="notice warn" style="margin-top:8px"><span><b>Rolle passt nicht:</b>
      ${esc(z.medium_hinweis)} – Etiketten werden so lange zurückgehalten.</span></div>` : ''}`;
  $('#brother-vorschau').src = z.vorschau || '';
  $('#brother-treiber-fig').hidden = !p.vorschau;
  if (p.vorschau) $('#brother-treiber').src = p.vorschau;
}
$('#brother-pruefen').addEventListener('click', () => loadBrother().catch((e) => toast(e.message, 'error')));

// Mit einer Etikettensorte (oder Treiberdatei) stehen Medium und Masse fest -
// die Felder dafuer gibt es nur bei "keine Vorlage".
function brotherSorteZeigen(wahl, profil) {
  const manuell = !wahl || wahl === 'keine';
  document.querySelectorAll('.brother-manuell').forEach((el) => { el.hidden = !manuell; });
  $('#brother-sorte-masse').hidden = manuell;
  if (!manuell && profil && profil.quelle !== 'standard') {
    const art = profil.laenge_mm ? 'Etiketten (gestanzt)' : 'Endlosband';
    $('#brother-sorte-masse-wert').textContent =
      `${profil.breite_mm} × ${profil.laenge_mm || '–'} mm · ${art}`;
  }
}
$('#brother-etikett').addEventListener('change', (ev) => {
  // Sofort umschalten; die Masse der neuen Sorte kommen mit dem naechsten Laden.
  brotherSorteZeigen(ev.target.value, null);
  $('#brother-sorte-masse-wert').textContent = '…';
});

// Geraeteeinstellungen im Drucker (ueber den Printserver). Jede Aenderung geht
// sofort hinaus - wie die anderen Felder auch.
function brotherGeraetZeigen(r) {
  $('#brother-geraet').innerHTML = r.einstellungen.map((e) => {
    const abweichend = e.empfohlen !== null && e.wert >= 0 && e.wert !== e.empfohlen;
    return `<label class="field">${esc(e.name)}
      <select data-brother-geraet="${esc(e.key)}">
        ${e.wert < 0 ? '<option value="" selected>– keine Antwort –</option>' : ''}
        ${e.optionen.map((o) => `<option value="${o.wert}" ${o.wert === e.wert ? 'selected' : ''}>${esc(o.text)}</option>`).join('')}
      </select>
      ${e.warum ? `<span class="hint">${abweichend ? '<b>Empfohlen:</b> ' : ''}${esc(e.warum)}</span>` : ''}</label>`;
  }).join('');
}
$('#brother-geraet-lesen').addEventListener('click', async (ev) => {
  // Nach dem await ist ev.currentTarget null - den Knopf vorher festhalten.
  const knopf = ev.currentTarget;
  knopf.disabled = true;
  try { brotherGeraetZeigen(await get('/api/brother/druckereinstellungen')); }
  catch (e) { toast(e.message, 'error'); }
  finally { knopf.disabled = false; }
});
document.addEventListener('change', async (ev) => {
  const sel = ev.target.closest('select[data-brother-geraet]');
  if (!sel || sel.value === '') return;
  try {
    brotherGeraetZeigen(await patch('/api/brother/druckereinstellungen',
      { [sel.dataset.brotherGeraet]: Number(sel.value) }));
    toast('Im Drucker gespeichert', 'success');
  } catch (e) { toast(e.message, 'error'); }
});
$('#brother-test').addEventListener('click', async () => {
  try {
    const r = await post('/api/brother/test');
    toast(r.wartet ? 'Ein Testetikett wartet schon – es kommt, sobald der Drucker bereit ist'
      : 'Testetikett an den Brother gesendet', 'success');
  }
  catch (e) { toast(e.message, 'error'); }
});
$('#brother-test-layouts').addEventListener('click', async () => {
  try {
    const r = await post('/api/brother/test/layouts');
    toast(r.wartet ? 'Ein Satz Layout-Tests wartet schon – er kommt, sobald der Drucker bereit ist'
      : `${r.anzahl} Musteretiketten an den Brother gesendet – je Layout eins`, 'success');
  }
  catch (e) { toast(e.message, 'error'); }
});
$('#brother-datei-laden').addEventListener('click', async () => {
  const datei = $('#brother-datei').files[0];
  if (!datei) return toast('Erst eine .prn-Datei wählen', 'warn');
  const form = new FormData();
  form.append('datei', datei);
  try {
    await post('/api/brother/treiberdatei', form);
    toast('Treiberdatei übernommen', 'success');
    await loadBrother();
  } catch (e) { toast(e.message, 'error'); }
});
$('#brother-datei-weg').addEventListener('click', async () => {
  try { await del('/api/brother/treiberdatei'); await loadBrother(); }
  catch (e) { toast(e.message, 'error'); }
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
      ${r.hinweis ? `<div class="notice warn" style="margin:0 0 10px"><span>${esc(r.hinweis)}</span></div>` : ''}
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
// Testknoepfe: je Befehlsvariante ein Etikett. Die Liste kommt vom Server
// (services/druckertest.py), damit Knopf und gedruckte Bytes nicht
// auseinanderlaufen.
async function loadDruckerTests() {
  const box = $('#drucker-tests-liste');
  if (!box || box.dataset.geladen) return;
  try {
    const tests = await get('/api/labels/tests');
    const gruppen = [...new Set(tests.map((t) => t.gruppe))];
    box.innerHTML = gruppen.map((g) => `
      <div style="margin:10px 0 4px"><b>${esc(g)}</b></div>
      <div class="row" style="flex-wrap:wrap;gap:8px">${tests.filter((t) => t.gruppe === g).map((t) => `
        <button class="ghost" data-druckertest="${esc(t.id)}" title="${esc(t.hinweis)}">
          ${esc(t.titel)}<br><span class="muted" style="font-size:11px">${esc(t.befehl)}</span>
        </button>`).join('')}</div>`).join('');
    box.dataset.geladen = '1';
  } catch (e) { box.textContent = e.message; }
}
$('#drucker-tests')?.addEventListener('toggle', (ev) => { if (ev.target.open) loadDruckerTests(); });
$('#drucker-tests-liste')?.addEventListener('click', async (ev) => {
  const knopf = ev.target.closest('[data-druckertest]');
  if (!knopf) return;
  const status = $('#drucker-tests-status');
  knopf.disabled = true;
  try {
    const r = await post(`/api/labels/tests/${encodeURIComponent(knopf.dataset.druckertest)}`);
    status.textContent = r.ok
      ? `Gesendet: ${knopf.title}`
      : `Nicht gedruckt: ${r.error || r.status}`;
    toast(r.ok ? 'Test gesendet' : (r.error || 'Nicht gedruckt'), r.ok ? 'success' : 'error');
  } catch (e) { toast(e.message, 'error'); }
  finally { knopf.disabled = false; }
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
    <td>${esc(t.category)}</td><td style="font-size:12px">${haltbarkeitText(
      t.haltbarkeit && Object.keys(t.haltbarkeit).length ? t.haltbarkeit : (t.shelf_days ? { kuehl: t.shelf_days } : {}))}</td>
    <td>${esc(t.unit)}</td>
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
    ...haltbarkeitFelder(tpl?.haltbarkeit && Object.keys(tpl.haltbarkeit).length
      ? tpl.haltbarkeit : (tpl?.shelf_days ? { kuehl: tpl.shelf_days } : {})).map((f) => ({
      ...f, label: `${f.label} – 0 = wie Kategorie` })),
    { name: 'unit', label: 'Einheit', value: tpl?.unit || '', options: EINHEITEN },
    { name: 'brands', label: 'Marken', type: 'tags', value: tpl?.brands || [], placeholder: 'Marke hinzufügen …' },
    { name: 'use_sorten', label: 'Sorte beim Einlagern abfragen (z. B. Fleischsorte) – steht dann auf dem Etikett: „Filet - Schwein“',
      value: tpl ? tpl.use_sorten : false, type: 'checkbox' },
    { name: 'sorten', label: 'Sorten zur Auswahl', type: 'tags', value: tpl?.sorten || [], placeholder: 'Sorte hinzufügen …' },
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
    // Eine neue Vorlage uebernimmt die Haltbarkeit der Kategorie als eigene
    // Werte (die Kategorie ist nur die Grundempfehlung) - bis man ein Feld
    // selbst aendert. Bestehende Vorlagen bleiben, wie sie sind.
    if (!tpl) {
      const felder = Object.keys(ARTEN).map((k) => body.querySelector(`[name="h_${k}"]`)).filter(Boolean);
      felder.forEach((f) => f.addEventListener('input', () => { f.dataset.vonHand = '1'; }));
      const uebernehmen = () => {
        const h = state.categories.find((c) => c.name === select.value)?.haltbarkeit || {};
        felder.forEach((f) => { if (!f.dataset.vonHand) f.value = h[f.name.slice(2)] || 0; });
      };
      select.addEventListener('change', uebernehmen);
      uebernehmen();
    }
  });
  if (!values) return;

  const body = {
    name: values.name,
    category: values.category,
    haltbarkeit: haltbarkeitAus(values),
    unit: values.unit,
    brands: values.brands,
    sorten: values.sorten,
    use_sorten: !!values.use_sorten,
    sort_order: tpl?.sort_order ?? 0,
  };
  if (tpl) await put(`/api/templates/${tpl.id}`, body);
  else await post('/api/templates', body);
  toast('Gespeichert', 'success');
  await loadTemplates();
}

// ------------------------------------------------------------------ Stammdaten
// Haushalte: anlegen, umbenennen, loeschen. Mit mehr als einem fragt die
// Startseite, in welchen man will; jeder hat seine Adresse /h/<id>/.
async function loadHaushalte() {
  const box = $('#haus-body');
  if (!box) return;
  state.haushalte = await get('/api/households');
  box.innerHTML = state.haushalte.map((h) => `<tr>
    <td class="name"><a href="/h/${h.id}/">${esc(h.name)}</a>${HAUSHALT && String(h.id) === HAUSHALT.id ? ' <span class="pill ok">hier</span>' : ''}</td>
    <td class="muted">${h.artikel} Artikel · ${h.terminals} Terminal(s)</td>
    <td class="right">
      <button class="sm" data-haus-ren="${h.id}">Umbenennen</button>
      <button class="sm ghost" data-haus-del="${h.id}">Löschen</button></td></tr>`).join('');
}
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('#haus-new,[data-haus-ren],[data-haus-del]');
  if (!b) return;
  try {
    if (b.id === 'haus-new') {
      const name = prompt('Name des neuen Haushalts');
      if (!name) return;
      await post('/api/households', { name });
    } else if (b.dataset.hausRen) {
      const h = state.haushalte.find((x) => String(x.id) === b.dataset.hausRen);
      const name = prompt('Neuer Name', h?.name || '');
      if (!name || name === h?.name) return;
      await patch(`/api/households/${b.dataset.hausRen}`, { name, sort_order: h.sort_order });
    } else if (b.dataset.hausDel) {
      if (!confirm('Haushalt löschen? Geht nur, wenn dort nichts mehr liegt.')) return;
      await del(`/api/households/${b.dataset.hausDel}`);
    }
    toast('Gespeichert', 'success');
    await loadHaushalte();
  } catch (e) { toast(e.message, 'error'); }
});

// Lagerarten - Schluessel wie im Server (services/haltbarkeit.ARTEN).
const ARTEN = { raum: 'Raumtemperatur', kuehl: 'Kühlschrank', tiefkuehl: 'Gefrierschrank' };
const ART_ICON = { raum: '🏠', kuehl: '❄️', tiefkuehl: '🧊' };
const artAusText = (text) => Object.keys(ARTEN).find((k) => ARTEN[k] === text) || 'raum';
const haltbarkeitText = (h) => Object.keys(ARTEN)
  .map((k) => `${ART_ICON[k]} ${h && h[k] ? `${h[k]} T` : '–'}`).join(' · ');
const haltbarkeitFelder = (h) => Object.keys(ARTEN).map((k) => ({
  name: `h_${k}`, label: `Tage im ${ARTEN[k] === 'Raumtemperatur' ? 'Vorrat (Raumtemperatur)' : ARTEN[k]}`,
  value: (h && h[k]) || 0, type: 'number',
}));
const haltbarkeitAus = (v) => Object.fromEntries(Object.keys(ARTEN).map((k) => [k, +v[`h_${k}`] || 0]));

async function kiStatusZeigen() {
  const el = $('#ki-status');
  if (!el) return;
  try {
    const k = await get('/api/ki');
    el.textContent = k.verfuegbar
      ? `eingerichtet (Modell ${k.modell}) · gefragt wird nur mit Produktname und Marke`
      : 'nicht eingerichtet: ANTHROPIC_API_KEY in der Umgebung des Containers setzen';
  } catch { el.textContent = ''; }
}

async function loadCatalogPage() {
  loadHaushalte().catch((e) => toast(e.message, 'error'));
  await loadCatalogData();
  $('#cat-body').innerHTML = state.categories.map((c) => `<tr>
    <td><span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}
      <div class="muted" style="font-size:12px;margin-top:4px" title="Haltbarkeit je Lagerart">
        ${haltbarkeitText(c.haltbarkeit)}</div>
      ${(c.subcategories || []).length ? `<div class="tag-list" style="margin-top:6px">${
        c.subcategories.map((s) => `<span class="tag">${esc(s)}</span>`).join('')}</div>` : ''}</td>
    <td class="right">
      <button class="sm" data-cat-edit="${c.id}">Ändern</button>
      <button class="sm danger" data-cat-del="${c.id}">Löschen</button></td></tr>`).join('');

  $('#loc-body').innerHTML = state.locations.map((l) => `<tr>
    <td><span class="swatch" style="background:${esc(l.color || '#1c222a')}"></span>${esc(l.name)}
      <span class="pill">${ART_ICON[l.kind] || ''} ${esc(ARTEN[l.kind] || 'ohne Lagerart')}</span>
      ${l.is_default ? '<span class="pill ok">Standard</span>' : ''}</td>
    <td class="right">
      <button class="sm" data-loc-edit="${l.id}">Ändern</button>
      <button class="sm danger" data-loc-del="${l.id}">Löschen</button></td></tr>`).join('');

  await loadProducts();
}

// Import / Export von Vorlagen und Stammdaten (services/stammdaten.py).
const sdTeile = () => [...document.querySelectorAll('#sd-teile input:checked')].map((c) => c.value).join(',');
const SD_NAMEN = { vorlagen: 'Vorlagen', kategorien: 'Kategorien', lagerorte: 'Lagerorte',
  haushalte: 'Haushalte', produkte: 'Produkt-Cache' };

$('#sd-export').addEventListener('click', () => {
  const teile = sdTeile();
  if (!teile) { toast('Mindestens einen Teil wählen', 'warn'); return; }
  const a = document.createElement('a');
  a.href = `/api/stammdaten/export?teile=${encodeURIComponent(teile)}`;
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
});

$('#sd-import-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const file = $('#sd-file').files[0];
  const teile = sdTeile();
  if (!file || !teile) { toast('Datei und mindestens einen Teil wählen', 'warn'); return; }
  const probe = !!ev.submitter?.dataset.probe;
  const modus = $('#sd-modus').value;
  if (!probe && modus === 'ueberschreiben'
      && !confirm('Vorhandene Einträge mit gleichem Namen werden mit den Werten aus der Datei überschrieben. Weiter?')) return;
  const body = new FormData();
  body.append('file', file);
  const box = $('#sd-result');
  box.innerHTML = '<span class="muted">Wird verarbeitet…</span>';
  try {
    const res = await fetch(`/api/stammdaten/import?teile=${encodeURIComponent(teile)}`
      + `&modus=${modus}&probe=${probe}`, { method: 'POST', body });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Fehler');
    const zeilen = Object.entries(data.teile);
    box.innerHTML = `<span class="pill ${probe ? '' : 'ok'}">${probe ? 'Prüflauf – nichts geändert' : 'Importiert'}</span>
      <div class="table-wrap" style="margin-top:8px"><table>
        <thead><tr><th></th><th class="right">neu</th><th class="right">geändert</th>
          <th class="right">unverändert</th><th class="right">übersprungen</th></tr></thead>
        <tbody>${zeilen.map(([t, z]) => `<tr><td>${esc(SD_NAMEN[t] || t)}</td><td class="right">${z.neu}</td>
          <td class="right">${z.geaendert}</td><td class="right">${z.gleich}</td>
          <td class="right">${z.uebersprungen}</td></tr>`).join('')}</tbody></table></div>
      ${modus === 'ergaenzen' && zeilen.some(([, z]) => z.uebersprungen)
    ? '<p class="muted" style="margin:6px 0 0">„übersprungen“: gibt es schon mit anderen Werten – zum Übernehmen „Vorhandenes überschreiben“ wählen.</p>' : ''}`;
    if (!probe) {
      toast('Stammdaten übernommen', 'success');
      await Promise.all([loadCatalogPage().catch(() => {}), loadTemplates().catch(() => {}),
        loadCatalogData().catch(() => {})]);
    }
  } catch (e) {
    box.innerHTML = '';
    toast(e.message, 'error');
  }
});

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
        ...haltbarkeitFelder(cat.haltbarkeit),
      ]);
      if (v) {
        await put(`/api/categories/${cat.id}`, {
          name: v.name, color: v.color, icon: cat.icon, sort_order: cat.sort_order,
          haltbarkeit: haltbarkeitAus(v),
        });
        await loadCatalogPage();
      }
    } else if (t.dataset.locDel) {
      if (!confirm('Lagerort löschen?')) return;
      await del(`/api/locations/${t.dataset.locDel}`); await loadCatalogPage();
    } else if (t.dataset.locEdit) {
      const loc = state.locations.find((l) => l.id === +t.dataset.locEdit);
      const v = await modal('Lagerort', [
        { name: 'name', label: 'Name', value: loc.name },
        { name: 'color', label: 'Farbe', value: loc.color || '#4c9eff', type: 'color' },
        { name: 'kind', label: 'Lagerart – bestimmt das MHD', value: ARTEN[loc.kind] || 'Raumtemperatur',
          options: Object.values(ARTEN) },
        { name: 'is_default', label: 'Standard', value: loc.is_default, type: 'checkbox' },
      ]);
      if (v) {
        await put(`/api/locations/${loc.id}`, { ...loc, ...v, kind: artAusText(v.kind), is_default: !!v.is_default });
        await loadCatalogPage();
      }
    } else if (t.dataset.prodDel) {
      await del(`/api/products/${t.dataset.prodDel}`); await loadProducts();
    }
  } catch (e) { toast(e.message, 'error'); }
});

$('#cat-new').addEventListener('click', async () => {
  const v = await modal('Neue Kategorie', [
    { name: 'name', label: 'Name', value: '' },
    { name: 'color', label: 'Farbe', value: '#1e88e5', type: 'color' },
    ...haltbarkeitFelder({}),
  ]);
  if (v) {
    await post('/api/categories', { name: v.name, color: v.color, icon: '', sort_order: state.categories.length,
      haltbarkeit: haltbarkeitAus(v) });
    await loadCatalogPage();
  }
});

$('#loc-new').addEventListener('click', async () => {
  const v = await modal('Neuer Lagerort', [
    { name: 'name', label: 'Name', value: '' },
    { name: 'color', label: 'Farbe', value: ['#4c9eff', '#26c6da', '#8d6e63', '#7e57c2', '#5c6bc0',
      '#ec407a', '#78909c', '#26a69a'][state.locations.length % 8], type: 'color' },
    { name: 'kind', label: 'Lagerart – bestimmt das MHD', value: 'Raumtemperatur', options: Object.values(ARTEN) },
    { name: 'is_default', label: 'Standard', value: false, type: 'checkbox' },
  ]);
  if (v) {
    await post('/api/locations', { ...v, kind: artAusText(v.kind), is_default: !!v.is_default,
      sort_order: state.locations.length });
    await loadCatalogPage();
  }
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

// Warum ein Terminal stumm ist - meldet die Firmware im hello ("audio").
function tonHinweis(t) {
  const a = t.audio;
  if (!a || a === 'ok') return '';
  const grund = {
    abgeschaltet: 'Auf dieser Boardvariante ist der Ton in der Firmware abgeschaltet – die Belegung '
      + 'des Audiotakts (MCLK) ist nicht dokumentiert.',
    'kein Codec': 'Der Audiocodec (ES8311) meldet sich nicht.',
    'I2S-Fehler': 'Die Audioschnittstelle (I2S) ließ sich nicht einrichten.',
  }[a] || `Ton: ${a}`;
  return `<div class="notice warn" style="margin-top:8px"><span><b>Kein Ton.</b> ${esc(grund)}</span></div>`;
}

function deviceCard(d) {
  const t = d.telemetry || {};
  const scanner = t.scanner || {};
  const kopf = `<div class="row"><h2 style="margin:0;flex:1">
        <span class="dot ${d.online ? 'on' : ''}"></span>${esc(d.name || d.device_id)}</h2>
        ${(d.telemetry || {}).board ? `<span class="pill">Board ${esc(String(d.telemetry.board).toUpperCase())}</span>` : ''}
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
        <div><b>${esc(scanner.state_text || (scanner.connected ? 'verbunden' : 'nicht verbunden'))}</b><span>Handscanner${
          scanner.connected && scanner.setup_ms ? ` · gekoppelt in ${(scanner.setup_ms / 1000).toLocaleString('de-DE', { maximumFractionDigits: 1 })} s` : ''}</span></div>
        <div><b>${scanner.battery >= 0 ? scanner.battery + ' %' : '–'}</b><span>Scanner-Akku</span></div>
        <div><b>${wlanText(t.rssi)}</b><span>WLAN</span></div>
        <div><b>${t.uptime ? Math.floor(t.uptime / 3600) + ' h' : '–'}</b><span>Läuft seit</span></div>
        <div><b>${t.heap ? Math.round(t.heap / 1024) + ' K' : '–'}</b><span>Speicher frei</span></div>
        <div><b>${t.min_heap ? Math.round(t.min_heap / 1024) + ' K' : '–'}</b><span>Speicher min.</span></div>
      </div>
      ${!scanner.connected && scanner.reason_text ? `<div class="muted" style="margin-top:8px">
        Scanner zuletzt getrennt: ${esc(scanner.reason_text)}</div>` : ''}
      ${tonHinweis(t)}
      <label class="field" style="margin-top:12px">Haushalt
        <select data-dev-haus="${esc(d.device_id)}">
          ${(state.haushalte || []).map((h) => `<option ${h.name === d.household ? 'selected' : ''}>${esc(h.name)}</option>`).join('')}
        </select></label>
      <label class="field" style="margin-top:8px">Lagert ein in
        <select data-dev-loc="${esc(d.device_id)}">
          ${state.locations.map((l) => `<option ${l.name === d.active_location ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}
        </select></label>
      ${usbFelder(d)}
      <div class="row" style="margin-top:10px">
        <button class="sm" data-dev-beep="${esc(d.device_id)}">Ton</button>
        <button class="sm" data-dev-update="${esc(d.device_id)}"${updateHint(d)}>Firmware-Update</button>
        <button class="sm" data-dev-reboot="${esc(d.device_id)}">Neustart</button>
      </div></div>`;
}

// Was am Terminal haengt: Drucker (keiner / seriell / Brother per USB) und
// ein USB-Barcodescanner. Beides am USB-Anschluss braucht eine Firmware mit
// "usb" - sonst ein Hinweis statt stiller Wirkungslosigkeit.
const DRUCKER = [['seriell', 'Seriell (ESC/POS, wie bisher)'], ['usb', 'Brother per USB'], ['keiner', 'Kein Drucker']];
function usbFelder(d) {
  const t = d.telemetry || {};
  const usb = t.usb || {};
  const caps = t.caps || [];
  const braucht = d.drucker === 'usb' || d.usb_scanner;
  const angeschlossen = usb.geraet
    ? `${usb.geraet === 'drucker' ? 'Drucker' : usb.geraet === 'scanner' ? 'Scanner' : 'Gerät'}${usb.name ? ` – ${esc(usb.name)}` : ''}`
    : 'nichts angeschlossen';
  return `<label class="field" style="margin-top:8px">Drucker
      <select data-dev-drucker="${esc(d.device_id)}">
        ${DRUCKER.map(([w, n]) => `<option value="${w}" ${w === (d.drucker || 'seriell') ? 'selected' : ''}>${n}</option>`).join('')}
      </select></label>
    <label class="row tight" style="margin-top:8px"><input type="checkbox" data-dev-usbscanner="${esc(d.device_id)}"
      ${d.usb_scanner ? 'checked' : ''}> Barcodescanner am USB-Anschluss</label>
    ${braucht ? (caps.includes('usb')
    ? `<div class="muted" style="margin-top:6px">USB: ${usb.aktiv ? angeschlossen : 'wird eingeschaltet…'}</div>`
    : '<div class="notice" style="margin-top:8px"><span>Für USB braucht das Terminal eine neuere Firmware – „Firmware-Update“.</span></div>') : ''}
    ${!braucht && usb.aktiv ? '<div class="muted" style="margin-top:6px">USB ist noch an – aus erst nach einem Neustart des Terminals.</div>' : ''}`;
}

document.addEventListener('change', async (ev) => {
  const sel = ev.target.closest('select[data-dev-drucker],input[data-dev-usbscanner]');
  if (!sel) return;
  try {
    const body = sel.dataset.devDrucker ? { drucker: sel.value } : { usb_scanner: sel.checked };
    await patch(`/api/devices/${sel.dataset.devDrucker || sel.dataset.devUsbscanner}`, body);
    toast('Gespeichert', 'success');
    await loadDevices();
  } catch (e) { toast(e.message, 'error'); }
});

// Der Brother am WLAN-Printserver als Geraet neben den Terminals - mit
// demselben Haushalt-Feld. Eingestellt wird er unter Etiketten.
function brotherCard(b) {
  const ps = b.printserver || {};
  const m = ps.medium || {};
  return `<div class="card">
    <div class="row"><h2 style="margin:0;flex:1"><span class="dot ${ps.erreichbar ? 'on' : ''}"></span>Brother-Etikettendrucker</h2>
      <span class="pill">WLAN-Printserver</span>${ps.firmware ? `<span class="pill">Firmware ${esc(ps.firmware)}</span>` : ''}</div>
    <div class="muted">${esc(b.url)}${b.aktiv ? '' : ' · <b>nicht aktiv</b>'}</div>
    ${ps.erreichbar ? `<div class="telemetry">
        <div><b>${ps.drucker_verbunden ? esc(ps.drucker || 'verbunden') : 'nicht angeschlossen'}</b><span>Drucker</span></div>
        <div><b>${m.media_width_mm ? `${m.media_width_mm}${m.media_length_mm ? ` × ${m.media_length_mm}` : ''} mm` : '–'}</b><span>Medium</span></div>
        <div><b>${(ps.warteschlange || {}).queued ?? 0}</b><span>wartend</span></div>
      </div>` : `<div class="notice warn" style="margin-top:12px"><span>${esc(ps.fehler || 'nicht erreichbar')}</span></div>`}
    <label class="field" style="margin-top:12px">Haushalt
      <select data-brother-haus>${brotherHaushaltOptionen(state.haushalte || [], b.haushalt || '')}</select></label>
    <div class="row" style="margin-top:10px">
      <button class="sm" data-brother-einstellen>Einstellungen</button>
    </div></div>`;
}

async function loadDevices() {
  let brotherGeraet;
  [state.devices, state.haushalte, brotherGeraet] = await Promise.all([
    get('/api/devices'), get('/api/households'),
    get('/api/brother/geraet').catch(() => ({ sichtbar: false }))]);
  const brotherHtml = brotherGeraet.sichtbar ? brotherCard(brotherGeraet) : '';
  // Die Anleitung bleibt erreichbar, wenn schon Terminals da sind - fuer das
  // zweite oder ein ersetztes Geraet.
  $('#devices-list').innerHTML = brotherHtml + (state.devices.length
    ? state.devices.map(deviceCard).join('')
      + `<details class="card" style="grid-column:1/-1"><summary><h2 style="display:inline">Weiteres Terminal einrichten</h2></summary>
          ${terminalEinrichten(true)}</details>`
    : terminalEinrichten());

  const sim = $('#sim-card');
  if (sim) sim.hidden = !state.devices.some((d) => d.online);
  $('#sim-device').innerHTML = state.devices.filter((d) => d.online).map((d) =>
    `<option value="${esc(d.device_id)}">${esc(d.name || d.device_id)}</option>`).join('');
}

document.addEventListener('change', async (ev) => {
  const sel = ev.target.closest('select[data-brother-haus]');
  if (!sel) return;
  try {
    await patch('/api/settings/brother', { haushalt: sel.value });
    toast(sel.value ? `Brother gehört jetzt zu ${sel.value}` : 'Brother gehört jetzt allen Haushalten', 'success');
    await loadDevices();
  } catch (e) { toast(e.message, 'error'); }
});
document.addEventListener('click', (ev) => {
  if (ev.target.closest('button[data-brother-einstellen]')) {
    showPage('labels');
    setTimeout(() => $('#brother-setup')?.scrollIntoView({ behavior: 'smooth' }), 150);
  }
});

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
  const sel = ev.target.closest('select[data-dev-haus]');
  if (!sel) return;
  await patch(`/api/devices/${sel.dataset.devHaus}`, { household: sel.value });
  toast(`Terminal gehört jetzt zu „${sel.value}“`, 'success');
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
  kiStatusZeigen().catch(() => {});
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

// Gefuellt wird, was im Aufbau steht - nicht, was das Skript erwartet. Aufbau
// und Skript kommen nie gleichzeitig im Browser an; frueher riss ein einziges
// entferntes Feld ($('#weg').value = x wirft) den Rest der Maske mit, und die
// halbe Etikettenmaske blieb nach einem Update ohne Fehlermeldung leer.
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
    await loadLayouts().catch((e) => toast(e.message, 'error'));
    hochformatHinweise().catch(() => {});
  }
  // Sorte, Layout, Drehung: Vorschau und Masse der Brother-Karte neu holen.
  if (ok && bereich === 'brother') loadBrother().catch((e) => toast(e.message, 'error'));
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

  // Es gibt nur noch die eigene Aufteilung (settings_store.FEST) - hier
  // steht nur ihre Vorschau, ohne Auswahl.
  box.innerHTML = rows.filter((l) => l.name === chosen).map((l) => `<div class="layout ${l.passt ? '' : 'zu-gross'}">
    <div class="paper">${l.svg}</div>
    <div class="fill">${l.passt
      ? `passt · braucht ${fmtMm(l.mm)} mm`
      : `<b>passt nicht</b> · braucht ${fmtMm(l.mm)} mm`}</div>
    ${(l.weggelassen || []).length ? `<div class="fill" style="color:var(--warn,#cc9218)">
      kein Platz für: ${esc(l.weggelassen.join(', '))}</div>` : ''}
  </div>`).join('');
}

function fmtMm(mm) {
  return Number(mm).toLocaleString('de-DE', { maximumFractionDigits: 1 });
}

async function chooseLayout(el) {
  const ok = await saveSetting(el, 'printer', { label_layout: el.dataset.layout });
  if (ok) { await loadLayouts(); await hochformatHinweise(); }
}

// Eigene Aufteilung: Zeilen auswaehlen, sortieren, Stil setzen. Jede
// Aenderung wird sofort gespeichert und die Vorschau neu gezeichnet - wie
// alle anderen Einstellungen hier, ohne Speichern-Knopf.
const eigen = { felder: {}, stile: {}, zeilen: [] };
async function eigenLaden() {
  const r = await get('/api/labels/eigen');
  Object.assign(eigen, r);
  eigenZeichnen();
}
function eigenZeichnen() {
  const box = $('#eigen-zeilen');
  if (!box) return;
  const opt = (map, wert) => Object.entries(map)
    .map(([k, v]) => `<option value="${esc(k)}" ${k === wert ? 'selected' : ''}>${esc(v)}</option>`).join('');
  box.innerHTML = eigen.zeilen.map((z, i) => {
    const ohneStil = z.feld === 'linie' || z.feld === 'code';
    return `<div class="row" data-eigen="${i}" style="gap:6px;margin:4px 0;align-items:center;flex-wrap:wrap">
      <select data-eigen-feld>${opt(eigen.felder, z.feld)}</select>
      <select data-eigen-stil style="${ohneStil ? 'display:none' : ''}">${opt(eigen.stile, z.stil || 'zeile')}</select>
      <label class="row tight" style="${ohneStil || (z.stil || 'zeile') !== 'zeile' ? 'display:none' : ''}">
        <input type="checkbox" data-eigen-unter ${z.unterstrichen ? 'checked' : ''}> unterstrichen</label>
      <button class="ghost" data-eigen-auf title="nach oben" ${i === 0 ? 'disabled' : ''}>↑</button>
      <button class="ghost" data-eigen-ab title="nach unten" ${i === eigen.zeilen.length - 1 ? 'disabled' : ''}>↓</button>
      <button class="ghost" data-eigen-weg title="entfernen">✕</button>
    </div>`;
  }).join('');
}
async function eigenSpeichern() {
  eigenZeichnen();
  const ok = await saveSetting($('#eigen-zeilen'), 'printer', { label_custom: eigen.zeilen });
  if (ok) await loadLayouts();
}
$('#eigen-zeilen')?.addEventListener('change', (ev) => {
  const zeile = ev.target.closest('[data-eigen]');
  if (!zeile) return;
  ev.stopPropagation();
  const z = eigen.zeilen[Number(zeile.dataset.eigen)];
  if (ev.target.matches('[data-eigen-feld]')) z.feld = ev.target.value;
  if (ev.target.matches('[data-eigen-stil]')) z.stil = ev.target.value;
  if (ev.target.matches('[data-eigen-unter]')) z.unterstrichen = ev.target.checked;
  eigenSpeichern();
});
$('#eigen-zeilen')?.addEventListener('click', (ev) => {
  const knopf = ev.target.closest('button');
  const zeile = ev.target.closest('[data-eigen]');
  if (!knopf || !zeile) return;
  const i = Number(zeile.dataset.eigen);
  const z = eigen.zeilen;
  if (knopf.matches('[data-eigen-auf]') && i > 0) [z[i - 1], z[i]] = [z[i], z[i - 1]];
  else if (knopf.matches('[data-eigen-ab]') && i < z.length - 1) [z[i + 1], z[i]] = [z[i], z[i + 1]];
  else if (knopf.matches('[data-eigen-weg]')) z.splice(i, 1);
  else return;
  eigenSpeichern();
});
$('#eigen-neu')?.addEventListener('click', () => {
  eigen.zeilen.push({ feld: 'location', stil: 'zeile' });
  eigenSpeichern();
});
$('#eigen-reset')?.addEventListener('click', () => {
  eigen.zeilen = [];
  saveSetting($('#eigen-zeilen'), 'printer', { label_custom: [] })
    .then(() => eigenLaden()).then(() => loadLayouts());
});

// Papier je Etikett im Ablauf "Rueckzug -> Etikett -> Vorschub". Kommt aus
// derselben Rechnung, mit der gedruckt wird (labels.ablauf_einfach). Steht
// nicht genau der Etikettenabstand da, wandert jedes Etikett ein Stueck -
// nach ein paar Etiketten druckt er in die Luecke, und der Rueckzug zieht
// die Kante unter die Walze. Genau das klang nach Durchdrehen.
const mmText = (v) => Number(v).toLocaleString('de-DE', { maximumFractionDigits: 3 });
async function ablaufZeigen() {
  const box = $('#ablauf-box');
  if (!box) return;
  const a = await get('/api/labels/ablauf');
  // Nur zum Ablesen: wie viel Papier ein Etikett verbraucht. Stimmt die
  // Zahl nicht mit dem Abstand der Rolle, wandert jedes Etikett - dann am
  // Nachlauf oder Rueckzug drehen.
  let text = `Höhe ${mmText(a.hoehe)} + Nachlauf ${mmText(a.vorschub)} − Rückzug ${mmText(a.rueckzug)}
    = <b>${mmText(a.papier)} mm Papier je Etikett</b> – soll so groß sein wie der Abstand von
    Etikettanfang zu Etikettanfang auf der Rolle.`;
  box.className = 'notice';
  if (a.zu_lang > 0) {
    box.className = 'notice warn';
    text += `<br>Die Höhe ist ${mmText(a.zu_lang)} mm zu lang – das Etikett liefe aufs nächste.`;
  }
  box.innerHTML = text;
  box.style.display = 'block';
}

// Hochformat wird als Bild gedruckt. Dafuer gibt es eigene Einstellungen,
// das Einzeln-Drehen der Buchstaben wirkt dort nicht - und ein Terminal mit
// aelterer Firmware kann es gar nicht. Das soll man sehen, bevor die ersten
// Auftraege mit "Firmware zu alt" in der Warteschlange stehen.
async function hochformatHinweise() {
  const drucker = state.settings?.printer || {};
  const hoch = drucker.label_layout === 'hochformat';
  $$('[data-nur-hochformat]').forEach((el) => { el.hidden = !hoch; });
  const eigen = drucker.label_layout === 'eigen';
  $$('[data-nur-eigen]').forEach((el) => { el.hidden = !eigen; });
  if (eigen) eigenLaden().catch(() => {});

  // Ablauf "Rueckzug -> Etikett -> Vorschub": eigene Felder und die
  // Nachrechnung, wie viel Papier ein Etikett verbraucht.
  const einfach = drucker.label_sequence === 'einfach';
  $$('[data-nur-einfach]').forEach((el) => { el.hidden = !einfach; });
  const hoeheHint = $('#hoehe-hint');
  if (hoeheHint) {
    hoeheHint.textContent = einfach
      ? 'bedruckte Länge ab dem Rückzug – der Rest bis zum nächsten Etikett ist „Vorschub danach“'
      : 'von einem Etikettanfang zum nächsten – Etikett + Lücke';
  }
  if (einfach) ablaufZeigen().catch(() => {});

  // Was die eingestellten Etiketten vom Terminal verlangen - genau das, was
  // der Server vor dem Senden prueft (workflow.flush_print_queue).
  const baud = Number(drucker.baud || 9600);
  const noetig = [];
  if (hoch) noetig.push('raster2');
  if (baud !== 9600) noetig.push('baud');

  const box = $('#hochformat-firmware');
  if (!box) return;
  let alte = [];
  if (noetig.length) {
    try {
      const geraete = await get('/api/devices');
      alte = geraete.filter((d) => d.online
        && noetig.some((n) => !((d.telemetry || {}).caps || []).includes(n)));
    } catch { /* Hinweis ist Beiwerk */ }
  }
  box.hidden = !alte.length;
  if (alte.length) {
    const was = [hoch && 'kein Hochformat', baud !== 9600 && `nicht mit ${baud} Baud`]
      .filter(Boolean).join(' und ');
    $('#hochformat-firmware-text').textContent =
      `${alte.map((d) => d.name || d.device_id).join(', ')}: Firmware kann noch ${was} drucken. `
      + 'Unter Terminals zuerst „Aus GitHub-Release holen“ und „Firmware-Update“ – '
      + 'bis dahin scheitern solche Etiketten mit einer Meldung in der Warteschlange.';
  }
}
$('#layout-picker')?.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-layout]');
  if (el) chooseLayout(el);
});
$('#layout-picker')?.addEventListener('keydown', (ev) => {
  const el = ev.target.closest('[data-layout]');
  if (el && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); chooseLayout(el); }
});

// ------------------------------------------------- MHD-Erkennung testen
// Mehrere Fotos auf einmal durch die Texterkennung des Containers schicken -
// mit allen Zwischenschritten: jede gelesene Zeile, jedes gefundene Datum und
// welches davon gewaehlt wurde. Zum Nachvollziehen, warum ein Aufdruck nicht
// erkannt wird, und als Material zum Nachschaerfen der Regeln.
// Je Ergebnis auch das Bild, das an den Server ging (verkleinertes Foto bzw.
// Rahmenausschnitt) - fuer den ZIP-Download, nicht fuer die JSON-Datei.
const mhdTest = { ergebnisse: [], bilder: [] };

async function mhdTestStatus() {
  const el = $('#mhd-test-status');
  try {
    const w = await get('/api/mhd');
    el.textContent = `Texterkennung im Container: ${w.lokal ? 'bereit' : 'nicht installiert'}`
      + ` · KI: ${w.ki ? 'eingerichtet' : 'nicht eingerichtet'}`;
    $('#mhd-test-ki').disabled = !w.ki;
  } catch (e) { el.textContent = e.message; }
}

function mhdTestBild(datei) {
  // Wie am Handy: hoechstens 1600 Pixel, JPEG - mehr liest der Server nicht.
  return createImageBitmap(datei).then((bild) => {
    const f = Math.min(1, 1600 / Math.max(bild.width, bild.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bild.width * f);
    c.height = Math.round(bild.height * f);
    c.getContext('2d').drawImage(bild, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.9).split(',')[1];
  });
}

const mhdDatum = (iso) => (iso ? iso.split('-').reverse().join('.') : '–');

function mhdTestKarte(name, url, r) {
  const l = r.lokal || {};
  const gewaehlt = new Set((l.kandidaten || []).slice(0, 1).filter((k) => !k.kein_mhd).map((k) => k.zeile));
  const markiert = new Set((l.kandidaten || []).map((k) => k.zeile));
  const zeilen = (l.texte || []).map((t, i) => `<tr${gewaehlt.has(i) ? ' style="font-weight:600"' : ''}>
      <td class="mono">${esc(t.text)}</td><td class="right muted">${Math.round(t.sicher * 100)} %</td>
      <td>${markiert.has(i) ? (gewaehlt.has(i) ? '✔ gewählt' : 'Datum') : ''}</td></tr>`).join('');
  const kand = (l.kandidaten || []).map((k, i) => {
    const fett = i === 0 && !k.kein_mhd;
    return `<li>${fett ? '<b>' : ''}${mhdDatum(k.datum)}${fett ? '</b>' : ''}
      <span class="muted">aus „${esc(k.gelesen)}“, Zeile ${k.zeile + 1}${k.hinweis ? ', mit Hinweiswort' : ''}${k.kein_mhd ? ', kein MHD (Einfrier-/Herstelldatum)' : ''}</span></li>`;
  }).join('');
  const ki = r.ki ? `<div>KI: <b>${mhdDatum(r.ki.datum)}</b>
      <span class="muted">${r.ki.gelesen ? `„${esc(r.ki.gelesen)}“ · ` : ''}${r.ki.ms} ms${r.ki.fehler ? ` · ${esc(r.ki.fehler)}` : ''}</span></div>` : '';
  const lokalText = l.fehler ? `<span style="color:var(--danger)">${esc(l.fehler)}</span>`
    : r.lokal === null ? '<span class="muted">Texterkennung nicht installiert</span>'
      : `<b style="color:${l.datum ? 'var(--success)' : 'var(--danger)'}">${l.datum ? mhdDatum(l.datum) : 'kein Datum'}</b>
        <span class="muted">${l.gelesen ? `„${esc(l.gelesen)}“ · ` : ''}${l.ms} ms · ${(l.groesse || []).join('×')} px</span>
        ${(l.durchgaenge || []).length ? `<div class="muted" style="font-size:12px">Durchgänge: ${
    l.durchgaenge.map((d) => (d.datum ? `<b>${esc(d.name)} ✔</b>` : esc(d.name))).join(' → ')}</div>` : ''}`;
  return `<div class="row" style="align-items:flex-start;gap:12px;border-top:1px solid var(--border);padding-top:10px">
    <img src="${url}" alt="" style="width:120px;max-height:160px;object-fit:contain;border-radius:8px;background:#000">
    <div style="flex:1;min-width:0">
      <div class="muted" style="font-size:12px">${esc(name)}</div>
      <div>Lokal: ${lokalText}</div>${ki}
      ${kand ? `<ul style="margin:6px 0 0 18px;padding:0">${kand}</ul>` : ''}
      ${zeilen ? `<details style="margin-top:6px"><summary class="muted">${(l.texte || []).length} gelesene Zeilen</summary>
        <div class="table-wrap"><table><tbody>${zeilen}</tbody></table></div></details>` : ''}
    </div></div>`;
}

$('#mhd-test-dateien').addEventListener('change', async (ev) => {
  const dateien = [...ev.target.files];
  ev.target.value = '';
  const liste = $('#mhd-test-liste');
  const mitKi = $('#mhd-test-ki').checked;
  for (const datei of dateien) {
    const url = URL.createObjectURL(datei);
    const platz = document.createElement('div');
    platz.innerHTML = `<div class="muted">${esc(datei.name)} wird gelesen …</div>`;
    liste.prepend(platz);
    try {
      const bild = await mhdTestBild(datei);
      const r = await post('/api/mhd/test', { bild, typ: 'image/jpeg', ki: mitKi, gruendlich: true });
      platz.innerHTML = mhdTestKarte(datei.name, url, r);
      mhdTestMerken(datei.name, bild, r);
    } catch (e) {
      platz.innerHTML = `<div style="color:var(--danger)">${esc(datei.name)}: ${esc(e.message)}</div>`;
    }
    $('#mhd-test-json').disabled = $('#mhd-test-leeren').disabled = !mhdTest.ergebnisse.length;
  }
});

function mhdTestMerken(name, bild, r) {
  const nr = String(mhdTest.ergebnisse.length + 1).padStart(2, '0');
  const sauber = name.replace(/\.[a-z0-9]+$/i, '').replace(/[^\w.-]+/g, '_').replace(/_+/g, '_').slice(0, 40);
  const bildDatei = `bilder/${nr}-${sauber}.jpg`;
  mhdTest.ergebnisse.push({ datei: name, bild: bildDatei, ...r });
  mhdTest.bilder.push({ name: bildDatei, b64: bild });
  $('#mhd-test-json').disabled = $('#mhd-test-leeren').disabled = false;
}

function mhdTestHinzu(name, bild, r) {
  const platz = document.createElement('div');
  platz.innerHTML = mhdTestKarte(name, `data:image/jpeg;base64,${bild}`, r);
  $('#mhd-test-liste').prepend(platz);
  mhdTestMerken(name, bild, r);
}

$('#mhd-test-kamera').addEventListener('click', async () => {
  if (!navigator.mediaDevices?.getUserMedia) {
    toast('Live-Kamera braucht HTTPS (oder localhost) – Fotos gehen trotzdem', 'warn');
    return;
  }
  let n = 0;
  try {
    // KI wie in der Praxis: nur wenn angehakt, und dann einmal nach 6 s ohne Treffer.
    await mhdKamera({
      test: true,
      kiErlaubt: $('#mhd-test-ki').checked,
      beiBild: (bild, r) => {
        n += 1;
        mhdTestHinzu(`Live ${new Date().toLocaleTimeString('de-DE')} · Bild ${n}`, bild, r);
      },
    });
  } catch (e) { toast(e.message, 'warn'); }
});

// Eine ZIP-Datei mit ergebnisse.json und den Bildern, so wie der Server sie
// bekam - zum Weitergeben, damit sich ein Fehlgriff nachstellen laesst.
$('#mhd-test-json').addEventListener('click', () => {
  const blob = zipBlob([
    { name: 'ergebnisse.json', daten: JSON.stringify(mhdTest.ergebnisse, null, 2) },
    ...mhdTest.bilder.map((b) => ({ name: b.name, daten: base64ZuBytes(b.b64) })),
  ]);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `mhd-test-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.zip`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
});

$('#mhd-test-leeren').addEventListener('click', () => {
  mhdTest.ergebnisse = [];
  mhdTest.bilder = [];
  $('#mhd-test-liste').innerHTML = '';
  $('#mhd-test-json').disabled = $('#mhd-test-leeren').disabled = true;
});

$('#mhd-test').addEventListener('toggle', (ev) => { if (ev.target.open) mhdTestStatus(); });

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
      devices: ['devices', 'dashboard', 'labels'],
      settings: ['system', 'labels', 'devices'],
      labels: ['labels'],
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
