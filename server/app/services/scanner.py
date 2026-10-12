"""Was die Telemetrie des Handscanners bedeutet - in Worten.

Die Firmware meldet den Zustand der Kopplung und den Grund der letzten
Trennung als HCI-Code. Fuer die Frage "warum ist der Scanner schon wieder
weg?" ist der Code entscheidend: 0x13 heisst, der Scanner hat selbst
aufgelegt (meist sein eigenes Energiesparen - einstellbar nur am Scanner),
0x08 ein Funkloch, 0x16 das Terminal (die eingestellte Verbindungsdauer).
"""

from __future__ import annotations

ZUSTAENDE = {
    "verbunden": "verbunden",
    "wartet": "wartet auf den Scanner",
    "sucht": "sucht einen Scanner",
    "verbindet": "verbindet …",
    "pausiert": "kurz pausiert",
    "aus": "Bluetooth aus",
}

TRENNGRUENDE = {
    0x05: "Anmeldung abgelehnt – Scanner neu koppeln",
    0x06: "Schlüssel fehlt – Scanner neu koppeln",
    0x08: "Funkverbindung abgerissen (Reichweite oder Störung)",
    0x13: "vom Scanner beendet – meist dessen eigenes Energiesparen",
    0x14: "vom Scanner beendet (zu wenig Ressourcen)",
    0x15: "Scanner hat sich abgeschaltet",
    0x16: "vom Terminal beendet (Verbindungsdauer abgelaufen oder von Hand)",
    0x22: "Zeitüberschreitung im Funkprotokoll",
    0x28: "Zeitüberschreitung im Funkprotokoll",
    0x3B: "Verbindungsparameter abgelehnt",
    0x3D: "Verschlüsselung fehlgeschlagen – Scanner neu koppeln",
    0x3E: "Verbindungsaufbau gescheitert",
}


def zustand(scanner: dict) -> str:
    if scanner.get("connected"):
        return "verbunden"
    return ZUSTAENDE.get(str(scanner.get("state") or ""), "nicht verbunden")


def trenngrund(code) -> str:
    try:
        wert = int(code or 0)
    except (TypeError, ValueError):
        return ""
    if wert <= 0:
        return ""
    return TRENNGRUENDE.get(wert, f"Code 0x{wert:02X}")


def beschreiben(scanner: dict) -> dict:
    """Die Telemetrie um lesbare Felder ergaenzen (fuer Web und Terminal)."""
    aus = dict(scanner or {})
    aus["state_text"] = zustand(aus)
    aus["reason_text"] = trenngrund(aus.get("reason"))
    return aus
