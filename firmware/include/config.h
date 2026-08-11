#pragma once

// Wird vom Pre-Build-Skript scripts/version.py gesetzt.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "dev"
#endif

// Exakt eines der beiden PlatformIO-Ziele muss die Boardvariante setzen.
#if !defined(BOARD_WAVESHARE_35) && !defined(BOARD_WAVESHARE_35B)
#error "Boardvariante fehlt: terminal-35 oder terminal-35b bauen"
#endif

// ---- Display (ST7796, SPI) -------------------------------------------------
// LCD_CS haengt nicht an einem direkten GPIO, LCD_RST am TCA9554-Expander.
// Beide bleiben deshalb auf -1.
#define LCD_MOSI 1
#define LCD_MISO 2
#define LCD_DC   3
#define LCD_CLK  5
#define LCD_BL   6

#if defined(BOARD_WAVESHARE_35B)
// 3.5B: AXS15231B ueber QSPI (Herstellerbeispiel 08_gfx_helloworld).
#define LCD_QSPI_CS  12
#define LCD_QSPI_CLK 5
#define LCD_QSPI_D0  1
#define LCD_QSPI_D1  2
#define LCD_QSPI_D2  3
#define LCD_QSPI_D3  4
#endif
#define LCD_RST -1

#define PANEL_WIDTH   320  // native Aufloesung (Hochformat)
#define PANEL_HEIGHT  480
#define UI_WIDTH      480  // Querformat, so wird gezeichnet
#define UI_HEIGHT     320

// ---- Touch (FT6336, I2C) ---------------------------------------------------
// Der Touch-Interrupt laeuft ueber EXIO2 des Expanders, wird also gepollt.
#define TOUCH_SDA  8
#define TOUCH_SCL  7
#if defined(BOARD_WAVESHARE_35B)
#define TOUCH_ADDR 0x3B
#else
#define TOUCH_ADDR 0x38
#endif
#define I2C_FREQ   400000

// ---- SD-Karte (SDMMC, 1-Bit-Modus) -----------------------------------------
// Derselbe physische Kartensteckplatz wie im Vorgaengerprojekt
// (SD_MMC.setPins(CLK, CMD, D0)); beide Boardvarianten teilen sich denselben
// Steckplatz, unabhaengig vom Display. Keine CS-Leitung - SDMMC ist kein SPI.
#define SD_CLK 11
#define SD_CMD 10
#define SD_D0  9

// ---- Drucker (ESC/POS ueber UART) ------------------------------------------
#define PRINTER_TX   44
#define PRINTER_RX   43
#define PRINTER_BAUD 9600

// ---- Signalton -------------------------------------------------------------
// Standardmaessig AUS (-1). Das Board hat keinen Piezo; die Toene wuerden ueber
// den ES8311-Codec laufen, was I2S und die Verstaerkerfreigabe am Expander
// braucht - dafuer ist die Quittung zu wenig wert.
//
// ACHTUNG bei der Wahl eines eigenen Pins: auf dem N16R8-Modul (octal PSRAM,
// board_build.arduino.memory_type = qio_opi) sind **GPIO 33-37 vom PSRAM
// belegt**. Wer dort etwas anschliesst, zerstoert die PSRAM-Anbindung; das
// aeussert sich nicht als Pin-Fehler, sondern als
//   assert failed: block_locate_free ... (block_size(block) >= *size)
// beim naechsten groesseren malloc - also als scheinbar zusammenhangloser
// Absturz. Die Bezeichnung "FREE_GPIO_1..3" fuer 35/36/37 im Vorgaengerprojekt
// war irrefuehrend; benutzt wurden sie dort nie.
//
// Wirklich frei sind auf diesem Board z.B. GPIO 17, 18 oder 21 - vor dem
// Anschluss trotzdem den Schaltplan pruefen.
#define BUZZER_PIN -1

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

// Gestufte Wiederherstellung, wenn WLAN oder Server dauerhaft nicht
// erreichbar sind (falsches Passwort nach einem Routertausch, Server-IP hat
// sich geaendert, ...). Beide Zeiten zaehlen ab dem Beginn desselben
// Ausfalls, nicht ab dem Geraetestart.
//
//   0 ................ SD_RETRY_AFTER_MS ................ PORTAL_FALLBACK_AFTER_MS
//   |  normale WLAN-Neuversuche (alle 20 s)  |  einmalig SD lesen  |  Portal
//
// Ein kurzer Routerneustart faellt in die erste Phase und bleibt unbemerkt;
// erst ein wirklich anhaltender Ausfall fuehrt zum Portal.
#define SD_RETRY_AFTER_MS       90000    // 1,5 Minuten
#define PORTAL_FALLBACK_AFTER_MS 180000  // 3 Minuten