# B3 Hive — Quick Guide

One Docker container: the B3Hive node (b3coind) plus a web wallet UI.
Image: `ghcr.io/engzizo79/b3hive` (linux/amd64 only — no Apple Silicon / Raspberry Pi).

> **Beta.** Don't put more into the wallet than you can afford to lose, and back up your wallet and passphrase.

## Beginners: Docker Compose (recommended)

You need Docker, and `docker-compose.yml` in an empty folder.

**Requires Docker Compose v2.24 or newer** (check with `docker compose version`). Older versions fail with an `env_file`/`required` error — update Docker, or create an empty `.env` file (`touch .env`) as a workaround. The old `docker-compose` (v1) command is not supported.

```bash
docker compose up -d
```

1. Open **http://localhost:8080** in a browser **on the machine that runs Docker**.
2. The setup wizard opens by itself: choose a login password, then create or load a wallet.
3. Wait for the node to sync. The first start takes about 5 minutes or more.

A `.env` file is **optional** — only create one (`cp .env.example .env`) if you want to change settings.

```bash
docker compose logs -f          # watch what's happening
docker compose down             # stop (your data is kept)
B3_IMAGE_TAG=<new-version> docker compose pull
B3_IMAGE_TAG=<new-version> docker compose up -d      # upgrade
```

Your chain and wallet live in the host folder `~/.B3-CoinV2`. **Deleting that folder deletes the wallet.**

Other devices (phone, laptop) can reach the UI too, but always need the password **and** 2FA (authenticator app; enable it in Settings).

## Advanced: plain `docker run`

```bash
docker run -d --name b3hive --restart unless-stopped \
  -p 8080:8080 \
  -v ~/.B3-CoinV2:/data \
  --stop-timeout 600 \
  ghcr.io/engzizo79/b3hive:v0.8.5-beta
```

- The volume **must** map to `/data`. Without a persistent volume the app refuses to start (the chain and wallet would be lost with the container).
- Add settings with `-e NAME=value` or `--env-file .env`.
- The node's RPC port is never published; the UI is the only way in.

## Settings (`.env` or `-e`) — all optional

| Setting | Default | What it does |
|---|---|---|
| `WEB_PORT` | `8080` | Host port for the UI |
| `B3_DATA_DIR` | `~/.B3-CoinV2` | Host folder for chain + wallet (compose only) |
| `B3_IMAGE_TAG` | current release | Version to run (compose only) |
| `RUN_UI` | `true` | `false` = node only, no web UI |
| `UI_PASSWORD` | unset | Preset the login password (normally use the wizard) |
| `LOCALHOST_SKIP_2FA` | `true` | `false` = require 2FA even from the Docker host |
| `B3_LOCAL_ADDRS` | empty | Extra addresses treated as local (skip 2FA). IPs/CIDRs, nothing broader than /16. Never list a reverse proxy. |
| `B3_TRUST_DOCKER_HOST` | `true` | `false` = don't treat the Docker host as local (see Troubleshooting) |
| `WALLET_UNLOCK_TIMEOUT` | `60` | Seconds the wallet stays unlocked after entering the passphrase |
| `REQUIRE_SECURE_TRANSPORT` | `warn` | `block` = refuse remote logins over plain HTTP |
| `COOKIE_SECURE` | `auto` | Cookie Secure flag: `auto`, `always`, `never` |
| `STALL_ALERT_MINUTES` | `10` | Minutes without a new block before an alert |
| `STALL_LEVEL` | `alert` | Response to a stall: `alert`, `restart`, `reindex` |
| `WEBHOOK_URL` | empty | Discord/Slack-style URL for alerts |
| `EXPLORER_URL` | explorer.b3hive.io | Used to compare your sync height; empty disables |
| `RPC_USER` / `RPC_PASSWORD` / `RPC_PORT` | random / random / `32647` | Seed `b3coin.conf` on first run only |
| `B3_DOMAIN` | empty | Domain for HTTPS (`docker-compose.prod.yml`) |

Internal secrets are generated automatically and stored in the data folder — leave the secret lines in `.env.example` commented out.

## Remote access

- **Tailscale** is bundled: enable it in the UI's Settings for HTTPS from anywhere, no domain needed.
- **Your own domain:** set `B3_DOMAIN` in `.env` and run `docker compose -f docker-compose.prod.yml up -d` (automatic HTTPS via Caddy). Do the first-run setup once with the normal compose file, then switch.

## Troubleshooting

- **"Setup required" page** — you're not on the Docker host, or your Docker setup hides where connections come from. Open the UI on the machine running Docker.
- **"Storage not persistent" page** — the `/data` volume is missing. Add `-v ~/.B3-CoinV2:/data` (or fix the compose `volumes:` line).
- **Docker Desktop (Windows/Mac) or rootless Docker** — machines on your network can look like the host there. Publish the port to yourself only: change the compose port line to `"127.0.0.1:8080:8080"`.
- **Health** — `docker ps` should show `(healthy)`; allow up to ~10 minutes on first start.

More detail: [SECURITY.md](SECURITY.md).
