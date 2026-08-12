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
export function toast(text, level = 'info') {
  const host = document.getElementById('toasts');
  if (!host) return;
  const node = document.createElement('div');
  node.className = `toast ${level}`;
  node.textContent = text;
  host.appendChild(node);
  setTimeout(() => node.remove(), 4200);
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

export function daysPill(days) {
  if (days === null || days === undefined) return '<span class="pill">kein MHD</span>';
  if (days < 0) return `<span class="pill danger">${Math.abs(days)} T über</span>`;
  if (days === 0) return '<span class="pill danger">heute</span>';
  if (days <= 3) return `<span class="pill warn">${days} T</span>`;
  return `<span class="pill ok">${days} T</span>`;
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
  return { values: () => [...items] };
}

// Live-Signale vom Server. Der Server schickt nur "was" sich geaendert hat;
// die Seite laedt daraufhin die betroffene Ansicht neu.
export function liveConnect(onEvent, onStatus) {
  let socket = null;
  let retry = 1000;

  const open = () => {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${scheme}://${location.host}/ws/ui`);

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
