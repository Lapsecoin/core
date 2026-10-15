/* Minimal SHAKE256 for the vendored PQClean Falcon-512 sources, in place of
 * PQClean's fips202.h (which in liboqs maps onto liboqs internals). Only the
 * calls inner.h needs. Checked against hashlib.shake_256 in tests. */
#ifndef FALCON_SEEDED_FIPS202_H
#define FALCON_SEEDED_FIPS202_H

#include <stddef.h>
#include <stdint.h>

#define SHAKE256_RATE 136

typedef struct {
    uint64_t s[25];
    unsigned int pos;
} shake256incctx;

void shake256_inc_init(shake256incctx *st);
void shake256_inc_absorb(shake256incctx *st, const uint8_t *in, size_t len);
void shake256_inc_finalize(shake256incctx *st);
void shake256_inc_squeeze(uint8_t *out, size_t len, shake256incctx *st);
void shake256_inc_ctx_release(shake256incctx *st);

#endif
