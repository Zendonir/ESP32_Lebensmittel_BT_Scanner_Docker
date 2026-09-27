// Schmaler Zugriff auf die REST-API. Eine Stelle fuer Fehlerbehandlung.

export async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 204) return null;

  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }

  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText || 'Fehler';
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

export const get = (p) => api(p);
export const post = (p, body) => api(p, { method: 'POST', body });
export const patch = (p, body) => api(p, { method: 'PATCH', body });
export const put = (p, body) => api(p, { method: 'PUT', body });
export const del = (p) => api(p, { method: 'DELETE' });

// ---------------------------------------------------------------- Hilfsmittel
// `aktion` haengt einen Knopf an die Meldung, etwa "Rueckgaengig". Solche
// Meldungen bleiben laenger stehen - wer den Knopf braucht, muss ihn noch
// erreichen koennen, nachdem er begriffen hat, was gerade passiert ist.
export function toast(text, level = 'info', aktion = null) {
  const host = document.getElementById('toasts');
  if (!host) return;
  host.setAttribute('aria-live', 'polite');
  const node = document.createElement('div');
  node.className = `toast ${level}`;
  const span = document.createElement('span');
  span.textContent = text;
  node.appendChild(span);
  if (aktion) {
    const btn = document.createElement('button');
    btn.className = 'sm';
    btn.textContent = aktion.label;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await aktion.run(); } finally { node.remove(); }
    });
    node.appendChild(btn);
  }
  host.appendChild(node);
  setTimeout(() => node.remove(), aktion ? 9000 : 4200);
}

// Auslagern mit Rueckweg. Im Web und am Handy war das ein Klick bzw. ein
// Wischer ohne Rueckfrage und ohne Rueckweg: der Artikel verschwand aus der
// Liste, und zurueckholen liess er sich nur, wenn man wusste, dass es unter
// "Ausgelagert" einen Knopf dafuer gibt. Am Terminal bucht ein zweiter Scan
// desselben Etiketts zurueck - hier uebernimmt das der Knopf in der Meldung.
export async function auslagern(label, reason, nachher = () => {}) {
  const item = await post('/api/inventory/remove', { label, reason });
  toast(`„${item.display_name || item.name}“ ausgelagert`, 'success', {
    label: 'Rückgängig',
    run: async () => {
      try {
        await post('/api/inventory/restore', { label });
        toast('Zurückgebucht', 'success');
      } catch (e) { toast(e.message, 'error'); }
      await nachher();
    },
  });
  return item;
}

export function fmtDate(iso) {
  if (!iso) return '–';
  const [y, m, d] = iso.split('-');
  return d ? `${d}.${m}.${y}` : iso;
}

export function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// Restlaufzeit in Worten. "3 T über" und "540 T" liessen rechnen: ob 540
// Tage ein halbes oder anderthalb Jahre sind, sieht man nicht auf einen Blick.
export function daysText(days) {
  if (days === null || days === undefined) return 'kein MHD';
  if (days < 0) return Math.abs(days) === 1 ? '1 Tag drüber' : `${Math.abs(days)} Tage drüber`;
  if (days === 0) return 'heute';
  if (days === 1) return 'morgen';
  if (days <= 60) return `${days} Tage`;
  if (days <= 730) {
    const m = Math.round(days / 30.44);
    return m === 1 ? '1 Monat' : `${m} Monate`;
  }
  const j = Math.round((days / 365.25) * 2) / 2;
  return `${j.toLocaleString('de-DE')} Jahre`;
}

export function daysPill(days) {
  const cls = days === null || days === undefined ? ''
    : days <= 0 ? 'danger' : days <= 3 ? 'warn' : 'ok';
  return `<span class="pill ${cls}">${daysText(days)}</span>`;
}

// ------------------------------------------------------- Ereignisse lesbar
// Im Protokoll standen die internen Kennungen ("add", "scan_unknown") und die
// Rohdaten als JSON. Beides ist fuer die Fehlersuche da, nicht zum Lesen.
const EVENT_NAMES = {
  add: 'Eingelagert',
  remove: 'Ausgelagert',
  restore: 'Zurückgebucht',
  edit: 'Geändert',
  delete: 'Gelöscht',
  print: 'Nachgedruckt',
  scan_unknown: 'Unbekannter Barcode',
  import: 'Übernommen',
  notify: 'Benachrichtigung',
  device: 'Terminal',
  rename_category: 'Kategorie umbenannt',
  rename_location: 'Lagerort umbenannt',
};

export function eventName(type) {
  return EVENT_NAMES[type] || type;
}

const REASONS = {
  scan: 'per Scan', web: 'im Web', mobile: 'am Handy', manual: 'von Hand',
  expired: 'abgelaufen', import: 'beim Import',
};

const FIELD_NAMES = {
  name: 'Name', brand: 'Marke', category: 'Kategorie', subcategory: 'Sorte',
  location: 'Ort', note: 'Notiz', unit: 'Einheit',
};

export function eventDetails(e) {
  const d = e.payload || {};
  const teile = [];
  if (d.expiry_date) teile.push(`MHD ${fmtDate(d.expiry_date)}`);
  if (d.quantity !== undefined && e.type !== 'edit') teile.push(`${d.quantity} ${d.unit || ''}`.trim());
  if (d.reason) teile.push(REASONS[d.reason] || d.reason);
  if (d.von) teile.push(`vorher „${d.von}“`);
  if (d.artikel !== undefined && e.type.startsWith('rename')) teile.push(`${d.artikel} Artikel`);
  if (d.battery !== undefined) teile.push(`Akku ${d.battery} %`);
  if (e.type === 'notify') teile.push(`${d.expired ?? 0} abgelaufen, ${d.soon ?? 0} bald`);
  if (e.type === 'edit') {
    Object.entries(d).forEach(([k, v]) => {
      if (k === 'expiry_date') return;
      teile.push(`${FIELD_NAMES[k] || (k === 'quantity' ? 'Menge' : k)}: ${v}`);
    });
  }
  if (e.location && ['add', 'restore'].includes(e.type)) teile.push(e.location);
  return teile.join(' · ');
}

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// Eingabefeld fuer Listen (Marken, Sorten): jeder Eintrag ist eine Blase, die
// sich einzeln wieder entfernen laesst. Vorher stand hier ein Textfeld mit
// Kommatrennung - Kommas im Markennamen zerlegten den Eintrag stillschweigend
// in zwei, und zum Loeschen eines Eintrags musste man den Text lesen koennen.
//
// Gibt ein Objekt mit `values()` zurueck; der Aufrufer holt sich damit den
// Stand beim Speichern. Bewusst kein verstecktes Eingabefeld: das Formular
// soll nichts von der Darstellung wissen.
export function tagEditor(host, initial = [], placeholder = 'Hinzufügen …') {
  const items = [...initial];
  host.classList.add('tags');
  host.innerHTML = `<div class="tag-list"></div>
    <div class="tag-add">
      <button type="button" class="sm tag-plus" title="${esc(placeholder)}">+</button>
      <input type="text" class="tag-input" placeholder="${esc(placeholder)}" hidden>
    </div>`;

  const list = host.querySelector('.tag-list');
  const plus = host.querySelector('.tag-plus');
  const input = host.querySelector('.tag-input');

  const draw = () => {
    list.innerHTML = items.map((v, i) => `<span class="tag">${esc(v)}
      <button type="button" class="tag-x" data-i="${i}" aria-label="${esc(v)} entfernen">×</button></span>`).join('');
  };

  const add = () => {
    const value = input.value.trim();
    // Doppelte stillschweigend schlucken statt zu meckern: der Nutzer wollte
    // den Eintrag, und er ist ja schon da.
    if (value && !items.some((v) => v.toLowerCase() === value.toLowerCase())) {
      items.push(value);
      draw();
    }
    input.value = '';
  };

  plus.addEventListener('click', () => {
    if (input.hidden) { input.hidden = false; input.focus(); return; }
    add();
    input.focus();
  });

  input.addEventListener('keydown', (ev) => {
    // Enter im Dialog wuerde sonst das Formular abschicken und den Dialog
    // schliessen, bevor die Blase ueberhaupt entsteht.
    if (ev.key === 'Enter' || ev.key === ',') { ev.preventDefault(); add(); }
    else if (ev.key === 'Escape' && !input.value) { ev.preventDefault(); input.hidden = true; }
    else if (ev.key === 'Backspace' && !input.value && items.length) { items.pop(); draw(); }
  });

  // Wer wegklickt, hat den Eintrag trotzdem gemeint - sonst geht getippter
  // Text beim Griff zum Speichern-Knopf verloren.
  input.addEventListener('blur', () => { if (input.value.trim()) add(); });

  list.addEventListener('click', (ev) => {
    const x = ev.target.closest('.tag-x');
    if (!x) return;
    items.splice(+x.dataset.i, 1);
    draw();
  });

  draw();
  return {
    values: () => [...items],
    isEmpty: () => items.length === 0,
    set: (next) => { items.length = 0; items.push(...next); draw(); },
  };
}

// Live-Signale vom Server. Der Server schickt nur "was" sich geaendert hat;
// die Seite laedt daraufhin die betroffene Ansicht neu.
export function liveConnect(onEvent, onStatus) {
  let socket = null;
  let retry = 1000;

  // Eintrittskarte fuer den Socket. Ist kein Web-Passwort gesetzt, braucht der
  // Server keine und die Anfrage schadet auch nicht; ist eines gesetzt, kann
  // die Seite das Passwort nicht selbst mitschicken - HTTP Basic liegt beim
  // Browser, und eine WebSocket-Verbindung nimmt keine eigenen Kopfzeilen an.
  const holeTicket = async () => {
    try {
      const antwort = await post('/api/ws-ticket');
      return antwort?.ticket || '';
    } catch {
      return '';
    }
  };

  const open = async () => {
    const ticket = await holeTicket();
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const suffix = ticket ? `?ticket=${encodeURIComponent(ticket)}` : '';
    socket = new WebSocket(`${scheme}://${location.host}/ws/ui${suffix}`);

    socket.onopen = () => { retry = 1000; onStatus(true); };
    socket.onmessage = (ev) => {
      try { onEvent(JSON.parse(ev.data)); } catch { /* ignorieren */ }
    };
    socket.onclose = () => {
      onStatus(false);
      // Exponentiell bis 15 s: ein Server-Neustart soll die Oberflaeche nicht
      // in eine Reconnect-Schleife mit Vollgas schicken.
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 15000);
    };
    socket.onerror = () => socket.close();
  };

  open();
  setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send('ping');
  }, 25000);
}
