from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.anomaly.ensemble import anomaly_event_payload, combine_results
from services.shared.anomaly.isolation_forest import isolation_forest_detect, status as isolation_status
from services.shared.anomaly.thresholds import threshold_detect
from services.shared.anomaly.zscore import zscore_detect
from services.shared.events.mapping import stable_json
from services.shared.features.extraction import ch_datetime
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    kafka_bootstrap_servers: str = "redpanda.cascade-system.svc.cluster.local:9092"
    anomalies_topic: str = "anomalies.detected"
    anomaly_lookback_minutes: int = 30
    anomaly_publish_threshold: float = 0.4
    zscore_threshold: float = 3.0
    isolation_forest_min_windows: int = 20
    isolation_forest_contamination: str = "auto"
    anomaly_background_enabled: bool = False
    anomaly_detect_interval_seconds: int = 120
    event_count_threshold: int = 100

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class DetectRequest(BaseModel):
    lookback_minutes: int | None = Field(default=None, ge=1, le=1440)
    service: str | None = None
    namespace: str | None = None
    publish: bool = True


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
producer = KafkaProducer(KafkaSettings(kafka_bootstrap_servers=settings.kafka_bootstrap_servers, kafka_client_id="anomaly-detector-service"))
last_detection: dict[str, Any] = {"windows_scored": 0, "anomalies_detected": 0, "published_count": 0, "last_error": ""}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await clickhouse.initialize_schema()
    await producer.start()
    task = None
    if settings.anomaly_background_enabled:
        task = asyncio.create_task(background_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await producer.stop()


app = FastAPI(title="Cascade Anomaly Detector Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "anomaly-detector-service", **last_detection}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ok = await clickhouse.ping()
    return {"status": "ok" if ok else "degraded", "service": "anomaly-detector-service", "clickhouse": ok, "redpanda_configured": bool(settings.kafka_bootstrap_servers)}


@app.get("/models/status")
async def models_status() -> dict[str, Any]:
    return {
        "threshold": {"available": True, "detector_type": "rule_baseline"},
        "rolling_zscore": {"available": True, "detector_type": "statistical_baseline", "threshold": settings.zscore_threshold},
        "isolation_forest": isolation_status(settings.isolation_forest_min_windows),
        "config": {
            "publish_threshold": settings.anomaly_publish_threshold,
            "isolation_forest_contamination": settings.isolation_forest_contamination,
        },
    }


@app.post("/detect")
async def detect(payload: DetectRequest | None = None) -> dict[str, Any]:
    payload = payload or DetectRequest()
    started = datetime.now(UTC)
    lookback = payload.lookback_minutes or settings.anomaly_lookback_minutes
    where = [f"window_end >= now64(3) - INTERVAL {lookback} MINUTE"]
    if payload.service:
        where.append(f"service = '{payload.service.replace("'", "\\'")}'")
    if payload.namespace:
        where.append(f"namespace = '{payload.namespace.replace("'", "\\'")}'")
    rows = await clickhouse.fetch_json_rows(f"SELECT * FROM {clickhouse.settings.clickhouse_database}.telemetry_feature_windows WHERE {' AND '.join(where)} ORDER BY window_end ASC")
    anomalies = []
    published = 0
    for window in rows:
        results = [
            threshold_detect(window, event_count_threshold=settings.event_count_threshold),
            zscore_detect(window, rows, settings.zscore_threshold),
            isolation_forest_detect(window, rows, settings.isolation_forest_min_windows, _contamination()),
        ]
        anomaly = combine_results(window, results, settings.anomaly_publish_threshold)
        if anomaly["is_anomaly"]:
            if payload.publish and anomaly["risk_score"] >= settings.anomaly_publish_threshold:
                await producer.send(settings.anomalies_topic, anomaly_event_payload(anomaly), key=anomaly["service"])
                anomaly["published_to_redpanda"] = 1
                published += 1
            anomalies.append(anomaly)
    inserted = await clickhouse.insert_anomaly_events(anomalies)
    await record_model_run(started, len(rows), inserted, "ok", "")
    last_detection.update({"windows_scored": len(rows), "anomalies_detected": inserted, "published_count": published, "last_error": ""})
    return {
        "status": "ok",
        "windows_scored": len(rows),
        "anomalies_detected": inserted,
        "model_outputs": {"threshold": "available", "rolling_zscore": "available", "isolation_forest": (await models_status())["isolation_forest"]},
        "published_count": published,
    }


@app.get("/anomalies/recent")
async def anomalies_recent(limit: int = 20, service: str | None = None, severity: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_anomalies(limit, service, severity)
    return {"anomalies": rows, "count": len(rows)}


@app.post("/models/warmup")
async def models_warmup() -> dict[str, Any]:
    rows = await clickhouse.recent_feature_windows(500)
    return {"status": "ok", "feature_windows_loaded": len(rows), "models": await models_status()}


async def record_model_run(started: datetime, windows_scored: int, anomalies_detected: int, status: str, error_message: str) -> None:
    completed = datetime.now(UTC)
    row = {
        "run_id": str(uuid4()),
        "started_at": ch_datetime(started),
        "completed_at": ch_datetime(completed),
        "model_name": "cascade_baseline_ensemble",
        "model_version": "phase4.v1",
        "detector_type": "ensemble",
        "windows_scored": windows_scored,
        "anomalies_detected": anomalies_detected,
        "status": status,
        "config_json": stable_json({"zscore_threshold": settings.zscore_threshold, "publish_threshold": settings.anomaly_publish_threshold}),
        "error_message": error_message,
    }
    await clickhouse.insert_rows("model_runs", [row])


async def background_loop() -> None:
    while True:
        try:
            await detect(DetectRequest())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_detection["last_error"] = str(exc)
            logger.exception("Background anomaly detection failed: %s", exc)
        await asyncio.sleep(settings.anomaly_detect_interval_seconds)


def _contamination() -> str | float:
    try:
        return float(settings.isolation_forest_contamination)
    except ValueError:
        return settings.isolation_forest_contamination
