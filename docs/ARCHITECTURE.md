# Architecture

## Diagram

```
Internet
  │ HTTPS
  ▼
[Reverse Proxy / TLS]  (nginx or Caddy, HSTS)
  │
  ▼
[Web Frontend SPA]  (Vue 3 or Svelte, mobile-first, static files)
  │ same-origin API
  ▼
[Backend API Proxy]  (FastAPI, async)
  │   - auth + CSRF
  │   - RPC allowlist middleware
  │   - monitoring + recovery + batch actions
  │ JSON-RPC 2.0, Basic auth (internal Docker network only)
  ▼
[B3Hive Node]  (b3coind, stock v1.1.5)
```

## Container layout

- `node` service: b3coind binary in a lightweight Linux image, data volume at /data/.B3-CoinV2, non-root user, RPC bound to internal interface only, generous healthcheck start_period for the ~5-min startup replay.
- `web` service: FastAPI backend + static SPA, the only published port, talks to node over the internal network.
- The node RPC port is NEVER published to the host.

## Data flow

- Read-only monitoring: SPA → backend → node (getblockchaininfo, getfinalitystatus, getbridgeinfo, getassetstate, gettxoutsetinfo, getmempoolinfo, getwalletinfo, listunspent, etc.).
- Wallet-affecting actions: SPA preview (dry-run via testmempoolaccept) → explicit user confirm → backend → node (createrawtransaction, signrawtransactionwithwallet, sendrawtransaction).
- Explorer comparison: backend polls https://explorer.b3hive.io/api/ and chainz.cryptoid.info/b3, displays sync-lag.
