#!/usr/bin/env bash
# Bash mirror of the PowerShell restart-and-wait flow (used across deploy*.ps1):
# rollout restart one or more deployments, then block until each rollout settles.
#
# Usage: ./scripts/restart-wait.sh [-n NAMESPACE] [--timeout DURATION] DEPLOYMENT...
#   With no deployments named, restarts the Command Center UI set.
set -euo pipefail

NAMESPACE="cascade-system"
TIMEOUT="240s"
DEPLOYMENTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    *) DEPLOYMENTS+=("$1"); shift ;;
  esac
done

if [[ ${#DEPLOYMENTS[@]} -eq 0 ]]; then
  DEPLOYMENTS=(project-qa-service evals-service command-center-api command-center)
fi

die() { echo "ERROR: $*" >&2; exit 1; }
kubectl version --request-timeout=5s >/dev/null 2>&1 || die "kubectl cannot reach a Kubernetes cluster"

failed=0
for deployment in "${DEPLOYMENTS[@]}"; do
  echo "---- Restart deployment/$deployment (namespace $NAMESPACE) ----"
  if ! kubectl -n "$NAMESPACE" rollout restart "deployment/$deployment"; then
    failed=1
    continue
  fi
  echo "---- Wait for deployment/$deployment (timeout $TIMEOUT) ----"
  if ! kubectl -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout="$TIMEOUT"; then
    failed=1
  fi
done

if [[ "$failed" -ne 0 ]]; then die "One or more rollouts failed; see output above."; fi
echo "PASS: all requested rollouts completed"
