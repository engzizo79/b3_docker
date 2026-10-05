"""Daemon release discovery, version comparison, and install/upgrade.

Managed mode downloads official B3-CoinV2 release binaries from GitHub into
<data>/daemon and records the installed version. All network access is
outbound HTTPS to api.github.com only. Failures are failure-tolerant.

Architecture: the binary must match the container's CPU (platform.machine).
Upstream only publishes linux-x86_64 today, so on other CPUs (arm64 — e.g.
a Raspberry Pi) a release falls back to a COMMUNITY build of the same
upstream tag: built by this project's CI (.github/workflows/daemon-build.yml)
from the pinned upstream commit and published as a `daemon-<tag>` release in
this repo. An official build for the CPU always wins over a community one,
and a community build is only installed with a verified SHA-256.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com/repos/B3-Coin/B3-CoinV2/releases"
ASSET_RE = re.compile(r"b3-hive-(.+)-unsigned-linux-[a-z0-9_]+-static-headless\.tar\.gz")
# Community builds (see module docstring): release `daemon-<upstream tag>`.
COMMUNITY_API = "https://api.github.com/repos/engzizo79/b3_docker/releases"
COMMUNITY_TAG_PREFIX = "daemon-"

_ARCH_ALIASES = {"x86_64": "x86_64", "amd64": "x86_64",
                 "aarch64": "aarch64", "arm64": "aarch64"}


def daemon_arch(machine: str | None = None) -> str:
    """The release-asset architecture name for this CPU (x86_64, aarch64)."""
    m = (machine if machine is not None else platform.machine()).strip().lower()
    return _ARCH_ALIASES.get(m, m)


def _asset_suffix(arch: str) -> str:
    return f"-linux-{arch}-static-headless.tar.gz"


def no_release_hint(arch: str | None = None) -> str:
    """Why no release is installable, in words an operator can act on."""
    arch = arch or daemon_arch()
    if arch == "x86_64":
        return "no suitable release found"
    return (f"no daemon build for this CPU ({arch}) yet — neither an official "
            f"B3-CoinV2 release nor a community build (a '{COMMUNITY_TAG_PREFIX}<tag>' "
            "release in engzizo79/b3_docker) provides one")


@dataclass
class ReleaseInfo:
    tag: str
    version: str
    url: str
    sha256_url: str
    prerelease: bool
    published: str
    # Phase 4 (docs/MULTINODE_PLAN.md): a directly-known digest for a dev
    # build (docker/Dockerfile.devbuild's output), bypassing the
    # GitHub-style "fetch a SHA256SUMS file from sha256_url" path entirely
    # — see _expected_sha256 and release_from_url. Empty for every GitHub
    # release, which still verifies via sha256_url exactly as before.
    sha256: str = ""
    # "official" (B3-CoinV2's own release asset) or "community" (this
    # project's CI build of the same upstream tag for a CPU upstream does
    # not ship — see module docstring). arch is the asset's CPU.
    source: str = "official"
    arch: str = "x86_64"
    # Further checksum files to try, in order, when sha256_url does not list
    # the asset. Releases ship several (SHA256SUMS, SHA256SUMS.linux-static,
    # SHA256SUMS.txt — which in v1.1.4 lists only bootstrap.dat).
    sha256_urls: tuple[str, ...] = ()


def _parse_version(v: str) -> tuple[int, int, int]:
    s = v.lstrip("vV").strip()
    parts = re.split(r"[.\-]", s)[:3]
    nums = []
    for p in parts:
        m = re.match(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])  # type: ignore[return-value]


def compare_versions(a: str, b: str) -> int:
    pa, pb = _parse_version(a), _parse_version(b)
    return (pa > pb) - (pa < pb)


def is_newer(candidate: str, baseline: str) -> bool:
    return compare_versions(candidate, baseline) > 0


def meets_minimum(version: str, minimum: str) -> bool:
    return compare_versions(version, minimum) >= 0


def _fetch_json(url: str, timeout: int = 15) -> dict | list:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "b3hive-wallet",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_bytes(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "b3hive-wallet"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _community_builds(arch: str, timeout: int) -> dict[str, tuple[str, str]]:
    """{upstream tag: (asset url, SHA256SUMS url)} for this CPU. Optional
    enrichment: any failure just means "no community builds"."""
    try:
        raw = _fetch_json(COMMUNITY_API + "?per_page=50", timeout=timeout)
    except Exception:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for rel in raw if isinstance(raw, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith(COMMUNITY_TAG_PREFIX):
            continue
        asset_url = sha_url = ""
        for a in rel.get("assets") or []:
            name = str(a.get("name") or "")
            if name.endswith(_asset_suffix(arch)):
                asset_url = str(a.get("browser_download_url") or "")
            elif name == "SHA256SUMS":
                sha_url = str(a.get("browser_download_url") or "")
        if asset_url and sha_url:
            out[tag[len(COMMUNITY_TAG_PREFIX):]] = (asset_url, sha_url)
    return out


def _sums_priority(name: str) -> int:
    """Most complete checksum manifest first: SHA256SUMS, then Linux-specific
    ones, then anything else (e.g. SHA256SUMS.txt)."""
    if name == "SHA256SUMS":
        return 0
    if "linux" in name:
        return 1
    return 2


def list_releases(include_prerelease: bool = False,
                   timeout: int = 15, arch: str | None = None) -> list[ReleaseInfo]:
    """Installable releases for this CPU, newest first. Upstream's release
    list is the source of truth for which versions exist; for each, the
    official asset for `arch` is used, else a community build of that same
    tag, else the release is skipped."""
    arch = arch or daemon_arch()
    raw = _fetch_json(GITHUB_API + "?per_page=30", timeout=timeout)
    if not isinstance(raw, list):
        return []
    community: dict[str, tuple[str, str]] | None = None
    out: list[ReleaseInfo] = []
    for rel in raw:
        if not isinstance(rel, dict):
            continue
        prerelease = bool(rel.get("prerelease"))
        if prerelease and not include_prerelease:
            continue
        tag = str(rel.get("tag_name") or "")
        if not tag:
            continue
        asset_url = ""
        sums: list[tuple[int, str]] = []
        for a in rel.get("assets") or []:
            name = str(a.get("name") or "")
            if name.endswith(_asset_suffix(arch)):
                asset_url = str(a.get("browser_download_url") or "")
            elif name.startswith("SHA256SUMS"):
                sums.append((_sums_priority(name), str(a.get("browser_download_url") or "")))
        sums_urls = [u for _, u in sorted(sums) if u]
        sha_url = sums_urls[0] if sums_urls else ""
        source = "official"
        if not asset_url and arch != "x86_64":
            if community is None:
                community = _community_builds(arch, timeout)
            if tag in community:
                asset_url, sha_url = community[tag]
                sums_urls = [sha_url]
                source = "community"
        if not asset_url:
            continue
        m = ASSET_RE.search(asset_url) if source == "official" else None
        version = m.group(1) if m else tag.lstrip("vV")
        out.append(ReleaseInfo(
            tag=tag, version=version, url=asset_url, sha256_url=sha_url,
            prerelease=prerelease,
            published=str(rel.get("published_at") or ""),
            source=source, arch=arch, sha256_urls=tuple(sums_urls[1:]),
        ))
    out.sort(key=lambda r: _parse_version(r.version), reverse=True)
    return out


def check_latest(minimum: str, cache_file: str | None = None,
                  timeout: int = 15) -> dict:
    result: dict = {
        "checked_at": "", "min_version": minimum,
        "current_installed": "", "latest_stable": "",
        "update_available": False, "releases": [], "error": None,
    }
    try:
        rels = list_releases(include_prerelease=False, timeout=timeout)
    except Exception as exc:
        result["error"] = str(exc)
        _write_cache(cache_file, result)
        return result
    result["releases"] = [asdict(r) for r in rels]
    if rels:
        result["latest_stable"] = rels[0].tag
    if cache_file:
        result["current_installed"] = _read_installed(cache_file)
    if result["latest_stable"] and result["current_installed"]:
        result["update_available"] = is_newer(
            result["latest_stable"].lstrip("vV"),
            result["current_installed"].lstrip("vV"))
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    _write_cache(cache_file, result)
    return result


def _write_cache(cache_file: str | None, data: dict) -> None:
    if not cache_file:
        return
    try:
        Path(cache_file).parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, cache_file)
    except OSError:
        pass


def _read_installed(release_check_file: str) -> str:
    try:
        vf = Path(release_check_file).parent / "daemon" / ".installed_version"
        if vf.exists():
            return vf.read_text().strip().lstrip("vV")
    except OSError:
        pass
    return ""


def read_installed_version(version_file: str) -> str:
    try:
        vf = Path(version_file)
        if vf.exists():
            return vf.read_text().strip()
    except OSError:
        pass
    return ""


def read_installed_source(version_file: str) -> dict:
    """{"source": "official"|"community"|"", "arch": ...} for the installed
    daemon; empty strings when unknown (e.g. installed before this existed,
    which can only have been an official x86_64 build)."""
    try:
        parts = (Path(version_file).with_name(".installed_source")
                 .read_text().split())
    except OSError:
        parts = []
    return {"source": parts[0] if parts else "",
            "arch": parts[1] if len(parts) > 1 else ""}


def install_version(release: ReleaseInfo, daemon_dir: str,
                    version_file: str, timeout: int = 180) -> str:
    daemon_path = Path(daemon_dir)
    daemon_path.mkdir(parents=True, exist_ok=True)
    blob = _fetch_bytes(release.url, timeout=timeout)
    expected_sha = _expected_sha256(release, timeout=timeout)
    actual_sha = hashlib.sha256(blob).hexdigest()
    if not expected_sha:
        # No checksum (missing SHA256SUMS, asset not listed in it, or the
        # sums file could not be fetched) means nothing ties these bytes to
        # the release — never install an unverified daemon binary, official
        # or community.
        raise RuntimeError(
            f"no verifiable SHA256 for {release.tag} ({release.source}) — refusing to install")
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"SHA256 mismatch: expected {expected_sha}, got {actual_sha}")
    staging = Path(tempfile.mkdtemp(prefix="b3daemon-"))
    backup = None
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            # "data" filter: refuse absolute paths, ../ traversal, links out
            # of the staging dir and device files, even from a verified tarball.
            tf.extractall(staging, filter="data")
        bin_dir = _find_bin_dir(staging)
        if bin_dir is None:
            raise RuntimeError("tarball has no bin/ directory")
        if any(daemon_path.iterdir()):
            backup = daemon_path.parent / ".daemon_backup"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(daemon_path), str(backup))
        daemon_path.mkdir(parents=True, exist_ok=True)
        for exe in ("b3coind", "b3coin-cli", "b3coin-wallet", "b3coin-tx", "b3coin-util"):
            src = bin_dir / exe
            if src.exists():
                shutil.copy2(str(src), str(daemon_path / exe))
                os.chmod(daemon_path / exe, 0o755)
        Path(version_file).write_text(release.tag)
        Path(version_file).with_name(".installed_source").write_text(
            f"{release.source} {release.arch}")
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
        return release.tag
    except (tarfile.TarError, OSError) as exc:
        if backup is not None and backup.exists():
            if daemon_path.exists():
                shutil.rmtree(daemon_path)
            shutil.move(str(backup), str(daemon_path))
        raise RuntimeError(f"install failed: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def release_from_url(url: str, sha256: str, label: str = "dev") -> ReleaseInfo:
    """Build a ReleaseInfo for an explicit {url, sha256} pair rather than a
    GitHub lookup (docs/MULTINODE_PLAN.md Phase 4.1) — e.g. the output of
    scripts/build_devbuild.sh, served over a LAN HTTP file server.
    install_version() needs nothing further: it already only cares about
    .url (what to fetch) and the sha256 _expected_sha256 resolves, which
    this ReleaseInfo makes a direct value instead of a second fetch.

    sha256 is REQUIRED (unlike a GitHub release with no sha256_url, which
    silently skips verification) — a plain-HTTP local fetch with no other
    integrity check has nothing else standing between "fetched some bytes"
    and "installed some bytes" on a node holding real funds."""
    digest = sha256.lower().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("sha256 must be a 64-character hex digest")
    return ReleaseInfo(tag=label, version=label, url=url, sha256_url="",
                       prerelease=True, published="", sha256=digest,
                       source="devbuild", arch=daemon_arch())


def _expected_sha256(release: ReleaseInfo, timeout: int = 15) -> str:
    if release.sha256:
        return release.sha256.lower()
    asset_name = release.url.rsplit("/", 1)[-1]
    for url in (release.sha256_url, *release.sha256_urls):
        if not url:
            continue
        try:
            sums = _fetch_bytes(url, timeout=timeout).decode("utf-8")
        except Exception:
            continue
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
                return parts[0].lower()
    return ""


def _find_bin_dir(root: Path) -> Path | None:
    for dirpath, dirnames, filenames in os.walk(root):
        if "b3coind" in filenames:
            return Path(dirpath)
    return None
