#!/bin/bash
# B3 Hive container healthcheck.
# Healthy = daemon RPC responds. When RUN_UI is enabled, the backend must
# respond too.

set -uo pipefail

B3_DATA_DIR="${B3_DATA_DIR:-/data}"
CONF="${B3_DATA_DIR}/b3coin.conf"
RUN_UI="${RUN_UI:-true}"

# Effective RPC settings come from the conf (source of truth).
RPC_USER="$(grep -E '^rpcuser=' "${CONF}" | tail -1 | cut -d= -f2-)"
RPC_PASSWORD="$(grep -E '^rpcpassword=' "${CONF}" | tail -1 | cut -d= -f2-)"
RPC_PORT="$(grep -E '^rpcport=' "${CONF}" | tail -1 | cut -d= -f2-)"
RPC_PORT="${RPC_PORT:-32647}"

# 1) Daemon: getblockchaininfo over loopback
if ! curl -s --max-time 15 \
        -u "${RPC_USER}:${RPC_PASSWORD}" \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"getblockchaininfo"}' \
        "http://127.0.0.1:${RPC_PORT}/" | grep -q '"result"'; then
    echo "daemon RPC not responding" >&2
    exit 1
fi

# 2) Backend (only when UI is enabled)
if [ "${RUN_UI}" != "false" ]; then
    WEB_PORT="${WEB_PORT:-8080}"
    if ! curl -sf --max-time 10 "http://127.0.0.1:${WEB_PORT}/api/health" > /dev/null; then
        echo "backend not responding" >&2
        exit 1
    fi
fi

exit 0
