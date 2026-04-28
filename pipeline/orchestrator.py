"""
pipeline/orchestrator.py — 5-stage pipeline runner

Responsibilities:
- Persists current_stage to SQLite BEFORE each stage starts
  (allows crash-recovery inspection of where a job died).
- Wraps each stage in try/except → converts to typed StageError.
- On StageError: writes error to SQLite; raises so the caller
  (APScheduler job or publish_now background task) can decide
  whether to retry.
- On ValidationError: marks job failed immediately (no retry).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from models.errors import StageError, ValidationError
from pipeline import illustrator, publisher, researcher, validator, writer
from scheduler.job_manager import JobManager

logger = structlog.get_logger()


async def run(
    job_id: str,
    topic: str,
    tags: list[str],
    style_hint: str,
    job_manager: JobManager,
) -> str:
    """
    Execute the full 5-stage pipeline for a given job.

    Returns:
        The canonical Medium URL of the published article.

    Raises:
        StageError / ValidationError — caller is responsible for
        updating job status after catching these.
    """
    bound = logger.bind(job_id=job_id, topic=topic[:60])
    bound.info("pipeline_start")

    # ── Stage 1: Research ─────────────────────────────────────────────────────
    job_manager.update_stage(job_id, "research")
    bound.info("stage_start", stage="research")
    try:
        research = await researcher.run(topic=topic, job_id=job_id)
    except StageError as exc:
        _persist_failure(job_manager, job_id, exc)
        raise
    except Exception as exc:
        from models.errors import ResearchError
        wrapped = ResearchError(stage="research", original=exc)
        _persist_failure(job_manager, job_id, wrapped)
        raise wrapped from exc
    bound.info("stage_complete", stage="research")

    # ── Stage 2: Write ────────────────────────────────────────────────────────
    job_manager.update_stage(job_id, "writing")
    bound.info("stage_start", stage="writing")
    try:
        article = await writer.run(
            topic=topic,
            research=research,
            style_hint=style_hint,
            job_id=job_id,
            tags=tags,
        )
    except StageError as exc:
        _persist_failure(job_manager, job_id, exc)
        raise
    except Exception as exc:
        from models.errors import WriterError
        wrapped = WriterError(stage="writing", original=exc)
        _persist_failure(job_manager, job_id, wrapped)
        raise wrapped from exc
    bound.info("stage_complete", stage="writing")

    # ── Stage 2.5: Validate ───────────────────────────────────────────────────
    job_manager.update_stage(job_id, "validating")
    bound.info("stage_start", stage="validating")
    try:
        quality = validator.check(article=article, job_id=job_id)
    except ValidationError as exc:
        job_manager.mark_failed(
            job_id=job_id,
            error=f"Quality gate failed (score={exc.score:.2f}): {'; '.join(exc.issues)}",
            stage="validating",
        )
        raise
    bound.info("stage_complete", stage="validating", score=quality.score)

    # ── Stage 3: Illustrate ───────────────────────────────────────────────────
    job_manager.update_stage(job_id, "image")
    bound.info("stage_start", stage="image")
    try:
        img_bytes = await illustrator.run(
            image_prompt=article.image_prompt,
            job_id=job_id,
        )
    except StageError as exc:
        _persist_failure(job_manager, job_id, exc)
        raise
    except Exception as exc:
        from models.errors import IllustratorError
        wrapped = IllustratorError(stage="image", original=exc)
        _persist_failure(job_manager, job_id, wrapped)
        raise wrapped from exc
    bound.info("stage_complete", stage="image")

    # ── Stage 4+5: Publish ────────────────────────────────────────────────────
    job_manager.update_stage(job_id, "publish")
    bound.info("stage_start", stage="publish")
    try:
        medium_url = await publisher.run(
            article=article,
            image_bytes=img_bytes,
            tags=tags,
            job_id=job_id,
        )
    except StageError as exc:
        _persist_failure(job_manager, job_id, exc)
        raise
    except Exception as exc:
        from models.errors import PublisherError
        wrapped = PublisherError(stage="publish", original=exc)
        _persist_failure(job_manager, job_id, wrapped)
        raise wrapped from exc

    job_manager.mark_done(job_id=job_id, medium_url=medium_url)
    bound.info("pipeline_complete", medium_url=medium_url)
    return medium_url


def _persist_failure(job_manager: JobManager, job_id: str, exc: StageError) -> None:
    """Write failure details to SQLite."""
    job_manager.mark_failed(
        job_id=job_id,
        error=str(exc),
        stage=exc.stage,
    )
    logger.error(
        "stage_failed",
        job_id=job_id,
        stage=exc.stage,
        error=str(exc.original),
    )
