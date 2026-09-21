"""Nachsehen, ob eine neuere Fassung des Servers bereitsteht.

Die Frage "laeuft hier eigentlich der aktuelle Stand?" war bisher nicht zu
beantworten: im System-Panel stand eine Zeichenkette aus Zweigname und
vollstaendigem Commit-Hash, und womit man sie haette vergleichen sollen, stand
nirgends.

Hier wird sie beantwortet - aber nur beantwortet. Aktualisiert wird nichts:
ein Dienst, der sich selbst ersetzt, braucht einen Rueckweg fuer den Fall,
dass die neue Fassung nicht startet, und den gibt es noch nicht.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# Die GitHub-Schnittstelle laesst ohne Anmeldung 60 Abfragen je Stunde und IP
# zu. Das Ergebnis aendert sich ohnehin selten, also wird es zwischengehalten -
# sonst verbraucht ein offener Browser-Tab mit Minutenaktualisierung das
# Kontingent und die Antwort ist danach fuer alle eine Fehlermeldung.
CACHE_SECONDS = 600
_cache: dict[str, Any] = {}
_cached_at = 0.0


def current() -> dict[str, str]:
    """Was gerade laeuft - aus der Umgebung, gesetzt beim Bau des Abbilds."""
    version = os.getenv("APP_VERSION", "dev")
    commit = os.getenv("APP_COMMIT", "")
    return {
        "version": version,
        "commit": commit,
        "commit_kurz": commit[:7],
        "gebaut": os.getenv("APP_BUILT", ""),
        # Bei einem Entwicklungsstand steht hier "dev", und dann gibt es nichts
        # zu vergleichen. Das ist kein Fehler, sondern der Normalfall beim
        # Selberbauen - nur soll es dann auch so dastehen.
        "aus_abbild": version != "dev",
    }


def _ist_tag(version: str) -> bool:
    """Sieht die Version nach einem Release-Tag aus (v2.1.0) oder nach einem Zweig?"""
    return version.startswith("v") and version[1:2].isdigit()


async def _github(client: httpx.AsyncClient, pfad: str) -> Any:
    antwort = await client.get(f"https://api.github.com/repos/{settings.app_repo}{pfad}")

    # Die drei Faelle, die wirklich vorkommen, verstaendlich beantworten. Ein
    # durchgereichtes "Client error '404 Not Found' for url ..." steht sonst
    # im Panel und sagt niemandem, was zu tun ist.
    if antwort.status_code == 403 and "rate limit" in antwort.text.lower():
        raise ValueError(
            "GitHub laesst gerade keine weiteren Abfragen zu (Kontingent "
            "erschoepft, 60 je Stunde ohne Anmeldung). Spaeter wieder "
            "versuchen."
        )
    if antwort.status_code == 404:
        if pfad.startswith("/compare/"):
            raise ValueError(
                "Der Stand dieses Abbilds ist im Ursprung nicht zu finden. "
                "Das passiert, wenn der Zweig, aus dem es gebaut wurde, "
                "inzwischen geloescht oder umbenannt wurde - dann gibt es "
                "nichts mehr, womit sich vergleichen liesse."
            )
        raise ValueError(
            f"In {settings.app_repo} gibt es noch kein Release. Eines "
            "entsteht erst, wenn ein Tag geschoben wird "
            "(git tag v2.1.0 && git push origin v2.1.0)."
        )
    if antwort.status_code == 401:
        raise ValueError(
            f"Kein Zugriff auf {settings.app_repo} - ist das Repository "
            "privat, braucht die Pruefung ein Token (hier nicht vorgesehen)."
        )
    antwort.raise_for_status()
    return antwort.json()


async def check(force: bool = False) -> dict:
    """Aktuellen Stand mit dem Ursprung vergleichen."""
    global _cached_at

    jetzt = time.monotonic()
    if _cache and not force and jetzt - _cached_at < CACHE_SECONDS:
        return {**_cache, "aus_zwischenspeicher": True}

    stand = current()
    ergebnis: dict[str, Any] = {
        "aktuell": stand,
        "update_verfuegbar": False,
        "rueckstand": None,
        "neueste": None,
        "url": f"https://github.com/{settings.app_repo}",
        "aus_zwischenspeicher": False,
    }

    if not stand["aus_abbild"]:
        ergebnis["hinweis"] = (
            "Dieser Server laeuft nicht aus einem gebauten Abbild "
            "(APP_VERSION ist nicht gesetzt). Ein Vergleich ist damit nicht "
            "moeglich - beim Selberbauen sagt git bescheid."
        )
        return ergebnis

    try:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github+json"},
        ) as client:
            if _ist_tag(stand["version"]):
                # Veroeffentlichter Stand: gegen das neueste Release vergleichen.
                release = await _github(client, "/releases/latest")
                neueste = str(release.get("tag_name") or "")
                ergebnis["neueste"] = {
                    "version": neueste,
                    "veroeffentlicht": release.get("published_at", ""),
                    "titel": release.get("name") or neueste,
                }
                ergebnis["url"] = release.get("html_url") or ergebnis["url"]
                ergebnis["update_verfuegbar"] = bool(neueste) and neueste != stand["version"]
            else:
                # Zweigstand: gegen die Spitze desselben Zweiges vergleichen.
                # `compare` sagt nicht nur *ob*, sondern auch *wie weit* -
                # "drei Commits zurueck" ist eine ganz andere Auskunft als
                # "nicht aktuell".
                zweig = stand["version"]
                if not stand["commit"]:
                    raise ValueError(
                        "Das Abbild nennt keinen Commit (APP_COMMIT fehlt) - "
                        "ein aelteres Abbild. Nach dem naechsten Bau geht es."
                    )
                vergleich = await _github(
                    client, f"/compare/{stand['commit']}...{zweig}"
                )
                # `ahead_by`, nicht `behind_by`: verglichen wird
                # base = unser Commit gegen head = die Zweigspitze, und
                # gesucht ist, um wie viele Commits die Spitze *voraus* ist.
                # `behind_by` waere die Gegenrichtung - die steht bei einem
                # ganz normalen Rueckstand auf 0 und meldete damit staendig
                # "aktuell", obwohl es das nicht war.
                rueckstand = int(vergleich.get("ahead_by") or 0)
                ergebnis["rueckstand"] = rueckstand
                ergebnis["update_verfuegbar"] = rueckstand > 0
                ergebnis["url"] = vergleich.get("html_url") or ergebnis["url"]

                # Nur wenn es wirklich etwas Neueres gibt, gibt es auch einen
                # neuesten Commit zu beschreiben. Bei Gleichstand ist die
                # Liste leer - und eine leere Commit-Meldung hat keine erste
                # Zeile, was die Pruefung vorher mit "list index out of range"
                # beendet hat, ausgerechnet im haeufigsten Fall.
                commits = vergleich.get("commits") or []
                if rueckstand > 0 and commits:
                    kopf = commits[-1]
                    meldung = ((kopf.get("commit") or {}).get("message") or "").strip()
                    ergebnis["neueste"] = {
                        "version": zweig,
                        "commit_kurz": str(kopf.get("sha", ""))[:7],
                        "veroeffentlicht": (kopf.get("commit") or {})
                        .get("committer", {})
                        .get("date", ""),
                        "titel": meldung.splitlines()[0][:100] if meldung else "",
                    }
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as fehler:
        log.info("Update-Pruefung fehlgeschlagen: %s", fehler)
        ergebnis["fehler"] = str(fehler)
        return ergebnis

    if ergebnis["update_verfuegbar"]:
        ergebnis["hinweis"] = (
            "Ein neuerer Stand liegt bereit. Der Server aktualisiert sich "
            "nicht selbst: das Abbild neu ziehen und den Container ersetzen "
            f"({settings.app_image}). Die Daten liegen im Volume unter /data "
            "und bleiben dabei unberuehrt."
        )
    else:
        ergebnis["hinweis"] = "Dieser Server laeuft auf dem neuesten Stand."

    _cache.clear()
    _cache.update(ergebnis)
    _cached_at = jetzt
    return ergebnis
