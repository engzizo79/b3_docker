"""Persistence guard tests (v0.3.1).

The CRITICAL user-reported bug: with the compose volume mapping removed,
Dockerfile VOLUME ["/data"] silently created an ANONYMOUS volume -
os.path.ismount() still returned True, so setup proceeded on storage that
dies with docker compose down. The guard now inspects the mount source.
"""

import pytest

def _fake_mountinfo(tmp_path, lines):
    f = tmp_path / "mountinfo"
    f.write_text(chr(10).join(lines) + chr(10))
    return str(f)

def test_bind_mount_is_persistent(tmp_path):
    from app.deps import _mount_is_persistent
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
        "37 36 253:1 /srv/b3 /data rw,rel - ext4 /dev/sda1",
    ])
    assert _mount_is_persistent("/data", mi) is True

def test_named_volume_is_persistent(tmp_path):
    from app.deps import _mount_is_persistent
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
        "37 36 253:2 /b3hive_data/_data /data rw,rel - ext4 /var/lib/docker/volumes/b3hive_data/_data",
    ])
    assert _mount_is_persistent("/data", mi) is True

def test_anonymous_volume_is_not_persistent(tmp_path):
    from app.deps import _mount_is_persistent
    anon = "a" * 64
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
        "37 36 253:3 /" + anon + "/_data /data rw,rel - ext4 /var/lib/docker/volumes/" + anon + "/_data",
    ])
    assert _mount_is_persistent("/data", mi) is False

def test_overlay_data_dir_is_not_persistent(tmp_path):
    from app.deps import _mount_is_persistent
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
        "37 36 0:41 / /data rw,rel - overlay /var/lib/docker/overlay2/xyz/merged",
    ])
    assert _mount_is_persistent("/data", mi) is False

def test_tmpfs_is_not_persistent(tmp_path):
    from app.deps import _mount_is_persistent
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
        "37 36 0:42 / /data rw,rel - tmpfs tmpfs",
    ])
    assert _mount_is_persistent("/data", mi) is False

def test_unmounted_dir_refuses_persistence(tmp_path):
    from app.deps import _mount_is_persistent
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
    ])
    assert _mount_is_persistent("/data", mi) is False

def test_missing_mountinfo_file_refuses(tmp_path):
    from app.deps import _mount_is_persistent
    assert _mount_is_persistent("/data", str(tmp_path / "nope")) is False

@pytest.fixture
def conf_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "b3coin.conf").write_text(chr(10).join([
        "# generated", "txindex=1", "listen=1", "listenonion=0",
        "rpcbind=127.0.0.1", "rpcallowip=127.0.0.1", "rpcport=32647",
        "rpcuser=test", "rpcpassword=testpass", "disablewallet=0"]))
    return d

@pytest.fixture
def setup_client(client, conf_dir):
    s = client.app.state.app_state.settings
    s.b3_data_dir = str(conf_dir)
    s.bootstrap_cmd_file = str(conf_dir / "bootstrap.cmd")
    s.bootstrap_progress_file = str(conf_dir / "bootstrap.progress.json")
    s.wizard_marker_file = str(conf_dir / ".wizard_complete")
    return client

def test_setup_endpoints_blocked_on_ephemeral_data(setup_client):
    """CRITICAL: first-run setup must refuse ephemeral storage - a wallet
    on throwaway storage silently loses funds on container removal."""
    def _login(client):
        r = client.post("/api/auth/login",
                        json={"password": "correct horse battery staple"})
        assert r.status_code == 200
    def _csrf(client):
        return {"x-csrf-token": client.cookies.get("b3_csrf")}
    _login(setup_client)
    s = setup_client.app.state.app_state.settings
    s.allow_ephemeral_data = False
    try:
        # The storage-blocked middleware intercepts ALL /api/* routes (except
        # /api/health) with 503 + storage_blocked:true before the per-endpoint
        # require_persistent_data guard (403) can run. Both layers exist; the
        # middleware is the user-facing gate, the per-endpoint guard is
        # defense-in-depth.
        r = setup_client.post("/api/setup/start-node", headers=_csrf(setup_client))
        assert r.status_code == 503, r.text
        assert r.json().get("storage_blocked") is True
        r = setup_client.post("/api/setup/complete", headers=_csrf(setup_client))
        assert r.status_code == 503, r.text
        assert r.json().get("storage_blocked") is True
        r = setup_client.post("/api/setup/bootstrap/start",
                              json={"height": 810000, "sha256": "ab" * 32,
                                    "url": "/bootstraps/bootstrap-810000.tar.zst",
                                    "size": 123, "wipe_chain": False},
                              headers=_csrf(setup_client))
        assert r.status_code == 503, r.text
        assert r.json().get("storage_blocked") is True
    finally:
        s.allow_ephemeral_data = True


def test_health_reports_storage_blocked_on_ephemeral(setup_client):
    """/api/health is the one public route in blocked mode: it tells the
    SPA and the Docker healthcheck that storage is refused."""
    s = setup_client.app.state.app_state.settings
    s.allow_ephemeral_data = False
    try:
        r = setup_client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["storage_blocked"] is True
        assert body["ok"] is False
    finally:
        s.allow_ephemeral_data = True


def test_non_api_routes_serve_remediation_page_on_ephemeral(setup_client):
    """The user sees a full-screen explanation page (not a crash, not a
    blank screen) when storage is not persistent."""
    s = setup_client.app.state.app_state.settings
    s.allow_ephemeral_data = False
    try:
        r = setup_client.get("/")
        assert r.status_code == 503
        assert "text/html" in r.headers.get("content-type", "")
        body = r.text
        assert "Storage is not persistent" in body
        assert "setup refused" in body.lower()
        assert "docker-compose" in body or "docker compose" in body
        # The remediation page must contain the fix steps.
        assert "b3hive-data" in body
        assert "/data" in body
    finally:
        s.allow_ephemeral_data = True


def test_api_routes_return_503_json_on_ephemeral(setup_client):
    """Programmatic clients get a 503 JSON body with the storage_blocked
    flag so they can distinguish it from a normal error."""
    s = setup_client.app.state.app_state.settings
    s.allow_ephemeral_data = False
    try:
        r = setup_client.get("/api/chain/summary")
        assert r.status_code == 503
        body = r.json()
        assert body.get("storage_blocked") is True
        assert "persistent" in body["detail"].lower()
    finally:
        s.allow_ephemeral_data = True


def test_routes_work_normally_when_persistent(setup_client):
    """When storage IS persistent, the middleware is invisible."""
    s = setup_client.app.state.app_state.settings
    # login first so chain/summary is reachable
    setup_client.post("/api/auth/login",
                     json={"password": "correct horse battery staple"})
    csrf = setup_client.cookies.get("b3_csrf")
    r = setup_client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("storage_blocked") is False
    # Root serves the SPA (200 with the Alpine root), not the 503 remediation
    # page. The SPA template does contain a hidden storage-blocked block, so
    # check for the SPA marker, not absence of the remediation text.
    r = setup_client.get("/")
    assert r.status_code == 200
    assert 'x-data="b3app"' in r.text


def test_guard_follows_effective_data_dir_not_literal_slash_data(tmp_path):
    """The exact scenario the user asked about: /data is a persistent bind
    mount, but B3_DATA_DIR points somewhere else (ephemeral). The guard
    must check the EFFECTIVE dir, not literal /data — otherwise setup would
    be allowed while the daemon writes to throwaway storage.

    Also proves the state-path consistency fix: with a custom B3_DATA_DIR,
    the DB / wizard marker / cmd files must default INTO that dir (not the
    old hardcoded /data), so state never splits across two directories.
    """
    from app.config import Settings
    import os

    custom = tmp_path / "customdata"
    custom.mkdir()

    # Point B3_DATA_DIR at the custom dir; leave everything else default.
    old = os.environ.get("B3_DATA_DIR")
    os.environ["B3_DATA_DIR"] = str(custom)
    try:
        s = Settings()
        # Every state path derives from the effective data dir.
        assert str(custom) in s.db_path, s.db_path
        assert str(custom) in s.wizard_marker_file
        assert str(custom) in s.bootstrap_cmd_file
        assert str(custom) in s.start_node_cmd_file
        assert str(custom) in s.recovery_cmd_file
        # The literal /data must not leak into any default.
        assert s.db_path != "/data/b3hive.db"
        assert s.wizard_marker_file != "/data/.wizard_complete"
    finally:
        if old is None:
            os.environ.pop("B3_DATA_DIR", None)
        else:
            os.environ["B3_DATA_DIR"] = old

    # And the mount check itself: a fake mountinfo where /data IS a
    # persistent bind mount but the custom dir is overlay (ephemeral).
    from app.deps import _mount_is_persistent
    mi = _fake_mountinfo(tmp_path, [
        "36 35 0:40 / / rw,rel - overlay /var/lib/docker/overlay2/abc/merged",
        "37 36 253:1 /srv/b3 /data rw,rel - ext4 /dev/sda1",
        "38 36 0:41 / /customdata rw,rel - overlay /var/lib/docker/overlay2/xyz/merged",
    ])
    # The effective dir is ephemeral -> blocked, even though /data is fine.
    assert _mount_is_persistent(str(custom), mi) is False
    # Literal /data is persistent in this fake world — proving the check
    # really consulted the EFFECTIVE dir, not the literal one.
    assert _mount_is_persistent("/data", mi) is True



def test_blocked_page_shows_actual_data_dir(setup_client):
    """The remediation page renders the effective B3_DATA_DIR, not a
    hardcoded /data (misleading when the operator customized it)."""
    s = setup_client.app.state.app_state.settings
    s.allow_ephemeral_data = False
    try:
        r = setup_client.get("/")
        assert r.status_code == 503
        # The page must contain the REAL data dir the guard checked.
        assert s.b3_data_dir in r.text
        # And no un-templated placeholder must leak.
        assert "{{DATA_DIR}}" not in r.text
    finally:
        s.allow_ephemeral_data = True
