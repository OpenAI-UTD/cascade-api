from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.shared.security.ratelimit import RateLimitMiddleware, TokenBucketRateLimiter, client_key

ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self, start: float | None = None) -> None:
        import time

        self.now = start if start is not None else time.monotonic()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_capacity_boundary_allows_exactly_capacity_then_denies() -> None:
    limiter = TokenBucketRateLimiter(capacity=3, window_seconds=60.0, clock=FakeClock())
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is False


def test_tokens_refill_continuously_with_injected_clock() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(capacity=2, window_seconds=60.0, clock=clock)
    assert limiter.try_acquire("ip-a")
    assert limiter.try_acquire("ip-a")
    assert limiter.try_acquire("ip-a") is False
    clock.advance(30.0)
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is False
    clock.advance(60.0)
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is False


def test_keys_are_isolated() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, window_seconds=60.0, clock=FakeClock())
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-b") is True
    assert limiter.try_acquire("ip-a") is False
    assert limiter.try_acquire("ip-b") is False


def test_retry_after_matches_refill_rate() -> None:
    limiter = TokenBucketRateLimiter(capacity=30, window_seconds=60.0, clock=FakeClock())
    assert limiter.retry_after_seconds() == pytest.approx(2.0)


def test_invalid_configuration_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=0, window_seconds=60.0, clock=FakeClock())
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=5, window_seconds=0, clock=FakeClock())
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=5, window_seconds=60.0, clock=FakeClock(), max_tracked_keys=-1)


def test_reset_clears_state() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, window_seconds=60.0, clock=FakeClock())
    assert limiter.try_acquire("ip-a") is True
    assert limiter.try_acquire("ip-a") is False
    limiter.reset()
    assert limiter.try_acquire("ip-a") is True


def test_client_key_prefers_first_forwarded_hop() -> None:
    scope = {
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("testclient", 50000),
    }
    assert client_key(scope) == "203.0.113.7"


def test_client_key_falls_back_to_asgi_client_then_unknown() -> None:
    assert client_key({"headers": [], "client": ("testclient", 50000)}) == "testclient"
    assert client_key({"headers": []}) == "unknown"


def _run_middleware(middleware: RateLimitMiddleware, scope: dict) -> tuple[list[dict], int]:
    """Invoke ASGI middleware synchronously; returns (sent events, upstream call count)."""
    calls = {"count": 0}

    async def app(scope, receive, send) -> None:
        calls["count"] += 1

    middleware.app = app
    events: list[dict] = []

    async def send(event: dict) -> None:
        events.append(event)

    asyncio.run(middleware(scope, None, send))
    return events, calls["count"]


def test_middleware_passes_non_matching_routes_through() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, window_seconds=60.0, clock=FakeClock())
    middleware = RateLimitMiddleware(app=None, limiter=limiter, routes=[("POST", "/runs")], message="limited")
    scope = {"type": "http", "method": "GET", "path": "/runs", "headers": [], "client": ("t", 1)}
    events, upstream_calls = _run_middleware(middleware, scope)
    assert events == []
    assert upstream_calls == 1


def test_middleware_returns_429_detail_body_when_limited() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, window_seconds=60.0, clock=FakeClock())
    middleware = RateLimitMiddleware(app=None, limiter=limiter, routes=[("POST", "/runs")], message="slow down")
    scope = {"type": "http", "method": "POST", "path": "/runs", "headers": [], "client": ("t", 1)}

    events, upstream_calls = _run_middleware(middleware, scope)
    assert events == []
    assert upstream_calls == 1

    limited_events, upstream_after_limit = _run_middleware(middleware, scope)
    assert upstream_after_limit == 0
    assert limited_events[0]["status"] == 429
    headers = {name: value for name, value in limited_events[0]["headers"]}
    assert headers[b"retry-after"] == b"60"
    body = b"".join(event.get("body", b"") for event in limited_events if event["type"] == "http.response.body")
    assert json.loads(body) == {"detail": "slow down"}


def test_middleware_ignores_non_http_scopes() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, window_seconds=60.0, clock=FakeClock())
    middleware = RateLimitMiddleware(app=None, limiter=limiter, routes=[("POST", "/runs")])
    scope = {"type": "lifespan"}
    events, upstream_calls = _run_middleware(middleware, scope)
    assert events == []
    assert upstream_calls == 1


def _load_evals_module(db_path: Path, per_minute: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CASCADE_EVALS_DB_PATH", str(db_path))
    monkeypatch.setenv("CASCADE_EVALS_RATE_LIMIT_PER_MINUTE", per_minute)
    package_seq = f"evals_rl_pkg_{db_path.name.replace('-', '_').replace('.', '_')}"
    service_dir = ROOT / "services" / "evals-service" / "app"
    package = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(package_seq, None))
    package.__path__ = [str(service_dir)]
    sys.modules[package_seq] = package
    spec = importlib.util.spec_from_file_location(f"{package_seq}.main", service_dir / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_seq}.main"] = module
    spec.loader.exec_module(module)
    return module


def test_evals_runs_endpoint_enforces_per_ip_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_evals_service import _payload

    client = TestClient(_load_evals_module(tmp_path / "evals-rl.db", "2", monkeypatch).app)

    assert client.post("/runs", json=_payload()).status_code == 202
    assert client.post("/runs", json=_payload()).status_code == 202
    limited = client.post("/runs", json=_payload())
    assert limited.status_code == 429
    assert set(limited.json()) == {"detail"}
    assert "Too many eval run submissions" in limited.json()["detail"]
    assert limited.headers.get("retry-after")

    assert client.get("/runs").status_code == 200
    assert client.get("/health").status_code == 200


def test_evals_rate_limit_recovers_with_injected_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_evals_service import _payload

    module = _load_evals_module(tmp_path / "evals-rl-clock.db", "2", monkeypatch)
    client = TestClient(module.app)
    limiter = module.rate_limiter
    assert isinstance(limiter, TokenBucketRateLimiter)
    clock = FakeClock()
    limiter.clock = clock

    assert client.post("/runs", json=_payload()).status_code == 202
    assert client.post("/runs", json=_payload()).status_code == 202
    assert client.post("/runs", json=_payload()).status_code == 429
    clock.advance(61.0)
    assert client.post("/runs", json=_payload()).status_code == 202


def test_project_qa_evaluations_endpoint_enforces_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASCADE_QA_DB_PATH", str(tmp_path / "qa-rl.db"))
    monkeypatch.setenv("CASCADE_QA_RATE_LIMIT_PER_MINUTE", "2")
    module_path = ROOT / "services" / "project-qa-service" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("project_qa_service_main_rl", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["project_qa_service_main_rl"] = module
    spec.loader.exec_module(module)
    client = TestClient(module.app)

    payload = {
        "project": {"project_id": "rate-limited", "name": "Rate Limited"},
        "checks": [{"name": "build", "status": "passed", "kind": "build"}],
    }
    assert client.post("/evaluations", json=payload).status_code == 201
    assert client.post("/v1/qa/evaluations", json=payload).status_code in {200, 201}
    limited = client.post("/evaluations", json=payload)
    assert limited.status_code == 429
    assert set(limited.json()) == {"detail"}
    assert "Request rate limit exceeded" in limited.json()["detail"]
    assert client.get("/projects").status_code == 200
