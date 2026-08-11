"""Geraete-Protokoll (WebSocket, JSON, eine Nachricht pro Frame).

Leitgedanke: **das Geraet enthaelt keine Anwendungslogik.** Es meldet Ereignisse
(Scan, Tipp, Eingabe, Telemetrie) und rendert Bildschirme, die der Server
vollstaendig beschreibt. Damit ist ein UI-Umbau ein Server-Deploy und kein
OTA-Flash, und ein Geraeteneustart kostet nichts ausser der Reconnect-Zeit.

Geraet -> Server
    hello       {device_id, name, firmware, ip, has_printer}
    scan        {code, source: "ble"|"manual"}
    tap         {screen, item}
    input       {screen, value}
    back        {screen}
    telemetry   {heap, min_heap, psram, rssi, uptime, scanner: {...}}
    print_result{job, ok, error}
    pong        {}

Server -> Geraet
    screen      siehe screen()
    toast       {text, level}
    beep        {pattern}
    print       {job, chars, blocks}
    config      {brightness, beep, idle_seconds}
    ping        {}
    reboot      {}
"""

from __future__ import annotations

from typing import Any, Literal

PROTOCOL_VERSION = 1

ScreenKind = Literal[
    "tiles", "list", "date", "number", "message", "confirm", "text", "keyboard",
    "home", "cards",
]


def screen(
    *,
    screen_id: int,
    kind: ScreenKind,
    title: str,
    subtitle: str = "",
    lines: list[str] | None = None,
    items: list[dict] | None = None,
    buttons: list[dict] | None = None,
    value: Any = None,
    meta: dict | None = None,
    status: dict | None = None,
) -> dict:
    """Ein Bildschirm in geraeteunabhaengiger Form.

    `items`   : [{id, label, sub, color, badge}]  - Kacheln oder Listenzeilen.
                Ein Eintrag mit `header: true` ist eine nicht antippbare
                Gruppenueberschrift innerhalb einer `list`.
    `buttons` : [{id, label, style}]              - feste Fussleiste
    `value`   : Startwert fuer date/number/keyboard
    `meta`    : {min, max, step, unit, presets:[{id,label}], max_len}
                `max_len` begrenzt die Eingabe bei `keyboard` (Standard 40).
                Bei `home`  : {stats:[{label,value,color}], wifi, ble, battery}
                Bei `cards` : {cards:[{title,title_color,status,status_color,
                               lines:[...], button:{id,label,color}}]}
    """
    return {
        "t": "screen",
        "id": screen_id,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "lines": lines or [],
        "items": items or [],
        "buttons": buttons or [],
        "value": value,
        "meta": meta or {},
        "status": status or {},
    }


def toast(text: str, level: str = "info") -> dict:
    return {"t": "toast", "text": text, "level": level}


def beep(pattern: str = "ok") -> dict:
    """pattern: ok | error | warn | scan | print"""
    return {"t": "beep", "pattern": pattern}


def print_job(job_id: int, rendered: dict) -> dict:
    return {
        "t": "print",
        "job": job_id,
        "chars": rendered.get("chars", 32),
        "blocks": rendered.get("blocks", []),
    }


def config(payload: dict) -> dict:
    return {"t": "config", **payload}


def ping() -> dict:
    return {"t": "ping"}


def reboot() -> dict:
    return {"t": "reboot"}


BTN_BACK = {"id": "back", "label": "Zurueck", "style": "ghost"}
BTN_HOME = {"id": "home", "label": "Start", "style": "ghost"}
BTN_OK = {"id": "ok", "label": "OK", "style": "primary"}
BTN_CANCEL = {"id": "cancel", "label": "Abbruch", "style": "ghost"}
