from __future__ import annotations

import logging
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.causality import CausalityAnalyzeRequest, analyze_causality
from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.embedding.deterministic import embed_text
from services.shared.events.mapping import build_memory_document, build_memory_payload, map_incident, map_incident_report, map_topology_snapshot
from services.shared.knowledge.context import assemble_context_pack
from services.shared.knowledge.search import KnowledgeSearchRequest
from services.shared.rca import build_rca_report
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.storage.qdrant_client import QdrantClient, QdrantSettings
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    embedding_dimensions: int = 128
    qdrant_knowledge_collection: str = "cascade_knowledge_base"

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=50)
    service: str | None = None
    namespace: str | None = None
    memory_type: str | None = None


class MemoryIndexRequest(BaseModel):
    memory_type: str
    payload: dict[str, Any]
    source: str = "retrieval-service"
    source_topic: str = ""


class IncidentIngestRequest(BaseModel):
    incident: dict[str, Any]


class ReportIngestRequest(BaseModel):
    report: dict[str, Any]
    incident: dict[str, Any] | None = None


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
qdrant = QdrantClient(QdrantSettings(embedding_dimensions=settings.embedding_dimensions))
knowledge_qdrant = QdrantClient(QdrantSettings(embedding_dimensions=settings.embedding_dimensions, qdrant_knowledge_collection=settings.qdrant_knowledge_collection))


@asynccontextmanager
async def lifespan(_: FastAPI):
    await clickhouse.initialize_schema()
    await qdrant.ensure_collection()
    await knowledge_qdrant.ensure_knowledge_collection()
    yield


app = FastAPI(title="Cascade Retrieval Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    clickhouse_ready = await clickhouse.ping()
    qdrant_ready = await qdrant.ping()
    collection_ready = await qdrant.collection_ready()
    knowledge_collection_ready = await knowledge_qdrant.knowledge_collection_ready()
    return {
        "status": "ok" if clickhouse_ready and qdrant_ready and collection_ready and knowledge_collection_ready else "degraded",
        "service": "retrieval-service",
        "clickhouse": clickhouse_ready,
        "qdrant": qdrant_ready,
        "collection": collection_ready,
        "knowledge_collection": knowledge_collection_ready,
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    response = await health()
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.get("/events/recent")
async def events_recent(limit: int = 20, service: str | None = None, namespace: str | None = None, event_type: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_telemetry(limit, service, namespace, event_type)
    return {"events": rows, "count": len(rows)}


@app.get("/events/service/{service_name}")
async def events_service(service_name: str, limit: int = 50) -> dict[str, Any]:
    rows = await clickhouse.service_history(service_name, limit)
    return {"service": service_name, "events": rows, "count": len(rows)}


@app.get("/audit/events")
async def audit_events(
    limit: int = 100,
    subsystem: str | None = None,
    severity: str | None = None,
    service: str | None = None,
    namespace: str | None = None,
    status: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    rows = await clickhouse.recent_audit_events(limit, subsystem, severity, service, namespace, status, start_time, end_time, correlation_id)
    return {"events": rows, "count": len(rows)}


@app.get("/audit/events/{event_id}")
async def audit_event_detail(event_id: str) -> dict[str, Any]:
    row = await clickhouse.audit_event(event_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit event not found")
    return {"event": row}


@app.get("/audit/timeline")
async def audit_timeline(correlation_id: str, limit: int = 200) -> dict[str, Any]:
    rows = await clickhouse.audit_timeline(correlation_id, limit)
    return {"correlation_id": correlation_id, "events": rows, "count": len(rows)}


@app.get("/audit/service/{namespace}/{service}")
async def audit_service(namespace: str, service: str, limit: int = 200) -> dict[str, Any]:
    rows = await clickhouse.audit_service_timeline(namespace, service, limit)
    return {"namespace": namespace, "service": service, "events": rows, "count": len(rows)}


@app.get("/audit/autopilot/{run_id}")
async def audit_autopilot(run_id: str, limit: int = 200) -> dict[str, Any]:
    rows = await clickhouse.audit_autopilot_timeline(run_id, limit)
    return {"run_id": run_id, "events": rows, "count": len(rows)}


@app.get("/experiments/recent")
async def experiments_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_experiments(limit)
    return {"experiments": rows, "count": len(rows)}


@app.get("/incidents/recent")
async def incidents_recent(limit: int = 20, service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_incidents(limit, service)
    return {"incidents": rows, "count": len(rows)}


@app.get("/incidents/{incident_id}")
async def incident_detail(incident_id: str) -> dict[str, Any]:
    result = await clickhouse.incident_with_reports(incident_id)
    if result["incident"] is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return result


@app.post("/incidents")
async def ingest_incident(payload: IncidentIngestRequest) -> dict[str, Any]:
    row = map_incident(payload.incident)
    await clickhouse.insert_rows("incidents", [row])
    memory = await index_memory("reconstructed_incident", {**payload.incident, "incident_id": row["incident_id"]}, "incident-ingest")
    return {"status": "ok", "incident_id": row["incident_id"], "memory": memory}


@app.post("/reports")
async def ingest_report(payload: ReportIngestRequest) -> dict[str, Any]:
    row = map_incident_report(payload.report, payload.incident)
    await clickhouse.insert_rows("incident_reports", [row])
    source_payload = {**(payload.incident or {}), **payload.report, "incident_id": row["incident_id"], "report_id": row["report_id"]}
    memory = await index_memory("incident_report", source_payload, "report-ingest")
    return {"status": "ok", "report_id": row["report_id"], "incident_id": row["incident_id"], "memory": memory}


@app.post("/topology/snapshots")
async def ingest_topology(payload: dict[str, Any]) -> dict[str, Any]:
    row = map_topology_snapshot(payload)
    await clickhouse.insert_rows("topology_snapshots", [row])
    memory = await index_memory("topology_context", {**payload, "snapshot_id": row["snapshot_id"]}, "topology-ingest")
    return {"status": "ok", "snapshot_id": row["snapshot_id"], "memory": memory}


@app.post("/memory/search")
async def memory_search(payload: MemorySearchRequest) -> dict[str, Any]:
    vector = embed_text(payload.query, settings.embedding_dimensions)
    results = await qdrant.search(vector, payload.limit, payload.service, payload.namespace, payload.memory_type)
    return {"query": payload.query, "results": results, "count": len(results)}


@app.post("/memory/index")
async def memory_index(payload: MemoryIndexRequest) -> dict[str, Any]:
    memory = await index_memory(payload.memory_type, payload.payload, payload.source, payload.source_topic)
    return {"status": "ok", "memory": memory}


@app.get("/topology/snapshot/latest")
async def topology_latest() -> dict[str, Any]:
    snapshot = await clickhouse.latest_topology_snapshot()
    return {"snapshot": snapshot}


@app.get("/features/recent")
async def features_recent(limit: int = 20, service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_feature_windows(limit, service)
    return {"features": rows, "count": len(rows)}


@app.get("/anomalies/recent")
async def anomalies_recent(limit: int = 20, service: str | None = None, severity: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_anomalies(limit, service, severity)
    return {"anomalies": rows, "count": len(rows)}


@app.get("/anomalies/service/{service_name}")
async def anomalies_service(service_name: str, limit: int = 50) -> dict[str, Any]:
    rows = await clickhouse.service_anomalies(service_name, limit)
    return {"service": service_name, "anomalies": rows, "count": len(rows)}


@app.get("/anomalies/{anomaly_id}")
async def anomaly_detail(anomaly_id: str) -> dict[str, Any]:
    anomaly = await clickhouse.anomaly_detail(anomaly_id)
    if anomaly is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anomaly not found")
    return {"anomaly": anomaly}


@app.post("/causality/analyze")
async def causality_analyze(payload: CausalityAnalyzeRequest) -> dict[str, Any]:
    target_service = payload.target_service or payload.service
    feature_windows = await clickhouse.recent_feature_windows(payload.limit)
    anomalies = await clickhouse.recent_anomalies(min(payload.limit, 500))
    if payload.namespace:
        feature_windows = [row for row in feature_windows if row.get("namespace") == payload.namespace]
        anomalies = [row for row in anomalies if row.get("namespace") == payload.namespace]
    topology = await clickhouse.latest_topology_snapshot()
    report = analyze_causality(
        feature_windows,
        anomalies,
        topology,
        target_service=target_service,
        target_feature=payload.target_feature,
        source_feature=payload.source_feature,
        max_lag_windows=payload.max_lag_windows,
        min_correlation_samples=payload.min_correlation_samples,
        min_granger_samples=payload.min_granger_samples,
        granger_max_lag=payload.granger_max_lag,
    )
    await clickhouse.insert_causal_report(report)
    return {"report": report}


@app.post("/rca/analyze")
async def rca_analyze(payload: CausalityAnalyzeRequest) -> dict[str, Any]:
    target_service = payload.target_service or payload.service
    feature_windows = await clickhouse.recent_feature_windows(payload.limit)
    anomalies = await clickhouse.recent_anomalies(min(payload.limit, 500))
    experiments = await clickhouse.recent_experiments(min(payload.limit, 200))
    if payload.namespace:
        feature_windows = [row for row in feature_windows if row.get("namespace") == payload.namespace]
        anomalies = [row for row in anomalies if row.get("namespace") == payload.namespace]
        experiments = [row for row in experiments if row.get("target_namespace") == payload.namespace]
    topology = await clickhouse.latest_topology_snapshot()
    report = build_rca_report(feature_windows, anomalies, topology, experiments, target_service=target_service)
    await clickhouse.insert_rca_report(report)
    await emit_audit_event(
        clickhouse,
        build_audit_event(
            "rca.report.created",
            "RCA",
            payload=report,
            service=str(report.get("target_service") or target_service or ""),
            namespace=str(payload.namespace or ""),
            status=str(report.get("status") or ""),
            rca_report_id=str(report.get("report_id") or ""),
            evidence_summary=str(report.get("explanation") or "RCA report created"),
            user_safe_message=f"RCA report created for {report.get('target_service') or target_service or 'service'}",
        ),
    )
    return {"report": report}


@app.get("/rca/recent")
async def rca_recent(limit: int = 20, service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_rca_reports(limit, service)
    return {"reports": [decode_rca_report_row(row) for row in rows], "count": len(rows)}


@app.get("/rca/{report_id}")
async def rca_detail(report_id: str) -> dict[str, Any]:
    row = await clickhouse.rca_report_detail(report_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RCA report not found")
    return {"report": decode_rca_report_row(row)}


@app.get("/causality/reports/recent")
async def causality_reports_recent(limit: int = 20, target_service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_causal_reports(limit, target_service)
    return {"reports": [decode_causal_report_row(row) for row in rows], "count": len(rows)}


@app.get("/causality/reports/{report_id}")
async def causality_report_detail(report_id: str) -> dict[str, Any]:
    result = await clickhouse.causal_report_detail(report_id)
    if result["report"] is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Causal report not found")
    return {
        "report": decode_causal_report_row(result["report"]),
        "candidates": [decode_causal_candidate_row(row) for row in result["candidates"]],
    }


@app.post("/knowledge/search")
async def knowledge_search(payload: KnowledgeSearchRequest) -> dict[str, Any]:
    started = time.perf_counter()
    query_id = "kq_" + uuid.uuid4().hex[:20]
    vector = embed_text(payload.query, settings.embedding_dimensions)
    filters = payload.filters.model_dump()
    raw_results = await knowledge_qdrant.search_knowledge(vector, payload.limit, filters)
    results = await enrich_knowledge_results(raw_results)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    response = {"query_id": query_id, "query": payload.query, "results": results, "count": len(results), "latency_ms": latency_ms}
    await record_knowledge_query(query_id, payload.query, filters, results, latency_ms, response)
    return response


@app.post("/knowledge/context")
async def knowledge_context(payload: KnowledgeSearchRequest) -> dict[str, Any]:
    search = await knowledge_search(payload)
    return assemble_context_pack(payload.query, search["results"])


@app.get("/knowledge/stats")
async def knowledge_stats() -> dict[str, Any]:
    return {**await clickhouse.knowledge_stats(), "qdrant_knowledge_points": await knowledge_qdrant.count(settings.qdrant_knowledge_collection)}


@app.get("/knowledge/documents/recent")
async def knowledge_documents_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_knowledge_documents(limit)
    return {"documents": [decode_knowledge_row(row) for row in rows], "count": len(rows)}


@app.get("/knowledge/runs/recent")
async def knowledge_runs_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_knowledge_runs(limit)
    return {"runs": rows, "count": len(rows)}


@app.get("/debug/counts")
async def debug_counts() -> dict[str, Any]:
    tables = [
        "telemetry_events",
        "experiment_events",
        "incidents",
        "incident_reports",
        "topology_snapshots",
        "telemetry_feature_windows",
        "anomaly_events",
        "causal_reports",
        "causal_candidates",
        "rca_reports",
        "model_runs",
        "knowledge_documents",
        "knowledge_chunks",
        "knowledge_ingestion_runs",
        "knowledge_queries",
        "autopilot_runs",
        "autopilot_steps",
        "chaos_campaigns",
        "chaos_campaign_runs",
        "chaos_campaign_steps",
        "audit_events",
    ]
    counts = {}
    for table in tables:
        try:
            counts[table] = await clickhouse.count(table)
        except Exception:
            counts[table] = None
    counts["qdrant_points"] = await qdrant.count()
    try:
        counts["qdrant_knowledge_points"] = await knowledge_qdrant.count(settings.qdrant_knowledge_collection)
    except Exception:
        counts["qdrant_knowledge_points"] = None
    return counts


async def index_memory(memory_type: str, payload: dict[str, Any], source: str, source_topic: str = "") -> dict[str, Any]:
    await qdrant.ensure_collection()
    memory_payload = build_memory_payload(memory_type, payload, source, source_topic)
    vector = embed_text(build_memory_document(memory_type, payload), settings.embedding_dimensions)
    await qdrant.upsert_point(memory_payload["memory_id"], vector, memory_payload)
    return memory_payload


async def enrich_knowledge_results(raw_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = [item.get("payload", {}).get("chunk_id", "") for item in raw_results if item.get("payload", {}).get("chunk_id")]
    rows = {row["chunk_id"]: decode_knowledge_row(row) for row in await clickhouse.knowledge_chunks_by_ids(chunk_ids)}
    results = []
    for item in raw_results:
        payload = item.get("payload", {})
        chunk_id = payload.get("chunk_id", "")
        row = rows.get(chunk_id, {})
        results.append({
            "score": item.get("score", 0.0),
            "chunk_id": chunk_id,
            "document_id": payload.get("document_id", row.get("document_id", "")),
            "title": payload.get("title", row.get("title", "")),
            "source_type": payload.get("source_type", row.get("source_type", "")),
            "document_type": payload.get("document_type", row.get("document_type", "")),
            "source_path": payload.get("source_path", row.get("source_path", "")),
            "source_uri": payload.get("source_uri", ""),
            "service": payload.get("service", row.get("service", "")),
            "namespace": payload.get("namespace", row.get("namespace", "")),
            "severity": payload.get("severity", row.get("severity", "")),
            "phase": payload.get("phase", row.get("phase", "")),
            "chunk_text": row.get("chunk_text", payload.get("compact_text", "")),
            "metadata": row.get("metadata", {}),
        })
    return results


async def record_knowledge_query(query_id: str, query: str, filters: dict[str, Any], results: list[dict[str, Any]], latency_ms: float, response: dict[str, Any]) -> None:
    await clickhouse.insert_knowledge_query({
        "query_id": query_id,
        "queried_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "query_text": query,
        "filters_json": json.dumps(filters, sort_keys=True),
        "result_count": len(results),
        "top_score": float(results[0].get("score", 0.0)) if results else 0.0,
        "latency_ms": latency_ms,
        "response_json": json.dumps(response, sort_keys=True, default=str),
    })


def decode_knowledge_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [("tags_json", "tags"), ("metadata_json", "metadata")]:
        value = decoded.pop(source, None)
        if value is not None:
            try:
                decoded[target] = json.loads(value)
            except Exception:
                decoded[target] = value
    return decoded


def decode_causal_report_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [("methodology_json", "methodology"), ("limitations_json", "limitations"), ("report_json", "report")]:
        value = decoded.pop(source, None)
        if value is not None:
            try:
                decoded[target] = json.loads(value)
            except Exception:
                decoded[target] = value
    return decoded


def decode_causal_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    json_fields = [
        ("pearson_json", "pearson"),
        ("spearman_json", "spearman"),
        ("granger_json", "granger"),
        ("anomaly_context_json", "anomaly_context"),
        ("limitations_json", "limitations"),
        ("candidate_json", "candidate"),
    ]
    for source, target in json_fields:
        value = decoded.pop(source, None)
        if value is not None:
            try:
                decoded[target] = json.loads(value)
            except Exception:
                decoded[target] = value
    return decoded


def decode_rca_report_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [
        ("affected_services_json", "affected_downstream_services"),
        ("related_chaos_json", "related_chaos_experiment"),
        ("evidence_json", "evidence"),
        ("timeline_json", "timeline"),
        ("limitations_json", "limitations"),
        ("report_json", "report"),
    ]:
        value = decoded.pop(source, None)
        if value is not None:
            try:
                decoded[target] = json.loads(value)
            except Exception:
                decoded[target] = value
    if isinstance(decoded.get("report"), dict):
        merged = dict(decoded["report"])
        merged.update({key: value for key, value in decoded.items() if key != "report"})
        return merged
    return decoded
