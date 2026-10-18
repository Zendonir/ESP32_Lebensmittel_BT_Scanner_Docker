// MHD scannen: Vollbild-Kamera mit Zielrahmen.
//
// Eine Datei fuer beide Stellen - die Einlagern-Maske am Handy und die
// Testseite unter System. Sonst testet man dort etwas anderes, als in der
// Praxis laeuft: anderer Ausschnitt, andere Aufloesung, anderer Takt.
//
// Gelesen wird nur, was im Rahmen liegt (kleiner Druck auf einer Flasche geht
// im ganzen Bild unter), etwa ein Bild pro Sekunde. Ohne Treffer fragt nach
// KI_NACH_MS genau ein Bild die KI - falls erlaubt.
import { post, esc } from './api.js';

export const KI_NACH_MS = 6000;
const TAKT_MS = 900;

// Ausschnitt eines Bildes (Video oder Foto) als JPEG in Base64, hoechstens
// 1280 Pixel breit - passt durch einen Reverse Proxy mit 1-MB-Grenze.
export function jpegAus(quelle, x, y, w, h) {
  const faktor = Math.min(1, 1280 / Math.max(w, h));
  const leinwand = document.createElement('canvas');
  leinwand.width = Math.round(w * faktor);
  leinwand.height = Math.round(h * faktor);
  leinwand.getContext('2d').drawImage(quelle, x, y, w, h, 0, 0, leinwand.width, leinwand.height);
  return leinwand.toDataURL('image/jpeg', 0.85).split(',')[1];
}

const datumText = (iso) => (iso ? iso.split('-').reverse().join('.') : '');

// Praxis: liefert {datum, gelesen, quelle} beim ersten Treffer oder null.
// Test (`test: true`): laeuft bis "Beenden", schickt jedes Bild an
// /api/mhd/test und meldet es an `beiBild(bild, antwort)` - die Testseite
// entscheidet, was sie davon aufhebt.
export async function mhdKamera({ kiErlaubt = false, test = false, beiBild = null } = {}) {
  const ebene = document.createElement('div');
  ebene.innerHTML = `<div style="position:fixed;inset:0;z-index:60;background:#000;display:flex;flex-direction:column">
    <div style="position:relative;flex:1;overflow:hidden">
      <video playsinline muted autoplay style="width:100%;height:100%;object-fit:cover"></video>
      <div class="ziel" style="position:absolute;left:8%;right:8%;top:36%;height:24%;border:3px solid #fff;
        border-radius:12px;box-shadow:0 0 0 9999px rgba(0,0,0,.5);transition:border-color .2s"></div>
      ${test ? `<div class="debug" style="position:absolute;left:0;right:0;top:0;padding:10px 12px;
        font:12px/1.4 ui-monospace,monospace;color:#fff;background:rgba(0,0,0,.65);max-height:34%;overflow:auto"></div>` : ''}
    </div>
    <div style="padding:14px 16px calc(14px + env(safe-area-inset-bottom));display:grid;gap:10px;background:#111">
      <div class="info" style="text-align:center;color:#fff">Datum in den Rahmen halten</div>
      <div style="display:flex;gap:10px">
        ${test ? '<button type="button" class="merken" style="flex:1">Bild merken</button>' : ''}
        <button type="button" class="ghost ende" style="flex:1">${test ? 'Beenden' : 'Abbrechen'}</button>
      </div>
    </div></div>`;
  document.body.appendChild(ebene);
  const video = ebene.querySelector('video');
  const ziel = ebene.querySelector('.ziel');
  const info = ebene.querySelector('.info');
  const debug = ebene.querySelector('.debug');
  let offen = true, kamera = null, merken = false;
  const ende = () => {
    offen = false;
    kamera?.getTracks().forEach((t) => t.stop());
    ebene.remove();
  };
  ebene.querySelector('.ende').addEventListener('click', ende);
  ebene.querySelector('.merken')?.addEventListener('click', () => { merken = true; });
  try {
    kamera = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } },
    });
  } catch (e) {
    ende();
    throw new Error(`Kamera nicht verfügbar: ${e.message}`);
  }
  video.srcObject = kamera;
  video.play?.().catch(() => {});

  // Rahmen (Bildschirm) -> Videopixel; object-fit: cover schneidet ab.
  function ausschnitt() {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw) return null;
    const flaeche = video.getBoundingClientRect(), r = ziel.getBoundingClientRect();
    const s = Math.max(flaeche.width / vw, flaeche.height / vh);
    const ox = (flaeche.width - vw * s) / 2, oy = (flaeche.height - vh * s) / 2;
    const x = Math.max(0, (r.left - flaeche.left - ox) / s);
    const y = Math.max(0, (r.top - flaeche.top - oy) / s);
    return jpegAus(video, x, y, Math.min(vw - x, r.width / s), Math.min(vh - y, r.height / s));
  }

  const start = Date.now();
  let kiVersucht = false, bilder = 0, treffer = 0, letzterTreffer = '';
  while (offen) {
    const t0 = Date.now();
    const bild = ausschnitt();
    if (bild) {
      const mitKi = kiErlaubt && !kiVersucht && !treffer && Date.now() - start > KI_NACH_MS;
      if (mitKi) { kiVersucht = true; info.textContent = 'Frage die KI …'; }
      try {
        if (!test) {
          const r = await post('/api/mhd/foto', { bild, ki: mitKi });
          if (!offen) return null;
          if (r.datum) { ende(); return r; }
          if (mitKi) info.textContent = 'Nicht erkannt – näher ran, Licht von der Seite';
        } else {
          const r = await post('/api/mhd/test', { bild, ki: mitKi });
          if (!offen) return null;
          bilder += 1;
          const l = r.lokal || {};
          const datum = l.datum || r.ki?.datum || '';
          if (datum) treffer += 1;
          ziel.style.borderColor = datum ? '#43a047' : '#fff';
          info.textContent = `Bild ${bilder} · ${treffer} Treffer`
            + (datum ? ` · zuletzt ${datumText(datum)}` : '');
          debug.innerHTML = `${l.ms ?? '–'} ms lokal${r.ki ? ` · KI ${r.ki.ms} ms → ${esc(datumText(r.ki.datum) || 'nichts')}` : ''}<br>`
            + `<b>${esc(datumText(l.datum) || 'kein Datum')}</b>`
            + `${l.gelesen ? ` aus „${esc(l.gelesen)}“` : ''}<br>`
            + (l.texte || []).map((t) => `${esc(t.text)} <span style="opacity:.6">${Math.round(t.sicher * 100)}%</span>`)
              .join(' · ');
          // Aufheben: was man ausdruecklich merkt, und jeder neue Treffer -
          // nicht jedes Bild, sonst stehen nach einer Minute 60 gleiche da.
          if (merken || (datum && datum !== letzterTreffer) || mitKi) {
            beiBild?.(bild, r);
            merken = false;
          }
          if (datum) letzterTreffer = datum;
        }
      } catch (e) {
        if (!offen) return null;
        info.textContent = e.message;
      }
    }
    await new Promise((ok) => setTimeout(ok, Math.max(150, TAKT_MS - (Date.now() - t0))));
  }
  return null;
}
