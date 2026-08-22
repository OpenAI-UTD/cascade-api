from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .schemas import RunCreate, RunResult

DEFAULT_DB_PATH = os.environ.get("CASCADE_EVALS_DB_PATH", ".cascade/evals-runs.db")
MAX_RUNS_PER_PROJECT = int(os.environ.get("CASCADE_EVALS_MAX_RUNS_PER_PROJECT", "500"))


class RunStore:
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
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        project_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_project_created
                        ON runs(project_name, created_at DESC);
                    CREATE TABLE IF NOT EXISTS case_results (
                        run_id TEXT NOT NULL,
                        case_name TEXT NOT NULL,
                        weighted_score REAL NOT NULL,
                        latency_ms REAL NOT NULL,
                        degraded INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_case_results_run
                        ON case_results(run_id);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def save(self, result: RunResult, *, request: RunCreate | None = None) -> None:
        payload = result.model_dump(mode="json")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO runs (run_id, project_name, status, created_at, payload_json, request_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        project_name = excluded.project_name,
                        status = excluded.status,
                        created_at = excluded.created_at,
                        payload_json = excluded.payload_json
                    """,
                    (
                        result.id,
                        result.project_name,
                        result.status,
                        result.created_at,
                        json.dumps(payload),
                        request.model_dump_json() if request is not None else "",
                    ),
                )
                if result.results:
                    conn.execute("DELETE FROM case_results WHERE run_id = ?", (result.id,))
                    conn.executemany(
                        """
                        INSERT INTO case_results (
                            run_id, case_name, weighted_score, latency_ms, degraded, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                result.id,
                                item.case.name,
                                item.weighted_score,
                                item.latency_ms,
                                int(item.degraded),
                                json.dumps(item.model_dump(mode="json")),
                            )
                            for item in result.results
                        ],
                    )
                self._trim_project(conn, result.project_name)
                conn.commit()
            finally:
                conn.close()

    def get(self, run_id: str) -> RunResult | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload_json FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return RunResult.model_validate(json.loads(row["payload_json"]))

    def get_request(self, run_id: str) -> RunCreate | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT request_json FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None or not row["request_json"]:
            return None
        return RunCreate.model_validate(json.loads(row["request_json"]))

    def list_project(self, project_name: str, *, limit: int = 20) -> list[RunResult]:
        limit = max(1, min(limit, 100))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM runs
                    WHERE project_name = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (project_name, limit),
                ).fetchall()
            finally:
                conn.close()
        return [RunResult.model_validate(json.loads(row["payload_json"])) for row in rows]

    def list_recent(self, *, limit: int = 20) -> list[RunResult]:
        limit = max(1, min(limit, 100))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM runs
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return [RunResult.model_validate(json.loads(row["payload_json"])) for row in rows]

    def list_projects(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT project_name, MAX(created_at) AS last_run_at, COUNT(*) AS run_count
                    FROM runs
                    GROUP BY project_name
                    ORDER BY last_run_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                summaries: list[dict[str, Any]] = []
                for row in rows:
                    latest = conn.execute(
                        """
                        SELECT payload_json FROM runs
                        WHERE project_name = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (row["project_name"],),
                    ).fetchone()
                    aggregate_score = None
                    if latest is not None:
                        payload = json.loads(latest["payload_json"])
                        aggregate_score = payload.get("aggregate_score")
                    summaries.append(
                        {
                            "project_name": row["project_name"],
                            "last_run_at": row["last_run_at"],
                            "run_count": row["run_count"],
                            "aggregate_score": aggregate_score,
                        }
                    )
            finally:
                conn.close()
        return summaries

    def count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS total FROM runs").fetchone()
            finally:
                conn.close()
        return int(row["total"]) if row else 0

    def _trim_project(self, conn: sqlite3.Connection, project_name: str) -> None:
        rows = conn.execute(
            """
            SELECT run_id FROM runs
            WHERE project_name = ?
            ORDER BY created_at DESC
            """,
            (project_name,),
        ).fetchall()
        if len(rows) <= MAX_RUNS_PER_PROJECT:
            return
        stale_ids = [row["run_id"] for row in rows[MAX_RUNS_PER_PROJECT:]]
        conn.executemany("DELETE FROM runs WHERE run_id = ?", [(item,) for item in stale_ids])
        conn.executemany("DELETE FROM case_results WHERE run_id = ?", [(item,) for item in stale_ids])

    def reset(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM runs")
                conn.execute("DELETE FROM case_results")
                conn.commit()
            finally:
                conn.close()
