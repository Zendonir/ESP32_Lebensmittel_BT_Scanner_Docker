# Fremdbibliotheken

Hier liegt genau eine: **ZXing für JavaScript** (`zxing.min.js`, Apache-2.0,
Lizenztext in `zxing-LICENSE.txt`). Sie entschlüsselt Barcodes aus einem
Kamerabild.

Warum überhaupt: Chrome und Android bringen mit `BarcodeDetector` einen
eingebauten Decoder mit — Safari nicht, weder auf dem iPhone noch auf dem Mac.
Und auf dem iPhone ist *jeder* Browser Safari, weil Apple die Engine vorgibt.
Ohne diese Datei bleibt am iPhone nur das Eintippen.

Eingecheckt statt über ein CDN geladen: das Projekt hat bewusst keinen
Bauschritt, der Container soll ohne Internet laufen, und eine fremde Adresse
im Auslieferungspfad wäre eine Abhängigkeit, die niemand bemerkt, bis sie
ausfällt.

Geladen wird sie erst, wenn sie gebraucht wird — kennt der Browser
`BarcodeDetector`, rührt der Scan-Bildschirm sie nicht an.

Aktualisieren:

    curl -s https://registry.npmjs.org/@zxing/library/-/library-<version>.tgz \
      | tar xz -O package/umd/index.min.js > js/vendor/zxing.min.js
