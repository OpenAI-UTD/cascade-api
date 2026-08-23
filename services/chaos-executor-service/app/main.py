from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.chaos.events import chaos_event
from services.shared.chaos.kubernetes_client import KubernetesChaosClient
from services.shared.chaos.observation import collect_observation
from services.shared.chaos.safety import default_policy, validate_plan
from services.shared.chaos.schemas import ChaosRunRequest
from services.shared.chaos.scoring import compute_resilience_score
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.live_demo import LiveDemoConfig, validate_live_demo_gate
from services.shared.security.approval import approval_valid_for_plan
from services.shared.security.auth import AuthSettings, auth_status, binding_guard, require_auth
from services.shared.storage.clickhouse_client import ClickHouseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    chaos_planner_service_url: str = "http://chaos-planner-service.cascade-system.svc.cluster.local:8019"
    retrieval_service_url: str = "http://retrieval-service.cascade-system.svc.cluster.local:8012"
    agent_orchestrator_service_url: str = "http://agent-orchestrator-service.cascade-system.svc.cluster.local:8018"
    experiment_tracker_service_url: str = "http://experiment-tracker-service.cascade-system.svc.cluster.local:8002"
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    chaos_events_topic: str = "chaos.experiments"
    chaos_publish_events: bool = True
    request_timeout_seconds: float = 20.0
    enable_dangerous_actions: bool = False
    enable_real_chaos: bool = False
    cascade_live_demo_mode: bool = False
    cascade_allowed_cluster_context: str = "kind-cascade"
    cascade_active_cluster_context: str = ""
    cascade_allowed_target_namespace: str = "cascade-targets"
    cascade_require_approval: bool = True
    cascade_require_dry_run_first: bool = True
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"
    cascade_approval_signing_secret: str = ""

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="chaos-executor-service"))
k8s: KubernetesChaosClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global k8s
    await clickhouse.initialize_schema()
    try:
        k8s = KubernetesChaosClient()
    except Exception as exc:
        logger.warning("Kubernetes client unavailable at startup: %s", exc)
        k8s = None
    if settings.chaos_publish_events:
        await producer.start()
    try:
        yield
    finally:
        if settings.chaos_publish_events:
            await producer.stop()


app = FastAPI(title="Cascade Chaos Executor Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "chaos-executor-service", **_live_status(), **auth_status(_auth_settings())}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    kube = bool(k8s and k8s.ready())
    chaos = bool(k8s and k8s.chaos_crds_ready())
    response = {"status": "ok" if ch and kube and chaos else "degraded", "service": "chaos-executor-service", "clickhouse": ch, "kubernetes": kube, "chaos_mesh_crds": chaos, "event_publishing": settings.chaos_publish_events, **_live_status()}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.get("/safety/policy")
async def safety_policy() -> dict[str, Any]:
    policy = default_policy()
    data = policy.model_dump()
    data.update(_live_status())
    data["denied"] = {"namespaces": policy.denied_namespaces, "services": policy.denied_services, "resource_kinds": policy.denied_resource_kinds}
    data["protected"] = {"services": policy.protected_services}
    return data


@app.post("/runs")
async def start_run(payload: ChaosRunRequest, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="execute chaos run")
    plan = await _load_plan(payload.plan_id)
    approval = await _load_approval(payload.approval_id, plan["plan_id"])
    approval_current, approval_violations = approval_valid_for_plan(approval, plan, signing_secret=settings.cascade_approval_signing_secret)
    safety = validate_plan(plan, default_policy(), approved=payload.approved or approval_current, dry_run=payload.dry_run)
    await _audit(plan, safety)
    if not safety.allowed:
        await _insert_violation(plan["plan_id"], "", "execution_rejected", "high", "; ".join(safety.violations), payload.model_dump())
        await _publish("chaos.plan.rejected", plan, {}, None, "; ".join(safety.violations))
        raise HTTPException(status_code=400, detail={"status": "rejected", "violations": safety.violations})

    run_id = "chaos_run_" + uuid.uuid4().hex[:16]
    run = _run_dict(run_id, plan, payload, "dry_run" if payload.dry_run else "running", "not_required", "")
    await _insert_run(run)
    await _publish("chaos.run.started", plan, run, None, f"Chaos run {run_id} started")

    if payload.dry_run:
        await _publish("chaos.run.completed", plan, run, None, "Dry-run completed without creating Chaos Mesh resources")
        return {"run_id": run_id, "plan_id": plan["plan_id"], "status": "dry_run", "dry_run": True, "manifest": plan["manifest"], "safety": safety.model_dump()}

    if k8s is None:
        raise HTTPException(status_code=503, detail="Kubernetes client unavailable")

    dry_run_passed = await _dry_run_passed(plan["plan_id"])
    live_violations = validate_live_demo_gate(
        action="ENABLE_REAL_CHAOS",
        namespace=plan["target_namespace"],
        service=plan["target_service"],
        config=_live_config(),
        feature_enabled=settings.enable_real_chaos,
        approval_current=approval_current,
        dry_run_passed=dry_run_passed,
        current_context=k8s.current_context(),
    )
    if approval_violations and settings.cascade_require_approval:
        live_violations = approval_violations + live_violations
    if live_violations:
        execution = _run_dict(run_id, plan, payload, "rejected", "not_required", "; ".join(live_violations))
        await _insert_run(execution)
        await _insert_violation(plan["plan_id"], run_id, "live_demo_gate_rejected", "high", "; ".join(live_violations), payload.model_dump())
        await _publish("chaos.run.failed", plan, execution, None, "; ".join(live_violations))
        raise HTTPException(status_code=400, detail={"status": "rejected", "violations": live_violations, "run_id": run_id})

    resource = None
    cleanup_status = "not_started"
    error = ""
    observation: dict[str, Any] | None = None
    score: dict[str, Any] | None = None
    try:
        await _create_experiment_record(plan)
        resource = k8s.create(plan["manifest"])
        run["chaos_resource_uid"] = resource.get("metadata", {}).get("uid", "")
        run["status"] = "running"
        await _insert_run(run)
        await asyncio.sleep(min(int(plan["duration_seconds"]), 120))
        cleanup_status = k8s.delete(plan["manifest"]["kind"], plan["target_namespace"], plan["manifest"]["metadata"]["name"])
        run["cleanup_status"] = cleanup_status
        run["status"] = "completed"
        run["completed_at"] = _now()
        await _complete_experiment_record(plan)
        await _insert_run(run)
        await _publish("chaos.run.cleaned_up", plan, run, None, cleanup_status)
        observation = await _observe(run, plan, payload.observation_window_seconds, payload.trigger_agent_investigation)
        score = await _score(run, plan, observation)
        await _publish("chaos.observation.completed", plan, run, score, "Observation completed")
        await _publish("chaos.resilience.scored", plan, run, score, score["explanation"])
        await _publish("chaos.run.completed", plan, run, score, "Chaos run completed")
    except Exception as exc:
        error = str(exc)
        logger.exception("Chaos run failed id=%s: %s", run_id, exc)
        run["status"] = "failed"
        run["error_message"] = error
        try:
            cleanup_status = k8s.delete(plan["manifest"]["kind"], plan["target_namespace"], plan["manifest"]["metadata"]["name"]) if k8s else "cleanup_unavailable"
        except Exception as cleanup_exc:
            cleanup_status = f"cleanup_failed: {cleanup_exc}"
        run["cleanup_status"] = cleanup_status
        run["completed_at"] = _now()
        await _insert_run(run)
        await _publish("chaos.run.failed", plan, run, None, error)
    if error:
        raise HTTPException(status_code=500, detail={"error": error, "cleanup_status": cleanup_status})
    return {"run_id": run_id, "plan_id": plan["plan_id"], "status": run["status"], "cleanup_status": cleanup_status, "observation": observation, "score": score}


@app.get("/runs")
async def runs(limit: int = 20, status: str | None = None, service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_chaos_runs(limit, service, status)
    return {"runs": [_decode_run(row) for row in rows], "count": len(rows)}


@app.get("/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    detail = await clickhouse.chaos_run_detail(run_id)
    if detail["run"] is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _decode_detail(detail)


@app.post("/runs/{run_id}/cleanup")
async def cleanup(run_id: str, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="cleanup chaos run")
    detail = await clickhouse.chaos_run_detail(run_id)
    if detail["run"] is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _decode_run(detail["run"])
    plan = await _load_plan(run["plan_id"])
    status_ = k8s.delete(plan["manifest"]["kind"], plan["target_namespace"], plan["manifest"]["metadata"]["name"]) if k8s else "kubernetes_unavailable"
    return {"run_id": run_id, "cleanup_status": status_}


@app.post("/runs/{run_id}/observe")
async def observe_again(run_id: str, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="observe chaos run")
    detail = await clickhouse.chaos_run_detail(run_id)
    if detail["run"] is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _decode_run(detail["run"])
    plan = await _load_plan(run["plan_id"])
    observation = await _observe(run, plan, 60, False)
    score = await _score(run, plan, observation)
    return {"run_id": run_id, "observation": observation, "score": score}


@app.get("/scores/recent")
async def scores_recent(limit: int = 20, service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_resilience_scores(limit, service)
    return {"scores": [_decode_score(row) for row in rows], "count": len(rows)}


async def _load_plan(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.chaos_plan(plan_id)
    if row is None:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(f"{settings.chaos_planner_service_url.rstrip('/')}/plans/{plan_id}")
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Plan not found")
            response.raise_for_status()
            return response.json()["plan"] if "plan" in response.json() else response.json()
    return _decode_plan(row)["plan"]


async def _load_approval(approval_id: str, plan_id: str) -> dict[str, Any] | None:
    if not approval_id:
        return None
    approval = await clickhouse.remediation_approval(approval_id)
    if not approval or approval.get("plan_id") != plan_id:
        return None
    return approval


async def _dry_run_passed(plan_id: str) -> bool:
    row = await clickhouse.latest_chaos_dry_run(plan_id)
    return bool(row)


def _run_dict(run_id: str, plan: dict[str, Any], payload: ChaosRunRequest, status_: str, cleanup: str, error: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "plan_id": plan["plan_id"],
        "experiment_id": plan["experiment_id"],
        "started_at": _now(),
        "completed_at": _now() if status_ in {"dry_run", "failed", "completed"} else None,
        "status": status_,
        "dry_run": payload.dry_run,
        "approved": payload.approved,
        "experiment_kind": plan["experiment_kind"],
        "target_namespace": plan["target_namespace"],
        "target_service": plan["target_service"],
        "target_workload": plan["target_workload"],
        "chaos_resource_name": plan["manifest"]["metadata"]["name"],
        "chaos_resource_uid": "",
        "duration_seconds": plan["duration_seconds"],
        "cleanup_status": cleanup,
        "error_message": error,
    }


async def _insert_run(run: dict[str, Any]) -> None:
    await clickhouse.insert_chaos_experiment_run({**run, "dry_run": 1 if run["dry_run"] else 0, "approved": 1 if run["approved"] else 0, "run_json": _json(run)})


async def _observe(run: dict[str, Any], plan: dict[str, Any], window: int, trigger_agent: bool) -> dict[str, Any]:
    await asyncio.sleep(min(window, 10))
    observation = await collect_observation(settings.retrieval_service_url, settings.agent_orchestrator_service_url, plan["target_service"], plan["target_namespace"], window, trigger_agent)
    await clickhouse.insert_chaos_observation({
        "observation_id": "chaos_obs_" + uuid.uuid4().hex[:16],
        "run_id": run["run_id"],
        "plan_id": plan["plan_id"],
        "observed_at": _now(),
        "observation_window_seconds": observation["observation_window_seconds"],
        "telemetry_events_count": observation["telemetry_events_count"],
        "anomaly_events_count": observation["anomaly_events_count"],
        "incidents_count": observation["incidents_count"],
        "investigation_id": observation["investigation_id"],
        "affected_services_json": _json(observation["affected_services"]),
        "telemetry_summary_json": _json(observation["telemetry_summary"]),
        "anomaly_summary_json": _json(observation["anomaly_summary"]),
        "incident_summary_json": _json(observation["incident_summary"]),
        "observation_json": _json(observation),
    })
    return observation


async def _score(run: dict[str, Any], plan: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    score = compute_resilience_score(run, observation, plan)
    await clickhouse.insert_resilience_score({
        "score_id": "chaos_score_" + uuid.uuid4().hex[:16],
        "run_id": run["run_id"],
        "plan_id": plan["plan_id"],
        "computed_at": _now(),
        "service": plan["target_service"],
        "namespace": plan["target_namespace"],
        "experiment_kind": plan["experiment_kind"],
        "resilience_score": score["resilience_score"],
        "recovery_score": score["recovery_score"],
        "blast_radius_score": score["blast_radius_score"],
        "anomaly_penalty": score["anomaly_penalty"],
        "incident_penalty": score["incident_penalty"],
        "evidence_score": score["evidence_score"],
        "grade": score["grade"],
        "explanation": score["explanation"],
        "recommendations_json": _json(score["recommendations"]),
        "score_json": _json(score),
    })
    return score


async def _create_experiment_record(plan: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            await client.post(f"{settings.experiment_tracker_service_url.rstrip('/')}/experiments", json={"experiment_id": plan["experiment_id"], "experiment_type": plan["experiment_kind"], "target_service": plan["target_service"], "namespace": plan["target_namespace"], "duration_seconds": plan["duration_seconds"], "chaos_mesh_resource": plan["manifest"]["metadata"]["name"], "status": "started"})
    except Exception as exc:
        logger.warning("Experiment tracker create failed: %s", exc)


async def _complete_experiment_record(plan: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            await client.post(f"{settings.experiment_tracker_service_url.rstrip('/')}/experiments/{plan['experiment_id']}/complete")
    except Exception as exc:
        logger.warning("Experiment tracker complete failed: %s", exc)


async def _insert_violation(plan_id: str, run_id: str, kind: str, severity: str, message: str, request: dict[str, Any]) -> None:
    await clickhouse.insert_chaos_safety_violation({"violation_id": "chaos_violation_" + uuid.uuid4().hex[:16], "created_at": _now(), "plan_id": plan_id, "run_id": run_id, "violation_type": kind, "severity": severity, "message": message, "policy_json": _json(default_policy().model_dump()), "request_json": _json(request)})


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
            chaos_experiment_id=plan.get("experiment_id", ""),
            evidence_summary="; ".join((result.findings + result.violations)[:3]),
            user_safe_message=f"Chaos policy {'allowed' if result.allowed else 'blocked'} {plan['experiment_kind']} for {plan['target_service']}",
        ),
    )


async def _publish(event_type: str, plan: dict[str, Any], run: dict[str, Any], score: dict[str, Any] | None, summary: str) -> None:
    subsystem = "chaos"
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            event_type,
            subsystem,
            severity="error" if event_type.endswith(".failed") else ("warning" if event_type.endswith(".rejected") else "info"),
            payload={"plan": plan, "run": run, "score": score or {}},
            run_id=run.get("run_id", ""),
            correlation_id=run.get("run_id") or plan.get("plan_id", ""),
            service=plan.get("target_service") or run.get("target_service", ""),
            namespace=plan.get("target_namespace") or run.get("target_namespace", ""),
            action=plan.get("experiment_kind") or run.get("experiment_kind", event_type),
            status=run.get("status") or plan.get("status", ""),
            risk_level=plan.get("risk_level", ""),
            chaos_experiment_id=run.get("experiment_id") or plan.get("experiment_id", ""),
            evidence_summary=summary,
            user_safe_message=summary or f"Chaos event {event_type}",
        ),
    )
    if not settings.chaos_publish_events:
        return
    try:
        await producer.send(settings.chaos_events_topic, chaos_event(event_type, plan, run, score, summary), key=run.get("run_id") or plan.get("plan_id"))
    except Exception as exc:
        logger.warning("Failed to publish chaos event type=%s error=%s", event_type, exc)


def _live_config() -> LiveDemoConfig:
    return LiveDemoConfig(
        enable_dangerous_actions=settings.enable_dangerous_actions,
        enable_real_chaos=settings.enable_real_chaos,
        enable_real_remediation=False,
        cascade_live_demo_mode=settings.cascade_live_demo_mode,
        cascade_allowed_cluster_context=settings.cascade_allowed_cluster_context,
        cascade_active_cluster_context=settings.cascade_active_cluster_context,
        cascade_allowed_target_namespace=settings.cascade_allowed_target_namespace,
        cascade_require_approval=settings.cascade_require_approval,
        cascade_require_dry_run_first=settings.cascade_require_dry_run_first,
    )


def _live_status() -> dict[str, Any]:
    return {
        "dangerous_actions_enabled": settings.enable_dangerous_actions,
        "real_chaos_enabled": settings.enable_real_chaos,
        "live_demo_mode": settings.cascade_live_demo_mode,
        "allowed_cluster_context": settings.cascade_allowed_cluster_context,
        "active_cluster_context": settings.cascade_active_cluster_context or (k8s.current_context() if k8s else ""),
        "allowed_target_namespace": settings.cascade_allowed_target_namespace,
        "approval_required": settings.cascade_require_approval,
        "dry_run_first_required": settings.cascade_require_dry_run_first,
    }


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )


def _decode_plan(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["target_selector"] = _loads(decoded.pop("target_selector_json", "{}"))
    decoded["safety_policy"] = _loads(decoded.pop("safety_policy_json", "{}"))
    decoded["manifest"] = _loads(decoded.pop("manifest_json", "{}"))
    decoded["plan"] = _loads(decoded.pop("plan_json", "{}"))
    return decoded


def _decode_run(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["dry_run"] = bool(decoded.get("dry_run"))
    decoded["approved"] = bool(decoded.get("approved"))
    decoded["run"] = _loads(decoded.pop("run_json", "{}"))
    return decoded


def _decode_score(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["recommendations"] = _loads(decoded.pop("recommendations_json", "[]"))
    decoded["score"] = _loads(decoded.pop("score_json", "{}"))
    return decoded


def _decode_observation(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ["affected_services_json", "telemetry_summary_json", "anomaly_summary_json", "incident_summary_json", "observation_json"]:
        decoded[key.replace("_json", "")] = _loads(decoded.pop(key, "{}"))
    return decoded


def _decode_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {"run": _decode_run(detail["run"]), "observation": _decode_observation(detail["observation"]) if detail["observation"] else None, "score": _decode_score(detail["score"]) if detail["score"] else None}


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
