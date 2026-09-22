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
docker compose pull && docker compose up -d      # upgrade to the newest release
```

Your chain and wallet live in the host folder `~/.B3-CoinV2`. **Deleting that folder deletes the wallet.**

Other devices (phone, laptop) can reach the UI too, but always need the password **and** 2FA (authenticator app; enable it in Settings).

## Advanced: plain `docker run`

```bash
docker run -d --name b3hive --restart unless-stopped \
  -p 8080:8080 \
  -v ~/.B3-CoinV2:/data \
  --stop-timeout 600 \
  ghcr.io/engzizo79/b3hive:latest
```

- The volume **must** map to `/data`. Without a persistent volume the app refuses to start (the chain and wallet would be lost with the container).
- Add settings with `-e NAME=value` or `--env-file .env`.
- The node's RPC port is never published; the UI is the only way in.

## Settings (`.env` or `-e`) — all optional

| Setting | Default | What it does |
|---|---|---|
| `WEB_PORT` | `8080` | Host port for the UI |
| `B3_P2P_PORT` | `5647` | Host port for node-to-node (P2P) connections — published by default so peers can reach you inbound; see [Port forwarding](#port-forwarding-inbound-p2p) |
| `B3_DATA_DIR` | `~/.B3-CoinV2` | Host folder for chain + wallet (compose only) |
| `B3_IMAGE_TAG` | `latest` | Version to run (compose only). Pin e.g. `v0.8.13-beta` to stay on one release |
| `RUN_UI` | `true` | `false` = node only, no web UI. On a fresh volume the daemon is installed automatically (see `B3_DAEMON_VERSION`) and syncs from scratch |
| `B3_DAEMON_VERSION` | latest stable | Headless only: daemon release to install on first run, e.g. `v1.1.4` |
| `B3_DAEMON_MODE` | `managed` | `external` = UI only, talk to a node running elsewhere; also set `EXT_RPC_HOST`, `EXT_RPC_PORT`, `EXT_RPC_USER`, `EXT_RPC_PASSWORD` (or choose it in the wizard) |
| `UI_PASSWORD` | unset | Preset the login password (normally use the wizard) |
| `LOCALHOST_SKIP_2FA` | `true` | `false` = require 2FA even from the Docker host |
| `B3_LOCAL_ADDRS` | empty | Extra addresses treated as local (skip 2FA). IPs/CIDRs, nothing broader than /16. Never list a reverse proxy. |
| `B3_TRUST_DOCKER_HOST` | `true` | `false` = don't treat the Docker host as local (see Troubleshooting) |
| `WALLET_UNLOCK_TIMEOUT` | `60` | Seconds the wallet stays unlocked after entering the passphrase |
| `REQUIRE_SECURE_TRANSPORT` | `warn` | `block` = refuse remote logins over plain HTTP |
| `COOKIE_SECURE` | `auto` | Cookie Secure flag: `auto`, `always`, `never` |
| `STALL_ALERT_MINUTES` | `10` | Minutes without a new block before an alert |
| `STALL_LEVEL` | `alert` | Response to a stall: `alert`, `restart`, `reindex` |
| `WEBHOOK_URL` | empty | Discord/Slack-style URL for alerts and wallet-event notifications |
| `NOTIFY_INTERVAL` | `30` | Seconds between wallet-event polls and digest flushes |
| `VAPID_SUBJECT` | `mailto:noreply@b3hive.local` | Contact URI in the Web Push VAPID claim (not a secret) |
| `EXPLORER_URL` | explorer.b3hive.io | Used to compare your sync height; empty disables |
| `RPC_USER` / `RPC_PASSWORD` / `RPC_PORT` | random / random / `32647` | Seed `b3coin.conf` on first run only |
| `B3_DOMAIN` | empty | Domain for HTTPS (`docker-compose.prod.yml`) |

Internal secrets are generated automatically and stored in the data folder — leave the secret lines in `.env.example` commented out.

## Remote access

- **Tailscale** is bundled: enable it in the UI's Settings for HTTPS from anywhere, no domain needed.
- **Your own domain:** set `B3_DOMAIN` in `.env` and run `docker compose -f docker-compose.prod.yml up -d` (automatic HTTPS via Caddy). Do the first-run setup once with the normal compose file, then switch.

## Port forwarding (inbound P2P)

Every B3Hive node connects OUT to peers on its own; that always works.
Accepting connections IN from other peers is a separate thing — it's what
makes you a well-connected node, and it's what a FlowMesh validator wants
(better peer diversity, faster propagation, being reachable for other
validators). It needs three things to line up:

1. **`b3coin.conf` has `listen=1` and a `port=`** — already the default
   (`port=5647`, the chain default), editable later from Settings → Node.
2. **Docker publishes that same port** — `docker-compose.yml` does this by
   default (`B3_P2P_PORT`, default `5647`). If you change one, change the
   other and recreate the container (`docker compose up -d`) — they have
   to match, or the inbound path is broken silently.
3. **Your router forwards that port to this machine.**

Running more than one daemon (including other coins) on one router that
only forwards a limited port range — for example port-forwarding rules
that only cover 5462–5479 — is the common case this trips up: each daemon
needs its own port *from that allowed range*, not its chain default.
Pick a free port in your allowed range, set it in **both** places:

```bash
# .env
B3_P2P_PORT=5470          # pick one from your router's allowed range
```

On a **fresh** node this is enough — the entrypoint seeds `port=5470` into
`b3coin.conf` on first run. On an **existing** node, `B3_P2P_PORT` alone
does nothing (the conf is never overwritten after first run): set
`port=5470` from **Settings → Node** in the UI instead, and use "Restart
node" to apply it. Either way, `docker compose up -d` afterwards to
republish the matching Docker port — the two have to agree.

Verify it worked: `getnetworkinfo` (Settings → Node, or the console) shows
your reachable address under `localaddresses` once a peer has connected
back to you, and `getpeerinfo` entries show `"inbound": true` for peers
that connected to you rather than the other way round. An online port
checker (from outside your network) against the router's WAN IP and your
chosen port is the fastest way to confirm the forward itself is working,
independent of the daemon.

If you don't need inbound reachability, do nothing — the node works fine
outbound-only, exactly as before this port was published by default.

## Notifications

Settings → Notifications controls what reaches you and how often, per event
type (coins received, a send confirming, a staking reward, and the chain
alerts above). Two channels, either or both per type:

- **Push** — a real browser/OS notification, works even with the tab
  closed. Click "Enable" on the device you want it on (needs HTTPS, or
  `localhost`); installing the app to your home screen makes delivery more
  reliable, especially on iOS.
- **Webhook** — relayed to `WEBHOOK_URL` alongside the chain alerts above.

Each type also has a **digest window** (Instant / 15 min / hourly / every 6
hours / daily): the first event of a burst fires right away, and anything
else of the same type inside the window folds into one summary instead of
one push per event — so, say, staking many blocks in an hour doesn't turn
into many notifications.

## Troubleshooting

- **"Setup required" page** — you're not on the Docker host, or your Docker setup hides where connections come from. Open the UI on the machine running Docker.
- **"Storage not persistent" page** — the `/data` volume is missing. Add `-v ~/.B3-CoinV2:/data` (or fix the compose `volumes:` line).
- **Docker Desktop (Windows/Mac) or rootless Docker** — machines on your network can look like the host there. Publish the port to yourself only: change the compose port line to `"127.0.0.1:8080:8080"`.
- **Health** — `docker ps` should show `(healthy)`; allow up to ~10 minutes on first start.

More detail: [SECURITY.md](SECURITY.md).
