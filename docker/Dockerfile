# B3 Hive — single all-in-one container: b3coind + backend proxy + UI
# Binaries are the official B3-CoinV2 release builds (static headless),
# downloaded and SHA256-verified at build time. The image never compiles from source.

ARG B3_VERSION=v1.1.4
FROM python:3.12-slim-bookworm

ARG B3_VERSION
# SHA256 of b3-hive-${B3_VERSION}-unsigned-linux-x86_64-static-headless.tar.gz
ARG B3_SHA256=34489a7ca3a02bf2b456c38c1aa87ab902f5233779bc37e0353d4b64053b4287
ARG B3_ASSET=b3-hive-${B3_VERSION}-unsigned-linux-x86_64-static-headless.tar.gz
ARG B3_DOWNLOAD_URL=https://github.com/B3-Coin/B3-CoinV2/releases/download/${B3_VERSION}/${B3_ASSET}

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gosu zstd \
 && rm -rf /var/lib/apt/lists/*

# Download, verify, and install the official node binaries
RUN curl -sSL -o /tmp/b3.tar.gz "${B3_DOWNLOAD_URL}" \
 && echo "${B3_SHA256}  /tmp/b3.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/b3/bin /tmp/b3x \
 && tar xzf /tmp/b3.tar.gz -C /tmp/b3x --strip-components=1 \
 && cp /tmp/b3x/bin/b3coind \
       /tmp/b3x/bin/b3coin-cli \
       /tmp/b3x/bin/b3coin-wallet \
       /tmp/b3x/bin/b3coin-tx \
       /tmp/b3x/bin/b3coin-util \
       /opt/b3/bin/ \
 && chmod 755 /opt/b3/bin/* \
 && rm -rf /tmp/b3.tar.gz /tmp/b3x

ENV PATH=/opt/b3/bin:$PATH \
    PYTHONUNBUFFERED=1

# Non-root daemon user
RUN groupadd -g 1000 b3coin \
 && useradd -u 1000 -g b3coin -m -s /usr/sbin/nologin b3coin

# Backend (FastAPI proxy + static UI)
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/ /app/
# Frontend SPA (served same-origin by the backend)
COPY frontend/ /app/frontend/
# Normalize permissions: source files may carry 0600 from the build host;
# the backend runs as the non-root b3coin user and must read /app.
RUN chmod -R a+rX /app
ENV FRONTEND_DIR=/app/frontend

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh

# Node data dir (host-mapped); conf lives at /data/b3coin.conf
# B3_APP_VERSION: baked in at build time (docker build --build-arg
# B3_APP_VERSION=$(git describe --tags)); shown in the UI About card.
# NOTE: no VOLUME instruction on purpose. Declaring VOLUME ["/data"] made
# Docker silently create an ANONYMOUS volume whenever the compose mapping
# was missing — data "persisted" until `docker compose down` wiped it.
# The backend now inspects the mount source (app/deps.py: named bind mount
# or named volume = persistent; overlay/tmpfs/anonymous volume = refused).
ARG B3_APP_VERSION=dev
ENV B3_DATA_DIR=/data \
    RUN_UI=true \
    WEB_PORT=8080 \
    ALLOW_EPHEMERAL_DATA=false \
    B3_APP_VERSION=${B3_APP_VERSION}

EXPOSE 8080

# Node startup replay takes ~5 min at current height — generous start period.
HEALTHCHECK --start-period=10m --interval=30s --timeout=20s --retries=5 \
  CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
