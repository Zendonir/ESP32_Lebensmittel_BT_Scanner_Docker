# Schriften für das Hochformat-Etikett

DejaVu Sans (normal und fett), unverändert aus dem Debian-Paket
`fonts-dejavu-core`. Lizenz: Bitstream Vera, siehe `LICENSE`.

Sie liegen im Repository statt im System, weil `services/hochformat.py` das
Etikett als Bild zeichnet und dabei auf den Punkt genau rechnet: eine andere
Schrift im Container als im Test ergäbe andere Zeilenumbrüche – und ein
Etikett, das im Browser passt und auf dem Papier nicht.
