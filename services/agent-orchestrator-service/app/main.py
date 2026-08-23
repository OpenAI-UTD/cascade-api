from __future__ import annotations

import importlib.util
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.agents.graph_runtime import DeterministicGraphRuntime
from services.shared.agents.reports import build_lifecycle_event
from services.shared.agents.schemas import AgentState, InvestigationRequest
from services.shared.agents.tool_client import ToolGatewayClient
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    agent_tool_gateway_url: str = "http://agent-tool-gateway.cascade-system.svc.cluster.local:8017"
    agent_runtime_mode: str = "deterministic"
    llm_provider: str = "none"
    llm_model: str = ""
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    agent_events_topic: str = "agent.investigations"
    agent_publish_events: bool = True
    tool_timeout_seconds: float = 20.0
    max_agent_steps: int = 20

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
gateway = ToolGatewayClient(settings.agent_tool_gateway_url, settings.tool_timeout_seconds)
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="agent-orchestrator-service"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    await clickhouse.initialize_schema()
    if settings.agent_publish_events:
        await producer.start()
    try:
        yield
    finally:
        if settings.agent_publish_events:
            await producer.stop()


app = FastAPI(title="Cascade Agent Orchestrator Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "agent-orchestrator-service"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    gw = await gateway.ready()
    response = {"status": "ok" if ch and gw else "degraded", "service": "agent-orchestrator-service", "clickhouse": ch, "agent_tool_gateway": gw, "event_publishing": settings.agent_publish_events}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.get("/agents/status")
async def agents_status() -> dict[str, Any]:
    return {
        "deterministic_planner_available": True,
        "langgraph_available": importlib.util.find_spec("langgraph") is not None,
        "llm_mode_available": settings.llm_provider != "none" and bool(settings.llm_model),
        "configured_mode": settings.agent_runtime_mode,
        "active_mode": "deterministic",
        "llm_status": "LLM mode not configured; deterministic mode active" if settings.llm_provider == "none" else "LLM hooks configured but acceptance uses deterministic mode",
        "safety": {"read_only_tools": True, "remediation_execution": False, "max_agent_steps": settings.max_agent_steps},
    }


@app.get("/tools")
async def tools() -> dict[str, Any]:
    return await gateway.tools()


@app.post("/investigations")
async def start_investigation(payload: InvestigationRequest) -> dict[str, Any]:
    if payload.mode != "deterministic":
        payload.mode = "deterministic"
    investigation_id = "inv_" + uuid.uuid4().hex[:20]
    state = AgentState(investigation_id=investigation_id, trigger=payload)
    created = _now()
    await _insert_run(state, created, "running", "", 0.0, [], "")
    await _publish("investigation.started", state, "running", payload.objective)
    try:
        runtime = DeterministicGraphRuntime(lambda tool, body: _call_tool(state, tool, body), lambda node, role, step_type, inp, out, status_: _record_step(state, node, role, step_type, inp, out, status_))
        report = await runtime.run(state)
        await _insert_report(report)
        await _insert_run(state, created, "completed", report["summary"], float(report["confidence"]), report["evidence"], "")
        await _publish("investigation.completed", state, "completed", report["summary"])
        return {"investigation_id": investigation_id, "status": "completed", "summary": report["summary"], "report_id": report["report_id"], "confidence": report["confidence"], "tools_used": sorted({c["tool_name"] for c in state.tool_call_history}), "evidence_refs": report["evidence"]}
    except Exception as exc:
        logger.exception("Investigation failed id=%s: %s", investigation_id, exc)
        await _insert_run(state, created, "failed", "", 0.0, [], str(exc))
        await _publish("investigation.failed", state, "failed", str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/investigations/from-latest-anomaly")
async def from_latest_anomaly(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    result = await gateway.call("get_recent_anomalies", {"limit": 1, "service": payload.get("service"), "namespace": payload.get("namespace")})
    anomalies = result.get("data", {}).get("anomalies", [])
    if not anomalies:
        raise HTTPException(status_code=404, detail="No anomaly available for investigation")
    anomaly = anomalies[0]
    req = InvestigationRequest(
        trigger_type="anomaly",
        trigger_id=anomaly.get("anomaly_id"),
        service=anomaly.get("service"),
        namespace=anomaly.get("namespace") or payload.get("namespace") or "cascade-targets",
        objective=f"Investigate anomaly for {anomaly.get('service', 'unknown service')}",
        mode="deterministic",
        max_steps=int(payload.get("max_steps", 12)),
    )
    return await start_investigation(req)


@app.get("/investigations")
async def investigations(limit: int = 20, service: str | None = None, status: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_investigation_runs(limit, service, status)
    return {"investigations": [_decode(row) for row in rows], "count": len(rows)}


@app.get("/investigations/{investigation_id}")
async def investigation_detail(investigation_id: str) -> dict[str, Any]:
    detail = await clickhouse.investigation_detail(investigation_id)
    if detail["run"] is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return _decode_detail(detail)


@app.get("/investigations/{investigation_id}/report")
async def investigation_report(investigation_id: str) -> dict[str, Any]:
    detail = await clickhouse.investigation_detail(investigation_id)
    if detail["report"] is None:
        raise HTTPException(status_code=404, detail="Investigation report not found")
    return _decode_report(detail["report"])


async def _call_tool(state: AgentState, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    result = await gateway.call(tool_name, payload)
    latency = round((time.perf_counter() - started) * 1000, 2)
    await clickhouse.insert_agent_tool_call({
        "call_id": "call_" + uuid.uuid4().hex[:20],
        "investigation_id": state.investigation_id,
        "called_at": _now(),
        "tool_name": tool_name,
        "target_service": tool_name,
        "status": result.get("status", "unknown"),
        "latency_ms": latency,
        "request_json": _json(payload),
        "response_summary": _json({"evidence_refs": result.get("evidence_refs", []), "keys": list(result.get("data", {}).keys())})[:1000],
        "error_message": result.get("error") or "",
    })
    if result.get("status") == "ok":
        await _publish("investigation.step.completed", state, "running", f"Tool {tool_name} completed")
    return result


async def _record_step(state: AgentState, node: str, role: str, step_type: str, inp: dict[str, Any], out: dict[str, Any], status_: str) -> None:
    await clickhouse.insert_agent_step({
        "step_id": "step_" + uuid.uuid4().hex[:20],
        "investigation_id": state.investigation_id,
        "created_at": _now(),
        "node_name": node,
        "agent_role": role,
        "step_type": step_type,
        "status": status_,
        "input_json": _json(inp)[:8000],
        "output_json": _json(out)[:8000],
        "tool_name": "",
        "tool_latency_ms": 0.0,
        "error_message": "",
    })


async def _insert_run(state: AgentState, created: str, status_: str, summary: str, confidence: float, evidence: list[dict[str, Any]], error: str) -> None:
    tools = sorted({c.get("tool_name", "") for c in state.tool_call_history if c.get("tool_name")})
    await clickhouse.insert_investigation_run({
        "investigation_id": state.investigation_id,
        "created_at": created,
        "updated_at": _now(),
        "completed_at": _now() if status_ in {"completed", "failed"} else None,
        "status": status_,
        "mode": "deterministic",
        "trigger_type": state.trigger.trigger_type,
        "trigger_id": state.trigger.trigger_id or "",
        "service": state.trigger.service or "",
        "namespace": state.trigger.namespace,
        "severity": _severity(state),
        "risk_score": _risk(state),
        "title": f"Investigation - {state.trigger.service or state.trigger.trigger_type}",
        "objective": state.trigger.objective,
        "final_summary": summary,
        "confidence": confidence,
        "tools_used_json": _json(tools),
        "evidence_refs_json": _json(evidence),
        "config_json": _json({"max_steps": state.trigger.max_steps, "llm_provider": settings.llm_provider}),
        "error_message": error,
    })


async def _insert_report(report: dict[str, Any]) -> None:
    await clickhouse.insert_investigation_report({
        "report_id": report["report_id"],
        "investigation_id": report["investigation_id"],
        "generated_at": report["generated_at"],
        "title": report["title"],
        "summary": report["summary"],
        "suspected_root_cause": report["suspected_root_cause"],
        "affected_services_json": _json(report["affected_services"]),
        "evidence_json": _json(report["evidence"]),
        "timeline_json": _json(report["timeline"]),
        "anomaly_refs_json": _json(report["anomaly_refs"]),
        "knowledge_refs_json": _json(report["knowledge_refs"]),
        "recommended_next_steps_json": _json(report["recommended_next_steps"]),
        "suggested_remediation_json": _json(report["suggested_remediation"]),
        "confidence": report["confidence"],
        "markdown_report": report["markdown_report"],
        "report_json": _json(report),
    })


async def _publish(event_type: str, state: AgentState, status_: str, summary: str) -> None:
    if not settings.agent_publish_events:
        return
    try:
        await producer.send(settings.agent_events_topic, build_lifecycle_event(event_type, state, status_, summary), key=state.investigation_id)
    except Exception as exc:
        logger.warning("Failed to publish agent event type=%s id=%s error=%s", event_type, state.investigation_id, exc)


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ["tools_used_json", "evidence_refs_json", "config_json"]:
        if key in decoded:
            decoded[key.replace("_json", "")] = _loads(decoded.pop(key))
    return decoded


def _decode_report(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ["affected_services_json", "evidence_json", "timeline_json", "anomaly_refs_json", "knowledge_refs_json", "recommended_next_steps_json", "suggested_remediation_json", "report_json"]:
        if key in decoded:
            decoded[key.replace("_json", "")] = _loads(decoded.pop(key))
    return decoded


def _decode_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {"run": _decode(detail["run"]), "steps": detail["steps"], "tool_calls": detail["tool_calls"], "report": _decode_report(detail["report"]) if detail["report"] else None}


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _severity(state: AgentState) -> str:
    return str(state.anomaly_refs[0].get("severity", "")) if state.anomaly_refs else ""


def _risk(state: AgentState) -> float:
    try:
        return float(state.anomaly_refs[0].get("risk_score", 0.0)) if state.anomaly_refs else 0.0
    except Exception:
        return 0.0
