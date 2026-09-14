/* zepwait - faithful replication of B&W's coprocessor init + readiness wait.
 *
 * Sequence taken from _execute_flash_coproc_from_init (0x18f0):
 *   1. atmel_select_ena(1)                -> 06 06 01 00
 *   2. atmel_coproc_init(startup, ready)  -> 06 09 <s_hi> <s_lo> <r_hi> <r_lo>
 *      (defaults from 0x7680: startup=5, ready=15, in seconds)
 *   3. loop up to 300 times, sleep(1) each time:
 *        state = atmel_get_coproc_status()   == iString byte of DFU_GETSTATUS
 *        state 0,1,2,3,0x11 -> keep waiting
 *        state 0x12         -> READY
 *        anything else      -> error
 *   4. only then is the coprocessor memory unit selectable.
 *
 * Nothing is written to memory.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <libusb.h>

#define VID 0x03EB
#define PID 0x2FF8
#define IFACE 0

static libusb_device_handle *h;
static unsigned transaction;

static int dn(const unsigned char *d, int len, int tmo) {
    return libusb_control_transfer(h, 0x21, 1, transaction++, IFACE,
                                   (unsigned char *)d, len, tmo);
}
static int stt(unsigned char s[6], int tmo) {
    memset(s, 0, 6);
    return libusb_control_transfer(h, 0xA1, 3, 0, IFACE, s, 6, tmo);
}

static const char *statename(int s) {
    switch (s) {
    case 0x00: return "idle / not started";
    case 0x01: return "starting up";
    case 0x02: return "starting up";
    case 0x03: return "waiting for coprocessor";
    case 0x11: return "coprocessor booting";
    case 0x12: return "READY";
    default:   return "unexpected -> error";
    }
}

int main(int argc, char **argv) {
    unsigned a = (argc > 1) ? (unsigned)strtoul(argv[1], 0, 0) : 5;
    unsigned b = (argc > 2) ? (unsigned)strtoul(argv[2], 0, 0) : 15;
    int maxpoll = (argc > 3) ? atoi(argv[3]) : 90;

    if (libusb_init(NULL) < 0) { fprintf(stderr, "libusb_init failed\n"); return 1; }
    h = libusb_open_device_with_vid_pid(NULL, VID, PID);
    if (!h) { fprintf(stderr, "device not found (is it in the bootloader?)\n"); return 1; }
    libusb_set_configuration(h, 1);
    if (libusb_claim_interface(h, IFACE) < 0) { fprintf(stderr, "claim failed\n"); return 1; }
    libusb_control_transfer(h, 0x21, 4, 0, IFACE, NULL, 0, 3000);  /* clear status */

    unsigned char ena[4] = { 0x06, 0x06, 0x01, 0x00 };
    unsigned char ini[6] = { 0x06, 0x09, (a >> 8) & 0xFF, a & 0xFF, (b >> 8) & 0xFF, b & 0xFF };
    unsigned char s[6];

    printf("select_ena ... ");
    if (dn(ena, 4, 5000) != 4) { printf("FAILED\n"); return 1; }
    stt(s, 5000);
    printf("ok\n");

    printf("coproc_init startup=%u ready=%u -> 06 09 %02x %02x %02x %02x ... ",
           a, b, ini[2], ini[3], ini[4], ini[5]);
    int r = dn(ini, 6, 30000);
    if (r != 6) { printf("FAILED (%s)\n", libusb_error_name(r)); return 1; }
    if (stt(s, 30000) < 0) { printf("status read failed\n"); return 1; }
    printf("bStatus=0x%02x\n", s[0]);
    if (s[0] != 0) {
        printf("init rejected; aborting\n");
        return 1;
    }

    printf("\npolling coprocessor state (up to %d s):\n", maxpoll);
    int last = -1;
    for (int i = 0; i < maxpoll; i++) {
        sleep(1);
        if (stt(s, 30000) < 0) { printf("  t=%3ds  status read failed (%s)\n", i + 1, "IO"); break; }
        int state = s[5];                      /* iString == coproc state */
        if (state != last) {
            printf("  t=%3ds  state=0x%02x  %-26s (bStatus=0x%02x bState=0x%02x)\n",
                   i + 1, state, statename(state), s[0], s[4]);
            last = state;
        }
        if (state == 0x12) {
            printf("\n*** COPROCESSOR REPORTS READY ***\n");
            unsigned char sel[4] = { 0x06, 0x03, 0x00, 0x07 };
            dn(sel, 4, 10000);
            stt(s, 10000);
            printf("select coproc unit 7: bStatus=0x%02x %s\n",
                   s[0], s[0] ? "(still failing)" : "OK - COPROCESSOR IS FLASHABLE");
            return s[0] ? 2 : 0;
        }
        if (!(state == 0 || state == 1 || state == 2 || state == 3 || state == 0x11)) {
            printf("\nstate 0x%02x is an error state; the app would abort here\n", state);
            return 3;
        }
    }
    printf("\nnever reached ready state\n");
    return 4;
}
