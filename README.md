# B3 Docker

One Docker container with the B3Hive daemon (b3coind) **and a full b3coin-qt replacement web UI** — send, receive, stake, consolidate, wallet management, accessible from anywhere including mobile phones. Security-first: TOTP 2FA for remote access (bypassed on localhost), RPC allowlist, carrier-output protection, and dry-run previews.

## Status

**Beta** — feature-complete for the core wallet workflow (send, receive, stake, consolidate, wallet management, setup wizard, remote access via Tailscale or Caddy). Expect rough edges; report them to the B3 team and **do not put more into a beta wallet than you can afford to lose**. See [PLAN.md](PLAN.md) for the roadmap and [AGENTS.md](AGENTS.md) for the binding project contract.

## Architecture (single all-in-one container)

One image contains b3coind + FastAPI backend proxy + static SPA. The browser never talks to the node RPC directly: the backend is the sole RPC client, over loopback inside the container, behind an allowlist. The only published port is the UI. `RUN_UI=false` runs the same image as a headless daemon. Remote access requires TOTP 2FA; localhost bypasses it (configurable; only loopback and addresses you list in `B3_LOCAL_ADDRS` count as local).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SECURITY.md](docs/SECURITY.md).

## Quick start

```bash
cp .env.example .env   # required by compose; all settings have safe defaults
docker compose up -d
# UI at http://localhost:8080
```

**First run in Docker:** the setup wizard is restricted to *local* clients, and only loopback counts as local. A browser on the Docker host arrives from the Docker bridge gateway, so the first page you see is "setup required", which shows the address the server saw (e.g. `172.17.0.1`). Add it to `.env`, then restart:

```bash
echo 'B3_LOCAL_ADDRS=172.17.0.1' >> .env   # use the address shown on the page
docker compose up -d
```

Finish the wizard (choose a password, enable 2FA). Anything not listed — every other machine on your LAN or the internet — always needs the password **and** TOTP 2FA. Details: [docs/SECURITY.md](docs/SECURITY.md#what-counts-as-local).

Your data persists in the mapped host directory (default `~/.B3-CoinV2`) — chain state, wallet, and `b3coin.conf` survive upgrades.

### Headless (daemon only)

Set `RUN_UI=false` in `.env` — same image, no UI, no published UI port.

### Upgrades

> **Behaviour change in the beta:** the Docker bridge gateway is no longer auto-treated as local. If you previously browsed from the Docker host and skipped 2FA, you will now be asked for TOTP (or, before a password is set, blocked from setup) until you set `B3_LOCAL_ADDRS` — see Quick start. Also, the developer console's default trusted networks are now loopback only; add ranges under Settings → Console if you relied on the old default.

```bash
docker compose pull
docker compose up -d
```

New image = new b3coind binaries. Data stays on the host volume. (If a release needs a `b3coin-wallet` migration, it's an explicit documented step.)

### Production with HTTPS (internet-facing)

For internet-facing deployments, use the Caddy profile — automatic Let's Encrypt TLS, HSTS, and no direct UI port published:

```bash
cp .env.example .env           # also set B3_DOMAIN (DNS A record → this host)
docker compose -f docker-compose.prod.yml up -d
# UI at https://your-domain
```

Cookies get the `Secure` flag automatically via `X-Forwarded-Proto` (`COOKIE_SECURE` overrides). Remote sessions always require TOTP 2FA.

### Secrets hygiene

`scripts/scan_secrets.sh` blocks any commit containing secrets or wallet data (installed as a git pre-commit hook). Run it manually anytime:

```bash
./scripts/scan_secrets.sh        # whole tree
./scripts/scan_secrets.sh --cached  # staged files
```

## B3Hive facts (distilled)

- Bitcoin-Core-31.1-derived v1.1.5; JSON-RPC 2.0 with Basic auth; 9-decimal B3 amounts (1 B3 = 1e9 base units).
- Legacy P2PKH addresses only (version byte 0x3F, S prefix). Witness/bech32 rejected.
- Data dir `.B3-CoinV2`, config at `<datadir>/b3coin.conf`; v1.1.4 uses `-datadir` (the `-confdir` flag was v1.1.3-only and is rejected by newer builds).
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
