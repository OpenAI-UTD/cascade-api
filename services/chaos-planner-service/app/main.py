from __future__ import annotations

import json
import logging
import uuid
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.chaos.safety import default_policy, validate_plan
from services.shared.chaos.schemas import ChaosCampaignRequest, ChaosCampaignStartRequest, ChaosCampaignStatus, ChaosPlanRequest
from services.shared.chaos.templates import build_manifest
from services.shared.security.auth import AuthSettings, auth_status, binding_guard, require_auth
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.targets.catalog import ACTIVE_NAMESPACE, ACTIVE_SAFE_CHAOS_SERVICES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    topology_service_url: str = "http://topology-service.cascade-system.svc.cluster.local:8004"
    retrieval_service_url: str = "http://retrieval-service.cascade-system.svc.cluster.local:8012"
    chaos_executor_service_url: str = "http://chaos-executor-service.cascade-system.svc.cluster.local:8020"
    request_timeout_seconds: float = 10.0
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await clickhouse.initialize_schema()
    yield


app = FastAPI(title="Cascade Chaos Planner Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "chaos-planner-service", **auth_status(_auth_settings())}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    topo = await _check(settings.topology_service_url)
    retrieval = await _check(settings.retrieval_service_url)
    response = {"status": "ok" if ch and topo and retrieval else "degraded", "service": "chaos-planner-service", "clickhouse": ch, "topology_service": topo, "retrieval_service": retrieval}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.post("/plans")
async def create_plan(payload: ChaosPlanRequest) -> dict[str, Any]:
    plan = await _build_plan(payload)
    safety = validate_plan(plan, default_policy(), dry_run=payload.dry_run)
    plan.update({"status": "ready" if safety.allowed else "rejected", "safety_score": safety.safety_score, "risk_level": safety.risk_level, "safety_findings": safety.findings + safety.violations})
    await _insert_plan(plan)
    await _audit(plan, safety)
    if not safety.allowed:
        await _insert_violation(plan["plan_id"], "", "plan_rejected", "high", "; ".join(safety.violations), payload.model_dump())
        raise HTTPException(status_code=400, detail={"status": "rejected", "plan_id": plan["plan_id"], "violations": safety.violations})
    logger.info("created chaos plan id=%s service=%s kind=%s", plan["plan_id"], plan["target_service"], plan["experiment_kind"])
    return _public_plan(plan)


@app.get("/plans")
async def plans(limit: int = 20, service: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_chaos_plans(limit, service, status)
    return {"plans": [_decode_plan(row) for row in rows], "count": len(rows)}


@app.get("/plans/{plan_id}")
async def get_plan(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.chaos_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return _decode_plan(row)


@app.post("/plans/from-anomaly")
async def plan_from_anomaly(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    service = payload.get("target_service")
    anomaly_id = payload.get("anomaly_id")
    if not service:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            if anomaly_id:
                resp = await client.get(f"{settings.retrieval_service_url.rstrip('/')}/anomalies/{anomaly_id}")
                if resp.status_code < 500:
                    service = resp.json().get("anomaly", {}).get("service")
            else:
                resp = await client.get(f"{settings.retrieval_service_url.rstrip('/')}/anomalies/recent", params={"limit": 1})
                if resp.status_code < 500:
                    rows = resp.json().get("anomalies", [])
                    service = rows[0].get("service") if rows else None
    request = ChaosPlanRequest(
        objective=f"Validate {service or 'service'} resilience based on anomaly evidence",
        target_service=service or ACTIVE_SAFE_CHAOS_SERVICES[0],
        target_namespace=payload.get("target_namespace", ACTIVE_NAMESPACE),
        experiment_kind=payload.get("experiment_kind", "pod_kill"),
        duration_seconds=int(payload.get("duration_seconds", 30)),
        dry_run=bool(payload.get("dry_run", True)),
    )
    return await create_plan(request)


@app.post("/plans/{plan_id}/validate")
async def validate_existing_plan(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.chaos_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan = _decode_plan(row)
    result = validate_plan(plan, default_policy(), dry_run=True)
    await _audit(plan, result)
    return {"plan_id": plan_id, **result.model_dump()}


@app.post("/campaigns")
async def create_campaign(payload: ChaosCampaignRequest) -> dict[str, Any]:
    campaign = _campaign_dict(payload)
    violations = _validate_campaign(campaign)
    if violations:
        campaign["status"] = ChaosCampaignStatus.DRAFT.value
        campaign["validation_violations"] = violations
    await _insert_campaign(campaign)
    if violations:
        raise HTTPException(status_code=400, detail={"status": "rejected", "campaign_id": campaign["campaign_id"], "violations": violations})
    return {"campaign": campaign}


@app.get("/campaigns")
async def campaigns(limit: int = 20, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_chaos_campaigns(limit, status)
    return {"campaigns": [_decode_campaign(row) for row in rows], "count": len(rows)}


@app.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str) -> dict[str, Any]:
    campaign = await _load_campaign(campaign_id)
    return {"campaign": campaign}


@app.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: str, request: Request, payload: ChaosCampaignStartRequest | None = None) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="start chaos campaign")
    payload = payload or ChaosCampaignStartRequest()
    campaign = await _load_campaign(campaign_id)
    if campaign["status"] == ChaosCampaignStatus.PAUSED.value:
        run = await _record_campaign_run(campaign, payload, "paused", [], "Campaign is paused")
        return {"run": run, "report": run["report"], "steps": []}
    if campaign["status"] == ChaosCampaignStatus.STOPPED.value:
        run = await _record_campaign_run(campaign, payload, "stopped", [], "Campaign is stopped")
        return {"run": run, "report": run["report"], "steps": []}
    run_id = "chaos_campaign_run_" + uuid.uuid4().hex[:16]
    dry_run = campaign["dry_run"] if payload.dry_run is None else bool(payload.dry_run)
    if not campaign.get("local_demo_execution_enabled"):
        dry_run = True
    max_experiments = min(int(payload.max_experiments or campaign["max_experiments_per_run"]), int(campaign["max_experiments_per_run"]))
    run = _campaign_run_dict(run_id, campaign, payload, "running", dry_run, [], "")
    await _insert_campaign_run(run)
    steps: list[dict[str, Any]] = []
    try:
        for index, template in enumerate(campaign["experiment_templates"][:max_experiments], start=1):
            step = await _run_campaign_template(campaign, run, payload, template, index, dry_run)
            steps.append(step)
            if campaign.get("cooldown_seconds", 0):
                await asyncio.sleep(min(int(campaign["cooldown_seconds"]), 2))
        status_ = "completed_with_blocks" if any(step["status"] == "blocked" for step in steps) else "completed"
        if any(step["status"] == "failed" for step in steps):
            status_ = "failed"
        run = await _finalize_campaign_run(run, campaign, payload, status_, steps, "")
    except Exception as exc:
        run = await _finalize_campaign_run(run, campaign, payload, "failed", steps, str(exc))
    return {"run": run, "report": run["report"], "steps": steps}


@app.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="pause chaos campaign")
    campaign = await _update_campaign_status(campaign_id, ChaosCampaignStatus.PAUSED.value)
    return {"campaign": campaign}


@app.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="resume chaos campaign")
    campaign = await _update_campaign_status(campaign_id, ChaosCampaignStatus.ACTIVE.value)
    return {"campaign": campaign}


@app.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="stop chaos campaign")
    campaign = await _update_campaign_status(campaign_id, ChaosCampaignStatus.STOPPED.value)
    return {"campaign": campaign}


@app.get("/campaign-runs")
async def campaign_runs(limit: int = 20, campaign_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_chaos_campaign_runs(limit, campaign_id, status)
    return {"runs": [_decode_campaign_run(row) for row in rows], "count": len(rows)}


@app.get("/campaigns/{campaign_id}/runs")
async def campaign_runs_for_campaign(campaign_id: str, limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_chaos_campaign_runs(limit, campaign_id, None)
    return {"runs": [_decode_campaign_run(row) for row in rows], "count": len(rows)}


@app.get("/campaign-runs/{run_id}/report")
async def campaign_report(run_id: str) -> dict[str, Any]:
    row = await clickhouse.chaos_campaign_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign run not found")
    steps = await clickhouse.chaos_campaign_steps(run_id)
    run = _decode_campaign_run(row)
    return {"run": run, "report": run.get("report", {}), "steps": [_decode_campaign_step(step) for step in steps]}


def _campaign_dict(payload: ChaosCampaignRequest) -> dict[str, Any]:
    now = _now()
    campaign = payload.model_dump()
    return {
        "campaign_id": "chaos_campaign_" + uuid.uuid4().hex[:16],
        "created_at": now,
        "updated_at": now,
        "status": ChaosCampaignStatus.ACTIVE.value,
        **campaign,
        "next_run_at": _next_run_at(campaign.get("schedule", {})),
        "validation_violations": [],
    }


def _validate_campaign(campaign: dict[str, Any]) -> list[str]:
    policy = default_policy()
    violations: list[str] = []
    namespace = str(campaign.get("target_namespace", ""))
    allowed_services = [str(item) for item in campaign.get("allowed_services", [])]
    if namespace in policy.denied_namespaces or namespace not in policy.allowed_namespaces:
        violations.append(f"Namespace '{namespace}' is not allowed for chaos campaigns")
    if not allowed_services:
        violations.append("At least one allowed service is required")
    for service in allowed_services:
        if service in policy.protected_services or service in policy.denied_services or service not in policy.allowed_services:
            violations.append(f"Service '{service}' is not allowed for chaos campaigns")
    for template in campaign.get("experiment_templates", []):
        service = str(template.get("target_service") or allowed_services[0] if allowed_services else "")
        kind = str(template.get("experiment_kind") or "")
        if service not in allowed_services:
            violations.append(f"Template service '{service}' is outside campaign allowed_services")
        if kind not in policy.supported_kinds:
            violations.append(f"Template experiment kind '{kind}' is unsupported")
        if int(template.get("duration_seconds") or 0) > policy.max_duration_seconds:
            violations.append(f"Template duration exceeds policy max {policy.max_duration_seconds}s")
    return violations


async def _load_campaign(campaign_id: str) -> dict[str, Any]:
    row = await clickhouse.chaos_campaign(campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _decode_campaign(row)


async def _update_campaign_status(campaign_id: str, status_: str) -> dict[str, Any]:
    campaign = await _load_campaign(campaign_id)
    campaign["status"] = status_
    campaign["updated_at"] = _now()
    await _insert_campaign(campaign)
    return campaign


async def _run_campaign_template(
    campaign: dict[str, Any],
    campaign_run: dict[str, Any],
    payload: ChaosCampaignStartRequest,
    template: dict[str, Any],
    sequence: int,
    dry_run: bool,
) -> dict[str, Any]:
    service = str(template.get("target_service") or (campaign["allowed_services"][0] if campaign["allowed_services"] else ""))
    before = await _campaign_evidence(service, campaign["target_namespace"])
    plan_request = ChaosPlanRequest(
        objective=str(template.get("objective") or campaign.get("name") or "Chaos campaign experiment"),
        target_service=service,
        target_namespace=campaign["target_namespace"],
        experiment_kind=str(template.get("experiment_kind") or "pod_kill"),
        duration_seconds=int(template.get("duration_seconds") or 30),
        dry_run=True,
    )
    step_base = {
        "step_id": "chaos_campaign_step_" + uuid.uuid4().hex[:16],
        "run_id": campaign_run["run_id"],
        "campaign_id": campaign["campaign_id"],
        "created_at": _now(),
        "sequence": sequence,
        "experiment_kind": plan_request.experiment_kind,
        "target_service": service,
        "evidence_before": before,
        "evidence_after": {},
        "cleanup_status": "not_required",
    }
    try:
        plan = await _build_plan(plan_request)
        safety = validate_plan(plan, default_policy(), dry_run=True)
        plan.update({"status": "ready" if safety.allowed else "rejected", "safety_score": safety.safety_score, "risk_level": safety.risk_level, "safety_findings": safety.findings + safety.violations})
        await _insert_plan(plan)
        await _audit(plan, safety)
        blocked_reason = ""
        if not safety.allowed:
            blocked_reason = "; ".join(safety.violations)
        elif float(plan["blast_radius_score"]) > float(campaign["blast_radius_limit"]):
            blocked_reason = f"Blast radius {plan['blast_radius_score']} exceeds campaign limit {campaign['blast_radius_limit']}"
        elif service not in campaign["allowed_services"]:
            blocked_reason = f"Service '{service}' is outside campaign allowed_services"
        if blocked_reason:
            step = {**step_base, "status": "blocked", "plan_id": plan["plan_id"], "chaos_run_id": "", "blocked_reason": blocked_reason, "result": {"safety": safety.model_dump(), "plan": _compact(plan)}}
            await _insert_campaign_step(step)
            return step
        run_result = await _execute_campaign_plan(plan["plan_id"], payload, dry_run)
        chaos_run_id = str(run_result.get("run_id") or "")
        cleanup_status = str(run_result.get("cleanup_status") or ("not_required" if dry_run else "unknown"))
        after = await _campaign_evidence(service, campaign["target_namespace"])
        step = {
            **step_base,
            "status": "completed" if str(run_result.get("status", "")).lower() in {"dry_run", "completed"} else str(run_result.get("status") or "completed"),
            "plan_id": plan["plan_id"],
            "chaos_run_id": chaos_run_id,
            "blocked_reason": "",
            "evidence_after": after,
            "cleanup_status": cleanup_status,
            "result": {"run": _compact(run_result), "safety": safety.model_dump(), "plan": _compact(plan)},
        }
    except Exception as exc:
        after = await _campaign_evidence(service, campaign["target_namespace"])
        step = {**step_base, "status": "failed", "plan_id": "", "chaos_run_id": "", "blocked_reason": str(exc), "evidence_after": after, "cleanup_status": "cleanup_delegated_to_executor", "result": {"error": str(exc)}}
    await _insert_campaign_step(step)
    return step


async def _execute_campaign_plan(plan_id: str, payload: ChaosCampaignStartRequest, dry_run: bool) -> dict[str, Any]:
    body = {
        "plan_id": plan_id,
        "approval_id": payload.approval_id,
        "approved": payload.approved,
        "dry_run": dry_run,
        "observation_window_seconds": payload.observation_window_seconds,
        "trigger_agent_investigation": payload.trigger_agent_investigation,
    }
    async with httpx.AsyncClient(timeout=max(settings.request_timeout_seconds, 120.0)) as client:
        response = await client.post(f"{settings.chaos_executor_service_url.rstrip('/')}/runs", json=body)
        if response.status_code >= 400:
            try:
                return {"status": "blocked", "error": response.json(), "plan_id": plan_id}
            except Exception:
                return {"status": "blocked", "error": response.text, "plan_id": plan_id}
        data = response.json()
        return data if isinstance(data, dict) else {"payload": data}


async def _campaign_evidence(service: str, namespace: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        events = await _optional_get(client, settings.retrieval_service_url, "/events/recent", {"service": service, "namespace": namespace, "limit": 25})
        anomalies = await _optional_get(client, settings.retrieval_service_url, "/anomalies/recent", {"service": service, "limit": 25})
        rca = await _optional_post(client, settings.retrieval_service_url, "/rca/analyze", {"target_service": service, "limit": 25})
    return {
        "service": service,
        "namespace": namespace,
        "telemetry_events": len(events.get("events", [])) if isinstance(events, dict) else 0,
        "anomalies": len(anomalies.get("anomalies", [])) if isinstance(anomalies, dict) else 0,
        "latest_rca": _compact(rca.get("report", rca) if isinstance(rca, dict) else {}),
    }


async def _optional_get(client: httpx.AsyncClient, base_url: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.get(base_url.rstrip("/") + path, params={k: v for k, v in params.items() if v not in (None, "")})
        if response.status_code < 500:
            data = response.json()
            return data if isinstance(data, dict) else {"payload": data}
    except Exception:
        return {}
    return {}


async def _optional_post(client: httpx.AsyncClient, base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await client.post(base_url.rstrip("/") + path, json=payload)
        if response.status_code < 500:
            data = response.json()
            return data if isinstance(data, dict) else {"payload": data}
    except Exception:
        return {}
    return {}


async def _record_campaign_run(campaign: dict[str, Any], payload: ChaosCampaignStartRequest, status_: str, steps: list[dict[str, Any]], error: str) -> dict[str, Any]:
    run = _campaign_run_dict("chaos_campaign_run_" + uuid.uuid4().hex[:16], campaign, payload, status_, campaign["dry_run"] if payload.dry_run is None else bool(payload.dry_run), steps, error)
    await _insert_campaign_run(run)
    return run


async def _finalize_campaign_run(run: dict[str, Any], campaign: dict[str, Any], payload: ChaosCampaignStartRequest, status_: str, steps: list[dict[str, Any]], error: str) -> dict[str, Any]:
    final = _campaign_run_dict(run["run_id"], campaign, payload, status_, bool(run["dry_run"]), steps, error)
    final["started_at"] = run["started_at"]
    await _insert_campaign_run(final)
    return final


def _campaign_run_dict(run_id: str, campaign: dict[str, Any], payload: ChaosCampaignStartRequest, status_: str, dry_run: bool, steps: list[dict[str, Any]], error: str) -> dict[str, Any]:
    completed = _now() if status_ != "running" else None
    report = _campaign_report(campaign, steps, status_, error)
    return {
        "run_id": run_id,
        "campaign_id": campaign["campaign_id"],
        "started_at": _now(),
        "completed_at": completed,
        "status": status_,
        "dry_run": dry_run,
        "requested_by": payload.requested_by,
        "experiments_attempted": len([step for step in steps if step.get("status") != "blocked"]),
        "experiments_succeeded": len([step for step in steps if step.get("status") == "completed"]),
        "experiments_blocked": len([step for step in steps if step.get("status") == "blocked"]),
        "experiments_failed": len([step for step in steps if step.get("status") == "failed"]),
        "services": sorted({str(step.get("target_service") or "") for step in steps if step.get("target_service")}),
        "report": report,
        "error_message": error,
    }


def _campaign_report(campaign: dict[str, Any], steps: list[dict[str, Any]], status_: str, error: str) -> dict[str, Any]:
    blocked = [step for step in steps if step.get("status") == "blocked"]
    failed = [step for step in steps if step.get("status") == "failed"]
    completed = [step for step in steps if step.get("status") == "completed"]
    recommendations = ["Keep campaigns dry-run by default and expand only one variable at a time."]
    if blocked:
        recommendations.append("Review blocked campaign templates before increasing cadence or blast radius.")
    if failed:
        recommendations.append("Verify Chaos Mesh cleanup and target health before rerunning failed templates.")
    return {
        "campaign_id": campaign["campaign_id"],
        "campaign_name": campaign["name"],
        "status": status_,
        "experiments_run": len(completed),
        "services_targeted": sorted({str(step.get("target_service") or "") for step in steps if step.get("target_service")}),
        "failures_observed": len(failed),
        "recovery_times": [],
        "rca_summaries": [step.get("evidence_after", {}).get("latest_rca", {}) for step in steps if step.get("evidence_after")],
        "resilience_score_changes": [],
        "recommendations_created": recommendations,
        "skipped_or_blocked": [{"service": step.get("target_service"), "reason": step.get("blocked_reason")} for step in blocked],
        "error": error,
    }


async def _insert_campaign(campaign: dict[str, Any]) -> None:
    await clickhouse.insert_chaos_campaign({
        "campaign_id": campaign["campaign_id"],
        "created_at": campaign["created_at"],
        "updated_at": campaign["updated_at"],
        "status": campaign["status"],
        "name": campaign["name"],
        "target_namespace": campaign["target_namespace"],
        "allowed_services_json": _json(campaign["allowed_services"]),
        "experiment_templates_json": _json(campaign["experiment_templates"]),
        "schedule_json": _json(campaign["schedule"]),
        "max_experiments_per_run": campaign["max_experiments_per_run"],
        "blast_radius_limit": campaign["blast_radius_limit"],
        "cooldown_seconds": campaign["cooldown_seconds"],
        "dry_run": 1 if campaign["dry_run"] else 0,
        "local_demo_execution_enabled": 1 if campaign["local_demo_execution_enabled"] else 0,
        "stop_conditions_json": _json(campaign["stop_conditions"]),
        "campaign_json": _json(campaign),
    })
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            f"campaign.{campaign['status']}",
            "campaigns",
            severity="warning" if campaign.get("validation_violations") else "info",
            payload=campaign,
            correlation_id=campaign["campaign_id"],
            namespace=campaign["target_namespace"],
            actor="cascade-operator",
            action="chaos_campaign",
            status=campaign["status"],
            campaign_id=campaign["campaign_id"],
            evidence_summary="; ".join(campaign.get("validation_violations", [])[:3]),
            user_safe_message=f"Chaos campaign {campaign['name']} is {campaign['status']}",
        ),
    )


async def _insert_campaign_run(run: dict[str, Any]) -> None:
    await clickhouse.insert_chaos_campaign_run({
        "run_id": run["run_id"],
        "campaign_id": run["campaign_id"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "status": run["status"],
        "dry_run": 1 if run["dry_run"] else 0,
        "requested_by": run["requested_by"],
        "experiments_attempted": run["experiments_attempted"],
        "experiments_succeeded": run["experiments_succeeded"],
        "experiments_blocked": run["experiments_blocked"],
        "experiments_failed": run["experiments_failed"],
        "services_json": _json(run["services"]),
        "report_json": _json(run["report"]),
        "error_message": run["error_message"],
        "run_json": _json(run),
    })
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            f"campaign.run.{run['status']}",
            "campaigns",
            severity="error" if run["status"] == "failed" else ("warning" if "block" in run["status"] or run["experiments_blocked"] else "info"),
            payload=run,
            run_id=run["run_id"],
            correlation_id=run["campaign_id"],
            actor=run.get("requested_by") or "cascade-operator",
            action="chaos_campaign_run",
            status=run["status"],
            campaign_id=run["campaign_id"],
            evidence_summary=str(run.get("report", {}).get("recommendations_created", [""])[0] if isinstance(run.get("report"), dict) else ""),
            user_safe_message=f"Chaos campaign run {run['status']}",
        ),
    )


async def _insert_campaign_step(step: dict[str, Any]) -> None:
    await clickhouse.insert_chaos_campaign_step({
        "step_id": step["step_id"],
        "run_id": step["run_id"],
        "campaign_id": step["campaign_id"],
        "created_at": step["created_at"],
        "sequence": step["sequence"],
        "status": step["status"],
        "plan_id": step["plan_id"],
        "chaos_run_id": step["chaos_run_id"],
        "experiment_kind": step["experiment_kind"],
        "target_service": step["target_service"],
        "blocked_reason": step["blocked_reason"],
        "evidence_before_json": _json(step.get("evidence_before", {})),
        "evidence_after_json": _json(step.get("evidence_after", {})),
        "result_json": _json(step.get("result", {})),
        "cleanup_status": step["cleanup_status"],
        "step_json": _json(step),
    })
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            f"campaign.step.{step['status']}",
            "campaigns",
            severity="error" if step["status"] == "failed" else ("warning" if step["status"] == "blocked" else "info"),
            payload=step,
            run_id=step["run_id"],
            correlation_id=step["campaign_id"],
            service=step["target_service"],
            action=step["experiment_kind"],
            status=step["status"],
            campaign_id=step["campaign_id"],
            chaos_experiment_id=step.get("chaos_run_id", ""),
            evidence_summary=step.get("blocked_reason", ""),
            user_safe_message=f"Campaign step {step['status']} for {step['target_service']}",
        ),
    )


async def _build_plan(payload: ChaosPlanRequest) -> dict[str, Any]:
    plan_id = "chaos_plan_" + uuid.uuid4().hex[:16]
    experiment_id = "chaos_exp_" + uuid.uuid4().hex[:16]
    manifest = build_manifest(payload.experiment_kind, experiment_id, payload.target_namespace, payload.target_service, payload.duration_seconds)
    blast = await _blast_radius(payload.target_service)
    selector = manifest.get("spec", {}).get("selector", {})
    return {
        "plan_id": plan_id,
        "experiment_id": experiment_id,
        "status": "draft",
        "plan_type": "controlled-chaos",
        "experiment_kind": payload.experiment_kind,
        "target_namespace": payload.target_namespace,
        "target_service": payload.target_service,
        "target_workload": payload.target_workload or "",
        "target_selector": selector,
        "duration_seconds": payload.duration_seconds,
        "blast_radius_score": blast["score"],
        "safety_score": 0.0,
        "risk_level": "unknown",
        "objective": payload.objective,
        "hypothesis": f"If {payload.target_service} loses one pod, user impact should remain bounded and recover automatically.",
        "expected_impact": f"At most one {payload.target_service} pod is disrupted; downstream blast radius estimate: {blast['affected_services']}.",
        "safety_policy": default_policy().model_dump(),
        "manifest": manifest,
        "safety_findings": [],
    }


async def _blast_radius(service: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(f"{settings.topology_service_url.rstrip('/')}/topology/impact", json={"root_service": service})
            if response.status_code < 500:
                data = response.json()
                affected = data.get("affected_services", [])
                return {"score": round(min(1.0, 0.1 + 0.1 * len(affected)), 2), "affected_services": affected}
    except Exception:
        pass
    return {"score": 0.25, "affected_services": []}


async def _insert_plan(plan: dict[str, Any]) -> None:
    now = _now()
    await clickhouse.insert_chaos_experiment_plan({
        "plan_id": plan["plan_id"],
        "created_at": now,
        "updated_at": now,
        "status": plan["status"],
        "plan_type": plan["plan_type"],
        "experiment_kind": plan["experiment_kind"],
        "target_namespace": plan["target_namespace"],
        "target_service": plan["target_service"],
        "target_workload": plan["target_workload"],
        "target_selector_json": _json(plan["target_selector"]),
        "duration_seconds": plan["duration_seconds"],
        "blast_radius_score": plan["blast_radius_score"],
        "safety_score": plan["safety_score"],
        "risk_level": plan["risk_level"],
        "objective": plan["objective"],
        "hypothesis": plan["hypothesis"],
        "expected_impact": plan["expected_impact"],
        "safety_policy_json": _json(plan["safety_policy"]),
        "manifest_json": _json(plan["manifest"]),
        "plan_json": _json(plan),
    })
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            "chaos.plan.created",
            "chaos",
            severity="warning" if plan["status"] == "rejected" else "info",
            payload=plan,
            correlation_id=plan["plan_id"],
            service=plan["target_service"],
            namespace=plan["target_namespace"],
            action=plan["experiment_kind"],
            status=plan["status"],
            risk_level=plan["risk_level"],
            chaos_experiment_id=plan["experiment_id"],
            evidence_summary="; ".join(plan.get("safety_findings", [])[:3]),
            user_safe_message=f"Chaos plan {plan['status']} for {plan['target_service']}",
        ),
    )


async def _insert_violation(plan_id: str, run_id: str, kind: str, severity: str, message: str, request: dict[str, Any]) -> None:
    await clickhouse.insert_chaos_safety_violation({
        "violation_id": "chaos_violation_" + uuid.uuid4().hex[:16],
        "created_at": _now(),
        "plan_id": plan_id,
        "run_id": run_id,
        "violation_type": kind,
        "severity": severity,
        "message": message,
        "policy_json": _json(default_policy().model_dump()),
        "request_json": _json(request),
    })


async def _audit(plan: dict[str, Any], result: Any) -> None:
    audit_id = "chaos_audit_" + uuid.uuid4().hex[:16]
    await clickhouse.insert_chaos_policy_audit({"audit_id": audit_id, "checked_at": _now(), "plan_id": plan["plan_id"], "experiment_kind": plan["experiment_kind"], "namespace": plan["target_namespace"], "service": plan["target_service"], "allowed": 1 if result.allowed else 0, "risk_level": result.risk_level, "findings_json": _json(result.findings + result.violations), "policy_json": _json(default_policy().model_dump())})
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            "policy.evaluated",
            "policy",
            severity="info" if result.allowed else "warning",
            payload={"plan": plan, "findings": result.findings, "violations": result.violations},
            correlation_id=plan["plan_id"],
            service=plan["target_service"],
            namespace=plan["target_namespace"],
            action=plan["experiment_kind"],
            status="allowed" if result.allowed else "blocked",
            risk_level=result.risk_level,
            policy_decision_id=audit_id,
            chaos_experiment_id=plan["experiment_id"],
            evidence_summary="; ".join((result.findings + result.violations)[:3]),
            user_safe_message=f"Chaos policy {'allowed' if result.allowed else 'blocked'} {plan['experiment_kind']} for {plan['target_service']}",
        ),
    )


async def _check(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(base_url.rstrip("/") + "/ready")
            if response.status_code == 404:
                response = await client.get(base_url.rstrip("/") + "/health")
            return response.status_code < 500
    except Exception:
        return False


def _decode_plan(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["target_selector"] = _loads(decoded.pop("target_selector_json", "{}"))
    decoded["safety_policy"] = _loads(decoded.pop("safety_policy_json", "{}"))
    decoded["manifest"] = _loads(decoded.pop("manifest_json", "{}"))
    decoded["plan"] = _loads(decoded.pop("plan_json", "{}"))
    return decoded


def _decode_campaign(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["allowed_services"] = _loads(decoded.pop("allowed_services_json", "[]"))
    decoded["experiment_templates"] = _loads(decoded.pop("experiment_templates_json", "[]"))
    decoded["schedule"] = _loads(decoded.pop("schedule_json", "{}"))
    decoded["stop_conditions"] = _loads(decoded.pop("stop_conditions_json", "{}"))
    decoded["campaign"] = _loads(decoded.pop("campaign_json", "{}"))
    decoded["dry_run"] = bool(decoded.get("dry_run"))
    decoded["local_demo_execution_enabled"] = bool(decoded.get("local_demo_execution_enabled"))
    if isinstance(decoded["campaign"], dict):
        decoded["next_run_at"] = decoded["campaign"].get("next_run_at", "")
        decoded["validation_violations"] = decoded["campaign"].get("validation_violations", [])
    return decoded


def _decode_campaign_run(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["dry_run"] = bool(decoded.get("dry_run"))
    decoded["services"] = _loads(decoded.pop("services_json", "[]"))
    decoded["report"] = _loads(decoded.pop("report_json", "{}"))
    decoded["run"] = _loads(decoded.pop("run_json", "{}"))
    return decoded


def _decode_campaign_step(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["evidence_before"] = _loads(decoded.pop("evidence_before_json", "{}"))
    decoded["evidence_after"] = _loads(decoded.pop("evidence_after_json", "{}"))
    decoded["result"] = _loads(decoded.pop("result_json", "{}"))
    decoded["step"] = _loads(decoded.pop("step_json", "{}"))
    return decoded


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": plan["plan_id"],
        "status": plan["status"],
        "risk_level": plan["risk_level"],
        "safety_score": plan["safety_score"],
        "blast_radius_score": plan["blast_radius_score"],
        "manifest": plan["manifest"],
        "safety_findings": plan["safety_findings"],
        "plan": plan,
    }


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _compact(value: Any, max_items: int = 8) -> Any:
    if isinstance(value, dict):
        return {str(k): _compact(v, max_items) for k, v in list(value.items())[:max_items]}
    if isinstance(value, list):
        return [_compact(item, max_items) for item in value[:max_items]]
    return value


def _next_run_at(schedule: dict[str, Any]) -> str:
    if str(schedule.get("trigger", "manual")) == "manual":
        return ""
    interval = int(schedule.get("interval_seconds") or 0)
    if interval <= 0:
        return ""
    return datetime.fromtimestamp(datetime.now(UTC).timestamp() + interval, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )
