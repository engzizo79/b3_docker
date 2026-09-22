"""B3RPCClient's per-wallet URL routing (POST /wallet/<name> instead of
POST /) — plumbing laid down ahead of any decision to build per-user
wallet isolation. wallet=None (every real call site today) must be
byte-for-byte the same request as before this existed."""

import asyncio

from app.rpc import B3RPCClient


def _fake_post(monkeypatch, result=None):
    """Capture every URL a call() posted to, without a real network call."""
    import app.rpc as rpc_mod

    seen = []

    async def fake_post(self, url, json=None, **kw):
        seen.append(url)

        class R:
            status_code = 200

            def json(self_r):
                return {"result": result}
        return R()

    monkeypatch.setattr(rpc_mod.httpx.AsyncClient, "post", fake_post)
    return seen


def test_no_wallet_posts_to_base_url(monkeypatch):
    seen = _fake_post(monkeypatch, result={"blocks": 1})
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")
    asyncio.run(client.call("getblockchaininfo"))
    assert seen == ["http://127.0.0.1:32647/"]


def test_wallet_kwarg_routes_to_wallet_path(monkeypatch):
    seen = _fake_post(monkeypatch, result=1.5)
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")
    asyncio.run(client.call("getbalance", wallet="alice"))
    assert seen == ["http://127.0.0.1:32647/wallet/alice"]


def test_wallet_name_is_url_encoded(monkeypatch):
    seen = _fake_post(monkeypatch, result=1.5)
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")
    asyncio.run(client.call("getbalance", wallet="kids/allowance jar"))
    assert seen == ["http://127.0.0.1:32647/wallet/kids%2Fallowance%20jar"]


def test_call_unrestricted_respects_wallet(monkeypatch):
    seen = _fake_post(monkeypatch, result="ok")
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")
    asyncio.run(client.call_unrestricted("backupwallet", "/tmp/b.dat", wallet="bob"))
    assert seen == ["http://127.0.0.1:32647/wallet/bob"]


def test_call_optional_passes_wallet_through(monkeypatch):
    seen = _fake_post(monkeypatch, result={"active": "5"})
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")
    result = asyncio.run(client.call_optional("getstakinginfo", wallet="carol"))
    assert seen == ["http://127.0.0.1:32647/wallet/carol"]
    assert result == {"active": "5"}


def test_call_optional_unknown_method_never_posts(monkeypatch):
    seen = _fake_post(monkeypatch)
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")
    result = asyncio.run(client.call_optional("notarealmethod", wallet="carol"))
    assert result is None
    assert seen == []


def test_same_client_serves_multiple_wallets(monkeypatch):
    """One client, different wallets per call — no shared 'current wallet'
    state to get out of sync between requests."""
    seen = _fake_post(monkeypatch, result=1)
    client = B3RPCClient("127.0.0.1", 32647, "u", "p")

    async def run():
        await client.call("getbalance", wallet="alice")
        await client.call("getbalance", wallet="bob")
        await client.call("getblockchaininfo")  # no wallet -> base URL

    asyncio.run(run())
    assert seen == [
        "http://127.0.0.1:32647/wallet/alice",
        "http://127.0.0.1:32647/wallet/bob",
        "http://127.0.0.1:32647/",
    ]
