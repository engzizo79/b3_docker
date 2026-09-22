# Architecture

## Deployment model: single all-in-one container

One image contains the B3Hive daemon **and** the UI. Users pull the image, map one persistent data directory, and run it with docker compose. Upgrades are `stop / pull / start`. `RUN_UI=false` toggles a daemon-only (headless) container from the same image.

## Diagram

```
Internet
  │ (optional HTTPS — reverse proxy: nginx/Caddy, HSTS)
  ▼ :8080 (only published port when RUN_UI=true)
┌─────────────────────────────────────────────────┐
│  Container: b3hive                              │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │ Backend API proxy (FastAPI)               │  │
│  │  - serves static SPA                      │  │
│  │  - auth + CSRF + rate limiting            │  │
│  │  - RPC allowlist middleware               │  │
│  │  - monitoring / recovery / batch actions  │  │
│  └──────────────┬────────────────────────────┘  │
│                 │ JSON-RPC 2.0, Basic auth       │
│                 │ loopback only (127.0.0.1)      │
│  ┌──────────────▼────────────────────────────┐  │
│  │ b3coind (stock B3Hive, foreground)         │  │
│  │  RPC 127.0.0.1:32647 — never published    │  │
│  └───────────────────────────────────────────┘  │
└───────────────────────┬─────────────────────────┘
                        │ volume
Host data dir ───────────┘
(e.g. ~/.B3-CoinV2) ──▶ /data
  chain state, blocks, wallet, b3coin.conf
```

## Container layout (single image)

- One `b3hive` service in docker-compose.yml. The UI port is published (default 8080) and so is the node's P2P port (default 5647, `B3_P2P_PORT`) — the daemon's `listen=1` default is meaningless in Docker without a matching port publish, so publishing it is what makes "accept inbound connections" actually true, same as a native install. The node RPC port is a different matter and NEVER published, binding to `127.0.0.1` inside the container only — see [SECURITY.md](SECURITY.md).
- Image contents: `b3coind`, `b3coin-cli`, `b3coin-wallet` binaries (built externally, copied in; tags track B3Hive releases), Python backend, prebuilt static SPA. Base: lightweight Linux (Alpine if binaries link against musl, else Debian slim).
- Entrypoint responsibilities: generate `b3coin.conf` from env on first run (never overwrite an existing one), start `b3coind -confdir=.B3-CoinV2 -daemon=0` in the foreground, and start the backend when `RUN_UI` is not `false`. Simple process supervision keeps both alive in one container (one container, one process group — deliberate exception to one-process-per-container for pull-and-run simplicity).
- Non-root user `b3coin` (UID 1000). Healthcheck: daemon RPC with generous start_period (~5 min startup replay) + backend health endpoint when the UI runs.

## RUN_UI toggle

- `RUN_UI=true` (default): daemon + backend + UI, UI port published.
- `RUN_UI=false`: daemon only — headless staking boxes reuse the same image.

## Persistence & upgrades

- The user maps a host directory to `/data`. `b3coin.conf` is seeded from env on first run and then persists on the host volume.
- Upgrades: `docker compose pull && docker compose up -d` — new binaries come from the new image; chain state, wallet, and conf stay on the host. Wallet migrations (b3coin-wallet) are an explicit documented flow when a release requires one.

## Data flow

- **Dashboard**: SPA → backend → node (getblockchaininfo, getfinalitystatus, getbridgeinfo, getassetstate, gettxoutsetinfo, getmempoolinfo, getwalletinfo, listunspent, etc.).
- **Wallet operations**: SPA → backend (auth + 2FA check) → node (send, receive, stake, consolidate). Every wallet-affecting action requires an unlocked wallet (passphrase) and is audited.
- **Send flow**: SPA preview (dry-run via testmempoolaccept) → explicit user confirm → backend → node (createrawtransaction, signrawtransactionwithwallet, sendrawtransaction).
- **Explorer comparison**: backend polls https://explorer.b3hive.io/api/ (and chainz.cryptoid.info/b3), displays sync-lag.
- **Notifications**: `app/notifier.py` is one shared "detect → in-app alert
  → maybe push/webhook" pipeline used by both `app/monitor.py` (chain
  stall/lag/recovery) and `app/wallet_monitor.py` (polls `listtransactions`
  for new receives/sends/stakes — one poller instead of instrumenting every
  wallet-write code path). Delivery is per-type configurable (push and/or
  webhook) with a smart-coalescing digest: the first event of a type fires
  immediately and opens a cooldown window; anything else of that type
  inside the window folds into one summary sent when it closes, instead of
  one notification per event.

## Security layers

1. Loopback RPC inside the container (network isolation).
2. RPC allowlist in the backend proxy (never rely on network position alone).
3. Session auth + CSRF + rate limiting at the backend.
4. Explicit confirmation gate for wallet-affecting actions.
5. Carrier-output protection: batch tools select plain P2PKH outputs only.

See [SECURITY.md](SECURITY.md) for the full model.
