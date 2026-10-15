CC ?= cc
CFLAGS ?= -O2
OUT ?= libfalcon_seeded.so
SRCS = seeded.c keygen.c codec.c common.c fft.c fpr.c rng.c vrfy.c keccak.c
$(OUT): $(SRCS) fips202.h inner.h fpr.h api.h
	$(CC) $(CFLAGS) -fPIC -fvisibility=hidden -shared -o $@ $(SRCS)
clean:
	rm -f $(OUT)
