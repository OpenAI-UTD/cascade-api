from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.shared.contracts import EVENT_TOPIC_CONTRACTS, contract_schema_bundle
from services.shared.security.auth import AuthSettings, auth_status, require_auth

logger = logging.getLogger("command-center-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class Settings(BaseSettings):
    retrieval_service_url: str = "http://retrieval-service.cascade-system.svc.cluster.local:8012"
    knowledge_retrieval_service_url: str = "http://knowledge-retrieval-service.cascade-system.svc.cluster.local:8016"
    topology_service_url: str = "http://topology-service.cascade-system.svc.cluster.local:8004"
    causal_reconstruction_service_url: str = "http://causal-reconstruction-service.cascade-system.svc.cluster.local:8005"
    incident_timeline_service_url: str = "http://incident-timeline-service.cascade-system.svc.cluster.local:8006"
    agent_tool_gateway_url: str = "http://agent-tool-gateway.cascade-system.svc.cluster.local:8017"
    agent_orchestrator_service_url: str = "http://agent-orchestrator-service.cascade-system.svc.cluster.local:8018"
    chaos_planner_service_url: str = "http://chaos-planner-service.cascade-system.svc.cluster.local:8019"
    chaos_executor_service_url: str = "http://chaos-executor-service.cascade-system.svc.cluster.local:8020"
    remediation_recommender_service_url: str = "http://remediation-recommender-service.cascade-system.svc.cluster.local:8021"
    approval_service_url: str = "http://approval-service.cascade-system.svc.cluster.local:8022"
    remediation_executor_service_url: str = "http://remediation-executor-service.cascade-system.svc.cluster.local:8023"
    autopilot_service_url: str = "http://autopilot-service.cascade-system.svc.cluster.local:8024"
    scheduler_service_url: str = "http://scheduler-service.cascade-system.svc.cluster.local:8025"
    anomaly_detector_service_url: str = "http://anomaly-detector-service.cascade-system.svc.cluster.local:8014"
    feature_extractor_service_url: str = "http://feature-extractor-service.cascade-system.svc.cluster.local:8013"
    project_qa_service_url: str = "http://project-qa-service.cascade-system.svc.cluster.local:8040"
    proxy_timeout_seconds: float = 10.0
    enable_dangerous_actions: bool = False
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 120
    rate_limit_burst: int = 60
    cascade_auth_enabled: bool = False
    cascade_local_demo_auth_bypass: bool = False
    cascade_api_keys: str = ""
    cascade_api_key_hashes: str = ""
    cascade_auth_header: str = "Authorization"

    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)


settings = Settings()
app = FastAPI(title="Cascade Command Center API", version="0.1.0")


@dataclass
class InMemoryRateLimiter:
    requests_per_minute: int
    burst: int
    buckets: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        cutoff = current - 60.0
        bucket = [ts for ts in self.buckets.get(key, []) if ts > cutoff]
        limit = max(1, self.requests_per_minute + max(0, self.burst))
        remaining = max(0, limit - len(bucket))
        if len(bucket) >= limit:
            self.buckets[key] = bucket
            return False, 0
        bucket.append(current)
        self.buckets[key] = bucket
        return True, max(0, remaining - 1)

    def reset(self) -> None:
        self.buckets.clear()


rate_limiter = InMemoryRateLimiter(
    requests_per_minute=settings.rate_limit_requests_per_minute,
    burst=settings.rate_limit_burst,
)

ROUTES: dict[str, str] = {
    "retrieval": settings.retrieval_service_url,
    "knowledge": settings.knowledge_retrieval_service_url,
    "topology": settings.topology_service_url,
    "causality": settings.retrieval_service_url,
    "causal-reconstruction": settings.causal_reconstruction_service_url,
    "target": settings.topology_service_url,
    "timeline": settings.incident_timeline_service_url,
    "agent": settings.agent_orchestrator_service_url,
    "tools": settings.agent_tool_gateway_url,
    "chaos/planner": settings.chaos_planner_service_url,
    "chaos/executor": settings.chaos_executor_service_url,
    "remediation/recommender": settings.remediation_recommender_service_url,
    "remediation/approval": settings.approval_service_url,
    "remediation/executor": settings.remediation_executor_service_url,
    "autopilot": settings.autopilot_service_url,
    "scheduler": settings.scheduler_service_url,
    "anomaly": settings.anomaly_detector_service_url,
    "features": settings.feature_extractor_service_url,
    "qa": settings.project_qa_service_url,
}

ALLOWED_METHODS = {"GET", "POST"}
SAFE_POST_PATHS = {
    ("knowledge", "knowledge/search"),
    ("knowledge", "knowledge/context"),
    ("topology", "topology/impact"),
    ("topology", "topology/blast-radius"),
    ("topology", "topology/critical-paths"),
    ("topology", "topology/refresh"),
    ("topology", "topology/traffic-inference"),
    ("causality", "causality/analyze"),
    ("causality", "rca/analyze"),
    ("causal-reconstruction", "reconstruct"),
    ("timeline", "timeline"),
    ("timeline", "report"),
    ("agent", "investigations"),
    ("chaos/planner", "plans"),
    ("chaos/planner", "campaigns"),
    ("chaos/executor", "runs"),
    ("remediation/recommender", "plans"),
    ("remediation/recommender", "policy/evaluate"),
    ("remediation/approval", "approvals"),
    ("remediation/executor", "safety/evaluate"),
    ("remediation/executor", "executions/dry-run"),
    ("remediation/executor", "executions"),
    ("autopilot", "runs"),
    ("scheduler", "scheduler/tick"),
    ("scheduler", "scheduler/items"),
    ("qa", "evaluations"),
    ("qa", "v1/qa/evaluations"),
}

ROUTE_ALIASES: dict[str, tuple[str, str]] = {
    "topology/graph": ("topology", "topology/graph"),
    "topology/evidence": ("topology", "topology/evidence"),
    "topology/refresh": ("topology", "topology/refresh"),
    "topology/snapshot/latest": ("retrieval", "topology/snapshot/latest"),
}


@app.middleware("http")
async def apply_rate_limit(request: Request, call_next):
    if not settings.rate_limit_enabled or request.url.path in {"/health", "/ready"}:
        return await call_next(request)

    rate_limiter.requests_per_minute = settings.rate_limit_requests_per_minute
    rate_limiter.burst = settings.rate_limit_burst
    allowed, remaining = rate_limiter.allow(_client_key(request))
    if not allowed:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": "Command Center API rate limit exceeded",
                "retry_after_seconds": 60,
                "limit": settings.rate_limit_requests_per_minute,
                "burst": settings.rate_limit_burst,
            },
            headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "command-center-api", "dangerous_actions_enabled": settings.enable_dangerous_actions, **auth_status(_auth_settings())}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    checks = {}
    async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
        for route, base_url in ROUTES.items():
            checks[route] = await _check(client, base_url)
    return {"status": "ok" if any(checks.values()) else "degraded", "service": "command-center-api", "checks": checks}


@app.get("/api/live-demo/status")
async def live_demo_status() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
        chaos_policy = await _optional_json(client, settings.chaos_executor_service_url, "/safety/policy")
        remediation_policy = await _optional_json(client, settings.remediation_executor_service_url, "/safety/policy")
        target = await _optional_json(client, settings.topology_service_url, "/target/workload")
        if target.get("available") is False:
            target = await _optional_json(client, settings.topology_service_url, "/topology")
    chaos = _policy_status(chaos_policy, "pod_kill")
    remediation = _policy_status(remediation_policy, "restart_deployment", require_execution_enabled=True)
    if not settings.enable_dangerous_actions:
        reason = "Command Center proxy gate ENABLE_DANGEROUS_ACTIONS is false"
        chaos["disabled_reasons"].append(reason)
        remediation["disabled_reasons"].append(reason)
    return {
        "command_center": {"dangerous_actions_enabled": settings.enable_dangerous_actions},
        "auth": auth_status(_auth_settings()),
        "chaos": chaos,
        "remediation": remediation,
        "target": target,
    }


@app.get("/api/contracts")
async def integration_contracts() -> dict[str, Any]:
    return {
        "schema": contract_schema_bundle(),
        "event_topics": {topic: model.__name__ for topic, model in EVENT_TOPIC_CONTRACTS.items()},
        "correlation": {
            "correlation_id": "Stable join key for a workflow; prefer Autopilot run_id when present, otherwise execution/report/event id.",
            "run_id": "Concrete run identifier for Autopilot, chaos, model, campaign, or execution-specific workflows.",
        },
    }


@app.get("/api/topology/graph")
async def topology_graph_alias() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
        graph = await _optional_json(client, settings.topology_service_url, "/topology/graph")
        if graph.get("available") is not False:
            return graph
        fallback = await _optional_json(client, settings.topology_service_url, "/topology")
        if fallback.get("available") is not False:
            fallback.setdefault("source", "topology-service-fallback")
            return fallback
    return {"nodes": [], "edges": [], "dependencies": {}, "source": "empty", "message": "No topology graph found. Register a target config with dependency_edges or deploy Sock Shop."}


@app.api_route("/api/{route:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(route: str, request: Request) -> Response:
    if request.method not in ALLOWED_METHODS:
        raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Command Center API allows only GET and POST")

    prefix, upstream_path = _resolve_route(route)
    body = await request.body()
    _enforce_auth(prefix, upstream_path, request.method, request)
    _enforce_safety(prefix, upstream_path, request.method, body)
    target = f"{ROUTES[prefix].rstrip('/')}/{upstream_path}".rstrip("/")
    headers = _forward_headers(request.headers)

    try:
        async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
            upstream = await client.request(
                request.method,
                target,
                params=request.query_params,
                content=body if body else None,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"Upstream timeout for {prefix}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream unavailable for {prefix}: {exc.__class__.__name__}") from exc

    response_headers = {"x-cascade-upstream": prefix}
    content_type = upstream.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


def _resolve_route(route: str) -> tuple[str, str]:
    cleaned = route.strip("/")
    if cleaned in ROUTE_ALIASES:
        return ROUTE_ALIASES[cleaned]
    for prefix in sorted(ROUTES, key=len, reverse=True):
        if cleaned == prefix:
            return prefix, ""
        marker = prefix + "/"
        if cleaned.startswith(marker):
            return prefix, cleaned[len(marker) :]
    raise HTTPException(status_code=404, detail="Unknown Command Center API route")


def _enforce_safety(prefix: str, upstream_path: str, method: str, body: bytes) -> None:
    if method != "POST":
        return
    normalized = upstream_path.strip("/")
    if (prefix, normalized) not in SAFE_POST_PATHS and not _is_safe_analysis_post(prefix, normalized):
        raise HTTPException(status_code=403, detail="This Command Center POST route is not exposed")

    if settings.enable_dangerous_actions:
        return
    payload = _json_body(body)
    if prefix == "remediation/executor" and normalized not in {"executions/dry-run", "safety/evaluate"}:
        raise HTTPException(status_code=403, detail="Real remediation execution is disabled by default")
    if prefix == "chaos/planner" and normalized == "plans" and payload.get("dry_run") is not True:
        raise HTTPException(status_code=403, detail="Only dry-run chaos plans are exposed by default")
    if prefix == "chaos/planner" and normalized == "campaigns":
        if payload.get("dry_run") is not True or payload.get("local_demo_execution_enabled") is True:
            raise HTTPException(status_code=403, detail="Only dry-run chaos campaigns are exposed by default")
    if prefix == "chaos/planner" and normalized.startswith("campaigns/") and normalized.endswith("/start"):
        if payload.get("dry_run") is not True:
            raise HTTPException(status_code=403, detail="Only dry-run chaos campaign starts are exposed by default")
    if prefix == "chaos/executor" and normalized == "runs":
        if payload.get("dry_run") is not True or payload.get("approved") is True:
            raise HTTPException(status_code=403, detail="Only chaos dry-run execution is exposed by default")
    if prefix == "remediation/executor" and normalized == "executions":
        raise HTTPException(status_code=403, detail="Real remediation execution is disabled by default")
    if prefix == "scheduler" and normalized == "scheduler/items":
        if payload.get("mode") not in {None, "dry_run_scheduler", "disabled"}:
            raise HTTPException(status_code=403, detail="Only dry-run scheduler mode is exposed by default")
        if payload.get("item_type") == "autopilot_run" and (payload.get("payload") or {}).get("mode") not in {None, "dry_run", "read_only"}:
            raise HTTPException(status_code=403, detail="Scheduled Autopilot runs must remain dry_run or read_only")


def _enforce_auth(prefix: str, upstream_path: str, method: str, request: Request) -> None:
    if method != "POST":
        return
    normalized = upstream_path.strip("/")
    if _is_sensitive_write(prefix, normalized):
        require_auth(request, _auth_settings(), action=f"{prefix}/{normalized}")


def _is_sensitive_write(prefix: str, normalized_path: str) -> bool:
    if prefix == "qa" and normalized_path in {"evaluations", "v1/qa/evaluations"}:
        return True
    if prefix == "remediation/approval" and normalized_path == "approvals":
        return True
    if prefix == "remediation/executor":
        return normalized_path == "executions" or normalized_path.endswith("/execute") or normalized_path.endswith("/retry")
    if prefix == "chaos/executor":
        return normalized_path == "runs" or normalized_path.endswith("/cleanup") or normalized_path.endswith("/observe")
    if prefix == "chaos/planner":
        parts = normalized_path.split("/")
        return len(parts) == 3 and parts[0] == "campaigns" and parts[2] in {"start", "pause", "resume", "stop"}
    if prefix == "autopilot" and normalized_path == "runs":
        return True
    if prefix == "scheduler" and normalized_path.startswith("scheduler/") and normalized_path != "scheduler/status":
        return True
    return "wipe" in normalized_path or "reset" in normalized_path


def _auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=settings.cascade_auth_enabled,
        local_demo_bypass=settings.cascade_local_demo_auth_bypass,
        api_keys=settings.cascade_api_keys,
        api_key_hashes=settings.cascade_api_key_hashes,
        auth_header=settings.cascade_auth_header,
    )


async def _optional_json(client: httpx.AsyncClient, base_url: str, path: str) -> dict[str, Any]:
    try:
        response = await client.get(base_url.rstrip("/") + path)
        if response.status_code >= 400:
            return {"available": False, "error": f"HTTP {response.status_code}"}
        data = response.json()
        return data if isinstance(data, dict) else {"available": False, "payload": data}
    except Exception as exc:
        return {"available": False, "error": exc.__class__.__name__}


def _policy_status(policy: dict[str, Any], safe_action: str, *, require_execution_enabled: bool = False) -> dict[str, Any]:
    protected = policy.get("protected") if isinstance(policy.get("protected"), dict) else {}
    allowed_actions = policy.get("supported_kinds") or policy.get("supported_action_types") or []
    allowed_namespaces = policy.get("allowed_namespaces", [])
    allowed_target_namespace = policy.get("allowed_target_namespace") or (allowed_namespaces[0] if len(allowed_namespaces) == 1 else None)
    disabled_reasons = []
    if policy.get("available") is False:
        disabled_reasons.append(f"Executor policy unavailable: {policy.get('error', 'unknown error')}")
    if not bool(policy.get("live_demo_mode")):
        disabled_reasons.append("CASCADE_LIVE_DEMO_MODE is false")
    if not bool(policy.get("dangerous_actions_enabled")):
        disabled_reasons.append("ENABLE_DANGEROUS_ACTIONS is false on executor")
    if safe_action == "pod_kill" and not bool(policy.get("real_chaos_enabled")):
        disabled_reasons.append("ENABLE_REAL_CHAOS is false")
    if safe_action == "restart_deployment":
        if not bool(policy.get("real_remediation_enabled")):
            disabled_reasons.append("ENABLE_REAL_REMEDIATION is false")
        if require_execution_enabled and not bool(policy.get("execution_enabled")):
            disabled_reasons.append("EXECUTION_ENABLED is false")
    if allowed_target_namespace != "cascade-targets":
        disabled_reasons.append("Allowed target namespace is not cascade-targets")
    if allowed_actions and safe_action not in allowed_actions:
        disabled_reasons.append(f"{safe_action} is not in allowed actions")
    return {
        "available": policy.get("available", True),
        "live_demo_mode": bool(policy.get("live_demo_mode")),
        "dangerous_actions_enabled": bool(policy.get("dangerous_actions_enabled")),
        "real_chaos_enabled": bool(policy.get("real_chaos_enabled")),
        "real_remediation_enabled": bool(policy.get("real_remediation_enabled")),
        "execution_enabled": bool(policy.get("execution_enabled")),
        "allowed_target_namespace": allowed_target_namespace,
        "allowed_namespaces": allowed_namespaces,
        "allowed_services": policy.get("allowed_services", []),
        "protected_services": protected.get("services", policy.get("protected_services", [])),
        "denied_services": (policy.get("denied") or {}).get("services", policy.get("denied_services", [])),
        "allowed_actions": [safe_action] if safe_action in allowed_actions else allowed_actions,
        "approval_required": policy.get("approval_required", True),
        "dry_run_first_required": policy.get("dry_run_first_required", True),
        "disabled_reasons": disabled_reasons,
    }


def _is_safe_analysis_post(prefix: str, normalized_path: str) -> bool:
    if prefix in {"chaos/planner", "remediation/recommender"}:
        parts = normalized_path.strip("/").split("/")
        if prefix == "chaos/planner" and len(parts) == 3 and parts[0] == "campaigns" and parts[2] in {"start", "pause", "resume", "stop"}:
            return True
        return len(parts) == 3 and parts[0] == "plans" and parts[2] == "validate"
    if prefix == "scheduler":
        parts = normalized_path.strip("/").split("/")
        return (
            len(parts) == 4
            and parts[0] == "scheduler"
            and parts[1] == "items"
            and parts[3] in {"enable", "disable", "pause", "resume", "run"}
        )
    return False


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        import json

        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    allowed = {}
    for key in ("content-type", "accept"):
        if key in headers:
            allowed[key] = headers[key]
    return allowed


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _check(client: httpx.AsyncClient, base_url: str) -> bool:
    try:
        response = await client.get(base_url.rstrip("/") + "/health")
        return response.status_code < 500
    except Exception:
        return False
