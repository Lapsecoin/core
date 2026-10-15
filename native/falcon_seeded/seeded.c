/* Seeded Falcon-512 key generation: PQClean pqclean.c (MIT, Falcon Project)
 * with the randombytes call replaced by a caller-supplied 48-byte seed. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "api.h"
#include "inner.h"

#if defined(_WIN32)
#define FS_EXPORT __declspec(dllexport)
#else
#define FS_EXPORT __attribute__((visibility("default")))
#endif

/* Same as PQClean's crypto_sign_keypair, except the 48-byte seed that it
 * draws from randombytes is passed in. The keys come out in the identical
 * format, so liboqs signs and verifies with them unchanged. */
FS_EXPORT int
falcon512_keypair_from_seed(
    uint8_t *pk, uint8_t *sk, const uint8_t *seed48) {
    union {
        uint8_t b[FALCON_KEYGEN_TEMP_9];
        uint64_t dummy_u64;
        fpr dummy_fpr;
    } tmp;
    int8_t f[512], g[512], F[512];
    uint16_t h[512];
    inner_shake256_context rng;
    size_t u, v;

    /*
     * Generate key pair.
     */
    inner_shake256_init(&rng);
    inner_shake256_inject(&rng, seed48, 48);
    inner_shake256_flip(&rng);
    PQCLEAN_FALCON512_CLEAN_keygen(&rng, f, g, F, NULL, h, 9, tmp.b);
    inner_shake256_ctx_release(&rng);

    /*
     * Encode private key.
     */
    sk[0] = 0x50 + 9;
    u = 1;
    v = PQCLEAN_FALCON512_CLEAN_trim_i8_encode(
            sk + u, PQCLEAN_FALCON512_CLEAN_CRYPTO_SECRETKEYBYTES - u,
            f, 9, PQCLEAN_FALCON512_CLEAN_max_fg_bits[9]);
    if (v == 0) {
        return -1;
    }
    u += v;
    v = PQCLEAN_FALCON512_CLEAN_trim_i8_encode(
            sk + u, PQCLEAN_FALCON512_CLEAN_CRYPTO_SECRETKEYBYTES - u,
            g, 9, PQCLEAN_FALCON512_CLEAN_max_fg_bits[9]);
    if (v == 0) {
        return -1;
    }
    u += v;
    v = PQCLEAN_FALCON512_CLEAN_trim_i8_encode(
            sk + u, PQCLEAN_FALCON512_CLEAN_CRYPTO_SECRETKEYBYTES - u,
            F, 9, PQCLEAN_FALCON512_CLEAN_max_FG_bits[9]);
    if (v == 0) {
        return -1;
    }
    u += v;
    if (u != PQCLEAN_FALCON512_CLEAN_CRYPTO_SECRETKEYBYTES) {
        return -1;
    }

    /*
     * Encode public key.
     */
    pk[0] = 0x00 + 9;
    v = PQCLEAN_FALCON512_CLEAN_modq_encode(
            pk + 1, PQCLEAN_FALCON512_CLEAN_CRYPTO_PUBLICKEYBYTES - 1,
            h, 9);
    if (v != PQCLEAN_FALCON512_CLEAN_CRYPTO_PUBLICKEYBYTES - 1) {
        return -1;
    }

    return 0;
}


/* One-shot SHAKE256, exported so the Python side can check that the bundled
 * Keccak agrees with hashlib before trusting any key derived with it. */
#include "fips202.h"
FS_EXPORT void falcon_seeded_shake256(uint8_t *out, size_t outlen,
                                      const uint8_t *in, size_t inlen) {
    shake256incctx c;
    shake256_inc_init(&c);
    shake256_inc_absorb(&c, in, inlen);
    shake256_inc_finalize(&c);
    shake256_inc_squeeze(out, outlen, &c);
    shake256_inc_ctx_release(&c);
}
