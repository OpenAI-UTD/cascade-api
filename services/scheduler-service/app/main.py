from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.audit import build_audit_event, emit_audit_event
from services.shared.security.auth import AuthSettings, auth_status, binding_guard, require_auth
from services.shared.scheduler import (
    ScheduledItem,
    SchedulerDecision,
    compute_next_run_at,
    evaluate_due,
    format_time,
    next_item_after_decision,
    parse_time,
    utc_now,
)
from services.shared.storage.clickhouse_client import ClickHouseClient

logger = logging.getLogger("scheduler-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class Settings(BaseSettings):
    service_name: str = "scheduler-service"
    scheduler_enabled: bool = True
    scheduler_poll_interval_seconds: int = 30
    scheduler_mode: str = "dry-run scheduler"
    chaos_planner_service_url: str = "http://chaos-planner-service.cascade-system.svc.cluster.local:8019"
    autopilot_service_url: str = "http://autopilot-service.cascade-system.svc.cluster.local:8024"
    request_timeout_seconds: float = 30.0
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


class UpsertScheduleRequest(BaseModel):
    item_id: str | None = None
    item_type: str
    target_id: str = ""
    name: str = ""
    schedule: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    paused: bool = False
    mode: str = "dry_run_scheduler"
    cooldown_seconds: int = 0
    max_runs_per_window: int = 1
    window_seconds: int = 3600
    payload: dict[str, Any] = Field(default_factory=dict)


class ControlRequest(BaseModel):
    reason: str = ""


settings = Settings()

binding_guard(settings)
store = ClickHouseClient()
_worker_task: asyncio.Task[None] | None = None
_tick_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _worker_task
    await store.initialize_schema()
    if settings.scheduler_enabled:
        _worker_task = asyncio.create_task(_poll_forever())
    try:
        yield
    finally:
        if _worker_task:
            _worker_task.cancel()
            try:
                await _worker_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Cascade Scheduler Service", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": settings.service_name,
        "mode": "dry-run scheduler" if settings.scheduler_mode not in {"local-demo scheduler", "disabled"} else settings.scheduler_mode,
        "scheduler_enabled": settings.scheduler_enabled,
        **auth_status(_auth_settings()),
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    checks = {"clickhouse": await store.ping()}
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        checks["chaos_planner"] = await _check(client, settings.chaos_planner_service_url)
        checks["autopilot"] = await _check(client, settings.autopilot_service_url)
    return {"status": "ok" if checks["clickhouse"] else "degraded", "service": settings.service_name, "checks": checks}


@app.get("/scheduler/status")
async def scheduler_status() -> dict[str, Any]:
    items = await _load_items()
    history = await _history(limit=20)
    due = [item for item in items if evaluate_due(item, now=datetime.now(UTC)).due]
    return {
        "service": settings.service_name,
        "enabled": settings.scheduler_enabled,
        "mode": "dry-run scheduler",
        "poll_interval_seconds": settings.scheduler_poll_interval_seconds,
        "items": len(items),
        "due": len(due),
        "last_decision": history[0].model_dump() if history else None,
    }


@app.get("/scheduler/items")
async def list_scheduled_items() -> dict[str, Any]:
    items = await _load_items()
    return {"items": [item.model_dump() for item in items], "count": len(items)}


@app.post("/scheduler/items")
async def upsert_scheduled_item(payload: UpsertScheduleRequest, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="upsert scheduler item")
    if payload.item_type not in {"chaos_campaign", "autopilot_run"}:
        raise HTTPException(status_code=400, detail="item_type must be chaos_campaign or autopilot_run")
    item_id = payload.item_id or f"{payload.item_type}:{payload.target_id or payload.name or 'default'}"
    next_run_at = compute_next_run_at(payload.schedule)
    item = ScheduledItem(
        item_id=item_id,
        item_type=payload.item_type,  # type: ignore[arg-type]
        target_id=payload.target_id,
        name=payload.name or item_id,
        schedule=payload.schedule,
        enabled=payload.enabled,
        paused=payload.paused,
        mode="dry_run_scheduler" if payload.mode != "disabled" else "disabled",
        next_run_at=next_run_at,
        cooldown_seconds=max(0, payload.cooldown_seconds),
        max_runs_per_window=max(1, payload.max_runs_per_window),
        window_seconds=max(60, payload.window_seconds),
        payload=payload.payload,
    )
    await _save_item(item)
    await _record_audit("schedule.updated", item, "scheduled item updated", status="updated")
    return {"item": item.model_dump()}


@app.post("/scheduler/items/{item_id}/enable")
async def enable_item(item_id: str, request: Request, payload: ControlRequest | None = None) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="enable scheduler item")
    return await _set_state(item_id, enabled=True, reason=payload.reason if payload else "")


@app.post("/scheduler/items/{item_id}/disable")
async def disable_item(item_id: str, request: Request, payload: ControlRequest | None = None) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="disable scheduler item")
    return await _set_state(item_id, enabled=False, reason=payload.reason if payload else "")


@app.post("/scheduler/items/{item_id}/pause")
async def pause_item(item_id: str, request: Request, payload: ControlRequest | None = None) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="pause scheduler item")
    return await _set_state(item_id, paused=True, reason=payload.reason if payload else "")


@app.post("/scheduler/items/{item_id}/resume")
async def resume_item(item_id: str, request: Request, payload: ControlRequest | None = None) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="resume scheduler item")
    return await _set_state(item_id, paused=False, reason=payload.reason if payload else "")


@app.post("/scheduler/items/{item_id}/run")
async def force_run_item(item_id: str, request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="force scheduler item")
    item = await _get_item(item_id)
    decision = await _run_item(item, idempotency_key=f"{item.item_id}:force:{utc_now()}", forced=True)
    return {"decision": decision.model_dump()}


@app.post("/scheduler/tick")
async def tick(request: Request) -> dict[str, Any]:
    require_auth(request, _auth_settings(), action="run scheduler tick")
    decisions = await _tick_once()
    return {"decisions": [decision.model_dump() for decision in decisions], "count": len(decisions)}


@app.get("/scheduler/history")
async def scheduler_history(limit: int = Query(100, ge=1, le=500), item_id: str | None = None) -> dict[str, Any]:
    rows = await store.scheduler_decisions(limit=limit, item_id=item_id)
    decisions = [_decode_decision(row) for row in rows]
    return {"decisions": [decision.model_dump() for decision in decisions], "count": len(decisions)}


async def _poll_forever() -> None:
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(max(5, settings.scheduler_poll_interval_seconds))


async def _tick_once() -> list[SchedulerDecision]:
    if not _tick_lock.locked():
        async with _tick_lock:
            return await _evaluate_items()
    return []


async def _evaluate_items() -> list[SchedulerDecision]:
    decisions: list[SchedulerDecision] = []
    now = datetime.now(UTC)
    for item in await _load_items():
        recent = await _recent_run_count(item, now)
        idempotency_key = _pending_key(item, now)
        seen = bool(idempotency_key and await store.scheduler_decisions(limit=1, idempotency_key=idempotency_key))
        evaluation = evaluate_due(item, now=now, recent_run_count=recent, idempotency_seen=seen)
        if not evaluation.due:
            decision = SchedulerDecision(
                item_id=item.item_id,
                item_type=item.item_type,
                target_id=item.target_id,
                due_at=item.next_run_at,
                status=evaluation.status,
                action="evaluate",
                reason=evaluation.reason,
                idempotency_key=evaluation.idempotency_key or idempotency_key,
                dry_run=True,
            )
            await _save_decision(decision)
            decisions.append(decision)
            continue
        decision = await _run_item(item, idempotency_key=evaluation.idempotency_key, skipped_missed_windows=evaluation.skipped_missed_windows)
        await _save_item(next_item_after_decision(item, evaluation, success=decision.status == "run_started"))
        decisions.append(decision)
    return decisions


async def _run_item(item: ScheduledItem, *, idempotency_key: str, skipped_missed_windows: int = 0, forced: bool = False) -> SchedulerDecision:
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            if item.item_type == "chaos_campaign":
                target_id = item.target_id or item.item_id.split(":", 1)[-1]
                response = await client.post(
                    f"{settings.chaos_planner_service_url.rstrip('/')}/campaigns/{target_id}/start",
                    json={"dry_run": True, "requested_by": "scheduler-service", "approved": False},
                )
            else:
                payload = {
                    "trigger_type": "scheduled",
                    "trigger_id": item.target_id or item.item_id,
                    "service": item.payload.get("service", ""),
                    "namespace": item.payload.get("namespace", "cascade-targets"),
                    "objective": item.payload.get("objective", "Scheduled Autopilot dry-run"),
                    "mode": item.payload.get("mode", "dry_run"),
                    "preferred_action_type": item.payload.get("preferred_action_type", "investigate_only"),
                }
                if payload["mode"] not in {"dry_run", "read_only"}:
                    payload["mode"] = "dry_run"
                response = await client.post(f"{settings.autopilot_service_url.rstrip('/')}/runs", json=payload)
        data = _safe_json(response)
        if response.status_code in {401, 403, 409, 422}:
            decision = SchedulerDecision(
                item_id=item.item_id,
                item_type=item.item_type,
                target_id=item.target_id,
                due_at=item.next_run_at,
                status="blocked_by_policy",
                action="force_run" if forced else "start_due_run",
                reason=data.get("detail") if isinstance(data.get("detail"), str) else "blocked_by_policy",
                idempotency_key=idempotency_key,
                dry_run=True,
                skipped_missed_windows=skipped_missed_windows,
                payload=data,
            )
        elif response.status_code >= 400:
            decision = SchedulerDecision(
                item_id=item.item_id,
                item_type=item.item_type,
                target_id=item.target_id,
                due_at=item.next_run_at,
                status="failed",
                action="force_run" if forced else "start_due_run",
                reason=f"HTTP {response.status_code}",
                idempotency_key=idempotency_key,
                dry_run=True,
                skipped_missed_windows=skipped_missed_windows,
                payload=data,
            )
        else:
            run_id = _extract_run_id(data)
            decision = SchedulerDecision(
                item_id=item.item_id,
                item_type=item.item_type,
                target_id=item.target_id,
                due_at=item.next_run_at,
                status="run_started",
                action="force_run" if forced else "start_due_run",
                reason="dry_run_started",
                idempotency_key=idempotency_key,
                run_id=run_id,
                dry_run=True,
                skipped_missed_windows=skipped_missed_windows,
                payload=data,
            )
    except Exception as exc:
        decision = SchedulerDecision(
            item_id=item.item_id,
            item_type=item.item_type,
            target_id=item.target_id,
            due_at=item.next_run_at,
            status="failed",
            action="force_run" if forced else "start_due_run",
            reason="scheduler_attempt_failed",
            idempotency_key=idempotency_key,
            dry_run=True,
            skipped_missed_windows=skipped_missed_windows,
            error_message=exc.__class__.__name__,
        )
    await _save_decision(decision)
    await _record_audit(f"schedule.{decision.status}", item, decision.reason, status=decision.status, decision=decision)
    return decision


async def _load_items() -> list[ScheduledItem]:
    persisted = {_decode_item(row).item_id: _decode_item(row) for row in await store.scheduler_items(limit=200)}
    for item in await _campaign_schedule_items():
        current = persisted.get(item.item_id)
        if current:
            current.status = item.status
            current.paused = current.paused or item.paused
            persisted[item.item_id] = current
        else:
            persisted[item.item_id] = item
            await _save_item(item)
    return sorted(persisted.values(), key=lambda item: item.next_run_at or "9999")


async def _campaign_schedule_items() -> list[ScheduledItem]:
    items: list[ScheduledItem] = []
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(f"{settings.chaos_planner_service_url.rstrip('/')}/campaigns", params={"limit": 100})
        if response.status_code >= 400:
            return items
        for campaign in response.json().get("campaigns", []):
            schedule = campaign.get("schedule") or {}
            if not compute_next_run_at(schedule):
                continue
            status = str(campaign.get("status") or "active").lower()
            next_run_at = campaign.get("next_run_at") or compute_next_run_at(schedule)
            items.append(
                ScheduledItem(
                    item_id=f"chaos_campaign:{campaign.get('campaign_id')}",
                    item_type="chaos_campaign",
                    target_id=str(campaign.get("campaign_id") or ""),
                    name=str(campaign.get("name") or campaign.get("campaign_id") or "chaos campaign"),
                    schedule=schedule,
                    enabled=status != "stopped",
                    paused=status == "paused",
                    next_run_at=next_run_at,
                    cooldown_seconds=int(campaign.get("cooldown_seconds") or 0),
                    payload={"campaign": campaign},
                    status=status,
                )
            )
    except Exception:
        logger.exception("failed to load chaos campaigns")
    return items


async def _get_item(item_id: str) -> ScheduledItem:
    row = await store.scheduler_item(item_id)
    if not row:
        await _load_items()
        row = await store.scheduler_item(item_id)
    if not row:
        raise HTTPException(status_code=404, detail="scheduled item not found")
    return _decode_item(row)


async def _set_state(item_id: str, *, enabled: bool | None = None, paused: bool | None = None, reason: str = "") -> dict[str, Any]:
    item = await _get_item(item_id)
    if enabled is not None:
        item.enabled = enabled
        item.mode = "dry_run_scheduler" if enabled else "disabled"
    if paused is not None:
        item.paused = paused
    item.updated_at = utc_now()
    await _save_item(item)
    await _record_audit("schedule.control", item, reason or "scheduler state updated", status="updated")
    return {"item": item.model_dump()}


async def _recent_run_count(item: ScheduledItem, now: datetime) -> int:
    since = format_time(now - timedelta(seconds=max(60, item.window_seconds)))
    return await store.scheduler_run_count_since(item.item_id, since)


def _pending_key(item: ScheduledItem, now: datetime) -> str:
    due_at = parse_time(item.next_run_at) or now
    return f"{item.item_id}:{format_time(due_at)}"


async def _history(limit: int) -> list[SchedulerDecision]:
    return [_decode_decision(row) for row in await store.scheduler_decisions(limit=limit)]


async def _save_item(item: ScheduledItem) -> None:
    await store.insert_scheduler_item(
        {
            "item_id": item.item_id,
            "item_type": item.item_type,
            "target_id": item.target_id,
            "name": item.name,
            "schedule_json": json.dumps(item.schedule, sort_keys=True),
            "enabled": 1 if item.enabled else 0,
            "paused": 1 if item.paused else 0,
            "mode": item.mode,
            "next_run_at": item.next_run_at,
            "last_run_at": item.last_run_at,
            "cooldown_seconds": item.cooldown_seconds,
            "max_runs_per_window": item.max_runs_per_window,
            "window_seconds": item.window_seconds,
            "failure_count": item.failure_count,
            "status": item.status,
            "payload_json": json.dumps(item.payload, sort_keys=True, default=str),
            "updated_at": item.updated_at,
        }
    )


async def _save_decision(decision: SchedulerDecision) -> None:
    await store.insert_scheduler_decision(
        {
            "decision_id": decision.decision_id,
            "item_id": decision.item_id,
            "item_type": decision.item_type,
            "target_id": decision.target_id,
            "evaluated_at": decision.evaluated_at,
            "due_at": decision.due_at,
            "status": decision.status,
            "action": decision.action,
            "reason": decision.reason,
            "idempotency_key": decision.idempotency_key,
            "run_id": decision.run_id,
            "dry_run": 1 if decision.dry_run else 0,
            "skipped_missed_windows": decision.skipped_missed_windows,
            "payload_json": json.dumps(decision.payload, sort_keys=True, default=str),
            "error_message": decision.error_message,
        }
    )


def _decode_item(row: dict[str, Any]) -> ScheduledItem:
    return ScheduledItem(
        item_id=str(row.get("item_id") or ""),
        item_type=str(row.get("item_type") or "chaos_campaign"),  # type: ignore[arg-type]
        target_id=str(row.get("target_id") or ""),
        name=str(row.get("name") or ""),
        schedule=_json(row.get("schedule_json"), {}),
        enabled=bool(row.get("enabled")),
        paused=bool(row.get("paused")),
        mode=str(row.get("mode") or "dry_run_scheduler"),  # type: ignore[arg-type]
        next_run_at=row.get("next_run_at"),
        last_run_at=row.get("last_run_at"),
        cooldown_seconds=int(row.get("cooldown_seconds") or 0),
        max_runs_per_window=int(row.get("max_runs_per_window") or 1),
        window_seconds=int(row.get("window_seconds") or 3600),
        failure_count=int(row.get("failure_count") or 0),
        status=str(row.get("status") or "active"),
        payload=_json(row.get("payload_json"), {}),
        updated_at=str(row.get("updated_at") or utc_now()),
    )


def _decode_decision(row: dict[str, Any]) -> SchedulerDecision:
    return SchedulerDecision(
        decision_id=str(row.get("decision_id") or ""),
        item_id=str(row.get("item_id") or ""),
        item_type=str(row.get("item_type") or "chaos_campaign"),  # type: ignore[arg-type]
        target_id=str(row.get("target_id") or ""),
        evaluated_at=str(row.get("evaluated_at") or utc_now()),
        due_at=row.get("due_at"),
        status=str(row.get("status") or "skipped"),  # type: ignore[arg-type]
        action=str(row.get("action") or ""),
        reason=str(row.get("reason") or ""),
        idempotency_key=str(row.get("idempotency_key") or ""),
        run_id=str(row.get("run_id") or ""),
        dry_run=bool(row.get("dry_run", 1)),
        skipped_missed_windows=int(row.get("skipped_missed_windows") or 0),
        payload=_json(row.get("payload_json"), {}),
        error_message=str(row.get("error_message") or ""),
    )


async def _record_audit(event_type: str, item: ScheduledItem, message: str, *, status: str, decision: SchedulerDecision | None = None) -> None:
    event = build_audit_event(
        event_type,
        "system/deployment",
        payload={"item": item.model_dump(), "decision": decision.model_dump() if decision else None},
        actor="scheduler-service",
        action=event_type,
        status=status,
        campaign_id=item.target_id if item.item_type == "chaos_campaign" else "",
        autopilot_run_id=decision.run_id if decision and item.item_type == "autopilot_run" else "",
        run_id=decision.run_id if decision else "",
        evidence_summary=message,
    )
    await emit_audit_event(store, event)


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return fallback


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {"payload": parsed}
    except Exception:
        return {"text": response.text[:500]}


def _extract_run_id(payload: dict[str, Any]) -> str:
    run = payload.get("run") if isinstance(payload.get("run"), dict) else payload
    return str(run.get("run_id") or "")


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )


async def _check(client: httpx.AsyncClient, base_url: str) -> bool:
    try:
        response = await client.get(base_url.rstrip("/") + "/health")
        return response.status_code < 500
    except Exception:
        return False
