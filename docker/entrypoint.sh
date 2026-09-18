#!/bin/bash
# B3 Hive all-in-one container entrypoint
#
# 1. Ensure /data/b3coin.conf exists (generated from env on FIRST RUN only —
#    an existing conf is never overwritten).
# 2. Start b3coind in the foreground (chain data in /data).
# 3. If RUN_UI is not "false", start the backend (FastAPI) which serves the
#    UI and talks to the node over loopback RPC.
# 4. Supervise: daemon death stops the container (restart policy handles it);
#    backend death is tolerated and restarted (up to 5 times).

set -euo pipefail

B3_DATA_DIR="${B3_DATA_DIR:-/data}"
RUN_UI="${RUN_UI:-true}"
WEB_PORT="${WEB_PORT:-8080}"
CONF="${B3_DATA_DIR}/b3coin.conf"
BACKEND_MAX_RESTARTS=5

log() { echo "[entrypoint] $*"; }

write_progress() {
    # Atomic-ish JSON progress for the setup wizard UI.
    PROGRESS_FILE="${B3_BOOTSTRAP_PROGRESS:-${B3_DATA_DIR}/bootstrap.progress.json}"
    printf '%s\n' "$1" > "${PROGRESS_FILE}.tmp"
    mv "${PROGRESS_FILE}.tmp" "${PROGRESS_FILE}"
}

progress_phase() {
    # write_progress "phase" extra-json-fields...
    phase="$1"; shift
    write_progress "{\"phase\":\"${phase}\"${1:+,$*}}"
}

generate_rpc_secret() {
    # Read exactly 16 bytes from /dev/urandom and hex-encode them (32 chars).
    # NOTE: never use `tr ... < /dev/urandom | head -c N` here — head closes the
    # pipe, tr gets SIGPIPE (141), and set -o pipefail kills the entrypoint.
    head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

storage_persistent() {
    # Same rule the backend enforces (app.deps._mount_is_persistent):
    # bind mounts and NAMED Docker volumes are persistent; anonymous
    # volumes, overlay, tmpfs are not. Reuse the python implementation so
    # the two checks can never drift apart.
    B3_DATA_DIR="${B3_DATA_DIR}" python3 - <<'PY' 2>/dev/null
import os
try:
    from app.deps import _mount_is_persistent
except Exception:
    print("unknown")
else:
    print("yes" if _mount_is_persistent(os.environ["B3_DATA_DIR"]) else "no")
PY
}

# --- 0. Privilege setup ----------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    # Started as root (docker default): fix data-dir ownership, run as b3coin.
    chown -R b3coin:b3coin "${B3_DATA_DIR}" 2>/dev/null || true
    run_as() { gosu b3coin "$@"; }
else
    run_as() { "$@"; }
fi

# --- 0.5 Storage persistence check -----------------------------------------
# /data may not even exist when no volume is mapped at all: create it so
# the checks and the backend can run, then decide if this storage is safe.
mkdir -p "${B3_DATA_DIR}"
PERSISTENT="$(storage_persistent)"
if [ "${PERSISTENT}" = "unknown" ]; then
    log "WARNING: storage persistence could not be verified — the backend enforces its own check"
fi
BLOCKED=""
if [ "${PERSISTENT}" = "no" ] && [ "${ALLOW_EPHEMERAL_DATA:-false}" != "true" ]; then
    BLOCKED=1
fi
if [ -n "${BLOCKED}" ]; then
    log "STORAGE CHECK FAILED: ${B3_DATA_DIR} is not a persistent volume (bind mount or named volume)."
    log "Refusing to set up: the blockchain and any wallet would be LOST when the container is removed."
    touch "${B3_DATA_DIR}/.storage_blocked" 2>/dev/null || true
    if [ "${RUN_UI}" = "false" ]; then
        log "RUN_UI=false (headless) — refusing to start at all."
        log "Remedy: stop the container, map a volume for /data (compose: volumes: b3hive-data:/data, or a host-directory bind mount), then start again."
        exit 1
    fi
    log "The node daemon will NOT start. The web UI stays up to explain the problem: open http://<host>:${WEB_PORT}/"
    # Backend needs a session secret to boot; use a random one in memory
    # only (NOT written to disk — nothing may be persisted on throwaway
    # storage). RPC creds stay empty: the daemon never starts anyway.
    export SESSION_SECRET="$(generate_rpc_secret)$(generate_rpc_secret)$(generate_rpc_secret)$(generate_rpc_secret)"
    export TOTP_ENCRYPTION_KEY="$(generate_rpc_secret)$(generate_rpc_secret)"
    # Backend state must NOT touch /data in blocked mode (it is throwaway
    # storage and would fail/leak): keep every state file in tmpfs /tmp.
    export B3_DB_PATH="/tmp/b3hive-blocked.db"
    export BOOTSTRAP_CMD_FILE="/tmp/bootstrap.cmd"
    export BOOTSTRAP_PROGRESS_FILE="/tmp/bootstrap.progress.json"
    export RECOVERY_CMD_FILE="/tmp/recovery.cmd"
    export EXPLORER_TIP_FILE="/tmp/explorer_tip_height"
    export START_NODE_CMD_FILE="/tmp/start-node.cmd"
    export WIZARD_MARKER_FILE="/tmp/.wizard_complete"
    export B3_DEFERRED_FILE="/tmp/.daemon_deferred"
    RPC_USER="b3coinrpc"
    RPC_PASSWORD=""
    RPC_PORT="${RPC_PORT:-32647}"
fi

# --- 1. Generate b3coin.conf on FIRST RUN only ------------------------------
if [ -z "${BLOCKED}" ] && [ ! -f "${CONF}" ]; then
    log "No b3coin.conf found — generating from environment (first run)"
    RPC_USER="${RPC_USER:-b3coinrpc}"
    if [ -z "${RPC_PASSWORD:-}" ]; then
        RPC_PASSWORD="$(generate_rpc_secret)"
        log "RPC_PASSWORD not set — generated a random one"
    fi
    RPC_PORT="${RPC_PORT:-32647}"

    cat > "${CONF}.tmp" <<EOF
# Generated by the B3 Hive container entrypoint on first run.
# Edit freely — this file is NOT overwritten on subsequent runs or upgrades.

# Network
txindex=1
listen=1
listenonion=0

# RPC — loopback only (the backend proxy is the only RPC client)
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=${RPC_PORT}
rpcuser=${RPC_USER}
rpcpassword=${RPC_PASSWORD}

# Staking: v1.1.4 has NO 'staking=' conf option (it is ignored with a
# warning). Staking is started via the wallet RPC (startstaking) — the UI
# will expose this in a later phase. Nothing to configure here.

# Wallet
disablewallet=0
EOF
    chown b3coin:b3coin "${CONF}.tmp" 2>/dev/null || true
    chmod 600 "${CONF}.tmp"
    mv "${CONF}.tmp" "${CONF}"
    log "Generated ${CONF}"
    log "NOTE: RPC credentials live only in ${CONF} — delete that file to regenerate (loses no chain data)"
fi

# Read back the EFFECTIVE RPC settings from the conf — it is the source of
# truth (the user may have edited it since it was generated).
if [ -f "${CONF}" ]; then
    RPC_USER="$(grep -E '^rpcuser=' "${CONF}" | tail -1 | cut -d= -f2-)"
    RPC_PASSWORD="$(grep -E '^rpcpassword=' "${CONF}" | tail -1 | cut -d= -f2-)"
    RPC_PORT="$(grep -E '^rpcport=' "${CONF}" | tail -1 | cut -d= -f2-)"
fi
RPC_PORT="${RPC_PORT:-32647}"

# --- 1.5 Zero-config UI secrets ---------------------------------------------
# The UI works with no .env at all: on the first UI run, internal secrets not
# provided via the environment are generated and persisted in
# ${B3_DATA_DIR}/.secrets.env (mode 600). Explicit env vars always win.
# NO login password is generated: the first run boots in SETUP MODE
# (local-only) and the operator sets the password in the wizard Security step.
SECRETS_FILE="${B3_DATA_DIR}/.secrets.env"
if [ -z "${BLOCKED}" ] && [ "${RUN_UI}" != "false" ] && [ ! -f "${SECRETS_FILE}" ]; then
    GEN_SESSION="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    GEN_TOTP="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    {
        echo "# Generated by the B3 Hive entrypoint on first UI run."
        echo "# Delete this file to regenerate (logs everyone out; 2FA stays valid)."
        echo "SESSION_SECRET=${GEN_SESSION}"
        echo "TOTP_ENCRYPTION_KEY=${GEN_TOTP}"
    } > "${SECRETS_FILE}.tmp"
    chmod 600 "${SECRETS_FILE}.tmp"
    chown b3coin:b3coin "${SECRETS_FILE}.tmp" 2>/dev/null || true
    mv "${SECRETS_FILE}.tmp" "${SECRETS_FILE}"
    unset GEN_SESSION GEN_TOTP
    log "Generated internal secrets in ${SECRETS_FILE}"
fi
if [ "${RUN_UI}" != "false" ] && [ -f "${SECRETS_FILE}" ]; then
    # Fill in any secrets the environment did not provide.
    while IFS='="' read -r k v; do
        case "${k}" in
            SESSION_SECRET|TOTP_ENCRYPTION_KEY)
                if [ -z "${!k:-}" ]; then
                    export "${k}=${v}"
                fi
                ;;
            esac
        done < "${SECRETS_FILE}"
fi

# --- 2. Start the daemon (deferred until first wizard completion in UI mode) ---
# First run with the UI enabled: the daemon stays DOWN until the setup wizard
# is completed (Finish Setup). This guarantees the user's sync-method and
# node-configuration choices apply on the FIRST daemon start — no restart
# needed — and no sync burns hours before the bootstrap choice.
# Upgrades and existing completed setups start immediately; headless mode too.
WIZARD_MARKER="${B3_DATA_DIR}/.wizard_complete"
DEFERRED_FILE="${B3_DEFERRED_FILE:-${B3_DATA_DIR}/.daemon_deferred}"
start_daemon() {
    rm -f "${DEFERRED_FILE}"
    log "Starting b3coind (datadir=${B3_DATA_DIR})"
    # Daemon output goes to a file the backend can tail (/data persists),
    # mirrored to container stdout so `docker logs` keeps working.
    touch "${DAEMON_LOG}"
    chmod 640 "${DAEMON_LOG}" 2>/dev/null || true
    run_as b3coind \
        -datadir="${B3_DATA_DIR}" \
        -printtoconsole \
        -daemon=0 "$@" >> "${DAEMON_LOG}" 2>&1 &
    DAEMON_PID=$!
    if [ -z "${TAIL_PID}" ]; then
        tail -F -n 0 "${DAEMON_LOG}" 2>/dev/null &
        TAIL_PID=$!
    fi
}

DAEMON_PID=""
TAIL_PID=""
DAEMON_LOG="${B3_DAEMON_LOG:-${B3_DATA_DIR}/daemon.log}"
if [ -n "${BLOCKED}" ]; then
    log "Daemon NOT started: storage is not persistent (setup refused)"
elif [ "${RUN_UI}" != "false" ] && [ ! -f "${WIZARD_MARKER}" ]; then
    touch "${DEFERRED_FILE}"
    log "First UI run — daemon deferred until the setup wizard is completed (Finish Setup)"
else
    start_daemon
fi

# --- 3. Backend (UI) --------------------------------------------------------
start_backend() {
    log "Starting backend (UI) on :${WEB_PORT}"
    B3_RPC_HOST=127.0.0.1 \
    B3_RPC_PORT="${RPC_PORT}" \
    B3_RPC_USER="${RPC_USER}" \
    B3_RPC_PASSWORD="${RPC_PASSWORD}" \
    run_as python -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT}" &
    BACKEND_PID=$!
}

run_bootstrap() {
    # $1 = bootstrap.cmd JSON: {id, height, url, sha256, size}
    CMD_JSON="$1"
    B3_TMP_ARCHIVE="${B3_DATA_DIR}/bootstrap-download.tar.zst"

    read -r B_HEIGHT B_URL B_SHA B_SIZE B_WIPE < <(python3 - "$CMD_JSON" <<'PY'
import json, sys
try:
    c = json.loads(sys.argv[1])
    print(c.get("height",""), c.get("url",""), c.get("sha256",""), c.get("size",""),
          "yes" if c.get("wipe") else "")
except Exception:
    print("", "", "", "", "")
PY
)
    if [ -z "${B_URL}" ] || [ -z "${B_SHA}" ]; then
        log "bootstrap cmd invalid — ignoring"
        write_progress '{"phase":"failed","error":"invalid command"}'
        return 0
    fi

    if [ -n "${DAEMON_PID}" ]; then
        log "bootstrap: stopping daemon for chain replacement (height ${B_HEIGHT})"
        progress_phase stopping "\"height\":${B_HEIGHT}"
        kill -TERM "${DAEMON_PID}" 2>/dev/null || true
        for _ in $(seq 1 60); do
            kill -0 "${DAEMON_PID}" 2>/dev/null || break
            sleep 2
        done
        if kill -0 "${DAEMON_PID}" 2>/dev/null; then
            log "daemon did not stop — aborting bootstrap"
            write_progress '{"phase":"failed","error":"daemon did not stop"}'
            return 0
        fi
    fi

    log "bootstrap: downloading ${B_URL}"
    write_progress "{\"phase\":\"downloading\",\"height\":${B_HEIGHT},\"bytes\":0,\"size\":${B_SIZE}}"
    rm -f "${B3_TMP_ARCHIVE}"
    curl -sL --fail -o "${B3_TMP_ARCHIVE}" "${B_URL}" &
    CURL_PID=$!
    while kill -0 "${CURL_PID}" 2>/dev/null; do
        sleep 3
        BYTES=$(stat -c %s "${B3_TMP_ARCHIVE}" 2>/dev/null || echo 0)
        write_progress "{\"phase\":\"downloading\",\"height\":${B_HEIGHT},\"bytes\":${BYTES},\"size\":${B_SIZE}}"
    done
    wait "${CURL_PID}" || {
        log "bootstrap: download failed"
        write_progress '{"phase":"failed","error":"download failed"}'
        rm -f "${B3_TMP_ARCHIVE}"
        if [ -n "${DAEMON_PID}" ]; then
            log "bootstrap failed — restarting daemon with previous chain"
            start_daemon
        else
            log "bootstrap failed — daemon stays deferred (retry, or choose sync-from-scratch)"
        fi
        return 0
    }

    log "bootstrap: verifying SHA256"
    progress_phase verifying "\"height\":${B_HEIGHT}"
    echo "${B_SHA}  ${B3_TMP_ARCHIVE}" | sha256sum -c - || {
        log "bootstrap: sha256 mismatch — aborting"
        write_progress '{"phase":"failed","error":"sha256 mismatch"}'
        rm -f "${B3_TMP_ARCHIVE}"
        if [ -n "${DAEMON_PID}" ]; then
            log "bootstrap failed — restarting daemon with previous chain"
            start_daemon
        else
            log "bootstrap failed — daemon stays deferred (retry, or choose sync-from-scratch)"
        fi
        return 0
    }

    log "bootstrap: extracting into ${B3_DATA_DIR}"
    progress_phase extracting "\"height\":${B_HEIGHT}"
    # Replace-chain mode (explicit user consent, wizard-gated in the backend):
    # remove exactly the chain dirs the bootstrap archive replaces.
    # Wallets, b3coin.conf, secrets, logs and settings are NEVER touched.
    if [ "${B_WIPE}" = "yes" ]; then
        log "bootstrap: wipe requested — removing existing chain data (blocks, chainstate, indexes, flowmesh)"
        for CHAIN_DIR in blocks chainstate indexes flowmesh; do
            if [ -e "${B3_DATA_DIR}/${CHAIN_DIR}" ]; then
                rm -rf "${B3_DATA_DIR}/${CHAIN_DIR}" || {
                    write_progress '{"phase":"failed","error":"chain wipe failed"}'
                    log "bootstrap: failed to remove ${CHAIN_DIR} — aborting"
                    if [ -n "${DAEMON_PID}" ]; then start_daemon; fi
                    return 0
                }
            fi
        done
    fi
    tar --zstd -xf "${B3_TMP_ARCHIVE}" -C "${B3_DATA_DIR}" || {
        log "bootstrap: extraction failed"
        write_progress '{"phase":"failed","error":"extraction failed"}'
        rm -f "${B3_TMP_ARCHIVE}"
        if [ -n "${DAEMON_PID}" ]; then
            log "bootstrap failed — restarting daemon with previous chain"
            start_daemon
        else
            log "bootstrap failed — daemon stays deferred (retry, or choose sync-from-scratch)"
        fi
        return 0
    }
    rm -f "${B3_TMP_ARCHIVE}"

    log "bootstrap: complete at height ${B_HEIGHT} — starting daemon"
    write_progress "{\"phase\":\"done\",\"height\":${B_HEIGHT}}"
    start_daemon
    log "daemon started (bootstrap height ${B_HEIGHT})"
}

BACKEND_PID=""
BACKEND_RESTARTS=0
if [ "${RUN_UI}" != "false" ]; then
    start_backend
    # (first run is passwordless: the operator sets the login password in the wizard Security step)
else
    log "RUN_UI=false — headless mode (daemon only)"
fi

# --- 4. Supervise -------------------------------------------------------------
terminate() {
    log "SIGTERM/SIGINT received — shutting down"
    if [ -n "${BACKEND_PID}" ]; then
        kill -TERM "${BACKEND_PID}" 2>/dev/null || true
    fi
    if [ -n "${DAEMON_PID}" ]; then
        kill -TERM "${DAEMON_PID}" 2>/dev/null || true
    fi
    if [ -n "${TAIL_PID}" ]; then
        kill -TERM "${TAIL_PID}" 2>/dev/null || true
    fi
    wait 2>/dev/null
    exit 0
}
trap terminate TERM INT

while true; do
    # Daemon death stops the container.
    if [ -n "${DAEMON_PID}" ] && ! kill -0 "${DAEMON_PID}" 2>/dev/null; then
        wait "${DAEMON_PID}"
        DAEMON_EXIT=$?
        log "b3coind exited (code ${DAEMON_EXIT}) — shutting down container"
        if [ -n "${BACKEND_PID}" ]; then
            kill -TERM "${BACKEND_PID}" 2>/dev/null || true
        fi
        wait 2>/dev/null
        exit "${DAEMON_EXIT}"
    fi
    # Backend death is tolerated: restart it (bounded).
    if [ -n "${BACKEND_PID}" ] && ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
        wait "${BACKEND_PID}" || true
        BACKEND_RESTARTS=$((BACKEND_RESTARTS + 1))
        if [ "${BACKEND_RESTARTS}" -gt "${BACKEND_MAX_RESTARTS}" ]; then
            log "backend crashed ${BACKEND_RESTARTS} times — giving up (daemon keeps running headless)"
            BACKEND_PID=""
        else
            log "backend exited — restarting (attempt ${BACKEND_RESTARTS}/${BACKEND_MAX_RESTARTS})"
            start_backend
        fi
    fi
    # Bootstrap command polling: the setup wizard writes a JSON command to
    # /data/bootstrap.cmd. Runs blocking inside this loop iteration; the
    # backend stays up to serve /api/setup/bootstrap/progress.
    BOOTSTRAP_CMD="${B3_BOOTSTRAP_CMD:-${B3_DATA_DIR}/bootstrap.cmd}"
    if [ -z "${BLOCKED}" ] && [ -f "${BOOTSTRAP_CMD}" ]; then
        CMD_JSON="$(cat "${BOOTSTRAP_CMD}")"
        rm -f "${BOOTSTRAP_CMD}"
        log "bootstrap command received"
        run_bootstrap "${CMD_JSON}"
    fi
    # Recovery command polling: the monitor writes a command to
    # /data/recovery.cmd when stall_level is restart or reindex.
    RECOVERY_CMD="${RECOVERY_CMD_FILE:-${B3_DATA_DIR}/recovery.cmd}"
    if [ -z "${BLOCKED}" ] && [ -f "${RECOVERY_CMD}" ]; then
        CMD=$(cat "${RECOVERY_CMD}")
        rm -f "${RECOVERY_CMD}"
        log "recovery command: ${CMD}"
        case "${CMD}" in
            restart)
                log "restarting b3coind (stall recovery)"
                if [ -n "${DAEMON_PID}" ]; then
                    kill -TERM "${DAEMON_PID}" 2>/dev/null || true
                    wait "${DAEMON_PID}" 2>/dev/null || true
                fi
                sleep 5
                start_daemon
                ;;
            reindex)
                log "restarting b3coind with -reindex (stall recovery)"
                if [ -n "${DAEMON_PID}" ]; then
                    kill -TERM "${DAEMON_PID}" 2>/dev/null || true
                    wait "${DAEMON_PID}" 2>/dev/null || true
                fi
                sleep 5
                start_daemon -reindex
                ;;
            *)
                log "unknown recovery command: ${CMD} — ignoring"
                ;;
            esac
    fi
    # Start-node command polling: the setup wizard writes start-node.cmd when
    # the user finishes the wizard choosing sync-from-scratch / keep-chain.
    START_NODE_CMD="${START_NODE_CMD_FILE:-${B3_DATA_DIR}/start-node.cmd}"
    if [ -z "${BLOCKED}" ] && [ -z "${DAEMON_PID}" ] && [ -f "${START_NODE_CMD}" ]; then
        rm -f "${START_NODE_CMD}"
        log "start-node command received — starting daemon (sync from scratch)"
        start_daemon
    fi
    sleep 5
done