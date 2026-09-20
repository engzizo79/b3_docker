"""v0.6.0 dual-mode deployment: wizard daemon picker, system mode flags,
managed-only gating, restart trigger, and choice redaction."""

import json
from collections import namedtuple
from pathlib import Path

from fastapi.testclient import TestClient

from app import db, daemon_release
from app.deps import AppState
from app.main import create_app
from app.session import SessionStore
from tests.conftest import LOCAL, login

_Rel = namedtuple("_Rel", "tag version prerelease published")

FAKE_RELEASES = [
    _Rel("v1.1.5", "1.1.5", True, "2026-01-01"),
    _Rel("v1.1.4", "1.1.4", False, "2025-06-01"),
    _Rel("v1.1.3", "1.1.3", False, "2025-01-01"),
]


def _patch_releases(monkeypatch):
    monkeypatch.setattr(daemon_release, "list_releases",
            lambda include_prerelease=False, timeout=15: [
                r for r in FAKE_RELEASES
                if include_prerelease or not r.prerelease])


def _ext_state(settings, mock_rpc):
    settings.daemon_mode = "external"
    state = AppState(settings, mock_rpc,
            SessionStore(settings.session_secret), username="admin")
    db.init_db(settings.db_path)
    db.ensure_user(settings.db_path, "admin", settings.ui_password)
    return state


def test_system_mode_managed_flags(client: TestClient):
    h = login(client)
    r = client.get("/api/system/mode", headers=h["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "managed"
    assert body["managed"] is True
    assert body["node_restart"] is True
    assert body["backup_download"] is True


def test_system_mode_external_flags(settings, mock_rpc):
    state = _ext_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        h = login(tc)
        r = tc.get("/api/system/mode", headers=h["headers"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "external"
        assert body["managed"] is False
        assert body["node_restart"] is False
        assert body["daemon_logs"] is False
        assert body["daemon_upgrade"] is False
        assert body["backup_download"] is False


def test_setup_daemon_releases_filters_minimum(client, monkeypatch):
    _patch_releases(monkeypatch)
    h = login(client)
    r = client.get("/api/setup/daemon/releases", headers=h["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    # endpoint returns ALL stable releases with a meets_minimum flag;
    # the wizard only offers ones that meet it. Prereleases never appear.
    by_tag = {x["tag"]: x for x in body["releases"]}
    assert "v1.1.5" not in by_tag  # prerelease never offered
    assert by_tag["v1.1.4"]["meets_minimum"] is True
    assert by_tag["v1.1.3"]["meets_minimum"] is False
    assert body["min_version"] == "1.1.4"


def test_setup_daemon_choose_managed_writes_version_and_choice(
        client, monkeypatch, settings):
    _patch_releases(monkeypatch)
    installed = {}
    monkeypatch.setattr(
        daemon_release, "install_version",
        lambda release, ddir, vfile, timeout=300: (
            installed.update({"version": release.version}),
            Path(vfile).write_text(release.version)))
    h = login(client)
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "managed", "version": "1.1.4"},
            headers=h["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choice"]["version"] == "1.1.4"
    assert body["downloaded"] is True
    assert installed["version"] == "1.1.4"
    assert body["backend_restart"] is False
    vf = Path(settings.daemon_version_file)
    assert vf.is_file() and vf.read_text().strip() == "1.1.4"
    cf = Path(settings.b3_data_dir) / ".daemon_choice.json"
    assert cf.is_file()
    assert json.loads(cf.read_text())["mode"] == "managed"


def test_setup_daemon_choose_no_download_when_installed(
        client, monkeypatch, settings):
    """Binary already present at the requested version: no download."""
    _patch_releases(monkeypatch)
    dbin = Path(settings.daemon_dir) / "b3coind"
    dbin.parent.mkdir(parents=True, exist_ok=True)
    dbin.write_text("#!/bin/sh\n")
    dbin.chmod(0o755)
    vf = Path(settings.daemon_version_file)
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text("1.1.4")
    called = {"n": 0}
    monkeypatch.setattr(
        daemon_release, "install_version",
        lambda *a, **k: called.update({"n": called["n"] + 1}))
    h = login(client)
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "managed", "version": "1.1.4"},
            headers=h["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["downloaded"] is False
    assert called["n"] == 0


def test_setup_daemon_choose_defaults_to_latest_stable(
        client, monkeypatch):
    _patch_releases(monkeypatch)
    monkeypatch.setattr(
        daemon_release, "install_version",
        lambda release, ddir, vfile, timeout=300:
            Path(vfile).write_text(release.version))
    h = login(client)
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "managed"}, headers=h["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["choice"]["version"] == "1.1.4"


def test_setup_daemon_choose_rejects_below_minimum(client, monkeypatch):
    _patch_releases(monkeypatch)
    h = login(client)
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "managed", "version": "1.1.3"},
            headers=h["headers"])
    assert r.status_code == 422, r.text


def test_setup_daemon_choose_external_validation_and_restart(
        client, monkeypatch, settings):
    _patch_releases(monkeypatch)
    h = login(client)
    from pathlib import Path
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "external", "ext_host": "10.0.0.5",
                "ext_port": 38647, "ext_user": "u"},
            headers=h["headers"])
    assert r.status_code == 422, r.text
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "external", "ext_host": "10.0.0.5",
                "ext_port": 38647, "ext_user": "u",
                "ext_password": "secret"},
            headers=h["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["backend_restart"] is True
    assert (Path(settings.b3_data_dir) / "restart-backend.cmd").is_file()
    r = client.get("/api/setup/daemon/choice", headers=h["headers"])
    assert r.status_code == 200, r.text
    assert "ext_password" not in json.dumps(r.json())
    assert r.json()["choice"]["ext_host"] == "10.0.0.5"


def test_setup_daemon_choose_requires_csrf(client, monkeypatch):
    _patch_releases(monkeypatch)
    login(client)
    r = client.post("/api/setup/daemon/choose",
            json={"mode": "managed", "version": "1.1.4"},
            headers=LOCAL)
    assert r.status_code == 403, r.text


def test_system_upgrade_gated_in_external_mode(settings, mock_rpc):
    state = _ext_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        h = login(tc)
        # body included: the mode gate must fire before payload checks; a body-required regression cannot mask it
        r = tc.post("/api/system/upgrade",
            json={"tag": "v1.1.5"}, headers=h["headers"])
        assert r.status_code == 403, r.text
        assert "managed mode" in r.json()["detail"]


def test_setup_status_shape_stable(client):
    h = login(client)
    r = client.get("/api/setup/status", headers=h["headers"])
    assert r.status_code == 200, r.text
    for key in ("wizard_done", "setup_required", "data_persistent", "conf"):
        assert key in r.json()
