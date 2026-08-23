#!/usr/bin/env bash
# Bash mirror of the Test-Endpoint / Test-EndpointReady PowerShell helpers:
# read-only check that each named service in the namespace has ready endpoints.
#
# Usage: ./scripts/endpoints.sh [-n NAMESPACE] [SERVICE...]
#   With no services named, checks the Command Center UI set.
set -euo pipefail

NAMESPACE="cascade-system"
SERVICES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    *) SERVICES+=("$1"); shift ;;
  esac
done

if [[ ${#SERVICES[@]} -eq 0 ]]; then
  SERVICES=(project-qa-service evals-service command-center-api command-center)
fi

die() { echo "ERROR: $*" >&2; exit 1; }
kubectl version --request-timeout=5s >/dev/null 2>&1 || die "kubectl cannot reach a Kubernetes cluster"

failed=0
for service in "${SERVICES[@]}"; do
  ready="$(kubectl -n "$NAMESPACE" get endpointslice -l "kubernetes.io/service-name=$service" -o json 2>/dev/null | jq '[.items[]?.endpoints[]? | select((.conditions.ready // true) and ((.addresses // []) | length > 0))] | length')"
  if [[ "${ready:-0}" -ge 1 ]]; then
    echo "PASS: svc/$service has endpoints"
  else
    echo "FAIL: svc/$service has no ready endpoints"
    failed=1
  fi
done

exit "$failed"
