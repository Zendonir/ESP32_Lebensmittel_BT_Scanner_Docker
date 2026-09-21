"""Ein Update anstossen - ohne selbst Code auszufuehren.

Ein Container kann sein eigenes Abbild nicht ersetzen; das kann nur, wer den
Docker-Dienst bedient. Auf TrueNAS heisst das Shell, und genau die soll man
sich sparen koennen.

Der naheliegende Weg waere, die neuen Quellen ins Volume zu laden und sich
neu zu starten. Der hat drei Nachteile, die erst beim Hinsehen auffallen:
neue Python-Abhaengigkeiten stecken im Abbild und liessen sich so gar nicht
nachziehen; der Server fuehrte Code aus, den er sich gerade selbst aus dem
Netz geholt hat; und das Abbild, das TrueNAS anzeigt, waere danach ein
anderes als das, was laeuft - beim naechsten Neuanlegen des Containers
verschwaende das Update stillschweigend wieder.

Deshalb der Umweg ueber einen Dienst, der genau dafuer da ist: Watchtower
zieht das neue Abbild und ersetzt den Container. Dieser Server schickt ihm
nur eine Anfrage. Er laedt nichts, packt nichts aus und fuehrt nichts aus -
und das Abbild, das danach laeuft, ist genau das, was auch in der
Registry steht.

Was das kostet: einen zusaetzlichen Container. Was es bringt: Updates
einschliesslich Abhaengigkeiten, und den Rueckweg macht der Abbild-Tag.
"""

from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# Watchtower haelt die Anfrage offen, bis es fertig ist - und beendet dabei
# diesen Container. Die Antwort kommt also nie an. Kurz warten, dann gilt das
# als angestossen und nicht als Fehler.
ANSTOSS_TIMEOUT_S = 8.0


def konfiguriert() -> bool:
    return bool(settings.update_hook_url)


def status() -> dict:
    """Ob der Knopf etwas tun kann - und was sonst zu tun waere."""
    return {
        "moeglich": konfiguriert(),
        "ziel": settings.update_hook_url,
        "abbild": settings.app_image,
        "hinweis": (
            "Bereit. Der Knopf laesst Watchtower das neue Abbild ziehen und "
            "den Container ersetzen."
            if konfiguriert()
            else "Nicht eingerichtet. Ohne einen Dienst, der den Container "
                 "ersetzen darf, kann sich der Server nicht selbst "
                 "aktualisieren - siehe deploy/truenas/watchtower.yaml."
        ),
    }


async def trigger() -> dict:
    """Das Update anstossen.

    Gibt zurueck, sobald der Auftrag draussen ist. Ob er durchlief, laesst
    sich von hier aus nicht feststellen - der eigene Container wird dabei ja
    beendet. Die Oberflaeche wartet deshalb auf das Wiederkommen des Servers,
    statt auf eine Antwort.
    """
    if not konfiguriert():
        raise ValueError(
            "Es ist kein Dienst eingerichtet, der den Container ersetzen "
            "darf. UPDATE_HOOK_URL und UPDATE_HOOK_TOKEN setzen - die "
            "fertige Vorlage steht in deploy/truenas/watchtower.yaml."
        )

    kopfzeilen = {}
    if settings.update_hook_token:
        kopfzeilen["Authorization"] = f"Bearer {settings.update_hook_token}"

    try:
        async with httpx.AsyncClient(timeout=ANSTOSS_TIMEOUT_S) as client:
            antwort = await client.post(settings.update_hook_url, headers=kopfzeilen)
        if antwort.status_code in (401, 403):
            raise ValueError(
                "Der Update-Dienst hat die Anfrage abgelehnt - UPDATE_HOOK_TOKEN "
                "stimmt nicht mit WATCHTOWER_HTTP_API_TOKEN ueberein."
            )
        antwort.raise_for_status()
        log.info("Update angestossen, Antwort %s", antwort.status_code)
        return {
            "ok": True,
            "hinweis": "Update angestossen. Der Server wird gleich ersetzt und "
                       "ist in etwa einer Minute wieder da.",
        }
    except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ConnectError) as fehler:
        # Der Normalfall bei einem gelungenen Update: Watchtower beendet
        # diesen Container, waehrend er noch auf die Antwort wartet. Das als
        # Fehler zu melden waere die haeufigste falsche Fehlermeldung im
        # ganzen Projekt.
        log.info("Verbindung zum Update-Dienst abgerissen (%s) - vermutlich laeuft es", fehler)
        return {
            "ok": True,
            "hinweis": "Update angestossen. Die Verbindung ist dabei abgerissen - "
                       "das ist zu erwarten, wenn der Container gerade ersetzt "
                       "wird. In etwa einer Minute ist der Server wieder da.",
        }
    except httpx.HTTPError as fehler:
        raise ValueError(f"Der Update-Dienst antwortet nicht: {fehler}") from fehler
