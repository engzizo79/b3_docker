"""FN Coin / FlowMesh asset tests: overview, markets, validator gating,
audit entries, allowlist. All against the mocked RPC layer."""

from fastapi.testclient import TestClient

from app import db
from tests.conftest import LOCAL, login, unlock

FN_ASSET_ID = "cd" * 32


def _mock(client: TestClient):
    return client.app.state.app_state.rpc


def _seed_assets(mock) -> None:
    mock.responses["getwalletassets"] = {
        "assets": [
            {
                "asset_id": FN_ASSET_ID, "kind": "fn", "ticker": "FN",
                "name": "FN Coin", "decimals": 0, "confirmed": 1200,
                "unconfirmed": 0, "spendable": 1200, "utxos": [],
            },
            {
                "asset_id": "ab" * 32, "kind": "colored", "ticker": "bUSD",
                "name": "Bridge USD", "decimals": 6, "confirmed": 5000000,
                "unconfirmed": 0, "spendable": 5000000, "utxos": [],
            },
        ],
    }
    mock.responses["getassetstate"] = {
        "next_height": 825000,
        "fn": {
            "configured": True, "active": True, "asset_id": FN_ASSET_ID,
            "historical_issued": 1000, "pod_active": True,
            "counter_known": True, "modern_issued": 200,
            "modern_capacity": 5000,
        },
        "colored": {
            "configured": True, "active": True,
            "issuance_fee": "50.000000000",
        },
    }


def test_overview_requires_auth(client: TestClient):
    r = client.get("/api/assets", headers=LOCAL)
    assert r.status_code == 401


def test_overview_parses_assets_and_fn(client: TestClient):
    _seed_assets(_mock(client))
    out = login(client)
    r = client.get("/api/assets", headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["assets"]) == 2
    fn = next(a for a in d["assets"] if a["kind"] == "fn")
    assert fn["ticker"] == "FN"
    assert fn["confirmed"] == 1200
    assert fn["decimals"] == 0
    busd = next(a for a in d["assets"] if a["kind"] == "colored")
    assert busd["decimals"] == 6
    assert d["fn"]["active"] is True
    assert d["fn"]["modern_issued"] == 200
    assert d["fn"]["modern_capacity"] == 5000


def test_overview_null_wallet_becomes_empty(client: TestClient):
    mock = _mock(client)
    mock.responses["getwalletassets"] = None
    mock.responses["getassetstate"] = {}
    out = login(client)
    r = client.get("/api/assets", headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["assets"] == []


def test_markets_list(client: TestClient):
    mock = _mock(client)
    mock.responses["listflowmeshmarkets"] = [
        {"market_id": "ef" * 32, "asset_id": FN_ASSET_ID, "state": "open"},
    ]
    out = login(client)
    r = client.get("/api/assets/markets", headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["markets"]) == 1
    assert d["markets"][0]["market_id"] == "ef" * 32


def test_validator_start_requires_unlock(client: TestClient):
    out = login(client)
    r = client.post("/api/assets/validator/start", headers=out["headers"])
    assert r.status_code == 423, r.text


def test_validator_start_stop_audited(client: TestClient):
    mock = _mock(client)
    mock.responses["startflowmeshvalidator"] = {"started": True}
    mock.responses["stopflowmeshvalidator"] = {"stopped": True}
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/assets/validator/start", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True}
    r = client.post("/api/assets/validator/stop", headers=out["headers"])
    assert r.status_code == 200, r.text
    dbp = client.app.state.app_state.settings.db_path
    actions = [a["action"] for a in db.audit_list(dbp)]
    assert "flowmesh_validator_start" in actions
    assert "flowmesh_validator_stop" in actions


def test_allowlist_assets_categories(client: TestClient):
    from app.rpc import ALLOWED, ALLOWED_METHODS
    assert "getwalletassets" in ALLOWED["assets_read"]
    assert "listflowmeshmarkets" in ALLOWED["assets_read"]
    assert "getflowmeshmarketdata" in ALLOWED["assets_read"]
    assert "startflowmeshvalidator" in ALLOWED["assets_write"]
    assert "stopflowmeshvalidator" in ALLOWED["assets_write"]
    # issuance/burn RPCs must NOT be allowed yet (not exposed by this UI)
    for m in ("issueasset", "sendasset", "burnasset", "createfncoin"):
        assert m not in ALLOWED_METHODS, m
