// Eine Kamera fuer die ganze Seite.
//
// iOS fragt fuer jeden neuen getUserMedia-Aufruf erneut nach der Erlaubnis,
// wenn sie nicht fest erteilt ist - und in der Web-App vom Home-Bildschirm
// laesst sie sich gar nicht fest erteilen (Safaris Website-Einstellung gilt
// dort nicht). Deshalb wird die Kamera nach dem Gebrauch nicht beendet,
// sondern nur pausiert (track.enabled = false: kein Bild, Kamera aus) und beim
// naechsten Holen wieder eingeschaltet - das fragt nicht. Ganz beendet wird
// sie erst nach NACHLAUF_MS; in der Web-App deutlich spaeter als in Safari,
// wo die Erlaubnis ohnehin fest erteilt werden kann.
const WEBAPP = window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone === true;
const NACHLAUF_MS = WEBAPP ? 30 * 60000 : 60000;
let strom = null;
let aus = null;

const lebt = (s) => s && s.getVideoTracks().some((t) => t.readyState === 'live');

function beenden() {
  clearTimeout(aus);
  strom?.getTracks().forEach((t) => t.stop());
  strom = null;
}

export async function kameraHolen() {
  clearTimeout(aus);
  if (lebt(strom)) {
    strom.getTracks().forEach((t) => { t.enabled = true; });
    return strom;
  }
  strom = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } },
  });
  return strom;
}

export function kameraFreigeben() {
  clearTimeout(aus);
  strom?.getTracks().forEach((t) => { t.enabled = false; });
  aus = setTimeout(beenden, NACHLAUF_MS);
}

// Seite verborgen: in Safari sofort aus. In der Web-App nicht - iOS haelt die
// Kamera im Hintergrund ohnehin an, und ein beendeter Strom hiesse beim
// Zurueckkommen eine neue Nachfrage.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && strom && !WEBAPP) beenden();
});
