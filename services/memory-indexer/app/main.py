from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.embedding.deterministic import embed_text
from services.shared.events.mapping import build_memory_document, build_memory_payload
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.consumer import KafkaConsumer
from services.shared.storage.qdrant_client import QdrantClient, QdrantSettings
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    telemetry_enriched_topic: str = "telemetry.enriched"
    experiments_events_topic: str = "experiments.events"
    memory_consumer_group: str = "cascade-memory-indexer"
    embedding_dimensions: int = 128

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class IndexRequest(BaseModel):
    memory_type: str
    payload: dict[str, Any]
    source: str = "api"
    source_topic: str = ""


class HealthResponse(BaseModel):
    status: str
    service: str
    consumed_count: int
    indexed_count: int
    qdrant_ready: bool


@dataclass
class Counters:
    consumed: int = 0
    indexed: int = 0
    qdrant_ready: bool = False
    last_error: str = ""


settings = Settings()

binding_guard(settings)
qdrant = QdrantClient(QdrantSettings(embedding_dimensions=settings.embedding_dimensions))
counters = Counters()


async def consume_loop(topic: str, group_id: str, memory_type: str) -> None:
    kafka_settings = KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id=f"{group_id}-{memory_type}")
    while True:
        consumer = KafkaConsumer(topic=topic, group_id=f"{group_id}-{memory_type}", settings=kafka_settings)
        try:
            await qdrant.ensure_collection()
            counters.qdrant_ready = True
            await consumer.start()
            async for payload in consumer.messages():
                counters.consumed += 1
                await index_memory(memory_type, payload, "kafka", topic)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            counters.last_error = str(exc)
            counters.qdrant_ready = await qdrant.ping()
            logger.exception("Memory indexer loop failed topic=%s and will retry: %s", topic, exc)
            await asyncio.sleep(5)
        finally:
            await consumer.stop()


async def index_memory(memory_type: str, payload: dict[str, Any], source: str, source_topic: str = "") -> dict[str, Any]:
    await qdrant.ensure_collection()
    memory_payload = build_memory_payload(memory_type, payload, source, source_topic)
    document = build_memory_document(memory_type, payload)
    vector = embed_text(document, settings.embedding_dimensions)
    await qdrant.upsert_point(memory_payload["memory_id"], vector, memory_payload)
    counters.indexed += 1
    counters.qdrant_ready = True
    return memory_payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(consume_loop(settings.telemetry_enriched_topic, settings.memory_consumer_group, "telemetry_event")),
        asyncio.create_task(consume_loop(settings.experiments_events_topic, settings.memory_consumer_group, "experiment_event")),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Cascade Memory Indexer", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="memory-indexer", consumed_count=counters.consumed, indexed_count=counters.indexed, qdrant_ready=counters.qdrant_ready)


@app.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    counters.qdrant_ready = await qdrant.collection_ready()
    return HealthResponse(status="ok" if counters.qdrant_ready else "degraded", service="memory-indexer", consumed_count=counters.consumed, indexed_count=counters.indexed, qdrant_ready=counters.qdrant_ready)


@app.post("/memory/index")
async def memory_index(payload: IndexRequest) -> dict[str, Any]:
    memory_payload = await index_memory(payload.memory_type, payload.payload, payload.source, payload.source_topic)
    return {"status": "ok", "memory": memory_payload}

