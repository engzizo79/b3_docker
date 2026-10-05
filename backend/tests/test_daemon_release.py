"""Architecture-aware daemon release selection (arm64 support): official
upstream assets for the container's CPU win; otherwise a community build
(`daemon-<tag>` release in this repo) of the SAME upstream tag; otherwise
the release is not installable on this CPU. Community builds are never
installed without a verified SHA-256."""

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from app import daemon_release
from app.daemon_release import ReleaseInfo

GH = "https://github.com/dl/"


def _asset(name):
    return {"name": name, "browser_download_url": GH + name}


UPSTREAM = [
    {"tag_name": "v1.1.5", "prerelease": False, "published_at": "2026-10-01",
     "assets": [_asset("b3-hive-v1.1.5-unsigned-linux-x86_64-static-headless.tar.gz"),
                _asset("b3-hive-v1.1.5-unsigned-linux-aarch64-static-headless.tar.gz"),
                _asset("SHA256SUMS")]},
    {"tag_name": "v1.1.4", "prerelease": False, "published_at": "2026-08-01",
     "assets": [_asset("b3-hive-v1.1.4-unsigned-linux-x86_64-static-headless.tar.gz"),
                _asset("SHA256SUMS")]},
    {"tag_name": "v1.1.3", "prerelease": False, "published_at": "2026-06-01",
     "assets": [_asset("b3-hive-v1.1.3-unsigned-linux-x86_64-static-headless.tar.gz"),
                _asset("SHA256SUMS")]},
    {"tag_name": "preview-1.1.6", "prerelease": True, "assets": []},
]

COMMUNITY = [
    {"tag_name": "daemon-v1.1.4", "draft": False,
     "assets": [_asset("b3-hive-v1.1.4-community-linux-aarch64-static-headless.tar.gz"),
                _asset("SHA256SUMS"), _asset("BUILD-INFO.json")]},
    # Also covers v1.1.5, but upstream ships aarch64 itself there — must lose.
    {"tag_name": "daemon-v1.1.5", "draft": False,
     "assets": [_asset("b3-hive-v1.1.5-community-linux-aarch64-static-headless.tar.gz"),
                _asset("SHA256SUMS")]},
    # No checksum file -> unusable, ignored.
    {"tag_name": "daemon-v1.1.3", "draft": False,
     "assets": [_asset("b3-hive-v1.1.3-community-linux-aarch64-static-headless.tar.gz")]},
    # The image's own release tags are not daemon builds.
    {"tag_name": "v0.8.13-beta", "draft": False, "assets": []},
]


@pytest.fixture()
def fake_github(monkeypatch):
    calls = []

    def fake_fetch_json(url, timeout=15):
        calls.append(url)
        if url.startswith(daemon_release.GITHUB_API):
            return UPSTREAM
        if url.startswith(daemon_release.COMMUNITY_API):
            return COMMUNITY
        raise AssertionError(url)

    monkeypatch.setattr(daemon_release, "_fetch_json", fake_fetch_json)
    return calls


@pytest.mark.parametrize("machine,arch", [
    ("x86_64", "x86_64"), ("AMD64", "x86_64"), ("aarch64", "aarch64"), ("arm64", "aarch64"),
    ("armv7l", "armv7l"),
])
def test_daemon_arch_aliases(machine, arch):
    assert daemon_release.daemon_arch(machine) == arch


def test_x86_64_uses_official_only_and_never_asks_community(fake_github):
    rels = daemon_release.list_releases(arch="x86_64")
    assert [r.tag for r in rels] == ["v1.1.5", "v1.1.4", "v1.1.3"]
    assert {r.source for r in rels} == {"official"}
    assert all("x86_64" in r.url for r in rels)
    assert not any(u.startswith(daemon_release.COMMUNITY_API) for u in fake_github)


def test_aarch64_prefers_official_then_community(fake_github):
    rels = {r.tag: r for r in daemon_release.list_releases(arch="aarch64")}
    # v1.1.3's community build has no SHA256SUMS -> not offered at all.
    assert list(rels) == ["v1.1.5", "v1.1.4"]
    assert rels["v1.1.5"].source == "official"
    assert rels["v1.1.5"].url.endswith("v1.1.5-unsigned-linux-aarch64-static-headless.tar.gz")
    assert rels["v1.1.4"].source == "community"
    assert rels["v1.1.4"].url.endswith("v1.1.4-community-linux-aarch64-static-headless.tar.gz")
    assert rels["v1.1.4"].sha256_url.endswith("SHA256SUMS")
    assert rels["v1.1.4"].version == "1.1.4"
    assert {r.arch for r in rels.values()} == {"aarch64"}


def test_community_lookup_failure_is_tolerated(monkeypatch):
    def fake_fetch_json(url, timeout=15):
        if url.startswith(daemon_release.GITHUB_API):
            return UPSTREAM
        raise OSError("github down")
    monkeypatch.setattr(daemon_release, "_fetch_json", fake_fetch_json)
    rels = daemon_release.list_releases(arch="aarch64")
    assert [r.tag for r in rels] == ["v1.1.5"]


def test_unknown_cpu_has_nothing_and_a_clear_hint(fake_github):
    assert daemon_release.list_releases(arch="armv7l") == []
    hint = daemon_release.no_release_hint("armv7l")
    assert "armv7l" in hint and "community" in hint
    assert daemon_release.no_release_hint("x86_64") == "no suitable release found"


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("pkg/bin/b3coind")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _community_release(sha256_url="https://x/SHA256SUMS") -> ReleaseInfo:
    return ReleaseInfo(tag="v1.1.4", version="1.1.4",
                       url="https://x/b3-hive-v1.1.4-community-linux-aarch64-static-headless.tar.gz",
                       sha256_url=sha256_url, prerelease=False, published="",
                       source="community", arch="aarch64")


def test_community_build_without_checksum_is_refused(tmp_path, monkeypatch):
    blob = _tarball()
    monkeypatch.setattr(daemon_release, "_fetch_bytes", lambda url, timeout=0: blob)
    rel = _community_release(sha256_url="")
    with pytest.raises(RuntimeError, match="SHA256"):
        daemon_release.install_version(rel, str(tmp_path / "daemon"), str(tmp_path / "ver"))
    assert not (tmp_path / "daemon" / "b3coind").exists()


def test_community_build_installs_and_records_source(tmp_path, monkeypatch):
    blob = _tarball()
    name = "b3-hive-v1.1.4-community-linux-aarch64-static-headless.tar.gz"
    sums = f"{hashlib.sha256(blob).hexdigest()}  {name}\n".encode()
    monkeypatch.setattr(daemon_release, "_fetch_bytes",
                        lambda url, timeout=0: sums if url.endswith("SHA256SUMS") else blob)
    version_file = tmp_path / "daemon" / ".installed_version"
    daemon_release.install_version(_community_release(), str(tmp_path / "daemon"), str(version_file))
    assert (tmp_path / "daemon" / "b3coind").exists()
    assert daemon_release.read_installed_version(str(version_file)) == "v1.1.4"
    assert daemon_release.read_installed_source(str(version_file)) == {
        "source": "community", "arch": "aarch64"}


def test_installed_source_unknown_before_this_existed(tmp_path):
    vf = tmp_path / ".installed_version"
    vf.write_text("v1.1.4")
    assert daemon_release.read_installed_source(str(vf)) == {"source": "", "arch": ""}
