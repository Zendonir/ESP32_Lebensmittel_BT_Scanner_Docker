#include "Audio.h"

#include <math.h>

#include "core/Board.h"

Audio audio;

#if AUDIO_ENABLED

#include <Wire.h>
#include <driver/i2s.h>

static constexpr uint8_t ES8311_ADDR = 0x18;
static constexpr uint8_t AXP2101_ADDR = 0x34;
static constexpr i2s_port_t I2S_PORT = I2S_NUM_1;

static void es_write(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(ES8311_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

static void axp_write(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(AXP2101_ADDR);
    Wire.write(reg);
    Wire.write(value);
    Wire.endTransmission();
}

static uint8_t axp_read(uint8_t reg) {
    Wire.beginTransmission(AXP2101_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    if (Wire.requestFrom((uint8_t)AXP2101_ADDR, (uint8_t)1) != 1) return 0xFF;
    return Wire.read();
}

// Der Codec haengt an drei Schienen des AXP2101 - ohne die bleibt er stumm und
// meldet sich auch auf dem I2C-Bus nicht. Spannung = (mV - 500) / 100.
static void enableAudioPower() {
    axp_write(0x92, (3300 - 500) / 100);   // ALDO1 3,3 V - AVDD
    axp_write(0x96, (1500 - 500) / 100);   // BLDO1 1,5 V - DVDD
    axp_write(0x97, (2800 - 500) / 100);   // BLDO2 2,8 V
    const uint8_t enabled = axp_read(0x90);
    if (enabled != 0xFF) axp_write(0x90, enabled | 0x31);
    delay(20);
}

// Registerfolge unveraendert aus dem Vorgaengerprojekt: 48 kHz, 16 Bit,
// I2S-Standard, MCLK = 256 x fs = 12,288 MHz. Die Werte stammen aus der
// Koeffiziententabelle des ESP-BSP - an einzelnen Registern zu drehen fuehrt
// zuverlaessig zu Rauschen oder Stille.
static void es8311_init() {
    es_write(0x00, 0x1F);
    delay(30);
    es_write(0x00, 0x80);   // CSM_ON
    delay(10);

    es_write(0x01, 0x3F);
    es_write(0x02, 0x00);
    es_write(0x03, 0x10);
    es_write(0x04, 0x10);
    es_write(0x05, 0x00);
    es_write(0x06, 0x03);
    es_write(0x07, 0x00);
    es_write(0x08, 0xFF);

    es_write(0x09, 0x0C);   // I2S, 16 Bit
    es_write(0x0A, 0x0C);

    es_write(0x0D, 0x01);
    es_write(0x0E, 0x02);
    es_write(0x0F, 0x44);
    es_write(0x10, 0x1C);
    es_write(0x11, 0x00);
    es_write(0x12, 0x00);
    es_write(0x13, 0x10);
    es_write(0x14, 0x1A);
    es_write(0x15, 0x00);
    es_write(0x16, 0x24);
    es_write(0x17, 0xBF);

    es_write(0x1A, 0xA0);
    es_write(0x1B, 0x00);
    es_write(0x1C, 0xF8);
    es_write(0x1D, 0x3C);
    es_write(0x1E, 0x28);
    es_write(0x1F, 0x00);
    es_write(0x20, 0x00);
    es_write(0x21, 0x00);
    es_write(0x22, 0x00);
    es_write(0x23, 0x00);

    es_write(0x31, 0x00);   // Stummschaltung aus
    es_write(0x32, 0xBF);   // Lautstaerke, setVolume() korrigiert gleich
    es_write(0x37, 0x08);
    es_write(0x45, 0x00);
    delay(50);
}

bool Audio::begin() {
    Wire.beginTransmission(ES8311_ADDR);
    if (Wire.endTransmission() != 0) {
        // Kann sein, dass die Schienen noch aus sind - erst einschalten,
        // dann noch einmal nachsehen.
        enableAudioPower();
        Wire.beginTransmission(ES8311_ADDR);
        if (Wire.endTransmission() != 0) {
            log_w("Kein ES8311 gefunden (0x%02X) - Geraet bleibt stumm", ES8311_ADDR);
            return false;
        }
    } else {
        enableAudioPower();
    }

    es8311_init();

    i2s_config_t cfg = {};
    cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
    cfg.sample_rate          = SAMPLE_RATE;
    cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags     = 0;
    cfg.dma_desc_num         = 8;
    cfg.dma_frame_num        = 256;     // 8 x 256 Bilder = rund 43 ms Vorlauf
    cfg.use_apll             = true;
    cfg.fixed_mclk           = 12288000;
    cfg.mclk_multiple        = I2S_MCLK_MULTIPLE_256;

    if (i2s_driver_install(I2S_PORT, &cfg, 0, nullptr) != ESP_OK) {
        log_w("I2S liess sich nicht einrichten - Geraet bleibt stumm");
        return false;
    }

    i2s_pin_config_t pins = {};
    pins.mck_io_num   = I2S_MCLK;
    pins.bck_io_num   = I2S_BCLK;
    pins.ws_io_num    = I2S_LRCK;
    pins.data_out_num = I2S_DOUT;
    pins.data_in_num  = I2S_PIN_NO_CHANGE;
    if (i2s_set_pin(I2S_PORT, &pins) != ESP_OK) {
        i2s_driver_uninstall(I2S_PORT);
        log_w("I2S-Pins liessen sich nicht setzen - Geraet bleibt stumm");
        return false;
    }

    i2s_zero_dma_buffer(I2S_PORT);
    _ok = true;
    setVolume(_volume);
    log_i("Ton bereit (ES8311, 48 kHz)");
    return true;
}

void Audio::setVolume(uint8_t percent) {
    _volume = constrain(percent, 0, 100);
    if (!_ok) return;
    // Register 0x32: 0x00 stumm bis 0xFF volle Lautstaerke.
    es_write(0x32, (uint8_t)(_volume * 255 / 100));
}

void Audio::playTone(uint16_t hz, uint16_t ms) {
    if (!_ok || hz == 0 || ms == 0) return;

    _framesLeft   = (uint32_t)SAMPLE_RATE * ms / 1000;
    _step         = 2.0f * (float)M_PI * (float)hz / (float)SAMPLE_RATE;
    _phase        = 0.0f;
    _ramp         = 0;
    _amp          = 32767.0f * 0.6f;   // Feinheiten macht der Codec
    _stagingBytes = 0;
    _stagingSent  = 0;

    if (!_ampOn) {
        board.setAmplifier(true);
        _ampOn = true;
    }
}

void Audio::generate() {
    const size_t frames = (_framesLeft < CHUNK) ? (size_t)_framesLeft : CHUNK;
    for (size_t i = 0; i < frames; i++) {
        // Weich einblenden - ein harter Sprung am Tonanfang knackt hoerbar.
        const float envelope = (_ramp < RAMP_FRAMES) ? (float)_ramp / RAMP_FRAMES : 1.0f;
        if (_ramp < RAMP_FRAMES) _ramp++;

        const int16_t sample = (int16_t)(sinf(_phase) * _amp * envelope);
        _staging[i * 2]     = sample;
        _staging[i * 2 + 1] = sample;

        _phase += _step;
        if (_phase >= 2.0f * (float)M_PI) _phase -= 2.0f * (float)M_PI;
    }
    _framesLeft -= frames;
    _stagingBytes = frames * 4;
    _stagingSent  = 0;
}

void Audio::loop() {
    if (!_ok) return;

    if (_stagingBytes == 0 && _framesLeft > 0) generate();

    if (_stagingBytes > 0) {
        size_t written = 0;
        // Zeitgrenze 0: schreibt nur, was gerade in den DMA-Puffer passt, und
        // kehrt sofort zurueck. Genau deshalb darf das hier im Hauptloop
        // stehen, ohne die Bedienung aufzuhalten.
        i2s_write(I2S_PORT, (const uint8_t *)_staging + _stagingSent,
                  _stagingBytes - _stagingSent, &written, 0);
        _stagingSent += written;
        if (_stagingSent >= _stagingBytes) _stagingBytes = 0;
        _silentSince = 0;
        return;
    }

    // Nichts mehr zu spielen: die Endstufe erst nach einer kurzen Pause
    // abschalten. Sofortiges Abschalten knackt, und bei einer Tonfolge waere
    // sie zwischen den Toenen staendig an und aus.
    if (_ampOn) {
        const uint32_t now = millis();
        if (_silentSince == 0) {
            _silentSince = now;
        } else if (now - _silentSince > 400) {
            board.setAmplifier(false);
            _ampOn = false;
            _silentSince = 0;
        }
    }
}

#else   // AUDIO_ENABLED == 0

// Boardvariante ohne gesicherte Audiobelegung: alles leer, damit der uebrige
// Code unveraendert bleibt und nicht an jeder Aufrufstelle geprueft werden muss.
bool Audio::begin() { return false; }
void Audio::setVolume(uint8_t percent) { _volume = percent; }
void Audio::playTone(uint16_t, uint16_t) {}
void Audio::generate() {}
void Audio::loop() {}

#endif
