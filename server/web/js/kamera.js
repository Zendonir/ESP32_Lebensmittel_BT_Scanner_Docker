// Eine Kamera fuer die ganze Seite.
//
// Safari fragt bei "Fragen" fuer jeden neuen getUserMedia-Aufruf erneut nach
// der Erlaubnis - Barcode-Scan und MHD-Kamera kurz hintereinander hiessen zwei
// Abfragen. Deshalb bleibt der Strom nach dem Freigeben noch NACHLAUF_MS offen
// und wird beim naechsten Holen wiederverwendet. Erst danach geht die Kamera
// wirklich aus (Akku, gruener Punkt).
const NACHLAUF_MS = 60000;
let strom = null;
let aus = null;

const lebt = (s) => s && s.getVideoTracks().some((t) => t.readyState === 'live');

export async function kameraHolen() {
  clearTimeout(aus);
  if (lebt(strom)) return strom;
  strom = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } },
  });
  return strom;
}

export function kameraFreigeben() {
  clearTimeout(aus);
  aus = setTimeout(() => {
    strom?.getTracks().forEach((t) => t.stop());
    strom = null;
  }, NACHLAUF_MS);
}

// Seite verborgen (App gewechselt, Bildschirm aus): sofort aus.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && strom) {
    clearTimeout(aus);
    strom.getTracks().forEach((t) => t.stop());
    strom = null;
  }
});
