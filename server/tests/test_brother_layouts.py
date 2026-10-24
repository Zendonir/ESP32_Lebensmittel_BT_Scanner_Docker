"""Etikett-Layouts fuer den Brother (services/brother_layouts.py)."""

import pytest

from app.services import brother, brother_layouts, settings_store

# Druckbereiche der Etikettensorten (Punkte quer x Zeilen).
MASSE = [(382, 157), (432, 224), (376, 192), (296, 352), (296, 432)]

VOLL = {"label": "LEB004711", "name": "Hähnchenbrustfilet ohne Haut tiefgefroren",
        "brand": "Wiesenhof Premium Geflügel", "expiry_date": "2027-01-03",
        "added_date": "2026-10-22", "quantity": 1500, "unit": "g",
        "location": "Gefrierschrank Keller Schublade 3"}
LEER = {"label": "LEB000009", "name": "Milch"}


@pytest.mark.parametrize("layout", sorted(brother_layouts.ZEICHNER))
@pytest.mark.parametrize("breite,hoehe", MASSE)
@pytest.mark.parametrize("item", [VOLL, LEER, {}])
def test_jedes_layout_fuellt_genau_den_druckbereich(layout, breite, hoehe, item):
    bild = brother_layouts.zeichnen(layout, item, breite, hoehe)
    assert bild.size == (breite, hoehe)
    assert bild.getextrema()[0] == 0          # es steht etwas drauf


def test_standard_bleibt_beim_bisherigen_etikett():
    assert brother_layouts.zeichnen("standard", VOLL, 382, 157) is None
    assert brother_layouts.zeichnen("gibtsnicht", VOLL, 382, 157) is None
    assert set(brother_layouts.ZEICHNER) | {"standard"} == set(brother_layouts.LAYOUTS)


def test_hochkant_gelesen_nimmt_immer_layout_e():
    erwartet = brother_layouts.hochkant(VOLL, 296, 432).tobytes()
    for layout in ("klassisch", "balken", "raster", "tabelle"):
        assert brother_layouts.zeichnen(layout, VOLL, 296, 432).tobytes() == erwartet


def test_alle_angaben_kommen_aus_dem_artikel():
    d = brother_layouts.daten(VOLL)
    assert d["marke"] == "Wiesenhof Premium Geflügel"
    assert d["mhd"] == "03.01.2027" and d["ein"] == "22.10.2026"
    assert d["menge"] == "1,5 kg"
    assert d["ort"] == "Gefrierschrank Keller Schublade 3"
    assert d["label"] == "LEB004711"
    # Die Marke reist im Auftrag mit.
    assert brother.auftrag(VOLL)["item"]["brand"] == VOLL["brand"]


def test_stapel_verkleinert_erst_und_laesst_dann_optionales_weg():
    E = brother_layouts.Eintrag
    eintraege = [E("Name", 40, 16, True, 1, True), E("Marke", 18, 12), E("Ort", 18, 12)]
    alles = brother_layouts._stapel(eintraege, 300, 500)
    assert [z for _, zz, _ in alles for z in zz] == ["Name", "Marke", "Ort"]
    assert alles[0][0].size == 40
    eng = brother_layouts._stapel(eintraege, 300, 40)
    # Pflicht bleibt, Optionales faellt von hinten weg.
    assert [z for _, zz, _ in eng for z in zz][0] == "Name"
    assert "Ort" not in [z for _, zz, _ in eng for z in zz]


def test_code_bleibt_lesbar_gross():
    """Auf 51 x 26 mm darf der Code nicht unter 3 Punkte je Modul fallen."""
    mindest = brother_layouts._code_min(VOLL["label"])
    code = brother_layouts._code(VOLL["label"], 130, mindest, nummer=False)
    assert code.width == code.height == mindest


def test_etikett_bild_nutzt_das_eingestellte_layout():
    profil = brother.standard_profil(51, 26)
    cfg = {"breite_mm": 51, "layout": "tabelle"}
    bild = brother.etikett_bild(VOLL, profil, cfg)
    breite, zeilen = brother.masse(profil, cfg)
    erwartet = brother.hochformat.einfarbig(brother_layouts.tabelle(VOLL, breite, zeilen))
    assert bild.tobytes() == erwartet.tobytes()
    # Ohne Layout wie bisher.
    alt = brother.etikett_bild(VOLL, profil, {"breite_mm": 51})
    assert alt.tobytes() != bild.tobytes()


def test_layout_ist_einstellbar():
    assert settings_store.DEFAULTS["brother"]["layout"] == "standard"


def test_f_bis_i_tragen_sorte_und_haushalt():
    item = {**VOLL, "name": "Hähnchenbrust", "subcategory": "Filet", "household": "Grote"}
    d = brother_layouts.daten(item)
    assert d["produkt"] == "Hähnchenbrust" and d["sorte"] == "Filet" and d["haushalt"] == "Grote"
    # Steckt die Sorte schon im Namen, steht sie nicht doppelt da.
    assert brother_layouts.daten({**item, "name": "Hähnchenbrust Filet"})["sorte"] == ""
    # Haushalt reist im Brother-Auftrag mit.
    assert brother.auftrag(item)["item"]["household"] == "Grote"


@pytest.mark.parametrize("layout", ["stufen", "kopf", "spalten", "ecke"])
def test_f_bis_i_zeichnen_den_haushalt(layout):
    # Der Haushalt kommt tatsaechlich aufs Etikett (bei G weiss im Balken).
    mit = brother_layouts.zeichnen(layout, {**VOLL, "household": "Grote"}, 382, 157)
    ohne = brother_layouts.zeichnen(layout, VOLL, 382, 157)
    assert mit.tobytes() != ohne.tobytes()
