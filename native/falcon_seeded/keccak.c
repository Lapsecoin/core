/* SHAKE256 (FIPS 202) on Keccak-f[1600]. Small and portable (little-endian
 * lanes are assembled byte by byte, so it is correct on any host). */
#include <string.h>
#include "fips202.h"

static const uint64_t RC[24] = {
    0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808aULL,
    0x8000000080008000ULL, 0x000000000000808bULL, 0x0000000080000001ULL,
    0x8000000080008081ULL, 0x8000000000008009ULL, 0x000000000000008aULL,
    0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000aULL,
    0x000000008000808bULL, 0x800000000000008bULL, 0x8000000000008089ULL,
    0x8000000000008003ULL, 0x8000000000008002ULL, 0x8000000000000080ULL,
    0x000000000000800aULL, 0x800000008000000aULL, 0x8000000080008081ULL,
    0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL};
static const int ROTC[24] = {1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
                             27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44};
static const int PILN[24] = {10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
                             15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1};

#define ROL(x, n) (((x) << (n)) | ((x) >> (64 - (n))))

static void keccakf(uint64_t st[25]) {
    uint64_t bc[5], t;
    int r, i, j;
    for (r = 0; r < 24; r++) {
        for (i = 0; i < 5; i++)
            bc[i] = st[i] ^ st[i + 5] ^ st[i + 10] ^ st[i + 15] ^ st[i + 20];
        for (i = 0; i < 5; i++) {
            t = bc[(i + 4) % 5] ^ ROL(bc[(i + 1) % 5], 1);
            for (j = 0; j < 25; j += 5) st[j + i] ^= t;
        }
        t = st[1];
        for (i = 0; i < 24; i++) {
            j = PILN[i];
            bc[0] = st[j];
            st[j] = ROL(t, ROTC[i]);
            t = bc[0];
        }
        for (j = 0; j < 25; j += 5) {
            for (i = 0; i < 5; i++) bc[i] = st[j + i];
            for (i = 0; i < 5; i++)
                st[j + i] ^= (~bc[(i + 1) % 5]) & bc[(i + 2) % 5];
        }
        st[0] ^= RC[r];
    }
}

static void xor_byte(shake256incctx *st, unsigned int pos, uint8_t b) {
    st->s[pos >> 3] ^= (uint64_t)b << (8 * (pos & 7));
}

void shake256_inc_init(shake256incctx *st) {
    memset(st, 0, sizeof *st);
}

void shake256_inc_absorb(shake256incctx *st, const uint8_t *in, size_t len) {
    size_t i;
    for (i = 0; i < len; i++) {
        xor_byte(st, st->pos++, in[i]);
        if (st->pos == SHAKE256_RATE) {
            keccakf(st->s);
            st->pos = 0;
        }
    }
}

void shake256_inc_finalize(shake256incctx *st) {
    xor_byte(st, st->pos, 0x1F);
    xor_byte(st, SHAKE256_RATE - 1, 0x80);
    keccakf(st->s);
    st->pos = 0;
}

void shake256_inc_squeeze(uint8_t *out, size_t len, shake256incctx *st) {
    size_t i;
    for (i = 0; i < len; i++) {
        if (st->pos == SHAKE256_RATE) {
            keccakf(st->s);
            st->pos = 0;
        }
        out[i] = (uint8_t)(st->s[st->pos >> 3] >> (8 * (st->pos & 7)));
        st->pos++;
    }
}

void shake256_inc_ctx_release(shake256incctx *st) {
    memset(st, 0, sizeof *st);
}
