# B3 Hive — UX Design

Status: draft for review. This document defines the target users, use cases,
view states, and the UX fixes required before release. It is the reference for
the UX polish pass.

## 1. Who this is for (personas)

| Persona | Description | Primary needs |
|---|---|---|
| **Newcomer Nina** | Never ran a node, holds B3 on an exchange, wants a safe self-custody wallet | Simple bootstrap, create wallet, receive address, clear guidance |
| **Migrator Max** | Existing b3coin-qt user with wallet.dat and chain data | Import wallet without surprises, keep his chain, staking keeps working |
| **Everyday Ella** | Uses the wallet weekly | Balance, send with preview, receive, transaction history |
| **Staker Sam** | Runs the node to stake | Staking status, unlock window, alerts when the node stalls |
| **Operator Omar** | Maintains the container on a VPS | Health, peers, sync state, recovery, batch consolidation |

Design rule: **Nina must never see a dead end**; **Omar can always reach the raw data** (collapsible), but never by default.

## 2. Core use cases

### UC1 — First start (Nina)
1. Container starts, wizard opens in the browser.
2. **The node does NOT sync yet.** Step "Sync method": bootstrap (recommended, shows size + height + time estimate) or sync from scratch.
3. Step "Wallet": create wallet (passphrase + repeat) or skip with a clear warning ("no wallet — you cannot receive or send coins").
4. Step "Node config": optional, collapsed by default, sane defaults pre-filled.
5. Finish → dashboard, which now reflects reality (syncing at X%, wallet present or not).

### UC2 — Migration (Max)
1. First start with existing data dir → wizard detects existing chain data and skips the sync step ("Existing chain detected at height N").
2. Wallet step offers "load wallet.dat present in the data directory" with a file picker of detected `wallets/*.dat` (read-only listing via the backend; never uploads).
3. If he skips: the Wallet view always shows "Load wallet" later — migration is NEVER permanently hidden.

### UC3 — Everyday wallet (Ella)
Dashboard → Wallet view: balance, receive (address + QR), send wizard (preview → confirm), history. Wallet-dependent sections appear ONLY when a wallet is loaded; otherwise the Wallet view shows a single call-to-action card (create/load) with one paragraph of guidance.

### UC4 — Staking (Sam)
Staking view: status (active weight, expected time, last stake), unlock controls with remaining-time countdown, staking on/off. If staking is impossible (no wallet / locked), show why and the exact next step — never disabled controls without explanation.

### UC5 — Node operation (Omar)
Dashboard: chain health as human cards (sync %, peers, mempool, finality summary). Alerts bell. Settings: peers table, node config, tools. Raw JSON only behind collapsible "Details" sections for debugging.

### UC6 — Batch consolidation (Omar)
Batch view: recipes table, editor, preview → confirm. Unchanged from Phase 4 but must respect the empty-wallet state (hidden with guidance instead of dead forms).

## 3. View-state matrix (what renders when)

| State | Dashboard | Wallet view | Staking view | Batch view |
|---|---|---|---|---|
| No wallet loaded | Chain cards + "Create or load a wallet" CTA | Create/Load CTA card | Guidance card ("staking needs a wallet") | Guidance card |
| Wallet locked | Chain cards + wallet summary (balance fine) | Sections visible; send shows "unlock first" CTA | Unlock CTA + countdown | Preview works; execute shows "unlock first" |
| Wallet unlocked | Full | Full | Full | Full |
| Syncing (IBD) | Sync banner: "Syncing 3.7% — ~6 h left" on every view | Visible, with "balances may be incomplete while syncing" hint | Visible with hint | Visible with hint |
| Node stopped (setup pending) | "Waiting for setup" state | N/A — wizard active | N/A | N/A |

Empty-state pattern (applies everywhere): **Title — one-sentence explanation — one primary action button**. No dead controls, no raw JSON, no silent sections.

## 4. Defect list from the WSL walkthrough (must fix)

| # | Defect | Fix |
|---|---|---|
| D1 | Daemon starts syncing before the wizard's bootstrap choice | Entrypoint defers node start on a fresh chain (UI mode): backend-only until the wizard sends `start-node` (scratch sync) or the bootstrap worker finishes. Headless and upgrade (existing chain) start immediately as today |
| D2 | "Continue without bootstrap" permanently hides wallet create/migrate | Wallet create/load moves to the Wallet view as a persistent feature; wizard wallet step links to it. The wizard is never the only path |
| D3 | Sync Progress shows 100% at block 30k/820k | Stop using raw `verificationprogress` as the primary number. Compute sync = blocks / explorer tip (we already poll it in the monitor), fall back to headers. Show "30,000 / 825,000 (3.6%), ~N h behind" with a progress bar; keep verificationprogress only as a secondary detail |
| D4 | Raw JSON dumped under Finality | Render human cards: finalized height, current epoch, epoch progress, finality lag; raw JSON behind a collapsed "Details" |
| D5 | Wallet Lock / Recent Transactions / Coin Control shown with no wallet | Apply the view-state matrix: hide wallet-dependent sections, show empty-state CTA instead |
| D6 | Dev-tool feel overall | Apply the empty-state pattern + contextual hints everywhere; first-run guidance texts; consistent units (9dp B3 everywhere, thousands separators) |

## 5. Zero-config (from the earlier discussion, folded in)

- On first boot, the entrypoint auto-generates `SESSION_SECRET`, `TOTP_ENCRYPTION_KEY`, and a strong `UI_PASSWORD`, persists them in `/data/.secrets.env` (mode 0600), and prints the initial UI password in `docker logs` once. Env vars still override for operators.
- The wizard's node-config step stays optional and collapsed — defaults are sane.

## 6. Staking automation (Sam, extended requirements)

Verified against B3-CoinV2 v1.1.5 source (`src/wallet/rpc/staking.cpp`):
- `startstaking` takes **no parameters** — it starts the node's automatic staking loop
  with the wallet's validator key. The **amount staked is not a startstaking knob**:
  it is governed by how much is locked in STAKE outputs (`createstake amount`).
- `createstake <amount>` locks B3 as a STAKE output (min stake enforced by network;
  UNCONFIRMED → PENDING → ACTIVE after STAKE_ACTIVATION_DEPTH blocks).
- `getstakinginfo.stakes[]` lists every STAKE output with txid, vout, amount, status,
  owner address — everything a one-click unstake needs.
- Unstaking is an explicit spend of the outpoint (today: a painful debug-console
  `sendall` with `options.inputs` ritual — see user report).

### S1 — Auto-stake on restart
- New persisted setting (SQLite, per node): `autostake_enabled`, `autostake_target`
  (amount of B3 to keep staked), `autostake_reserve` (amount to keep liquid).
- On backend startup (and on wallet load/unlock when enabled): call `startstaking`,
  then reconcile — if ACTIVE+PENDING stake < target and spendable balance allows,
  `createstake (target - staked)` capped by balance minus reserve. Audited.
- The user can set how much of his coins to autostake; reserve keeps the rest spendable.

### S2 — Regular consolidation + restake
- Scheduled task (interval configurable, default daily): sweep plain-P2PKH UTXOs
  below a threshold into a fresh P2PKH output (reusing the batch engine's verified
  rails: P2PKH-only filter, carrier outputs never touched, Decimal 9dp, fee floor,
  testmempoolaccept, confirm-token binding) and optionally `createstake` the sweep
  result when autostake is below target. Off by default; enable + interval in Staking view.

### S3 — One-click unstake (replaces the sendall console ritual)

- Staking view lists each stake from `getstakinginfo.stakes[]` with status badges
  (UNCONFIRMED / PENDING / ACTIVE), amount, confirmations, owner address.
- Per stake: **Unstake** button -> two-phase flow like Send: preview (backend derives a
  fresh receiving address, builds and signs the spend of the stake outpoint, shows the
  fee and resulting amount, testmempoolaccept dry-run; wallet must be unlocked) ->
  **Confirm** -> broadcast via the wrapped sendall call. Audited.
- Safety rails identical to Send: CSRF, unlock gate, confirm-token binding, no
  broadcast from preview. Warning note about validator coordination is surfaced in
  the confirm step when the stake is ACTIVE.

### S4 — Staking view (human cards)

- Cards: staked sums by status (active / pending / unconfirmed), active weight vs
  total network weight, min stake, activation depth, next block phase, blocks produced.
- Settings: autostake on/off, target, reserve, consolidation interval.

### S5 — Auto-unlock for unattended operation (opt-in, scoped)

Problem: truly automatic staking after an unattended crash/reboot requires the
wallet to unlock itself, because `walletpassphrase` needs the passphrase. There is
**no native auto-unlock** in B3-CoinV2 (verified in source). This is a real security
trade-off and is opt-in only.

Verified security properties that make this acceptable:
- `startstaking` keeps a copy of the validator key until `stopstaking`, so
  **re-locking the wallet does not interrupt block production**. Block production
  needs only a one-time unlock, then the wallet can be relocked.
- `walletpassphrase` timeout is capped at ~3 years; timeout=0 means locked now.
  We never use long timeouts for auto-unlock — we unlock for the minimum time
  needed and call `walletlock` immediately after each operation.

Design:
- **Opt-in**: a dedicated Staking settings card "Allow unattended operation"
  with an explicit risk acknowledgement the user must check. Off by default.
- **Encrypted at rest**: the passphrase is stored in the SQLite DB encrypted
  with a vault key. The vault key source, in priority order:
  1. `WALLET_VAULT_KEY` env var (operator-managed, e.g. from a secrets sidecar
     or removable media mounted read-only — the secure option).
  2. A generated key persisted to `/data/.vault.key` (mode 0400, owner b3coin)
     on first enable — the convenient option. **Honest caveat documented in the
     UI: an attacker with read access to the persistent data directory can
     recover the passphrase. Only enable when /data is on trusted,
     access-controlled storage.**
- **Scoped authorization**: the stored passphrase is only ever used to
  `walletpassphrase` with a short timeout (default 30s) for an explicit
  authorized use case, then `walletlock` is called immediately:
  - startstaking at startup (then relock — production survives relock)
  - createstake top-ups during autostake reconcile (then relock)
  - consolidation sweep (then relock)
  It is **never** used for: sends, unstake, export/sign, or any interactive
  action. Those keep requiring an interactive unlock + CSRF + 2FA confirm.
- **Audit**: every auto-unlock event is logged with the use case and timestamp.
- **Revocation**: disabling "unattended operation" or changing the wallet
  passphrase wipes the stored passphrase; the user must re-enable.

## 7. FN coins / FlowMesh (currently absent)

FN coins are the FlowMesh asset system (verified RPCs: getassetstate,
listflowmeshmarkets, startflowmeshvalidator, stopflowmeshvalidator).

- New **Assets** view: asset list with balances and per-asset state (getassetstate),
  market listings (listflowmeshmarkets). Hidden when the node reports no asset activity.
- FlowMesh validator controls (start/stop) in Settings, wallet-unlock gated, with a
  clear warning about validator-set responsibilities (same caveat as unstaking).
- Same empty-state pattern: no FN assets for this wallet -> a single explainer card,
  not dead tables.

## 8. Versions, logs, and polish

- **Version display**: Settings About card + dashboard footer: UI/backend version
  (injected at build time from git tag), daemon version + subversion (getnetworkinfo),
  protocol version.
- **Log display**: Settings -> Logs card: last N lines of the container log
  (backend reads its own captured daemon log tail; never a browser path to the node).
  Collapsible raw view, refresh button, level filter. Omar-friendly, Nina-hidden by default.
- General polish pass applies D6 (empty states, hints, units, separators).

## 9. Verification plan

- Backend: tests for the new sync-progress computation, start-node command file,
  autostake reconcile logic (mocked RPC), unstake preview/confirm rails, version and
  log-tail endpoints.
- Browser: walkthrough of UC1 (fresh), UC2 (migration), no-wallet / locked-wallet
  states, staking view with stakes, Assets view empty state — at 375px and desktop,
  both themes; screenshots per use case.
- The sync banner must show a believable number during real IBD (test with a
  truncated bootstrap or mocked tip).
