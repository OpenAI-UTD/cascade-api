from __future__ import annotations

import importlib.machinery
import importlib.util
import itertools
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SERVICE_APP_DIR = ROOT / "services" / "evals-service" / "app"

ENV_KEYS = (
    "CASCADE_AUTH_ENABLED",
    "CASCADE_LOCAL_DEMO_AUTH_BYPASS",
    "CASCADE_API_KEYS",
    "CASCADE_API_KEY_HASHES",
    "CASCADE_AUTH_HEADER",
    "CASCADE_EVALS_DB_PATH",
    "CASCADE_EVALS_LLM_BASE_URL",
    "CASCADE_EVALS_LLM_API_KEY",
    "CASCADE_EVALS_LLM_MODEL",
    "CASCADE_EVALS_PRICE_IN_PER_MTOK",
    "CASCADE_EVALS_PRICE_OUT_PER_MTOK",
    "CASCADE_EVALS_RATE_LIMIT_PER_MINUTE",
)

_MODULE_SEQ = itertools.count()

RUBRIC = [
    {"name": "accuracy", "weight": 0.6, "description": "Factual correctness"},
    {"name": "tone", "weight": 0.4, "description": "Friendly club tone"},
]

CANDIDATE = "Accuracy matters. The Cascade Evals Studio reports weighted rubric scores for club prompt evaluations."


def _load_module(db_path: Path, **env_overrides: str):
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CASCADE_EVALS_DB_PATH"] = str(db_path)
        for key, value in env_overrides.items():
            if value is not None:
                os.environ[key] = value
        package_name = f"evals_service_pkg_{next(_MODULE_SEQ)}"
        package = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(package_name, None))
        package.__path__ = [str(SERVICE_APP_DIR)]
        sys.modules[package_name] = package
        spec = importlib.util.spec_from_file_location(f"{package_name}.main", SERVICE_APP_DIR / "main.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package_name}.main"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_app(db_path: Path, **env_overrides: str) -> TestClient:
    return TestClient(_load_module(db_path, **env_overrides).app)


def _payload(**overrides) -> dict:
    payload = {
        "project_name": "club-website",
        "rubric": RUBRIC,
        "cases": [{"name": "membership-answer", "input": CANDIDATE, "expected": "rubric scores"}],
        "adapter": "heuristic",
    }
    payload.update(overrides)
    return payload


def _submit(client: TestClient, payload: dict | None = None) -> dict:
    response = client.post("/runs", json=payload or _payload())
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    return body


def _await_run(client: TestClient, run_id: str, *, timeout_seconds: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"completed", "failed"}:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not settle within {timeout_seconds}s")


def _documented_score(name: str, words: int, expected_bonus: float) -> float:
    tokens = [token for token in name.casefold().replace("-", " ").split() if len(token) >= 3]
    matched = sum(1 for token in tokens if token in CANDIDATE.casefold())
    coverage = matched / len(tokens) if tokens else 0.0
    length_adequacy = min(1.0, words / 30)
    raw = 3.0 * coverage + 1.0 * length_adequacy + 1.0 * expected_bonus
    return round(min(5.0, raw), 2)


def test_health_reports_service_storage_and_auth(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "evals-service"
    assert body["stored_runs"] == 0
    assert body["storage"] == "sqlite"
    assert body["auth"]["auth_enabled"] is False

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "service": "evals-service"}


def test_adapters_payload_lists_both_and_default_state(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    body = client.get("/adapters").json()
    assert body["count"] == 2
    assert body["default_adapter"] == "heuristic"
    by_name = {item["name"]: item for item in body["adapters"]}
    assert by_name["heuristic"]["configured"] is True
    assert by_name["openai-compatible"]["configured"] is False
    assert body["llm_model"] is None


def test_adapters_reports_llm_configured_when_env_set(tmp_path: Path) -> None:
    client = _load_app(
        tmp_path / "evals.db",
        CASCADE_EVALS_LLM_BASE_URL="http://llm.test/v1",
        CASCADE_EVALS_LLM_MODEL="judge-1",
    )
    body = client.get("/adapters").json()
    by_name = {item["name"]: item for item in body["adapters"]}
    assert by_name["openai-compatible"]["configured"] is True
    assert body["llm_model"] == "judge-1"
    assert client.get("/health").json()["llm_judge_configured"] is True


def test_rubric_weight_sum_must_total_one(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    payload = _payload(rubric=[RUBRIC[0], {"name": "tone", "weight": 0.2}])
    response = client.post("/runs", json=payload)
    assert response.status_code == 422
    assert "must sum to 1.0" in response.json()["detail"][0]["msg"]


def test_rubric_weight_tolerance_accepts_near_one(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    payload = _payload(rubric=[{"name": "accuracy", "weight": 0.5}, {"name": "tone", "weight": 0.497}])
    assert client.post("/runs", json=payload).status_code == 202


def test_criterion_weight_bounds_are_enforced(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    response = client.post("/runs", json=_payload(rubric=[{"name": "accuracy", "weight": 1.5}]))
    assert response.status_code == 422


def test_rubric_and_case_size_limits_are_enforced(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    assert client.post("/runs", json=_payload(rubric=[])).status_code == 422
    too_many_criteria = [
        {"name": f"criterion-{index}", "weight": 1.0 / 9} for index in range(9)
    ]
    assert client.post("/runs", json=_payload(rubric=too_many_criteria)).status_code == 422
    assert client.post("/runs", json=_payload(cases=[])).status_code == 422
    fifty_one = [
        {"name": f"case-{index}", "input": "text"} for index in range(51)
    ]
    assert client.post("/runs", json=_payload(cases=fifty_one)).status_code == 422


def test_run_lifecycle_queued_then_completed_with_aggregate(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    accepted = _submit(client)
    assert accepted["run_id"].startswith("run_")

    queued = client.get(f"/runs/{accepted['run_id']}").json()
    assert queued["status"] in {"queued", "running", "completed"}

    run = _await_run(client, accepted["run_id"])
    assert run["status"] == "completed"
    assert run["finished_at"] is not None
    assert len(run["results"]) == 1
    weighted = run["results"][0]["weighted_score"]
    assert run["aggregate_score"] == round(weighted / 1, 4)


def test_heuristic_scoring_matches_documented_formula(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    run = _await_run(client, _submit(client)["run_id"])
    result = run["results"][0]

    words = len(CANDIDATE.split())
    accuracy = _documented_score("accuracy", words, expected_bonus=1.0)
    tone = _documented_score("tone", words, expected_bonus=1.0)
    assert result["scores"]["accuracy"]["score"] == accuracy
    assert result["scores"]["tone"]["score"] == tone
    expected_weighted = round(0.6 * accuracy + 0.4 * tone, 4)
    assert result["weighted_score"] == expected_weighted
    assert "keyword coverage" in result["scores"]["accuracy"]["justification"]

    case_result = result
    assert case_result["degraded"] is False
    assert case_result["tokens_out"] == 0
    assert case_result["tokens_in"] == len(CANDIDATE) // 4
    expected_cost = round((len(CANDIDATE) // 4) / 1_000_000 * 0.15, 6)
    assert case_result["cost_usd"] == expected_cost
    assert case_result["latency_ms"] >= 0


def test_heuristic_scoring_is_deterministic(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    first = _await_run(client, _submit(client)["run_id"])
    second = _await_run(client, _submit(client)["run_id"])
    assert first["id"] != second["id"]
    assert second["aggregate_score"] == first["aggregate_score"]
    assert second["results"][0]["scores"] == first["results"][0]["scores"]


def test_expected_substring_bonus_raises_scores(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    with_bonus = _payload(cases=[{"name": "bonus", "input": CANDIDATE, "expected": "rubric scores"}])
    without_bonus = _payload(cases=[{"name": "no-bonus", "input": CANDIDATE, "expected": "not present anywhere"}])
    hit = _await_run(client, _submit(client, with_bonus)["run_id"])["results"][0]
    miss = _await_run(client, _submit(client, without_bonus)["run_id"])["results"][0]
    assert hit["weighted_score"] > miss["weighted_score"]
    assert miss["weighted_score"] == round(
        0.6 * (_documented_score("accuracy", len(CANDIDATE.split()), 0.0))
        + 0.4 * (_documented_score("tone", len(CANDIDATE.split()), 0.0)),
        4,
    )


def test_run_persistence_round_trip_across_reload(tmp_path: Path) -> None:
    db_path = tmp_path / "evals.db"
    first = _load_app(db_path)
    run_id = _submit(first)["run_id"]
    original = _await_run(first, run_id)

    reloaded = _load_app(db_path)
    fetched = reloaded.get(f"/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "completed"
    assert fetched.json()["aggregate_score"] == original["aggregate_score"]
    assert fetched.json()["results"][0]["scores"] == original["results"][0]["scores"]
    assert reloaded.get("/health").json()["stored_runs"] == 1


def test_list_runs_filters_by_project(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    club_run = _submit(client)["run_id"]
    other_payload = _payload(project_name="discord-bot", cases=[{"name": "c", "input": "text"}])
    other_run = _submit(client, other_payload)["run_id"]

    listing = client.get("/runs", params={"project": "club-website"}).json()
    assert listing["count"] == 1
    assert listing["runs"][0]["id"] == club_run

    everything = client.get("/runs").json()
    assert everything["count"] >= 2
    assert {club_run, other_run} <= {run["id"] for run in everything["runs"]}


def test_get_unknown_run_returns_404(tmp_path: Path) -> None:
    client = _load_app(tmp_path / "evals.db")
    response = client.get("/runs/run_does_not_exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found"


def test_submit_requires_auth_when_enabled(tmp_path: Path) -> None:
    client = _load_app(
        tmp_path / "evals.db",
        CASCADE_AUTH_ENABLED="true",
        CASCADE_API_KEYS="secret-evals-key",
    )
    denied = client.post("/runs", json=_payload())
    assert denied.status_code == 401
    assert "submit eval run" in denied.json()["detail"]

    wrong = client.post("/runs", json=_payload(), headers={"Authorization": "Bearer wrong-key"})
    assert wrong.status_code == 401

    allowed = client.post("/runs", json=_payload(), headers={"Authorization": "Bearer secret-evals-key"})
    assert allowed.status_code == 202


def test_local_demo_bypass_allows_submission_when_auth_enabled(tmp_path: Path) -> None:
    client = _load_app(
        tmp_path / "evals.db",
        CASCADE_AUTH_ENABLED="true",
        CASCADE_LOCAL_DEMO_AUTH_BYPASS="true",
    )
    health = client.get("/health").json()
    assert health["auth"]["local_demo_auth_bypass"] is True
    assert client.post("/runs", json=_payload()).status_code == 202


def test_openai_compatible_degrades_to_heuristic_when_judge_unreachable(tmp_path: Path) -> None:
    client = _load_app(
        tmp_path / "evals.db",
        CASCADE_EVALS_LLM_BASE_URL="http://127.0.0.1:9",
        CASCADE_EVALS_LLM_MODEL="unreachable-judge",
    )
    payload = _payload(adapter="openai-compatible")
    run = _await_run(client, _submit(client, payload)["run_id"])
    assert run["status"] == "completed"
    assert run["adapter"] == "openai-compatible"
    result = run["results"][0]
    assert result["degraded"] is True
    fallback_accuracy = _documented_score("accuracy", len(CANDIDATE.split()), 1.0)
    assert result["scores"]["accuracy"]["score"] == fallback_accuracy


class _StubJudgeHandler(BaseHTTPRequestHandler):
    criteria = []
    usage = {}

    def do_POST(self):  # noqa: N802 - http.server API
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        content = json.dumps({"criteria": self.criteria})
        body = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": self.usage,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def test_openai_compatible_uses_judge_scores_and_usage_cost(tmp_path: Path) -> None:
    _StubJudgeHandler.criteria = [
        {"name": "accuracy", "score": 4.5, "justification": "factually solid"},
        {"name": "tone", "score": 2.5, "justification": "a bit dry"},
    ]
    _StubJudgeHandler.usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    server = HTTPServer(("127.0.0.1", 0), _StubJudgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        client = _load_app(
            tmp_path / "evals.db",
            CASCADE_EVALS_LLM_BASE_URL=base_url,
            CASCADE_EVALS_LLM_MODEL="stub-judge",
            CASCADE_EVALS_PRICE_IN_PER_MTOK="1.0",
            CASCADE_EVALS_PRICE_OUT_PER_MTOK="2.0",
        )
        payload = _payload(adapter="openai-compatible")
        run = _await_run(client, _submit(client, payload)["run_id"])
    finally:
        server.shutdown()
        server.server_close()

    assert run["status"] == "completed"
    result = run["results"][0]
    assert result["degraded"] is False
    assert result["scores"]["accuracy"]["score"] == 4.5
    assert result["scores"]["accuracy"]["justification"] == "factually solid"
    assert result["tokens_in"] == 1_000_000
    assert result["tokens_out"] == 500_000
    assert result["cost_usd"] == 2.0
    assert result["weighted_score"] == round(0.6 * 4.5 + 0.4 * 2.5, 4)
    assert run["aggregate_score"] == round(0.6 * 4.5 + 0.4 * 2.5, 4)


def test_estimate_cost_matches_price_table(tmp_path: Path) -> None:
    module = _load_module(tmp_path / "evals.db")
    engine = sys.modules[f"{module.__package__}.engine"]
    default_cost = engine.estimate_cost(1_000_000, 1_000_000, module._engine_settings())
    assert default_cost == round(0.15 + 0.60, 6)
    custom = engine.EngineSettings(price_in_per_mtok=3.0, price_out_per_mtok=1.0)
    assert engine.estimate_cost(500_000, 250_000, custom) == 1.75
