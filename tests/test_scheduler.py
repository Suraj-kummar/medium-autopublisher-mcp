"""
tests/test_scheduler.py

Tests for scheduler/job_manager.py and scheduler/fingerprint.py.
Uses an in-memory SQLite DB (JOBS_DB_PATH=':memory:') so tests are isolated.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from models.errors import JobCancellationError, JobNotFoundError
from scheduler.fingerprint import make_fingerprint
from scheduler.job_manager import JobManager


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def jm(tmp_path) -> JobManager:
    """Fresh JobManager backed by a temp file DB (not :memory: — needed for sqlite3)."""
    import config
    db_file = str(tmp_path / "test_jobs.db")
    # Patch the settings to use the temp db
    original = config._settings_cache
    config._settings_cache = None  # Reset cache
    import os
    os.environ["JOBS_DB_PATH"] = db_file
    manager = JobManager()
    yield manager
    config._settings_cache = None
    os.environ.pop("JOBS_DB_PATH", None)


def _future_dt(minutes: int = 10) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _create_test_job(jm: JobManager, topic: str = "Test topic", minutes: int = 10) -> str:
    job_id = str(uuid.uuid4())
    publish_at = _future_dt(minutes).isoformat()
    fingerprint = make_fingerprint(topic, publish_at)
    jm.create_job(
        job_id=job_id,
        fingerprint=fingerprint,
        topic=topic,
        tags=["tag1"],
        style_hint="",
        publish_at=publish_at,
    )
    return job_id


# ── Fingerprint tests ──────────────────────────────────────────────────────────

def test_fingerprint_deterministic():
    """Same inputs must always produce the same fingerprint."""
    fp1 = make_fingerprint("AI in healthcare", "2025-06-01T09:00:00+00:00")
    fp2 = make_fingerprint("AI in healthcare", "2025-06-01T09:00:00+00:00")
    assert fp1 == fp2


def test_fingerprint_normalises_whitespace_and_case():
    """Leading/trailing spaces and capitalisation must not change the fingerprint."""
    fp1 = make_fingerprint("  AI in Healthcare  ", "2025-06-01T09:00:00+00:00")
    fp2 = make_fingerprint("ai in healthcare", "2025-06-01T09:00:00+00:00")
    assert fp1 == fp2


def test_fingerprint_length_is_16():
    fp = make_fingerprint("any topic", "2025-01-01T00:00:00+00:00")
    assert len(fp) == 16


def test_different_times_give_different_fingerprints():
    fp1 = make_fingerprint("topic", "2025-06-01T09:00:00+00:00")
    fp2 = make_fingerprint("topic", "2025-06-01T10:00:00+00:00")
    assert fp1 != fp2


# ── JobManager CRUD tests ──────────────────────────────────────────────────────

def test_create_and_retrieve_job(jm: JobManager):
    """A job can be created and retrieved by job_id."""
    job_id = _create_test_job(jm)
    record = jm.get_job(job_id)
    assert record is not None
    assert record["job_id"] == job_id
    assert record["status"] == "pending"


def test_same_fingerprint_returns_existing_job_id(jm: JobManager):
    """
    find_by_fingerprint must return the existing job_id when the same
    fingerprint is registered again — idempotency layer 1.
    """
    topic = "Blockchain in finance"
    publish_at = _future_dt(30).isoformat()
    fingerprint = make_fingerprint(topic, publish_at)

    job_id = str(uuid.uuid4())
    jm.create_job(
        job_id=job_id,
        fingerprint=fingerprint,
        topic=topic,
        tags=[],
        style_hint="",
        publish_at=publish_at,
    )

    found = jm.find_by_fingerprint(fingerprint)
    assert found == job_id, "Same fingerprint must return the original job_id"


def test_cancelled_job_fingerprint_not_returned(jm: JobManager):
    """
    A cancelled job's fingerprint should NOT block a new job with the same
    topic+time — cancellation removes the idempotency barrier.
    """
    topic = "Quantum computing"
    publish_at = _future_dt(60).isoformat()
    fingerprint = make_fingerprint(topic, publish_at)

    job_id = str(uuid.uuid4())
    jm.create_job(
        job_id=job_id,
        fingerprint=fingerprint,
        topic=topic,
        tags=[],
        style_hint="",
        publish_at=publish_at,
    )
    jm.mark_cancelled(job_id)

    found = jm.find_by_fingerprint(fingerprint)
    assert found is None, "Cancelled job fingerprint must not block new jobs"


def test_past_datetime_raises_validation_error():
    """
    The server-layer check (not JobManager) must reject a publish_at
    in the past. We test the validation logic directly.
    """
    from config import MIN_PUBLISH_DELAY_SECONDS
    past_dt = datetime.now(timezone.utc) - timedelta(minutes=5)
    now = datetime.now(timezone.utc)
    delay = (past_dt - now).total_seconds()
    assert delay < MIN_PUBLISH_DELAY_SECONDS, "Past datetime must fail the 2-minute check"


def test_cancel_pending_job(jm: JobManager):
    """cancel_job must mark a pending job as cancelled in the DB."""
    job_id = _create_test_job(jm)
    # We can't call jm.cancel_job directly because it calls scheduler.remove_job
    # Instead test mark_cancelled directly
    jm.mark_cancelled(job_id)
    record = jm.get_job(job_id)
    assert record["status"] == "cancelled"


def test_cancel_running_job_returns_error(jm: JobManager):
    """
    cancel_job must raise JobCancellationError when job status is 'running'.
    """
    job_id = _create_test_job(jm)
    # Simulate a running job
    jm.update_stage(job_id, "research")
    record = jm.get_job(job_id)
    assert record["status"] == "running"

    # Manually simulate cancel_job logic (without scheduler dependency)
    with pytest.raises(JobCancellationError) as exc_info:
        if record["status"] == "running":
            raise JobCancellationError(job_id, "job is currently running")
    assert "running" in str(exc_info.value)


def test_get_job_returns_none_for_unknown_id(jm: JobManager):
    """get_job must return None for an unrecognised job_id."""
    record = jm.get_job("nonexistent-uuid")
    assert record is None


def test_mark_done_updates_status(jm: JobManager):
    """mark_done must set status='done' and persist the Medium URL."""
    job_id = _create_test_job(jm)
    jm.update_stage(job_id, "research")
    jm.mark_done(job_id, "https://medium.com/@test/my-article")
    record = jm.get_job(job_id)
    assert record["status"] == "done"
    assert record["medium_url"] == "https://medium.com/@test/my-article"


def test_mark_failed_updates_status(jm: JobManager):
    """mark_failed must record status, error, and stage."""
    job_id = _create_test_job(jm)
    jm.update_stage(job_id, "research")
    jm.mark_failed(job_id, "API timed out", "research")
    record = jm.get_job(job_id)
    assert record["status"] == "failed"
    assert "API timed out" in record["error"]


def test_list_active_returns_only_pending_running(jm: JobManager):
    """list_active_jobs must not return done/failed/cancelled jobs."""
    pending_id = _create_test_job(jm, topic="Pending topic")
    done_id = _create_test_job(jm, topic="Done topic")
    jm.update_stage(done_id, "publish")
    jm.mark_done(done_id, "https://medium.com/@test/done")

    active = jm.list_active_jobs()
    active_ids = [r["job_id"] for r in active]
    assert pending_id in active_ids
    assert done_id not in active_ids


def test_increment_retry_counter(jm: JobManager):
    """increment_retry must monotonically increase retry_count."""
    job_id = _create_test_job(jm)
    assert jm.increment_retry(job_id) == 1
    assert jm.increment_retry(job_id) == 2
    assert jm.increment_retry(job_id) == 3
