"""Zentrale Konfiguration - ausschliesslich ueber Umgebungsvariablen.

Der Container ist die einzige Quelle der Wahrheit; es gibt bewusst keine
Konfigurationsdatei, die zwischen Neustarts auseinanderlaufen kann.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- Datenbank -----------------------------------------------------
    # Default: SQLite in einem Volume. Postgres wird ueber DATABASE_URL
    # gesetzt (postgresql+asyncpg://user:pass@host/db).
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "sqlite+aiosqlite:////data/lebensmittel.db"
        )
    )

    # --- HTTP ----------------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", ""))

    # --- Auth ----------------------------------------------------------
    # Geraete-Token: das ESP32 meldet sich damit am /ws/device an.
    device_token: str = field(
        default_factory=lambda: os.getenv("DEVICE_TOKEN", "change-me")
    )
    # Optionales Passwort fuer das Web-Interface ("" = offen im LAN).
    ui_password: str = field(default_factory=lambda: os.getenv("UI_PASSWORD", ""))

    # --- Haushalt / Etiketten ------------------------------------------
    household: str = field(
        default_factory=lambda: os.getenv("HOUSEHOLD", "Standard")
    )
    label_prefix: str = field(
        default_factory=lambda: os.getenv("LABEL_PREFIX", "LEB")
    )
    label_paper_chars: int = field(default_factory=lambda: _int("LABEL_PAPER_CHARS", 32))

    # --- Produktdaten ---------------------------------------------------
    openfoodfacts_enabled: bool = field(
        default_factory=lambda: _bool("OPENFOODFACTS_ENABLED", True)
    )
    openfoodfacts_url: str = field(
        default_factory=lambda: os.getenv(
            "OPENFOODFACTS_URL", "https://world.openfoodfacts.org/api/v2/product"
        )
    )
    http_timeout: int = field(default_factory=lambda: _int("HTTP_TIMEOUT", 8))

    # --- Benachrichtigungen ---------------------------------------------
    ntfy_url: str = field(default_factory=lambda: os.getenv("NTFY_URL", ""))
    ntfy_topic: str = field(default_factory=lambda: os.getenv("NTFY_TOPIC", ""))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )
    mqtt_host: str = field(default_factory=lambda: os.getenv("MQTT_HOST", ""))
    mqtt_port: int = field(default_factory=lambda: _int("MQTT_PORT", 1883))
    mqtt_user: str = field(default_factory=lambda: os.getenv("MQTT_USER", ""))
    mqtt_pass: str = field(default_factory=lambda: os.getenv("MQTT_PASS", ""))
    mqtt_prefix: str = field(
        default_factory=lambda: os.getenv("MQTT_PREFIX", "lebensmittel")
    )

    # --- Ablauf-Ueberwachung ---------------------------------------------
    expiry_warn_days: int = field(default_factory=lambda: _int("EXPIRY_WARN_DAYS", 3))
    expiry_check_hour: int = field(default_factory=lambda: _int("EXPIRY_CHECK_HOUR", 8))

    # --- Firmware-Verteilung (OTA) ---------------------------------------
    # Ablage der Abbilder. Liegt im selben Volume wie die Datenbank, damit ein
    # hochgeladenes Abbild einen Containerneustart ueberlebt.
    firmware_dir: str = field(
        default_factory=lambda: os.getenv("FIRMWARE_DIR", "/data/firmware")
    )
    # Repository, aus dessen neuestem Release die Abbilder geholt werden.
    firmware_repo: str = field(
        default_factory=lambda: os.getenv(
            "FIRMWARE_REPO", "Zendonir/ESP32_Lebensmittel_BT_Scanner_Docker"
        )
    )

    # --- Betrieb ---------------------------------------------------------
    timezone: str = field(default_factory=lambda: os.getenv("TZ", "Europe/Berlin"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    # Ein ausgelagerter Artikel bleibt so lange per Re-Scan zurueckbuchbar.
    restore_window_hours: int = field(
        default_factory=lambda: _int("RESTORE_WINDOW_HOURS", 48)
    )


settings = Settings()
