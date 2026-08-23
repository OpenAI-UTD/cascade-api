from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.embedding.deterministic import embed_text
from services.shared.knowledge.context import assemble_context_pack
from services.shared.knowledge.search import KnowledgeSearchRequest
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.storage.qdrant_client import QdrantClient, QdrantSettings
from services.shared.security.auth import binding_guard


class Settings(BaseSettings):
    qdrant_knowledge_collection: str = "cascade_knowledge_base"
    embedding_dimensions: int = 128

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
qdrant = QdrantClient(QdrantSettings(embedding_dimensions=settings.embedding_dimensions, qdrant_knowledge_collection=settings.qdrant_knowledge_collection))


@asynccontextmanager
async def lifespan(_: FastAPI):
    await clickhouse.initialize_schema()
    await qdrant.ensure_knowledge_collection()
    yield


app = FastAPI(title="Cascade Knowledge Retrieval Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "knowledge-retrieval-service"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ch = await clickhouse.ping()
    qd = await qdrant.ping()
    collection = await qdrant.knowledge_collection_ready()
    response = {"status": "ok" if ch and qd and collection else "degraded", "service": "knowledge-retrieval-service", "clickhouse": ch, "qdrant": qd, "collection": collection}
    if response["status"] != "ok":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=response)
    return response


@app.post("/knowledge/search")
async def knowledge_search(payload: KnowledgeSearchRequest) -> dict[str, Any]:
    started = time.perf_counter()
    query_id = "kq_" + uuid.uuid4().hex[:20]
    vector = embed_text(payload.query, settings.embedding_dimensions)
    filter_dict = payload.filters.model_dump()
    raw_results = await qdrant.search_knowledge(vector, payload.limit, filter_dict)
    results = await enrich_results(raw_results)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    response = {"query_id": query_id, "query": payload.query, "results": results, "count": len(results), "latency_ms": latency_ms}
    await record_query(query_id, payload.query, filter_dict, results, latency_ms, response)
    return response


@app.post("/knowledge/context")
async def knowledge_context(payload: KnowledgeSearchRequest) -> dict[str, Any]:
    search = await knowledge_search(payload)
    return assemble_context_pack(payload.query, search["results"])


@app.get("/knowledge/documents/recent")
async def documents_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_knowledge_documents(limit)
    return {"documents": [_decode(row) for row in rows], "count": len(rows)}


@app.get("/knowledge/chunks/recent")
async def chunks_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_knowledge_chunks(limit)
    return {"chunks": [_decode(row) for row in rows], "count": len(rows)}


@app.get("/knowledge/stats")
async def stats() -> dict[str, Any]:
    ch = await clickhouse.knowledge_stats()
    return {**ch, "qdrant_knowledge_points": await qdrant.count(settings.qdrant_knowledge_collection)}


@app.get("/knowledge/runs/recent")
async def runs_recent(limit: int = 20) -> dict[str, Any]:
    rows = await clickhouse.recent_knowledge_runs(limit)
    return {"runs": rows, "count": len(rows)}


async def enrich_results(raw_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids = [item.get("payload", {}).get("chunk_id", "") for item in raw_results if item.get("payload", {}).get("chunk_id")]
    rows = {row["chunk_id"]: _decode(row) for row in await clickhouse.knowledge_chunks_by_ids(chunk_ids)}
    results = []
    for item in raw_results:
        payload = item.get("payload", {})
        chunk_id = payload.get("chunk_id", "")
        row = rows.get(chunk_id, {})
        metadata = row.get("metadata", {}) if row else {}
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
            "metadata": metadata,
        })
    return results


async def record_query(query_id: str, query: str, filters: dict[str, Any], results: list[dict[str, Any]], latency_ms: float, response: dict[str, Any]) -> None:
    top_score = float(results[0].get("score", 0.0)) if results else 0.0
    await clickhouse.insert_knowledge_query({
        "query_id": query_id,
        "queried_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "query_text": query,
        "filters_json": json.dumps(filters, sort_keys=True),
        "result_count": len(results),
        "top_score": top_score,
        "latency_ms": latency_ms,
        "response_json": json.dumps(response, sort_keys=True, default=str),
    })


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for source, target in [("tags_json", "tags"), ("metadata_json", "metadata")]:
        value = decoded.pop(source, None)
        if value is not None:
            try:
                decoded[target] = json.loads(value)
            except Exception:
                decoded[target] = value
    return decoded
