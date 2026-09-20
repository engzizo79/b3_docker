"""Daemon release discovery, version comparison, and install/upgrade.

Managed mode downloads official B3-CoinV2 release binaries from GitHub into
<data>/daemon and records the installed version. All network access is
outbound HTTPS to api.github.com only. Failures are failure-tolerant.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com/repos/B3-Coin/B3-CoinV2/releases"
ASSET_RE = re.compile(r"b3-hive-(.+)-unsigned-linux-x86_64-static-headless\.tar\.gz")


@dataclass
class ReleaseInfo:
    tag: str
    version: str
    url: str
    sha256_url: str
    prerelease: bool
    published: str


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


def list_releases(include_prerelease: bool = False,
                   timeout: int = 15) -> list[ReleaseInfo]:
    raw = _fetch_json(GITHUB_API + "?per_page=30", timeout=timeout)
    if not isinstance(raw, list):
        return []
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
        sha_url = ""
        for a in rel.get("assets") or []:
            name = str(a.get("name") or "")
            if name.endswith("-linux-x86_64-static-headless.tar.gz"):
                asset_url = str(a.get("browser_download_url") or "")
            elif name in ("SHA256SUMS", "SHA256SUMS.txt"):
                sha_url = str(a.get("browser_download_url") or "")
        if not asset_url:
            continue
        m = ASSET_RE.search(asset_url)
        version = m.group(1) if m else tag.lstrip("vV")
        out.append(ReleaseInfo(
            tag=tag, version=version, url=asset_url, sha256_url=sha_url,
            prerelease=prerelease,
            published=str(rel.get("published_at") or ""),
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


def install_version(release: ReleaseInfo, daemon_dir: str,
                    version_file: str, timeout: int = 180) -> str:
    daemon_path = Path(daemon_dir)
    daemon_path.mkdir(parents=True, exist_ok=True)
    blob = _fetch_bytes(release.url, timeout=timeout)
    expected_sha = _expected_sha256(release, timeout=timeout)
    actual_sha = hashlib.sha256(blob).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(
            f"SHA256 mismatch: expected {expected_sha}, got {actual_sha}")
    staging = Path(tempfile.mkdtemp(prefix="b3daemon-"))
    backup = None
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            tf.extractall(staging)
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


def _expected_sha256(release: ReleaseInfo, timeout: int = 15) -> str:
    if not release.sha256_url:
        return ""
    try:
        sums = _fetch_bytes(release.sha256_url, timeout=timeout).decode("utf-8")
    except Exception:
        return ""
    asset_name = release.url.rsplit("/", 1)[-1]
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
