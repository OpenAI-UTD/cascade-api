from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from .schemas import EvaluationRequest, EvaluationResult
from .storage import DEFAULT_DB_PATH

JobStatusName = Literal["queued", "running", "completed", "failed"]


class EvaluationJob(BaseModel):
    job_id: str
    status: JobStatusName
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    evaluation_id: str | None = None
    project_id: str = ""
    pipeline_id: str = ""
    error: str = ""


class JobQueue:
    def __init__(self, db_path: str | Path | None = None, *, webhook_url: str = "") -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.webhook_url = webhook_url.strip()
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
                    CREATE TABLE IF NOT EXISTS evaluation_jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        evaluation_id TEXT,
                        project_id TEXT NOT NULL,
                        pipeline_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_evaluation_jobs_status
                        ON evaluation_jobs(status, created_at DESC);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def enqueue(self, payload: EvaluationRequest, *, evaluate_fn: Callable[[EvaluationRequest], EvaluationResult], save_fn: Callable[[EvaluationResult], None]) -> EvaluationJob:
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        created_at = datetime.now(UTC).isoformat()
        job = EvaluationJob(
            job_id=job_id,
            status="queued",
            created_at=created_at,
            project_id=payload.project.project_id,
            pipeline_id=payload.pipeline.pipeline_id,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO evaluation_jobs (
                        job_id, status, created_at, project_id, pipeline_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.job_id,
                        job.status,
                        job.created_at,
                        job.project_id,
                        job.pipeline_id,
                        payload.model_dump_json(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        worker = threading.Thread(
            target=self._run_job,
            args=(job_id, evaluate_fn, save_fn),
            name=f"cascade-qa-job-{job_id}",
            daemon=True,
        )
        worker.start()
        return job

    def get(self, job_id: str) -> EvaluationJob | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT job_id, status, created_at, started_at, completed_at, evaluation_id, project_id, pipeline_id, error FROM evaluation_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return EvaluationJob(
            job_id=row["job_id"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            evaluation_id=row["evaluation_id"],
            project_id=row["project_id"],
            pipeline_id=row["pipeline_id"],
            error=row["error"] or "",
        )

    def get_evaluation_payload(self, job_id: str) -> EvaluationResult | None:
        job = self.get(job_id)
        if job is None or not job.evaluation_id:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT payload_json FROM evaluations WHERE evaluation_id = ?",
                    (job.evaluation_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return EvaluationResult.model_validate(json.loads(row["payload_json"]))

    def _run_job(self, job_id: str, evaluate_fn: Callable[[EvaluationRequest], EvaluationResult], save_fn: Callable[[EvaluationResult], None]) -> None:
        started_at = datetime.now(UTC).isoformat()
        self._update_status(job_id, status="running", started_at=started_at)
        try:
            payload = self._load_payload(job_id)
            result = evaluate_fn(payload)
            save_fn(result)
            completed_at = datetime.now(UTC).isoformat()
            self._update_status(
                job_id,
                status="completed",
                completed_at=completed_at,
                evaluation_id=result.evaluation_id,
            )
            self._notify_webhook(job_id, result)
        except Exception as exc:  # noqa: BLE001 - job worker must capture all failures
            completed_at = datetime.now(UTC).isoformat()
            self._update_status(job_id, status="failed", completed_at=completed_at, error=f"{exc.__class__.__name__}: {exc}")

    def _load_payload(self, job_id: str) -> EvaluationRequest:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT payload_json FROM evaluation_jobs WHERE job_id = ?", (job_id,)).fetchone()
            finally:
                conn.close()
        if row is None:
            raise RuntimeError(f"job not found: {job_id}")
        return EvaluationRequest.model_validate(json.loads(row["payload_json"]))

    def _update_status(
        self,
        job_id: str,
        *,
        status: JobStatusName,
        started_at: str | None = None,
        completed_at: str | None = None,
        evaluation_id: str | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                fields: list[str] = ["status = ?"]
                values: list[Any] = [status]
                if started_at is not None:
                    fields.append("started_at = ?")
                    values.append(started_at)
                if completed_at is not None:
                    fields.append("completed_at = ?")
                    values.append(completed_at)
                if evaluation_id is not None:
                    fields.append("evaluation_id = ?")
                    values.append(evaluation_id)
                if error:
                    fields.append("error = ?")
                    values.append(error[:4000])
                values.append(job_id)
                conn.execute(f"UPDATE evaluation_jobs SET {', '.join(fields)} WHERE job_id = ?", values)
                conn.commit()
            finally:
                conn.close()

    def _notify_webhook(self, job_id: str, result: EvaluationResult) -> None:
        if not self.webhook_url:
            return
        payload = {
            "job_id": job_id,
            "evaluation_id": result.evaluation_id,
            "project_id": result.project.project_id,
            "quality_gate": result.quality_gate.model_dump(mode="json"),
            "status": result.status,
            "summary": result.summary,
        }
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "cascade-project-qa/0.2"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15):  # noqa: S310 - webhook URL is operator-controlled
                return
        except (HTTPError, URLError, TimeoutError, OSError):
            return


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatusName = "queued"
    message: str = Field(default="Evaluation queued")


class JobDetailResponse(BaseModel):
    job: EvaluationJob
    evaluation: EvaluationResult | None = None
