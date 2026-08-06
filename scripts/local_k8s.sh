#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export JOBOPS_REPOSITORY_ROOT="${REPOSITORY_ROOT}"
source "${SCRIPT_DIR}/jobops_k8s_env.sh"

CLUSTER_NAME="jobops-local"

require_tools() {
  for tool in kind kubectl kubeconform; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      echo "Missing ${tool}; run scripts/bootstrap_local_k8s_tools.sh first." >&2
      exit 2
    fi
  done
}

require_docker_desktop() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker CLI is unavailable. Install Docker Desktop, then retry." >&2
    exit 3
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "Docker Desktop is not running. Install/start it, then retry." >&2
    exit 3
  fi
}

start_cluster() {
  if ! kind get clusters 2>/dev/null | grep -Fxq "${CLUSTER_NAME}"; then
    kind create cluster \
      --name "${CLUSTER_NAME}" \
      --config "${REPOSITORY_ROOT}/deploy/overlays/local/kind-config.yaml" \
      --kubeconfig "${KUBECONFIG}"
  fi
  kubectl wait --for=condition=Ready node --all --timeout=180s
}

render() {
  kubectl kustomize "${REPOSITORY_ROOT}/deploy/overlays/local"
}

prepare() {
  kubectl apply -f "${REPOSITORY_ROOT}/deploy/overlays/local/namespace.yaml"
  kubectl -n jobops-demo apply --server-side -k "${REPOSITORY_ROOT}/deploy/base"
  kubectl -n jobops-demo get deployment,service,serviceaccount,configmap
}

status() {
  kubectl get nodes
  kubectl get storageclass
  kubectl -n jobops-demo get deployment,pod,service,persistentvolumeclaim
}

validate() {
  local schema_cache="${JOBOPS_TOOL_ROOT}/cache/kubeconform-schemas"
  local overlay
  mkdir -p "${schema_cache}"
  for overlay in local production; do
    kubectl kustomize "${REPOSITORY_ROOT}/deploy/overlays/${overlay}" | \
      kubeconform \
        -cache "${schema_cache}" \
        -exit-on-error \
        -kubernetes-version 1.36.0 \
        -strict \
        -summary
  done
}

stop_cluster() {
  if kind get clusters 2>/dev/null | grep -Fxq "${CLUSTER_NAME}"; then
    kind delete cluster --name "${CLUSTER_NAME}"
  fi
}

usage() {
  echo "Usage: scripts/local_k8s.sh doctor|cluster-up|prepare|status|render|validate|down"
}

require_tools
case "${1:-}" in
  doctor)
    if command -v docker >/dev/null 2>&1; then
      docker --version
    else
      echo "Docker Desktop CLI: not installed"
    fi
    kind version
    kubectl version --client=true
    kubeconform -v
    ;;
  cluster-up)
    require_docker_desktop
    start_cluster
    kubectl get nodes
    ;;
  prepare)
    prepare
    ;;
  status)
    status
    ;;
  render)
    render
    ;;
  validate)
    validate
    ;;
  down)
    stop_cluster
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
