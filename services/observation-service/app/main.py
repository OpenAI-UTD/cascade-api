import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status

from app.config import Settings, get_settings
from app.models import HealthResponse, RawMetricsResponse, SnapshotResponse, TelemetryPodSnapshot
from app.normalizer import build_snapshot_queries, normalize_snapshot
from app.prometheus_client import PrometheusClient
from services.shared.kafka.config import KafkaSettings
from services.shared.kafka.producer import KafkaProducer
from services.shared.security.auth import binding_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    publisher_task = None
    if settings.telemetry_publish_enabled:
        publisher_task = asyncio.create_task(_publish_telemetry_loop(settings))
        logger.info("Telemetry Kafka publisher enabled")
    else:
        logger.info("Telemetry Kafka publisher disabled")

    try:
        yield
    finally:
        if publisher_task is not None:
            publisher_task.cancel()
            try:
                await publisher_task
            except asyncio.CancelledError:
                logger.info("Telemetry publisher loop stopped")


app = FastAPI(title="Cascade Observation Service", version="0.2.0", lifespan=lifespan)

binding_guard()


def get_prometheus_client(settings: Settings = Depends(get_settings)) -> PrometheusClient:
    return PrometheusClient(settings.prometheus_url, settings.request_timeout_seconds)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="observation-service")


@app.get("/snapshot", response_model=SnapshotResponse)
async def snapshot(
    settings: Settings = Depends(get_settings),
    prometheus: PrometheusClient = Depends(get_prometheus_client),
) -> SnapshotResponse:
    return await collect_snapshot(settings, prometheus)


async def collect_snapshot(settings: Settings, prometheus: PrometheusClient) -> SnapshotResponse:
    queries = build_snapshot_queries(settings.target_namespace)
    query_results = await asyncio.gather(
        *(prometheus.query_with_status(metric_name, query) for metric_name, query in queries.items()),
        return_exceptions=True,
    )

    normalized_results: dict[str, dict[str, Any] | None] = {}
    query_status: dict[str, dict[str, Any]] = {}
    for metric_name, result in zip(queries.keys(), query_results, strict=True):
        if isinstance(result, Exception):
            logger.warning("Snapshot metric '%s' is unavailable: %s", metric_name, result)
            normalized_results[metric_name] = None
            query_status[metric_name] = {"metric": metric_name, "query": queries[metric_name], "status": "failed", "error": str(result)}
        else:
            normalized_results[metric_name] = result.payload
            query_status[metric_name] = result.status.model_dump(exclude_none=True)

    snapshot_response = normalize_snapshot(settings.target_namespace, normalized_results, query_status=query_status)
    system_health = await _collect_system_health(settings)
    _apply_system_health(snapshot_response, system_health)
    if snapshot_response.pods:
        return snapshot_response

    fallback = await _collect_kubernetes_snapshot(settings)
    _apply_system_health(fallback, system_health)
    if fallback.pods:
        fallback.query_status = query_status
        fallback.missing_metrics = sorted(set(fallback.missing_metrics) | set(snapshot_response.missing_metrics))
        fallback.collection_warnings = [
            *snapshot_response.collection_warnings,
            "Prometheus returned no pod samples; Kubernetes fallback supplied pod status only.",
        ]
        fallback.used_kubernetes_fallback = True
        for pod in fallback.pods:
            pod.metric_status = {name: str(status.get("status") or "unknown") for name, status in query_status.items()}
            pod.used_kubernetes_fallback = True
            pod.evidence_quality = "kubernetes_fallback"
        logger.warning(
            "Prometheus snapshot returned no pods; using Kubernetes API fallback namespace=%s count=%s",
            settings.target_namespace,
            len(fallback.pods),
        )
        return fallback
    return snapshot_response


@app.get("/metrics/raw", response_model=RawMetricsResponse)
async def raw_metrics(
    query: str = Query(..., min_length=1, max_length=2000),
    prometheus: PrometheusClient = Depends(get_prometheus_client),
) -> RawMetricsResponse:
    safe_query = _validate_promql(query)
    result = await prometheus.query_with_status("raw", safe_query)

    return RawMetricsResponse(query=safe_query, prometheus=result.payload, query_status=result.status.model_dump(exclude_none=True))


def _validate_promql(query: str) -> str:
    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Query must not be empty")

    # This endpoint only permits a single instant query expression.
    if re.search(r"[;\r\n]", normalized_query):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query must be a single PromQL expression",
        )

    return normalized_query


async def _collect_kubernetes_snapshot(settings: Settings) -> SnapshotResponse:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if not token_path.exists():
        return SnapshotResponse(namespace=settings.target_namespace, timestamp=datetime.now(UTC), pods=[])

    try:
        token = token_path.read_text(encoding="utf-8").strip()
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{settings.target_namespace}/pods"
        verify: str | bool = str(ca_path) if ca_path.exists() else True
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, verify=verify) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.warning("Kubernetes snapshot fallback unavailable: %s", exc)
        return SnapshotResponse(namespace=settings.target_namespace, timestamp=datetime.now(UTC), pods=[])

    pods: list[TelemetryPodSnapshot] = []
    for item in payload.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        name = metadata.get("name")
        namespace = metadata.get("namespace") or settings.target_namespace
        if not name:
            continue
        container_statuses = status.get("containerStatuses") or []
        restart_count = sum(int(container.get("restartCount") or 0) for container in container_statuses)
        ready = bool(container_statuses) and all(bool(container.get("ready")) for container in container_statuses)
        warning_count = await _pod_warning_event_count(settings, name)
        pods.append(
            TelemetryPodSnapshot(
                namespace=namespace,
                pod_name=name,
                service_name=_service_name_from_pod(metadata),
                labels={str(key): str(value) for key, value in (metadata.get("labels") or {}).items()},
                pod_phase=status.get("phase"),
                restart_count=float(restart_count),
                ready=ready,
                warning_event_count=float(warning_count),
                service_available=status.get("phase") == "Running" and ready,
                missing_metrics=["cpu", "memory", "request_rate", "error_rate", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms"],
                collection_warnings=["Prometheus snapshot unavailable; using Kubernetes pod status fallback."],
                evidence_quality="kubernetes_fallback",
                used_kubernetes_fallback=True,
            )
        )

    return SnapshotResponse(namespace=settings.target_namespace, timestamp=datetime.now(UTC), pods=pods)


def _service_name_from_pod(metadata: dict[str, Any]) -> str | None:
    labels = metadata.get("labels") or {}
    for key in ("app", "app.kubernetes.io/name", "name"):
        value = labels.get(key)
        if value:
            return str(value)
    name = str(metadata.get("name") or "")
    deployment_match = re.match(r"^(?P<name>.+)-[a-f0-9]{8,10}-[a-z0-9]{5}$", name)
    if deployment_match:
        return deployment_match.group("name")
    stateful_match = re.match(r"^(?P<name>.+)-\d+$", name)
    if stateful_match:
        return stateful_match.group("name")
    return name or None


async def _pod_warning_event_count(settings: Settings, pod_name: str) -> int:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if not token_path.exists():
        return 0
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        selector = f"involvedObject.name={pod_name},type=Warning"
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{settings.target_namespace}/events"
        verify: str | bool = str(ca_path) if ca_path.exists() else True
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, verify=verify) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params={"fieldSelector": selector})
            response.raise_for_status()
            payload = response.json()
        return len(payload.get("items") or [])
    except Exception:
        return 0


async def _collect_system_health(settings: Settings) -> dict[str, bool | None]:
    async def check(url: str) -> bool | None:
        if not url:
            return None
        try:
            async with httpx.AsyncClient(timeout=min(settings.request_timeout_seconds, 3.0)) as client:
                response = await client.get(url)
            return response.status_code < 500
        except Exception:
            return False

    redpanda, clickhouse, qdrant = await asyncio.gather(
        check(settings.redpanda_health_url),
        check(settings.clickhouse_health_url),
        check(settings.qdrant_health_url),
    )
    return {"redpanda_healthy": redpanda, "clickhouse_healthy": clickhouse, "qdrant_healthy": qdrant}


def _apply_system_health(snapshot: SnapshotResponse, health: dict[str, bool | None]) -> None:
    for pod in snapshot.pods:
        pod.redpanda_healthy = health.get("redpanda_healthy")
        pod.clickhouse_healthy = health.get("clickhouse_healthy")
        pod.qdrant_healthy = health.get("qdrant_healthy")
        if pod.service_available is None:
            pod.service_available = pod.pod_phase == "Running" and pod.ready is not False


async def _publish_telemetry_loop(settings: Settings) -> None:
    kafka_settings = KafkaSettings(
        kafka_bootstrap_servers=settings.kafka_bootstrap_servers,
        kafka_client_id="observation-service",
    )
    prometheus = PrometheusClient(settings.prometheus_url, settings.request_timeout_seconds)

    while True:
        producer = KafkaProducer(kafka_settings)
        try:
            await producer.start()
            while True:
                snapshot_response = await collect_snapshot(settings, prometheus)
                published_count = 0
                for pod in snapshot_response.pods:
                    event = _build_raw_telemetry_event(snapshot_response.timestamp, pod)
                    await producer.send(settings.kafka_raw_topic, event, key=pod.pod_name)
                    published_count += 1

                logger.info(
                    "Telemetry publisher emitted events topic=%s count=%s namespace=%s",
                    settings.kafka_raw_topic,
                    published_count,
                    settings.target_namespace,
                )

                await asyncio.sleep(settings.telemetry_publish_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Telemetry publisher loop failed and will retry: %s", exc)
            await asyncio.sleep(settings.telemetry_publish_interval_seconds)
        finally:
            await producer.stop()


def _build_raw_telemetry_event(timestamp: datetime, pod: TelemetryPodSnapshot) -> dict[str, Any]:
    event_timestamp = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    return {
        "event_id": str(uuid4()),
        "timestamp": event_timestamp.isoformat(),
        "namespace": pod.namespace,
        "pod_name": pod.pod_name,
        "service_name": pod.service_name,
        "labels": pod.labels,
        "cpu": pod.cpu_usage_cores,
        "memory": pod.memory_working_set_bytes,
        "restart_count": pod.restart_count,
        "pod_phase": pod.pod_phase,
        "ready": pod.ready,
        "request_rate": pod.request_rate,
        "error_rate": pod.error_rate,
        "latency_p50_ms": pod.latency_p50_ms,
        "latency_p95_ms": pod.latency_p95_ms,
        "latency_p99_ms": pod.latency_p99_ms,
        "warning_event_count": pod.warning_event_count,
        "service_available": pod.service_available,
        "redpanda_healthy": pod.redpanda_healthy,
        "clickhouse_healthy": pod.clickhouse_healthy,
        "qdrant_healthy": pod.qdrant_healthy,
        "missing_metrics": pod.missing_metrics,
        "collection_warnings": pod.collection_warnings,
        "metric_status": pod.metric_status,
        "evidence_quality": pod.evidence_quality,
        "used_kubernetes_fallback": pod.used_kubernetes_fallback,
        "source": "observation-service",
    }
