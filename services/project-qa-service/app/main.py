from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, HTTPException, Query, status

from services.shared.qa import EvaluationRequest, EvaluationResult, evaluate

app = FastAPI(
    title="Cascade Project QA API",
    version="0.1.0",
    description="Project-agnostic CI and runtime quality evaluation with documentation-grounded recommendations and fix proposals.",
)

MAX_RUNS_PER_PROJECT = 100
_evaluations: dict[str, EvaluationResult] = {}
_project_runs: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=MAX_RUNS_PER_PROJECT))


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "project-qa-service", "stored_evaluations": len(_evaluations)}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    return {"status": "ok", "service": "project-qa-service"}


@app.post("/evaluations", response_model=EvaluationResult, status_code=status.HTTP_201_CREATED)
@app.post("/v1/qa/evaluations", response_model=EvaluationResult, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_evaluation(payload: EvaluationRequest) -> EvaluationResult:
    result = evaluate(payload)
    _evaluations[result.evaluation_id] = result
    runs = _project_runs[payload.project.project_id]
    if len(runs) == runs.maxlen:
        expired = runs[0]
        _evaluations.pop(expired, None)
    runs.append(result.evaluation_id)
    return result


@app.get("/evaluations/{evaluation_id}", response_model=EvaluationResult)
@app.get("/v1/qa/evaluations/{evaluation_id}", response_model=EvaluationResult, include_in_schema=False)
async def get_evaluation(evaluation_id: str) -> EvaluationResult:
    result = _evaluations.get(evaluation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return result


@app.get("/projects/{project_id}/evaluations")
@app.get("/v1/qa/projects/{project_id}/evaluations", include_in_schema=False)
async def project_evaluations(project_id: str, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    run_ids = list(_project_runs.get(project_id, []))[-limit:]
    results = [_evaluations[run_id] for run_id in reversed(run_ids) if run_id in _evaluations]
    return {"project_id": project_id, "evaluations": results, "count": len(results)}
