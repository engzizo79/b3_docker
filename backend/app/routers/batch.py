"""User-definable batch actions: recipe CRUD, preview (no signing) and
execute (unlock + CSRF + confirm-token gated; testmempoolaccept-gated
broadcasts; abort on first rejection; audit log per run).

The browser never talks to the node; all RPC flows through the allowlisted
client. Recipes are declarative JSON — never code — and everything a recipe
says is re-validated and constrained by batch_engine rails."""

import json
import secrets

from fastapi import APIRouter, HTTPException, Request

from app import db
from app.batch_engine import (
    BatchEngine,
    RecipeError,
    confirm_token,
    validate_recipe,
)
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable

router = APIRouter(prefix="/api/batch", tags=["batch"])


def _state(request: Request):
    state = request.app.state.app_state
    state.engine = getattr(state, "engine", None) or BatchEngine(state.rpc)
    return state


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, RPCNotAllowed):
        return HTTPException(status_code=403, detail=f"RPC not allowed: {exc}")
    if isinstance(exc, RPCUnavailable):
        return HTTPException(status_code=503, detail="node RPC unavailable")
    if isinstance(exc, RPCError):
        return HTTPException(status_code=502, detail=f"node RPC error: {exc}")
    return HTTPException(status_code=500, detail=str(exc))


def _load_recipe(state, recipe_id: int) -> dict:
    """Load a stored recipe; re-validate so a corrupt row cannot bypass
    engine rails."""
    row = db.get_recipe(state.settings.db_path, recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    try:
        return validate_recipe(json.loads(row["recipe"]))
    except (RecipeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"stored recipe invalid: {exc}") from exc


# ---------------------------------------------------------------------------
# Recipe CRUD
# ---------------------------------------------------------------------------

@router.get("/recipes")
async def list_recipes(request: Request):
    state = _state(request)
    state.require_2fa(request)
    return {"recipes": [
        {"id": r["id"], "name": r["name"], "recipe": json.loads(r["recipe"])}
        for r in db.list_recipes(state.settings.db_path)
    ]}


@router.get("/recipes/{recipe_id}")
async def get_recipe(request: Request, recipe_id: int):
    state = _state(request)
    state.require_2fa(request)
    row = db.get_recipe(state.settings.db_path, recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return {"id": row["id"], "name": row["name"], "recipe": json.loads(row["recipe"])}


@router.post("/recipes")
async def create_recipe(request: Request):
    state = _state(request)
    state.require_csrf(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        recipe = validate_recipe(body)
    except RecipeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        recipe_id = db.create_recipe(
            state.settings.db_path, recipe["name"], json.dumps(body))
    except Exception:
        raise HTTPException(status_code=409, detail="recipe name already exists")
    db.audit(state.settings.db_path, "recipe_create",
             state.username, recipe["name"])
    return {"ok": True, "id": recipe_id}


@router.put("/recipes/{recipe_id}")
async def update_recipe(request: Request, recipe_id: int):
    state = _state(request)
    state.require_csrf(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        recipe = validate_recipe(body)
    except RecipeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not db.update_recipe(state.settings.db_path, recipe_id,
                            recipe["name"], json.dumps(body)):
        raise HTTPException(status_code=404, detail="recipe not found")
    db.audit(state.settings.db_path, "recipe_update", state.username, recipe["name"])
    return {"ok": True}


@router.delete("/recipes/{recipe_id}")
async def delete_recipe(request: Request, recipe_id: int):
    state = _state(request)
    state.require_csrf(request)
    if not db.delete_recipe(state.settings.db_path, recipe_id):
        raise HTTPException(status_code=404, detail="recipe not found")
    db.audit(state.settings.db_path, "recipe_delete", state.username, str(recipe_id))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Preview / execute
# ---------------------------------------------------------------------------

@router.post("/recipes/{recipe_id}/preview")
async def preview_recipe(request: Request, recipe_id: int):
    """Read-only plan: sources resolved, UTXOs selected, fees computed,
    batches laid out. Signs nothing, broadcasts nothing."""
    state = _state(request)
    state.require_2fa(request)
    recipe = _load_recipe(state, recipe_id)
    try:
        plan = await state.engine.preview(
            recipe_id, recipe, state.settings.session_secret)
    except (RPCError, RPCNotAllowed, RPCUnavailable, RecipeError) as exc:
        raise _translate(exc)
    return plan


@router.post("/recipes/{recipe_id}/execute")
async def execute_recipe(request: Request, recipe_id: int):
    """Execute a previously previewed plan. Gated by session + 2FA + CSRF
    + wallet unlock. The confirm token from the preview is verified against
    a FRESH plan, so it can never execute a stale UTXO set. Every batch is
    signed, testmempoolaccept-checked, then broadcast; the run aborts on the
    first rejection. All steps are audit-logged."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    recipe = _load_recipe(state, recipe_id)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    token = str(body.get("confirm_token") or "")
    if not token:
        raise HTTPException(status_code=400, detail="confirm_token required")

    try:
        # Re-derive the plan NOW and bind the token to it.
        plan = await state.engine.plan(recipe_id, recipe)
        expected = confirm_token(recipe_id, plan["_plan_key"],
                                 state.settings.session_secret)
        if not secrets.compare_digest(token, expected):
            db.audit(state.settings.db_path, "batch_execute_denied", sess.username,
                     detail="confirm token mismatch (plan changed or stale)", success=False)
            raise HTTPException(status_code=409,
                                 detail="plan changed since preview — run preview again")

        destination = plan["destination"]
        results = []
        for idx, chunk in enumerate(plan["_chunks"]):
            bmeta = plan["batches"][idx]
            if bmeta["below_min_output"]:
                results.append({"batch": idx, "skipped": True,
                                "reason": "below min_output"})
                continue
            inputs = [{"txid": u["txid"], "vout": u["vout"]} for u in chunk]
            outputs = {destination: bmeta["output"]}
            raw = await state.rpc.call("createrawtransaction", inputs, outputs)
            signed = await state.rpc.call("signrawtransactionwithwallet", raw)
            if not signed.get("complete"):
                raise HTTPException(422, "signing incomplete — wallet may be locked")
            tx_hex = signed["hex"]
            accepted = await state.rpc.call("testmempoolaccept", [tx_hex])
            if not (accepted and accepted[0].get("allowed")):
                reason = accepted[0].get("reject-reason") if accepted else "rejected"
                db.audit(state.settings.db_path, "batch_tx_denied", sess.username,
                         detail=f"batch={idx} reason={reason}", success=False)
                raise HTTPException(422, detail=f"batch {idx} rejected: {reason}")
            txid = await state.rpc.call("sendrawtransaction", tx_hex)
            results.append({"batch": idx, "txid": txid,
                            "output": bmeta["output"], "fee": bmeta["fee"]})
    except HTTPException:
        raise
    except (RPCError, RPCNotAllowed, RPCUnavailable, RecipeError) as exc:
        db.audit(state.settings.db_path, "batch_execute_error", sess.username,
                 detail=str(exc), success=False)
        raise _translate(exc)

    db.audit(state.settings.db_path, "batch_execute", sess.username,
             detail=f"recipe={recipe['name']} batches={len(results)}")
    return {"ok": True, "results": results}
