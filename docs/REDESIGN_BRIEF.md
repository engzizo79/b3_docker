# B3 Hive Web Wallet — Full UI Redesign Brief

**Audience:** AI coding agent (Junie / JetBrains AI Agent). You are redesigning the B3 Hive web wallet SPA from scratch. The current UI is piecemeal and the owner is "totally unhappy." You have full latitude on visual design, information architecture, and interaction patterns. Keep the backend API and its 154-test suite intact.

**Stack:** FastAPI backend proxy + Alpine.js SPA. The browser NEVER talks to the node RPC directly — only through the backend proxy.

**Repo root:** `<repo root>`

---

## 0. Read this first — the tl;dr for Junie

This is a **redesign, not a maintenance task**. Discard the current visual design entirely; it is unsatisfactory by the owner's explicit statement. Design from first principles using the references in §4.

What you MUST preserve (functional, not visual):
1. Every backend capability is reachable from the UI (§2).
2. The first-run wizard is the ONLY thing on first run (§5.1).
3. The 423 point-of-action unlock intercept works across Send/Staking/Consolidation/Batch/SignMessage (§5.4).
4. CSRF double-submit + session-cookie auth (§7).
5. The 9-decimal Decimal math + P2PKH-only + carrier-output protections (§7).
6. The 154 backend tests stay green (§10).

Everything else — layout, colors, nav structure, component library, copy, interaction flows — is yours to invent.

---

## 1. Files you own (frontend only)

```
frontend/
  index.html      # ~1400 lines — markup + Alpine x-data components
  js/app.js       # ~1100 lines — all SPA logic, API calls, state
  css/theme.css   # ~620 lines — tokens + themes + layout
  js/alpine.min.js, js/qrcode.min.js, favicon.svg  # vendored, do not touch
```

You may restructure freely (split into modules/components). Clean, modular architecture is preferred over the current monoliths. Do NOT touch anything under `backend/` except to read it.

---

## 2. Backend capability surface — the UI must expose ALL of this

Read each router for exact request/response JSON shapes — don't guess. All endpoints are prefixed `/api/<router>`.

### 2.1 auth.py (prefix `/api/auth`)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | /login | `{password}` | `{totp_required: bool}` |
| POST | /2fa | `{code}` | session upgraded |
| GET | /status | — | `{authenticated, wallet_unlocked, totp_configured}` |
| POST | /totp/setup | — | `{provisioning_uri, recovery_codes[]}` |
| POST | /logout | — | `{ok}` |
| POST | /setup-password | `{password}` | sets the operator password (first run) |

### 2.2 setup.py (prefix `/api/setup`) — FIRST-RUN WIZARD
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /status | — | `{wizard_done, setup_required, conf:{editable,locked}, wallet:{loaded[],reachable}, bootstrap:{phase}}` |
| GET | /bootstraps | — | `{bootstraps[{height,size,sha256,url,created_utc}], fresh_chain}` |
| POST | /bootstrap/start | `{height,sha256,url,size,wipe_chain}` | `{ok, cmd_id}` |
| GET | /bootstrap/progress | — | `{phase: idle|downloading|verifying|extracting|wiping|done|failed, bytes, size, height, error}` |
| POST | /restart-node | — | `{ok}` |
| POST | /start-node | — | `{ok}` |
| POST | /complete | — | `{ok, wizard_done:true}` |
| GET | /wallet/queue | — | `{queue[{id,status,wallet,kind}]}` |
| POST | /wallet/queue/create | `{wallet_name, passphrase}` | `{ok, queued, id, wallet}` |
| POST | /wallet/queue/load | `{filename}` | `{ok, queued, id, wallet}` |
| POST | /wallet/queue/{id}/remove | — | `{ok}` |
| GET | /wallet/status | — | `{loaded[], reachable}` |
| POST | /wallet/create | `{wallet_name, passphrase}` | `{ok, wallet}` |
| POST | /wallet/load | `{filename}` | `{ok, wallet}` |
| GET | /conf | — | `{editable:{txindex,listen,maxconnections,proxy,bantime,...}, locked[]}` |
| POST | /conf/apply | `{conf: {...}}` | `{ok, applied[]}` |

### 2.3 chain.py (prefix `/api/chain`) — read-only
| Method | Path | Returns |
|---|---|---|
| GET | /summary | `{blockchain:{blocks,verificationprogress,...}, network:{connections,...}, mempool:{size,...}, sync:{behind, percent}, explorer}` |
| GET | /finality | `{finality: {...getfinalitystatus...}}` |
| GET | /bridge | `{bridge: {...getbridgeinfo...}}` |
| GET | /supply | `{supply: {...gettxoutsetinfo...}}` |
| GET | /staking | `{staking: {...getstakinginfo...}}` |
| GET | /peers | `{peers[{addr,subver,ping,inbound}]}` |

### 2.4 wallet.py (prefix `/api/wallet`)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | /unlock | `{passphrase}` | `{ok, unlocked_for_s}` (HTTP 423 if locked) |
| POST | /lock | — | `{ok}` |
| GET | /info | — | `{wallet, balances:{mine:{trusted, untrusted_pending, immature}}}` |
| GET | /addresses?label= | — | `{addresses: {...}}` |
| GET | /labels | — | `{labels: [...]}` |
| GET | /history?count=&skip= | — | `{transactions: [...]}` |
| POST | /send | `{recipients[{address,amount}], confirm:bool}` | preview=`{preview:true, txid, recipients, mempool_ok, rejection}` or send=`{preview:false, txid, mempool_ok}` |
| POST | /staking/start | — | `{ok, staking:true}` |
| POST | /staking/stop | — | `{ok, staking:false}` |

### 2.5 wallet_extra.py (prefix `/api/wallet`)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /manage | — | `{loaded[], on_disk[], persistent_data}` |
| POST | /manage/create | `{wallet_name, passphrase}` | `{ok, wallet}` |
| POST | /manage/load | `{filename}` | `{ok, wallet}` |
| POST | /manage/unload | `{filename}` | `{ok}` |
| POST | /manage/backup | — | `{ok, path}` |
| POST | /receive | `{label}` | `{address, label}` |
| POST | /label | `{address, label}` | `{ok}` |
| GET | /utxos?minconf= | — | `{utxos[{txid, vout, address, amount, confirmations, spendable, scriptPubKey}]}` |
| GET | /tx/{txid} | — | `{transaction}` |
| POST | /signmessage | `{address, message}` | `{signature}` |
| POST | /verifymessage | `{address, signature, message}` | `{valid: bool}` |
| POST | /passphrase-change | `{old_passphrase, new_passphrase}` | `{ok}` |
| GET | /book | — | `{addresses[{address, label, amount}]}` |

### 2.6 staking.py (prefix `/api/staking`)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /settings | — | `{autostake_enabled, autostake_target, autostake_reserve, vault_stored}` |
| POST | /settings | `{autostake_enabled, autostake_target, autostake_reserve, passphrase, acknowledge_risk}` | updated settings |
| POST | /vault/revoke | — | `{ok, vault_stored:false, autostake_enabled:false}` |
| POST | /reconcile | — | `{ran, topped_up, reason}` |
| POST | /unstake | `{txid, vout, confirm:bool, confirm_token?}` | preview=`{preview:true, stake, destination, fee, confirm_token, validator_warning}` or send=`{preview:false, txid, destination}` |
| GET | /consolidation/settings | — | consolidation settings |
| POST | /consolidation/settings | `{...}` | updated |
| POST | /consolidation/preview | — | `{eligible_utxos, total_fee, total_output, fee_rate, fee_source, skipped:{script, unspendable}, batches[], confirm_token}` |
| POST | /consolidation/execute | `{confirm_token}` | `{results[]}` |

### 2.7 assets.py (prefix `/api/assets`)
| Method | Path | Returns |
|---|---|---|
| GET | / | `{assets[{asset_id, kind, ticker, name, decimals, confirmed, unconfirmed, spendable, utxos[]}], fn:{...getassetstate.fn...}}` |
| GET | /markets | `{markets[{market_id, asset_id, state}]}` |
| POST | /validator/start | `{ok}` |
| POST | /validator/stop | `{ok}` |

### 2.8 batch.py (prefix `/api/batch`)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /recipes | — | `{recipes[{id, name, recipe:{...}}]}` |
| GET | /recipes/{id} | — | `{id, name, recipe}` |
| POST | /recipes | `{name, filters:{sources[],sort,min_utxo_value?}, action:{type,destination}, limits:{inputs_per_tx?,max_batches?,min_output?}, fees:{mode,fee_rate?,fee_target?}}` | `{ok, id}` |
| PUT | /recipes/{id} | same | `{ok}` |
| DELETE | /recipes/{id} | — | `{ok}` |
| POST | /recipes/{id}/preview | — | `{...preview, confirm_token}` |
| POST | /recipes/{id}/execute | `{confirm_token}` | `{ok, results[]}` |

### 2.9 alerts.py (prefix `/api/alerts`)
| Method | Path | Returns |
|---|---|---|
| GET | /?unacked=true | `{alerts[{id,level,message,ts}], count}` |
| POST | /{id}/ack | `{ok}` |
| POST | /ack-all | `{ok}` |
| GET | /status | alert status |

### 2.10 system.py (prefix `/api/system`)
| Method | Path | Returns |
|---|---|---|
| GET | /info | `{app_version, node_up, daemon:{subversion,...}}` |
| GET | /logs?lines= | `{lines[], note}` |

---

## 3. Dev server & login

```bash
cd backend && python dev_server.py 8777
# Mock RPC — no real node needed. Login: admin / b3hive dev x7
# B3DEV_SETUP_MODE=1 boots with NO operator account (passwordless first-run wizard) for E2E.
```

---

## 4. Design direction — what "good" looks like

Study these reference screenshots BEFORE designing:
- `<design reference screenshot>` — **b3coin-qt Overview** (the desktop wallet to BEAT: balance hero, status cards, recent activity, left nav). Functional but flat and unguided.
- `<design reference screenshot>` and `<design reference screenshot>` — **OmniRoute** (the look the user LOVES):
  - Icon rail sidebar with **collapsible grouped sections**, each group has a one-line subtitle. High information scent.
  - **Global search / quick navigation** (Ctrl+K / Cmd+K) in the top header. Type to jump to any view or action.
  - Clean white cards, generous whitespace, ONE coherent accent system.
  - Calm, intelligent, grouped — never a wall of buttons.

**The goal:** MORE intelligent and MORE user-friendly than b3coin-qt. Important info at a glance + guided actions. The user said: "Even I who asked you to create that wallet somehow feel lost." Never let a user feel lost.

---

## 5. Hard requirements (non-negotiable acceptance criteria)

1. **First-run wizard is the ONLY thing on first run.** No dashboard flash. The wizard gathers ALL choices across 4 steps and applies them ONLY on **Finish**. The daemon starts ONLY after the wizard completes.
   - Detect first-run via `GET /api/setup/status` → `setup_required: true` and/or `wizard_done: false`.
   - The 4 wizard steps (current order): **1. Security** (login password) → **2. Node** (config) → **3. Sync** (bootstrap vs scratch vs keep) → **4. Wallet** (create/load/queue).
   - On Finish, the exact sequence (port this, it's load-bearing): (0) if `setup_required`, POST `/api/auth/setup-password` then `/api/auth/login`; (1) if `conf.editable`, POST `/api/setup/conf/apply`; (2a) if bootstrap chosen, POST `/api/setup/bootstrap/start` then poll `/api/setup/bootstrap/progress`; (2b) if scratch/keep, POST `/api/setup/start-node`; (3) POST `/api/setup/complete`. Then show dashboard.
   - While setup_mode is active, gate every other view (the backend 403s non-localhost API calls in setup mode; the UI must also hide nav).
2. **Visible feedback after setup completes.** Not "nothingness then a log dive." Show a clear success state + progress bar as the node boots / bootstrap downloads, then transition to the dashboard. A sync/bootstrap banner must be visible post-finish (the existing `syncBannerVisible()` logic in app.js is a good reference).
3. **Wallet create / load / backup reachable ANYTIME** from the Wallet view — not only inside the wizard. Use `/api/wallet/manage/*`.
4. **Locked-wallet passphrase prompt at the point of action (QT-style).** The backend returns HTTP 423 when the wallet needs unlocking. Intercept 423 globally and show a passphrase modal inline, then retry the original action on submit. This MUST work across: **Send, Staking start, Consolidation execute, Batch execute, Sign Message**.
   - The existing `app.js` already has this: `promptUnlockRetry(path, opts)` (called by `api()` on 423) → sets `unlockPrompt = {show, pw, busy, err, retry:{resolve,reject,path,opts}}` → `confirmUnlockPrompt()` unlocks via `/api/wallet/unlock`, then retries the original request with `opts._retried = true` → `dismissUnlockPrompt()` cancels. Port this mechanism; verify it; make it robust. The retry must not infinite-loop (the `_retried` flag prevents it).
5. **Don't show "locked" when no wallet is loaded.** Distinguish "no wallet loaded" from "wallet locked." Use `GET /api/setup/wallet/status` (`reachable` + `loaded.length`) to determine loaded state, and `/api/auth/status` `wallet_unlocked` for lock state. The status strip/header badge must reflect three distinct states: no wallet / locked / unlocked.
6. **Tooltips must be readable.** Current ones are too transparent. Use opaque backgrounds with sufficient contrast in both themes.
7. **Coherent visual language.** ONE radius scale, ONE elevation scale, ONE type scale, ONE accent color (with semantic states). No mix of tall buttons, tall icons, rounded tags that feel wild. Consistent component grammar across every view.
8. **Mobile-first.** Works great at 375px portrait (thumb-zone bottom tab bar, collapsible chrome). Scales up to tablet landscape and desktop (sidebar nav, higher density).
9. **Security-first, internet-facing, live wallets.** See §7.

---

## 6. Proposed information architecture (starting point — improve as you see fit)

Grouped like OmniRoute (each group is a collapsible nav section with a subtitle):

- **Overview** — balance hero (confirmed / pending / immature, 9-decimal B3), network status strip, staking snapshot, recent activity, alerts bell.
- **Funds**: Send (guided, preview→confirm), Receive (address+QR+labels), Addresses (book, `/label`,`/book`), Activity (history, `/tx/{txid}` drawer).
- **Staking**: Stake (start/stop, settings, info), Consolidation (settings+preview+execute), Unstake / Reconcile / Vault revoke (advanced).
- **Assets**: colored assets + FN, balances, markets, validator start/stop.
- **Tools** (power panel): Batch (recipe CRUD+preview+execute), Sign/Verify Message, UTXO inspector.
- **System**: Node (chain summary, finality, bridge, supply, peers, restart/start), Logs, Alerts, Settings (staking settings, conf, passphrase change, 2FA, backup).

Global **Ctrl/Cmd+K command palette** to jump to any view or trigger any action.

**Layering:** Default to the safe guided path. Expose power-user features progressively ("Advanced" disclosure, expert toggles, the Tools group). Beginners aren't overwhelmed; advanced users aren't blocked.

**Empty states are guided:** every empty view explains what it is + the next step ("No wallet loaded — Create or Load a wallet to begin").

---

## 7. Hard security constraints (do NOT violate)

- Browser NEVER calls node RPC directly. Every action goes through `/api/*`.
- Every wallet-affecting action needs explicit confirmation + audit log (backend enforces; UI presents it clearly — show what will change, the fee, the consequence, before commit). **Ban `window.confirm()` for wallet-affecting actions** — use a modal Confirmation component.
- CSRF: double-submit cookie. Read `b3_csrf` cookie and mirror it into `x-csrf-token` header on every mutating request. (The existing `api()` helper in app.js does this — preserve it.)
- Session: HTTP-only cookie; `GET /api/auth/status` returns `{authenticated, wallet_unlocked, totp_configured}`.
- 2FA: remote (non-localhost) access requires TOTP after login; localhost skips 2FA (`LOCALHOST_SKIP_2FA=true` default). The login flow is: POST /login → if `totp_required`, POST /2fa.
- Secrets only via env/OS keyring. Never commit `.env`, `b3coin.conf` with rpcpassword, `wallet.dat`, passphrases.
- B3 amounts: 9 decimals, 1 B3 = 1e9 base units. Parse/format with Decimal, never float.
- Legacy P2PKH only (S-prefix, version byte 0x3F). Witness/bech32 rejected.
- **Carrier outputs must NEVER be spent by batch tools:** B3S1 stake carriers, B3A1 colored-asset envelopes, B3MC metadata cells. Plain P2PKH (script `76a914…88ac`) only. Consolidation/batch must filter these out.
- Errors: plain language, no RPC method names, no stack traces, no leaked `detail` strings.
- Blocked RPCs (backend never proxies): importprivkey, importmulti, dumpprivkey, dumpwallet, encryptwallet, signmessagewithprivkey, setgenerate, all mining RPCs.

---

## 8. Design language — build on the existing token system

The current `theme.css` already has a token layer (`--space-*`, `--text-*`, `--radius-*`, `--shadow-*`, `--duration-*`, `--z-*`, `--min-target`, `--focus-ring`). Reuse and extend it; don't reinvent. The new design should keep these tokens but apply a fresh, coherent visual language on top.

### 8.1 Existing tokens (dark default, light override via `[data-theme="light"]`)
- Colors: `--bg, --bg-surface, --bg-card, --bg-input, --border, --border-focus, --text, --text-muted, --text-dim, --accent, --accent-text, --accent-hover, --accent-muted, --accent-bg, --success(+bg/border), --warning(+bg/border), --danger(+bg/border)`
- Spacing: `--space-1` (4px) … `--space-6` (32px)
- Type: `--text-xs` (12) / `--text-sm` (14) / `--text-base` (16) / `--text-md` (17.6) / `--text-lg` (20) / `--text-xl` (24) / `--text-2xl` (32, hero)
- Radius: `--radius-sm` (8) / `--radius-md` (12) / `--radius-lg` (16) / `--radius-pill` (999)
- Shadow: `--shadow-sm` / `--shadow` / `--shadow-lg`
- Motion: `--duration-fast/med/slow`, `--ease-out`
- Z: `--z-50/100/200/300`
- Touch: `--min-target` (44) / `--min-target-sm` (36)
- Focus: `--focus-ring`
- State attributes on `<html>`: `data-theme` (dark|light), `data-mode` (simple|advanced), `data-wallet` (loaded|none), `data-sync` (synced|ibd|stopped)

### 8.2 Contrast failures you MUST fix (from the independent design review)
- Light `--accent` (#c4a030) on `--bg` = 2.25:1 — FAIL. Use `--accent-text` (#6f5a0e) for accent-colored text on light backgrounds.
- Light `.btn-secondary` text = 2.27:1 — FAIL. Use `--accent-text`.
- `--text-dim` on bg = 2.86:1 (light) / 3.13:1 (dark) — FAIL. Don't use `--text-dim` for placeholder text; use `--text-muted` + opacity.
- All text tokens must hit ≥4.5:1 on their background (WCAG AA), ≥3:1 for large text.

### 8.3 Component canon (target ~19 components)
The review identified these as needed; implement them as one canonical version each in `theme.css`:
- **Existing:** Card, Button (primary/secondary/danger/success/ghost/sm/block), Input, Badge (success/warning/danger/info — **info must be implemented**), Stat (+stat-grid, stat-value-lg), Empty State, Banner, Toast, Toggle, Progress, Tabs.
- **Add:** Modal (sm/md/lg, focus-trap, ESC, backdrop-click, focus restore — replaces `window.confirm`), Disclosure (styled `<details>` for raw JSON), CodeBlock (mono, overflow-x), CopyField (value+copy button+toast), Address (truncate first 8…last 6, never the S prefix, click-to-copy), AmountInput (B3 suffix, inputmode decimal, max-9-decimal validation, optional max-available), Callout (info/success/danger review panel), Confirmation (modal: amount+destination+"cannot be undone"+CSRF+confirm-token), Dropdown (anchored, click-away, focus management — for the alerts bell).

### 8.4 Mode matrix (Simple vs Advanced — single global toggle)
- Default: **Simple** on first run; persist per-node.
- Simple shows: balances, send, receive, stake on/off, unstake (guided), password. Hides jargon and footguns.
- Advanced adds: coin control/UTXOs, custom fees, batch, validator ops, autostake target/reserve, raw RPC JSON (behind per-card Disclosure), node logs.
- **Raw data is behind per-card Disclosure, reachable in BOTH modes** (operators in Simple can still reach raw details via a caret — no mode switch needed). This resolves the old UX_DESIGN vs DESIGN_LANGUAGE contradiction.
- Move `autostake target + reserve` to Advanced only (silently locks funds).
- One header pill toggles mode. No per-page "Switch to Advanced" prompts.

### 8.5 A11y baseline
- `:focus-visible` ring on all interactive components.
- ARIA: Tabs (tablist/aria-selected), Modal (role=dialog/aria-modal/focus-trap/ESC), Dropdown (aria-expanded/aria-haspopup), Toast (role=status/aria-live), Toggle (role=switch/aria-checked).
- `prefers-reduced-motion` fallback for all animations.
- Touch targets: `min-height: var(--min-target)` on `.btn`, `.nav-tab`, etc.
- Status never color-only — pair with icon/word (color-blind safety).
- WCAG 2.1 AA throughout.

### 8.6 Voice rules
1. Irreversible-action confirm restates amount + destination + "this cannot be undone".
2. Errors: plain language, no RPC leakage.
3. Amounts: Decimal, 9dp, thousands separators, " B3" suffix.
4. Addresses truncate by character (first 8…last 6), not CSS width.
5. Raw RPC JSON never primary content — behind Disclosure.
6. "Staking" as nav noun, "Earn rewards" as action verb.
7. Risk trade-offs stated where the toggle is, not buried in docs.

---

## 9. Functional context the UI must reflect (from the design docs — functional facts, not design constraints)

### 9.1 Personas
- **Newcomer Nina** — never ran a node; needs simple bootstrap, create wallet, receive, clear guidance. **Must never see a dead end.**
- **Migrator Max** — existing wallet.dat + chain data; wizard detects existing chain (skips sync step), offers load wallet. Migration is NEVER permanently hidden — the Wallet view always shows Load later.
- **Everyday Ella** — balance, send with preview, receive, history.
- **Staker Sam** — staking status, unlock window + countdown, on/off. If staking impossible (no wallet/locked), show why + the exact next step.
- **Operator Omar** — health, peers, sync, recovery, batch. Can always reach raw data (collapsible), never by default.

### 9.2 View-state matrix (what renders when)
| State | Overview | Wallet | Staking | Batch |
|---|---|---|---|---|
| No wallet loaded | Chain cards + "Create/load wallet" CTA | Create/Load CTA card | Guidance card | Guidance card |
| Wallet locked | Chain cards + wallet summary (balance fine) | Sections visible; send shows "unlock first" CTA | Unlock CTA + countdown | Preview works; execute shows "unlock first" |
| Wallet unlocked | Full | Full | Full | Full |
| Syncing (IBD) | Sync banner on every view | Visible + "balances may be incomplete" hint | Visible + hint | Visible + hint |
| Node stopped (setup pending) | "Waiting for setup" | N/A — wizard active | N/A | N/A |

Empty-state pattern (everywhere): **Title — one-sentence explanation — one primary action button.** No dead controls, no raw JSON, no silent sections.

### 9.3 Staking automation (the UI must surface all of this)
- **S1 Auto-stake on restart**: persisted settings `autostake_enabled`, `autostake_target`, `autostake_reserve`. Reconcile runs at startup. UI in Staking settings (Advanced).
- **S2 Regular consolidation + restake**: scheduled sweep of plain-P2PKH UTXOs below a threshold into a fresh P2PKH output, optionally restake. Off by default; enable + interval in Staking view.
- **S3 One-click unstake**: list stakes from `getstakinginfo.stakes[]` with status badges (UNCONFIRMED/PENDING/ACTIVE), amount, confirmations, owner. Per stake: Unstake → two-phase (preview→confirm) like Send. Warning about validator coordination when ACTIVE.
- **S4 Staking view (human cards)**: staked sums by status, active weight vs total network weight, min stake, activation depth, next block phase, blocks produced.
- **S5 Auto-unlock for unattended operation (opt-in)**: "Allow unattended operation" with explicit risk acknowledgement. Encrypted passphrase vault. Scoped authorization (startstaking, createstake top-ups, consolidation only — never sends/unstake/export/sign). Revocation wipes the stored passphrase.

### 9.4 FN coins / FlowMesh (Assets view)
- FN coins = FlowMesh asset system. RPCs: getassetstate, listflowmeshmarkets, startflowmeshvalidator, stopflowmeshvalidator.
- Assets view: asset list with balances + per-asset state, market listings. Hidden when no asset activity (empty-state explainer card, not dead tables).
- FlowMesh validator controls (start/stop), wallet-unlock gated, with validator-responsibility warning.

### 9.5 Sync progress (fix the old bug)
- Don't use raw `verificationprogress` as the primary number (shows 100% at 30k/820k). Compute sync = blocks / explorer tip, fall back to headers. Show "30,000 / 825,000 (3.6%), ~N h behind" with a progress bar; keep verificationprogress only as a secondary detail.

---

## 10. Verification protocol (MANDATORY — do not skip)

1. **Backend tests stay green:** `cd backend && /opt/venv/bin/python -m pytest tests/ -q` (154 tests). Do not break them.
2. **Real-browser verification with CDP/Chromium at 375px mobile AND desktop widths, in BOTH light and dark themes.** DOM-only checks miss SSR-blind bugs. Use a CDP script (PyCDP / pyppeteer / Playwright) — NOT just `curl`.
   - The Alpine SPA needs real interaction (focus + input events, not just `.value=`) to trigger `x-model` updates. If `Input.insertText` doesn't update Alpine state, dispatch proper `input`/`change` events or use keyboard typing via CDP.
   - Screenshot every major view at 375px and desktop, both themes, into `docs/screenshots/`.
3. **Specific flows to verify end-to-end:**
   - First run: `B3DEV_SETUP_MODE=1` → wizard is the ONLY thing shown (no dashboard flash) → gather all choices → apply on Finish → daemon-start feedback → dashboard.
   - After setup: visible success feedback, then dashboard.
   - 423 passphrase modal: trigger on Send, Staking start, Consolidation execute, Batch execute, Sign Message — modal appears inline, submit retries the original action.
   - Wallet create/load/backup from the Wallet view while a node is running (not just in wizard).
   - "No wallet loaded" state does NOT show "locked."
   - Tooltips readable in both themes.
   - Ctrl+K command palette opens and navigates.
4. **Report back with:** files changed (with paths), screenshots (absolute paths), and verification results (tests pass, flows verified, any intentionally hidden capability documented).

---

## 11. Existing design docs (functional facts only — NOT design constraints)

The owner considers the current UI unsatisfactory. These docs describe the OLD design — mine them for functional facts, but do NOT treat them as a quality bar or layout to preserve:
- `docs/DESIGN_LANGUAGE.md` (token system, 19-component canon, mode matrix, a11y — the token/component facts in §8 are extracted from here)
- `docs/UX_DESIGN.md` (personas, use cases, defect list D1-D6, staking S1-S5, FN/FlowMesh — extracted in §9)
- `docs/DESIGN_REVIEW.md` (14 gaps, contrast failures, missing components — extracted in §8.2/8.3)
- `docs/SECURITY.md` (11 security layers, auth flow, blocked RPCs — extracted in §7)
- `docs/ARCHITECTURE.md` (single container, data flow)

You are free — and expected — to discard the old UI entirely and design from first principles.

---

## 12. Reference patterns from a sibling project

- `b3-utxo-combiner` in `<b3_monitoring>/tools/b3-utxo-combiner` has verified patterns: P2PKH-only UTXO filter, Decimal 9dp math, unlock-state restore, `testmempoolaccept` dry-run, fee floor at `minrelaytxfee`. Mirror these for consolidation/batch.
- B3Hive node facts (verified against B3-CoinV2 v1.1.5): 9-decimal amounts, legacy P2PKH (0x3F, S prefix), seal at 810,000, modern PoS from 811,001, ~1-min blocks, BLS finality with epochs. Carrier outputs (B3S1/B3A1/B3MC) must never be spent by batch tools.

---

**Summary of success:** A coherent, guided, mobile-first web wallet that surfaces every backend capability, beats b3coin-qt on intelligence and friendliness, never lets the user feel lost, and passes every verification in §10 — with the 154 backend tests still green.
