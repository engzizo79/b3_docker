# B3 Docker — Implementation Plan

## Overview

Containerize the B3Hive daemon (b3coind) and build a modern, mobile-friendly web UI that interacts with the node RPC seamlessly. Security is paramount: the stack is internet-facing and may serve wallets holding coins.

## Architecture

```
Internet ── HTTPS ── [Reverse Proxy / TLS]
                         │
                    [Web Frontend SPA]
                         │ (same-origin API)
                    [Backend API Proxy]  ◄── only RPC client
                         │ (JSON-RPC 2.0, Basic auth)
                    [B3Hive Node (b3coind)]
```

### Principles

1. **Browser NEVER touches node RPC directly.** The backend proxy is the sole RPC client, behind an RPC allowlist.
2. **Defense in depth.** TLS at the edge, auth at the backend, allowlist at the RPC layer, carrier-output protection in every batch action.
3. **Read-first.** Monitoring and chain-state views are read-only RPC calls. Wallet-affecting actions are a separate, gated surface requiring explicit confirmation.
4. **Dry-run by default.** Every batch action has a dry-run path (testmempoolaccept / preview) that signs and validates but never broadcasts.
5. **No secrets in git.** Env files, b3coin.conf with credentials, wallet.dat, and passphrases are gitignored and mounted at runtime.

## Container Design

### Node Image (lightweight Linux)

- Base: `debian:bookworm-slim` or `alpine` (if B3Hive binaries link cleanly against musl — test first; fall back to slim Debian).
- B3Hive binaries built externally (host or CI) and copied in — the image does NOT compile from source (keeps it small and reproducible).
- Data volume: `/data/.B3-CoinV2` (chain state, wallet, blocks).
- Config: `b3coin.conf` mounted read-only from a docker secret or env-generated file.
- Entrypoint: `b3coind -confdir=.B3-CoinV2 -daemon=0` (foreground).
- Healthcheck: `b3coin-cli -rpcconnect=127.0.0.1 -rpcport=$RPC_PORT getblockchaininfo` with generous `start_period` (5 min for initial sync / startup replay).
- Non-root user: `b3coin` (UID 1000).

### Web Image (backend + frontend)

- Backend: Python FastAPI (async, lightweight, type-safe) — the RPC proxy + auth + monitoring + batch actions.
- Frontend: single-page app (Vue 3 or Svelte — mobile-first, responsive). Served as static files by the backend or a sidecar.
- The backend talks to the node over the internal Docker network only; the node RPC port is never published to the host.

### Compose

- `docker-compose.yml`: two services (node, web), internal network, only web port published.
- `docker-compose.test.yml`: node pointed at test RPC (host.docker.internal), no real wallet.

## Security Model

### Authentication

- Backend API requires session-based auth (JWT or signed session cookie) with CSRF protection for browser-initiated mutations.
- Wallet-affecting actions (unlock, send, consolidate) require a second confirmation: either a passphrase re-entry or a TOTP step, configurable.
- Rate limiting on auth endpoints (login, unlock) to prevent brute force.

### RPC Allowlist

The backend proxy only forwards a curated set of RPCs. Everything else returns 403.

| Category | Allowed RPCs |
|---|---|
| Chain state (read) | getblockchaininfo, getnetworkinfo, getblockchaininfo, getblock, getblockheader, getblockstats, gettxout, gettxoutproof, getrawtransaction, getblockcount, getbestblockhash, getdifficulty |
| Finality / bridge / assets (read) | getfinalitystatus, getbridgeinfo, getassetstate, getindexinfo |
| Mempool (read) | getmempoolinfo, getrawmempool |
| Supply (read) | gettxoutsetinfo |
| Wallet (read) | getwalletinfo, listunspent, getaddressesbylabel, listaddressgroupings, listtransactions, getbalance, getbalances |
| Wallet (write — gated) | walletpassphrase, walletlock, createrawtransaction, signrawtransactionwithwallet, sendrawtransaction, testmempoolaccept |
| Staking (read) | getstakinginfo (wallet-scoped) |

**Never proxied:** importprivkey, importmulti, dumpprivkey, dumpwallet, encryptwallet, setgenerate, signmessagewithprivkey, any mining RPC.

### Carrier-Output Protection (batch actions)

Every UTXO-selection path (consolidation, send) filters to **plain P2PKH only** (script `76a914<20 bytes>88ac`). B3S1 stake carriers, B3A1 colored-asset envelopes, and B3MC metadata cells are never selected. This is the verified pattern from the b3-utxo-combiner tool.

### Secret Storage

- Node RPC credentials: `b3coin.conf` mounted as a docker secret or generated from env at container start; never committed.
- Wallet passphrase: OS keyring on the backend host (preferred) or a `config.ini` with mode 0600; never committed.
- API session secrets: generated at deploy time, stored in the backend env.

## Features

### 1. Chain-State Monitoring

- Dashboard: block height, sync progress, peer count, finality epoch/quorum, bridge status, supply, mempool size.
- Comparison against the two official explorers: our own `https://explorer.b3hive.io` (Esplora REST) and the obsolete `chainz.cryptoid.info/b3`.
- Alerts (configurable): stall detection (no new block in N minutes), peer count drop, finality quorum erosion, sync lag vs explorer tip.

### 2. Automated / Assisted Recovery

- Configurable: if the node stalls (no new block for a configurable window), the backend can:
  - Level 1: alert only (default).
  - Level 2: restart the node container (assisted).
  - Level 3: reindex from a snapshot (automated, needs a pre-staged snapshot).
- Reindex / rescan progress bar (from `getindexinfo` and `debug.log` parsing).

### 3. Configurable Batch Actions

- **Stake consolidation**: reuse the b3-utxo-combiner logic — select P2PKH UTXOs from source addresses/labels, batch N inputs per tx, dry-run by default, send to a configured receiving address.
- **Sweep**: send all spendable balance to one address.
- **Scheduled runs**: cron-like recurring batch actions (e.g., consolidate daily).
- Every batch action: preview (dry-run) → explicit user confirmation → execute → audit log.

### 4. Wallet Operations (advanced, user-friendly)

- Address book with labels, balances, and QR codes.
- Transaction history with filtering and export.
- Send (single + multi-recipient) with fee estimation and testmempoolaccept preview.
- Wallet lock/unlock with configurable timeout, staking-safe (re-lock does not stop staking).

## Implementation Phases

### Phase 1: Node Container (foundation)

- [ ] Dockerfile.node: lightweight image with b3coind binaries, non-root user, healthcheck.
- [ ] b3coin.conf template (txindex=1, staking config, RPC bind to internal interface only).
- [ ] docker-compose.yml: node + web skeleton, internal network, node RPC not published.
- [ ] Entrypoint script: generate b3coin.conf from env if not mounted, start b3coind in foreground.
- [ ] Verify: `docker compose up node` brings the node up; healthcheck passes after startup window.

### Phase 2: Backend API Proxy (security core)

- [ ] FastAPI app: config loading from env, RPC client wrapper (JSON-RPC 2.0, Basic auth, Decimal parsing).
- [ ] Auth: session + CSRF, rate limiting on login/unlock.
- [ ] RPC allowlist middleware (403 on non-allowlisted RPCs).
- [ ] Read-only endpoints: chain state, finality, bridge, supply, mempool, wallet info.
- [ ] Health/status endpoint for Docker.
- [ ] pytest suite with mocked RPC (no funded wallet needed).
- [ ] Verify: `docker compose up` — backend proxies allowlisted reads, rejects everything else.

### Phase 3: Frontend SPA (mobile-first UI)

- [ ] Vue 3 or Svelte SPA: dashboard (chain state), responsive 375px–1920px.
- [ ] Monitoring views: chain state, finality, bridge, supply, explorer comparison.
- [ ] Wallet views: balance, address book, transaction history.
- [ ] Send view with fee estimation and testmempoolaccept preview.
- [ ] Real-browser screenshot verification at 375px AND desktop for every layout-affecting change.
- [ ] Verify: UI loads, shows live chain state from the local test node.

### Phase 4: Batch Actions (consolidation)

- [ ] Backend: consolidation endpoint (P2PKH-only filter, Decimal 9dp, unlock-state restore, dry-run, fee floor).
- [ ] Frontend: batch action UI with preview → confirm → execute → audit log.
- [ ] Scheduled actions: backend scheduler for recurring consolidation.
- [ ] Tests: offline suite (reuse b3-utxo-combiner test patterns) + mocked RPC end-to-end.
- [ ] Verify: dry-run on the test node produces a valid plan without broadcasting.

### Phase 5: Recovery + Alerts

- [ ] Stall detection + alerting (configurable: alert / restart / reindex).
- [ ] Reindex/rescan progress UI.
- [ ] Explorer comparison: poll explorer.b3hive.io REST + chainz, display sync-lag.
- [ ] Verify: simulate a stall (stop node), verify alert fires and recovery runs per config.

### Phase 6: Production Hardening

- [ ] TLS / reverse proxy config (nginx or Caddy) with HSTS.
- [ ] Secrets scan in CI (detect-secrets or trufflehog).
- [ ] Security review: auth bypass, RPC allowlist holes, carrier-output filter tests, rate-limit tests.
- [ ] Deployment: docker compose for production, env files, healthchecks, log rotation.
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

- Building a full block explorer (that is b3_monitoring's job — we consume its API).
- Compiling B3Hive from source inside Docker (binaries are built externally and copied in).
- Consensus changes or node modifications (we run the stock v1.1.5 daemon).
- Handling B3A1 colored assets in batch actions (P2PKH only — assets are future work).