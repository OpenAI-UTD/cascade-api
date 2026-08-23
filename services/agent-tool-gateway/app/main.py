from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.agents.safety import ensure_read_only_tool, redact_sensitive
from services.shared.agents.schemas import ToolResponse
from services.shared.agents.tool_contracts import TOOL_REGISTRY, ToolContract
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    retrieval_service_url: str = "http://retrieval-service.cascade-system.svc.cluster.local:8012"
    knowledge_retrieval_service_url: str = "http://knowledge-retrieval-service.cascade-system.svc.cluster.local:8016"
    topology_service_url: str = "http://topology-service.cascade-system.svc.cluster.local:8004"
    causal_reconstruction_service_url: str = "http://causal-reconstruction-service.cascade-system.svc.cluster.local:8005"
    incident_timeline_service_url: str = "http://incident-timeline-service.cascade-system.svc.cluster.local:8006"
    anomaly_detector_service_url: str = "http://anomaly-detector-service.cascade-system.svc.cluster.local:8014"
    chaos_executor_service_url: str = "http://chaos-executor-service.cascade-system.svc.cluster.local:8020"
    remediation_recommender_service_url: str = "http://remediation-recommender-service.cascade-system.svc.cluster.local:8021"
    remediation_executor_service_url: str = "http://remediation-executor-service.cascade-system.svc.cluster.local:8023"
    tool_timeout_seconds: float = 10.0
    max_tool_response_bytes: int = 120000

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
app = FastAPI(title="Cascade Agent Tool Gateway", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "agent-tool-gateway"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    checks = {
        "retrieval-service": await _check(settings.retrieval_service_url),
        "knowledge-retrieval-service": await _check(settings.knowledge_retrieval_service_url),
        "topology-service": await _check(settings.topology_service_url),
        "causal-reconstruction-service": await _check(settings.causal_reconstruction_service_url),
        "incident-timeline-service": await _check(settings.incident_timeline_service_url),
        "anomaly-detector-service": await _check(settings.anomaly_detector_service_url),
    }
    response = {"status": "ok" if all(checks.values()) else "degraded", "service": "agent-tool-gateway", "checks": checks}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.get("/tools")
async def tools() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": contract.name,
                "description": contract.description,
                "target_service": contract.target_service,
                "read_only": contract.read_only,
                "method": contract.method,
            }
            for contract in TOOL_REGISTRY.values()
        ],
        "count": len(TOOL_REGISTRY),
    }


@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    started = time.perf_counter()
    try:
        ensure_read_only_tool(tool_name)
        contract = TOOL_REGISTRY[tool_name]
        data = await _invoke(contract, payload)
        latency = round((time.perf_counter() - started) * 1000, 2)
        response = ToolResponse(tool_name=tool_name, status="ok", data=data, evidence_refs=_refs(tool_name, data), latency_ms=latency)
        logger.info("tool_call tool=%s status=ok latency_ms=%s", tool_name, latency)
        return response.model_dump()
    except Exception as exc:
        latency = round((time.perf_counter() - started) * 1000, 2)
        logger.warning("tool_call tool=%s status=error error=%s", tool_name, exc)
        return ToolResponse(tool_name=tool_name, status="error", data={}, evidence_refs=[], latency_ms=latency, error=str(exc)).model_dump()


async def _check(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as client:
            response = await client.get(base_url.rstrip("/") + "/ready")
            if response.status_code == 404:
                response = await client.get(base_url.rstrip("/") + "/health")
            return response.status_code < 500
    except Exception:
        return False


async def _invoke(contract: ToolContract, payload: dict[str, Any]) -> dict[str, Any]:
    if contract.name in {"get_upstream_services", "get_downstream_services"}:
        return await _topology_neighbors(contract.name, payload)
    if contract.name == "get_latest_blast_radius":
        return await _latest_blast_radius(payload)
    if contract.name == "get_causal_report":
        return await _causal_report(payload)
    if contract.name == "get_target_workload":
        return await _target_workload(payload)
    base = _base_url(contract.target_service)
    path = _path(contract.path_template, payload)
    params = _params(contract, payload)
    body = _body(contract, payload)
    async with httpx.AsyncClient(timeout=min(settings.tool_timeout_seconds, contract.timeout_seconds)) as client:
        if contract.method == "GET":
            response = await client.get(base + path, params=params)
        else:
            response = await client.post(base + path, json=body)
        response.raise_for_status()
        if len(response.content) > settings.max_tool_response_bytes:
            return {"truncated": True, "raw_preview": response.text[: settings.max_tool_response_bytes]}
        return redact_sensitive(response.json())


async def _topology_neighbors(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    service = str(payload.get("service", ""))
    async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as client:
        response = await client.get(settings.topology_service_url.rstrip("/") + "/topology")
        response.raise_for_status()
        dependencies = response.json().get("dependencies", {})
    if tool_name == "get_downstream_services":
        return {"service_name": service, "downstream": dependencies.get(service, [])}
    upstream = sorted(parent for parent, children in dependencies.items() if service in children)
    return {"service_name": service, "upstream": upstream}


async def _latest_blast_radius(payload: dict[str, Any]) -> dict[str, Any]:
    service = str(payload.get("root_service") or payload.get("service") or payload.get("target_service") or "")
    if not service:
        return {"root_service": "", "blast_radius": {}, "summary": "No service supplied for blast-radius lookup."}
    async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as client:
        response = await client.post(settings.topology_service_url.rstrip("/") + "/topology/blast-radius", json={"root_service": service})
        response.raise_for_status()
        blast_radius = redact_sensitive(response.json())
    affected = blast_radius.get("affected_services", []) if isinstance(blast_radius, dict) else []
    return {
        "root_service": service,
        "blast_radius": blast_radius,
        "affected_services": affected,
        "summary": f"{service} blast radius affects {len(affected)} service(s).",
    }


async def _causal_report(payload: dict[str, Any]) -> dict[str, Any]:
    report_id = str(payload.get("report_id") or payload.get("incident_id") or payload.get("trigger_id") or "")
    async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as client:
        if report_id:
            response = await client.get(settings.retrieval_service_url.rstrip("/") + f"/causality/reports/{report_id}")
            response.raise_for_status()
            report = redact_sensitive(response.json())
            return {"report": report.get("report", report), "candidates": report.get("candidates", []), "summary": _causal_summary(report.get("report", report))}
        response = await client.get(settings.retrieval_service_url.rstrip("/") + "/causality/reports/recent", params={"limit": int(payload.get("limit", 10))})
        response.raise_for_status()
        reports_response = redact_sensitive(response.json())
    reports = reports_response.get("reports", []) if isinstance(reports_response, dict) else []
    if not isinstance(reports, list):
        return {"reports": [], "summary": "Causal report endpoint returned no report list."}
    service = str(payload.get("service") or "")
    filtered = [item for item in reports if not service or item.get("target_service") == service]
    latest = sorted(filtered or reports, key=lambda item: str(item.get("generated_at", "")), reverse=True)[:1]
    report = latest[0] if latest else {}
    return {"report": report, "reports": latest, "summary": _causal_summary(report) if report else "No causal reports available."}


async def _target_workload(payload: dict[str, Any]) -> dict[str, Any]:
    service = str(payload.get("service") or payload.get("target_service") or "")
    async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as client:
        response = await client.get(settings.topology_service_url.rstrip("/") + "/target/workload")
        response.raise_for_status()
        target = redact_sensitive(response.json())
    services = target.get("services", []) if isinstance(target, dict) else []
    dependencies = target.get("dependencies", {}) if isinstance(target, dict) else {}
    return {
        "service": service,
        "workload": target.get("name", "") if isinstance(target, dict) else "",
        "target": target,
        "service_known": service in services if service else False,
        "dependencies": dependencies.get(service, []) if service and isinstance(dependencies, dict) else [],
        "summary": f"Target workload is {target.get('name', 'unknown') if isinstance(target, dict) else 'unknown'} with {len(services)} service(s).",
    }


def _causal_summary(incident: Any) -> str:
    if not isinstance(incident, dict) or not incident:
        return "No causal report available."
    target = incident.get("target_service") or incident.get("root_cause_service") or "unknown"
    status = incident.get("status") or "unknown"
    summary = incident.get("summary") or "No summary returned."
    return f"Causal report target={target}, status={status}, summary={summary}"


def _base_url(target: str) -> str:
    return {
        "retrieval-service": settings.retrieval_service_url.rstrip("/"),
        "knowledge-retrieval-service": settings.knowledge_retrieval_service_url.rstrip("/"),
        "topology-service": settings.topology_service_url.rstrip("/"),
        "incident-timeline-service": settings.incident_timeline_service_url.rstrip("/"),
        "anomaly-detector-service": settings.anomaly_detector_service_url.rstrip("/"),
        "causal-reconstruction-service": settings.causal_reconstruction_service_url.rstrip("/"),
        "chaos-executor-service": settings.chaos_executor_service_url.rstrip("/"),
        "remediation-recommender-service": settings.remediation_recommender_service_url.rstrip("/"),
        "remediation-executor-service": settings.remediation_executor_service_url.rstrip("/"),
    }[target]


def _path(template: str, payload: dict[str, Any]) -> str:
    path = template
    for key, value in payload.items():
        path = path.replace("{" + key + "}", str(value))
    return path


def _params(contract: ToolContract, payload: dict[str, Any]) -> dict[str, Any]:
    if contract.method != "GET":
        return {}
    return {k: v for k, v in payload.items() if "{" + k + "}" not in contract.path_template and v not in (None, "")}


def _body(contract: ToolContract, payload: dict[str, Any]) -> dict[str, Any]:
    if contract.name in {"search_knowledge", "build_knowledge_context"}:
        return {"query": payload.get("query", ""), "limit": int(payload.get("limit", 5)), "filters": payload.get("filters", {})}
    if contract.name == "get_service_impact":
        return {"root_service": payload.get("root_service") or payload.get("service")}
    if contract.name == "search_similar_incidents":
        return {"query": payload.get("query", ""), "limit": int(payload.get("limit", 5)), "service": payload.get("service"), "namespace": payload.get("namespace"), "memory_type": payload.get("memory_type")}
    if contract.name == "generate_incident_timeline":
        return {"experiment": payload.get("experiment", {}), "incident": payload.get("incident", {}), "topology_impact": payload.get("topology_impact", {})}
    if contract.name == "generate_investigation_report_template":
        incident = payload.get("incident") or {"experiment_id": "", "root_cause_service": payload.get("service", ""), "affected_services": [], "evidence": []}
        return {"incident": incident}
    return payload


def _refs(tool_name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for key in ["anomalies", "events", "features", "incidents", "results", "evidence_chunks"]:
        value = data.get(key)
        if isinstance(value, list):
            for item in value[:5]:
                refs.append({"tool": tool_name, "type": key, "id": _id(item)})
    if data.get("anomaly"):
        refs.append({"tool": tool_name, "type": "anomaly", "id": _id(data["anomaly"])})
    if data.get("incident"):
        refs.append({"tool": tool_name, "type": "incident", "id": _id(data["incident"])})
    if data.get("blast_radius"):
        refs.append({"tool": tool_name, "type": "blast_radius", "id": _id(data["blast_radius"])})
    if data.get("workload"):
        refs.append({"tool": tool_name, "type": "workload", "id": str(data["workload"])[:80]})
    return refs[:10]


def _id(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)[:80]
    for key in ["anomaly_id", "event_id", "window_id", "incident_id", "chunk_id", "document_id", "memory_id", "root_service"]:
        if item.get(key):
            return str(item[key])
    return json.dumps(item, sort_keys=True, default=str)[:80]
