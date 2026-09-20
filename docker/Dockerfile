# B3 Hive — single all-in-one container: backend proxy + UI
# v0.6.0: the daemon is NOT baked into the image. Managed mode downloads
# official B3-CoinV2 release binaries at runtime into <data>/daemon; this
# decouples UI releases from daemon versions and keeps the image small.

FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gosu zstd \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

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
