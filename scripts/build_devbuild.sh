#!/usr/bin/env bash
# Build an unpublished B3-CoinV2 dev build (docs/MULTINODE_PLAN.md Phase 4)
# and extract the resulting tarball + sha256 to a local output directory.
#
# Usage:
#   scripts/build_devbuild.sh --src /path/to/B3-CoinV2 [--label NAME] \
#     [--out DIR] [--serve [PORT]]
#
#   --src    Path to a LOCAL CHECKOUT of B3-CoinV2, already on the
#            branch/commit to build (this script never clones or fetches —
#            no network access or credentials for a private/experimental
#            branch are needed). Required.
#   --label  Free-text label for the tarball name (default: short commit
#            sha of --src, or "dev" if that checkout isn't a git repo).
#   --out    Where to write the tarball + .sha256 (default: ./devbuild-out).
#   --serve  Start a plain local HTTP file server over the output dir after
#            building (default port 8765), so install_version() on another
#            container can fetch it by {url, sha256} — see
#            docs/MULTINODE_PLAN.md Phase 4.1. Never exposes anything
#            beyond localhost/your LAN; nothing here touches a public host.
#
# This never builds a runtime b3hive image and never pushes anywhere —
# docker/Dockerfile.devbuild "must not share a base layer with, or be
# referenced by, the runtime image" (the plan's own words). The only output
# is a tarball on disk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC=""
LABEL=""
OUT="${REPO_ROOT}/devbuild-out"
SERVE=""
SERVE_PORT="8765"

while [ $# -gt 0 ]; do
    case "$1" in
        --src) SRC="$2"; shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --serve) SERVE="1"
                 if [ "${2:-}" ] && [[ "${2:-}" =~ ^[0-9]+$ ]]; then SERVE_PORT="$2"; shift; fi
                 shift ;;
        -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [ -z "${SRC}" ]; then
    echo "error: --src /path/to/B3-CoinV2 is required" >&2
    exit 1
fi
if [ ! -d "${SRC}" ]; then
    echo "error: --src does not exist: ${SRC}" >&2
    exit 1
fi

if [ -z "${LABEL}" ]; then
    LABEL="$(git -C "${SRC}" rev-parse --short HEAD 2>/dev/null || true)"
    LABEL="${LABEL:-dev}"
fi
# Keep the label filesystem/tarball-name safe.
LABEL="$(printf '%s' "${LABEL}" | tr -c 'A-Za-z0-9._-' '-')"

echo "==> Building devbuild image (label=${LABEL}, src=${SRC})"
echo "    This compiles B3-CoinV2 from source (Bitcoin-Core-derived C++) —"
echo "    expect this to take a while and to need real CPU/RAM."
docker build -f "${REPO_ROOT}/docker/Dockerfile.devbuild" \
    --build-arg "B3_BUILD_LABEL=${LABEL}" \
    -t "b3hive-devbuild:${LABEL}" \
    "${SRC}"

mkdir -p "${OUT}"
echo "==> Extracting build output to ${OUT}"
CID="$(docker create "b3hive-devbuild:${LABEL}")"
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT
docker cp "${CID}:/out/." "${OUT}/"
docker rm -f "${CID}" >/dev/null 2>&1 || true
trap - EXIT

# Name matches docker/Dockerfile.devbuild's packaging step exactly.
TARBALL="${OUT}/b3-hive-${LABEL}-unsigned-linux-x86_64-static-headless-devbuild.tar.gz"
SHA_FILE="${TARBALL}.sha256"
if [ ! -f "${TARBALL}" ] || [ ! -f "${SHA_FILE}" ]; then
    echo "error: expected output not found in ${OUT}" >&2
    exit 1
fi
SHA256="$(awk '{print $1}' "${SHA_FILE}")"

echo "==> Done"
echo "    tarball: ${TARBALL}"
echo "    sha256:  ${SHA256}"
echo
echo "    POST /api/system/upgrade body (once served — see --serve):"
echo "    {\"url\": \"http://<this-machine>:${SERVE_PORT}/$(basename "${TARBALL}")\", \"sha256\": \"${SHA256}\", \"label\": \"${LABEL}\"}"

if [ -n "${SERVE}" ]; then
    echo
    echo "==> Serving ${OUT} on 0.0.0.0:${SERVE_PORT} (Ctrl-C to stop)"
    echo "    Point every validator's upgrade request at this machine's"
    echo "    address on your LAN/Docker network — never a public host."
    cd "${OUT}" && exec python3 -m http.server "${SERVE_PORT}" --bind 0.0.0.0
fi
