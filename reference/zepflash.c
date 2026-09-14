/* zepflash - complete Zeppelin Air coprocessor (DMP) flash, replicating
 * B&W's "flash-coproc from init" path in one USB session.
 *
 *   1. select_ena(1)                         06 06 01 00
 *   2. coproc_init(startup=5, ready=15)      06 09 00 05 00 0F
 *   3. poll DFU_GETSTATUS iString until 0x12 (READY), up to 300 s
 *   4. select_ena(1); select coproc unit      06 03 00 07   (the "first" path)
 *   5. per 1024-byte chunk: 0x450-byte packet
 *        [0x40 header][0x400 data][0x10 DFU suffix]
 *        header  = 01 00 <start_hi> <start_lo> <end_hi> <end_lo>, start ALWAYS 0
 *        suffix  = 00 00 00 00 10 'D' 'F' 'U' 01 10 FF FF FF FF FF FF
 *   6. sleep(2); select_eot                   06 04 07 00
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <libusb.h>

#define VID 0x03EB
#define PID 0x2FF8
#define IFACE 0
#define BLOCK 0x400
#define HDR 0x40
#define FTR 0x10
#define PKT (HDR + BLOCK + FTR)

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
static void clr(void) { libusb_control_transfer(h, 0x21, 4, 0, IFACE, NULL, 0, 3000); }

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: zepflash <app_DMP.bcd>\n"); return 1; }

    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("open firmware"); return 1; }
    fseek(f, 0, SEEK_END);
    long fsz = ftell(f);
    fseek(f, 0, SEEK_SET);
    long total = (fsz + BLOCK - 1) / BLOCK;
    printf("firmware: %s  %ld bytes  %ld blocks\n\n", argv[1], fsz, total);

    if (libusb_init(NULL) < 0) { fprintf(stderr, "libusb_init failed\n"); return 1; }
    h = libusb_open_device_with_vid_pid(NULL, VID, PID);
    if (!h) { fprintf(stderr, "device not found (must be in bootloader)\n"); return 1; }
    libusb_set_configuration(h, 1);
    if (libusb_claim_interface(h, IFACE) < 0) { fprintf(stderr, "claim failed\n"); return 1; }
    clr();

    unsigned char ena[4] = { 0x06, 0x06, 0x01, 0x00 };
    unsigned char ini[6] = { 0x06, 0x09, 0x00, 0x05, 0x00, 0x0F };
    unsigned char sel[4] = { 0x06, 0x03, 0x00, 0x07 };
    unsigned char eot[4] = { 0x06, 0x04, 0x07, 0x00 };
    unsigned char s[6];

    /* ---- step 1/2: enable + coprocessor init ---- */
    if (dn(ena, 4, 5000) != 4) { fprintf(stderr, "select_ena failed\n"); return 1; }
    stt(s, 5000);

    int ready = 0;
    if (s[5] == 0x12) {
        printf("coprocessor is already READY (skipping init)\n");
        ready = 1;
    } else {
        if (dn(ini, 6, 30000) != 6) { fprintf(stderr, "coproc_init failed\n"); return 1; }
        stt(s, 30000);
        if (s[0]) { fprintf(stderr, "coproc_init rejected bStatus=0x%02x\n", s[0]); return 1; }
        printf("coproc_init accepted; waiting for the coprocessor to boot\n");
    }

    /* ---- step 3: wait for READY ---- */
    int last = -1, stalled = 0;
    for (int i = 0; !ready && i < 300; i++) {
        if (stt(s, 30000) < 0) { fprintf(stderr, "status read failed\n"); return 1; }
        if (s[5] != last) { printf("  t=%3ds  state=0x%02x\n", i, s[5]); last = s[5]; stalled = 0; }
        else if (++stalled >= 60) {
            fprintf(stderr, "\nstate stuck at 0x%02x for 60 s - the coprocessor needs a fresh\n"
                            "power cycle before init will work again.\n", s[5]);
            return 1;
        }
        if (s[5] == 0x12) { ready = 1; break; }
        if (!(s[5] == 0 || s[5] == 1 || s[5] == 2 || s[5] == 3 || s[5] == 0x11)) {
            fprintf(stderr, "unexpected state 0x%02x - aborting\n", s[5]);
            return 1;
        }
        sleep(1);
    }
    if (!ready) { fprintf(stderr, "coprocessor never became ready\n"); return 1; }
    printf("coprocessor READY\n\n");

    /* ---- step 4: select the coprocessor memory unit (first-block path) ---- */
    if (dn(ena, 4, 5000) != 4) { fprintf(stderr, "select_ena(2) failed\n"); return 1; }
    stt(s, 5000);
    if (dn(sel, 4, 10000) != 4) { fprintf(stderr, "select unit DNLOAD failed\n"); return 1; }
    stt(s, 10000);
    if (s[0]) { fprintf(stderr, "select coproc unit rejected bStatus=0x%02x\n", s[0]); return 1; }
    printf("coprocessor memory unit selected; programming\n\n");

    /* ---- step 5: stream the image ---- */
    unsigned char pkt[PKT];
    long blk = 0;
    size_t n;
    unsigned char data[BLOCK];

    while ((n = fread(data, 1, BLOCK, f)) > 0) {
        if (n < BLOCK) memset(data + n, 0, BLOCK - n);

        memset(pkt, 0, PKT);
        unsigned start = 0, end = start + BLOCK - 1;
        pkt[0] = 0x01; pkt[1] = 0x00;
        pkt[2] = (start >> 8) & 0xFF; pkt[3] = start & 0xFF;
        pkt[4] = (end >> 8) & 0xFF;   pkt[5] = end & 0xFF;
        memcpy(pkt + HDR, data, BLOCK);
        unsigned char *ft = pkt + HDR + BLOCK;
        ft[0] = ft[1] = ft[2] = ft[3] = 0x00;
        ft[4] = 0x10; ft[5] = 'D'; ft[6] = 'F'; ft[7] = 'U';
        ft[8] = 0x01; ft[9] = 0x10;
        memset(ft + 10, 0xFF, 6);

        int r = dn(pkt, PKT, 20000);
        if (r != PKT) {
            fprintf(stderr, "\nblock %ld: DNLOAD=%d (%s)\n", blk, r, libusb_error_name(r));
            return 1;
        }
        if (stt(s, 20000) < 0) { fprintf(stderr, "\nblock %ld: status read failed\n", blk); return 1; }
        if (s[0]) { fprintf(stderr, "\nblock %ld: bStatus=0x%02x\n", blk, s[0]); return 1; }

        blk++;
        if (blk % 100 == 0 || blk == total) {
            printf("\r  %ld/%ld blocks (%ld%%)", blk, total, blk * 100 / total);
            fflush(stdout);
        }
    }
    fclose(f);
    printf("\n\nall %ld blocks written; finalising\n", blk);

    /* ---- step 6: end of transfer ---- */
    sleep(2);
    if (dn(eot, 4, 30000) != 4) { fprintf(stderr, "select_eot failed\n"); return 1; }
    stt(s, 30000);
    printf("select_eot bStatus=0x%02x\n", s[0]);
    if (s[0]) return 1;

    printf("\n*** COPROCESSOR FLASH COMPLETE ***\n");
    libusb_release_interface(h, IFACE);
    libusb_close(h);
    libusb_exit(NULL);
    return 0;
}
