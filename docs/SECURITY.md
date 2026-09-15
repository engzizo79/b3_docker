# Security Model

## Threat surface

The stack is internet-facing and may serve wallets holding coins. The primary assets at risk are: wallet funds (via unauthorized spends), wallet keys (via key-exporting RPCs), and node availability (via RPC abuse or crash loops).

## Layers

1. **TLS at the edge** — nginx/Caddy terminates HTTPS with HSTS; no plaintext.
2. **Backend auth** — session-based (signed cookie or JWT) with CSRF protection for browser mutations; rate limiting on login and unlock.
3. **Second-factor on wallet actions** — unlock/send/consolidate require either passphrase re-entry or a TOTP step (configurable).
4. **RPC allowlist** — the backend proxy only forwards a curated set of RPCs; everything else returns 403. See PLAN.md for the allowlist table.
5. **Carrier-output protection** — every UTXO-selection path filters to plain P2PKH only (script 76a914..88ac). B3S1/B3A1/B3MC carriers are never selected. This is the verified pattern from the b3-utxo-combiner tool.
6. **Dry-run by default** — every batch action signs and validates (testmempoolaccept) but never broadcasts without explicit confirmation.
7. **No secrets in git** — env files, b3coin.conf, wallet.dat, passphrases are gitignored and mounted at runtime.
8. **Audit log** — every wallet-affecting action is logged with who/what/when/txid.

## Explicitly blocked RPCs

importprivkey, importmulti, dumpprivkey, dumpwallet, encryptwallet, signmessagewithprivkey, setgenerate, and all mining RPCs. These can exfiltrate keys or destabilize the node.

## Secret storage

- Node RPC credentials: docker secret or env-generated b3coin.conf (mode 0600), never committed.
- Wallet passphrase: OS keyring on the backend host (preferred) or config.ini (mode 0600).
- API session secret: generated at deploy time.
