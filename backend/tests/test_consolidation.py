"""Consolidation sweep (S2) tests: settings, plan/preview, execute with
token binding, restake, auth gates. All against the mocked RPC layer -
no funded wallet needed. The mock listunspent returns 2 valid plain-P2PKH
UTXOs, so a plan yields 1 batch with 2 inputs."""

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, login, unlock

DEST = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"  # valid B3 P2PKH from mock listunspent


def test_consolidation_settings_require_auth(client: TestClient):
    r = client.get("/api/staking/consolidation/settings", headers=LOCAL)
    assert r.status_code == 401


def test_consolidation_settings_defaults(client: TestClient):
    out = login(client)
    r = client.get("/api/staking/consolidation/settings",
                   headers=out["headers"])
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is False
    assert d["destination"] == ""
    assert d["interval_minutes"] == 1440
    assert d["restake_after"] is False


def test_consolidation_update_settings(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/consolidation/settings",
                    json={"enabled": True, "destination": DEST,
                          "interval_minutes": 720, "restake_after": True,
                          "inputs_per_tx": 100, "max_batches": 10},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enabled"] is True
    assert d["destination"] == DEST
    assert d["interval_minutes"] == 720
    assert d["restake_after"] is True
    assert d["inputs_per_tx"] == 100


def test_consolidation_update_rejects_bad_address(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/consolidation/settings",
                    json={"destination": "1BadAddress"},
                    headers=out["headers"])
    assert r.status_code == 400


def test_consolidation_update_rejects_bad_inputs(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/consolidation/settings",
                    json={"destination": DEST, "inputs_per_tx": 0},
                    headers=out["headers"])
    assert r.status_code == 400


def test_consolidation_preview_requires_unlock(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST}, headers=out["headers"])
    r = client.post("/api/staking/consolidation/preview",
                    headers=out["headers"])
    assert r.status_code == 423  # wallet locked, not 401


def test_consolidation_preview_no_destination(client: TestClient):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/consolidation/preview",
                    headers=out["headers"])
    assert r.status_code == 400
    assert "destination" in r.json()["detail"]


def test_consolidation_preview(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST}, headers=out["headers"])
    unlock(client, out["headers"])
    r = client.post("/api/staking/consolidation/preview",
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["destination"] == DEST
    assert d["eligible_utxos"] == 2  # mock has 2 P2PKH UTXOs
    assert len(d["batches"]) == 1  # 2 inputs fit in 1 batch (per=50)
    assert d["batches"][0]["inputs"] == 2
    assert "confirm_token" in d
    assert d["confirm_token"]  # non-empty HMAC
    # Internal fields must be stripped
    assert "_chunks" not in d
    assert "_plan_key" not in d
    assert "_rate" not in d


def test_consolidation_execute_requires_token(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST}, headers=out["headers"])
    unlock(client, out["headers"])
    r = client.post("/api/staking/consolidation/execute",
                    json={}, headers=out["headers"])
    assert r.status_code == 400


def test_consolidation_execute_stale_token(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST}, headers=out["headers"])
    unlock(client, out["headers"])
    r = client.post("/api/staking/consolidation/execute",
                    json={"confirm_token": "stale-fake-token"},
                    headers=out["headers"])
    assert r.status_code == 422
    assert "plan changed" in r.json()["detail"]


def test_consolidation_execute(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST}, headers=out["headers"])
    unlock(client, out["headers"])
    # Preview to get the token
    p = client.post("/api/staking/consolidation/preview",
                    headers=out["headers"])
    assert p.status_code == 200
    token = p.json()["confirm_token"]
    # Execute with the valid token
    r = client.post("/api/staking/consolidation/execute",
                    json={"confirm_token": token},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert len(d["results"]) == 1
    assert d["results"][0]["txid"]  # broadcast returned a txid


def test_consolidation_execute_restake(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST, "restake_after": True},
                headers=out["headers"])
    unlock(client, out["headers"])
    p = client.post("/api/staking/consolidation/preview",
                    headers=out["headers"])
    assert p.status_code == 200
    assert p.json()["restake_after"] is True
    token = p.json()["confirm_token"]
    r = client.post("/api/staking/consolidation/execute",
                    json={"confirm_token": token},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    # The mock RPC should have received a createstake call
    rpc = client.app.state.app_state.rpc
    assert rpc.called("createstake")


def test_consolidation_execute_no_restake_by_default(client: TestClient):
    out = login(client)
    client.post("/api/staking/consolidation/settings",
                json={"destination": DEST}, headers=out["headers"])
    unlock(client, out["headers"])
    p = client.post("/api/staking/consolidation/preview",
                    headers=out["headers"])
    token = p.json()["confirm_token"]
    client.post("/api/staking/consolidation/execute",
               json={"confirm_token": token}, headers=out["headers"])
    rpc = client.app.state.app_state.rpc
    assert not rpc.called("createstake")


def test_consolidation_settings_round_trip(client: TestClient):
    """GET defaults must POST back cleanly (min_utxo_value=0 is legal)."""
    out = login(client)
    d = client.get("/api/staking/consolidation/settings",
                   headers=out["headers"]).json()
    r = client.post("/api/staking/consolidation/settings", json=d,
                    headers=out["headers"])
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Scheduled sweep lock handling (unit-level, direct calls against the mock)
# ---------------------------------------------------------------------------

import asyncio
import time

from app import db
from app.consolidation import _run_once
from app.vault import vault_from_settings


def _enable_sweep(settings, destination=DEST, passphrase="test-passphrase"):
    vault = vault_from_settings(settings, settings.b3_data_dir)
    db.set_consolidation_settings(settings.db_path, enabled=1, destination=destination)
    db.set_staking_settings(settings.db_path, passphrase_enc=vault.encrypt(passphrase))
    return vault


def test_scheduled_sweep_does_not_relock_an_already_unlocked_wallet(client, mock_rpc, settings):
    """Regression: same class of bug as autostake.reconcile - the node has
    ONE global wallet lock. A scheduled sweep must not relock a wallet a
    user (or another pass) already has open, or their next signing call
    fails right after they entered their passphrase."""
    vault = _enable_sweep(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getwalletinfo"] = {"unlocked_until": time.time() + 55}
    result = asyncio.run(_run_once(settings, mock_rpc, vault, reason="scheduled"))
    assert result["ran"] is True
    assert not mock_rpc.called("walletlock")
    assert not mock_rpc.called("walletpassphrase")


def test_scheduled_sweep_still_relocks_when_it_took_the_lock_itself(client, mock_rpc, settings):
    vault = _enable_sweep(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getwalletinfo"] = {"unlocked_until": 0}
    result = asyncio.run(_run_once(settings, mock_rpc, vault, reason="scheduled"))
    assert result["ran"] is True
    assert mock_rpc.called("walletpassphrase")
    assert mock_rpc.called("walletlock")


def test_scheduled_sweep_dry_run_never_unlocks(client, mock_rpc, settings):
    """dry_run only plans (read-only); it must never touch the real
    passphrase or the node's lock state."""
    vault = _enable_sweep(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    result = asyncio.run(_run_once(settings, mock_rpc, vault, reason="manual-dry-run", dry_run=True))
    assert result["ran"] is True
    assert not mock_rpc.called("walletpassphrase")
    assert not mock_rpc.called("walletlock")
