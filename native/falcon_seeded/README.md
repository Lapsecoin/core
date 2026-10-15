# falcon_seeded

Seeded FALCON-512 key generation for the HD wallet. liboqs draws its own
randomness for key generation and exposes no seed for signature schemes, so
this builds the same PQClean Falcon-512 "clean" sources liboqs 0.16.0 ships,
with the 48-byte seed that `PQCLEAN_FALCON512_CLEAN_crypto_sign_keypair` takes
from `randombytes` passed in instead (`seeded.c`). Keys come out in the same
format, so liboqs signs and verifies with them unchanged.

- `keygen.c codec.c common.c fft.c fpr.c rng.c vrfy.c inner.h fpr.h api.h`:
  byte for byte the PQClean files in liboqs 0.16.0 (MIT, Falcon Project, see
  `LICENSE`). Keep them identical when the liboqs pin moves.
- `keccak.c fips202.h`: a small SHAKE256 standing in for PQClean's fips202,
  checked against `hashlib` when the library loads.
- `seeded.c`: the seeded entry point.

Build: `make -C native/falcon_seeded`.
