# Security Model

## Scope: full b3coin-qt alternative

This is **not** a read-only monitoring dashboard. The web UI is a fully functional replacement for the Qt desktop wallet — send, receive, stake, consolidate, wallet management — accessible from anywhere, including mobile phones. Because the stack is internet-facing and controls live wallets, security is the top priority.

## Threat surface

The primary assets at risk are: wallet funds (via unauthorized spends), wallet keys (via key-exporting RPCs), and node availability (via RPC abuse or crash loops). The UI may be accessed from untrusted networks (mobile, public Wi-Fi, shared devices).

## Security layers (defense in depth)

| # | Layer | Purpose | Enforced |
|---|---|---|---|
| 1 | TLS at the edge | nginx/Caddy terminates HTTPS with HSTS; no plaintext | Internet-facing |
| 2 | App login | Username + strong password; session cookie (HTTP-only, Secure, SameSite=Strict); rate-limited | Always |
| 3 | 2FA for remote access | TOTP (e.g. Google Authenticator) required unless the client is local (see below) | Remote only (bypassed for local clients) |
| 4 | Wallet passphrase for signing | Every wallet-affecting action (unlock, send, consolidate, stake) requires the wallet passphrase — stored only in the session, never persisted on disk | Always |
| 5 | CSRF protection | Double-submit cookie or synchronizer-token for all mutating POST/PUT/DELETE | Always |
| 6 | Rate limiting | Login, unlock, and 2FA endpoints are rate-limited (e.g. 5 attempts / minute) | Always |
| 7 | RPC allowlist | The backend proxy only forwards a curated set of RPCs; everything else returns 403 | Always |
| 8 | Carrier-output protection | Every UTXO-selection path filters to plain P2PKH only | Batch actions |
| 9 | Dry-run by default | testmempoolaccept preview before broadcast; explicit user confirmation required | Batch actions |
| 10 | No secrets in git | Env files, b3coin.conf, wallet.dat, passphrases are gitignored | Always |
| 11 | Audit log | Every wallet-affecting action logged: who / what / when / txid | Always |

## Localhost vs. remote auth (layer 3)

The UI is designed for both local administration (same machine) and remote access (internet / LAN). The 2FA layer adapts:

- **Local** clients: 2FA is **skipped** and first-run setup is allowed. App login + wallet passphrase are still required once a password is set.
- **Remote** (every other source IP): TOTP 2FA is **required** after login. Without a valid TOTP code, the user cannot access the dashboard or any wallet features. This protects against compromised passwords from phishing or credential stuffing.
- The bypass is opt-in and configurable: set `LOCALHOST_SKIP_2FA=true` (default) to enable, `false` to always require 2FA.

### What counts as "local"

1. **Loopback** — `127.0.0.1` / `::1` inside the container.
2. **The Docker host** — connections from the machine running Docker to a published port arrive from the container's default gateway (e.g. `172.17.0.1`). That address is detected automatically, so `http://localhost:8080` on the host needs no setup: the first-run wizard opens and 2FA is skipped. Remote machines keep their own source IP through Docker's NAT and stay remote.
3. **`B3_LOCAL_ADDRS`** — extra addresses you list explicitly (comma-separated IPs or CIDRs). Entries broader than `/16` (IPv4) or `/64` (IPv6), and invalid entries, are ignored with a log warning (fail closed). Every client behind a listed address becomes local, so never list a reverse proxy (Caddy, nginx) or shared NAT.

`X-Forwarded-For` is honored only from a loopback peer or an entry of `B3_TRUSTED_PROXIES`; `B3_LOCAL_ADDRS` does not grant that trust. The developer console's trusted networks default to loopback (the Docker host already counts as local); change them under Settings → Console.

**Caveat — set `B3_TRUST_DOCKER_HOST=false` if traffic is masqueraded.** Rule 2 assumes Docker preserves remote source IPs. On Docker Desktop (Windows/Mac), rootless Docker, or hosts using the userland proxy for LAN traffic, LAN clients can appear to come from the gateway and would wrongly count as local (2FA bypass, first-run takeover). On such setups either publish the port to loopback only (`"127.0.0.1:8080:8080"`) or set `B3_TRUST_DOCKER_HOST=false` (then list your host's address in `B3_LOCAL_ADDRS` for the first-run wizard, and remove it afterwards). Verify once: open the UI from another machine on your LAN — it must ask for the password *and* 2FA, and before setup it must show "setup required".

**Production behind Caddy:** `docker-compose.prod.yml` forces `B3_LOCAL_ADDRS` empty (Caddy's address is not the Docker host). Do the first-run setup once with `docker-compose.yml`, then switch files (same data volume).

## Auth flow

```
Browser → POST /api/auth/login (username + password)
         ↓ rate-limited
         ← session cookie
         ↓ if remote AND LOCALHOST_SKIP_2FA != false:
           → POST /api/auth/2fa (TOTP code)
           ← session upgraded (2fa_verified=true)
         ↓
Dashboard loads (read-only chain state, balances)
         ↓
User clicks "Send" / "Consolidate" / "Unlock wallet"
         ↓
         → POST /api/wallet/unlock (wallet passphrase)
         ← wallet unlocked for N seconds (configurable timeout)
         ↓
Action executed, signed, broadcast (or dry-run preview → confirm → broadcast)
         ↓
Wallet re-locks after timeout or explicit lock
```

## Explicitly blocked RPCs (never proxied)

importprivkey, importmulti, dumpprivkey, dumpwallet, encryptwallet, signmessagewithprivkey, setgenerate, and all mining RPCs. These can exfiltrate keys or destabilize the node.

## Secret storage

| Secret | Storage |
|---|---|
| Node RPC credentials | Docker secret or env-generated `b3coin.conf` (mode 0600), never committed |
| Wallet passphrase | Held in encrypted session only (never persisted on disk); user re-enters for each unlock |
| API session secret | Generated at deploy time, stored in env |
| TOTP secret | Per-user, stored encrypted in the backend DB (SQLite, mode 0600) |
| App login password | Argon2id hash stored in the backend DB |

## Transport & browser hardening (v0.1.0-alpha)

| Layer | Detail |
|---|---|
| TLS | Caddy sidecar (`docker-compose.prod.yml`): automatic Let's Encrypt for `B3_DOMAIN`, HTTP/3, HSTS `max-age=31536000` |
| Cookie `Secure` flag | `auto` (default): set when the request is HTTPS, directly or via `X-Forwarded-Proto` from the trusted reverse proxy; `COOKIE_SECURE=true/false` overrides |
| CSP | `default-src 'self'` — fully self-hosted SPA, no external origins; `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'` |
| Other headers | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on all responses |
| Port exposure | Only Caddy's 80/443 are published in production; the backend port and node RPC are never published |
| Image hygiene | `.dockerignore` keeps tests/dev data/secrets out of the image; backend runs as non-root `b3coin` |
| Secrets scan | `scripts/scan_secrets.sh` pre-commit hook blocks commits with secret-like values or wallet data |

## TOTP setup

On first login (after setting a password), the user is prompted to scan a QR code with their authenticator app. The TOTP secret is generated server-side, stored encrypted, and the QR code is shown **once**. Recovery codes are generated and should be stored offline.
