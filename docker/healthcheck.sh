#!/bin/bash
# B3 Hive container healthcheck.
# Healthy = daemon RPC responds. When RUN_UI is enabled, the backend must
# respond too.

set -uo pipefail

B3_DATA_DIR="${B3_DATA_DIR:-/data}"
# v0.6.0: the node datadir (and its b3coin.conf) lives under <data>/node.
CONF="${B3_DATA_DIR}/node/b3coin.conf"
RUN_UI="${RUN_UI:-true}"

# Blocked setup mode (storage not persistent): the daemon intentionally
# never starts. Healthy as long as the backend serves the remediation page.
if [ -f "${B3_DATA_DIR}/.storage_blocked" ] && [ "${RUN_UI}" != "false" ]; then
    WEB_PORT="${WEB_PORT:-8080}"
    if curl -sf --max-time 10 "http://127.0.0.1:${WEB_PORT}/api/health" > /dev/null; then
        echo "storage not persistent — backend serving remediation page (healthy)"
        exit 0
    fi
    echo "backend not responding (storage blocked)" >&2
    exit 1
fi

# Deferred daemon (first UI run, wizard pending): the daemon intentionally
# stays DOWN until the setup wizard picks a sync method (bootstrap download
# or sync-from-scratch). Healthy as long as the backend (wizard) responds.
DEFERRED="${B3_DEFERRED_FILE:-${B3_DATA_DIR}/.daemon_deferred}"
if [ -f "${DEFERRED}" ] && [ "${RUN_UI}" != "false" ]; then
    WEB_PORT="${WEB_PORT:-8080}"
    if curl -sf --max-time 10 "http://127.0.0.1:${WEB_PORT}/api/health" > /dev/null; then
        echo "daemon deferred (setup wizard pending) — backend healthy"
        exit 0
    fi
    echo "backend not responding while daemon deferred" >&2
    exit 1
fi

# Bootstrap in progress: the daemon is intentionally stopped while the
# setup wizard applies a chain bootstrap. Container stays healthy.
PROGRESS="${B3_BOOTSTRAP_PROGRESS:-${B3_DATA_DIR}/bootstrap.progress.json}"
if [ -f "${PROGRESS}" ]; then
    PHASE=$(python3 -c "import json;print(json.load(open('${PROGRESS}')).get('phase',''))" 2>/dev/null || true)
    case "${PHASE}" in
        stopping|downloading|verifying|extracting)
            echo "bootstrap in progress (${PHASE}) — healthy by design"
            exit 0
            ;;
    esac
fi

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
