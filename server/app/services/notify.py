"""Benachrichtigungen: ntfy, Telegram, MQTT.

Alles feuert vom Server, nicht vom Geraet - kein TLS-Stack und keine
Broker-Reconnect-Logik mehr in der Firmware.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)


async def _ntfy(title: str, message: str, priority: str = "default") -> bool:
    if not (settings.ntfy_url and settings.ntfy_topic):
        return False
    url = f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}"
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.post(
                url,
                content=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8").decode("latin-1", "ignore"),
                    "Priority": priority,
                },
            )
        return resp.status_code < 300
    except httpx.HTTPError as exc:
        log.warning("ntfy fehlgeschlagen: %s", exc)
        return False


async def _telegram(title: str, message: str) -> bool:
    if not (settings.telegram_token and settings.telegram_chat_id):
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": f"*{title}*\n{message}",
                    "parse_mode": "Markdown",
                },
            )
        return resp.status_code < 300
    except httpx.HTTPError as exc:
        log.warning("Telegram fehlgeschlagen: %s", exc)
        return False


async def send(title: str, message: str, priority: str = "default") -> dict[str, bool]:
    """An alle konfigurierten Kanaele gleichzeitig senden."""
    ntfy_ok, tg_ok = await asyncio.gather(
        _ntfy(title, message, priority), _telegram(title, message)
    )
    return {"ntfy": ntfy_ok, "telegram": tg_ok}


# --------------------------------------------------------------------------
# MQTT (optional, fuer Home Assistant)
# --------------------------------------------------------------------------
_mqtt_client = None


async def mqtt_publish(topic: str, payload: dict | str, retain: bool = False) -> bool:
    """Feuert-und-vergisst. Fehlt der Broker, ist das kein Fehlerfall."""
    global _mqtt_client
    if not settings.mqtt_host:
        return False
    try:
        import aiomqtt
    except ImportError:
        return False

    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    full_topic = f"{settings.mqtt_prefix}/{topic.lstrip('/')}"
    try:
        async with aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_user or None,
            password=settings.mqtt_pass or None,
            timeout=float(settings.http_timeout),
        ) as client:
            await client.publish(full_topic, body, retain=retain)
        return True
    except Exception as exc:  # aiomqtt wirft breit gefaecherte Fehler
        log.warning("MQTT publish fehlgeschlagen (%s): %s", full_topic, exc)
        return False


def configured_channels() -> dict[str, bool]:
    return {
        "ntfy": bool(settings.ntfy_url and settings.ntfy_topic),
        "telegram": bool(settings.telegram_token and settings.telegram_chat_id),
        "mqtt": bool(settings.mqtt_host),
    }
