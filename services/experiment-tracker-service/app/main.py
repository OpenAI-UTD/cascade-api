from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from app.kafka_publisher import ExperimentPublisher
from app.models import ExperimentCreate, ExperimentRecord, HealthResponse, Settings
from app.store import ExperimentStore
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = Settings()

binding_guard(settings)
store = ExperimentStore()
publisher = ExperimentPublisher(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.start()
    try:
        yield
    finally:
        await publisher.stop()


app = FastAPI(title="Cascade Experiment Tracker Service", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="experiment-tracker-service")


@app.post("/experiments", response_model=ExperimentRecord, status_code=status.HTTP_201_CREATED)
async def create_experiment(payload: ExperimentCreate) -> ExperimentRecord:
    record = store.add(ExperimentRecord.from_create(payload))
    await publisher.publish(record)
    return record


@app.get("/experiments", response_model=list[ExperimentRecord])
async def list_experiments() -> list[ExperimentRecord]:
    return store.list()


@app.get("/experiments/{experiment_id}", response_model=ExperimentRecord)
async def get_experiment(experiment_id: str) -> ExperimentRecord:
    record = store.get(experiment_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found")
    return record


@app.post("/experiments/{experiment_id}/complete", response_model=ExperimentRecord)
async def complete_experiment(experiment_id: str) -> ExperimentRecord:
    record = store.complete(experiment_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experiment not found")
    await publisher.publish(record)
    return record
