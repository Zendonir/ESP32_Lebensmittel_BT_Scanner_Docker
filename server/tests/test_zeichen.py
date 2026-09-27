"""Was das Telefon beim Tippen einsetzt, muss auf Etikett und Terminal lesbar
ankommen.

Drucker und Terminal kennen nur Latin-1; alles darueber wird in der Firmware
zu "?". Das iPhone setzt aber von selbst typografische Anfuehrungszeichen und
Gedankenstriche - ein Name, der im Browser tadellos aussieht, kam als
?Omas Suppe? aufs Etikett.
"""

from __future__ import annotations

from app.device import protocol
from app.services import labels
from app.services import hochformat
from app.services.zeichen import latin1, latin1_tief

VOM_IPHONE = "„Omas Suppe“ – Rest…"


def _nur_latin1(text: str) -> bool:
    return all(ord(c) <= 0xFF for c in text)


def test_typografische_zeichen_werden_ersetzt():
    assert latin1(VOM_IPHONE) == '"Omas Suppe" - Rest...'


def test_umlaute_bleiben():
    assert latin1("Hähnchenbrust, süß & groß – 5 €") == "Hähnchenbrust, süß & groß - 5 EUR"


def test_etikett_enthaelt_nur_druckbare_zeichen():
    item = {"label": "LEB000001", "name": VOM_IPHONE, "brand": "Metzgerei „Zur Post“",
            "category": "Sonstiges", "expiry_date": "2027-01-01", "quantity": 1,
            "location": "Kühlschrank", "added_date": "2026-09-27"}
    for layout in labels.LAYOUTS:
        payload = labels.render_label(item, {"label_layout": layout, "household": "Fam. Müller – Nord"})
        texte = [str(b.get(k, "")) for b in payload["blocks"]
                 if b["t"] != "raster" for k in ("v", "k")]
        if layout in labels.BILD_LAYOUTS:
            # Ein Bild hat keine Textbloecke - geprueft wird, was hineingezeichnet
            # wurde, und zwar aus demselben umgesetzten Artikel wie im Druck.
            texte = hochformat.textzeilen(latin1_tief(item), {}, 216, 384)
        schlecht = [t for t in texte if not _nur_latin1(t)]
        assert not schlecht, f"{layout}: {schlecht}"
        assert any("Omas Suppe" in t for t in texte), layout


def test_terminalbildschirm_enthaelt_nur_darstellbare_zeichen():
    s = protocol.screen(screen_id=1, kind="list", title=VOM_IPHONE,
                        items=[{"id": "x", "label": VOM_IPHONE, "sub": "–"}])
    assert _nur_latin1(s["title"])
    assert _nur_latin1(s["items"][0]["label"]) and _nur_latin1(s["items"][0]["sub"])
    assert _nur_latin1(protocol.toast(f"{VOM_IPHONE} ausgelagert")["text"])
