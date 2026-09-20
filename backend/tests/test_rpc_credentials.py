"""v0.6.5: b3coin.conf is the RPC credential source of truth in managed mode.

Regression tests for the 'incorrect password attempt' flood: if env vars
disagree with the conf (fragile entrypoint grep, migration, regenerated
conf), the conf must WIN. Also covers cookie-auth fallback daemons.
"""
import os

import pytest


def _make_conf(tmp_path, lines):
    nd = tmp_path / "node"
    nd.mkdir(exist_ok=True)
    (nd / "b3coin.conf").write_text("\n".join(lines) + "\n")
    return str(tmp_path)


def test_conf_overrides_wrong_env_vars(tmp_path, monkeypatch):
    """Env vars disagree with the conf -> conf wins (the flood scenario)."""
    data = _make_conf(tmp_path, [
        "rpcbind=127.0.0.1",
        "rpcport=32647",
        "rpcuser=realuser",
        "rpcpassword=realpass",
    ])
    monkeypatch.setenv("B3_DATA_DIR", data)
    monkeypatch.setenv("B3_DAEMON_MODE", "managed")
    # entrypoint passed WRONG credentials via env (the bug scenario)
    monkeypatch.setenv("B3_RPC_USER", "wronguser")
    monkeypatch.setenv("B3_RPC_PASSWORD", "wrongpass")
    from app.config import Settings
    s = Settings()
    assert s.rpc_user == "realuser"
    assert s.rpc_password == "realpass"
    assert s.rpc_port == 32647
    assert s.rpc_host == "127.0.0.1"


def test_conf_fills_empty_env_vars(tmp_path, monkeypatch):
    data = _make_conf(tmp_path, [
        "rpcport=32647",
        "rpcuser=confuser",
        "rpcpassword=confpass",
    ])
    monkeypatch.setenv("B3_DATA_DIR", data)
    monkeypatch.setenv("B3_DAEMON_MODE", "managed")
    monkeypatch.delenv("B3_RPC_USER", raising=False)
    monkeypatch.delenv("B3_RPC_PASSWORD", raising=False)
    from app.config import Settings
    s = Settings()
    assert s.rpc_user == "confuser"
    assert s.rpc_password == "confpass"


def test_cookie_auth_fallback(tmp_path, monkeypatch):
    """Conf without rpcuser/rpcpassword -> daemon uses .cookie auth; the
    backend must use the cookie instead of Basic user/pass (which would
    produce the incorrect-password flood)."""
    data = _make_conf(tmp_path, [
        "rpcbind=127.0.0.1",
        "rpcport=32647",
        "# no rpcuser / rpcpassword - cookie auth",
    ])
    (tmp_path / "node" / ".cookie").write_text("__cookie__:S3cr3tValue")
    monkeypatch.setenv("B3_DATA_DIR", data)
    monkeypatch.setenv("B3_DAEMON_MODE", "managed")
    monkeypatch.delenv("B3_RPC_USER", raising=False)
    monkeypatch.delenv("B3_RPC_PASSWORD", raising=False)
    from app.config import Settings
    s = Settings()
    assert s.rpc_user == "__cookie__"
    assert s.rpc_password == "S3cr3tValue"


def test_external_mode_never_reads_conf(tmp_path, monkeypatch):
    """External mode uses EXT_RPC_* env vars; the conf must not override."""
    data = _make_conf(tmp_path, [
        "rpcuser=shouldnotbeused",
        "rpcpassword=nope",
    ])
    monkeypatch.setenv("B3_DATA_DIR", data)
    monkeypatch.setenv("B3_DAEMON_MODE", "external")
    monkeypatch.setenv("EXT_RPC_HOST", "10.0.0.5")
    monkeypatch.setenv("EXT_RPC_PORT", "38647")
    monkeypatch.setenv("EXT_RPC_USER", "extuser")
    monkeypatch.setenv("EXT_RPC_PASSWORD", "extpass")
    from app.config import Settings
    s = Settings()
    assert s.rpc_user != "shouldnotbeused"
    assert s.ext_rpc_user == "extuser"
    assert s.ext_rpc_host == "10.0.0.5"


def test_no_conf_keeps_env_vars(tmp_path, monkeypatch):
    """No conf at all (fresh pre-wizard boot): env vars stay as-is."""
    monkeypatch.setenv("B3_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("B3_DAEMON_MODE", "managed")
    monkeypatch.setenv("B3_RPC_USER", "envuser")
    monkeypatch.setenv("B3_RPC_PASSWORD", "envpass")
    from app.config import Settings
    s = Settings()
    assert s.rpc_user == "envuser"
    assert s.rpc_password == "envpass"
