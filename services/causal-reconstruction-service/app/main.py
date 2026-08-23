from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from app.kafka_reader import EventBuffer, consume_topic
from app.models import HealthResponse, IncidentRecord, ReconstructRequest, Settings
from app.reconstructor import reconstruct
from services.shared.kafka.config import KafkaSettings
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = Settings()

binding_guard(settings)
buffer = EventBuffer()
incidents: dict[str, IncidentRecord] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    kafka_settings = KafkaSettings(
        kafka_bootstrap_servers=settings.kafka_bootstrap_servers,
        kafka_client_id="causal-reconstruction-service",
    )
    tasks = [
        asyncio.create_task(consume_topic(settings.experiment_topic, f"{settings.kafka_consumer_group}-experiments", kafka_settings, buffer.add_experiment)),
        asyncio.create_task(consume_topic(settings.enriched_topic, f"{settings.kafka_consumer_group}-telemetry", kafka_settings, buffer.add_telemetry)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Cascade Causal Reconstruction Service", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="causal-reconstruction-service")


@app.post("/reconstruct", response_model=IncidentRecord)
async def reconstruct_incident(payload: ReconstructRequest) -> IncidentRecord:
    incident = reconstruct(payload, buffer)
    incidents[incident.incident_id] = incident
    return incident


@app.get("/incidents", response_model=list[IncidentRecord])
async def list_incidents() -> list[IncidentRecord]:
    return list(incidents.values())


@app.get("/incidents/{incident_id}", response_model=IncidentRecord)
async def get_incident(incident_id: str) -> IncidentRecord:
    incident = incidents.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return incident
