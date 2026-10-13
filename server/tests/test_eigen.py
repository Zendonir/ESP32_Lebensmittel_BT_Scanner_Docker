"""Eigene Aufteilung: Felder in der gewaehlten Reihenfolge und im gewaehlten Stil."""

from __future__ import annotations

from app.services import labels

ITEM = {"label": "LEB000777", "name": "Gulasch", "subcategory": "Rind",
        "category": "Fleisch", "expiry_date": "2026-12-24", "added_date": "2026-10-11",
        "quantity": 2, "unit": "Port.", "location": "Truhe", "household": "Grote"}


def _cfg(zeilen):
    return {"label_layout": "eigen", "label_custom": zeilen, "label_height_mm": 40,
            "label_feed_edge": "hoehe"}


def _texte(payload):
    out = []
    for b in payload["blocks"]:
        if b["t"] == "row":
            out.append(f'{b["k"]}={b["v"]}')
        elif b["t"] == "text":
            out.append(b["v"])
        elif b["t"] in ("qr", "code128", "sep"):
            out.append(b["t"])
    return out


def test_reihenfolge_und_stil():
    p = labels.render_label(ITEM, _cfg([
        {"feld": "location", "stil": "fett"},
        {"feld": "expiry_date", "stil": "zeile", "unterstrichen": True},
        {"feld": "linie"},
        {"feld": "count", "stil": "zeile"},
        {"feld": "household", "stil": "links"},
        {"feld": "code"},
    ]))
    t = _texte(p)
    assert t[:5] == ["Truhe", "MHD=24.12.2026", "sep", "Stück=2", "Grote"]
    assert t[5] in ("qr", "code128")
    mhd = next(b for b in p["blocks"] if b["t"] == "row" and b["k"] == "MHD")
    assert mhd["underline"] and mhd["keep"]


def test_leere_felder_fallen_weg_und_unbekannte_auch():
    p = labels.render_label({**ITEM, "location": ""}, _cfg([
        {"feld": "location", "stil": "zeile"}, {"feld": "gibtsnicht"}, {"feld": "name", "stil": "text"},
    ]))
    assert _texte(p) == ["Gulasch"]


def test_ohne_eigene_zeilen_gilt_der_vorschlag():
    p = labels.render_label(ITEM, _cfg([]))
    t = " ".join(_texte(p))
    assert "Gulasch" in t and "MHD=24.12.2026" in t and "Ort=Truhe" in t


def test_mhd_bleibt_wenn_es_eng_wird():
    zeilen = [{"feld": f, "stil": "zeile"} for f in
              ("category", "subcategory", "location", "household", "added_date", "count",
               "quantity", "label", "brand")] + [{"feld": "expiry_date", "stil": "zeile"},
                                                 {"feld": "code"}]
    p = labels.render_label(ITEM, {**_cfg(zeilen), "label_height_mm": 20})
    assert any(b["t"] == "row" and b["k"] == "MHD" for b in p["blocks"])
    assert p["overflow"] == 0 or any(b["t"] in ("qr", "code128") for b in p["blocks"])


def test_vorschau_mit_esc_k_zeigt_die_erste_zeile():
    """Der raw-Block des Rueckzugs (h negativ) schob die erste Zeile ueber den Rand."""
    import re

    cfg = {**_cfg([{"feld": "name", "stil": "gross"}, {"feld": "code"}]),
           "backfeed_dots": 48, "backfeed_cmd": "esck", "label_dead_zone_mm": 8}
    p = labels.render_label(ITEM, cfg)
    svg = labels.render_preview_svg(p, cfg)
    ys = [int(y) for y in re.findall(r'<text[^>]*\by="(-?\d+)"', svg)]
    assert ys and min(ys) > 0


def test_ablauf_einfach_rueckzug_laenge_vorschub():
    cfg = {"label_layout": "standard", "label_height_mm": 25, "label_feed_edge": "hoehe",
           "backfeed_dots": 48, "label_dead_zone_mm": 8,
           "label_sequence": "einfach", "label_gap_mm": 3}
    p = labels.render_label(ITEM, cfg)
    b = p["blocks"]
    assert b[0] == {"t": "back", "dots": 48}
    assert b[-1] == {"t": "feed", "dots": 24}               # 3 mm Vorschub danach
    inhalt = sum(labels.block_dots(x) for x in b[1:-1])
    assert inhalt == 25 * 8                                 # genau die Hoehe
    assert labels.total_dots(b) == -48 + 200 + 24


def test_weggelassene_zeilen_werden_genannt():
    zeilen = [{"feld": f, "stil": "zeile"} for f in
              ("name", "brand", "quantity", "location", "added_date", "household",
               "category", "label")] + [{"feld": "code"}]
    p = labels.render_label(ITEM, {**_cfg(zeilen), "label_height_mm": 20})
    assert p["weggelassen"], "bei 20 mm passt nicht alles"
    gedruckt = {b.get("k") for b in p["blocks"] if b["t"] == "row"}
    assert not gedruckt & set(p["weggelassen"])


def test_vorschub_in_schritten_aendert_die_laenge_nicht():
    cfg = {"label_layout": "standard", "label_height_mm": 25, "label_feed_edge": "hoehe",
           "label_sequence": "einfach", "label_gap_mm": 3}
    am_stueck = labels.render_label(ITEM, cfg)
    schritte = labels.render_label(ITEM, {**cfg, "feed_step_dots": 8})
    vorschuebe = [b["dots"] for b in schritte["blocks"] if b["t"] == "feed"]
    assert vorschuebe and max(vorschuebe) <= 8
    assert labels.total_dots(schritte["blocks"]) == labels.total_dots(am_stueck["blocks"])


# ------------------------------------------- Ablauf: Papier je Etikett
_EINFACH = {"label_layout": "standard", "label_height_mm": 25, "label_feed_edge": "hoehe",
            "label_sequence": "einfach", "backfeed_dots": 48}


def test_vorschub_danach_ergibt_sich_aus_dem_etikettenabstand():
    """25 mm Hoehe, 6 mm Rueckzug, 32-mm-Rolle: genau 13 mm Vorschub danach -
    der Wert, der am echten Drucker als einziger funktionierte."""
    a = labels.ablauf_einfach({**_EINFACH, "label_pitch_mm": 32})
    assert a["vorschub"] == 13 * 8 and a["papier"] == 32 * 8 and a["aus_abstand"]
    for hoehe in (22, 25, 28):          # Hoehe aendern: die Teilung bleibt
        p = labels.render_label(ITEM, {**_EINFACH, "label_pitch_mm": 32, "label_height_mm": hoehe})
        assert labels.total_dots(p["blocks"]) == 32 * 8


def test_ohne_abstand_gilt_der_vorschub_danach():
    a = labels.ablauf_einfach({**_EINFACH, "label_gap_mm": 12})
    assert a["papier"] == (25 + 12) * 8 - 48 and not a["aus_abstand"]


def test_zu_lange_hoehe_wird_gemeldet():
    a = labels.ablauf_einfach({**_EINFACH, "label_pitch_mm": 20, "label_height_mm": 30})
    assert a["vorschub"] == 0 and a["zu_lang"] == (30 - 20) * 8 - 48


def test_gedruckter_vorschub_aendert_die_laenge_nicht():
    cfg = {**_EINFACH, "label_pitch_mm": 32, "backfeed_cmd": "esck", "raster_mode": "esc33"}
    schnell = labels.render_label(ITEM, cfg)
    for art, typ in (("leerzeilen", "text"), ("bild", "raster")):
        p = labels.render_label(ITEM, {**cfg, "feed_mode": art})
        assert labels.total_dots(p["blocks"]) == labels.total_dots(schnell["blocks"])
        # Was bleibt an ESC J, ist nur der Rest unter 24 Punkten
        assert all(b["dots"] < 24 for b in p["blocks"] if b["t"] == "feed")
        assert any(b["t"] == typ for b in p["blocks"])
    bild = labels.render_label(ITEM, {**cfg, "feed_mode": "bild"})
    assert labels.BILD_FAEHIGKEIT in bild["braucht"] and labels.RAW_FAEHIGKEIT in bild["braucht"]
    for b in bild["blocks"]:
        if b["t"] == "raster" and b["w"] == labels.LEERBILD_BREITE:
            import base64

            from app.services import hochformat
            roh = hochformat.unpackbits(base64.b64decode(b["d"]), 10**6)
            assert roh == bytes(b["h"]) and b["h"] % 24 == 0
