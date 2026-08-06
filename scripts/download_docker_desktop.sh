#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALLER_DIR="${REPOSITORY_ROOT}/.tools/installers"
DOCKER_DESKTOP_VERSION="4.85.0"
DOCKER_DESKTOP_BUILD="235549"
DOCKER_DESKTOP_SHA256="84b1224c93456fe261955ebc91f3cd88ce19778ffdb6d0a0d423ce37246f7c2b"
INSTALLER="${INSTALLER_DIR}/Docker-${DOCKER_DESKTOP_VERSION}-arm64.dmg"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This installer download currently supports macOS arm64 only." >&2
  exit 2
fi

mkdir -p "${INSTALLER_DIR}"
if [[ ! -s "${INSTALLER}" ]]; then
  curl --fail --location --retry 3 \
    --output "${INSTALLER}" \
    "https://desktop.docker.com/mac/main/arm64/${DOCKER_DESKTOP_BUILD}/Docker.dmg"
fi

ACTUAL_SHA256="$(shasum -a 256 "${INSTALLER}" | awk '{print $1}')"
if [[ "${ACTUAL_SHA256}" != "${DOCKER_DESKTOP_SHA256}" ]]; then
  echo "Docker Desktop installer SHA-256 verification failed." >&2
  exit 3
fi

echo "Verified Docker Desktop ${DOCKER_DESKTOP_VERSION} installer: ${INSTALLER}"
echo "Installation is intentionally manual because it writes to /Applications and installs privileged helpers."
