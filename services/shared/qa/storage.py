from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .schemas import EvaluationResult

DEFAULT_DB_PATH = os.environ.get("CASCADE_QA_DB_PATH", ".cascade/qa-evaluations.db")
MAX_RUNS_PER_PROJECT = int(os.environ.get("CASCADE_QA_MAX_RUNS_PER_PROJECT", "500"))


class EvaluationStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS evaluations (
                        evaluation_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_evaluations_project_created
                        ON evaluations(project_id, created_at DESC);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def save(self, result: EvaluationResult) -> None:
        payload = result.model_dump(mode="json")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO evaluations (evaluation_id, project_id, created_at, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (result.evaluation_id, result.project.project_id, result.created_at, json.dumps(payload)),
                )
                self._trim_project(conn, result.project.project_id)
                conn.commit()
            finally:
                conn.close()

    def get(self, evaluation_id: str) -> EvaluationResult | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload_json FROM evaluations WHERE evaluation_id = ?",
                    (evaluation_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return EvaluationResult.model_validate(json.loads(row["payload_json"]))

    def list_project(self, project_id: str, *, limit: int = 20) -> list[EvaluationResult]:
        limit = max(1, min(limit, 100))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM evaluations
                    WHERE project_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (project_id, limit),
                ).fetchall()
            finally:
                conn.close()
        return [EvaluationResult.model_validate(json.loads(row["payload_json"])) for row in rows]

    def list_projects(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT project_id, MAX(created_at) AS last_run_at, COUNT(*) AS run_count
                    FROM evaluations
                    GROUP BY project_id
                    ORDER BY last_run_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                summaries: list[dict[str, Any]] = []
                for row in rows:
                    latest = conn.execute(
                        """
                        SELECT payload_json FROM evaluations
                        WHERE project_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (row["project_id"],),
                    ).fetchone()
                    gate_passed = None
                    findings = 0
                    if latest is not None:
                        payload = json.loads(latest["payload_json"])
                        gate_passed = (payload.get("quality_gate") or {}).get("passed")
                        findings = len(payload.get("findings") or [])
                    summaries.append(
                        {
                            "project_id": row["project_id"],
                            "last_run_at": row["last_run_at"],
                            "run_count": row["run_count"],
                            "gate_passed": gate_passed,
                            "findings": findings,
                        }
                    )
            finally:
                conn.close()
        return summaries

    def count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS total FROM evaluations").fetchone()
            finally:
                conn.close()
        return int(row["total"]) if row else 0

    def _trim_project(self, conn: sqlite3.Connection, project_id: str) -> None:
        rows = conn.execute(
            """
            SELECT evaluation_id FROM evaluations
            WHERE project_id = ?
            ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
        if len(rows) <= MAX_RUNS_PER_PROJECT:
            return
        stale_ids = [row["evaluation_id"] for row in rows[MAX_RUNS_PER_PROJECT:]]
        conn.executemany("DELETE FROM evaluations WHERE evaluation_id = ?", [(item,) for item in stale_ids])

    def reset(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM evaluations")
                conn.commit()
            finally:
                conn.close()
