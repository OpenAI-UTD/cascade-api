#!/usr/bin/env bash
# Bash mirror of the port-forward smoke section of scripts/deploy.ps1:
# forward each Command Center UI service to a free local port, curl its health
# endpoint (read-only GETs only), then clean up the forwards.
#
# Usage: ./scripts/port-forward-smoke.sh [-n NAMESPACE]
set -euo pipefail

NAMESPACE="cascade-system"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
kubectl version --request-timeout=5s >/dev/null 2>&1 || die "kubectl cannot reach a Kubernetes cluster"
command -v jq >/dev/null || die "jq is required"

PORT_FORWARD_PIDS=()
cleanup() {
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
PY
}

smoke_json_health() {
  local service="$1" remote_port="$2" path="$3" local_port pid
  local_port="$(free_port)"
  kubectl -n "$NAMESPACE" port-forward "svc/$service" "$local_port:$remote_port" >/dev/null 2>&1 &
  pid=$!
  PORT_FORWARD_PIDS+=("$pid")
  sleep 3
  if ! kill -0 "$pid" 2>/dev/null; then
    die "port-forward for svc/$service exited early"
  fi
  if [[ "$(curl --fail --silent "http://localhost:$local_port$path" | jq -r .status 2>/dev/null)" == "ok" ]]; then
    echo "PASS: $service $path"
  else
    die "$service $path returned unexpected payload"
  fi
}

smoke_json_health project-qa-service 8040 /health
cleanup

smoke_json_health evals-service 8041 /health
cleanup

smoke_json_health command-center-api 8031 /health
cleanup

ui_port="$(free_port)"
kubectl -n "$NAMESPACE" port-forward svc/command-center "$ui_port:8030" >/dev/null 2>&1 &
ui_pid=$!
PORT_FORWARD_PIDS+=("$ui_pid")
sleep 3
kill -0 "$ui_pid" 2>/dev/null || die "port-forward for svc/command-center exited early"
curl --fail --silent "http://localhost:$ui_port/" | grep -q '<div id="root"' || die "command-center / did not return expected HTML"
curl --fail --silent "http://localhost:$ui_port/api/retrieval/health" | jq -e .service >/dev/null || die "command-center /api/retrieval/health returned unexpected payload"
echo "PASS: command-center / and /api/retrieval/health"
cleanup

echo "PASS: all port-forward smoke checks succeeded"
