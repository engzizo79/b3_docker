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
B3_DAEMON_MODE="${B3_DAEMON_MODE:-managed}"
DAEMON_DIR="${B3_DATA_DIR}/daemon"
NODE_DATADIR="${B3_DATA_DIR}/node"
DAEMON_BIN="${DAEMON_DIR}/b3coind"
DAEMON_VERSION_FILE="${DAEMON_DIR}/.installed_version"
UPGRADE_CMD_FILE="${B3_DATA_DIR}/upgrade.cmd"
TS_STATE_DIR="${B3_DATA_DIR}/ts"
TS_LOG="${B3_DATA_DIR}/tailscale.log"
TS_SOCKET="${TS_STATE_DIR}/tailscaled.sock"
CONF="${NODE_DATADIR}/b3coin.conf"  # v0.6.0: conf under node/ (defined after NODE_DATADIR)
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

# --- 0.7 Migrate old flat layout to node/ subfolder (one-time) --------------
# v0.6.0 moved chain data from <data>/ to <data>/node/. If the old flat
# layout is detected (chain dirs at root), move them before daemon start.
if [ -z "${BLOCKED}" ]; then
    mkdir -p "${NODE_DATADIR}"
    FLAT_MARKER="${B3_DATA_DIR}/.migrated_to_node"
    if [ ! -f "${FLAT_MARKER}" ]; then
        MOVED=0
        for ITEM in blocks chainstate indexes flowmesh wallets wallet.dat b3coin.conf; do
            SRC="${B3_DATA_DIR}/${ITEM}"
            DST="${NODE_DATADIR}/${ITEM}"
            if [ -e "${SRC}" ] && [ ! -e "${DST}" ]; then
                log "Migrating ${ITEM} -> node/${ITEM} (v0.6.0 layout)"
                mv "${SRC}" "${DST}"
                MOVED=1
            fi
        done
        if [ "${MOVED}" = "1" ]; then
            log "Migration complete: chain data moved to ${NODE_DATADIR}"
        fi
        touch "${FLAT_MARKER}"
    fi
fi

# --- 0.8 Managed mode: daemon binary status --------------------------------
# The daemon binary is downloaded by the wizard's Node step
# (POST /api/setup/daemon/choose), NOT automatically at startup.
# On first run, the backend starts with the daemon deferred; the user
# completes the wizard, which downloads the binary and writes
# start-node.cmd; the supervisor loop then launches it.
if [ -z "${BLOCKED}" ] && [ "${B3_DAEMON_MODE}" = "managed" ]; then
    if [ -x "${DAEMON_BIN}" ]; then
        log "Daemon binary present: $(cat ${DAEMON_VERSION_FILE} 2>/dev/null || echo unknown)"
    else
        log "Daemon binary not found — will be downloaded via the setup wizard"
    fi
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
    run_as "${DAEMON_BIN}" \
        -datadir="${NODE_DATADIR}" \
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
elif [ "${B3_DAEMON_MODE}" = "external" ]; then
    log "External (UI-only) mode - daemon NOT started (runs elsewhere)"
elif [ "${RUN_UI}" != "false" ] && [ ! -f "${WIZARD_MARKER}" ]; then
    touch "${DEFERRED_FILE}"
    log "First UI run — daemon deferred until the setup wizard is completed (Finish Setup)"
elif [ ! -x "${DAEMON_BIN}" ]; then
    # v0.6.0+: the daemon is no longer baked into the image. On upgrade from
    # an older version the wizard marker exists but the binary doesn't. Defer
    # and let the UI guide the user through downloading it.
    touch "${DEFERRED_FILE}"
    log "Daemon binary missing on completed setup — deferred (use the UI to install)"
else
    start_daemon
fi

# --- 3. Backend (UI) --------------------------------------------------------
start_backend() {
    log "Starting backend (UI) on :${WEB_PORT}"
    # v0.6.0: wizard daemon choice in .daemon_choice.json overrides boot env
    EFFECTIVE_MODE="${B3_DAEMON_MODE}"
    E_EXT_HOST="${EXT_RPC_HOST:-}"; E_EXT_PORT="${EXT_RPC_PORT:-0}"
    E_EXT_USER="${EXT_RPC_USER:-}"; E_EXT_PASS="${EXT_RPC_PASSWORD:-}"
    if [ -f "${B3_DATA_DIR}/.daemon_choice.json" ]; then
        PARSED="$(python3 -c "import json; c=json.load(open('${B3_DATA_DIR}/.daemon_choice.json')); print(c.get('mode',''), c.get('ext_host',''), c.get('ext_port',0), c.get('ext_user',''), c.get('ext_password',''))" 2>/dev/null || true)"
        if [ -n "${PARSED}" ]; then
            read -r C_MODE C_HOST C_PORT C_USER C_PASS <<< "${PARSED}"
            if [ "${C_MODE}" = "external" ] || [ "${C_MODE}" = "managed" ]; then
                EFFECTIVE_MODE="${C_MODE}"
                if [ "${C_MODE}" = "external" ]; then
                    E_EXT_HOST="${C_HOST}"; E_EXT_PORT="${C_PORT}"
                    E_EXT_USER="${C_USER}"; E_EXT_PASS="${C_PASS}"
                fi
            fi
        fi
    fi
    if [ "${EFFECTIVE_MODE}" = "external" ]; then
        B3_DAEMON_MODE=external         EXT_RPC_HOST="${E_EXT_HOST}"         EXT_RPC_PORT="${E_EXT_PORT}"         EXT_RPC_USER="${E_EXT_USER}"         EXT_RPC_PASSWORD="${E_EXT_PASS}"         run_as python -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT}" &
    else
        B3_RPC_HOST=127.0.0.1         B3_RPC_PORT="${RPC_PORT}"         B3_RPC_USER="${RPC_USER}"         B3_RPC_PASSWORD="${RPC_PASSWORD}"         B3_DAEMON_MODE=managed         run_as python -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT}" &
    fi
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
            if [ -e "${NODE_DATADIR}/${CHAIN_DIR}" ]; then
                rm -rf "${B3_DATA_DIR}/${CHAIN_DIR}" || {
                    write_progress '{"phase":"failed","error":"chain wipe failed"}'
                    log "bootstrap: failed to remove ${CHAIN_DIR} — aborting"
                    if [ -n "${DAEMON_PID}" ]; then start_daemon; fi
                    return 0
                }
            fi
        done
    fi
    tar --zstd -xf "${B3_TMP_ARCHIVE}" -C "${NODE_DATADIR}" || {
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

run_upgrade() {
    CMD_JSON="$1"; log "upgrade command received"
    TAG="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('tag',''))" "$CMD_JSON")"
    [ -z "${TAG}" ] && { log "upgrade: no tag"; return 0; }
    [ -n "${DAEMON_PID}" ] && { kill -TERM "${DAEMON_PID}" 2>/dev/null || true; wait "${DAEMON_PID}" 2>/dev/null || true; DAEMON_PID=""; }
    CHOSEN_VERSION="${TAG}" DAEMON_DIR="${DAEMON_DIR}" DAEMON_VERSION_FILE="${DAEMON_VERSION_FILE}" 
    MIN_DAEMON_VERSION="${MIN_DAEMON_VERSION:-1.1.4}" python3 - <<'UPGRADEPY'
import os, sys
sys.path.insert(0, "/app")
from app import daemon_release
tag = os.environ.get("CHOSEN_VERSION", "")
daemon_dir = os.environ["DAEMON_DIR"]
version_file = os.environ["DAEMON_VERSION_FILE"]
minimum = os.environ.get("MIN_DAEMON_VERSION", "1.1.4")
version = tag.lstrip("vV")
if not daemon_release.meets_minimum(version, minimum):
    print(f"ERROR: {tag} below minimum {minimum}", file=sys.stderr); sys.exit(1)
try:
    rels = daemon_release.list_releases(include_prerelease=False, timeout=30)
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr); sys.exit(1)
release = next((r for r in rels if r.tag == tag or r.version == version), None)
if release is None:
    print(f"ERROR: {tag} not found", file=sys.stderr); sys.exit(1)
try:
    daemon_release.install_version(release, daemon_dir, version_file, timeout=300)
    print(f"Upgraded to {release.tag}")
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr); sys.exit(1)
UPGRADEPY
    if [ $? -eq 0 ]; then
        log "upgrade succeeded - restarting daemon"
        start_daemon
    else
        log "upgrade FAILED - daemon stays stopped"
    fi
}

start_tailscale() {
    # v0.7.0: bundled Tailscale, userspace networking (no /dev/net/tun).
    # State lives under /data/ts — the tailnet IDENTITY persists across
    # container recreation, so stop/pull/start or upgrade never requires
    # re-authentication. Runs as the same non-root user as the backend so
    # the backend can query/control it via the local socket.
    mkdir -p "${TS_STATE_DIR}"
    run_as /usr/local/bin/tailscaled \
        --tun=userspace-networking \
        --state="${TS_STATE_DIR}/tailscaled.state" \
        --statedir="${TS_STATE_DIR}" \
        --socket="${TS_SOCKET}" \
        >> "${TS_LOG}" 2>&1 &
    TS_PID=$!
    log "Starting tailscaled (state=${TS_STATE_DIR}, userspace networking)"
}

ts_status() {
    # Best-effort status JSON for the backend (never fatal).
    run_as /usr/local/bin/tailscale --socket="${TS_SOCKET}" \
        status --json --peers=false 2>/dev/null || echo '{}'
}

BACKEND_PID=""
BACKEND_RESTARTS=0
TS_PID=""
TS_RESTARTS=0
if [ "${RUN_UI}" != "false" ]; then
    start_backend
    # (first run is passwordless: the operator sets the login password in the wizard Security step)
    if [ -x /usr/local/bin/tailscaled ]; then
        start_tailscale
    fi
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
    if [ -n "${TS_PID}" ]; then
        kill -TERM "${TS_PID}" 2>/dev/null || true
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
    # Tailscaled death is tolerated: restart it (bounded, same policy as
    # the backend). Losing it only disables tailnet access, not the wallet.
    if [ -n "${TS_PID}" ] && ! kill -0 "${TS_PID}" 2>/dev/null; then
        wait "${TS_PID}" || true
        TS_RESTARTS=$((TS_RESTARTS + 1))
        if [ "${TS_RESTARTS}" -gt "${BACKEND_MAX_RESTARTS}" ]; then
            log "tailscaled exited ${TS_RESTARTS} times — giving up"
            TS_PID=""
        else
            log "tailscaled exited — restarting (attempt ${TS_RESTARTS}/${BACKEND_MAX_RESTARTS})"
            start_tailscale
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
    if [ -z "${BLOCKED}" ] && [ -z "${DAEMON_PID}" ] && [ "${B3_DAEMON_MODE}" = "managed" ] && [ -f "${START_NODE_CMD}" ]; then
        rm -f "${START_NODE_CMD}"
        if [ ! -x "${DAEMON_BIN}" ]; then
            log "start-node: daemon binary missing — download it in Settings / setup wizard first"
        else
            log "start-node command received — starting daemon (sync from scratch)"
            start_daemon
        fi
    fi
    RESTART_BACKEND_CMD="${B3_DATA_DIR}/restart-backend.cmd"
    if [ -z "${BLOCKED}" ] && [ -f "${RESTART_BACKEND_CMD}" ]; then
        rm -f "${RESTART_BACKEND_CMD}"
        log "backend restart command received - reloading with new mode env"
        [ -n "${BACKEND_PID}" ] && kill -TERM "${BACKEND_PID}" 2>/dev/null || true
        sleep 2
        start_backend
    fi
    UPGRADE_CMD="${UPGRADE_CMD_FILE}"
    if [ -z "${BLOCKED}" ] && [ "${B3_DAEMON_MODE}" = "managed" ] && [ -f "${UPGRADE_CMD}" ]; then
        CMD_JSON="$(cat "${UPGRADE_CMD}")"
        rm -f "${UPGRADE_CMD}"
        run_upgrade "${CMD_JSON}"
    fi
    sleep 5
done