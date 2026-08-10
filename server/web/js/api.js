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
