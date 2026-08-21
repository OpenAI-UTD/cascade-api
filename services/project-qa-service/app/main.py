from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.qa import EvaluationRequest, EvaluationResult, evaluate
from services.shared.qa.evals import fixture_catalog, run_eval_fixtures, summarize_eval_checks
from services.shared.qa.jobs import JobAcceptedResponse, JobDetailResponse, JobQueue
from services.shared.qa.projects import ProjectRegistration, ProjectRegistry
from services.shared.qa.storage import EvaluationStore
from services.shared.security.auth import AuthSettings, auth_status, require_auth

app = FastAPI(
    title="Cascade Project QA API",
    version="0.3.0",
    description="Project-agnostic CI and runtime quality evaluation with documentation-grounded recommendations and fix proposals.",
)


class Settings(BaseSettings):
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"
    cascade_qa_db_path: str = ".cascade/qa-evaluations.db"
    cascade_qa_webhook_url: str = ""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
store = EvaluationStore(settings.cascade_qa_db_path)
registry = ProjectRegistry(settings.cascade_qa_db_path)
job_queue = JobQueue(settings.cascade_qa_db_path, webhook_url=settings.cascade_qa_webhook_url)


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )


def _require_write_auth(request: Request) -> dict[str, str]:
    return require_auth(request, _auth_settings(), action="submit evaluation")


def _require_register_auth(request: Request) -> dict[str, str]:
    return require_auth(request, _auth_settings(), action="register project")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "project-qa-service",
        "stored_evaluations": store.count(),
        "registered_projects": len(registry.list_registered(limit=500)),
        "storage": "sqlite",
        "auth": auth_status(_auth_settings()),
        "webhook_configured": bool(settings.cascade_qa_webhook_url.strip()),
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    return {"status": "ok", "service": "project-qa-service"}


@app.get("/evals")
@app.get("/v1/qa/evals", include_in_schema=False)
async def list_eval_fixtures() -> dict[str, Any]:
    return {"fixtures": fixture_catalog(), "count": len(fixture_catalog())}


@app.post("/evals/run")
@app.post("/v1/qa/evals/run", include_in_schema=False)
async def run_eval_suite(responses: dict[str, str] | None = None) -> dict[str, Any]:
    checks = run_eval_fixtures(responses or {})
    summary = summarize_eval_checks(checks)
    return {"checks": [check.model_dump(mode="json") for check in checks], "summary": summary}


@app.get("/projects")
@app.get("/v1/qa/projects", include_in_schema=False)
async def list_projects(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    summaries = store.list_projects(limit=limit)
    projects = registry.merge_summaries(summaries)
    return {"projects": projects, "count": len(projects)}


@app.post("/projects/register", status_code=status.HTTP_201_CREATED)
@app.post("/v1/qa/projects/register", status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def register_project(
    payload: ProjectRegistration,
    request: Request,
    _: dict[str, str] = Depends(_require_register_auth),
) -> dict[str, Any]:
    project = registry.register(payload)
    return {"project": project.model_dump(mode="json")}


@app.post("/evaluations")
@app.post("/v1/qa/evaluations", include_in_schema=False)
async def create_evaluation(
    payload: EvaluationRequest,
    request: Request,
    async_mode: bool = Query(default=False, alias="async"),
    _: dict[str, str] = Depends(_require_write_auth),
) -> JSONResponse:
    pipeline_id = payload.pipeline.pipeline_id.strip()
    if pipeline_id:
        existing = _find_by_pipeline(payload.project.project_id, pipeline_id)
        if existing is not None:
            if async_mode:
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={"job_id": existing.evaluation_id, "status": "completed", "evaluation_id": existing.evaluation_id},
                )
            return JSONResponse(status_code=status.HTTP_201_CREATED, content=existing.model_dump(mode="json"))
    if not registry.record_run(payload.project.project_id):
        raise HTTPException(status_code=429, detail="Daily evaluation quota exceeded for project")
    if async_mode:
        job = job_queue.enqueue(payload, evaluate_fn=evaluate, save_fn=store.save)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=JobAcceptedResponse(job_id=job.job_id).model_dump(mode="json"),
        )
    result = evaluate(payload)
    store.save(result)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=result.model_dump(mode="json"))


@app.get("/jobs/{job_id}", response_model=JobDetailResponse)
@app.get("/v1/qa/jobs/{job_id}", response_model=JobDetailResponse, include_in_schema=False)
async def get_job(job_id: str) -> JobDetailResponse:
    job = job_queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    evaluation = job_queue.get_evaluation_payload(job_id) if job.status == "completed" else None
    return JobDetailResponse(job=job, evaluation=evaluation)


@app.get("/evaluations/{evaluation_id}", response_model=EvaluationResult)
@app.get("/v1/qa/evaluations/{evaluation_id}", response_model=EvaluationResult, include_in_schema=False)
async def get_evaluation(evaluation_id: str) -> EvaluationResult:
    result = store.get(evaluation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    return result


@app.get("/projects/{project_id}/evaluations")
@app.get("/v1/qa/projects/{project_id}/evaluations", include_in_schema=False)
async def project_evaluations(project_id: str, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    results = store.list_project(project_id, limit=limit)
    affiliation_findings = 0
    agent_authz_findings = 0
    for result in results:
        for finding in result.findings:
            if finding.category == "affiliation":
                affiliation_findings += 1
            elif finding.category in {"agent-authz", "eval"}:
                agent_authz_findings += 1
    return {
        "project_id": project_id,
        "evaluations": results,
        "count": len(results),
        "eval_fixture_summary": {
            "affiliation_findings": affiliation_findings,
            "agent_authz_findings": agent_authz_findings,
        },
    }


def _find_by_pipeline(project_id: str, pipeline_id: str) -> EvaluationResult | None:
    for result in store.list_project(project_id, limit=100):
        if result.pipeline.pipeline_id == pipeline_id:
            return result
    return None


@app.post("/admin/reset", include_in_schema=False)
async def reset_store(request: Request) -> dict[str, str]:
    require_auth(request, _auth_settings(), action="reset evaluation store")
    store.reset()
    return {"status": "reset"}
