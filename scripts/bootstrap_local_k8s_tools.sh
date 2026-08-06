#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOOL_ROOT="${REPOSITORY_ROOT}/.tools"
BIN_DIR="${TOOL_ROOT}/bin"
CACHE_DIR="${TOOL_ROOT}/cache"

KIND_VERSION="0.32.0"
KUBECTL_VERSION="1.36.3"
KUBECONFORM_VERSION="0.8.0"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This bootstrap currently supports macOS arm64 only." >&2
  exit 2
fi

mkdir -p "${BIN_DIR}" "${CACHE_DIR}" "${TOOL_ROOT}/state"

download() {
  local url="$1"
  local destination="$2"
  if [[ ! -s "${destination}" ]]; then
    curl --fail --location --retry 3 --output "${destination}" "${url}"
  fi
}

verify_sha256() {
  local expected="$1"
  local path="$2"
  local actual
  actual="$(shasum -a 256 "${path}" | awk '{print $1}')"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "SHA-256 verification failed for ${path}" >&2
    exit 3
  fi
}

KIND_ASSET="${CACHE_DIR}/kind-darwin-arm64-${KIND_VERSION}"
KIND_SUM="${CACHE_DIR}/kind-darwin-arm64-${KIND_VERSION}.sha256sum"
download "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-darwin-arm64" "${KIND_ASSET}"
download "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-darwin-arm64.sha256sum" "${KIND_SUM}"
verify_sha256 "$(awk '{print $1}' "${KIND_SUM}")" "${KIND_ASSET}"
install -m 0755 "${KIND_ASSET}" "${BIN_DIR}/kind"

KUBECTL_ASSET="${CACHE_DIR}/kubectl-${KUBECTL_VERSION}"
KUBECTL_SUM="${CACHE_DIR}/kubectl-${KUBECTL_VERSION}.sha256"
download "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/darwin/arm64/kubectl" "${KUBECTL_ASSET}"
download "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/darwin/arm64/kubectl.sha256" "${KUBECTL_SUM}"
verify_sha256 "$(tr -d '[:space:]' < "${KUBECTL_SUM}")" "${KUBECTL_ASSET}"
install -m 0755 "${KUBECTL_ASSET}" "${BIN_DIR}/kubectl"

KUBECONFORM_ASSET="${CACHE_DIR}/kubeconform-darwin-arm64-${KUBECONFORM_VERSION}.tar.gz"
KUBECONFORM_SUMS="${CACHE_DIR}/kubeconform-${KUBECONFORM_VERSION}-CHECKSUMS"
download "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/kubeconform-darwin-arm64.tar.gz" "${KUBECONFORM_ASSET}"
download "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/CHECKSUMS" "${KUBECONFORM_SUMS}"
verify_sha256 "$(awk '$2 == "kubeconform-darwin-arm64.tar.gz" {print $1}' "${KUBECONFORM_SUMS}")" "${KUBECONFORM_ASSET}"
tar -xzf "${KUBECONFORM_ASSET}" -C "${BIN_DIR}" kubeconform
chmod 0755 "${BIN_DIR}/kubeconform"

export JOBOPS_REPOSITORY_ROOT="${REPOSITORY_ROOT}"
source "${SCRIPT_DIR}/jobops_k8s_env.sh"
mkdir -p "${DOCKER_CONFIG}" "$(dirname "${KUBECONFIG}")"

echo "Repository-local Kubernetes tools are ready in ${TOOL_ROOT}."
"${BIN_DIR}/kind" version
"${BIN_DIR}/kubectl" version --client=true
"${BIN_DIR}/kubeconform" -v
