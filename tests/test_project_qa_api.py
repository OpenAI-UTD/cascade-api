from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "project-qa-service" / "app" / "main.py"
spec = importlib.util.spec_from_file_location("project_qa_service_main", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["project_qa_service_main"] = module
spec.loader.exec_module(module)
client = TestClient(module.app)


def test_create_get_and_list_evaluation() -> None:
    payload = {
        "project": {"project_id": "web-app", "name": "Web App"},
        "revision": "deadbeef",
        "checks": [{"name": "build", "status": "error", "kind": "build", "output": "TypeScript compilation failed"}],
    }

    created = client.post("/evaluations", json=payload)
    assert created.status_code == 201
    evaluation = created.json()
    assert evaluation["project"]["project_id"] == "web-app"
    assert evaluation["quality_gate"]["passed"] is False

    fetched = client.get(f"/evaluations/{evaluation['evaluation_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["evaluation_id"] == evaluation["evaluation_id"]

    listed = client.get("/projects/web-app/evaluations")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1


def test_evaluation_requires_ci_or_runtime_evidence() -> None:
    response = client.post("/evaluations", json={"project": {"project_id": "empty", "name": "Empty"}})
    assert response.status_code == 422
