from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _load_app(db_path: Path):
    os.environ["CASCADE_QA_DB_PATH"] = str(db_path)
    module_path = Path(__file__).resolve().parents[1] / "services" / "project-qa-service" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("project_qa_service_main", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["project_qa_service_main"] = module
    spec.loader.exec_module(module)
    return TestClient(module.app)


def test_create_get_and_list_evaluation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _load_app(Path(tmp) / "qa.db")
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
    with tempfile.TemporaryDirectory() as tmp:
        client = _load_app(Path(tmp) / "qa.db")
        response = client.post("/evaluations", json={"project": {"project_id": "empty", "name": "Empty"}})
        assert response.status_code == 422


def test_affiliation_findings_from_source_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _load_app(Path(tmp) / "qa.db")
        payload = {
            "project": {"project_id": "bad-copy", "name": "Bad Copy"},
            "checks": [{"name": "lint", "status": "passed", "kind": "lint"}],
            "source_files": [{"path": "README.md", "content": "We are OpenAI official website."}],
            "affiliation_lint": True,
        }
        created = client.post("/evaluations", json=payload)
        assert created.status_code == 201
        evaluation = created.json()
        assert evaluation["quality_gate"]["passed"] is False
        assert any(finding["category"] == "affiliation" for finding in evaluation["findings"])


def test_list_projects_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _load_app(Path(tmp) / "qa.db")
        payload = {
            "project": {"project_id": "club-api", "name": "Club API"},
            "checks": [{"name": "tests", "status": "passed", "kind": "test"}],
        }
        assert client.post("/evaluations", json=payload).status_code == 201
        projects = client.get("/projects")
        assert projects.status_code == 200
        body = projects.json()
        assert body["count"] == 1
        assert body["projects"][0]["project_id"] == "club-api"
