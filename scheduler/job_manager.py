"""
scheduler/job_manager.py — APScheduler lifecycle + SQLite CRUD for job records.

Design decisions:
- Two separate SQLite databases:
    scheduler.db  → APScheduler's internal job store (trigger times, pickled callables)
    jobs.db       → Application-level job metadata (status, stage, urls, errors)
  Keeping them separate avoids schema conflicts and lets us query job metadata
  without touching APScheduler internals.
- Synchronous sqlite3 calls are fast (< 1 ms on local SSD) and wrapped where
  needed. The scheduler callbacks run inside the asyncio loop via AsyncIOExecutor.
- Retry logic: on StageError the orchestrator increments the retry counter.
  After SCHEDULER_MAX_RETRIES failures the job is permanently marked failed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Optional

import structlog
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    SCHEDULER_MISFIRE_GRACE,
    SCHEDULER_MAX_RETRIES,
    SCHEDULER_RETRY_DELAY_SECONDS,
    get_settings,
)
from models.errors import JobCancellationError, JobNotFoundError
from scheduler.fingerprint import make_fingerprint

logger = structlog.get_logger()

AsyncCallable = Callable[..., Coroutine[Any, Any, Any]]


class JobManager:
    """Owns the APScheduler instance and the application jobs SQLite database."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._db_path: str = self._settings.jobs_db_path
        self._init_db()

    # ── Database bootstrap ────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create jobs table + indexes on first run."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id        TEXT PRIMARY KEY,
                    fingerprint   TEXT NOT NULL,
                    topic         TEXT NOT NULL,
                    publish_at    TEXT,
                    tags          TEXT NOT NULL DEFAULT '[]',
                    style_hint    TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT 'pending',
                    current_stage TEXT NOT NULL DEFAULT '',
                    medium_url    TEXT,
                    error         TEXT,
                    created_at    TEXT NOT NULL,
                    started_at    TEXT,
                    completed_at  TEXT,
                    retry_count   INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fingerprint ON jobs(fingerprint)"
                " WHERE status NOT IN ('cancelled','failed')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON jobs(status)")
            conn.commit()

    # ── Scheduler lifecycle ───────────────────────────────────────────────────

    @property
    def scheduler(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            raise RuntimeError("JobManager not started — call await start() first.")
        return self._scheduler

    async def start(self) -> None:
        """Initialise and start APScheduler."""
        settings = get_settings()
        self._scheduler = AsyncIOScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(
                    url=f"sqlite:///{settings.scheduler_db_path}"
                )
            },
            executors={"default": AsyncIOExecutor()},
            job_defaults={"coalesce": True, "max_instances": 1},
        )
        self._scheduler.start()
        logger.info("scheduler_started", db=settings.scheduler_db_path)

    async def stop(self) -> None:
        """Gracefully shut down APScheduler."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")

    # ── Idempotency ───────────────────────────────────────────────────────────

    def find_by_fingerprint(self, fingerprint: str) -> Optional[str]:
        """Return existing job_id if an active job with this fingerprint exists."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT job_id FROM jobs "
                "WHERE fingerprint = ? AND status NOT IN ('cancelled','failed')",
                (fingerprint,),
            ).fetchone()
        return row[0] if row else None

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """Return the full job record as a dict, or None if not found."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_job(
        self,
        job_id: str,
        fingerprint: str,
        topic: str,
        tags: list[str],
        style_hint: str,
        publish_at: Optional[str] = None,
    ) -> None:
        """Insert a new pending job record."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, fingerprint, topic, publish_at, tags, style_hint,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    job_id,
                    fingerprint,
                    topic,
                    publish_at,
                    json.dumps(tags),
                    style_hint,
                    now,
                ),
            )
            conn.commit()

    def update_stage(self, job_id: str, stage: str) -> None:
        """
        Advance current_stage. Sets status='running' and records started_at
        on the very first stage transition.
        """
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT started_at FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row and row[0] is None:
                conn.execute(
                    "UPDATE jobs SET current_stage=?, status='running', started_at=? "
                    "WHERE job_id=?",
                    (stage, now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET current_stage=? WHERE job_id=?",
                    (stage, job_id),
                )
            conn.commit()

    def mark_done(self, job_id: str, medium_url: str) -> None:
        """Mark job successfully completed."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='done', medium_url=?, completed_at=?, "
                "current_stage='' WHERE job_id=?",
                (medium_url, now, job_id),
            )
            conn.commit()

    def mark_failed(self, job_id: str, error: str, stage: str) -> None:
        """Mark job failed with error message and failing stage."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, current_stage=?, "
                "completed_at=? WHERE job_id=?",
                (error, stage, now, job_id),
            )
            conn.commit()

    def mark_cancelled(self, job_id: str) -> None:
        """Mark job cancelled."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='cancelled', completed_at=? WHERE job_id=?",
                (now, job_id),
            )
            conn.commit()

    def increment_retry(self, job_id: str) -> int:
        """Increment retry_count. Returns the new count."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE jobs SET retry_count = retry_count + 1 WHERE job_id=?",
                (job_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT retry_count FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return row[0] if row else 0

    def list_active_jobs(self) -> list[dict[str, Any]]:
        """Return all pending/running jobs ordered by creation time."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','running') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Job control ───────────────────────────────────────────────────────────

    def cancel_job(self, job_id: str) -> None:
        """
        Cancel a pending job.
        Raises JobNotFoundError if job_id unknown.
        Raises JobCancellationError if status is not 'pending'.
        """
        record = self.get_job(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        if record["status"] == "running":
            raise JobCancellationError(job_id, "job is currently running")
        if record["status"] != "pending":
            raise JobCancellationError(
                job_id, f"job has status '{record['status']}' — cannot cancel"
            )
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass  # Job may have already fired or not exist in scheduler store
        self.mark_cancelled(job_id)
        logger.info("job_cancelled", job_id=job_id)

    def schedule_pipeline(
        self,
        job_id: str,
        topic: str,
        tags: list[str],
        style_hint: str,
        publish_at: datetime,
        orchestrator_fn: AsyncCallable,
    ) -> None:
        """Register a DateTrigger APScheduler job."""
        self.scheduler.add_job(
            orchestrator_fn,
            trigger="date",
            run_date=publish_at,
            id=job_id,
            kwargs={
                "job_id": job_id,
                "topic": topic,
                "tags": tags,
                "style_hint": style_hint,
            },
            misfire_grace_time=SCHEDULER_MISFIRE_GRACE,
            coalesce=True,
            max_instances=1,
        )
        logger.info(
            "job_scheduled",
            job_id=job_id,
            topic=topic[:60],
            publish_at=publish_at.isoformat(),
        )

    def reschedule_retry(
        self,
        job_id: str,
        topic: str,
        tags: list[str],
        style_hint: str,
        orchestrator_fn: AsyncCallable,
    ) -> bool:
        """
        Schedule a retry run SCHEDULER_RETRY_DELAY_SECONDS from now.
        Returns False (and marks job failed) if max retries exhausted.
        """
        retry_count = self.increment_retry(job_id)
        if retry_count > SCHEDULER_MAX_RETRIES:
            logger.warning("max_retries_exhausted", job_id=job_id, retries=retry_count)
            return False
        run_date = datetime.now(timezone.utc) + timedelta(
            seconds=SCHEDULER_RETRY_DELAY_SECONDS
        )
        self.schedule_pipeline(
            job_id=job_id,
            topic=topic,
            tags=tags,
            style_hint=style_hint,
            publish_at=run_date,
            orchestrator_fn=orchestrator_fn,
        )
        logger.info(
            "job_retry_scheduled",
            job_id=job_id,
            retry=retry_count,
            run_date=run_date.isoformat(),
        )
        return True


# ── Module-level singleton ────────────────────────────────────────────────────

_job_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Return the process-level JobManager singleton."""
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager
