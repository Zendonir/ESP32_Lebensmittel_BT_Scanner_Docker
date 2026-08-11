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
// (SD_MMC.setPins(CLK, CMD, D0)). Keine CS-Leitung - SDMMC ist kein SPI.
// Beide Boardvarianten benutzen dieselben Pins.
//
// ACHTUNG, hier lag ich schon einmal falsch: im Handbuch der 3.5B stehen
// GPIO 9, 10 und 11 auch auf der Stiftleiste. Daraus folgt *nicht*, dass dort
// keine SD-Karte haengt - die Leitungen sind auf beides gefuehrt. Wer das
// verwechselt und den Kartenleser abschaltet, nimmt dem Geraet die
// Wiederherstellung der Zugangsdaten nach einem Werksreset.
#define SD_ENABLED 1
#define SD_CLK 11
#define SD_CMD 10
#define SD_D0  9

// ---- Drucker (ESC/POS ueber UART) ------------------------------------------
#define PRINTER_TX   44
#define PRINTER_RX   43
#define PRINTER_BAUD 9600

// ---- Ton (ES8311-Codec ueber I2S) ------------------------------------------
// Das Board hat keinen Piezo. Toene laufen ueber den ES8311-Audiocodec
// (I2C 0x18), dessen Spannungsschienen der AXP2101 (I2C 0x34) liefert und
// dessen Endstufe am TCA9554 haengt (EXIO7, siehe Board::setAmplifier).
//
// AUDIO_ENABLED 0 schaltet den ganzen Zweig ab; die Firmware laeuft dann
// unveraendert, nur eben stumm.
#if defined(BOARD_WAVESHARE_35B)
// Die 3.5B hat dieselben Bausteine (ES8311, AXP2101, TCA9554), nur ist ihre
// I2S-Belegung nicht dokumentiert. Aus dem Handbuch laesst sich eingrenzen:
// belegt sind 0 (BOOT), 1-5 (LCD QSPI), 6 (Beleuchtung), 7/8 (I2C),
// 12 (LCD CS), 19/20 (USB), 26-37 (Flash und PSRAM), 43/44 (UART); auf der
// Stiftleiste liegen 9, 10, 11, 17, 18, 21, 38-42 und 45-48. Uebrig bleiben
// genau 13, 14, 15 und 16 - und das ist exakt die Belegung der 3.5
// (BCLK 13, DIN 14, LRCK 15, DOUT 16).
//
// Offen bleibt der Takt: auf der 3.5 ist MCLK GPIO12, das ist hier die
// Auswahlleitung des Displays. Die 3.5B muss den Codec also ohne eigenen
// MCLK betreiben (Takt aus SCLK), was eine andere Registerfolge braucht als
// die portierte. Das laesst sich nicht erraten, deshalb bleibt es aus.
//
// Zum Freischalten fehlt genau eine Angabe aus dem Schaltplan: ob und an
// welchem Pin MCLK liegt. Danach hier eintragen und AUDIO_ENABLED auf 1.
#define AUDIO_ENABLED 0
#define I2S_MCLK -1
#define I2S_BCLK 13
#define I2S_LRCK 15
#define I2S_DOUT 16
#else
#define AUDIO_ENABLED 1
#define I2S_MCLK 12
#define I2S_BCLK 13
#define I2S_LRCK 15
#define I2S_DOUT 16
#endif

// ---- Signalton -------------------------------------------------------------
// Ausweichweg fuer einen nachtraeglich angeloeteten Piezo. Standardmaessig AUS
// (-1): das Board hat keinen, die Toene laufen ueber den ES8311-Codec (siehe
// oben). Wird hier ein Pin gesetzt, benutzt Buzzer ihn nur, wenn sich der
// Codec nicht meldet.
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
// Wiederverbindung nach Abriss. Jeder vergebliche Versuch haelt den Loop um
// WEBSOCKETS_TCP_TIMEOUT (siehe platformio.ini) auf, weil der TCP-Aufbau in
// ws.loop() blockierend ist - der Abstand bestimmt also mit, wie fluessig sich
// das Geraet bei ausgefallenem Server noch bedienen laesst.
#define WS_RECONNECT_MS      5000
#define TELEMETRY_INTERVAL_MS 30000
#define BATTERY_POLL_MS      300000 // Scanner-Akku alle 5 Minuten
#define WDT_TIMEOUT_S        30
#define TOUCH_POLL_MS        20
#define BLE_CONNECT_TIMEOUT_MS 30000
#define BLE_SCAN_DURATION_MS   10000  // Dauer eines Suchlaufs (blockiert nicht)

// Der Scanner bleibt nach dem letzten Barcode noch so lange verbunden; jeder
// Scan setzt die Zeit zurueck. Danach wird getrennt, damit der Handscanner
// schlafen und seinen Akku schonen kann.
#define BLE_IDLE_TIMEOUT_MS 600000        // 10 Minuten

// Nach dem Trennen wegen Untaetigkeit kurz nicht neu verbinden. Ein Scanner,
// der sofort wieder wirbt, haenge sonst augenblicklich wieder dran und das
// Trennen haette nichts gebracht. Kurz genug, dass es niemand merkt, der den
// Scanner gerade wieder in die Hand nimmt.
#define BLE_IDLE_COOLDOWN_MS 15000

// So lange darf der Controller unbeaufsichtigt auf den bekannten Scanner
// warten. Danach einmal regulaer suchen - fuer den Fall, dass die gespeicherte
// Kopplung nicht mehr stimmt (anderer oder zurueckgesetzter Scanner). Ohne
// diesen Ausweg waere ein veralteter Eintrag eine Sackgasse.
#define BLE_AUTOCONNECT_RETRY_MS 180000   // 3 Minuten

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