#pragma once

// Wird vom Pre-Build-Skript scripts/version.py gesetzt.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "dev"
#endif

// ============================================================================
// Waveshare ESP32-S3-Touch-LCD-3.5 - Pinbelegung
// Uebernommen aus dem Vorgaengerprojekt; die Werte sind am Geraet verifiziert.
// ============================================================================

// ---- Display (ST7796, SPI) -------------------------------------------------
// LCD_CS haengt nicht an einem direkten GPIO, LCD_RST am TCA9554-Expander.
// Beide bleiben deshalb auf -1.
#define LCD_MOSI 1
#define LCD_MISO 2
#define LCD_DC   3
#define LCD_CLK  5
#define LCD_BL   6

#define PANEL_WIDTH   320  // native Aufloesung (Hochformat)
#define PANEL_HEIGHT  480
#define UI_WIDTH      480  // Querformat, so wird gezeichnet
#define UI_HEIGHT     320

// ---- Touch (FT6336, I2C) ---------------------------------------------------
// Der Touch-Interrupt laeuft ueber EXIO2 des Expanders, wird also gepollt.
#define TOUCH_SDA  8
#define TOUCH_SCL  7
#define TOUCH_ADDR 0x38
#define I2C_FREQ   400000

// ---- Drucker (ESC/POS ueber UART) ------------------------------------------
#define PRINTER_TX   44
#define PRINTER_RX   43
#define PRINTER_BAUD 9600

// ---- Signalton -------------------------------------------------------------
// Passiver Piezo an einem der freien GPIOs. Der ES8311-Codec des Boards braucht
// I2S + Expander-Freigabe fuer den Verstaerker; fuer Quittungstoene ist das
// unverhaeltnismaessig viel Code und Fehlerflaeche.
#define BUZZER_PIN 35

// ---- Bedienung -------------------------------------------------------------
#define BOOT_BTN 0

// ============================================================================
// Netzwerk
// ============================================================================
#define AP_SSID     "Lebensmittel-Terminal"
#define AP_PASSWORD "12345678"

// Voreinstellungen; zur Laufzeit ueber das WLAN-Portal aenderbar und im NVS
// abgelegt. Der Server wird als Host:Port angegeben, nicht als volle URL -
// die Firmware baut daraus sowohl den HTTP- als auch den WS-Pfad.
#define DEFAULT_SERVER_HOST "lebensmittel.local"
#define DEFAULT_SERVER_PORT 8080
#define DEFAULT_DEVICE_TOKEN ""

#define WS_PATH "/ws/device"

// ============================================================================
// Zeitverhalten
// ============================================================================
#define WS_RECONNECT_MS      3000   // Wiederverbindung nach Abriss
#define TELEMETRY_INTERVAL_MS 30000
#define BATTERY_POLL_MS      300000 // Scanner-Akku alle 5 Minuten
#define WDT_TIMEOUT_S        30
#define TOUCH_POLL_MS        20
#define BLE_CONNECT_TIMEOUT_MS 30000
