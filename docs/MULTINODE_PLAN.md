# Multi-node fleet console — implementation plan

> Status: planned, not started. Written to be executed by someone (or some
> model) with **no prior context** — every seam, pattern and gotcha needed is
> named here with a file path. Read this file, `AGENTS.md` and
> `docs/ARCHITECTURE.md` before touching code.

## Why

FlowMesh has to be tested with **several validators running at once**, all on
the same unpublished daemon build, and those validator wallets hold **real FN
Coins** (throwaway data dirs are not an option). Today the UI manages exactly
one node, so testing that means N browser tabs, N logins, and no way to watch
finality quorum form *across* validators — which is the thing that actually
needs watching.

The feature this calls for is a **multi-node control plane**, not a "dev
mode". Running an unpublished build is one attribute of a node entry, not a
mode the application runs in. Built as a dev-only mode it would be
second-class and would rot; built as "the UI manages N nodes" it is also
useful in production (a validator plus a spare, or a watch-only node), and
the question of forking the project into a separate dev variant disappears.

## Deployment shape (no new concepts needed)

- Each validator is a **daemon-only container**: the existing `RUN_UI=false`
  mode, same image, its own data dir and its own `WEB_PORT` / `RPC_PORT` /
  `B3_P2P_PORT` triple (see `docs/GUIDE.md` → Port forwarding).
- One container runs with `RUN_UI=true` and acts as the **control plane**,
  holding connections to every validator.
- Nothing about the image, entrypoint or supervision model changes. The
  control plane reaches other nodes over RPC exactly as `external` daemon
  mode already does.

## What already exists (build on these, do not reinvent)

| Seam | Where | Note |
|---|---|---|
| Single RPC client on app state | `backend/app/deps.py:21`, wired at `:173-186` | The one attribute that becomes a registry |
| External-node connection | `deps.py:180` (`EXT_RPC_*`) | Already "talk to a node we don't supervise" — just singular |
| RPC allowlist choke point | `backend/app/rpc.py` (`ALLOWED`, `assert_allowed`) | Lives *inside* `B3RPCClient.call`, so it applies per node for free |
| Encrypted-at-rest secrets | `backend/app/vault.py` (Fernet, `<data>/.vault.key`) | Reuse verbatim for per-node RPC credentials |
| Single-row + multi-row settings tables | `backend/app/db.py` (`console_settings`, `notification_prefs`) | Copy the style for the `nodes` table |
| Background task pattern | `backend/app/monitor.py`, `wallet_monitor.py`, `autostake.py` | Module-level singleton + `start_*`/`stop_*` in `main.py` lifespan |
| Expert RPC console | `backend/app/routers/console.py` (`console_run` at `:234`) | Gets a node selector in Phase 1 — cheapest real win |
| Frontend HTTP choke point | `frontend/js/core/api.js` (`api()`) | Injects `x-csrf-token` on every call; injects the node header the same way |
| View registry / routing | `frontend/js/core/router.js` (`VIEWS`, `onEnterView`) | Add the fleet view here |

There are **112 `state.rpc` / `self.rpc` call sites across 14 files**, but they
are all the same shape (`state.rpc.call(...)`). The migration is mechanical
breadth, not depth.

## Target architecture

```
AppState.rpc                 ->  AppState.rpc_for(request) -> B3RPCClient
  (one client, one node)          (registry lookup + per-node client cache)
```

- Node selection travels as an **`X-B3-Node: <node_id>` request header**.
  Chosen over a query param because `frontend/js/core/api.js` is a single
  choke point: adding the header there scopes *every existing* frontend call
  in one edit.
- **No header = the default node.** Every existing endpoint keeps working
  unchanged, so the migration is non-breaking and can land incrementally.
- The local managed daemon is auto-seeded as the default node row, so
  existing single-node installs gain a registry with zero user action.
- `rpc_for()` returns a real `B3RPCClient`, never a raw HTTP client — that is
  what keeps the allowlist non-bypassable.

---

# Phase 1 — Node registry + `rpc_for()` + console selector

Non-breaking. Delivers immediate value: RPC to any node from the console.

### 1.1 Schema (`backend/app/db.py`)

Add to `SCHEMA` (additive `CREATE TABLE IF NOT EXISTS`, matching existing style
— there is no migration framework; `init_db` runs the script every start):

```sql
CREATE TABLE IF NOT EXISTS nodes (
 id INTEGER PRIMARY KEY,
 name TEXT UNIQUE NOT NULL,          -- operator-facing label, e.g. "validator-2"
 kind TEXT NOT NULL CHECK (kind IN ('local','remote')),
 host TEXT NOT NULL DEFAULT '127.0.0.1',
 port INTEGER NOT NULL,
 rpc_user_enc TEXT,                  -- Fernet blob (app/vault.py); NULL for 'local'
 rpc_password_enc TEXT,              -- Fernet blob; NULL for 'local'
 is_default INTEGER NOT NULL DEFAULT 0,
 daemon_build TEXT,                  -- pinned build tag/sha, Phase 4; NULL = whatever is installed
 created_ts REAL NOT NULL,
 updated_ts REAL NOT NULL
);
```

`kind='local'` means "the daemon this container supervises" — its credentials
come from `settings` (which reads `b3coin.conf`, see `config.py._read_rpc_from_conf`),
never from the DB. Only `remote` rows store credentials.

Accessors to add, following the naming style already in `db.py`
(`notification_prefs_get` etc.): `node_list`, `node_get`, `node_add`,
`node_update`, `node_delete`, `node_set_default`, `node_get_default`.

`node_delete` must refuse to delete the last remaining node and must refuse to
delete the default without promoting another first.

### 1.2 Registry + resolver (`backend/app/deps.py`)

- Keep `AppState.rpc` as-is (it stays the default-node client; leaving it
  means nothing breaks while call sites are migrated one at a time).
- Add `AppState._node_clients: dict[int, B3RPCClient]` — a cache so a client
  is built once per node, not per request.
- Add:

```python
def rpc_for(self, request: Request) -> B3RPCClient:
    """Resolve the X-B3-Node header to that node's RPC client.
    No header -> the default node. Unknown id -> 404."""
```

  It must: read `X-B3-Node`; fall back to `node_get_default`; for `kind='local'`
  return `self.rpc`; for `kind='remote'` build/cache a
  `B3RPCClient(host, port, user, password)` (signature at `rpc.py`, decrypting
  creds via `vault_from_settings(...)` as `routers/staking.py` does); raise
  `HTTPException(404, "unknown node")` for a bad id.
- Auto-seed the local node row in `create_app_state()` if the `nodes` table is
  empty (name `"local"`, `kind='local'`, `is_default=1`, port from settings).

### 1.3 Node CRUD API (`backend/app/routers/nodes.py`, new)

Copy the shape of `backend/app/routers/contacts.py` exactly — `_state(request)`,
`require_2fa` for reads, `require_csrf` for writes, `db.audit(...)` on every
write, pydantic bodies.

- `GET /api/nodes` — list. **Never return credentials**, not even encrypted
  (mirrors how `routers/notifications.py` omits push subscription keys).
- `POST /api/nodes` — add a remote node `{name, host, port, rpc_user, rpc_password}`.
  Verify connectivity with a `getblockchaininfo` before saving; refuse to save
  a node that does not answer (a silently-dead node entry is worse than an error).
- `PUT /api/nodes/{id}` — update label/host/port/credentials.
- `DELETE /api/nodes/{id}` — with the guards from 1.1.
- `POST /api/nodes/{id}/default` — promote to default.
- Register in `backend/app/main.py` alongside the other routers.

### 1.4 Console node selector (`backend/app/routers/console.py`)

`console_run` (`:234`) currently uses `state.rpc.call_unrestricted(...)` at
`:282` and `state.rpc.call(...)` at `:301`. Switch both to `state.rpc_for(request)`.
Keep the trust/IP gate (`_trust_mode`) exactly as it is — it gates the
*operator*, not the node.

Frontend: add a node `<select>` to the console view in `frontend/index.html`
and set the header on the console call in `frontend/js/features/console.js`.

### 1.5 Frontend plumbing

- `frontend/js/app.js`: add `nodes: { list: [], selected: null, loaded: false }`
  to `initialState()`.
- `frontend/js/core/api.js`: in `api()`, alongside the existing CSRF header
  injection, add `if (this.nodes?.selected) headers['X-B3-Node'] = this.nodes.selected;`
  — this one edit scopes every existing call.
- `frontend/js/features/nodes.js` (new mixin): `loadNodes`, `selectNode`,
  `addNode`, `removeNode`, `setDefaultNode`. Wire into `app.js` like
  `notifMixin`.

### Phase 1 done when

- `pytest` green (see Verification below) including new `tests/test_nodes.py`:
  registry CRUD needs session/CSRF; credentials never returned; unknown
  `X-B3-Node` → 404; **no header still resolves to the default node** (the
  non-breaking guarantee — assert an existing endpoint like
  `/api/chain/summary` behaves identically with and without the header);
  last-node and default-node delete guards.
- Console can run `getblockchaininfo` against two different nodes in one
  session and get different answers (verify against two containers).

---

# Phase 2 — Node switcher + fleet dashboard

### 2.1 Fleet endpoint (`backend/app/routers/nodes.py`)

`GET /api/nodes/fleet` — for every registered node, in parallel:

```python
results = await asyncio.gather(*(_probe(n) for n in nodes), return_exceptions=True)
```

Per node collect `getblockchaininfo`, `getstakinginfo`, `getfinalityinfo`,
`getwalletassets` (all already allowlisted in `rpc.py`). **One unreachable node
must never fail the whole response** — return `{"reachable": false, "error": ...}`
for that row and keep the rest. Give the endpoint its own short RPC timeout so
a hung node cannot stall the dashboard.

### 2.2 Fleet view (frontend)

- Register a `fleet` view in `frontend/js/core/router.js` (`VIEWS` +
  `NAV_GROUPS`/`MORE_GROUPS` + an `onEnterView` case, exactly like the
  existing entries).
- One row per node: name, reachable, height (+ delta vs. the highest node —
  divergence is the signal that matters), peers, finality epoch + quorum
  participation, staking state, validator bound/unbound, FN Coin balance,
  last block produced.
- Reuse existing classes (`.list-row`, `.badge`, `.card`); do not invent a new
  component vocabulary. Check `frontend/css/components.css` first.
- A node switcher in the shell scopes every other view; the existing wallet /
  send / staking / node views need **no redesign**, they inherit the selection.

### 2.3 Safety: name the node in confirmations

Every wallet-affecting confirm flow (`send`, `unstake`, batch/consolidation
execute) must show the **node name** in the confirmation text, not just in a
dropdown elsewhere on the page. Sending from the wrong validator's wallet is
the failure mode that costs real FN Coins. The `confirm({...})` helper in
`frontend/js/app.js` already takes a `detail` list of `{key, value}` rows — add
the node there.

### Phase 2 done when

- Fleet view renders N nodes with one unreachable node degrading to a single
  bad row (test with a deliberately wrong port).
- Screenshots at 375px **and** desktop (project rule, see `AGENTS.md`).
- Confirm dialogs show the node name.

---

# Phase 3 — Per-node background monitors + node dimension on alerts

The fiddliest phase. Do it before the notification/monitor code grows further.

### 3.1 Monitors become per-node

`monitor.py` (`ChainMonitor`, module singleton `monitor`), `wallet_monitor.py`
(`WalletMonitor`, singleton `wallet_monitor`), `autostake.py` and
`consolidation.py` all hold module-level state plus one `rpc`.

Convert the singletons to registries keyed by node id:

```python
monitors: dict[int, ChainMonitor] = {}
def start_monitors(settings, state) -> None   # one instance per registered node
async def stop_monitors() -> None
```

Update `main.py`'s lifespan accordingly, and `GET /api/alerts/status`
(`routers/alerts.py:45`) which currently reaches into `monitor._last_blocks`
etc. — it becomes per-node.

Keep autostake/consolidation **default-node-only for now** unless there is a
reason not to: unattended spending on N nodes at once is a much bigger blast
radius, and it is not needed for FlowMesh testing. Say so explicitly in the
code comment rather than leaving it ambiguous.

### 3.2 Node dimension on alerts + notifications

- `alerts` table: add `node_id INTEGER`. `db.alert_add(...)` takes an optional
  node id; `alert_list` can filter by node. The alerts bell should show which
  node an alert came from (`frontend/js/features/alerts.js`).
- `app/notifier.py`: the coalescing window is keyed by `event_type` only
  (`notification_pending.event_type` is the PRIMARY KEY). It must become
  `(event_type, node_id)` or a composite key string like `stake@3`, otherwise
  a burst on one validator suppresses notifications from another — which
  would be a silent, hard-to-notice bug. Update
  `notification_pending_open/bump/due/close` and `notification_seen` (the
  dedupe key `{txid}:{vout}:{bucket}` must gain the node id, since two nodes
  can legitimately see the same txid).
- Notification bodies should name the node.

### Phase 3 done when

- A burst on node A and a burst on node B produce independent digests (add a
  test asserting exactly this — it is the regression this phase exists to
  prevent).
- Per-node monitor status is visible in the fleet view.

---

# Phase 4 — Pinned dev-build installation

### 4.1 The build artifact

`install_version()` (`backend/app/daemon_release.py:173`) is already
source-agnostic: it fetches a tarball from a URL, verifies sha256, extracts,
copies `b3coind`/`b3coin-cli`/`b3coin-wallet`/`b3coin-tx`/`b3coin-util` into
`daemon_dir`, writes `.installed_version`, and rolls back on failure. It does
not care that GitHub exists — only `list_releases()` does.

So a dev build needs only to produce **a tarball with a `bin/` directory plus
its sha256**, and be reachable by URL (a LAN HTTP server or file path is fine).

- Add `docker/Dockerfile.devbuild` (new, **never pulled by normal users**):
  a build-toolchain image that compiles B3-CoinV2 from a given branch/commit
  and emits that tarball + digest. It must not share a base layer with, or be
  referenced by, the runtime image — the runtime image stays
  `python:3.12-slim` + curl/gosu/zstd + Tailscale, with no compiler.
- Extend `ReleaseInfo` handling so a build can be installed from an explicit
  `{url, sha256}` pair rather than a GitHub lookup. `install_version()` itself
  should need little or no change.

### 4.2 Pin the whole fleet to one build

For FlowMesh results to mean anything, **every validator must run the same
binary**. Store the pinned build (tag or sha256) per node in `nodes.daemon_build`,
surface a mismatch loudly in the fleet view, and install by digest — never
"latest".

### 4.3 Real-funds protection

These wallets hold real FN Coins, so the risk is **not** an untrusted binary
(the operator compiled it) — it is wallet-format and consensus risk: an
unreleased build can write a `wallet.dat` an official release will not read,
or fork the validator off.

- Call `backupwallet` (already allowlisted) **automatically before every
  daemon swap**, and keep the backup outside the data dir being swapped.
- Refuse the swap if the backup fails. No exceptions.
- Note in the UI that validator stakes should be segregated from main
  holdings.

### 4.4 Unpublished GitHub prereleases (cheap alternative)

If the dev build is published as a GitHub **prerelease**, most of Phase 4 is
unnecessary: `ReleaseInfo` already carries a `prerelease` field and
`list_releases(include_prerelease=...)` already filters on it — it is simply
hardcoded `False` at three call sites (`routers/setup.py:588`, `:624`, `:684`).
Expose it behind the dev toggle and skip the builder image entirely. **Check
this first** — it may remove the need for 4.1 altogether.

---

# Cross-cutting rules (do not break these)

1. **The allowlist stays the only RPC choke point.** `rpc_for()` returns a
   `B3RPCClient`. Never hand a router a raw `httpx` client.
2. **Node RPC credentials are encrypted at rest** via `app/vault.py`, and are
   never returned by any API, even encrypted.
3. **Auth stays singular** — one operator, one login/2FA/session, N nodes. Do
   not add per-node auth.
4. **No header = default node**, forever. That is what keeps every existing
   endpoint and every existing test valid.
5. **No new runtime dependencies** in Phases 1–3. The build toolchain in
   Phase 4 lives only in `Dockerfile.devbuild`.
6. **Wallet-affecting actions name their node** in the confirmation.
7. Secrets scan (`scripts/scan_secrets.sh`) before every commit.

---

# Verification (per phase)

- **Backend**: `cd backend && ../.venv/bin/python -m pytest tests -q`.
  Baseline on WSL is 6 pre-existing failures (`test_persistence.py` ×5,
  `test_setup.py::test_wallet_unlock_blocked_on_ephemeral_data`) caused by
  `/tmp` being ext4 on WSL — **ignore those, flag anything else**.
- **Frontend**: real-browser screenshots at **375px and desktop** for any
  layout-affecting change (`AGENTS.md` rule). Recipe is in the
  `ui-visual-verification` memory.
- **Docker**: smoke-test with a scratch container/port/volume
  (`b3rc` / 18181 / fresh named volume) — **never touch the live `b3hive`
  container on port 18080**. See the `docker-smoke-testing` memory.
- **Multi-node specifically**: you need at least two daemons to test anything
  here. Two `RUN_UI=false` containers with distinct data dirs and distinct
  `RPC_PORT`/`B3_P2P_PORT` values is the cheapest rig; a mocked second node in
  `conftest.py` (a second `MockRPC` with different canned answers) covers the
  unit-test side without any real daemon.

# Gotchas (learned the hard way — do not rediscover these)

1. **New `Settings` attributes must be mirrored in two places** or everything
   breaks with `AttributeError`: `backend/tests/conftest.py` (the `settings`
   fixture builds `Settings.__new__(Settings)` and sets fields by hand) and
   `backend/dev_server.py`. Both already carry comments saying so.
2. **Alpine: never use `:value` bindings on `<option>` inside an `x-for` row.**
   The binding races `x-model` on the parent `<select>`, and the select
   silently falls back to the first option. Use static `value="..."`
   attributes. This produced a real bug in the notifications settings card.
3. The `settings` test fixture **touches `.daemon_deferred`**, which pauses
   `ChainMonitor` and `WalletMonitor`. A test that wants a monitor to actually
   run must `unlink()` it first.
4. `app.rpc.parse_amount` **rejects negative values** (it validates user
   input). `listtransactions` amounts are negative for sends — take `abs()`
   or parse with a local `Decimal` helper, as `wallet_monitor.py` does.
5. Amounts are **9dp `Decimal`, never float** (`AGENTS.md`).
6. Playwright against `dev_server.py`: submit the login form by **clicking the
   submit button** (pressing Enter proved unreliable), and the login
   rate-limiter trips after a few repeated runs — restart `dev_server.py` to
   reset it. Touch `<tmpdir>/.wizard_complete` to skip the wizard.
7. Compose merges `ports:` lists by union across `-f` files — verify with
   `docker compose -f ... config` rather than assuming.
