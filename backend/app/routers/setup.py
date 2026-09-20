"""Setup wizard: first-run bootstrap + safe node configuration.

Flow (guarded by session + CSRF, all audited):
- status: wizard state (fresh chain? bootstrap in progress? conf view)
- bootstraps: live manifest from the explorer (heights, sizes, sha256)
- bootstrap/start: writes a JSON command for the entrypoint supervisor;
  only allowed on a FRESH chain (chainstate absent/empty) — never overwrites
  an existing chain
- bootstrap/progress: supervisor progress JSON (phase, bytes, percent)
- conf get/apply: view and edit b3coin.conf through a strict whitelist;
  RPC credentials and consensus-critical keys are LOCKED
- restart-node: queue a daemon restart to apply conf changes
- complete: write the wizard marker (UI stops redirecting to the wizard)
"""

import json
import os
import re
import secrets
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.deps import AppState

router = APIRouter(prefix="/api/setup", tags=["setup"])

# The explorer's reverse proxy 403s default python/httpx User-Agents; the
# manifest fetch must present a browser-like UA.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Keys the wizard may read and edit. Everything else is left untouched but
# preserved verbatim; unknown keys are rejected on apply.
WIZARD_KEYS = {
    "txindex": "Full transaction index (enables tx lookup for the explorer view)",
    "listen": "Accept incoming P2P connections",
    "maxconnections": "Maximum P2P connections",
    "proxy": "Tor/VPN proxy for P2P traffic (host:port, empty = direct)",
    "staking": None,  # documented as invalid in v1.1.4 — never written
    "bantime": "How long misbehaving peers stay banned (seconds)",
}

# Never editable through the wizard — RPC security and consensus.
LOCKED_KEYS = {
    "rpcuser", "rpcpassword", "rpcauth", "rpcbind", "rpcallowip", "rpcport",
    "datadir", "confdir", "reindex", "daemon", "printtoconsole",
    "disablewallet", "addresstype", "changetype", "testnet", "regtest",
}


class ApplyConfBody(BaseModel):
    conf: dict[str, str]


class BootstrapBody(BaseModel):
    height: int
    sha256: str
    url: str
    size: int = 0
    wipe_chain: bool = False  # explicit consent to replace an existing chain


class CreateWalletBody(BaseModel):
    wallet_name: str
    passphrase: str
    load_on_startup: bool = True


class LoadWalletBody(BaseModel):
    filename: str


def _wallet_name_ok(name: str) -> bool:
    """Wallet filenames live under datadir/wallets/ — reject traversal."""
    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name))


async def _wallet_status(s: AppState) -> dict:
    """Detect loaded wallets via listwallets; never expose keys."""
    try:
        loaded = await s.rpc.call("listwallets")
    except Exception:
        return {"loaded": [], "reachable": False}
    return {"loaded": list(loaded or []), "reachable": True}


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _wizard_done(s: AppState) -> bool:
    return Path(s.settings.wizard_marker_file).is_file()


def _progress(s: AppState) -> dict | None:
    p = Path(s.settings.bootstrap_progress_file)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _fresh_chain(s: AppState) -> bool:
    """A fresh chain has no (or an empty) chainstate directory. Bootstrap
    may only be applied on a fresh chain — never over existing data."""
    cs = Path(s.settings.node_datadir) / "chainstate"
    if not cs.is_dir():
        return True
    return not any(cs.iterdir())


def _bootstrap_running(s: AppState) -> bool:
    prog = _progress(s)
    return bool(prog and prog.get("phase") not in (None, "done", "failed"))


@router.get("/status")
async def setup_status(request: Request):
    s = _state(request)
    s.require_2fa(request)
    prog = _progress(s)
    return {
        "wizard_done": _wizard_done(s),
        "setup_required": s.setup_mode(),
        # True while the daemon has NOT been told to start yet (first-run
        # deferral). After the entrypoint starts the daemon it removes the
        # marker — so wizard_done + daemon_deferred=false + node not
        # answering means the daemon IS booting, which the UI uses to keep
        # "Start node" disabled instead of inviting a duplicate start.
        "daemon_deferred": Path(s.settings.daemon_deferred_file).is_file(),
        # Upgrade path (v0.6.0+): wizard done, managed mode, but the daemon
        # binary is absent (unbundled image; install predates the daemon/
        # subfolder). The UI must offer a targeted install screen, NOT a
        # wizard re-run, and must NOT claim the node is "starting".
        "daemon_binary_missing": (
            s.settings.daemon_mode == "managed"
            and not (Path(s.settings.daemon_dir) / "b3coind").is_file()
        ),
        "daemon_mode": s.settings.daemon_mode,
        "fresh_chain": _fresh_chain(s),
        "data_persistent": s.data_persistent(),
        "wallet": await _wallet_status(s),
        "bootstrap": prog or {"phase": "idle"},
        "conf": _read_conf(s),
    }


@router.get("/bootstraps")
async def list_bootstraps(request: Request):
    s = _state(request)
    s.require_2fa(request)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": BROWSER_UA}) as client:
            r = await client.get(s.settings.bootstrap_manifest_url)
            r.raise_for_status()
            manifest = r.json()
    except Exception as exc:
        raise HTTPException(502, f"explorer manifest unavailable: {exc}")
    entries = manifest.get("bootstraps", [])
    fresh = _fresh_chain(s)
    return {
        "available": fresh and not _bootstrap_running(s),
        "fresh_chain": fresh,
        "manifest_url": s.settings.bootstrap_manifest_url,
        "bootstraps": [
            {
                "height": e.get("height"),
                "size": e.get("size"),
                "sha256": e.get("sha256"),
                "url": e.get("url"),
                "created_utc": e.get("created_utc"),
            }
            for e in entries if e.get("sha256")
        ],
    }


@router.post("/bootstrap/start")
async def start_bootstrap(body: BootstrapBody, request: Request):
    s = _state(request)
    s.require_csrf(request)
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
         (request.client.host if request.client else "unknown")

    # Fund-loss guard: never download a chain snapshot into a container
    # that loses its data on `docker compose down`.
    s.require_persistent_data("downloading a chain snapshot")

    # Guards: session/CSRF are enforced by dependency wiring; here the rails:
    if not _fresh_chain(s) and not body.wipe_chain:
        raise HTTPException(409, "chain data already exists — bootstrap would "
                                 "overwrite it; enable the replace-chain option "
                                 "to allow")
    if _bootstrap_running(s):
        raise HTTPException(409, "bootstrap already in progress")
    if not re.fullmatch(r"[0-9a-f]{64}", body.sha256 or ""):
        raise HTTPException(422, "invalid sha256")
    if not re.fullmatch(r"/bootstraps/bootstrap-\d+\.tar\.zst", str(body.url)):
        raise HTTPException(422, "invalid bootstrap url")

    # Resolve the relative URL against the manifest origin (the host that
    # actually serves the archives); fall back to the explorer URL.
    base = s.settings.bootstrap_manifest_url
    base = base[:base.find("/bootstraps/")] if "/bootstraps/" in base else ""
    if not base.startswith("http"):
        base = s.settings.explorer_url.rstrip("/")
    full_url = base + str(body.url)

    cmd = {
        "id": secrets.token_hex(8),
        "height": body.height,
        "url": full_url,
        "sha256": body.sha256,
        "size": body.size,
        "wipe": bool(body.wipe_chain),
    }
    cmd_file = Path(s.settings.bootstrap_cmd_file)
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_file.write_text(json.dumps(cmd))
    db.audit(s.settings.db_path, "setup.bootstrap.start", s.username,
             detail=f"ip={ip} height={body.height} wipe={bool(body.wipe_chain)}")
    return {"ok": True, "cmd_id": cmd["id"]}


@router.get("/bootstrap/progress")
async def bootstrap_progress(request: Request):
    s = _state(request)
    s.require_2fa(request)
    return _progress(s) or {"phase": "idle"}


@router.post("/restart-node")
async def restart_node(request: Request):
    """Queue a daemon restart (applies b3coin.conf changes)."""
    s = _state(request)
    s.require_csrf(request)
    if _bootstrap_running(s):
        raise HTTPException(409, "bootstrap in progress")
    Path(s.settings.recovery_cmd_file).parent.mkdir(parents=True, exist_ok=True)
    Path(s.settings.recovery_cmd_file).write_text("restart")
    db.audit(s.settings.db_path, "setup.restart_node", s.username,
             detail=f"ip={request.client.host if request.client else 'unknown'}")
    return {"ok": True}


@router.post("/start-node")
async def start_node(request: Request):
    """Wizard: user chose sync-from-scratch. Signals the entrypoint supervisor
    to start the daemon (deferred until first wizard completion in UI mode)."""
    s = _state(request)
    s.require_csrf(request)
    # Fund-loss guard: starting a sync (and any wallet created afterwards)
    # on ephemeral storage can silently lose funds on container removal.
    s.require_persistent_data("starting the node")
    # Dedup: if the daemon was already told to start (entrypoint removed the
    # deferred marker when it launched it) and setup is complete, a second
    # command would be a duplicate — the UI must never offer it, and the API
    # never accepts it. Plain language only; no internal file names leak.
    wizard_done = Path(s.settings.wizard_marker_file).is_file()
    deferred = Path(s.settings.daemon_deferred_file).is_file()
    if wizard_done and not deferred:
        db.audit(s.settings.db_path, "setup.start_node_rejected", s.username,
                 detail=f"ip={request.client.host if request.client else 'unknown'}")
        raise HTTPException(
            status_code=409,
            detail="Your node is already starting or running. Give it a few minutes "
                   "to scan its block index, or check the node view.",
        )
    # Upgrade-path guard: with the wizard complete but the daemon binary
    # missing (v0.6.0+ unbundled image), a start command is a lie — the
    # supervisor has nothing to launch. Refuse and point at the install
    # flow instead of writing a command that silently does nothing.
    if (wizard_done
            and s.settings.daemon_mode == "managed"
            and not (Path(s.settings.daemon_dir) / "b3coind").is_file()):
        raise HTTPException(
            status_code=409,
            detail="The node software is not installed yet. Install it from "
                   "the update screen first, then start the node.",
        )
    cmd_file = Path(s.settings.start_node_cmd_file)
    cmd_file.parent.mkdir(parents=True, exist_ok=True)
    cmd_file.write_text("start")
    db.audit(s.settings.db_path, "setup.start_node", s.username,
             detail=f"ip={request.client.host if request.client else 'unknown'}")
    return {"ok": True}


@router.post("/complete")
async def complete_wizard(request: Request):
    s = _state(request)
    if s.setup_mode():
        # No password yet: local-only window is still open.
        raise HTTPException(status_code=403, detail="set a login password before finishing setup (Security step)")
    # Fund-loss guard (CRITICAL): finishing first-run setup and starting a
    # sync on ephemeral storage can silently lose a wallet and its coins
    # on container removal. The wizard must stop HERE, not at wallet time.
    s.require_csrf(request)
    s.require_persistent_data("finishing setup")
    Path(s.settings.wizard_marker_file).parent.mkdir(parents=True, exist_ok=True)
    Path(s.settings.wizard_marker_file).write_text("done")
    db.audit(s.settings.db_path, "setup.complete", s.username,
             detail=f"ip={request.client.host if request.client else 'unknown'}")
    return {"ok": True, "wizard_done": True}


# --- wallet create / migrate -------------------------------------------------


# --- wizard wallet queue (daemon may be DOWN: intents, not executions) -----


class QueueCreateBody(BaseModel):
    wallet_name: str
    passphrase: str


class QueueLoadBody(BaseModel):
    filename: str


@router.get("/wallet/queue")
async def wallet_queue_list(request: Request):
    """Queued wallet intents and their status (passphrase never returned)."""
    s = _state(request)
    s.require_2fa(request)
    return {"queue": db.list_wallet_queue(s.settings.db_path)}


@router.post("/wallet/queue/create")
async def wallet_queue_create(body: QueueCreateBody, request: Request):
    """Queue a create-wallet intent. Executed automatically once the node
    is up (start of daemon). Passphrase is Fernet-encrypted at rest and
    wiped after processing."""
    s = _state(request)
    s.require_csrf(request)
    s.require_persistent_data()
    name = (body.wallet_name or "").strip()
    if not _wallet_name_ok(name):
        raise HTTPException(422, "invalid wallet name")
    passphrase = (body.passphrase or "").strip()
    if len(passphrase) < 8:
        raise HTTPException(422, "passphrase must be at least 8 characters")
    from app.vault import vault_from_settings
    vault = vault_from_settings(s.settings, s.settings.b3_data_dir)
    if vault is None:
        raise HTTPException(500, "cannot create vault for queued passphrase")
    blob = vault.encrypt(passphrase)
    intent_id = db.queue_wallet_intent(s.settings.db_path, "create", name, blob)
    db.audit(s.settings.db_path, "setup.wallet.queue_create", s.username,
             detail=f"ip={request.client.host if request.client else 'unknown'} name={name}")
    return {"ok": True, "queued": True, "id": intent_id, "wallet": name}


@router.post("/wallet/queue/load")
async def wallet_queue_load(body: QueueLoadBody, request: Request):
    """Queue a load-wallet intent (no passphrase needed)."""
    s = _state(request)
    s.require_csrf(request)
    s.require_persistent_data()
    name = (body.filename or "").strip()
    if not _wallet_name_ok(name):
        raise HTTPException(422, "invalid wallet filename")
    intent_id = db.queue_wallet_intent(s.settings.db_path, "load", name, None)
    db.audit(s.settings.db_path, "setup.wallet.queue_load", s.username,
             detail=f"ip={request.client.host if request.client else 'unknown'} name={name}")
    return {"ok": True, "queued": True, "id": intent_id, "wallet": name}


@router.post("/wallet/queue/{intent_id}/remove")
async def wallet_queue_remove(intent_id: int, request: Request):
    """Remove a still-pending intent (user changed their mind in the wizard)."""
    s = _state(request)
    s.require_csrf(request)
    if not db.remove_wallet_intent(s.settings.db_path, intent_id):
        raise HTTPException(404, "intent not found or already processed")
    db.audit(s.settings.db_path, "setup.wallet.queue_remove", s.username,
             detail=f"id={intent_id}")
    return {"ok": True}


@router.get("/wallet/status")
async def wallet_status(request: Request):
    s = _state(request)
    s.require_2fa(request)
    return await _wallet_status(s)


@router.post("/wallet/create")
async def create_wallet(body: CreateWalletBody, request: Request):
    s = _state(request)
    s.require_csrf(request)
    s.require_persistent_data()
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
         (request.client.host if request.client else "unknown")
    name = (body.wallet_name or "").strip()
    if not _wallet_name_ok(name):
        raise HTTPException(422, "invalid wallet name")
    passphrase = (body.passphrase or "").strip()
    if len(passphrase) < 8:
        raise HTTPException(422, "passphrase must be at least 8 characters")
    try:
        await s.rpc.call(
            "createwallet", name, False, False, passphrase,
            False, True, body.load_on_startup, False,
        )
    except Exception as exc:
        from app.rpc import RPCError
        if isinstance(exc, RPCError):
            raise HTTPException(409, f"node rejected createwallet: {exc.message}")
        raise HTTPException(502, f"createwallet failed: {exc}")
    db.audit(s.settings.db_path, "setup.wallet.create", s.username,
             detail=f"ip={ip} name={name}")
    return {"ok": True, "wallet": name}


@router.post("/wallet/load")
async def load_wallet(body: LoadWalletBody, request: Request):
    s = _state(request)
    s.require_csrf(request)
    s.require_persistent_data()
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
         (request.client.host if request.client else "unknown")
    name = (body.filename or "").strip()
    if not _wallet_name_ok(name):
        raise HTTPException(422, "invalid wallet filename")
    try:
        await s.rpc.call("loadwallet", name)
    except Exception as exc:
        from app.rpc import RPCError
        if isinstance(exc, RPCError):
            raise HTTPException(409, f"node rejected loadwallet: {exc.message}")
        raise HTTPException(502, f"loadwallet failed: {exc}")
    db.audit(s.settings.db_path, "setup.wallet.load", s.username,
             detail=f"ip={ip} name={name}")
    return {"ok": True, "wallet": name}


# --- conf helpers ------------------------------------------------------------


def _read_conf(s: AppState) -> dict:
    """Read the wizard-visible view of b3coin.conf: editable values,
    locked keys reported as present, unknown keys reported as unrecognized."""
    conf_path = Path(s.settings.node_datadir) / "b3coin.conf"
    editable: dict[str, str] = {}
    locked: list[str] = []
    unknown: list[str] = []
    if conf_path.is_file():
        for line in conf_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in LOCKED_KEYS:
                locked.append(key)
            elif key in WIZARD_KEYS and WIZARD_KEYS[key] is not None:
                editable[key] = val.strip()
            elif key in WIZARD_KEYS:
                continue  # documented-invalid keys (staking) just skipped
            else:
                unknown.append(key)
    return {"editable": editable, "locked": sorted(locked),
            "unrecognized": unknown, "path": str(conf_path)}


def _apply_conf(s: AppState, new_vals: dict[str, str]) -> None:
    """Write wizard keys into b3coin.conf, preserving every other line
    verbatim. Locked keys are impossible to reach (rejected earlier)."""
    conf_path = Path(s.settings.node_datadir) / "b3coin.conf"
    if not conf_path.is_file():
        raise HTTPException(409, "b3coin.conf not found")
    key_re = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=")
    out_lines: list[str] = []
    written = set()
    for line in conf_path.read_text().splitlines():
        m = key_re.match(line)
        if m and m.group(1) in new_vals:
            k = m.group(1)
            out_lines.append(f"{k}={new_vals[k]}")
            written.add(k)
        else:
            out_lines.append(line)
    # Keys not present in the file yet get appended under a wizard section
    missing = [k for k in new_vals if k not in written]
    if missing:
        out_lines.append("")
        out_lines.append("# --- wizard settings ---")
        for k in missing:
            out_lines.append(f"{k}={new_vals[k]}")
    tmp = conf_path.with_suffix(".conf.tmp")
    tmp.write_text("\n".join(out_lines) + "\n")
    tmp.replace(conf_path)


@router.get("/conf")
async def get_conf(request: Request):
    s = _state(request)
    s.require_2fa(request)
    return _read_conf(s)


@router.post("/conf/apply")
async def apply_conf(body: ApplyConfBody, request: Request):
    s = _state(request)
    s.require_csrf(request)
    ip = request.client.host if request.client else "unknown"
    clean: dict[str, str] = {}
    for k, v in body.conf.items():
        if k in LOCKED_KEYS:
            raise HTTPException(422, f"key '{k}' is locked")
        if k not in WIZARD_KEYS or WIZARD_KEYS[k] is None:
            raise HTTPException(422, f"key '{k}' is not a wizard-editable key")
        v = str(v).strip()
        if v == "":
            raise HTTPException(422, f"key '{k}': empty value not allowed")
        clean[k] = v
    if "txindex" in clean and clean["txindex"] not in ("0", "1"):
        raise HTTPException(422, "txindex must be 0 or 1")
    if "listen" in clean and clean["listen"] not in ("0", "1"):
        raise HTTPException(422, "listen must be 0 or 1")
    if "maxconnections" in clean:
        try:
            n = int(clean["maxconnections"])
            if not (1 <= n <= 1000):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "maxconnections must be 1-1000")
    if "bantime" in clean:
        try:
            n = int(clean["bantime"])
            if n < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "bantime must be a non-negative integer")
    _apply_conf(s, clean)
    db.audit(s.settings.db_path, "setup.conf.apply", s.username,
             detail=f"ip={ip} keys={','.join(sorted(clean))}")
    return {"ok": True, "applied": sorted(clean)}



# ---------------------------------------------------------------------------
# v0.6.0 daemon mode + version picker (wizard Node step)
# ---------------------------------------------------------------------------

from app import daemon_release
from pydantic import BaseModel


class DaemonChooseBody(BaseModel):
    mode: str  # "managed" | "external"
    version: str | None = None  # tag like "v1.1.4" (managed only)
    ext_host: str | None = None
    ext_port: int | None = None
    ext_user: str | None = None
    ext_password: str | None = None


@router.get("/daemon/releases")
async def daemon_releases(request: Request):
    """List available daemon releases from GitHub (cached, failure-tolerant).

    Returns the releases the wizard can offer in the Node step.
    """
    s = _state(request)
    s.require_2fa(request)
    minimum = s.settings.min_daemon_version
    try:
        rels = daemon_release.list_releases(include_prerelease=False, timeout=15)
    except Exception as exc:
        return {"releases": [], "error": str(exc), "min_version": minimum}
    out = []
    for r in rels:
        out.append({
            "tag": r.tag,
            "version": r.version,
            "meets_minimum": daemon_release.meets_minimum(r.version, minimum),
            "published": getattr(r, "published", ""),
        })
    return {"releases": out, "min_version": minimum,
            "installed": daemon_release.read_installed_version(
                s.settings.daemon_version_file)}


@router.post("/daemon/choose")
async def daemon_choose(body: DaemonChooseBody, request: Request):
    """Save the wizard's daemon mode + version / external RPC choice.

    Persisted to <data>/.daemon_choice.json. The entrypoint reads this on
    backend restart to set the mode; complete_wizard triggers the restart.
    """
    s = _state(request)
    s.require_csrf(request)
    ip = request.client.host if request.client else "unknown"
    mode = body.mode.strip().lower()
    if mode not in ("managed", "external"):
        raise HTTPException(422, "mode must be 'managed' or 'external'")
    choice = {"mode": mode}
    if mode == "managed":
        version = (body.version or "").strip().lstrip("vV")
        if not version:
            # default to latest stable that meets minimum
            try:
                rels = daemon_release.list_releases(
                    include_prerelease=False, timeout=15)
            except Exception as exc:
                raise HTTPException(502, f"release list failed: {exc}")
            picked = None
            for r in rels:
                if daemon_release.meets_minimum(r.version,
                                                s.settings.min_daemon_version):
                    picked = r.version
                    break
            if not picked:
                raise HTTPException(422, "no release meets minimum version")
            version = picked
        if not daemon_release.meets_minimum(version,
                                             s.settings.min_daemon_version):
            raise HTTPException(422,
                f"version {version} is below minimum "
                f"{s.settings.min_daemon_version}")
        choice["version"] = version
    else:
        # external: validate RPC params
        host = (body.ext_host or "").strip()
        port = body.ext_port or 0
        user = (body.ext_user or "").strip()
        password = body.ext_password or ""
        if not host:
            raise HTTPException(422, "external mode requires ext_host")
        if not (1 <= port <= 65535):
            raise HTTPException(422, "ext_port must be 1-65535")
        if not user:
            raise HTTPException(422, "external mode requires ext_user")
        if not password:
            raise HTTPException(422, "external mode requires ext_password")
        choice["ext_host"] = host
        choice["ext_port"] = port
        choice["ext_user"] = user
        choice["ext_password"] = password
    import json as _json
    choice_file = Path(s.settings.b3_data_dir) / ".daemon_choice.json"
    choice_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = choice_file.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(choice))
    tmp.replace(choice_file)
    # Managed mode: download the daemon binary if not already installed.
    restart_needed = False
    downloaded = False
    if mode == "managed":
        vf = Path(s.settings.daemon_version_file)
        vf.parent.mkdir(parents=True, exist_ok=True)
        daemon_bin = Path(s.settings.daemon_dir) / "b3coind"
        installed_ver = daemon_release.read_installed_version(
            str(vf)) if vf.exists() else ""
        need_download = (
            not daemon_bin.exists() or not daemon_bin.is_file()
            or not os.access(str(daemon_bin), os.X_OK)
            or installed_ver.lstrip("vV") != version
        )
        if need_download:
            # Find the matching release and download + install it.
            try:
                rels = daemon_release.list_releases(
                    include_prerelease=False, timeout=30)
            except Exception as exc:
                raise HTTPException(502, f"release list failed: {exc}")
            release = None
            for r in rels:
                if r.version == version or r.tag.lstrip("vV") == version:
                    release = r
                    break
            if release is None:
                raise HTTPException(
                    422, f"version {version} not found in releases")
            try:
                daemon_release.install_version(
                    release, s.settings.daemon_dir,
                    str(vf), timeout=300)
                downloaded = True
            except Exception as exc:
                raise HTTPException(500, f"daemon download failed: {exc}")
        else:
            # Already installed at the requested version.
            downloaded = False
        vf.write_text(version)
        # Backend restart when switching back from external to loopback RPC.
        if s.settings.daemon_mode == "external":
            restart_needed = True
    else:
        # External: backend must reload with EXT_RPC_* env.
        if s.settings.daemon_mode != "external":
            restart_needed = True
    if restart_needed:
        rb = Path(s.settings.b3_data_dir) / "restart-backend.cmd"
        rb.write_text("restart")
    db.audit(s.settings.db_path, "setup.daemon_choose", s.username,
             detail=f"ip={ip} mode={mode}"
                   + (f" version={version} downloaded={downloaded}"
                      if mode == "managed" else ""))
    return {"ok": True, "choice": {k: v for k, v in choice.items()
                                    if k != "ext_password"},
            "backend_restart": restart_needed,
            "downloaded": downloaded}


@router.get("/daemon/choice")
async def daemon_choice_get(request: Request):
    """Return the saved daemon choice (password redacted)."""
    s = _state(request)
    s.require_2fa(request)
    import json as _json
    choice_file = Path(s.settings.b3_data_dir) / ".daemon_choice.json"
    if not choice_file.is_file():
        return {"chosen": False}
    try:
        choice = _json.loads(choice_file.read_text())
    except Exception:
        return {"chosen": False}
    choice.pop("ext_password", None)
    return {"chosen": True, "choice": choice}
