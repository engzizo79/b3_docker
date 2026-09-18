"""Auth tests: login, session, CSRF, TOTP (localhost bypass + remote
requirement), recovery codes, rate limiting."""

import pyotp
from fastapi.testclient import TestClient

from tests.conftest import LOCAL, REMOTE, MockRPC, login


def test_health_no_auth(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["storage_blocked"] is False


def test_login_wrong_password(client: TestClient):
    r = client.post("/api/auth/login", json={"password": "nope"},
                    headers=LOCAL)
    assert r.status_code == 401


def test_unauthenticated_chain_blocked(client: TestClient):
    r = client.get("/api/chain/summary", headers=LOCAL)
    assert r.status_code == 401


def test_login_local_localhost_bypass(client: TestClient):
    out = login(client, headers=LOCAL)
    assert out["body"]["totp_required"] is False
    r = client.get("/api/chain/summary", headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["blockchain"]["blocks"] == 822000


def test_login_remote_requires_totp(client: TestClient):
    out = login(client, headers=REMOTE)
    assert out["body"]["totp_required"] is False  # TOTP not configured yet
    r = client.get("/api/chain/summary", headers=out["headers"])
    assert r.status_code == 200  # remote + no TOTP configured = allowed


def test_totp_flow_remote(client: TestClient, app_state, mock_rpc: MockRPC):
    # Configure TOTP from localhost.
    local = login(client, headers=LOCAL)
    r = client.post("/api/auth/totp/setup", headers=local["headers"])
    assert r.status_code == 200
    data = r.json()
    assert data["recovery_codes"] and len(data["recovery_codes"]) == 8

    # Login from remote now requires TOTP.
    out = login(client, headers=REMOTE)
    assert out["body"]["totp_required"] is True
    r = client.get("/api/chain/summary", headers=out["headers"])
    assert r.status_code == 403  # blocked until 2FA

    # Extract secret from the provisioning URI and compute a valid code.
    uri = data["provisioning_uri"]
    secret = uri.split("secret=")[1].split("&")[0]
    code = pyotp.TOTP(secret).now()
    r = client.post("/api/auth/2fa", json={"code": code},
                    headers=out["headers"])
    assert r.status_code == 200

    # Now the remote session can access the dashboard.
    r = client.get("/api/chain/summary", headers=out["headers"])
    assert r.status_code == 200


def test_totp_replay_rejected(client: TestClient):
    local = login(client, headers=LOCAL)
    r = client.post("/api/auth/totp/setup", headers=local["headers"])
    secret = r.json()["provisioning_uri"].split("secret=")[1].split("&")[0]
    code = pyotp.TOTP(secret).now()

    out = login(client, headers=REMOTE)
    assert out["body"]["totp_required"] is True
    r = client.post("/api/auth/2fa", json={"code": code},
                    headers=out["headers"])
    assert r.status_code == 200

    # Fresh remote session, same code within the window -> replay rejected.
    out2 = login(client, headers=REMOTE)
    r = client.post("/api/auth/2fa", json={"code": code},
                    headers=out2["headers"])
    assert r.status_code == 401


def test_recovery_code_single_use(client: TestClient):
    local = login(client, headers=LOCAL)
    r = client.post("/api/auth/totp/setup", headers=local["headers"])
    code = r.json()["recovery_codes"][0]

    out = login(client, headers=REMOTE)
    r = client.post("/api/auth/2fa", json={"code": code},
                    headers=out["headers"])
    assert r.status_code == 200

    # Same recovery code cannot be used twice.
    out2 = login(client, headers=REMOTE)
    r = client.post("/api/auth/2fa", json={"code": code},
                    headers=out2["headers"])
    assert r.status_code == 401


def test_csrf_required_on_mutation(client: TestClient):
    out = login(client, headers=LOCAL)
    # Drop the CSRF header -> 403.
    hdr = dict(out["headers"])
    hdr.pop("x-csrf-token")
    r = client.post("/api/wallet/lock", headers=hdr)
    assert r.status_code == 403


def test_csrf_wrong_token_rejected(client: TestClient):
    out = login(client, headers=LOCAL)
    hdr = dict(out["headers"])
    hdr["x-csrf-token"] = "forged-token"
    r = client.post("/api/wallet/lock", headers=hdr)
    assert r.status_code == 403


def test_login_rate_limit(client: TestClient):
    for _ in range(5):
        r = client.post("/api/auth/login", json={"password": "nope"},
                        headers=LOCAL)
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"password": "nope"},
                    headers=LOCAL)
    assert r.status_code == 429


def test_logout_destroys_session(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.post("/api/auth/logout", headers=out["headers"])
    assert r.status_code == 200
    r = client.get("/api/chain/summary", headers=out["headers"])
    assert r.status_code == 401
