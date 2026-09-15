# B3 Docker

Containerize the B3Hive daemon (b3coind) and provide a modern, mobile-friendly web UI that interacts with the node RPC seamlessly — security-first, with chain-state monitoring, automated/assisted recovery, and configurable batch actions.

## Status

Project scaffolded and ready for implementation. See [PLAN.md](PLAN.md) for the full phased implementation roadmap and [AGENTS.md](AGENTS.md) for the binding project contract.

## Architecture

Internet → TLS reverse proxy → Web SPA → Backend API proxy (FastAPI) → B3Hive node (b3coind) RPC.

The browser never talks to the node RPC directly. The backend proxy is the sole RPC client, behind an allowlist. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SECURITY.md](docs/SECURITY.md).

## Quick start (once implemented)

```bash
cp node/b3coin.conf.example node/b3coin.conf  # fill in RPC credentials
cp .env.example .env                          # fill in config
docker compose up -d                          # node + web
# UI at http://localhost:8080
```

## B3Hive facts (distilled)

- Bitcoin-Core-31.1-derived v1.1.5; JSON-RPC 2.0 with Basic auth; 9-decimal B3 amounts (1 B3 = 1e9 base units).
- Legacy P2PKH addresses only (version byte 0x3F, S prefix). Witness/bech32 rejected.
- Carrier outputs (B3S1 stake, B3A1 assets, B3MC metadata) are NEVER spent by batch tools — plain P2PKH only.
- Node startup takes ~5 min at current height — healthchecks use generous timeouts.

## Cross-project reuse

This project reuses verified patterns from the b3_monitoring project:
- `b3-utxo-combiner` — P2PKH-only UTXO filter, Decimal math, dry-run, unlock-state restore.
- `research/b3hive/` — RPC surface reference.
- `explorer/b3api/` — REST API patterns.
- Esplora REST API — explorer comparison data source.

## License

TBD
