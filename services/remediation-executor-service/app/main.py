from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.live_demo import LiveDemoConfig, validate_live_demo_gate
from services.shared.remediation.dry_run import KubernetesRemediationClient, local_dry_run
from services.shared.remediation.events import remediation_event
from services.shared.remediation.safety import default_policy, validate_plan
from services.shared.remediation.schemas import ExecutionRequest, RollbackPlan, VerificationResult
from services.shared.remediation.verification import build_health_snapshot, build_rollback_plan, evaluate_verification
from services.shared.security.approval import approval_valid_for_plan
from services.shared.security.auth import AuthSettings, auth_status, binding_guard, require_auth
from services.shared.storage.clickhouse_client import ClickHouseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    remediation_events_topic: str = "remediation.actions"
    remediation_publish_events: bool = True
    execution_enabled: bool = False
    enable_dangerous_actions: bool = False
    enable_real_remediation: bool = False
    cascade_live_demo_mode: bool = False
    cascade_allowed_cluster_context: str = "kind-cascade"
    cascade_active_cluster_context: str = ""
    cascade_allowed_target_namespace: str = "cascade-targets"
    cascade_require_approval: bool = True
    cascade_require_dry_run_first: bool = True
    cascade_autonomy_level: int = 4
    cascade_action_budget: int = 3
    remediation_verification_enabled: bool = True
    remediation_stabilization_seconds: int = 15
    remediation_auto_rollback_enabled: bool = False
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
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="remediation-executor-service"))
kube: KubernetesRemediationClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global kube
    await clickhouse.initialize_schema()
    try:
        kube = KubernetesRemediationClient()
    except Exception as exc:
        logger.warning("Kubernetes remediation client unavailable: %s", exc)
        kube = None
    if settings.remediation_publish_events:
        await producer.start()
    try:
        yield
    finally:
        if settings.remediation_publish_events:
            await producer.stop()


app = FastAPI(title="Cascade Remediation Executor Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "remediation-executor-service", **_live_status(), **auth_status(_auth_settings())}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    kube_ready = bool(kube and kube.ready())
    response = {"status": "ok" if ch else "degraded", "service": "remediation-executor-service", "clickhouse": ch, "kubernetes": kube_ready, "execution_enabled": settings.execution_enabled, **_live_status()}
    if not ch:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.get("/safety/policy")
async def safety_policy() -> dict[str, Any]:
    policy = default_policy()
    policy.autonomy_level_default = settings.cascade_autonomy_level
    policy.action_budget = settings.cascade_action_budget
    data = policy.model_dump()
    data["execution_enabled"] = settings.execution_enabled
    data.update(_live_status())
    data["denied"] = {"namespaces": policy.denied_namespaces, "services": policy.denied_services, "resource_kinds": policy.denied_resource_kinds}
    data["protected"] = {"services": policy.protected_services}
    return data


@app.post("/safety/evaluate")
async def evaluate_safety(payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(payload.get("plan_id") or "")
    if plan_id:
        plan = await _load_plan(plan_id)
    else:
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    dry_run = bool(payload.get("dry_run", True))
    safety = validate_plan(
        plan,
        _policy(),
        approved=bool(payload.get("approved", False)),
        dry_run=dry_run,
        execution_enabled=settings.execution_enabled,
        mode=str(payload.get("mode") or ("dry-run" if dry_run else ("local-demo" if settings.cascade_live_demo_mode else "production-safe"))),  # type: ignore[arg-type]
        autonomy_level=int(payload.get("autonomy_level", settings.cascade_autonomy_level)),
        dangerous_actions_enabled=settings.enable_dangerous_actions,
        local_demo_enabled=settings.cascade_live_demo_mode,
        actions_used=await _actions_used(plan) if plan.get("service") else 0,
    )
    if plan.get("plan_id"):
        await _audit(plan, safety)
    return {"policy_decision": safety.policy_decision, "safety": safety.model_dump()}


@app.post("/executions/dry-run")
async def dry_run_execution(payload: ExecutionRequest) -> dict[str, Any]:
    plan = await _load_plan(payload.plan_id)
    safety = validate_plan(plan, _policy(), approved=False, dry_run=True, execution_enabled=settings.execution_enabled, mode="dry-run", autonomy_level=settings.cascade_autonomy_level, actions_used=await _actions_used(plan))
    await _audit(plan, safety)
    if not safety.allowed:
        await _violation(plan["plan_id"], "", "dry_run_rejected", "high", "; ".join(safety.violations), payload.model_dump())
        raise HTTPException(status_code=400, detail={"status": "rejected", "violations": safety.violations})
    await _publish("remediation.dry_run.started", plan, {}, {}, "Dry-run validation started")
    result = local_dry_run(plan, kube)
    execution = _execution(plan, payload.approval_id, True, False, "completed" if result["validation_status"] in {"passed", "degraded"} else "failed", result["validation_status"], "not_executed", result["summary"], "", result["output"])
    await _insert_execution(execution)
    await _publish("remediation.dry_run.completed", plan, {}, execution, result["summary"])
    return {"execution": execution, "safety": safety.model_dump()}


@app.post("/executions")
async def execute(payload: ExecutionRequest, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="execute remediation")
    plan = await _load_plan(payload.plan_id)
    approval = await _load_approval(payload.approval_id)
    approved, approval_violations = approval_valid_for_plan(approval, plan, signing_secret=settings.cascade_approval_signing_secret)
    safety = validate_plan(
        plan,
        _policy(),
        approved=approved,
        dry_run=payload.dry_run,
        execution_enabled=settings.execution_enabled,
        mode="dry-run" if payload.dry_run else ("local-demo" if settings.cascade_live_demo_mode else "production-safe"),
        autonomy_level=settings.cascade_autonomy_level,
        dangerous_actions_enabled=settings.enable_dangerous_actions,
        local_demo_enabled=settings.cascade_live_demo_mode,
        actions_used=await _actions_used(plan),
    )
    await _audit(plan, safety)
    if payload.dry_run:
        return await dry_run_execution(payload)
    if not safety.allowed:
        execution = _execution(plan, payload.approval_id, False, False, "rejected", "not_started", "blocked", "; ".join(safety.violations), "; ".join(safety.violations), {})
        await _insert_execution(execution)
        await _violation(plan["plan_id"], execution["execution_id"], "execution_rejected", "high", "; ".join(safety.violations), payload.model_dump())
        await _publish("remediation.execution.failed", plan, approval or {}, execution, "; ".join(safety.violations))
        raise HTTPException(status_code=400, detail={"status": "rejected", "violations": safety.violations, "execution_id": execution["execution_id"]})

    dry_run_passed = await _dry_run_passed(plan["plan_id"])
    live_violations = validate_live_demo_gate(
        action="ENABLE_REAL_REMEDIATION",
        namespace=plan["namespace"],
        service=plan["service"],
        config=_live_config(),
        feature_enabled=settings.enable_real_remediation,
        approval_current=approved,
        dry_run_passed=dry_run_passed,
        current_context=kube.current_context() if kube else "",
    )
    if approval_violations and settings.cascade_require_approval:
        live_violations = approval_violations + live_violations
    if live_violations:
        execution = _execution(plan, payload.approval_id, False, False, "rejected", "not_started", "blocked", "; ".join(live_violations), "; ".join(live_violations), {})
        await _insert_execution(execution)
        await _violation(plan["plan_id"], execution["execution_id"], "live_demo_gate_rejected", "high", "; ".join(live_violations), payload.model_dump())
        await _publish("remediation.execution.failed", plan, approval or {}, execution, "; ".join(live_violations))
        raise HTTPException(status_code=400, detail={"status": "rejected", "violations": live_violations, "execution_id": execution["execution_id"]})

    dry = local_dry_run(plan, kube)
    if dry["validation_status"] not in {"passed", "degraded"}:
        execution = _execution(plan, payload.approval_id, False, False, "failed", dry["validation_status"], "not_executed", dry["summary"], dry["summary"], dry["output"])
        await _insert_execution(execution)
        await _publish("remediation.execution.failed", plan, approval or {}, execution, dry["summary"])
        raise HTTPException(status_code=400, detail={"status": "failed", "validation": dry})

    await _publish("remediation.execution.started", plan, approval or {}, {}, "Execution started")
    try:
        before = await _capture_snapshot(plan, "before") if settings.remediation_verification_enabled else None
        output = _execute_allowed(plan)
        execution = _execution(plan, payload.approval_id, False, True, "completed", "passed", "executed", "Execution completed", "", output)
        await _insert_execution(execution)
        verification: VerificationResult | None = None
        rollback_plan: RollbackPlan | None = None
        if settings.remediation_verification_enabled:
            verification, rollback_plan = await _verify_execution(plan, execution, before)
        await _publish("remediation.execution.completed", plan, approval or {}, execution, "Execution completed")
        response: dict[str, Any] = {"execution": execution}
        if verification:
            response["verification"] = verification.model_dump()
        if rollback_plan:
            response["rollback_plan"] = rollback_plan.model_dump()
        return response
    except Exception as exc:
        execution = _execution(plan, payload.approval_id, False, False, "failed", "passed", "failed", str(exc), str(exc), {})
        await _insert_execution(execution)
        await _publish("remediation.execution.failed", plan, approval or {}, execution, str(exc))
        raise HTTPException(status_code=500, detail={"error": str(exc), "execution_id": execution["execution_id"]})


@app.get("/executions")
async def executions(limit: int = 20, plan_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_remediation_executions(limit, plan_id, status)
    return {"executions": [_decode_execution(row) for row in rows], "count": len(rows)}


@app.get("/executions/{execution_id}")
async def execution_detail(execution_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return {"execution": _decode_execution(row)}


@app.get("/verifications")
async def verifications(limit: int = 20, execution_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_remediation_verification_results(limit, execution_id, status)
    return {"verifications": [_decode_verification(row) for row in rows], "count": len(rows)}


@app.get("/verifications/{verification_id}")
async def verification_detail(verification_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_verification_result(verification_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Verification result not found")
    return {"verification": _decode_verification(row)}


@app.get("/executions/{execution_id}/verification")
async def execution_verification(execution_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_verification_for_execution(execution_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Verification result not found")
    return {"verification": _decode_verification(row)}


@app.get("/rollback-plans")
async def rollback_plans(limit: int = 20, execution_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_remediation_rollback_plans(limit, execution_id, status)
    return {"rollback_plans": [_decode_rollback_plan(row) for row in rows], "count": len(rows)}


@app.get("/rollback-plans/{rollback_plan_id}")
async def rollback_plan_detail(rollback_plan_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_rollback_plan(rollback_plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Rollback plan not found")
    return {"rollback_plan": _decode_rollback_plan(row)}


async def _load_plan(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return _decode_plan(row)


async def _load_approval(approval_id: str) -> dict[str, Any] | None:
    if not approval_id:
        return None
    return await clickhouse.remediation_approval(approval_id)


async def _dry_run_passed(plan_id: str) -> bool:
    row = await clickhouse.latest_remediation_dry_run(plan_id)
    return bool(row)


async def _actions_used(plan: dict[str, Any]) -> int:
    rows = await clickhouse.recent_remediation_executions(settings.cascade_action_budget, service=plan.get("service"), status="completed")
    return len([row for row in rows if not bool(row.get("dry_run"))])


def _execute_allowed(plan: dict[str, Any]) -> dict[str, Any]:
    if kube is None:
        raise RuntimeError("Kubernetes client unavailable")
    if plan["action_type"] == "restart_deployment":
        return kube.restart_deployment(plan["namespace"], plan["service"], plan["plan_id"])
    if plan["action_type"] == "scale_deployment_noop":
        return kube.scale_noop(plan["namespace"], plan["service"])
    raise RuntimeError(f"Action type {plan['action_type']} is not implemented for real execution")


async def _verify_execution(plan: dict[str, Any], execution: dict[str, Any], before: dict[str, Any] | None) -> tuple[VerificationResult, RollbackPlan]:
    before = before or await _capture_snapshot(plan, "before")
    rollback_plan = build_rollback_plan(plan, execution["execution_id"], before)
    await _insert_rollback_plan(rollback_plan)
    await asyncio.sleep(max(0, min(settings.remediation_stabilization_seconds, 300)))
    after = await _capture_snapshot(plan, "after")
    rollback_result: dict[str, Any] | None = None
    preliminary = evaluate_verification(plan=plan, execution=execution, before=before, after=after, rollback_plan=rollback_plan)
    if preliminary.status == "degraded" and settings.remediation_auto_rollback_enabled:
        rollback_result = _execute_rollback(rollback_plan)
        rollback_plan.status = "executed" if rollback_result.get("status") == "executed" else "failed"
        rollback_plan.result = rollback_result
        rollback_plan.updated_at = _now()
        await _insert_rollback_plan(rollback_plan)
    elif preliminary.status == "degraded" and rollback_plan.available:
        rollback_plan.status = "disabled"
        rollback_plan.reason = f"{rollback_plan.reason} Automatic rollback is disabled by REMEDIATION_AUTO_ROLLBACK_ENABLED=false."
        rollback_plan.updated_at = _now()
        await _insert_rollback_plan(rollback_plan)
    verification = evaluate_verification(plan=plan, execution=execution, before=before, after=after, rollback_plan=rollback_plan, rollback_result=rollback_result)
    await _insert_verification(verification)
    return verification, rollback_plan


async def _capture_snapshot(plan: dict[str, Any], phase: str) -> dict[str, Any]:
    service = str(plan.get("service") or "")
    namespace = str(plan.get("namespace") or "")
    limitations: list[str] = []
    kube_snapshot: dict[str, Any] | None = None
    if kube is not None:
        try:
            kube_snapshot = kube.deployment_snapshot(namespace, service)
        except Exception as exc:
            limitations.append(f"Kubernetes snapshot failed: {exc}")
    try:
        feature_windows = await clickhouse.recent_feature_windows(20, service)
    except Exception as exc:
        feature_windows = []
        limitations.append(f"Feature windows unavailable: {exc}")
    try:
        anomalies = await clickhouse.recent_anomalies(20, service)
    except Exception as exc:
        anomalies = []
        limitations.append(f"Anomaly rows unavailable: {exc}")
    try:
        scores = await clickhouse.recent_resilience_scores(5, service)
    except Exception as exc:
        scores = []
        limitations.append(f"Resilience scores unavailable: {exc}")
    return build_health_snapshot(service=service, namespace=namespace, phase=phase, kubernetes=kube_snapshot, feature_windows=feature_windows, anomalies=anomalies, resilience_scores=scores, limitations=limitations)


def _execute_rollback(rollback_plan: RollbackPlan) -> dict[str, Any]:
    if kube is None:
        return {"status": "failed", "reason": "Kubernetes client unavailable"}
    if not rollback_plan.auto_executable:
        return {"status": "unavailable", "reason": rollback_plan.reason}
    for action in rollback_plan.actions:
        if action.get("action") == "restore_deployment_replicas":
            return {"status": "executed", "output": kube.rollback_replicas(str(action["namespace"]), str(action["deployment"]), int(action["replicas"]))}
    return {"status": "unavailable", "reason": "No executable rollback action was found"}


def _execution(plan: dict[str, Any], approval_id: str, dry_run: bool, executed: bool, status_: str, validation: str, execution_status: str, summary: str, error: str, output: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    return {
        "execution_id": "rem_exec_" + uuid.uuid4().hex[:16],
        "plan_id": plan["plan_id"],
        "approval_id": approval_id or "",
        "started_at": now,
        "completed_at": now,
        "status": status_,
        "dry_run": dry_run,
        "executed": executed,
        "action_type": plan["action_type"],
        "namespace": plan["namespace"],
        "service": plan["service"],
        "resource_kind": "Deployment" if plan["action_type"] in {"restart_deployment", "scale_deployment_noop", "rollback_deployment"} else "",
        "resource_name": plan["service"],
        "validation_status": validation,
        "execution_status": execution_status,
        "rollback_available": bool(plan.get("rollback_steps")),
        "output_summary": summary[:1000],
        "error_message": error[:1000],
        "output": output,
    }


async def _insert_execution(execution: dict[str, Any]) -> None:
    await clickhouse.insert_remediation_execution({
        "execution_id": execution["execution_id"],
        "plan_id": execution["plan_id"],
        "approval_id": execution["approval_id"],
        "started_at": execution["started_at"],
        "completed_at": execution["completed_at"],
        "status": execution["status"],
        "dry_run": 1 if execution["dry_run"] else 0,
        "executed": 1 if execution["executed"] else 0,
        "action_type": execution["action_type"],
        "namespace": execution["namespace"],
        "service": execution["service"],
        "resource_kind": execution["resource_kind"],
        "resource_name": execution["resource_name"],
        "validation_status": execution["validation_status"],
        "execution_status": execution["execution_status"],
        "rollback_available": 1 if execution["rollback_available"] else 0,
        "output_summary": execution["output_summary"],
        "error_message": execution["error_message"],
        "execution_json": _json(execution),
    })


async def _insert_rollback_plan(plan: RollbackPlan) -> None:
    data = plan.model_dump()
    await clickhouse.insert_remediation_rollback_plan({
        "rollback_plan_id": data["rollback_plan_id"],
        "execution_id": data["execution_id"],
        "plan_id": data["plan_id"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "action_type": data["action_type"],
        "namespace": data["namespace"],
        "service": data["service"],
        "rollback_type": data["rollback_type"],
        "available": 1 if data["available"] else 0,
        "auto_executable": 1 if data["auto_executable"] else 0,
        "status": data["status"],
        "reason": data["reason"],
        "snapshot_json": _json(data["snapshot"]),
        "actions_json": _json(data["actions"]),
        "result_json": _json(data["result"]),
        "rollback_plan_json": _json(data),
    })
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            f"rollback.plan.{data['status']}",
            "rollback",
            severity="warning" if not data["available"] or data["status"] in {"failed", "disabled"} else "info",
            payload=data,
            correlation_id=data["execution_id"],
            service=data["service"],
            namespace=data["namespace"],
            action=data["rollback_type"],
            status=data["status"],
            remediation_execution_id=data["execution_id"],
            rollback_plan_id=data["rollback_plan_id"],
            evidence_summary=data["reason"],
            user_safe_message=f"Rollback plan {data['status']} for {data['service']}",
        ),
    )


async def _insert_verification(result: VerificationResult) -> None:
    data = result.model_dump()
    await clickhouse.insert_remediation_verification_result({
        "verification_id": data["verification_id"],
        "execution_id": data["execution_id"],
        "plan_id": data["plan_id"],
        "rollback_plan_id": data["rollback_plan_id"],
        "created_at": data["created_at"],
        "completed_at": data["completed_at"],
        "action_type": data["action_type"],
        "namespace": data["namespace"],
        "service": data["service"],
        "status": data["status"],
        "evidence_quality": data["evidence_quality"],
        "rollback_status": data["rollback_status"],
        "summary": data["summary"],
        "before_json": _json(data["before"]),
        "after_json": _json(data["after"]),
        "comparisons_json": _json(data["comparisons"]),
        "limitations_json": _json(data["limitations"]),
        "rollback_json": _json(data["rollback"]),
        "verification_json": _json(data),
    })
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            "verification.completed",
            "verification",
            severity="warning" if data["status"] in {"degraded", "failed", "insufficient_evidence"} else "info",
            payload=data,
            correlation_id=data["execution_id"],
            service=data["service"],
            namespace=data["namespace"],
            action=data["action_type"],
            status=data["status"],
            remediation_execution_id=data["execution_id"],
            verification_id=data["verification_id"],
            rollback_plan_id=data["rollback_plan_id"],
            evidence_summary=data["summary"],
            user_safe_message=f"Verification {data['status']} for {data['service']}",
        ),
    )


async def _audit(plan: dict[str, Any], result: Any) -> None:
    audit_id = "rem_audit_" + uuid.uuid4().hex[:16]
    await clickhouse.insert_remediation_policy_audit({"audit_id": audit_id, "checked_at": _now(), "plan_id": plan["plan_id"], "action_type": plan["action_type"], "namespace": plan["namespace"], "service": plan["service"], "allowed": 1 if result.allowed else 0, "risk_level": result.risk_level, "findings_json": _json(result.findings + result.violations), "policy_json": _json({"policy": _policy().model_dump(), "decision": result.policy_decision})})
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            "policy.evaluated",
            "policy",
            severity="info" if result.allowed else "warning",
            payload={"plan": plan, "decision": result.policy_decision, "findings": result.findings, "violations": result.violations},
            correlation_id=plan["plan_id"],
            service=plan["service"],
            namespace=plan["namespace"],
            action=plan["action_type"],
            decision=str(result.policy_decision.get("status", "")),
            status="allowed" if result.allowed else "blocked",
            risk_level=result.risk_level,
            policy_decision_id=audit_id,
            evidence_summary="; ".join((result.findings + result.violations)[:3]),
            user_safe_message=f"Policy {'allowed' if result.allowed else 'blocked'} {plan['action_type']} for {plan['service']}",
        ),
    )


async def _violation(plan_id: str, execution_id: str, kind: str, severity: str, message: str, request: dict[str, Any]) -> None:
    await clickhouse.insert_remediation_safety_violation({"violation_id": "rem_violation_" + uuid.uuid4().hex[:16], "created_at": _now(), "plan_id": plan_id, "execution_id": execution_id, "violation_type": kind, "severity": severity, "message": message, "policy_json": _json(_policy().model_dump()), "request_json": _json(request)})


async def _publish(event_type: str, plan: dict[str, Any], approval: dict[str, Any], execution: dict[str, Any], summary: str) -> None:
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            event_type,
            "remediation",
            severity="error" if event_type.endswith(".failed") else "info",
            payload={"plan": plan, "approval": approval, "execution": execution},
            run_id=execution.get("execution_id", ""),
            correlation_id=execution.get("execution_id") or plan.get("plan_id", ""),
            service=plan.get("service") or execution.get("service", ""),
            namespace=plan.get("namespace") or execution.get("namespace", ""),
            actor=approval.get("approver") or "cascade-system",
            action=plan.get("action_type") or execution.get("action_type", event_type),
            decision=approval.get("decision", ""),
            status=execution.get("status") or plan.get("status", ""),
            risk_level=plan.get("risk_level", ""),
            remediation_execution_id=execution.get("execution_id", ""),
            evidence_summary=summary,
            user_safe_message=summary or f"Remediation event {event_type}",
        ),
    )
    if not settings.remediation_publish_events:
        return
    try:
        await producer.send(settings.remediation_events_topic, remediation_event(event_type, plan=plan, approval=approval, execution=execution, summary=summary), key=execution.get("execution_id") or plan.get("plan_id"))
    except Exception as exc:
        logger.warning("Failed to publish execution event type=%s error=%s", event_type, exc)


def _live_config() -> LiveDemoConfig:
    return LiveDemoConfig(
        enable_dangerous_actions=settings.enable_dangerous_actions,
        enable_real_chaos=False,
        enable_real_remediation=settings.enable_real_remediation,
        cascade_live_demo_mode=settings.cascade_live_demo_mode,
        cascade_allowed_cluster_context=settings.cascade_allowed_cluster_context,
        cascade_active_cluster_context=settings.cascade_active_cluster_context,
        cascade_allowed_target_namespace=settings.cascade_allowed_target_namespace,
        cascade_require_approval=settings.cascade_require_approval,
        cascade_require_dry_run_first=settings.cascade_require_dry_run_first,
    )


def _policy():
    policy = default_policy()
    policy.autonomy_level_default = settings.cascade_autonomy_level
    policy.action_budget = settings.cascade_action_budget
    return policy


def _live_status() -> dict[str, Any]:
    return {
        "dangerous_actions_enabled": settings.enable_dangerous_actions,
        "real_remediation_enabled": settings.enable_real_remediation,
        "live_demo_mode": settings.cascade_live_demo_mode,
        "allowed_cluster_context": settings.cascade_allowed_cluster_context,
        "active_cluster_context": settings.cascade_active_cluster_context or (kube.current_context() if kube else ""),
        "allowed_target_namespace": settings.cascade_allowed_target_namespace,
        "approval_required": settings.cascade_require_approval,
        "dry_run_first_required": settings.cascade_require_dry_run_first,
        "verification_enabled": settings.remediation_verification_enabled,
        "auto_rollback_enabled": settings.remediation_auto_rollback_enabled,
        "stabilization_seconds": settings.remediation_stabilization_seconds,
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
    for key in ["remediation_steps_json", "rollback_steps_json", "evidence_refs_json", "safety_findings_json", "dry_run_manifest_json", "plan_json"]:
        decoded[key.replace("_json", "")] = _loads(decoded.pop(key, "{}"))
    return decoded


def _decode_execution(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["dry_run"] = bool(decoded.get("dry_run"))
    decoded["executed"] = bool(decoded.get("executed"))
    decoded["rollback_available"] = bool(decoded.get("rollback_available"))
    decoded["execution"] = _loads(decoded.pop("execution_json", "{}"))
    return decoded


def _decode_rollback_plan(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["available"] = bool(decoded.get("available"))
    decoded["auto_executable"] = bool(decoded.get("auto_executable"))
    for source, target in [
        ("snapshot_json", "snapshot"),
        ("actions_json", "actions"),
        ("result_json", "result"),
        ("rollback_plan_json", "rollback_plan"),
    ]:
        decoded[target] = _loads(decoded.pop(source, "[]" if source == "actions_json" else "{}"))
    return decoded


def _decode_verification(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [
        ("before_json", "before"),
        ("after_json", "after"),
        ("comparisons_json", "comparisons"),
        ("limitations_json", "limitations"),
        ("rollback_json", "rollback"),
        ("verification_json", "verification"),
    ]:
        decoded[target] = _loads(decoded.pop(source, "[]" if source in {"comparisons_json", "limitations_json"} else "{}"))
    return decoded


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
