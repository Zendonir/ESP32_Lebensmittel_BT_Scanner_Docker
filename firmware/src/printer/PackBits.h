#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// PackBits entpacken - die Gegenstelle zu services/hochformat.packbits().
//
// Ein Kopfbyte n, dann bei 0..127 n+1 woertliche Bytes, bei 129..255 ein
// Byte, das 257-n mal wiederholt wird; 128 bedeutet nichts. So kommen die
// Bildstreifen des Hochformat-Etiketts: ein Etikett ist roh rund 11 KB, fast
// alles davon weiss, und die Nachricht an das Terminal darf hoechstens 14 KB
// gross sein.
//
// Bewusst ohne Arduino-Abhaengigkeit, damit sich der Entpacker auf dem
// Rechner gegen den Packer des Servers pruefen laesst.
//
// Schreibt hoechstens `outCap` Bytes und gibt zurueck, wie viele es waren.
// Eine abgeschnittene oder verfaelschte Eingabe fuehrt nie ueber die Puffer
// hinaus - der Aufrufer sieht es an einer zu kleinen Zahl.
inline size_t packbitsEntpacken(const uint8_t *in, size_t inLen, uint8_t *out, size_t outCap) {
    size_t i = 0, o = 0;
    while (i < inLen && o < outCap) {
        const uint8_t kopf = in[i++];
        if (kopf < 128) {
            size_t n = (size_t)kopf + 1;
            if (n > inLen - i) n = inLen - i;
            if (n > outCap - o) n = outCap - o;
            memcpy(out + o, in + i, n);
            i += n;
            o += n;
        } else if (kopf > 128) {
            if (i >= inLen) break;
            size_t n = 257u - kopf;
            if (n > outCap - o) n = outCap - o;
            memset(out + o, in[i++], n);
            o += n;
        }
    }
    return o;
}
