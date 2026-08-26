"""Feste Kategorien, ihre Unterkategorien und die Zuordnung von OpenFoodFacts.

OpenFoodFacts liefert eigene, sehr feingliedrige Kategorien ("Beverages And
Beverages Preparations"). Ungefiltert landet das im Bestand und macht jede
Auswertung nach Kategorie wertlos. Hier wird daraus eine der festen
Kategorien des Haushalts.

Die Zuordnung arbeitet auf **allen** Kategoriemarken eines Produkts, nicht nur
auf der ersten: OpenFoodFacts sortiert sie von allgemein nach speziell, und
die brauchbare steht selten vorn.
"""

from __future__ import annotations

import re

# Die feste Auswahl. Muss zu services/seed.CATEGORIES passen, sonst liefert
# match_off Namen, die es als Kategorie gar nicht gibt - ein Test haelt die
# beiden Listen zusammen, der Kommentar allein tat es nicht.
#
# Aenderungen hier wirken nur auf neue Installationen; bestehende Kategorien
# in der Datenbank bleiben, wie sie sind.
FIXED = (
    "Getraenke",
    "Milchprodukte",
    "Fleisch & Fisch",
    "Obst & Gemüse",
    "Backwaren",
    "Tiefkuehl",
    "Konserven",
    "Trockenware",
    "Süßes & Snacks",
    "Sonstiges",
)

FALLBACK = "Sonstiges"

# Unterkategorien je Kategorie. Steht eine dabei, taucht sie im Bestand und
# auf dem Etikett hinter dem Namen auf ("Filet - Schwein").
SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    "Fleisch & Fisch": ("Schwein", "Geflügel", "Rind", "Kalb", "Lamm"),
}

# Schluesselwoerter je Kategorie, deutsch und englisch. Geprueft wird gegen die
# Kategoriemarken von OpenFoodFacts (en:dairies, de:milchprodukte, ...).
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Fleisch & Fisch",
        ("meat", "fleisch", "wurst", "sausage", "ham", "schinken", "salami",
         "fish", "fisch", "seafood", "poultry", "geflugel", "gefluegel",
         "chicken", "huhn", "beef", "rind", "pork", "schwein", "lamb", "lamm",
         "veal", "kalb", "hackfleisch", "steak", "filet", "speck", "bacon"),
    ),
    (
        "Tiefkuehl",
        ("frozen", "tiefkuhl", "tiefkuehl", "gefroren", "ice-cream",
         "ice cream", "eiscreme", "speiseeis"),
    ),
    (
        "Getraenke",
        ("beverage", "getrank", "getraenk", "drink", "water", "wasser",
         "juice", "saft", "soda", "limonade", "tea", "tee", "iced-tea",
         "coffee", "kaffee", "beer", "bier", "wine", "wein", "spirits",
         "schnaps", "milk-drink", "smoothie"),
    ),
    (
        "Milchprodukte",
        ("dairy", "dairies", "milk", "milch", "cheese", "kase", "kaese",
         "yogurt", "yoghurt", "joghurt", "butter", "quark", "sahne", "cream",
         "curd", "molkerei"),
    ),
    (
        "Obst & Gemüse",
        ("fruit", "obst", "vegetable", "gemuse", "gemuese", "salad", "salat",
         "legume", "beeren", "berries", "kartoffel", "potato"),
    ),
    (
        "Backwaren",
        ("bread", "brot", "bakery", "backwaren", "pastries", "broetchen",
         "brotchen", "cake", "kuchen", "toast", "baguette"),
    ),
    (
        "Konserven",
        ("canned", "conserve", "konserven", "tinned", "dosen", "eingelegt",
         "pickled"),
    ),
    (
        "Trockenware",
        ("pasta", "nudeln", "rice", "reis", "cereal", "getreide", "flour",
         "mehl", "muesli", "musli", "hulsenfruchte", "legumes-secs",
         "groceries", "dried", "trocken", "gewurze", "spices", "zucker",
         "sugar", "oils", "speiseol", "olivenol", "essig", "vinegar",
         "sauce", "sossen"),
    ),
    (
        "Süßes & Snacks",
        ("snack", "sweet", "suss", "suess", "chocolate", "schokolade",
         "candy", "bonbon", "confectionery", "biscuit", "keks", "cookie",
         "chips", "crisps", "dessert", "nuss", "nuts"),
    ),
)


def _normalise(text: str) -> str:
    """Kleinschreibung ohne Umlaute - Marken kommen mal so, mal so."""
    lowered = text.lower()
    for umlaut, plain in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        lowered = lowered.replace(umlaut, plain)
    return lowered


def _key(name: str) -> str:
    """Vergleichsform fuer Kategorienamen.

    Zusaetzlich zu _normalise werden die ausgeschriebenen Umlaute eingezogen
    und alles Nichtalphanumerische verworfen: "Tiefkühl", "Tiefkuehl" und
    "tiefkuhl" sind dieselbe Kategorie, und "Fleisch & Fisch" bleibt es auch
    ohne das Kaufmanns-Und. Nur fuer Namensvergleiche gedacht, nicht fuer die
    Schluesselwortsuche - dort wuerde das Einziehen falsche Treffer erzeugen.
    """
    reduced = _normalise(name)
    for digraph, plain in (("ae", "a"), ("oe", "o"), ("ue", "u")):
        reduced = reduced.replace(digraph, plain)
    return re.sub(r"[^a-z0-9]+", "", reduced)


def resolve(name: str, available: list[str] | None) -> str:
    """Einen Kategorienamen auf die tatsaechlich vorhandenen abbilden.

    Die Regeln unten liefern die Schreibweise aus FIXED. In der Datenbank kann
    dieselbe Kategorie anders geschrieben stehen - eingedeutscht, umbenannt
    oder aus einem Import. Ohne diesen Schritt traegt der Bestand Kategorien,
    die es in der Auswahl gar nicht gibt, und die Filter finden nichts.
    """
    if not available:
        return name
    wanted = _key(name)
    for candidate in available:
        if _key(candidate) == wanted:
            return candidate
    for candidate in available:
        if _key(candidate) == _key(FALLBACK):
            return candidate
    return name


def _matches(tag: str, keyword: str) -> bool:
    """Passt ein Schluesselwort auf eine Kategoriemarke?

    Wortweise, nicht als beliebige Teilzeichenkette: sonst steckt "ol" (Oel)
    in "chocolates" und "cream" in "ice-cream", und die Zuordnung landet
    zuverlaessig in der falschen Kategorie. Mehrwortbegriffe werden als Ganzes
    gesucht, Einzelwoerter als Wortanfang - damit greift "pasta" auch bei
    "pastas".
    """
    normalised = _normalise(tag)
    if "-" in keyword or " " in keyword:
        return keyword in normalised
    return any(word.startswith(keyword) for word in re.split(r"[^a-z0-9]+", normalised))


def match_off(tags: list[str], available: list[str] | None = None) -> str:
    """Kategoriemarken von OpenFoodFacts einer festen Kategorie zuordnen.

    Von hinten nach vorn, weil die spezifischste Marke am Ende steht: bei
    ("en:meats", "en:pork") soll Schwein den Ausschlag geben, nicht Fleisch
    allgemein. Passt nichts, bleibt es bei "Sonstiges" - lieber ehrlich
    unsortiert als falsch einsortiert.

    `available` sind die tatsaechlich angelegten Kategorien; das Ergebnis wird
    darauf abgebildet, damit im Bestand keine Kategorie landet, die es in der
    Auswahl nicht gibt.
    """
    for tag in reversed(tags):
        for category, keywords in _RULES:
            if any(_matches(tag, word) for word in keywords):
                return resolve(category, available)
    return resolve(FALLBACK, available)


def match_subcategory(category: str, tags: list[str]) -> str:
    """Passende Unterkategorie aus den Kategoriemarken ableiten, sonst ""."""
    options = subcategories_for(category)
    if not options:
        return ""

    # Dieselben Woerter wie oben, aber je Unterkategorie - inklusive der
    # englischen Entsprechung, weil OpenFoodFacts ueberwiegend englisch marktet.
    aliases = {
        "Schwein": ("schwein", "pork", "porc"),
        "Geflügel": ("geflugel", "poultry", "chicken", "huhn", "pute", "turkey", "ente", "duck"),
        "Rind": ("rind", "beef", "boeuf"),
        "Kalb": ("kalb", "veal"),
        "Lamm": ("lamm", "lamb", "agneau", "schaf", "mutton"),
    }
    for option in options:
        for word in aliases.get(option, (_normalise(option),)):
            if any(_matches(tag, word) for tag in tags):
                return option
    return ""


def subcategories_for(category: str) -> tuple[str, ...]:
    """Unterkategorien einer Kategorie - unabhaengig von Umlautschreibweise.

    Bestehende Datenbanken tragen die Kategorie teils noch umschrieben
    ("Suesses & Snacks"), deshalb wird normalisiert verglichen.
    """
    wanted = _key(category)
    for name, options in SUBCATEGORIES.items():
        if _key(name) == wanted:
            return options
    return ()


def display_name(name: str, subcategory: str = "") -> str:
    """Anzeigename fuer Bestand und Etikett: "Filet - Schwein".

    An einer Stelle, damit Bildschirm, Web-Interface und Etikett nicht
    auseinanderlaufen - im Vorgaengerprojekt stand genau so etwas an drei
    Stellen leicht verschieden.
    """
    name = (name or "").strip()
    subcategory = (subcategory or "").strip()
    if not subcategory or subcategory.lower() in name.lower():
        return name
    return f"{name} - {subcategory}"
