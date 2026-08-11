"""Ablage und Verteilung der Terminal-Firmware (OTA).

Der Server haelt die Abbilder, das Terminal laedt sie bei sich ab - dieselbe
Richtung wie beim Rest des Systems. Das Geraet braucht damit weder Internet
noch Zertifikate, und welche Version wo laeuft, entscheidet der Server.

Ein Abbild ist die reine `firmware.bin` (Offset 0x10000), nicht die
`firmware.factory.bin`: Letztere enthaelt Bootloader und Partitionstabelle und
laesst sich nur ueber USB einspielen, nicht per OTA.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# Die beiden Boardvarianten, die die CI baut. Der Name ist zugleich der
# Dateiname im Release (firmware-<board>.bin) und der, den das Geraet in
# `hello` meldet.
BOARDS = ("35", "35b")

# Ein Abbild, das kleiner ist, kann keine gueltige Anwendung sein - und eines,
# das groesser ist, passt nicht in einen App-Slot (7,5 MB laut
# partitions_ota.csv). Beides vor dem Speichern abfangen, nicht erst auf dem
# Geraet.
MIN_SIZE = 256 * 1024
MAX_SIZE = 7 * 1024 * 1024


def _dir() -> Path:
    path = Path(settings.firmware_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bin_path(board: str) -> Path:
    return _dir() / f"firmware-{board}.bin"


def _meta_path(board: str) -> Path:
    return _dir() / f"firmware-{board}.json"


def store(board: str, data: bytes, version: str, source: str) -> dict:
    """Abbild ablegen und seine Kenndaten zurueckgeben."""
    if board not in BOARDS:
        raise ValueError(f"Unbekannte Boardvariante: {board}")
    if not (MIN_SIZE <= len(data) <= MAX_SIZE):
        raise ValueError(
            f"Abbild ist {len(data)} Byte gross - erwartet werden "
            f"{MIN_SIZE} bis {MAX_SIZE} Byte"
        )
    # Ein ESP32-Anwendungsabbild beginnt immer mit 0xE9. Das faengt die
    # haeufigste Verwechslung ab: eine hochgeladene .factory.bin oder eine
    # ganz andere Datei.
    if data[0] != 0xE9:
        raise ValueError(
            "Das ist kein ESP32-Anwendungsabbild (erwartet wird firmware.bin, "
            "nicht firmware.factory.bin)"
        )

    meta = {
        "board": board,
        "version": version,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source": source,
    }
    _bin_path(board).write_bytes(data)
    _meta_path(board).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("Firmware abgelegt: %s %s (%d Byte)", board, version, len(data))
    return meta


def meta(board: str) -> dict | None:
    path = _meta_path(board)
    if not path.exists() or not _bin_path(board).exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def available() -> list[dict]:
    return [m for m in (meta(b) for b in BOARDS) if m is not None]


def path_for(board: str) -> Path | None:
    path = _bin_path(board)
    return path if path.exists() else None


async def fetch_from_github(tag: str = "") -> list[dict]:
    """Abbilder aus einem GitHub-Release holen (ohne Angabe: das neueste).

    Braucht Internetzugang des Servers - nicht des Terminals. Schlaegt das
    fehl, bleibt der manuelle Upload als Weg.
    """
    base = f"https://api.github.com/repos/{settings.firmware_repo}/releases"
    url = f"{base}/tags/{tag}" if tag else f"{base}/latest"

    async with httpx.AsyncClient(
        timeout=60, follow_redirects=True, headers={"Accept": "application/vnd.github+json"}
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        release = response.json()

        version = str(release.get("tag_name") or "unbekannt")
        assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", [])}

        stored: list[dict] = []
        for board in BOARDS:
            name = f"firmware-{board}.bin"
            link = assets.get(name)
            if not link:
                log.warning("Release %s enthaelt %s nicht", version, name)
                continue
            binary = await client.get(link)
            binary.raise_for_status()
            stored.append(store(board, binary.content, version, f"github:{version}"))

    if not stored:
        raise ValueError(f"Release {version} enthaelt keine passenden Abbilder")
    return stored
