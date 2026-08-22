from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.security.auth import AuthSettings, auth_status, require_auth

from .engine import EngineSettings, adapters_catalog, estimate_cost, run_case, weighted_score
from .schemas import CaseResult, RunCreate, RunResult
from .storage import RunStore

app = FastAPI(
    title="Cascade Evals Studio API",
    version="0.1.0",
    description="Evals-as-a-service: members and officers submit prompt-eval runs that are scored against weighted rubrics with per-case latency and cost reporting.",
)


class Settings(BaseSettings):
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"
    cascade_evals_db_path: str = ".cascade/evals-runs.db"
    cascade_evals_llm_base_url: str = ""
    cascade_evals_llm_api_key: str = ""
    cascade_evals_llm_model: str = ""
    cascade_evals_price_in_per_mtok: float = 0.15
    cascade_evals_price_out_per_mtok: float = 0.60

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
store = RunStore(settings.cascade_evals_db_path)


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )


def _engine_settings() -> EngineSettings:
    return EngineSettings(
        llm_base_url=settings.cascade_evals_llm_base_url,
        llm_api_key=settings.cascade_evals_llm_api_key,
        llm_model=settings.cascade_evals_llm_model,
        price_in_per_mtok=settings.cascade_evals_price_in_per_mtok,
        price_out_per_mtok=settings.cascade_evals_price_out_per_mtok,
    )


def _require_run_auth(request: Request) -> dict[str, str]:
    return require_auth(request, _auth_settings(), action="submit eval run")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "evals-service",
        "stored_runs": store.count(),
        "storage": "sqlite",
        "auth": auth_status(_auth_settings()),
        "llm_judge_configured": _engine_settings().llm_configured,
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    return {"status": "ok", "service": "evals-service"}


@app.get("/adapters")
async def list_adapters() -> dict[str, Any]:
    engine_settings = _engine_settings()
    adapters = adapters_catalog(engine_settings)
    return {
        "adapters": adapters,
        "count": len(adapters),
        "default_adapter": "heuristic",
        "llm_model": engine_settings.llm_model or None,
    }


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    payload: RunCreate,
    request: Request,
    _: dict[str, str] = Depends(_require_run_auth),
) -> JSONResponse:
    run = RunResult(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_name=payload.project_name,
        adapter=payload.adapter,
        model=payload.model,
        status="queued",
        created_at=datetime.now(UTC).isoformat(),
    )
    store.save(run, request=payload)
    worker = threading.Thread(
        target=_execute_run,
        args=(run.id,),
        name=f"cascade-evals-run-{run.id}",
        daemon=True,
    )
    worker.start()
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"run_id": run.id, "status": run.status},
    )


@app.get("/runs")
async def list_runs(
    project: str = Query(default="", alias="project", description="Filter by project name"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    runs = (
        store.list_project(project.strip(), limit=limit)
        if project.strip()
        else store.list_recent(limit=limit)
    )
    return {
        "runs": [run.model_dump(mode="json") for run in runs],
        "count": len(runs),
    }


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> RunResult:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/admin/reset", include_in_schema=False)
async def reset_store(request: Request) -> dict[str, str]:
    require_auth(request, _auth_settings(), action="reset eval run store")
    store.reset()
    return {"status": "reset"}


def _execute_run(run_id: str) -> None:
    run = store.get(run_id)
    if run is None:
        return
    running = run.model_copy(update={"status": "running"})
    store.save(running)
    try:
        payload = store.get_request(run_id)
        if payload is None:
            raise RuntimeError(f"request payload missing for run {run_id}")
        engine_settings = _engine_settings()
        results: list[CaseResult] = []
        for case in payload.cases:
            scores, latency_ms, tokens_in, tokens_out, degraded = run_case(case, payload.rubric, engine_settings)
            results.append(
                CaseResult(
                    case=case,
                    scores=scores,
                    weighted_score=weighted_score(scores, payload.rubric),
                    latency_ms=latency_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=estimate_cost(tokens_in, tokens_out, engine_settings),
                    degraded=degraded,
                )
            )
        aggregate = round(sum(item.weighted_score for item in results) / len(results), 4) if results else None
        store.save(
            running.model_copy(
                update={
                    "status": "completed",
                    "aggregate_score": aggregate,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "results": results,
                }
            )
        )
    except Exception as exc:  # noqa: BLE001 - run worker must capture all failures
        store.save(
            running.model_copy(
                update={
                    "status": "failed",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        )
