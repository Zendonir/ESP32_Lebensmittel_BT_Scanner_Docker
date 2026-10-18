// ZIP-Datei im Browser bauen, ohne Bibliothek.
//
// Nur "gespeichert" (Methode 0, keine Kompression): gedacht fuer JPEGs, die
// schon gepackt sind, und ein wenig JSON. So bleibt es bei der Pruefsumme
// (CRC-32) und zwei Kopfformaten - jedes Entpackprogramm liest das.

const CRC_TABELLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(daten) {
  let c = 0xffffffff;
  for (let i = 0; i < daten.length; i++) c = CRC_TABELLE[(c ^ daten[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// Zeitstempel im DOS-Format, wie ZIP ihn erwartet.
function dosZeit(d) {
  return {
    zeit: (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1),
    datum: ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate(),
  };
}

// dateien: [{ name: "a/b.jpg", daten: Uint8Array | string }]
export function zipBlob(dateien) {
  const kodierer = new TextEncoder();
  const { zeit, datum } = dosZeit(new Date());
  const teile = [];
  const verzeichnis = [];
  let versatz = 0;

  for (const datei of dateien) {
    const name = kodierer.encode(datei.name);
    const daten = typeof datei.daten === 'string' ? kodierer.encode(datei.daten) : datei.daten;
    const crc = crc32(daten);

    const kopf = new DataView(new ArrayBuffer(30));
    kopf.setUint32(0, 0x04034b50, true);    // lokaler Dateikopf
    kopf.setUint16(4, 20, true);            // benoetigte Version 2.0
    kopf.setUint16(6, 0x0800, true);        // Bit 11: Name ist UTF-8
    kopf.setUint16(8, 0, true);             // Methode: gespeichert
    kopf.setUint16(10, zeit, true);
    kopf.setUint16(12, datum, true);
    kopf.setUint32(14, crc, true);
    kopf.setUint32(18, daten.length, true);
    kopf.setUint32(22, daten.length, true);
    kopf.setUint16(26, name.length, true);
    kopf.setUint16(28, 0, true);
    teile.push(new Uint8Array(kopf.buffer), name, daten);

    const eintrag = new DataView(new ArrayBuffer(46));
    eintrag.setUint32(0, 0x02014b50, true); // Eintrag im zentralen Verzeichnis
    eintrag.setUint16(4, 20, true);
    eintrag.setUint16(6, 20, true);
    eintrag.setUint16(8, 0x0800, true);
    eintrag.setUint16(10, 0, true);
    eintrag.setUint16(12, zeit, true);
    eintrag.setUint16(14, datum, true);
    eintrag.setUint32(16, crc, true);
    eintrag.setUint32(20, daten.length, true);
    eintrag.setUint32(24, daten.length, true);
    eintrag.setUint16(28, name.length, true);
    eintrag.setUint32(42, versatz, true);   // wo der lokale Kopf steht
    verzeichnis.push(new Uint8Array(eintrag.buffer), name);

    versatz += 30 + name.length + daten.length;
  }

  const groesse = verzeichnis.reduce((s, t) => s + t.length, 0);
  const ende = new DataView(new ArrayBuffer(22));
  ende.setUint32(0, 0x06054b50, true);      // Ende des zentralen Verzeichnisses
  ende.setUint16(8, dateien.length, true);
  ende.setUint16(10, dateien.length, true);
  ende.setUint32(12, groesse, true);
  ende.setUint32(16, versatz, true);
  return new Blob([...teile, ...verzeichnis, new Uint8Array(ende.buffer)], { type: 'application/zip' });
}

export function base64ZuBytes(b64) {
  const roh = atob(b64);
  const bytes = new Uint8Array(roh.length);
  for (let i = 0; i < roh.length; i++) bytes[i] = roh.charCodeAt(i);
  return bytes;
}
