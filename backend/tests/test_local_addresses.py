"""What counts as "the local machine": loopback, the Docker host (the
container's default gateway), and addresses listed in B3_LOCAL_ADDRS.
LAN/remote clients never do. B3_TRUST_DOCKER_HOST=false turns the Docker
host rule off."""

import pytest

from app import session
from app.session import client_is_localhost, is_local_address

GATEWAY = "172.17.0.1"

# A container whose default route goes via 172.17.0.1 (0100 11AC little-endian)
ROUTE = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
         "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\n"
         "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n")


class _Req:
    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("B3_LOCAL_ADDRS", raising=False)
    monkeypatch.delenv("B3_TRUST_DOCKER_HOST", raising=False)
    monkeypatch.setenv("B3_TRUSTED_PROXIES", "")
    monkeypatch.setattr(session, "_default_gateway_addrs",
                        session._default_gateway_addrs)  # real impl, fake file below
    route = tmp_path / "route"
    route.write_text(ROUTE)
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k:
                        real_open(route if f == "/proc/net/route" else f, *a, **k))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", GATEWAY])
def test_loopback_and_docker_host_are_local(host):
    assert is_local_address(host)


@pytest.mark.parametrize("host", [
    "172.17.0.2", "172.18.0.1", "192.168.1.20", "10.0.0.5", "203.0.113.7",
    "127.0.0.2", "", "garbage",
])
def test_everything_else_is_remote(host):
    assert not is_local_address(host)


def test_docker_host_rule_can_be_disabled(monkeypatch):
    monkeypatch.setenv("B3_TRUST_DOCKER_HOST", "false")
    assert not is_local_address(GATEWAY)
    assert is_local_address("127.0.0.1")


def test_explicit_env_opts_more_addresses_in(monkeypatch):
    monkeypatch.setenv("B3_LOCAL_ADDRS", " 192.168.50.0/24 , 172.18.0.1 ,")
    assert is_local_address("192.168.50.9") and is_local_address("172.18.0.1")
    assert not is_local_address("192.168.51.9")


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "10.0.0.0/8", "::/0", "not-an-ip"])
def test_overly_broad_or_invalid_entries_are_ignored(monkeypatch, bad):
    monkeypatch.setenv("B3_LOCAL_ADDRS", bad)
    assert not is_local_address("10.1.2.3")
    assert not is_local_address("203.0.113.7")


def test_spoofed_xff_from_remote_peer_is_ignored():
    assert not client_is_localhost(
        _Req("203.0.113.7", {"x-forwarded-for": "127.0.0.1"}))


def test_docker_host_peer_is_local():
    assert client_is_localhost(_Req(GATEWAY))


def test_local_addrs_grants_no_xff_trust(monkeypatch):
    monkeypatch.setenv("B3_LOCAL_ADDRS", "192.168.50.7")
    req = _Req("192.168.50.7", {"x-forwarded-for": "203.0.113.7"})
    assert client_is_localhost(req)  # the peer itself is local; XFF not believed


def test_loopback_proxy_forwarding_remote_client_is_remote():
    # e.g. tailscale serve / a local reverse proxy that sets XFF
    assert not client_is_localhost(
        _Req("127.0.0.1", {"x-forwarded-for": "100.64.0.9"}))
