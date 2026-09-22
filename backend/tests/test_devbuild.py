"""Phase 4 (docs/MULTINODE_PLAN.md): installing an unpublished dev build by
an explicit {url, sha256} pair rather than a GitHub release lookup."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import daemon_release
from tests.conftest import LOCAL, login

GOOD_SHA = "a" * 64


def test_release_from_url_builds_release_info():
    r = daemon_release.release_from_url(
        "http://10.0.0.5:8765/b3-hive-flowmesh-abc123.tar.gz", GOOD_SHA, "flowmesh-abc123")
    assert r.url == "http://10.0.0.5:8765/b3-hive-flowmesh-abc123.tar.gz"
    assert r.sha256 == GOOD_SHA
    assert r.tag == "flowmesh-abc123"
    assert r.prerelease is True
    assert r.sha256_url == ""


def test_release_from_url_rejects_bad_sha256():
    with pytest.raises(ValueError):
        daemon_release.release_from_url("http://x/y.tar.gz", "not-a-hex-digest")
    with pytest.raises(ValueError):
        daemon_release.release_from_url("http://x/y.tar.gz", "")


def test_expected_sha256_prefers_direct_value_over_url_fetch():
    """A dev build's sha256 is used directly — _expected_sha256 must never
    reach out to sha256_url (there isn't one) to resolve it."""
    r = daemon_release.release_from_url("http://x/y.tar.gz", GOOD_SHA, "dev")
    assert daemon_release._expected_sha256(r) == GOOD_SHA


def test_system_upgrade_devbuild_requires_tag(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.post("/api/system/upgrade", json={
        "url": "http://10.0.0.5:8765/b.tar.gz", "sha256": GOOD_SHA,
    }, headers=out["headers"])
    assert r.status_code == 400
    assert "tag" in r.json()["detail"]


def test_system_upgrade_devbuild_rejects_bad_sha256(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.post("/api/system/upgrade", json={
        "url": "http://10.0.0.5:8765/b.tar.gz", "sha256": "garbage", "tag": "flowmesh-1",
    }, headers=out["headers"])
    assert r.status_code == 400


def test_system_upgrade_devbuild_rejects_non_http_url(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.post("/api/system/upgrade", json={
        "url": "file:///etc/passwd", "sha256": GOOD_SHA, "tag": "flowmesh-1",
    }, headers=out["headers"])
    assert r.status_code == 400


def test_system_upgrade_devbuild_skips_min_version_gate(client: TestClient):
    """A dev build's tag is a free-text label, not a version — it must
    never be run through meets_minimum (which would reject almost any
    non-numeric label)."""
    out = login(client, headers=LOCAL)
    r = client.post("/api/system/upgrade", json={
        "url": "http://10.0.0.5:8765/b3-hive-flowmesh-abc123.tar.gz",
        "sha256": GOOD_SHA, "tag": "flowmesh-abc123",
    }, headers=out["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["tag"] == "flowmesh-abc123"


def test_system_upgrade_devbuild_writes_full_cmd(client: TestClient):
    out = login(client, headers=LOCAL)
    settings = client.app.state.app_state.settings
    r = client.post("/api/system/upgrade", json={
        "url": "http://10.0.0.5:8765/b3-hive-flowmesh-abc123.tar.gz",
        "sha256": GOOD_SHA, "tag": "flowmesh-abc123",
    }, headers=out["headers"])
    assert r.status_code == 200, r.text
    cmd = json.loads(Path(settings.b3_data_dir, "upgrade.cmd").read_text())
    assert cmd["tag"] == "flowmesh-abc123"
    assert cmd["url"] == "http://10.0.0.5:8765/b3-hive-flowmesh-abc123.tar.gz"
    assert cmd["sha256"] == GOOD_SHA
    assert cmd["action"] == "upgrade"


def test_system_upgrade_tag_only_path_unaffected(client: TestClient, monkeypatch):
    """The plain {tag} path (real GitHub releases) still works exactly as
    before Phase 4 — no url/sha256 in the written command."""
    from collections import namedtuple
    Rel = namedtuple("Rel", "tag version prerelease published")
    monkeypatch.setattr(daemon_release, "list_releases",
            lambda include_prerelease=False, timeout=15: [Rel("v1.1.5", "1.1.5", False, "")])
    out = login(client, headers=LOCAL)
    settings = client.app.state.app_state.settings
    r = client.post("/api/system/upgrade", json={"tag": "v1.1.5"}, headers=out["headers"])
    assert r.status_code == 200, r.text
    cmd = json.loads(Path(settings.b3_data_dir, "upgrade.cmd").read_text())
    assert cmd["tag"] == "v1.1.5"
    assert "url" not in cmd
    assert "sha256" not in cmd
