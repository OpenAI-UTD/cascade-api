from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.remediation.approval import is_approval_current
from services.shared.remediation.events import remediation_event
from services.shared.remediation.safety import validate_approval
from services.shared.remediation.schemas import ApprovalRequest
from services.shared.security.approval import approval_metadata
from services.shared.security.auth import AuthSettings, auth_status, binding_guard, require_auth
from services.shared.storage.clickhouse_client import ClickHouseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    remediation_events_topic: str = "remediation.actions"
    remediation_publish_events: bool = True
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
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="approval-service"))


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


app = FastAPI(title="Cascade Approval Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "approval-service", **auth_status(_auth_settings())}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    response = {"status": "ok" if ch else "degraded", "service": "approval-service", "clickhouse": ch}
    if not ch:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.post("/approvals")
async def decide(payload: ApprovalRequest, request: Request) -> dict[str, Any]:
    auth = require_auth(request, _auth_settings(), action="record approval decision")
    violations = validate_approval(payload.decision, payload.approver, payload.reason)
    if violations:
        raise HTTPException(status_code=400, detail={"violations": violations})
    plan_row = await clickhouse.remediation_plan(payload.plan_id)
    plan_type = "remediation"
    if plan_row is None:
        plan_row = await clickhouse.chaos_plan(payload.plan_id)
        plan_type = "chaos"
    if plan_row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    decoded_plan = _decode_plan(plan_row, plan_type)
    metadata = approval_metadata(
        plan=decoded_plan,
        plan_type=plan_type,
        actor=auth.get("actor") or payload.approver,
        risk_level=str(decoded_plan.get("risk_level") or "unknown"),
        policy_decision=decoded_plan.get("policy_decision") if isinstance(decoded_plan.get("policy_decision"), dict) else {},
        expires_minutes=payload.expires_minutes,
        signing_secret=settings.cascade_approval_signing_secret,
    )
    approval = {
        "approval_id": "rem_approval_" + uuid.uuid4().hex[:16],
        "plan_id": payload.plan_id,
        "decided_at": _now(),
        "decision": payload.decision,
        "approver": payload.approver,
        "approver_role": payload.approver_role,
        "reason": payload.reason,
        "expires_at": _future(payload.expires_minutes) if payload.decision == "approved" else None,
        "metadata": metadata,
    }
    await clickhouse.insert_remediation_approval({
        "approval_id": approval["approval_id"],
        "plan_id": approval["plan_id"],
        "decided_at": approval["decided_at"],
        "decision": approval["decision"],
        "approver": approval["approver"],
        "approver_role": approval["approver_role"],
        "reason": approval["reason"],
        "expires_at": approval["expires_at"],
        "approval_metadata_json": _json(approval["metadata"]),
    })
    event_type = "remediation.approved" if payload.decision == "approved" else "remediation.rejected"
    await _publish(event_type, decoded_plan, approval, payload.reason)
    logger.info("approval decision=%s plan_id=%s approval_id=%s", payload.decision, payload.plan_id, approval["approval_id"])
    return {"approval": approval}


@app.get("/approvals")
async def approvals(limit: int = 20, plan_id: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_remediation_approvals(limit, plan_id)
    return {"approvals": [_decode_approval(row) for row in rows], "count": len(rows)}


@app.get("/approvals/{approval_id}")
async def approval_detail(approval_id: str) -> dict[str, Any]:
    row = await clickhouse.remediation_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"approval": _decode_approval(row)}


@app.get("/plans/{plan_id}/approval-status")
async def approval_status(plan_id: str) -> dict[str, Any]:
    row = await clickhouse.latest_remediation_approval(plan_id)
    approval = _decode_approval(row) if row else None
    return {"plan_id": plan_id, "approval": approval, "approved": is_approval_current(approval), "approval_current": is_approval_current(approval)}


async def _publish(event_type: str, plan: dict[str, Any], approval: dict[str, Any], summary: str) -> None:
    service = plan.get("service") or plan.get("target_service", "")
    namespace = plan.get("namespace") or plan.get("target_namespace", "")
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            event_type,
            "remediation" if approval.get("metadata", {}).get("plan_type") == "remediation" else "chaos",
            severity="info" if approval.get("decision") == "approved" else "warning",
            payload={"plan": plan, "approval": approval},
            correlation_id=approval.get("plan_id", ""),
            service=service,
            namespace=namespace,
            actor=approval.get("approver") or "unknown",
            action=plan.get("action_type") or plan.get("experiment_kind") or event_type,
            decision=approval.get("decision", ""),
            status=approval.get("decision", ""),
            evidence_summary=summary,
            user_safe_message=f"{approval.get('approver', 'Operator')} {approval.get('decision')} {approval.get('plan_id')}",
        ),
    )
    if not settings.remediation_publish_events:
        return
    try:
        await producer.send(settings.remediation_events_topic, remediation_event(event_type, plan=plan, approval=approval, summary=summary), key=approval.get("approval_id"))
    except Exception as exc:
        logger.warning("Failed to publish approval event type=%s error=%s", event_type, exc)


def _decode_approval(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["metadata"] = _loads(decoded.pop("approval_metadata_json", "{}"))
    decoded["approval_current"] = is_approval_current(decoded)
    return decoded


def _decode_plan(row: dict[str, Any], plan_type: str = "remediation") -> dict[str, Any]:
    decoded = dict(row)
    if plan_type == "chaos":
        for key in ["target_selector_json", "safety_policy_json", "manifest_json", "plan_json"]:
            decoded[key.replace("_json", "")] = _loads(decoded.pop(key, "{}"))
        return decoded
    for key in ["remediation_steps_json", "rollback_steps_json", "evidence_refs_json", "safety_findings_json", "dry_run_manifest_json", "plan_json"]:
        decoded[key.replace("_json", "")] = _loads(decoded.pop(key, "{}"))
    return decoded


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _future(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
