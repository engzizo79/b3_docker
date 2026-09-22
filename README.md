# B3 Docker

One Docker container with the B3Hive daemon (b3coind) **and a full b3coin-qt replacement web UI** — send, receive, stake, consolidate, wallet management, accessible from anywhere including mobile phones. Security-first: TOTP 2FA for remote access (bypassed on localhost), RPC allowlist, carrier-output protection, and dry-run previews.

## Status

**Beta** — feature-complete for the core wallet workflow (send, receive, stake, consolidate, wallet management, setup wizard, remote access via Tailscale or Caddy). Expect rough edges; report them to the B3 team and **do not put more into a beta wallet than you can afford to lose**. See [PLAN.md](PLAN.md) for the roadmap and [AGENTS.md](AGENTS.md) for the binding project contract.

## Architecture (single all-in-one container)

One image contains b3coind + FastAPI backend proxy + static SPA. The browser never talks to the node RPC directly: the backend is the sole RPC client, over loopback inside the container, behind an allowlist. The only published port is the UI. The same image can also run as a UI in front of an existing node, or as a headless daemon (see [Run modes](#run-modes)). Remote access requires TOTP 2FA; localhost bypasses it (configurable; local = loopback, the Docker host, and addresses in `B3_LOCAL_ADDRS`).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SECURITY.md](docs/SECURITY.md).

## Is this safe to run with a real wallet?

This handles real coins, so you shouldn't have to take that on faith — the source is public, so read it instead of trusting this paragraph. Here's exactly what it does and doesn't do, and what you're actually trusting if you run it.

**What never leaves your machine:** your wallet passphrase is held only in your browser session and the backend's memory; it's never written to disk unencrypted, and the backend's own database only ever stores it Fernet-encrypted — and only if you opt into autostake, to unlock the wallet locally for its own scheduled checks. In the default (all-in-one) mode, it's sent to the node over `127.0.0.1` inside the container, nowhere else. Node RPC credentials are generated locally into `b3coin.conf` / `.secrets.env` on your own data volume and never transmitted. The frontend is fully self-hosted (`Content-Security-Policy: default-src 'self'` — see [docs/SECURITY.md](docs/SECURITY.md)): no CDN scripts, no fonts, no analytics, nothing loaded from a third party at all.

**The few things that do reach the internet, and why:** a GitHub API call to check/download `b3coind` releases (Settings → node install/upgrade, or automatically on a fresh headless run — see [Run modes](#run-modes)); an optional block-height check against `EXPLORER_URL` (set it empty to disable); and a webhook POST of a short alert message, only if you set `WEBHOOK_URL` yourself. None of these ever carry your passphrase, keys or RPC credentials — check `backend/app/monitor.py`, `backend/app/daemon_release.py` and `backend/app/routers/setup.py` yourself.

**What you're trusting, spelled out:**
- **This project's code** — public, and small enough to actually read. Issues and PRs welcome.
- **The published image** (`ghcr.io/engzizo79/b3hive`) — built straight from this repo by the GitHub Actions workflow in [`.github/workflows/release.yml`](.github/workflows/release.yml); nothing hand-uploaded. If you'd rather not trust that pipeline (or my GitHub account) at all, build it yourself: `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.
- **The `b3coind` binary itself** — downloaded from B3-Coin/B3-CoinV2's own GitHub releases, not built by this project. Its SHA256 is checked against that same release's checksums before it's installed, which catches a corrupted or tampered *download* — it does not audit the upstream project's own code. You're trusting B3Hive's binaries the same as you would running them any other way.

None of that makes this bulletproof — it's beta software (see [Status](#status)), and "the code looks fine" is not a substitute for keeping backups and not risking more than you can afford to lose.

## Getting started

**You need:** Docker with Compose v2.24 or newer (`docker compose version`), a 64-bit Linux/Windows/Mac machine (the image is `linux/amd64` only, so no Raspberry Pi or Apple Silicon without emulation), and a few GB of free disk (the chain is currently about 1.3 GB and grows). The image is published to GitHub Container Registry as `ghcr.io/engzizo79/b3hive`.

### Option A: Docker Compose (recommended)

You only need `docker-compose.yml`; cloning the repo is optional.

```bash
mkdir b3hive && cd b3hive
curl -O https://raw.githubusercontent.com/engzizo79/b3_docker/master/docker-compose.yml
mkdir -p ~/.B3-CoinV2   # the data folder compose maps in — create it before first run
docker compose up -d
```

Then open **http://localhost:8080 on the machine that runs Docker**. The setup wizard starts by itself: choose a login password, pick how to sync (a chain snapshot is much faster than syncing from scratch), and create or load a wallet. The first node start takes about 5 minutes or more; `docker ps` shows `(healthy)` once it is up.

> Repo is private, or the package is? Log in first with a GitHub token that has the `read:packages` scope: `echo "$GITHUB_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin`.

Settings are optional. To change any, copy `.env.example` to `.env` next to the compose file (`cp .env.example .env`); see the [settings table](docs/GUIDE.md#settings-env-or--e--all-optional). Everything has a safe default.

Where things live: chain, wallet and `b3coin.conf` are stored in the host folder `~/.B3-CoinV2` (change it with `B3_DATA_DIR` in `.env`). **Deleting that folder deletes the wallet, so back it up** and keep your wallet passphrase somewhere safe.

### Option B: plain `docker run`

```bash
docker run -d --name b3hive --restart unless-stopped \
  -p 8080:8080 \
  -v b3hive-data:/data \
  --stop-timeout 600 \
  ghcr.io/engzizo79/b3hive:latest
```

- `-v ...:/data` is required. It can be a named volume (as above) or a host folder such as `-v ~/.B3-CoinV2:/data`. Without persistent storage the app refuses to start, because the chain and wallet would vanish with the container.
- `--stop-timeout 600` gives the node time to shut down cleanly. Compose sets this for you.
- Add settings with `-e NAME=value` or `--env-file .env`.
- Only the UI port is published. The node RPC port never is.

### Everyday commands (Compose)

| Do this | Command |
|---|---|
| Watch the logs | `docker compose logs -f` |
| Stop (data is kept) | `docker compose down` |
| Start again | `docker compose up -d` |
| Restart | `docker compose restart` |
| Update to the newest release | `docker compose pull && docker compose up -d` |
| Check health | `docker ps` (look for `healthy`; allow ~10 min on first start) |

With plain `docker run`, use `docker logs -f b3hive`, `docker stop b3hive`, and to update: `docker pull ghcr.io/engzizo79/b3hive:latest`, then `docker rm -f b3hive` and run the same `docker run` command again (your data stays in the volume).

### Versions: `latest` or pinned

The default tag is `latest`, which always points at the newest release, so updating is just pull and up. This is a beta: if you would rather choose when to change, pin a version with `B3_IMAGE_TAG=v0.8.13-beta` in `.env` (or on the command line) and change it deliberately. Available versions are listed under the repository's tags. To build from source instead: `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.

### First run and other devices

The Docker host counts as local, so opening `http://localhost:8080` on it skips 2FA and starts the wizard. Every other device (phone, laptop, LAN, Tailscale) always needs the password **and** TOTP 2FA, which you enable in the wizard or in Settings. For access from outside, enable the bundled Tailscale in Settings (HTTPS from anywhere, no domain needed) or use the HTTPS section below.

> Docker Desktop (Windows/Mac) or rootless Docker? LAN clients may look like the host there. Set `B3_TRUST_DOCKER_HOST=false` (or publish the port as `127.0.0.1:8080:8080`); see [docs/SECURITY.md](docs/SECURITY.md#what-counts-as-local).

More help and troubleshooting: [docs/GUIDE.md](docs/GUIDE.md).

### Run modes

The same image runs in three modes:

| Mode | How | What runs |
|---|---|---|
| **Full** (default) | nothing to set | b3coind + backend + UI in one container. The setup wizard downloads the daemon, applies your node settings and optional chain snapshot, then starts it. |
| **UI only** (external daemon) | pick "Use an existing node" in the wizard's Node step, or set `B3_DAEMON_MODE=external` plus `EXT_RPC_HOST`, `EXT_RPC_PORT`, `EXT_RPC_USER`, `EXT_RPC_PASSWORD` | Backend + UI only. No local daemon is started or downloaded; every RPC goes to the node you named. |
| **Headless** (daemon only) | `RUN_UI=false` | b3coind only, no UI and no published UI port. |

**UI only** suits a node you already run (another container, a VPS, a home server). The node must be reachable from the container, have its RPC enabled with `rpcallowip` covering the container, and have the wallet loaded. The backend still enforces its RPC allowlist and the login/2FA rules, but RPC credentials and traffic now cross a network, so keep that link private (same host, a LAN you trust, or Tailscale) and never expose node RPC to the internet. Daemon install and upgrade are managed-mode features and are not available here; update the external node yourself.

**Headless notes:** with no wizard, the entrypoint installs the daemon itself on the first run: the newest stable release, or the one named in `B3_DAEMON_VERSION` (e.g. `v1.1.4`). The download is SHA256-verified. If it fails (no network, unknown version) the container exits with an error so your restart policy retries, rather than idling with no node. There is also no chain-snapshot step, so a fresh headless node syncs from block 0, which takes a long time. RPC listens on loopback inside the container only; edit `node/b3coin.conf` on the volume if you need it reachable. To use a snapshot, run the wizard once with the UI enabled, then switch `RUN_UI=false` on the same volume.

### Upgrades

```bash
docker compose pull
docker compose up -d
```

If you pinned `B3_IMAGE_TAG`, change it first. The image contains the UI and backend only. The b3coind daemon lives on your data volume and is updated separately from the UI, so a new image does not change your node version. Data stays on the host volume. (If a release needs a `b3coin-wallet` migration, it's an explicit documented step.)

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
