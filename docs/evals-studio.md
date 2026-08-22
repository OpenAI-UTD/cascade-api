# Evals Studio API

The Evals Studio service (`services/evals-service`, port **8041**) is evals-as-a-service for the club: members and officers submit prompt-eval runs over HTTP, and the service scores each output against a weighted rubric while reporting per-case latency and estimated token cost. It complements the [Project QA API](project-qa-api.md): Project QA evaluates CI/runtime evidence for a repository; Evals Studio evaluates prompt responses.

## Run it

From the Cascade repository root:

```powershell
python -m pip install -r services/evals-service/requirements.txt
python -m uvicorn --app-dir services/evals-service app.main:app --host 0.0.0.0 --port 8041
```

The service exposes:

- `POST /runs`: submit an eval run (async; returns `202` with `run_id`).
- `GET /runs?project=`: list recent runs, optionally filtered by project name.
- `GET /runs/{run_id}`: retrieve one run with all per-case results.
- `GET /adapters`: list scoring adapters and whether the LLM judge is configured.
- `GET /health`, `GET /ready`, and `/docs`: operations and OpenAPI endpoints.

When deployed with the platform, the Command Center proxy exposes submissions at `POST /api/evals/runs`.

## Submit a run

```powershell
./scripts/invoke-evals-run.ps1 `
  -PayloadPath examples/evals-run-request.json `
  -ApiUrl http://localhost:8041
```

Or with curl:

```bash
curl -X POST http://localhost:8041/runs -H 'Content-Type: application/json' \
  -d @examples/evals-run-request.json
# -> {"run_id":"run_…","status":"queued"}
curl http://localhost:8041/runs/run_…
```

A run request contains:

- `project_name`: stable project identifier used for listing and quotas-style trimming.
- `rubric`: 1-8 criteria, each `{name, weight 0..1, description}`, weights summing to `1.0` within a tolerance of +/-`0.01`.
- `cases`: 1-50 cases, each `{name, input, expected?}`. The `input` field carries the candidate output text under evaluation; `expected` is an optional reference substring.
- `adapter`: `heuristic` (default) or `openai-compatible`; optional `model` override.

The response contains `aggregate_score` (mean weighted score across cases), per-case results with per-criterion `score` (0-5) plus justification, `weighted_score`, `latency_ms`, estimated `tokens_in` / `tokens_out` / `cost_usd`, and a `degraded` flag when the LLM judge failed and fell back to heuristics.

## Scoring adapters

### heuristic (default)

Deterministic local scorer, no network. Per criterion:

```text
keyword coverage k   = fraction of rubric-name tokens (lowercased alphanumeric
                       words of length >= 3) present in the candidate text
length adequacy l    = min(1.0, word_count / 30)
expected bonus e     = 1.0 when case.expected occurs case-insensitively in the
                       candidate text, else 0.0

score = round(min(5.0, 3.0 * k + 1.0 * l + 1.0 * e), 2)
```

The maximum without the expected-substring bonus is 4.0. Identical input always produces identical scores, so the adapter is safe for regression gates.

### openai-compatible

Sends the rubric and candidate text to `{CASCADE_EVALS_LLM_BASE_URL}/chat/completions` in JSON mode and asks the judge model for `{"criteria": [{"name", "score" 0-5, "justification"}]}` covering every criterion exactly once. On any network or parsing failure the service falls back to the heuristic scorer for that case and marks the result `degraded=true`.

Configuration:

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `CASCADE_EVALS_LLM_BASE_URL` | Judge base URL, e.g. `http://llm.test/v1` | empty (judge disabled) |
| `CASCADE_EVALS_LLM_API_KEY` | Bearer token sent to the judge | empty |
| `CASCADE_EVALS_LLM_MODEL` | Judge model name | empty |
| `CASCADE_EVALS_PRICE_IN_PER_MTOK` | Input price, USD per million tokens | `0.15` |
| `CASCADE_EVALS_PRICE_OUT_PER_MTOK` | Output price, USD per million tokens | `0.60` |

Cost math: `cost_usd = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out`. Token estimates use `len(text) // 4` unless the judge response reports usage.

## Storage and auth

Runs are persisted in SQLite at `CASCADE_EVALS_DB_PATH` (default `.cascade/evals-runs.db`) in `runs` + `case_results` tables, mirroring the Project QA store. Submission requires auth when `CASCADE_AUTH_ENABLED=true` (same key configuration as the other services); reads stay open for local demos.

## Deployment

Kubernetes manifests live in `infra/kubernetes/evals-service/` (port 8041). The Command Center proxy forwards `/api/evals/*` through `EVALS_SERVICE_URL`.
