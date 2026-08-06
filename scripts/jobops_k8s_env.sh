#!/usr/bin/env bash

# Source this file before using the repository-local Kubernetes toolchain.
# It does not modify the user's global shell, Docker Desktop, or kubeconfig.

if [[ -z "${JOBOPS_REPOSITORY_ROOT:-}" ]]; then
  JOBOPS_REPOSITORY_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
fi
if [[ -z "${JOBOPS_REPOSITORY_ROOT}" ]]; then
  echo "Source jobops_k8s_env.sh from inside the Jobops worktree." >&2
  return 2 2>/dev/null || exit 2
fi
export JOBOPS_TOOL_ROOT="${JOBOPS_REPOSITORY_ROOT}/.tools"
export PATH="${JOBOPS_TOOL_ROOT}/bin:${PATH}"
export DOCKER_CONFIG="${JOBOPS_TOOL_ROOT}/state/docker"
export KUBECONFIG="${JOBOPS_TOOL_ROOT}/state/kubeconfig"
