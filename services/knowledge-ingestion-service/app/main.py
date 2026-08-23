from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.embedding.deterministic import embed_text
from services.shared.knowledge.chunking import chunk_document
from services.shared.knowledge.documents import build_document, load_repo_documents
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.storage.qdrant_client import QdrantClient, QdrantSettings
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    qdrant_knowledge_collection: str = "cascade_knowledge_base"
    embedding_dimensions: int = 128
    knowledge_root_path: str = "/app/knowledge"
    chunk_size: int = 1000
    chunk_overlap: int = 150
    knowledge_background_enabled: bool = False
    knowledge_ingest_interval_seconds: int = 300

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class DocsIngestRequest(BaseModel):
    paths: list[str] = Field(default_factory=lambda: ["docs/knowledge", "README.md"])
    include_patterns: list[str] = Field(default_factory=lambda: [".md", ".txt", "*.json"])
    exclude_patterns: list[str] = Field(default_factory=lambda: [".git", "__pycache__", "node_modules"])
    source_type: str = "repo_docs"


class LimitRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
qdrant = QdrantClient(QdrantSettings(embedding_dimensions=settings.embedding_dimensions, qdrant_knowledge_collection=settings.qdrant_knowledge_collection))
last_ingest: dict[str, Any] = {"status": "not_started", "last_error": ""}
_background_task: asyncio.Task[None] | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _background_task
    await clickhouse.initialize_schema()
    await qdrant.ensure_knowledge_collection()
    if settings.knowledge_background_enabled:
        _background_task = asyncio.create_task(background_loop())
    try:
        yield
    finally:
        if _background_task:
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Cascade Knowledge Ingestion Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "knowledge-ingestion-service", **last_ingest}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    qd = await qdrant.ping()
    collection = await qdrant.knowledge_collection_ready()
    tables = {name: await clickhouse.table_exists(name) for name in ["knowledge_documents", "knowledge_chunks", "knowledge_ingestion_runs", "knowledge_queries"]} if ch else {}
    response = {"status": "ok" if ch and qd and collection and all(tables.values()) else "degraded", "service": "knowledge-ingestion-service", "clickhouse": ch, "qdrant": qd, "collection": collection, "tables": tables}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.post("/ingest/docs")
async def ingest_docs(payload: DocsIngestRequest | None = None) -> dict[str, Any]:
    payload = payload or DocsIngestRequest()
    roots = [str((Path(settings.knowledge_root_path) / path).resolve()) for path in payload.paths]
    docs = load_repo_documents(roots, payload.include_patterns, payload.exclude_patterns, payload.source_type)
    return await index_documents("docs", payload.source_type, docs, payload.model_dump())


@app.post("/ingest/incidents")
async def ingest_incidents(payload: LimitRequest | None = None) -> dict[str, Any]:
    payload = payload or LimitRequest(limit=50)
    incidents = await clickhouse.recent_incidents(payload.limit)
    reports = await clickhouse.recent_incident_reports_for_knowledge(payload.limit)
    docs = []
    for row in incidents:
        content = "Incident\n" + json.dumps(row, sort_keys=True, indent=2)
        docs.append(build_document(content, f"clickhouse/incidents/{row.get('incident_id', uuid.uuid4())}.json", "incident_history", metadata={"document_type": "incident", "service": row.get("root_cause_service", ""), "severity": row.get("severity", ""), "tags": ["incident"]}))
    for row in reports:
        content = row.get("markdown_report") or json.dumps(row, sort_keys=True, indent=2)
        docs.append(build_document(content, f"clickhouse/incident_reports/{row.get('report_id', uuid.uuid4())}.md", "incident_report", metadata={"document_type": "incident_report", "service": row.get("root_cause_service", ""), "severity": row.get("severity", ""), "tags": ["incident_report"]}))
    return await index_documents("incidents", "incident_report", docs, payload.model_dump())


@app.post("/ingest/anomalies")
async def ingest_anomalies(payload: LimitRequest | None = None) -> dict[str, Any]:
    payload = payload or LimitRequest(limit=100)
    rows = await clickhouse.recent_anomalies(payload.limit)
    docs = []
    for row in rows:
        title = f"Anomaly {row.get('service', 'unknown')} {row.get('severity', '')}".strip()
        content = f"# {title}\n\nExplanation: {row.get('explanation', '')}\n\n" + json.dumps(row, sort_keys=True, indent=2)
        docs.append(build_document(content, f"clickhouse/anomaly_events/{row.get('anomaly_id', uuid.uuid4())}.json", "anomaly_events", metadata={"document_type": "anomaly_record", "title": title, "service": row.get("service", ""), "namespace": row.get("namespace", ""), "severity": row.get("severity", ""), "tags": ["anomaly"]}))
    return await index_documents("anomalies", "anomaly_events", docs, payload.model_dump())


@app.post("/ingest/topology")
async def ingest_topology(payload: LimitRequest | None = None) -> dict[str, Any]:
    payload = payload or LimitRequest(limit=10)
    rows = await clickhouse.recent_topology_snapshots(payload.limit)
    docs = []
    for row in rows:
        content = "# Topology Snapshot\n\n" + json.dumps(row, sort_keys=True, indent=2)
        docs.append(build_document(content, f"clickhouse/topology_snapshots/{row.get('snapshot_id', uuid.uuid4())}.json", "topology_snapshots", metadata={"document_type": "topology", "phase": "phase-3", "tags": ["topology"]}))
    return await index_documents("topology", "topology_snapshots", docs, payload.model_dump())


@app.post("/ingest/all")
async def ingest_all() -> dict[str, Any]:
    results = [
        await ingest_docs(DocsIngestRequest()),
        await ingest_incidents(LimitRequest(limit=50)),
        await ingest_anomalies(LimitRequest(limit=100)),
        await ingest_topology(LimitRequest(limit=10)),
    ]
    return {
        "status": "ok" if all(item["status"] == "success" for item in results) else "partial",
        "results": results,
        "documents_ingested": sum(item["documents_ingested"] for item in results),
        "chunks_indexed": sum(item["chunks_indexed"] for item in results),
    }


@app.get("/runs/recent")
async def runs_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_knowledge_runs(limit)
    return {"runs": rows, "count": len(rows)}


@app.get("/stats")
async def stats() -> dict[str, Any]:
    ch = await clickhouse.knowledge_stats()
    qd = await qdrant.count(settings.qdrant_knowledge_collection)
    return {**ch, "qdrant_knowledge_points": qd}


async def index_documents(source: str, source_type: str, docs: list[Any], config: dict[str, Any]) -> dict[str, Any]:
    run_id = "krun_" + uuid.uuid4().hex[:20]
    started = _now()
    started_timer = time.perf_counter()
    try:
        doc_rows = []
        chunk_rows = []
        point_count = 0
        known_doc_ids = await clickhouse.existing_knowledge_document_ids([doc.document_id for doc in docs])
        for doc in docs:
            chunks = chunk_document(doc, settings.chunk_size, settings.chunk_overlap)
            known_chunk_ids = await clickhouse.existing_knowledge_chunk_ids([chunk["chunk_id"] for chunk in chunks])
            if doc.document_id not in known_doc_ids:
                doc_rows.append(_document_row(doc, started))
            for chunk in chunks:
                payload = _payload(doc, chunk, started)
                vector = embed_text(chunk["chunk_text"], settings.embedding_dimensions)
                point_id = await qdrant.upsert_knowledge_chunk(chunk["chunk_id"], vector, payload)
                if chunk["chunk_id"] not in known_chunk_ids:
                    chunk_rows.append(_chunk_row(chunk, started, point_id))
                point_count += 1
        inserted_docs = await clickhouse.insert_knowledge_documents(doc_rows)
        await clickhouse.insert_knowledge_chunks(chunk_rows)
        result = {"status": "success", "run_id": run_id, "source": source, "documents_seen": len(docs), "documents_ingested": inserted_docs, "chunks_created": len(chunk_rows), "chunks_indexed": point_count, "latency_ms": round((time.perf_counter() - started_timer) * 1000, 2)}
        await clickhouse.insert_knowledge_ingestion_run(_run_row(run_id, started, source, source_type, result, config, ""))
        last_ingest.update(result, last_error="")
        return result
    except Exception as exc:
        logger.exception("Knowledge ingestion failed source=%s: %s", source, exc)
        result = {"status": "failed", "run_id": run_id, "source": source, "documents_seen": len(docs), "documents_ingested": 0, "chunks_created": 0, "chunks_indexed": 0, "latency_ms": round((time.perf_counter() - started_timer) * 1000, 2)}
        await clickhouse.insert_knowledge_ingestion_run(_run_row(run_id, started, source, source_type, result, config, str(exc)))
        last_ingest.update(result, last_error=str(exc))
        return result


def _document_row(doc: Any, now: str) -> dict[str, Any]:
    return {"document_id": doc.document_id, "source_type": doc.source_type, "document_type": doc.document_type, "title": doc.title, "source_path": doc.source_path, "source_uri": doc.source_uri, "content_hash": doc.content_hash, "ingested_at": now, "updated_at": now, "phase": doc.phase, "service": doc.service, "namespace": doc.namespace, "severity": doc.severity, "tags_json": json.dumps(doc.tags, sort_keys=True), "metadata_json": json.dumps(doc.metadata, sort_keys=True, default=str), "raw_content": doc.raw_content}


def _chunk_row(chunk: dict[str, Any], now: str, point_id: str) -> dict[str, Any]:
    return {"chunk_id": chunk["chunk_id"], "document_id": chunk["document_id"], "chunk_index": chunk["chunk_index"], "source_type": chunk["source_type"], "document_type": chunk["document_type"], "title": chunk["title"], "source_path": chunk["source_path"], "content_hash": chunk["content_hash"], "chunk_hash": chunk["chunk_hash"], "ingested_at": now, "phase": chunk["phase"], "service": chunk["service"], "namespace": chunk["namespace"], "severity": chunk["severity"], "tags_json": json.dumps(chunk["tags"], sort_keys=True), "metadata_json": json.dumps(chunk["metadata"], sort_keys=True, default=str), "chunk_text": chunk["chunk_text"], "embedding_model": "deterministic-hash-v1", "qdrant_collection": settings.qdrant_knowledge_collection, "qdrant_point_id": point_id}


def _payload(doc: Any, chunk: dict[str, Any], now: str) -> dict[str, Any]:
    compact = " ".join(chunk["chunk_text"].split())[:500]
    return {"chunk_id": chunk["chunk_id"], "document_id": doc.document_id, "source_type": doc.source_type, "document_type": doc.document_type, "title": doc.title, "source_path": doc.source_path, "source_uri": doc.source_uri, "phase": doc.phase, "service": doc.service, "namespace": doc.namespace, "severity": doc.severity, "tags": doc.tags, "content_hash": doc.content_hash, "chunk_hash": chunk["chunk_hash"], "ingested_at": now, "summary": compact[:220], "compact_text": compact}


def _run_row(run_id: str, started: str, source: str, source_type: str, result: dict[str, Any], config: dict[str, Any], error: str) -> dict[str, Any]:
    return {"run_id": run_id, "started_at": started, "completed_at": _now(), "source": source, "source_type": source_type, "documents_seen": result["documents_seen"], "documents_ingested": result["documents_ingested"], "chunks_created": result["chunks_created"], "chunks_indexed": result["chunks_indexed"], "status": result["status"], "config_json": json.dumps(config, sort_keys=True), "error_message": error}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


async def background_loop() -> None:
    while True:
        try:
            await ingest_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_ingest["last_error"] = str(exc)
            logger.exception("Background ingestion failed: %s", exc)
        await asyncio.sleep(settings.knowledge_ingest_interval_seconds)
