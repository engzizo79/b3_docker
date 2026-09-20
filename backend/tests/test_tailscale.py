"""Tailscale router tests (v0.7.0).

The CLI layer (_ts) is mocked: these tests cover the HTTP contract —
auth/CSRF gating, authkey validation, key redaction in audit output,
and status parsing. No real tailscale install is required.
"""

import json
from pathlib import Path

import pytest

from app.routers import tailscale as tsr
from tests.conftest import login

LOCAL = {"x-forwarded-for": "127.0.0.1"}


class FakeTS:
    """Scripted _ts replacement."""

    def __init__(self):
        self.calls = []
        self.rc = 0
        self.out = '{}'

    async def __call__(self, *args, timeout=45.0):
        self.calls.append(args)
        return self.rc, self.out


@pytest.fixture
def fake_ts(monkeypatch):
    f = FakeTS()
    monkeypatch.setattr(tsr, "_ts", f)
    return f


class TestTailscale:
    def test_status_requires_auth(self, client, fake_ts):
        r = client.get("/api/tailscale/status")
        assert r.status_code == 401

    def test_status_joined(self, client, fake_ts):
        out = login(client)
        fake_ts.out = json.dumps({
            "BackendState": "Running",
            "CurrentTailnet": {"Name": "tail-scale.ts.net"},
            "Self": {"DNSName": "b3hive.tail-scale.ts.net."},
        })
        r = client.get("/api/tailscale/status", headers=out["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["joined"] is True
        assert body["https_url"] == "https://b3hive.tail-scale.ts.net"
        assert body["tailnet"] == "tail-scale.ts.net"

    def test_status_not_joined(self, client, fake_ts):
        out = login(client)
        fake_ts.out = json.dumps({"BackendState": "NeedsLogin"})
        r = client.get("/api/tailscale/status", headers=out["headers"])
        assert r.status_code == 200
        assert r.json()["joined"] is False
        assert r.json()["https_url"] is None

    def test_status_daemon_down(self, client, fake_ts):
        out = login(client)
        fake_ts.rc = 1
        fake_ts.out = ""
        r = client.get("/api/tailscale/status", headers=out["headers"])
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["joined"] is False
        assert "reachable" in body["detail"]

    def test_join_requires_csrf(self, client, fake_ts):
        out = login(client)
        h = dict(out["headers"])
        h.pop("x-csrf-token")
        r = client.post("/api/tailscale/join",
                        json={"authkey": "tskey-abc123"}, headers=h)
        assert r.status_code == 403

    def test_join_rejects_non_tskey(self, client, fake_ts):
        out = login(client)
        r = client.post("/api/tailscale/join",
                        json={"authkey": "not-a-key"},
                        headers=out["headers"])
        assert r.status_code == 422

    def test_join_success_never_echoes_key(self, client, fake_ts):
        out = login(client)
        r = client.post("/api/tailscale/join",
                        json={"authkey": "tskey-abc123"},
                        headers=out["headers"])
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # the CLI received the key exactly once...
        assert fake_ts.calls[0][0] == "up"
        assert "tskey-abc123" in fake_ts.calls[0]
        # ...and the response body never contains it
        assert "tskey" not in r.text

    def test_join_failure_redacts_key_in_audit(self, client, fake_ts,
                                               tmp_path):
        out = login(client)
        fake_ts.rc = 1
        fake_ts.out = "boom tskey-abc123 leaked"
        r = client.post("/api/tailscale/join",
                        json={"authkey": "tskey-abc123"},
                        headers=out["headers"])
        assert r.status_code == 409
        st = client.app.state.app_state
        audit_path = Path(st.settings.db_path)
        rows = audit_path.read_text(errors="replace")
        assert "tskey-abc123" not in rows
        assert "tskey-[REDACTED" in rows or "REDACTED" in rows or rows == ""

    def test_serve_enable_disable(self, client, fake_ts):
        out = login(client)
        r = client.post("/api/tailscale/serve", json={"enable": True},
                        headers=out["headers"])
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert fake_ts.calls[-1][0] == "serve"
        r = client.post("/api/tailscale/serve", json={"enable": False},
                        headers=out["headers"])
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_leave(self, client, fake_ts):
        out = login(client)
        r = client.post("/api/tailscale/leave", headers=out["headers"])
        assert r.status_code == 200
        assert fake_ts.calls[-1][0] == "logout"