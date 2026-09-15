# B3 Docker Project

## Purpose

Containerize the B3Hive daemon (b3coind) and provide a modern, mobile-friendly web UI that talks to the node RPC seamlessly through a secure backend proxy. The stack is internet-facing and may serve wallets holding coins — security is the top priority.

## B3Hive Node Facts (verified against B3-CoinV2 v1.1.5 source and live nodes)

- B3Hive v1.1.5 is Bitcoin-Core-31.1-derived. Binaries: b3coind, b3coin-cli, b3coin-wallet, b3coin-qt. Requires the confdir flag -confdir=.B3-CoinV2; config file b3coin.conf.
- RPC: JSON-RPC 2.0 over HTTP with Basic auth (rpcuser/rpcpassword or rpcauth). Wallet-scoped RPCs (listunspent, getaddressesbylabel, listflowmeshmarkets, getstakinginfo) require a loaded wallet (loadwallet).
- Amounts: 9 decimals, 1 B3 = 1e9 base units. Parse with Decimal, never float.
- Addresses: legacy P2PKH only, version byte 0x3F (S prefix). Witness/bech32 is rejected.
- Chain: seal at height 810,000; modern PoS from 811,001; ~1-minute blocks; BLS finality with epochs; ~822k blocks at time of writing.
- Carrier outputs must NEVER be spent by batch tools: B3S1 stake carriers, B3A1 colored-asset envelopes, B3MC metadata cells. Plain P2PKH (script 76a914..88ac) only.
- Key read-only RPCs: getblockchaininfo, getnetworkinfo, getblockchaininfo, getfinalitystatus, getbridgeinfo, getassetstate, gettxoutsetinfo, getmempoolinfo, getindexinfo, getstakinginfo (wallet-scoped).
- Node startup takes ~5 minutes at current height (block-index scan + O(modern-span) finality/bridge tracker replay). Container healthchecks MUST use generous timeouts and must not kill the node during initial sync.
- Staking survives wallet re-lock: the staker copies signing material at Start; re-locking the wallet does not stop block production.
- Full RPC surface reference and RPC-probing recipes: <b3_monitoring>/research/b3hive/ (rpc_probe.json, help.json) and B3-CoinV2 source at <b3_monitoring>/research/B3-CoinV2 (branch release/v1.1.5).

## Local Contracts

- Use git and a modern software architecture from the beginning. No quick-and-dirty shortcuts.
- Test node RPC: <test-node RPC URL> (monitoring-only wallet, credentials kept outside this repo). Production uses different credentials and ports.
- NEVER commit secrets: .env files, b3coin.conf with rpcpassword, wallet.dat, passphrases. The .gitignore covers them.
- The browser NEVER talks to node RPC directly. The backend proxy is the only RPC client, behind an RPC allowlist.
- Every wallet-affecting action requires an explicit confirmation flow and an audit log entry.
- Reuse verified patterns from the b3-utxo-combiner tool: <b3_monitoring>/tools/b3-utxo-combiner (P2PKH-only UTXO filter, Decimal 9dp math, unlock-state restore, testmempoolaccept dry-run, fee floor at minrelaytxfee).
- Explorer cross-references: https://explorer.b3hive.io (own Esplora stack, REST /api/...) and the obsolete chainz.cryptoid.info/b3. The b3_monitoring project owns the explorer code; this project consumes its REST API for chain-state comparison.

## Ownership

- docker/ — container build: node image, web image, compose files, entrypoints
- node/ — b3coin.conf template, data layout docs
- backend/ — FastAPI (or equivalent) proxy: auth, RPC allowlist, monitoring, recovery, batch actions
- frontend/ — mobile-friendly SPA
- docs/ — architecture, security model, RPC surface notes
- scripts/ — build, test, deploy helpers

## Verification (project-wide)

- Docker: images build clean; compose up brings node + web; healthchecks green after sync window.
- Backend: pytest suite with a mocked RPC layer; no test requires a funded wallet.
- Frontend: unit tests for pure helpers; real-browser screenshot verification at mobile 375px AND desktop widths for layout-affecting changes (SSR-blind bug class — see b3_monitoring AGENTS.md lessons).
- Security: secrets scan before every commit; auth/CSRF/rate-limit tests in the backend suite.