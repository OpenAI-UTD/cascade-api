from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.remediation.events import remediation_event
from services.shared.remediation.planner import build_plan
from services.shared.remediation.safety import default_policy, validate_plan
from services.shared.remediation.schemas import RemediationPlanRequest
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.targets.catalog import ACTIVE_NAMESPACE, ACTIVE_SAFE_CHAOS_SERVICES
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    retrieval_service_url: str = "http://retrieval-service.cascade-system.svc.cluster.local:8012"
    agent_orchestrator_service_url: str = "http://agent-orchestrator-service.cascade-system.svc.cluster.local:8018"
    knowledge_retrieval_service_url: str = "http://knowledge-retrieval-service.cascade-system.svc.cluster.local:8016"
    topology_service_url: str = "http://topology-service.cascade-system.svc.cluster.local:8004"
    chaos_executor_service_url: str = "http://chaos-executor-service.cascade-system.svc.cluster.local:8020"
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    remediation_events_topic: str = "remediation.actions"
    remediation_publish_events: bool = True
    request_timeout_seconds: float = 8.0

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="remediation-recommender-service"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    await clickhouse.initialize_schema()
    if settings.remediation_publish_events:
        await producer.start()
    try:
        yield
    finally:
        if settings.remediation_publish_events:
            await producer.stop()


app = FastAPI(title="Cascade Remediation Recommender Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "remediation-recommender-service"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    checks = {
        "clickhouse": await clickhouse.ping(),
        "retrieval-service": await _check(settings.retrieval_service_url),
        "agent-orchestrator-service": await _check(settings.agent_orchestrator_service_url),
        "knowledge-retrieval-service": await _check(settings.knowledge_retrieval_service_url),
    }
    response = {"status": "ok" if checks["clickhouse"] else "degraded", "service": "remediation-recommender-service", "checks": checks}
    if not checks["clickhouse"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.post("/plans")
async def create_plan(payload: RemediationPlanRequest) -> dict[str, Any]:
    evidence, context = await _gather_evidence(payload)
    plan = build_plan(payload, evidence, context)
    await _insert_plan(plan)
    safety = validate_plan(plan, default_policy(), dry_run=True)
    await _audit(plan, safety)
    if safety.allowed:
        await _publish("remediation.plan.created", plan, summary=plan["action_summary"])
        logger.info("created remediation plan id=%s service=%s action=%s", plan["plan_id"], plan["service"], plan["action_type"])
        return {"plan_id": plan["plan_id"], "status": plan["status"], "plan": plan}
    await _violation(plan["plan_id"], "", "plan_rejected", "high", "; ".join(safety.violations), payload.model_dump())
    await _publish("remediation.plan.rejected", plan, summary="; ".join(safety.violations))
    raise HTTPException(status_code=400, detail={"status": "rejected", "plan_id": plan["plan_id"], "violations": safety.violations})


@app.get("/plans")
async def plans(limit: int = 20, service: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_remediation_plans(limit, service, status)
    return {"plans": [_decode_plan(row) for row in rows], "count": len(rows)}


@app.get("/plans/{plan_id}")
async def get_plan(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return {"plan": _decode_plan(row)}


@app.post("/plans/from-latest-investigation")
async def plan_from_latest_investigation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    service = payload.get("service")
    rows = await clickhouse.recent_investigation_runs(1, service=service)
    trigger_id = rows[0]["investigation_id"] if rows else ""
    request = RemediationPlanRequest(trigger_type="investigation", trigger_id=trigger_id, service=service or (rows[0].get("service") if rows else ACTIVE_SAFE_CHAOS_SERVICES[0]), namespace=payload.get("namespace", ACTIVE_NAMESPACE), objective=payload.get("objective", "Recommend remediation from latest investigation"), preferred_action_type=payload.get("preferred_action_type", "investigate_only"))
    return await create_plan(request)


@app.post("/plans/from-latest-anomaly")
async def plan_from_latest_anomaly(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    service = payload.get("service")
    rows = await clickhouse.recent_anomalies(1, service=service)
    trigger_id = rows[0]["anomaly_id"] if rows else ""
    request = RemediationPlanRequest(trigger_type="anomaly", trigger_id=trigger_id, service=service or (rows[0].get("service") if rows else ACTIVE_SAFE_CHAOS_SERVICES[0]), namespace=payload.get("namespace", ACTIVE_NAMESPACE), objective=payload.get("objective", "Recommend remediation from latest anomaly"), preferred_action_type=payload.get("preferred_action_type", "investigate_only"))
    return await create_plan(request)


@app.post("/plans/{plan_id}/validate")
async def validate_existing_plan(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_plan(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan = _decode_plan(row)
    result = validate_plan(plan, default_policy(), dry_run=True)
    await _audit(plan, result)
    return {"plan_id": plan_id, **result.model_dump()}


@app.post("/policy/evaluate")
async def evaluate_policy(payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(payload.get("plan_id") or "")
    if plan_id:
        row = await clickhouse.remediation_plan(plan_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        plan = _decode_plan(row)
    else:
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
    result = validate_plan(
        plan,
        default_policy(),
        dry_run=bool(payload.get("dry_run", True)),
        approved=bool(payload.get("approved", False)),
        mode=str(payload.get("mode") or "dry-run"),  # type: ignore[arg-type]
        autonomy_level=int(payload.get("autonomy_level", default_policy().autonomy_level_default)),
    )
    if plan.get("plan_id"):
        await _audit(plan, result)
    return {"policy_decision": result.policy_decision, "safety": result.model_dump()}


async def _gather_evidence(payload: RemediationPlanRequest) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    context: dict[str, Any] = {"summary": ""}
    rca = await _latest_rca(payload.service)
    if rca:
        evidence.append({"source": "clickhouse", "type": "rca", "id": str(rca.get("report_id") or ""), "summary": str(rca.get("explanation") or "")[:240]})
        context.update(
            {
                "source_rca_id": rca.get("report_id", ""),
                "summary": rca.get("explanation") or context.get("summary", ""),
                "confidence": rca.get("confidence_score", 0.0),
                "root_cause_service": rca.get("likely_root_cause_service", ""),
                "affected_services": rca.get("affected_downstream_services", []),
            }
        )
    if payload.trigger_type == "anomaly" and payload.trigger_id:
        anomaly = await clickhouse.anomaly_detail(payload.trigger_id)
        if anomaly:
            evidence.append({"source": "clickhouse", "type": "anomaly", "id": payload.trigger_id})
            context.update(anomaly)
    elif payload.trigger_type == "investigation" and payload.trigger_id:
        detail = await clickhouse.investigation_detail(payload.trigger_id)
        if detail.get("run") or detail.get("report"):
            evidence.append({"source": "clickhouse", "type": "investigation", "id": payload.trigger_id})
            context.update(detail.get("report") or detail.get("run") or {})
    elif payload.trigger_type == "incident" and payload.trigger_id:
        detail = await clickhouse.incident_with_reports(payload.trigger_id)
        if detail.get("incident"):
            evidence.append({"source": "clickhouse", "type": "incident", "id": payload.trigger_id})
            context.update(detail.get("incident") or {})
    elif payload.trigger_type == "chaos" and payload.trigger_id:
        detail = await clickhouse.chaos_run_detail(payload.trigger_id)
        if detail.get("run"):
            evidence.append({"source": "clickhouse", "type": "chaos_run", "id": payload.trigger_id})
            context.update(detail.get("score") or detail.get("run") or {})
    else:
        rows = await clickhouse.recent_anomalies(1, service=payload.service)
        if rows:
            evidence.append({"source": "clickhouse", "type": "anomaly", "id": rows[0].get("anomaly_id", "")})
            context.update(rows[0])
        else:
            evidence.append({"source": "manual", "type": "objective", "id": payload.objective[:80], "limitation": "No recent platform evidence was available; confidence is reduced."})

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            resp = await client.post(settings.knowledge_retrieval_service_url.rstrip("/") + "/knowledge/context", json={"query": payload.objective, "limit": 3, "filters": {"service": payload.service}})
            if resp.status_code < 500:
                chunks = resp.json().get("evidence_chunks", []) or resp.json().get("results", [])
                for item in chunks[:3]:
                    evidence.append({"source": "knowledge", "type": "runbook_chunk", "id": str(item.get("chunk_id") or item.get("document_id") or "")})
    except Exception as exc:
        evidence.append({"source": "integration", "type": "knowledge", "id": "unavailable", "limitation": str(exc)[:120]})
    return evidence[:10], context


async def _latest_rca(service: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(settings.retrieval_service_url.rstrip("/") + "/rca/recent", params={"limit": 1, "service": service})
            if response.status_code >= 500:
                return None
            reports = response.json().get("reports", [])
            return reports[0] if reports else None
    except Exception:
        return None


async def _insert_plan(plan: dict[str, Any]) -> None:
    now = _now()
    await clickhouse.insert_remediation_plan({
        "plan_id": plan["plan_id"], "created_at": now, "updated_at": now, "status": plan["status"], "trigger_type": plan["trigger_type"], "trigger_id": plan["trigger_id"],
        "source_investigation_id": plan["source_investigation_id"], "source_anomaly_id": plan["source_anomaly_id"], "source_incident_id": plan["source_incident_id"], "source_chaos_run_id": plan["source_chaos_run_id"],
        "service": plan["service"], "namespace": plan["namespace"], "severity": plan["severity"], "risk_score": plan["risk_score"], "confidence": plan["confidence"], "action_type": plan["action_type"], "action_summary": plan["action_summary"],
        "remediation_steps_json": _json(plan["remediation_steps"]), "rollback_steps_json": _json(plan["rollback_steps"]), "evidence_refs_json": _json(plan["evidence_refs"]), "safety_findings_json": _json(plan["safety_findings"]), "dry_run_manifest_json": _json(plan["dry_run_manifest"]), "plan_json": _json(plan),
    })


async def _audit(plan: dict[str, Any], result: Any) -> None:
    audit_id = "rem_audit_" + plan["plan_id"][-12:] + "_" + datetime.now(UTC).strftime("%H%M%S%f")
    await clickhouse.insert_remediation_policy_audit({"audit_id": audit_id, "checked_at": _now(), "plan_id": plan["plan_id"], "action_type": plan["action_type"], "namespace": plan["namespace"], "service": plan["service"], "allowed": 1 if result.allowed else 0, "risk_level": result.risk_level, "findings_json": _json(result.findings + result.violations), "policy_json": _json({"policy": default_policy().model_dump(), "decision": result.policy_decision})})
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
    await clickhouse.insert_remediation_safety_violation({"violation_id": "rem_violation_" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f"), "created_at": _now(), "plan_id": plan_id, "execution_id": execution_id, "violation_type": kind, "severity": severity, "message": message, "policy_json": _json(default_policy().model_dump()), "request_json": _json(request)})


async def _publish(event_type: str, plan: dict[str, Any], summary: str) -> None:
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            event_type,
            "remediation",
            severity="warning" if event_type.endswith(".rejected") else "info",
            payload=plan,
            correlation_id=plan.get("plan_id", ""),
            service=plan.get("service", ""),
            namespace=plan.get("namespace", ""),
            action=plan.get("action_type", event_type),
            decision=str((plan.get("policy_decision") or {}).get("status", "")) if isinstance(plan.get("policy_decision"), dict) else "",
            status=plan.get("status", ""),
            risk_level=plan.get("risk_level", ""),
            evidence_summary=summary,
            user_safe_message=summary or f"Remediation event {event_type}",
        ),
    )
    if not settings.remediation_publish_events:
        return
    try:
        await producer.send(settings.remediation_events_topic, remediation_event(event_type, plan=plan, summary=summary), key=plan.get("plan_id"))
    except Exception as exc:
        logger.warning("Failed to publish remediation event type=%s error=%s", event_type, exc)


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
    for key in ["remediation_steps_json", "rollback_steps_json", "evidence_refs_json", "safety_findings_json", "dry_run_manifest_json", "plan_json"]:
        decoded[key.replace("_json", "")] = _loads(decoded.pop(key, "[]" if key.endswith("steps_json") or key.endswith("refs_json") or key.endswith("findings_json") else "{}"))
    plan_json = decoded.get("plan")
    if isinstance(plan_json, dict):
        decoded.setdefault("policy_decision", plan_json.get("policy_decision", {}))
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

