"""/api/chain/node-state: the UI must be able to tell WHY the node is not
answering (warming up, never started, RPC not listening yet, external node
unreachable, bad credentials) instead of calling everything "starting"."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.rpc import RPCError, RPCUnavailable
from tests.conftest import LOCAL, MockRPC, login


def _fail_with(mock_rpc: MockRPC, exc: Exception) -> None:
    orig = mock_rpc.call

    async def call(method, *params):
        if method == "getblockchaininfo":
            raise exc
        return await orig(method, *params)

    mock_rpc.call = call


def _get(client: TestClient) -> dict:
    out = login(client, headers=LOCAL)
    r = client.get("/api/chain/node-state", headers=out["headers"])
    assert r.status_code == 200
    return r.json()


@pytest.fixture()
def daemon_installed(settings):
    """A managed install with the daemon binary present."""
    d = Path(settings.daemon_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "b3coind").write_text("")
    return settings


def test_running(client: TestClient):
    assert _get(client) == {"state": "running", "detail": ""}


def test_warming_up_reports_the_nodes_own_words(client, mock_rpc):
    _fail_with(mock_rpc, RPCError(-28, "Loading block index..."))
    assert _get(client) == {"state": "warming_up", "detail": "Loading block index..."}


def test_bad_credentials(client, mock_rpc):
    _fail_with(mock_rpc, RPCError(-1, "node RPC rejected credentials"))
    assert _get(client)["state"] == "auth_error"


def test_other_node_error(client, mock_rpc):
    _fail_with(mock_rpc, RPCError(-5, "something odd"))
    assert _get(client) == {"state": "error", "detail": "something odd"}


def test_never_started_is_not_called_starting(client, mock_rpc, daemon_installed):
    # The conftest settings model a fresh install: the deferred marker exists.
    assert Path(daemon_installed.daemon_deferred_file).is_file()
    _fail_with(mock_rpc, RPCUnavailable("refused"))
    assert _get(client)["state"] == "not_started"


def test_started_but_rpc_not_listening_yet(client, mock_rpc, daemon_installed):
    Path(daemon_installed.daemon_deferred_file).unlink()
    _fail_with(mock_rpc, RPCUnavailable("refused"))
    assert _get(client)["state"] == "starting"


def test_binary_missing(client, mock_rpc):
    _fail_with(mock_rpc, RPCUnavailable("refused"))
    assert _get(client)["state"] == "binary_missing"


def test_external_node_unreachable(client, mock_rpc, settings):
    settings.daemon_mode = "external"
    settings.ext_rpc_host, settings.ext_rpc_port = "10.0.0.5", 32647
    _fail_with(mock_rpc, RPCUnavailable("refused"))
    assert _get(client) == {"state": "unreachable", "detail": "10.0.0.5:32647"}


def test_requires_session(client: TestClient):
    assert client.get("/api/chain/node-state").status_code in (401, 403)
