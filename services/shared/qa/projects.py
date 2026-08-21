from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .storage import DEFAULT_DB_PATH

Visibility = Literal["club", "officers", "members", "public"]


class ProjectRegistration(BaseModel):
    project_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=200)
    team: str = Field(default="", max_length=200)
    visibility: Visibility = "club"
    quota_runs_per_day: int = Field(default=100, ge=1, le=10_000)
    repository: str = Field(default="", max_length=500)


class RegisteredProject(ProjectRegistration):
    registered_at: str
    runs_today: int = 0
    quota_remaining: int = 0


class ProjectRegistry:
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
                    CREATE TABLE IF NOT EXISTS project_registry (
                        project_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        team TEXT NOT NULL DEFAULT '',
                        visibility TEXT NOT NULL DEFAULT 'club',
                        quota_runs_per_day INTEGER NOT NULL DEFAULT 100,
                        repository TEXT NOT NULL DEFAULT '',
                        registered_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS project_run_counts (
                        project_id TEXT NOT NULL,
                        run_day TEXT NOT NULL,
                        run_count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (project_id, run_day)
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def register(self, payload: ProjectRegistration) -> RegisteredProject:
        registered_at = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO project_registry (
                        project_id, name, team, visibility, quota_runs_per_day, repository, registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        name = excluded.name,
                        team = excluded.team,
                        visibility = excluded.visibility,
                        quota_runs_per_day = excluded.quota_runs_per_day,
                        repository = excluded.repository
                    """,
                    (
                        payload.project_id,
                        payload.name,
                        payload.team,
                        payload.visibility,
                        payload.quota_runs_per_day,
                        payload.repository,
                        registered_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get(payload.project_id) or RegisteredProject(**payload.model_dump(), registered_at=registered_at)

    def get(self, project_id: str) -> RegisteredProject | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT project_id, name, team, visibility, quota_runs_per_day, repository, registered_at
                    FROM project_registry WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
                runs_today = self._runs_today(conn, project_id)
            finally:
                conn.close()
        if row is None:
            return None
        quota = int(row["quota_runs_per_day"])
        return RegisteredProject(
            project_id=row["project_id"],
            name=row["name"],
            team=row["team"],
            visibility=row["visibility"],
            quota_runs_per_day=quota,
            repository=row["repository"],
            registered_at=row["registered_at"],
            runs_today=runs_today,
            quota_remaining=max(0, quota - runs_today),
        )

    def list_registered(self, *, limit: int = 200) -> list[RegisteredProject]:
        limit = max(1, min(limit, 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT project_id, name, team, visibility, quota_runs_per_day, repository, registered_at
                    FROM project_registry
                    ORDER BY registered_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                projects: list[RegisteredProject] = []
                for row in rows:
                    quota = int(row["quota_runs_per_day"])
                    runs_today = self._runs_today(conn, row["project_id"])
                    projects.append(
                        RegisteredProject(
                            project_id=row["project_id"],
                            name=row["name"],
                            team=row["team"],
                            visibility=row["visibility"],
                            quota_runs_per_day=quota,
                            repository=row["repository"],
                            registered_at=row["registered_at"],
                            runs_today=runs_today,
                            quota_remaining=max(0, quota - runs_today),
                        )
                    )
            finally:
                conn.close()
        return projects

    def record_run(self, project_id: str) -> bool:
        day = datetime.now(UTC).date().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT quota_runs_per_day FROM project_registry WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    return True
                quota = int(row["quota_runs_per_day"])
                runs_today = self._runs_today(conn, project_id)
                if runs_today >= quota:
                    return False
                conn.execute(
                    """
                    INSERT INTO project_run_counts (project_id, run_day, run_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(project_id, run_day) DO UPDATE SET run_count = run_count + 1
                    """,
                    (project_id, day),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    def merge_summaries(self, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        registry = {item.project_id: item for item in self.list_registered(limit=500)}
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for summary in summaries:
            project_id = str(summary.get("project_id") or "")
            seen.add(project_id)
            reg = registry.get(project_id)
            merged.append(_merge_project(summary, reg))
        for project_id, reg in registry.items():
            if project_id in seen:
                continue
            merged.append(_merge_project({"project_id": project_id}, reg))
        return merged

    def _runs_today(self, conn: sqlite3.Connection, project_id: str) -> int:
        day = datetime.now(UTC).date().isoformat()
        row = conn.execute(
            "SELECT run_count FROM project_run_counts WHERE project_id = ? AND run_day = ?",
            (project_id, day),
        ).fetchone()
        return int(row["run_count"]) if row else 0


def _merge_project(summary: dict[str, Any], reg: RegisteredProject | None) -> dict[str, Any]:
    payload = dict(summary)
    if reg is None:
        return payload
    payload.update(
        {
            "name": reg.name,
            "team": reg.team,
            "visibility": reg.visibility,
            "quota_runs_per_day": reg.quota_runs_per_day,
            "repository": reg.repository,
            "registered_at": reg.registered_at,
            "runs_today": reg.runs_today,
            "quota_remaining": reg.quota_remaining,
        }
    )
    return payload
