from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.features.extraction import extract_feature_windows, synthetic_feature_window, telemetry_query
from services.shared.storage.clickhouse_client import ClickHouseClient
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    feature_window_seconds: int = 300
    feature_lookback_minutes: int = 30
    min_events_per_window: int = 1
    feature_extract_interval_seconds: int = 120
    feature_background_enabled: bool = False

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class ExtractRequest(BaseModel):
    lookback_minutes: int | None = Field(default=None, ge=1, le=1440)
    window_seconds: int | None = Field(default=None, ge=30, le=3600)
    service: str | None = None
    namespace: str | None = None


settings = Settings()

binding_guard(settings)
clickhouse = ClickHouseClient()
last_extract: dict[str, Any] = {"windows_created": 0, "services_seen": 0, "last_error": ""}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await clickhouse.initialize_schema()
    task = None
    if settings.feature_background_enabled:
        task = asyncio.create_task(background_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="Cascade Feature Extractor Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "feature-extractor-service", **last_extract}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    ok = await clickhouse.ping()
    return {"status": "ok" if ok else "degraded", "service": "feature-extractor-service", "clickhouse": ok}


@app.post("/extract")
async def extract(payload: ExtractRequest | None = None) -> dict[str, Any]:
    payload = payload or ExtractRequest()
    lookback = payload.lookback_minutes or settings.feature_lookback_minutes
    window_seconds = payload.window_seconds or settings.feature_window_seconds
    query = telemetry_query(clickhouse.settings.clickhouse_database, lookback, payload.service, payload.namespace)
    events = await clickhouse.fetch_json_rows(query)
    windows = [row for row in extract_feature_windows(events, window_seconds) if int(row["event_count"]) >= settings.min_events_per_window]
    inserted = await clickhouse.insert_rows("telemetry_feature_windows", windows)
    services_seen = len({row["service"] for row in windows})
    last_extract.update({"windows_created": inserted, "services_seen": services_seen, "last_error": ""})
    logger.info("Extracted feature windows count=%s services=%s", inserted, services_seen)
    return {
        "status": "ok",
        "windows_created": inserted,
        "windows_updated": 0,
        "services_seen": services_seen,
        "lookback_minutes": lookback,
        "window_seconds": window_seconds,
    }


@app.get("/features/recent")
async def features_recent(limit: int = 20, service: str | None = None) -> dict[str, Any]:
    rows = await clickhouse.recent_feature_windows(limit, service)
    return {"features": rows, "count": len(rows)}


@app.post("/features/synthetic")
async def synthetic_feature() -> dict[str, Any]:
    row = synthetic_feature_window()
    inserted = await clickhouse.insert_rows("telemetry_feature_windows", [row])
    return {"status": "ok", "inserted": inserted, "window": row}


async def background_loop() -> None:
    while True:
        try:
            await extract(ExtractRequest())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_extract["last_error"] = str(exc)
            logger.exception("Background feature extraction failed: %s", exc)
        await asyncio.sleep(settings.feature_extract_interval_seconds)
