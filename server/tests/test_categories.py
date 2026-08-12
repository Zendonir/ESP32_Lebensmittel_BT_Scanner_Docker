"""Zuordnung der OpenFoodFacts-Kategorien und der Anzeigename.

Reine Rechenlogik ohne Datenbank - deshalb ohne die Umgebungsvorbereitung der
uebrigen Testdateien.
"""

from __future__ import annotations

import pytest

from app.services import categories as cat


@pytest.mark.parametrize(
    "tags,erwartet",
    [
        (["en:dairies", "en:fermented-foods", "en:cheeses"], "Milchprodukte"),
        (["en:meats", "en:pork"], "Fleisch & Fisch"),
        (["en:plant-based-foods", "en:fruits"], "Obst & Gemüse"),
        (["en:breads"], "Backwaren"),
        (["en:frozen-foods", "en:ice-cream"], "Tiefkühl"),
        (["en:canned-foods"], "Konserven"),
        (["en:pastas"], "Trockenware"),
        (["en:snacks", "en:sweet-snacks", "en:chocolates"], "Süßes & Snacks"),
        (["en:meals", "en:pizzas"], "Restmahlzeit"),
        (["de:milchprodukte"], "Milchprodukte"),
    ],
)
def test_off_kategorien_werden_zugeordnet(tags, erwartet):
    assert cat.match_off(tags) == erwartet


def test_unbekanntes_bleibt_sonstiges():
    """Lieber ehrlich unsortiert als falsch einsortiert.

    Getraenke sind der praktische Fall: in der festen Auswahl gibt es dafuer
    keine Kategorie, also darf hier nichts Erfundenes herauskommen.
    """
    assert cat.match_off(["en:beverages", "en:iced-teas"]) == "Sonstiges"
    assert cat.match_off([]) == "Sonstiges"


def test_spezifischste_marke_gewinnt():
    """OpenFoodFacts sortiert von allgemein nach speziell.

    Bei ("en:plant-based-foods", "en:breads") ist Backwaren die brauchbare
    Angabe - die steht hinten, nicht vorn.
    """
    assert cat.match_off(["en:plant-based-foods", "en:breads"]) == "Backwaren"


@pytest.mark.parametrize(
    "tags,erwartet",
    [
        (["en:meats", "en:pork"], "Schwein"),
        (["en:meats", "en:poultry", "en:chicken"], "Geflügel"),
        (["en:meats", "en:beef"], "Rind"),
        (["en:meats", "en:veal"], "Kalb"),
        (["en:meats", "en:lamb"], "Lamm"),
        (["en:meats"], ""),          # Fleisch ohne naehere Angabe
    ],
)
def test_unterkategorie_aus_den_marken(tags, erwartet):
    assert cat.match_subcategory("Fleisch & Fisch", tags) == erwartet


def test_unterkategorien_nur_wo_vorgesehen():
    assert cat.subcategories_for("Backwaren") == ()
    assert "Schwein" in cat.subcategories_for("Fleisch & Fisch")
    # Bestehende Datenbanken tragen die Kategorie teils noch umschrieben.
    assert cat.subcategories_for("Fleisch & Fisch") == cat.subcategories_for("fleisch & fisch")


def test_anzeigename_haengt_die_sorte_an():
    assert cat.display_name("Filet", "Schwein") == "Filet - Schwein"
    assert cat.display_name("Filet", "") == "Filet"


def test_anzeigename_wiederholt_die_sorte_nicht():
    """Steht die Sorte schon im Namen, waere "Schweinefilet - Schwein" albern."""
    assert cat.display_name("Schweinefilet", "Schwein") == "Schweinefilet"
