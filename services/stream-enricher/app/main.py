from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.kafka_worker import StreamEnricherWorker
from app.models import HealthResponse, Settings
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

settings = Settings()

binding_guard(settings)
worker = StreamEnricherWorker(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info("Stream enricher worker stopped")


app = FastAPI(title="Cascade Stream Enricher", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="stream-enricher")


@app.get("/stats")
async def stats() -> dict[str, int]:
    return {
        "processed_count": worker.processed_count,
        "enriched_count": worker.enriched_count,
    }
