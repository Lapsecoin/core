FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    cmake ninja-build build-essential pkg-config git \
    libssl-dev libtorrent-rasterbar-dev python3-libtorrent \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# liboqs (post-quantum crypto backend needed by liboqs-python)
RUN git clone --depth 1 https://github.com/open-quantum-safe/liboqs.git /tmp/liboqs \
    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \
        -DOQS_ALGS_ENABLED=STD \
        -DBUILD_SHARED_LIBS=ON \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/liboqs/build -j"$(nproc)" \
    && cmake --install /tmp/liboqs/build \
    && ldconfig \
    && rm -rf /tmp/liboqs

WORKDIR /app

# The HD wallet's seeded Falcon key generation, built from the same PQClean
# sources liboqs ships (native/falcon_seeded) and installed beside liboqs.
COPY native/falcon_seeded /tmp/falcon_seeded
RUN cmake -S /tmp/falcon_seeded -B /tmp/falcon_seeded/build -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/falcon_seeded/build \
    && cmake --install /tmp/falcon_seeded/build \
    && ldconfig \
    && rm -rf /tmp/falcon_seeded

COPY requirements.txt .
# chiavdf's build pulls in libtorrent via apt above; pip install picks up the
# system libtorrent module since apt provides python3-libtorrent, not a wheel.
RUN pip install --no-cache-dir -r requirements.txt
# cairosvg is intentionally not in requirements.txt (it broke source installs
# on machines without libcairo/a compiler); install it explicitly here since
# the image already has libcairo2, so the /lapsecoin.png icon routes work.
RUN pip install --no-cache-dir cairosvg>=2.7

COPY . .
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# The wallet, chain DB and logs live on a mounted volume so they survive
# container restarts/rebuilds. The node keeps its files in its working
# directory, so that is the volume.
VOLUME ["/data"]
WORKDIR /data

EXPOSE 8333

# LAPSECOIN_PASSPHRASE must be supplied at runtime (e.g. via `docker run -e`
# or an env file); it is intentionally not baked into the image.
# A volume that still holds an old lapsecoin_key.json is moved to a recovery
# phrase at start: the phrase is printed once to this container's output.
CMD ["python", "/app/main.py", \
     "--log-level", "INFO", \
     "--db", "/data/lapsecoin_chain.db", \
     "--no-gui"]
