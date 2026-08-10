#include "Board.h"

#include "config.h"

Board board;

// TCA9554 am I2C-Bus
static constexpr uint8_t TCA_ADDR   = 0x20;
static constexpr uint8_t REG_OUTPUT = 0x01;
static constexpr uint8_t REG_CONFIG = 0x03;   // 0 = Ausgang, 1 = Eingang

static constexpr uint8_t BIT_LCD_RST = 0x02;  // EXIO1
static constexpr uint8_t BIT_PA_CTRL = 0x80;  // EXIO7, Verstaerker (aktiv high)
static constexpr uint8_t OUTPUT_BITS = BIT_LCD_RST | BIT_PA_CTRL;

uint8_t Board::write(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(TCA_ADDR);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission();
}

uint8_t Board::read(uint8_t reg) {
    Wire.beginTransmission(TCA_ADDR);
    Wire.write(reg);
    Wire.endTransmission(false);
    Wire.requestFrom(TCA_ADDR, (uint8_t)1);
    return Wire.available() ? Wire.read() : 0xFF;
}

bool Board::begin() {
    Wire.begin(TOUCH_SDA, TOUCH_SCL, I2C_FREQ);
    // Harte Obergrenze pro Transfer: zieht ein gestoerter Touchcontroller SDA
    // dauerhaft auf Masse, blockiert sonst der ganze Loop und die Bedienung
    // wirkt eingefroren, obwohl die Firmware laeuft.
    Wire.setTimeOut(50);

    Wire.beginTransmission(TCA_ADDR);
    if (Wire.endTransmission() != 0) {
        log_e("TCA9554 (0x%02X) antwortet nicht - Display bleibt im Reset", TCA_ADDR);
        _expanderOk = false;
        return false;
    }
    _expanderOk = true;

    // EXIO1 und EXIO7 als Ausgang, alles andere bleibt Eingang.
    write(REG_CONFIG, (uint8_t)~OUTPUT_BITS);
    write(REG_OUTPUT, 0xFF);
    delay(10);

    // Reset-Impuls fuer das Display; der Verstaerker bleibt dabei oben.
    write(REG_OUTPUT, BIT_PA_CTRL);
    delay(10);
    write(REG_OUTPUT, 0xFF);
    _outputs = 0xFF;

    // Der ST7796 braucht nach dem Reset rund 120 ms, bis er Befehle annimmt.
    delay(200);

    log_i("TCA9554 bereit (OUT=0x%02X CFG=0x%02X)", read(REG_OUTPUT), read(REG_CONFIG));
    return true;
}

void Board::setAmplifier(bool on) {
    if (!_expanderOk) return;
    _outputs = on ? (uint8_t)(_outputs | BIT_PA_CTRL)
                  : (uint8_t)(_outputs & ~BIT_PA_CTRL);
    write(REG_OUTPUT, _outputs);
}
