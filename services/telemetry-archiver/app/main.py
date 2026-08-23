from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.events.mapping import map_experiment_event, map_telemetry_event
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.consumer import KafkaConsumer
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    telemetry_enriched_topic: str = "telemetry.enriched"
    experiments_events_topic: str = "experiments.events"
    telemetry_consumer_group: str = "cascade-telemetry-archiver"
    experiment_consumer_group: str = "cascade-experiment-archiver"

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class HealthResponse(BaseModel):
    status: str
    service: str
    consumed_count: int
    inserted_count: int
    clickhouse_ready: bool


@dataclass
class Counters:
    consumed: int = 0
    inserted: int = 0
    last_error: str = ""
    clickhouse_ready: bool = False


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
counters = Counters()


async def consume_loop(topic: str, group_id: str, table: str, mapper) -> None:
    kafka_settings = KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id=f"{group_id}-client")
    while True:
        consumer = KafkaConsumer(topic=topic, group_id=group_id, settings=kafka_settings)
        try:
            await clickhouse.initialize_schema()
            counters.clickhouse_ready = True
            await consumer.start()
            async for payload in consumer.messages():
                await process_payload(topic, table, mapper, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            counters.last_error = str(exc)
            counters.clickhouse_ready = await clickhouse.ping()
            logger.exception("Archiver loop failed topic=%s and will retry: %s", topic, exc)
            await asyncio.sleep(5)
        finally:
            await consumer.stop()


async def process_payload(topic: str, table: str, mapper, payload: dict[str, Any]) -> None:
    counters.consumed += 1
    row = mapper(payload, topic)
    inserted = await clickhouse.insert_rows(table, [row])
    counters.inserted += inserted
    counters.clickhouse_ready = True
    logger.info("Archived event topic=%s table=%s consumed=%s inserted=%s", topic, table, counters.consumed, counters.inserted)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(consume_loop(settings.telemetry_enriched_topic, settings.telemetry_consumer_group, "telemetry_events", map_telemetry_event)),
        asyncio.create_task(consume_loop(settings.experiments_events_topic, settings.experiment_consumer_group, "experiment_events", map_experiment_event)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Cascade Telemetry Archiver", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="telemetry-archiver",
        consumed_count=counters.consumed,
        inserted_count=counters.inserted,
        clickhouse_ready=counters.clickhouse_ready,
    )


@app.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    counters.clickhouse_ready = await clickhouse.ping()
    status = "ok" if counters.clickhouse_ready else "degraded"
    return HealthResponse(
        status=status,
        service="telemetry-archiver",
        consumed_count=counters.consumed,
        inserted_count=counters.inserted,
        clickhouse_ready=counters.clickhouse_ready,
    )

