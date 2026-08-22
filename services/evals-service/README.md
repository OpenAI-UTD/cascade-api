# Evals Studio Service

This FastAPI service is evals-as-a-service for the club: members and officers submit prompt-eval runs, and the service scores each output against a weighted rubric while reporting per-case latency and estimated token cost.

Run locally from the repository root:

```powershell
python -m pip install -r services/evals-service/requirements.txt
python -m uvicorn --app-dir services/evals-service app.main:app --host 0.0.0.0 --port 8041
```

OpenAPI is available at `http://localhost:8041/docs`. See `docs/evals-studio.md` for the request contract, scoring formula, and adapter configuration.

## Adapters

- `heuristic` (default): deterministic local scorer with no network calls.
- `openai-compatible`: asks an OpenAI-compatible `/chat/completions` endpoint to judge each case in JSON mode; any failure falls back to the heuristic scorer and marks the result `degraded=true`.

Configuration: `CASCADE_EVALS_LLM_BASE_URL`, `CASCADE_EVALS_LLM_API_KEY`, `CASCADE_EVALS_LLM_MODEL`, plus the price table `CASCADE_EVALS_PRICE_IN_PER_MTOK` / `CASCADE_EVALS_PRICE_OUT_PER_MTOK` (defaults 0.15 / 0.60 USD per million tokens).
