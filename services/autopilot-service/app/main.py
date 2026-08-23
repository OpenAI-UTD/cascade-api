from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.autopilot.events import autopilot_event
from services.shared.autopilot.schemas import AutopilotRunRecord, AutopilotRunRequest, AutopilotStepRecord
from services.shared.autopilot.workflow import AutopilotWorkflow
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.security.auth import AuthSettings, auth_status, binding_guard, require_auth
from services.shared.storage.clickhouse_client import ClickHouseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    retrieval_service_url: str = "http://retrieval-service.cascade-system.svc.cluster.local:8012"
    anomaly_detector_service_url: str = "http://anomaly-detector-service.cascade-system.svc.cluster.local:8014"
    agent_orchestrator_service_url: str = "http://agent-orchestrator-service.cascade-system.svc.cluster.local:8018"
    remediation_recommender_service_url: str = "http://remediation-recommender-service.cascade-system.svc.cluster.local:8021"
    approval_service_url: str = "http://approval-service.cascade-system.svc.cluster.local:8022"
    remediation_executor_service_url: str = "http://remediation-executor-service.cascade-system.svc.cluster.local:8023"
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    autopilot_events_topic: str = "autopilot.runs"
    autopilot_publish_events: bool = True
    request_timeout_seconds: float = 20.0
    default_mode: str = "dry_run"
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="autopilot-service"))
recorder = None


class ClickHouseAutopilotRecorder:
    def __init__(self, client: ClickHouseClient) -> None:
        self.client = client

    async def save_run(self, run: AutopilotRunRecord) -> None:
        await self.client.insert_autopilot_run(
            {
                "run_id": run.run_id,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "completed_at": run.completed_at,
                "status": run.status,
                "final_result": run.final_result,
                "mode": run.mode,
                "trigger_type": run.trigger_type,
                "trigger_id": run.trigger_id,
                "service": run.service,
                "namespace": run.namespace,
                "objective": run.objective,
                "anomaly_id": run.anomaly_id,
                "investigation_id": run.investigation_id,
                "remediation_plan_id": run.remediation_plan_id,
                "approval_id": run.approval_id,
                "dry_run_execution_id": run.dry_run_execution_id,
                "execution_id": run.execution_id,
                "proposed_action": run.proposed_action,
                "error_message": run.error_message,
                "evidence_json": _json(run.evidence),
                "recommendation_json": _json(run.recommendation),
                "action_json": _json(run.action),
                "verification_json": _json(run.verification),
                "run_json": _json(run.model_dump()),
            }
        )
        await emit_audit_event(
            self.client,
            build_audit_event(
                f"autopilot.run.{run.status}",
                "Autopilot",
                severity="error" if run.status == "failed" else "info",
                payload=run.model_dump(),
                run_id=run.run_id,
                correlation_id=run.run_id,
                service=run.service,
                namespace=run.namespace,
                action=run.proposed_action or run.objective or "autopilot_run",
                status=run.status,
                autopilot_run_id=run.run_id,
                remediation_execution_id=run.execution_id or run.dry_run_execution_id,
                evidence_summary=run.final_result or run.error_message,
                user_safe_message=f"Autopilot run {run.status} for {run.service or 'target service'}",
            ),
        )

    async def add_step(self, step: AutopilotStepRecord) -> None:
        await self.client.insert_autopilot_step(
            {
                "step_id": step.step_id,
                "run_id": step.run_id,
                "created_at": step.created_at,
                "state": step.state,
                "status": step.status,
                "summary": step.summary,
                "input_json": _json(step.input),
                "output_json": _json(step.output),
                "error_message": step.error_message,
                "step_json": _json(step.model_dump()),
            }
        )
        await emit_audit_event(
            self.client,
            build_audit_event(
                f"autopilot.step.{step.state}",
                "Autopilot",
                severity="error" if step.status == "failed" else "info",
                payload=step.model_dump(),
                run_id=step.run_id,
                correlation_id=step.run_id,
                action=step.state,
                status=step.status,
                autopilot_run_id=step.run_id,
                evidence_summary=step.summary,
                user_safe_message=step.summary or f"Autopilot step {step.state}",
            ),
        )

    async def list_runs(self, limit: int = 20, service: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return [_decode_run(row) for row in await self.client.recent_autopilot_runs(limit, service, status)]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = await self.client.autopilot_run(run_id)
        return _decode_run(row) if row else None

    async def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        return [_decode_step(row) for row in await self.client.autopilot_steps(run_id)]


class HttpAutopilotClient:
    def __init__(self, settings_: Settings) -> None:
        self.settings = settings_

    async def detect_anomalies(self, service: str, namespace: str) -> dict[str, Any]:
        return await self._post(self.settings.anomaly_detector_service_url, "/detect", {"service": service or None, "namespace": namespace or None, "publish": True})

    async def recent_anomalies(self, service: str, namespace: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get(self.settings.retrieval_service_url, "/anomalies/recent", {"service": service or None, "namespace": namespace or None, "limit": limit})
        return data.get("anomalies", [])

    async def anomaly_detail(self, anomaly_id: str) -> dict[str, Any] | None:
        if not anomaly_id:
            return None
        data = await self._get(self.settings.retrieval_service_url, f"/anomalies/{anomaly_id}", {})
        return data.get("anomaly", data)

    async def start_investigation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(self.settings.agent_orchestrator_service_url, "/investigations", payload)

    async def investigation_detail(self, investigation_id: str) -> dict[str, Any]:
        return await self._get(self.settings.agent_orchestrator_service_url, f"/investigations/{investigation_id}", {})

    async def create_remediation_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post(self.settings.remediation_recommender_service_url, "/plans", payload)

    async def remediation_policy(self) -> dict[str, Any]:
        return await self._get(self.settings.remediation_executor_service_url, "/safety/policy", {})

    async def dry_run_remediation(self, plan_id: str) -> dict[str, Any]:
        return await self._post(self.settings.remediation_executor_service_url, "/executions/dry-run", {"plan_id": plan_id, "dry_run": True})

    async def approval_status(self, plan_id: str) -> dict[str, Any]:
        return await self._get(self.settings.approval_service_url, f"/plans/{plan_id}/approval-status", {})

    async def execute_remediation(self, plan_id: str, approval_id: str) -> dict[str, Any]:
        return await self._post(self.settings.remediation_executor_service_url, "/executions", {"plan_id": plan_id, "approval_id": approval_id, "dry_run": False})

    async def recent_telemetry(self, service: str, namespace: str, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._get(self.settings.retrieval_service_url, "/events/recent", {"service": service or None, "namespace": namespace or None, "limit": limit})
        return data.get("events", [])

    async def _get(self, base_url: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(base_url.rstrip("/") + path, params={k: v for k, v in params.items() if v is not None and v != ""})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"payload": data}

    async def _post(self, base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(base_url.rstrip("/") + path, json={k: v for k, v in payload.items() if v is not None})
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"payload": data}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global recorder
    await clickhouse.initialize_schema()
    recorder = ClickHouseAutopilotRecorder(clickhouse)
    if settings.autopilot_publish_events:
        await producer.start()
    try:
        yield
    finally:
        if settings.autopilot_publish_events:
            await producer.stop()


app = FastAPI(title="Cascade Autopilot Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "autopilot-service", "default_mode": settings.default_mode, **auth_status(_auth_settings())}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    checks = {
        "clickhouse": await clickhouse.ping(),
        "retrieval-service": await _check(settings.retrieval_service_url),
        "anomaly-detector-service": await _check(settings.anomaly_detector_service_url),
        "agent-orchestrator-service": await _check(settings.agent_orchestrator_service_url),
        "remediation-recommender-service": await _check(settings.remediation_recommender_service_url),
        "approval-service": await _check(settings.approval_service_url),
        "remediation-executor-service": await _check(settings.remediation_executor_service_url),
    }
    response = {"status": "ok" if checks["clickhouse"] and checks["agent-orchestrator-service"] else "degraded", "service": "autopilot-service", "checks": checks}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.get("/mode")
async def mode() -> dict[str, Any]:
    policy = await _optional_policy()
    return {
        "default_mode": settings.default_mode,
        "available_modes": ["read_only", "dry_run", "local_demo_execute"],
        "safe_by_default": True,
        "real_execution_requires_existing_live_demo_gates": True,
        "remediation_policy": policy,
    }


@app.post("/runs")
async def start_run(payload: AutopilotRunRequest, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="start Autopilot run")
    active_recorder = recorder or ClickHouseAutopilotRecorder(clickhouse)
    workflow = AutopilotWorkflow(HttpAutopilotClient(settings), active_recorder)
    run = await workflow.run(payload)
    await _publish(f"autopilot.{run.status}", run, f"Autopilot run ended in {run.status}")
    return {"run": run.model_dump()}


@app.get("/runs")
async def runs(limit: int = 20, service: str | None = None, status: str | None = None) -> dict[str, Any]:
    active_recorder = recorder or ClickHouseAutopilotRecorder(clickhouse)
    rows = await active_recorder.list_runs(limit, service, status)
    return {"runs": rows, "count": len(rows)}


@app.get("/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    active_recorder = recorder or ClickHouseAutopilotRecorder(clickhouse)
    run = await active_recorder.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Autopilot run not found")
    return {"run": run, "steps": await active_recorder.get_steps(run_id)}


@app.get("/runs/{run_id}/evidence")
async def run_evidence(run_id: str) -> dict[str, Any]:
    active_recorder = recorder or ClickHouseAutopilotRecorder(clickhouse)
    run = await active_recorder.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Autopilot run not found")
    return {
        "run_id": run_id,
        "evidence": run.get("evidence", {}),
        "recommendation": run.get("recommendation", {}),
        "action": run.get("action", {}),
        "verification": run.get("verification", {}),
    }


async def _publish(event_type: str, run: AutopilotRunRecord, summary: str) -> None:
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            event_type,
            "Autopilot",
            severity="error" if run.status == "failed" else "info",
            payload=run.model_dump(),
            run_id=run.run_id,
            correlation_id=run.run_id,
            service=run.service,
            namespace=run.namespace,
            action=run.proposed_action or run.objective or event_type,
            status=run.status,
            autopilot_run_id=run.run_id,
            remediation_execution_id=run.execution_id or run.dry_run_execution_id,
            evidence_summary=summary,
            user_safe_message=summary,
        ),
    )
    if not settings.autopilot_publish_events:
        return
    try:
        await producer.send(settings.autopilot_events_topic, autopilot_event(event_type, run, summary), key=run.run_id)
    except Exception as exc:
        logger.warning("Failed to publish Autopilot event type=%s run_id=%s error=%s", event_type, run.run_id, exc)


async def _check(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(base_url.rstrip("/") + "/health")
            return response.status_code < 500
    except Exception:
        return False


async def _optional_policy() -> dict[str, Any]:
    try:
        return await HttpAutopilotClient(settings).remediation_policy()
    except Exception as exc:
        return {"available": False, "error": exc.__class__.__name__}


def _decode_run(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [
        ("evidence_json", "evidence"),
        ("recommendation_json", "recommendation"),
        ("action_json", "action"),
        ("verification_json", "verification"),
        ("run_json", "run"),
    ]:
        if source in decoded:
            decoded[target] = _loads(decoded.pop(source))
    return decoded


def _decode_step(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [("input_json", "input"), ("output_json", "output"), ("step_json", "step")]:
        if source in decoded:
            decoded[target] = _loads(decoded.pop(source))
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
