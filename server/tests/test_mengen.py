import pytest

from app.services import mengen


@pytest.mark.parametrize("angabe, erwartet", [
    ("1l", (1000, "ml")),
    ("1 L", (1000, "ml")),
    ("0,5 l", (500, "ml")),
    ("1.5 kg", (1500, "g")),
    ("500g e", (500, "g")),
    ("75 cl", (750, "ml")),
    ("6 x 330 ml", (330, "ml")),
    ("250 g (2 x 125 g)", (250, "g")),
    ("", None),
    ("1 Stück", None),
    ("Glas", None),
])
def test_aus_angabe(angabe, erwartet):
    assert mengen.aus_angabe(angabe) == erwartet


@pytest.mark.parametrize("menge, einheit, text", [
    (1500, "g", "1,5 kg"),
    (1000, "ml", "1 l"),
    (330, "ml", "330 ml"),
    (999, "g", "999 g"),
    (2, "kg", "2 kg"),
    (0.5, "l", "500 ml"),
    (3, "St.", "3 St."),
    (1, "", "1"),
])
def test_text(menge, einheit, text):
    assert mengen.text(menge, einheit) == text


def test_normalisieren():
    assert mengen.normalisieren(1.5, "kg") == (1500, "g")
    assert mengen.normalisieren(2, "l") == (2000, "ml")
    assert mengen.normalisieren(4, "Stk") == (4, "St.")
    assert mengen.einheit("kg") == "g"
    assert mengen.einheit("") == ""


def test_api_speichert_g_und_ml():
    import uuid

    from fastapi.testclient import TestClient

    from app.main import app

    name = f"Mehl {uuid.uuid4().hex[:6]}"
    with TestClient(app) as c:
        tpl = c.post("/api/templates", json={"name": name, "unit": "kg"}).json()
        assert tpl["unit"] == "g"
        r = c.post("/api/labels", json={"name": name, "quantity": 1.5, "unit": "kg", "print": False})
        assert r.status_code == 201
        item = c.get(f"/api/inventory/{r.json()['labels'][0]}").json()
        assert (item["quantity"], item["unit"]) == (1500, "g")
        item = c.patch(f"/api/inventory/{item['label']}", json={"quantity": 2, "unit": "l"}).json()
        assert (item["quantity"], item["unit"]) == (2000, "ml")


def test_etikett_zeigt_kg():
    from app.services import labels

    assert labels._quantity({"quantity": 1500, "unit": "g"}) == "1,5 kg"
