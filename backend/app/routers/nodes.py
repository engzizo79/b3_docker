"""Node registry API (multi-node fleet console, docs/MULTINODE_PLAN.md
Phase 1). Lets the operator register other B3Hive daemons (validators
under test, a watch-only node, a spare) and select which one a request
targets via the X-B3-Node header (see app/deps.py AppState.rpc_for).

The 'local' row (this container's own managed/external daemon) is
auto-seeded and cannot be deleted or have its credentials edited here —
those come from Settings (b3coin.conf), never the registry. Only 'remote'
rows are created/edited through this API, and their credentials are
Fernet-encrypted at rest (app/vault.py) and never returned, not even
encrypted."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.deps import AppState
from app.rpc import B3RPCClient, RPCError, RPCNotAllowed, RPCUnavailable
from app.vault import vault_from_settings

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

# The fleet dashboard polls every registered node in parallel; a hung node
# must never stall the whole response, so probes use a short timeout of
# their own instead of B3RPCClient's normal 30s default.
_FLEET_PROBE_TIMEOUT = 8.0


def _state(request: Request) -> AppState:
    return request.app.state.app_state


class NodeCreateBody(BaseModel):
    name: str
    host: str = "127.0.0.1"
    port: int
    rpc_user: str = ""
    rpc_password: str = ""


class NodeUpdateBody(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    rpc_user: str | None = None
    rpc_password: str | None = None


def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())[:64]
    if not name:
        raise HTTPException(status_code=422, detail="give the node a name")
    return name


def _valid_port(port: int) -> int:
    if not isinstance(port, int) or port <= 0 or port > 65535:
        raise HTTPException(status_code=422, detail="port must be between 1 and 65535")
    return port


def _public(node: dict) -> dict:
    """Never returns credentials, not even encrypted — mirrors how
    routers/notifications.py omits push subscription keys."""
    return {
        "id": node["id"], "name": node["name"], "kind": node["kind"],
        "host": node["host"], "port": node["port"],
        "is_default": bool(node["is_default"]),
        "daemon_build": node["daemon_build"],
        "has_credentials": bool(node["rpc_user_enc"] and node["rpc_password_enc"]),
        "created_ts": node["created_ts"], "updated_ts": node["updated_ts"],
    }


async def _verify_reachable(host: str, port: int, user: str, password: str) -> None:
    """A silently-dead node entry is worse than an error — refuse to save
    a node that does not answer getblockchaininfo."""
    probe = B3RPCClient(host, port, user, password, timeout=10.0)
    try:
        await probe.call("getblockchaininfo")
    except RPCError as exc:
        raise HTTPException(status_code=422,
                            detail=f"node rejected the request: {exc.message}")
    except RPCUnavailable as exc:
        raise HTTPException(status_code=422, detail=f"could not reach that node: {exc}")


@router.get("")
async def list_nodes(request: Request):
    state = _state(request)
    state.require_2fa(request)
    return {"nodes": [_public(n) for n in db.node_list(state.settings.db_path)]}


def _probe_client(state: AppState, node: dict) -> B3RPCClient:
    """A short-timeout, uncached client for fleet probing — never the
    request-scoped rpc_for() client, whose (longer) timeout would let one
    hung node stall the whole dashboard."""
    if node["kind"] == "local":
        s = state.settings
        return B3RPCClient(s.rpc_host, s.rpc_port, s.rpc_user, s.rpc_password,
                           timeout=_FLEET_PROBE_TIMEOUT)
    vault = vault_from_settings(state.settings, state.settings.b3_data_dir)
    user = password = ""
    if vault is not None:
        if node["rpc_user_enc"]:
            user = vault.decrypt(node["rpc_user_enc"]) or ""
        if node["rpc_password_enc"]:
            password = vault.decrypt(node["rpc_password_enc"]) or ""
    return B3RPCClient(node["host"], node["port"], user, password,
                       timeout=_FLEET_PROBE_TIMEOUT)


async def _probe(state: AppState, node: dict) -> dict:
    """Everything the fleet dashboard shows for one node. Only
    getblockchaininfo failing marks the node unreachable — the other calls
    are wallet-scoped (getstakinginfo, getfinalityinfo, getwalletassets)
    and simply come back None if no wallet is loaded on that node, so one
    missing wallet never hides the rest of a reachable node's row."""
    out = {"id": node["id"], "name": node["name"], "kind": node["kind"],
          "is_default": bool(node["is_default"]), "reachable": False}
    client = _probe_client(state, node)
    try:
        info = await client.call("getblockchaininfo")
    except (RPCError, RPCUnavailable, RPCNotAllowed) as exc:
        out["error"] = str(exc)
        return out
    out["reachable"] = True
    out["blocks"] = info.get("blocks")
    out["chain"] = info.get("chain")

    try:
        net = await client.call("getnetworkinfo")
        out["peers"] = net.get("connections")
    except (RPCError, RPCUnavailable, RPCNotAllowed):
        out["peers"] = None

    try:
        stk = await client.call("getstakinginfo")
        loop = stk.get("staking") or {}
        out["staking"] = {
            "running": bool(loop.get("running")),
            "active": stk.get("active"),
            "blocks_produced": loop.get("blocks_produced"),
        }
    except (RPCError, RPCUnavailable, RPCNotAllowed):
        out["staking"] = None

    try:
        fin = await client.call("getfinalityinfo")
        binding = fin.get("binding") or {}
        vset = fin.get("validator_set") or {}
        out["finality"] = {
            "bound": bool(binding.get("bound")) and not bool(binding.get("revoked")),
            "member": bool(vset.get("member")),
            "weight": vset.get("weight"),
            "total_weight": vset.get("total_weight"),
        }
    except (RPCError, RPCUnavailable, RPCNotAllowed):
        out["finality"] = None

    try:
        assets = await client.call("getwalletassets")
        fn = next((a for a in (assets.get("assets") or []) if a.get("kind") == "fn"), None)
        out["fn_balance"] = fn.get("spendable") if fn else None
    except (RPCError, RPCUnavailable, RPCNotAllowed):
        out["fn_balance"] = None

    return out


@router.get("/fleet")
async def fleet_status(request: Request):
    """One round trip covering every registered node, in parallel. One
    unreachable/erroring node never fails the whole response — see _probe."""
    state = _state(request)
    state.require_2fa(request)
    nodes = db.node_list(state.settings.db_path)
    results = await asyncio.gather(*(_probe(state, n) for n in nodes), return_exceptions=True)
    rows = []
    for n, r in zip(nodes, results):
        if isinstance(r, BaseException):
            rows.append({"id": n["id"], "name": n["name"], "kind": n["kind"],
                        "is_default": bool(n["is_default"]), "reachable": False,
                        "error": str(r)})
        else:
            rows.append(r)
    heights = [r["blocks"] for r in rows if r.get("reachable") and r.get("blocks") is not None]
    top = max(heights) if heights else None
    for r in rows:
        r["height_delta"] = (top - r["blocks"]) if (
            top is not None and r.get("blocks") is not None) else None
    return {"nodes": rows, "top_height": top}


@router.post("")
async def add_node(body: NodeCreateBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    dbp = state.settings.db_path
    name = _clean_name(body.name)
    host = (body.host or "127.0.0.1").strip()
    port = _valid_port(body.port)
    user = body.rpc_user or ""
    password = body.rpc_password or ""

    await _verify_reachable(host, port, user, password)

    vault = vault_from_settings(state.settings, state.settings.b3_data_dir)
    if vault is None:
        raise HTTPException(status_code=503, detail="vault unavailable")
    try:
        node_id = db.node_add(
            dbp, name=name, kind="remote", host=host, port=port,
            rpc_user_enc=vault.encrypt(user), rpc_password_enc=vault.encrypt(password))
    except ValueError:
        raise HTTPException(status_code=409, detail="a node with that name already exists")
    db.audit(dbp, "node_add", sess.username, detail=f"name={name} host={host} port={port}")
    return {"ok": True, "node": _public(db.node_get(dbp, node_id))}


@router.put("/{node_id}")
async def update_node(node_id: int, body: NodeUpdateBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    dbp = state.settings.db_path
    node = db.node_get(dbp, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")

    fields: dict = {}
    if body.name is not None:
        fields["name"] = _clean_name(body.name)
    if node["kind"] == "local":
        # The local row's connection comes from Settings (b3coin.conf), never
        # this API — only its display name can be edited here.
        if body.host is not None or body.port is not None \
                or body.rpc_user is not None or body.rpc_password is not None:
            raise HTTPException(status_code=400,
                                detail="the local node's connection is managed by "
                                      "b3coin.conf, not editable here")
    else:
        host = body.host if body.host is not None else node["host"]
        port = _valid_port(body.port) if body.port is not None else node["port"]
        creds_changed = body.rpc_user is not None or body.rpc_password is not None
        conn_changed = body.host is not None or body.port is not None or creds_changed
        vault = vault_from_settings(state.settings, state.settings.b3_data_dir)
        if creds_changed or (conn_changed and vault is not None):
            if vault is None:
                raise HTTPException(status_code=503, detail="vault unavailable")
            user = body.rpc_user if body.rpc_user is not None else (
                vault.decrypt(node["rpc_user_enc"]) or "" if node["rpc_user_enc"] else "")
            password = body.rpc_password if body.rpc_password is not None else (
                vault.decrypt(node["rpc_password_enc"]) or "" if node["rpc_password_enc"] else "")
            if conn_changed:
                await _verify_reachable(host, port, user, password)
            fields["rpc_user_enc"] = vault.encrypt(user)
            fields["rpc_password_enc"] = vault.encrypt(password)
        if body.host is not None:
            fields["host"] = host
        if body.port is not None:
            fields["port"] = port
        # Credentials changed: drop any cached client so the next call
        # rebuilds it with the fresh values instead of the stale ones.
        state._node_clients.pop(node_id, None)

    try:
        ok = db.node_update(dbp, node_id, **fields)
    except ValueError:
        raise HTTPException(status_code=409, detail="a node with that name already exists")
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    db.audit(dbp, "node_update", sess.username, detail=f"id={node_id}")
    return {"ok": True, "node": _public(db.node_get(dbp, node_id))}


@router.delete("/{node_id}")
async def delete_node(node_id: int, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    dbp = state.settings.db_path
    try:
        deleted = db.node_delete(dbp, node_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="node not found")
    state._node_clients.pop(node_id, None)
    db.audit(dbp, "node_delete", sess.username, detail=f"id={node_id}")
    return {"ok": True}


@router.post("/{node_id}/default")
async def set_default_node(node_id: int, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    dbp = state.settings.db_path
    if not db.node_set_default(dbp, node_id):
        raise HTTPException(status_code=404, detail="node not found")
    db.audit(dbp, "node_set_default", sess.username, detail=f"id={node_id}")
    return {"ok": True, "node": _public(db.node_get(dbp, node_id))}
