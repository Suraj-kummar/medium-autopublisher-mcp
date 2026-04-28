"""Typed exception hierarchy for the Medium Auto-Publisher MCP Server."""
from __future__ import annotations


class MediumMCPError(Exception):
    """Base exception for all application errors."""


class StageError(MediumMCPError):
    """Wraps an exception raised during a named pipeline stage."""

    def __init__(self, stage: str, original: Exception) -> None:
        self.stage = stage
        self.original = original
        super().__init__(f"Stage '{stage}' failed: {original}")


class ResearchError(StageError):
    """Raised when the research stage fails."""


class WriterError(StageError):
    """Raised when the writer stage fails."""


class IllustratorError(StageError):
    """Raised when the DALL-E image generation stage fails."""


class PublisherError(StageError):
    """Raised when the Medium publish stage fails."""


class ValidationError(MediumMCPError):
    """Raised when the quality gate score is below the threshold."""

    def __init__(self, score: float, issues: list[str]) -> None:
        self.score = score
        self.issues = issues
        super().__init__(
            f"Quality score {score:.2f} below threshold. Issues: {'; '.join(issues)}"
        )


class IdempotencyError(MediumMCPError):
    """Raised when a duplicate job is detected (same fingerprint)."""

    def __init__(self, existing_job_id: str) -> None:
        self.existing_job_id = existing_job_id
        super().__init__(f"Duplicate job detected. Existing job_id: {existing_job_id}")


class JobNotFoundError(MediumMCPError):
    """Raised when a job_id lookup finds no record."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Job not found: {job_id}")


class JobCancellationError(MediumMCPError):
    """Raised when a job cannot be cancelled (e.g., already running)."""

    def __init__(self, job_id: str, reason: str) -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(f"Cannot cancel job {job_id}: {reason}")
