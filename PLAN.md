# B3 Docker — Implementation Plan

## Overview

Ship the B3Hive daemon (b3coind) **and a modern, mobile-friendly web UI together in a single Docker container**. A user pulls one image, maps one persistent data directory, and runs it with docker compose. Security is paramount: the stack is internet-facing and may serve wallets holding coins.

## Architecture

```
Internet ── (optional TLS reverse proxy) ──▶ :8080
                                             ┌──────────────────────────────────┐
                                             │  Single container: b3hive        │
                                             │  ┌────────────────────────────┐  │
                                             │  │ b3coind                    │  │
                                             │  │  RPC 127.0.0.1:32647       │  │
                                             │  └────────────▲───────────────┘  │
                                             │  ┌────────────┴───────────────┐  │
                                             │  │ Backend API proxy (FastAPI)│  │
                                             │  │ + static SPA  →  :8080     │  │
                                             │  │ sole RPC client, loopback  │  │
                                             │  └────────────────────────────┘  │
                                             └───────────────┬──────────────────┘
                                                             │ volume
Host data dir (e.g. ~/.B3-CoinV2) ───────────────────────────┴─▶ /data
                                              (chain state, wallet, b3coin.conf)
```

### Principles

1. **One container, one command.** `docker compose up -d` is all a user needs. Node + UI ship in the same image.
2. **Full b3coin-qt alternative, not read-only.** The web UI replaces the Qt desktop wallet — send, receive, stake, consolidate, wallet management — accessible from anywhere including mobile phones.
3. **Browser NEVER touches node RPC.** The backend proxy is the sole RPC client — over loopback *inside* the container — behind an RPC allowlist. The RPC port is never published.
4. **2FA for remote, bypass on localhost.** TOTP is required when accessing from the internet; localhost connections skip 2FA for convenience (configurable). App login + wallet passphrase are always required.
5. **RUN_UI toggle.** `RUN_UI=false` runs a daemon-only (headless) container. Default: UI enabled.
6. **Upgrades = stop / pull / start.** Binaries live in the image; data lives on the host volume. A new B3Hive release is just a new image tag: `docker compose pull && docker compose up -d`.
7. **Dry-run by default.** Every wallet-affecting batch action has a preview path (testmempoolaccept) that signs and validates but never broadcasts.
8. **No secrets in git.** Env files, b3coin.conf with credentials, wallet.dat, and passphrases are gitignored and mounted/generated at runtime.

## Container Design (single image)

- Base: `debian:bookworm-slim` (test `alpine` first — fall back to slim Debian if the B3Hive binaries don't link against musl).
- B3Hive binaries (`b3coind`, `b3coin-cli`, `b3coin-wallet`) built externally per release and copied in — the image does NOT compile from source. Image tags track B3Hive releases (e.g. `b3hive:v1.1.5`, `b3hive:latest`).
- Python backend (FastAPI) and the prebuilt static SPA are included in the same image.
- Entrypoint:
  1. Generate `b3coin.conf` into the data dir from env vars if not already present (existing conf is never overwritten).
  2. Start `b3coind -datadir=/data -daemon=0` (foreground; v1.1.4 uses -datadir — the -confdir flag is REJECTED by v1.1.4; conf lives at /data/b3coin.conf).
  3. If `RUN_UI` is not `false`, also start the backend, which serves the SPA and proxies allowlisted RPC to `127.0.0.1`.
- RPC binds to `127.0.0.1` inside the container only — unreachable from other containers or the host.
- Non-root user: `b3coin` (UID 1000).
- Healthcheck: `b3coin-cli -rpcconnect=127.0.0.1 getblockchaininfo` with a generous `start_period` (~5 min startup replay), plus the backend health endpoint when the UI is enabled.

### RUN_UI toggle

- `RUN_UI=true` (default): daemon + backend + UI; UI port published.
- `RUN_UI=false`: daemon only; no backend process, no published UI port. Same image — useful for headless staking boxes.

### Data dir & persistence

- The user maps a host directory (e.g. `~/.B3-CoinV2` — the daemon's default dir name is `.B3-CoinV2`) to `/data`.
- `b3coin.conf` lives inside the data dir and persists across upgrades; it is seeded from env on first run only.

### Upgrade flow

```bash
docker compose pull
 docker compose up -d
```

- The new image carries the new b3coind binaries; chain state, wallet, and conf stay on the host volume.
- If a release requires a `b3coin-wallet` migration, that is handled explicitly (documented / assisted flow in a later phase).

### Compose

- `docker-compose.yml`: single `b3hive` service, only the UI port published, data-dir volume, `env_file: .env`, `restart: unless-stopped`.
- `docker-compose.test.yml`: same image in test mode (test RPC, no real wallet).

## Security Model

### Authentication

- Backend API requires session-based auth (JWT or signed session cookie) with CSRF protection for browser-initiated mutations.
- Wallet-affecting actions (unlock, send, consolidate) always require the wallet passphrase; remote sessions additionally require TOTP 2FA (localhost bypass configurable).
- Rate limiting on auth endpoints (login, unlock) to prevent brute force.

### RPC Allowlist

The backend proxy only forwards a curated set of RPCs. Everything else returns 403.

| Category | Allowed RPCs |
|---|---|
| Chain state (read) | getblockchaininfo, getnetworkinfo, getblock, getblockheader, getblockstats, gettxout, gettxoutproof, getrawtransaction, getblockcount, getbestblockhash, getdifficulty |
| Finality / bridge / assets (read) | getfinalitystatus, getbridgeinfo, getassetstate, getindexinfo |
| Mempool (read) | getmempoolinfo, getrawmempool |
| Supply (read) | gettxoutsetinfo |
| Wallet (read) | getwalletinfo, listunspent, getaddressesbylabel, listaddressgroupings, listtransactions, getbalance, getbalances |
| Wallet (write — gated) | walletpassphrase, walletlock, createrawtransaction, signrawtransactionwithwallet, sendrawtransaction, testmempoolaccept |
| Staking (read) | getstakinginfo (wallet-scoped) |
| Node control (gated) | loadwallet (required at startup for wallet-scoped RPCs) |

**Never proxied:** importprivkey, importmulti, dumpprivkey, dumpwallet, encryptwallet, setgenerate, signmessagewithprivkey, any mining RPC.

Even though the RPC is loopback-only inside the container, the allowlist remains a hard second layer — never rely on network position alone.

### Carrier-Output Protection (batch actions)

Every UTXO-selection path (consolidation, send) filters to **plain P2PKH only** (script `76a914<20 bytes>88ac`). B3S1 stake carriers, B3A1 colored-asset envelopes, and B3MC metadata cells are never selected. This is the verified pattern from the b3-utxo-combiner tool.

### Secret Storage

- Node RPC credentials: generated into `b3coin.conf` inside the data dir at first run (from env), or user-supplied; never committed.
- Wallet passphrase: OS keyring on the backend host (preferred) or a `config.ini` with mode 0600; never committed.
- API session secrets: generated at deploy time, stored in the backend env.

## Features

### First-Use Setup Wizard (bootstrap + node config)

- [x] Wizard auto-opens on first login (`.wizard_complete` marker in data dir); re-openable from Settings.
- [x] Bootstrap sync: fetches the explorer's live manifest (`/api/setup/bootstraps`), user picks a snapshot, entrypoint worker stops the daemon, downloads, SHA256-verifies, extracts, restarts; progress polled from `bootstrap.progress.json`; rollback restarts with the previous chain on failure.
- [x] Node config editor: safe subset of `b3coin.conf` keys (txindex, listen, maxconnections, proxy, bantime); RPC/consensus keys locked; per-key validation; atomic rewrite preserving unrecognized lines; restart-node wired to the supervisor recovery command.
- [x] Backend guarded by session/2FA/CSRF; all wizard actions audited.

### 1. Dashboard (chain-state overview)

- Dashboard: block height, sync progress, peer count, finality epoch/quorum, bridge status, supply, mempool size.
- Comparison against the official explorer: our own `https://explorer.b3hive.io` (Esplora REST) and the obsolete `chainz.cryptoid.info/b3`.
- Alerts (configurable): stall detection (no new block in N minutes), peer count drop, finality quorum erosion, sync lag vs explorer tip.

### 2. Full Wallet Operations (b3coin-qt replacement)

- **Balances**: total, confirmed, unconfirmed, staking, immature — per-address and aggregate.
- **Address book**: labels, balances, QR codes (generate new addresses, organize by label).
- **Send**: single + multi-recipient, fee estimation, testmempoolaccept preview, coin control (select which UTXOs to spend).
- **Receive**: generate addresses with labels, show QR codes.
- **Transaction history**: paginated, filterable (sent/received/staking/asset), export to CSV.
- **Wallet lock/unlock**: configurable timeout; staking survives re-lock.
- **Staking management**: start/stop staking, staking status, validator info — all via the UI (the `startstaking` RPC, not a conf option).

### 3. Authentication & Access Control

- App login (username + password) required for all access.
- **2FA (TOTP)** required for remote access; bypassed on localhost (configurable).
- Wallet passphrase required for every wallet-affecting action (unlock, send, consolidate, stake).
- Rate limiting on login, unlock, and 2FA endpoints.
- TOTP setup: QR code scan on first login, recovery codes generated once.

### 4. Automated / Assisted Recovery

- Configurable: if the node stalls (no new block for a configurable window), the backend can:
  - Level 1: alert only (default).
  - Level 2: restart the daemon process inside the container (assisted).
  - Level 3: reindex from a snapshot (automated, needs a pre-staged snapshot).
- Reindex / rescan progress bar (from `getindexinfo` and `debug.log` parsing).

### 5. Configurable Batch Actions

- **Stake consolidation**: reuse the b3-utxo-combiner logic — select P2PKH UTXOs from source addresses/labels, batch N inputs per tx, dry-run by default, send to a configured receiving address.
- **Sweep**: send all spendable balance to one address.
- **Scheduled runs**: cron-like recurring batch actions (e.g. consolidate daily).
- Every batch action: preview (dry-run) → explicit user confirmation → execute → audit log.

## Implementation Phases

### Phase 1: All-in-One Container (foundation)

- [x] Dockerfile: single image — b3coind/b3coin-cli/b3coin-wallet binaries + Python backend skeleton + prebuilt SPA placeholder, non-root user.
- [x] Entrypoint: conf generation from env (first run only), daemon foreground, RUN_UI toggle (backend starts only when enabled), process supervision (daemon + backend in one container).
- [x] b3coin.conf template: txindex=1, staking config, RPC bind to 127.0.0.1 only.
- [x] docker-compose.yml: single service, UI port published, RPC never published, data-dir volume mapping.
- [x] Healthcheck: daemon RPC with generous start_period; backend health endpoint when RUN_UI enabled.
- [x] Verify: `docker compose up -d` → healthy node + UI reachable; RUN_UI=false → daemon-only; stop/pull/start upgrade flow keeps data.

### Phase 2: Backend API Proxy (security core)

- [ ] FastAPI app: config loading from env, RPC client wrapper (JSON-RPC 2.0, Basic auth, Decimal parsing) — talks to 127.0.0.1 inside the container.
- [ ] Wallet autoload on startup (loadwallet) for wallet-scoped RPCs.
- [ ] Auth: session + CSRF, rate limiting on login/unlock.
- [ ] RPC allowlist middleware (403 on non-allowlisted RPCs).
- [ ] Read-only endpoints: chain state, finality, bridge, supply, mempool, wallet info.
- [ ] Health/status endpoint for Docker.
- [ ] pytest suite with mocked RPC (no funded wallet needed).
- [ ] Verify: container up — backend proxies allowlisted reads, rejects everything else.

### Phase 3: Frontend SPA (mobile-first UI)

- [ ] Vue 3 or Svelte SPA: dashboard (chain state), responsive 375px–1920px, built into the image and served by the backend.
- [ ] Monitoring views: chain state, finality, bridge, supply, explorer comparison.
- [ ] Wallet views: balance, address book, transaction history.
- [ ] Send view with fee estimation and testmempoolaccept preview.
- [ ] Real-browser screenshot verification at 375px AND desktop for every layout-affecting change.
- [ ] Verify: UI loads in the container, shows live chain state from the node.

### Phase 4: User-Definable Batch Actions

Users define their own batch actions as declarative recipes (JSON, never
code). The backend engine interprets recipes and enforces hard safety rails
regardless of recipe content.

Recipe schema (validated on create/update):
- filters: sources (addresses or `label:` names), min_utxo_value, min_conf,
  max_conf, sort (smallest|largest|oldest)
- action: type=consolidate, destination (P2PKH)
- limits: inputs_per_tx (cap 675), max_batches (cap by settings), min_output
- fees: mode=estimate|fixed, fee_rate, fee_target, fallback_fee_rate

Engine rails (non-overridable):
- Plain P2PKH script regex only (76a914...88ac) — carriers (B3S1/B3A1/B3MC)
  are NEVER selected, whatever the recipe says.
- spendable-only, Decimal 9dp, fee floor clamp at minrelaytxfee,
  testmempoolaccept gate before every broadcast, abort on first rejection,
  unlock-state restore, audit log per run.

API: recipe CRUD (`/api/batch/recipes`), preview (plan only, no signing,
  no passphrase), execute (unlocked session + CSRF + confirm token from
  preview; signs, dry-runs each batch, broadcasts only mempool-accepted txs).

- [ ] Backend: recipe store (SQLite) + engine (pure planning) + executor + API.
- [ ] Frontend: Batch view — recipe list, create/edit forms, preview results,
  execute with explicit confirm + result report.
- [ ] Scheduled actions: backend scheduler for recurring recipes (opt-in per recipe).
- [ ] Tests: engine unit tests (carrier exclusion, fee floor, caps) + mocked RPC
  end-to-end (preview never signs; execute aborts on rejection; audit written).
- [ ] Verify: preview on the test node produces a valid plan without broadcasting.

### Phase 5: Recovery + Alerts

- [ ] Stall detection + alerting (configurable: alert / restart daemon / reindex).
- [ ] Reindex/rescan progress UI.
- [ ] Explorer comparison: poll explorer.b3hive.io REST + chainz, display sync-lag.
- [ ] Verify: simulate a stall (stop node), verify alert fires and recovery runs per config.

### Phase 6: Distribution + Hardening

- [ ] Publish the image to a registry (Docker Hub / GHCR) with per-release tags; CI builds a new image when a B3Hive release drops → upgrades stay pull-and-run.
- [ ] TLS / reverse proxy config (nginx or Caddy) with HSTS for internet-facing setups.
- [ ] Secrets scan in CI (detect-secrets or trufflehog).
- [ ] Security review: auth bypass, RPC allowlist holes, carrier-output filter tests, rate-limit tests.
- [ ] Deployment docs: env files, healthchecks, log rotation, upgrade runbook (stop / pull / start, wallet migration notes).
- [ ] Verify: full stack on a VPS, HTTPS, all features, no secrets committed.

## Cross-Project Reuse

| Source (b3_monitoring) | Reuse in b3_docker |
|---|---|
| `tools/b3-utxo-combiner/b3_utxo_combiner.py` | P2PKH filter, Decimal math, unlock-state restore, dry-run pattern — extract into a shared module for the backend |
| `research/b3hive/help.json`, `rpc_probe.json` | RPC surface reference for the allowlist |
| `research/B3-CoinV2` (release/v1.1.5 source) | Node config defaults, startup behavior, carrier-output script patterns |
| `explorer/b3api/app.py` | REST API patterns, caching, stale-serve patterns |
| Esplora REST API (`https://explorer.b3hive.io/api/`) | Explorer comparison data source |

## Non-Goals (this project)

- Splitting the stack into multiple containers (single all-in-one image is a deliberate requirement; RUN_UI covers the headless case).
- Building a full block explorer (that is b3_monitoring's job — we consume its API).
- Compiling B3Hive from source inside Docker (binaries are built externally and copied in).
- Consensus changes or node modifications (we run the stock daemon).
- Handling B3A1 colored assets in batch actions (P2PKH only — assets are future work).
