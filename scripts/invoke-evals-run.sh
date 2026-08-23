#!/usr/bin/env bash
# Bash mirror of scripts/invoke-evals-run.ps1: submit an eval run payload to the
# Evals Studio API, optionally wait for completion, and optionally save the JSON.
#
# Usage: ./scripts/invoke-evals-run.sh -PayloadPath examples/evals-run-request.json
#        [--api-url URL] [--api-key KEY] [--result-path FILE] [--wait] [--timeout-seconds N]
#
# Environment fallbacks: CASCADE_EVALS_API_URL, CASCADE_EVALS_API_KEY.
set -euo pipefail

PAYLOAD_PATH=""
API_URL="${CASCADE_EVALS_API_URL:-http://localhost:8041}"
API_KEY="${CASCADE_EVALS_API_KEY:-}"
RESULT_PATH=""
WAIT=false
TIMEOUT_SECONDS=60

while [[ $# -gt 0 ]]; do
  case "$1" in
    -PayloadPath|--payload-path) PAYLOAD_PATH="$2"; shift 2 ;;
    --api-url) API_URL="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --result-path|-ResultPath) RESULT_PATH="$2"; shift 2 ;;
    --wait|-Wait) WAIT=true; shift ;;
    --timeout-seconds) TIMEOUT_SECONDS="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 64 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
command -v jq >/dev/null || die "jq is required"
[[ -f "$PAYLOAD_PATH" ]] || die "Payload file not found: $PAYLOAD_PATH (pass -PayloadPath)"
jq empty "$PAYLOAD_PATH" 2>/dev/null || die "Payload file is not valid JSON: $PAYLOAD_PATH"

endpoint="${API_URL%/}/runs"
auth_args=()
if [[ -n "$API_KEY" ]]; then auth_args=(-H "Authorization: Bearer $API_KEY"); fi

echo "Submitting eval run to $endpoint"
accepted="$(curl --fail --silent --show-error -X POST "$endpoint" \
  -H 'Content-Type: application/json' "${auth_args[@]}" --data-binary @"$PAYLOAD_PATH")" || die "Submit to $endpoint failed"
run_id="$(printf '%s' "$accepted" | jq -r .run_id)"
status="$(printf '%s' "$accepted" | jq -r .status)"
echo "Run accepted: $run_id ($status)"
result="$accepted"

if [[ "$WAIT" == true ]]; then
  status_endpoint="${API_URL%/}/runs/$run_id"
  deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    sleep 0.5
    result="$(curl --fail --silent "${auth_args[@]}" "$status_endpoint")" || die "Polling $status_endpoint failed"
    status="$(printf '%s' "$result" | jq -r .status)"
    if [[ "$status" == "completed" || "$status" == "failed" ]]; then break; fi
  done
  echo "Evals status: $status"
  echo "Aggregate score: $(printf '%s' "$result" | jq -r .aggregate_score)"
fi

if [[ -n "$RESULT_PATH" ]]; then
  printf '%s' "$result" | jq . > "$RESULT_PATH"
  echo "Saved eval run result to $RESULT_PATH"
fi

if [[ "$(printf '%s' "$result" | jq -r .status)" == "failed" ]]; then
  echo "ERROR: Eval run failed: $(printf '%s' "$result" | jq -r '.error // "unknown error"')" >&2
  exit 2
fi

printf '%s\n' "$result" | jq .
