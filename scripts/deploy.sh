#!/usr/bin/env bash
# Bash mirror of scripts/deploy.ps1: deploy the Cascade Command Center UI stack
# (project-qa-service, evals-service, command-center-api, command-center) to a
# local kind cluster.
#
# Usage: ./scripts/deploy.sh [--cluster-name NAME] [--namespace NS]
#                            [--skip-remediation-deploy] [--skip-npm-install]
set -euo pipefail

CLUSTER_NAME="cascade"
NAMESPACE="cascade-system"
SKIP_REMEDIATION_DEPLOY=false
SKIP_NPM_INSTALL=false
DEPLOYMENTS=(project-qa-service evals-service command-center-api command-center)
MANIFEST_DIRS=(infra/kubernetes/project-qa-service infra/kubernetes/command-center-api infra/kubernetes/command-center)
IMAGES=(
  "cascade-project-qa-service:dev services/project-qa-service/Dockerfile"
  "cascade-command-center-api:dev services/command-center-api/Dockerfile"
  "cascade-command-center:dev web/command-center/Dockerfile"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --skip-remediation-deploy) SKIP_REMEDIATION_DEPLOY=true; shift ;;
    --skip-npm-install) SKIP_NPM_INSTALL=true; shift ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done

section() { printf '\n============================================================\n%s\n============================================================\n' "$1"; }
die() { echo "ERROR: $*" >&2; exit 1; }
checked() {
  local description="$1"; shift
  echo "---- $description ----"
  if ! "$@"; then die "$description failed"; fi
}

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

start_port_forward() {
  local service="$1" local_port="$2" remote_port="$3" pid
  kubectl -n "$NAMESPACE" port-forward "svc/$service" "$local_port:$remote_port" >/dev/null 2>&1 &
  pid=$!
  PORT_FORWARD_PIDS+=("$pid")
  sleep 3
  if ! kill -0 "$pid" 2>/dev/null; then
    die "port-forward for svc/$service exited early"
  fi
}

wait_endpoint_ready() {
  local service="$1"
  for _ in $(seq 1 30); do
    local ready
    ready="$(kubectl -n "$NAMESPACE" get endpointslice -l "kubernetes.io/service-name=$service" -o json 2>/dev/null | jq '[.items[]?.endpoints[]? | select((.conditions.ready // true) and ((.addresses // []) | length > 0))] | length')"
    if [[ "${ready:-0}" -ge 1 ]]; then return 0; fi
    sleep 2
  done
  die "Service $service has no ready endpoints"
}

for path in \
  services/project-qa-service/Dockerfile \
  services/command-center-api/Dockerfile \
  web/command-center/Dockerfile \
  web/command-center/package.json \
  infra/kubernetes/project-qa-service/deployment.yaml \
  infra/kubernetes/command-center-api/deployment.yaml \
  infra/kubernetes/command-center/deployment.yaml; do
  [[ -f "$path" ]] || die "Required Command Center UI file missing: $path"
done

section "Preflight"
checked "Verify kubectl can reach Kubernetes" kubectl version --request-timeout=5s >/dev/null
checked "Verify Docker is available" docker info >/dev/null
command -v kind >/dev/null || die "kind is not installed"
checked "Verify kind cluster '$CLUSTER_NAME' exists" bash -c "[[ \"\$(kind get clusters 2>/dev/null)\" == *\"$CLUSTER_NAME\"* ]]"
checked "Use kind context" kubectl config use-context "kind-$CLUSTER_NAME"
checked "Verify Kubernetes API" kubectl cluster-info >/dev/null

if [[ "$SKIP_REMEDIATION_DEPLOY" != true ]]; then
  section "Ensure Remediation Baseline"
  checked "Deploy Remediation baseline" ./scripts/deploy-remediation.sh --cluster-name "$CLUSTER_NAME" --namespace "$NAMESPACE" --skip-chaos-deploy
else
  echo "Skipping Remediation deploy by flag; existing Remediation services must already be available."
fi

section "Build Command Center Frontend"
(
  cd web/command-center
  if [[ "$SKIP_NPM_INSTALL" != true ]]; then
    checked "npm install" npm install
  fi
  checked "npm run build" npm run build
)

section "Build Command Center UI Images"
for entry in "${IMAGES[@]}"; do
  image="${entry%% *}"; dockerfile="${entry##* }"
  checked "Build $image" docker build -f "$dockerfile" -t "$image" .
done

section "Load Command Center UI Images Into kind"
for entry in "${IMAGES[@]}"; do
  image="${entry%% *}"
  checked "kind load $image" kind load docker-image "$image" --name "$CLUSTER_NAME"
done

section "Apply Command Center UI Manifests"
for path in "${MANIFEST_DIRS[@]}"; do
  compgen -G "$path/*.yaml" >/dev/null || die "No Kubernetes manifests found in $path"
  checked "Apply $path" kubectl apply -f "$path"
done

section "Restart Deployments"
for deployment in "${DEPLOYMENTS[@]}"; do
  checked "Restart $deployment" kubectl -n "$NAMESPACE" rollout restart "deployment/$deployment"
done

section "Wait For Command Center UI Rollouts"
for deployment in "${DEPLOYMENTS[@]}"; do
  checked "Wait for $deployment rollout" kubectl -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout=240s
done

section "Wait For Service Endpoints"
for service in "${DEPLOYMENTS[@]}"; do
  wait_endpoint_ready "$service"
  echo "PASS: svc/$service has endpoints"
done

section "Smoke Test Command Center UI Services"
qa_port="$(free_port)"
start_port_forward project-qa-service "$qa_port" 8040
[[ "$(curl --fail --silent "http://localhost:$qa_port/health" | jq -r .status)" == "ok" ]] || die "project-qa-service /health returned unexpected payload"
echo "PASS: project-qa-service /health"
cleanup

evals_port="$(free_port)"
start_port_forward evals-service "$evals_port" 8041
[[ "$(curl --fail --silent "http://localhost:$evals_port/health" | jq -r .status)" == "ok" ]] || die "evals-service /health returned unexpected payload"
echo "PASS: evals-service /health"
cleanup

api_port="$(free_port)"
start_port_forward command-center-api "$api_port" 8031
[[ "$(curl --fail --silent "http://localhost:$api_port/health" | jq -r .status)" == "ok" ]] || die "command-center-api /health returned unexpected payload"
echo "PASS: command-center-api /health"
cleanup

ui_port="$(free_port)"
start_port_forward command-center "$ui_port" 8030
curl --fail --silent "http://localhost:$ui_port/" | grep -q '<div id="root"' || die "command-center / did not return expected HTML"
curl --fail --silent "http://localhost:$ui_port/api/retrieval/health" | jq -e .service >/dev/null || die "command-center /api/retrieval/health returned unexpected payload"
echo "PASS: command-center / and /api/retrieval/health"
cleanup

section "Command Center UI Deploy Complete"
echo "PASS: command-center deployed"
echo "Local access:"
echo "kubectl port-forward -n $NAMESPACE svc/command-center 18300:8030"
echo "Open http://localhost:18300"
echo "./scripts/accept.sh"
