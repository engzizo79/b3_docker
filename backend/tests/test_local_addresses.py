"""What counts as "the local machine": loopback only, plus addresses the
operator explicitly lists in B3_LOCAL_ADDRS. Nothing is auto-detected — in
particular the Docker bridge gateway is NOT local unless opted in."""

import pytest

from app.session import client_is_localhost, is_local_address


class _Req:
    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("B3_LOCAL_ADDRS", raising=False)
    monkeypatch.setenv("B3_TRUSTED_PROXIES", "")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_is_local(host):
    assert is_local_address(host)


@pytest.mark.parametrize("host", [
    "172.17.0.1",      # default Docker bridge gateway
    "172.18.0.1",      # compose project network gateway
    "192.168.1.20", "10.0.0.5", "203.0.113.7", "127.0.0.2", "", "garbage",
])
def test_everything_else_is_remote_by_default(host):
    assert not is_local_address(host)


def test_explicit_env_opts_an_address_in(monkeypatch):
    monkeypatch.setenv("B3_LOCAL_ADDRS", "172.17.0.1")
    assert is_local_address("172.17.0.1")
    assert not is_local_address("172.17.0.2")


def test_env_accepts_lists_and_cidrs(monkeypatch):
    monkeypatch.setenv("B3_LOCAL_ADDRS", " 172.17.0.1 , 192.168.50.0/24 ,")
    assert is_local_address("172.17.0.1")
    assert is_local_address("192.168.50.9")
    assert not is_local_address("192.168.51.9")


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "10.0.0.0/8", "::/0", "not-an-ip"])
def test_overly_broad_or_invalid_entries_are_ignored(monkeypatch, bad):
    monkeypatch.setenv("B3_LOCAL_ADDRS", bad)
    assert not is_local_address("10.1.2.3")
    assert not is_local_address("203.0.113.7")
    assert is_local_address("127.0.0.1")  # loopback unaffected


def test_docker_gateway_peer_is_remote_by_default():
    assert not client_is_localhost(_Req("172.17.0.1"))


def test_docker_gateway_peer_is_local_when_opted_in(monkeypatch):
    monkeypatch.setenv("B3_LOCAL_ADDRS", "172.17.0.1")
    assert client_is_localhost(_Req("172.17.0.1"))


def test_spoofed_xff_from_remote_peer_is_ignored():
    req = _Req("203.0.113.7", {"x-forwarded-for": "127.0.0.1"})
    assert not client_is_localhost(req)


def test_opted_in_address_is_not_a_trusted_proxy(monkeypatch):
    # B3_LOCAL_ADDRS grants "local", not permission to assert other
    # clients' addresses via X-Forwarded-For.
    monkeypatch.setenv("B3_LOCAL_ADDRS", "172.17.0.1")
    req = _Req("172.17.0.1", {"x-forwarded-for": "203.0.113.7"})
    assert client_is_localhost(req)  # peer itself is local; XFF not believed


def test_loopback_proxy_forwarding_remote_client_is_remote():
    # e.g. tailscale serve / a local reverse proxy that sets XFF
    req = _Req("127.0.0.1", {"x-forwarded-for": "100.64.0.9"})
    assert not client_is_localhost(req)
