"""
Pydantic v2 models for MCP tool inputs/outputs and internal pipeline data.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ─── Pipeline Internal Models ─────────────────────────────────────────────────

class ResearchSource(BaseModel):
    title: str
    url: str
    content: str
    published_date: Optional[str] = None


class ResearchResult(BaseModel):
    sub_questions: list[str]
    sources: list[ResearchSource]
    combined_context: str


class ArticleDraft(BaseModel):
    title: str
    subtitle: str
    body: str
    image_prompt: str
    tags: list[str]


class QualityReport(BaseModel):
    score: float
    issues: list[str]
    checks: dict[str, float]   # check_name → individual score


# ─── MCP Tool Input Models ────────────────────────────────────────────────────

class ScheduleArticleInput(BaseModel):
    topic: str = Field(..., min_length=3, max_length=200)
    publish_at: datetime
    tags: list[str] = Field(..., min_length=1, max_length=5)
    style_hint: str = Field("", description="technical | opinion | beginner")

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if len(tag) > 25:
                raise ValueError(f"Tag '{tag}' exceeds 25 characters")
        return v


class PublishNowInput(BaseModel):
    topic: str = Field(..., min_length=3, max_length=200)
    tags: list[str] = Field(..., min_length=1, max_length=5)
    style_hint: str = Field("")

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if len(tag) > 25:
                raise ValueError(f"Tag '{tag}' exceeds 25 characters")
        return v


class CancelJobInput(BaseModel):
    job_id: str


class GetStatusInput(BaseModel):
    job_id: str


class PreviewArticleInput(BaseModel):
    topic: str = Field(..., min_length=3, max_length=200)
    style_hint: str = Field("")


# ─── MCP Tool Output Models ───────────────────────────────────────────────────

class ScheduleArticleOutput(BaseModel):
    job_id: str
    fingerprint: str
    scheduled_at: str
    is_duplicate: bool
    message: str


class PublishNowOutput(BaseModel):
    job_id: str
    message: str


class ScheduledJobInfo(BaseModel):
    job_id: str
    topic: str
    scheduled_at: str
    status: str
    fingerprint: str


class ListScheduledOutput(BaseModel):
    jobs: list[ScheduledJobInfo]
    count: int


class CancelJobOutput(BaseModel):
    job_id: str
    status: Literal["cancelled"]
    message: str


class GetStatusOutput(BaseModel):
    job_id: str
    status: Literal["pending", "running", "done", "failed", "cancelled"]
    current_stage: str
    medium_url: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None


class PreviewArticleOutput(BaseModel):
    title: str
    subtitle: str
    article_markdown: str
    word_count: int
    suggested_tags: list[str]
    quality_score: float
    quality_notes: list[str]


# ─── Drafts Viewer Output Models ──────────────────────────────────────────────

class DraftInfo(BaseModel):
    slug: str
    title: str
    tags: str
    has_image: bool
    created_at: str


class ListDraftsOutput(BaseModel):
    drafts: list[DraftInfo]
    count: int


class ReadDraftOutput(BaseModel):
    slug: str
    article_markdown: str
    word_count: int
    has_image: bool
    image_path: Optional[str] = None


# ─── Content Calendar Output Models ───────────────────────────────────────────

class CalendarEntry(BaseModel):
    topic: str
    tags: list[str]
    suggested_date: str
    style_hint: str
    rationale: str


class GenerateCalendarOutput(BaseModel):
    niche: str
    cadence: str
    entries: list[CalendarEntry]
    count: int


class BatchScheduleResult(BaseModel):
    topic: str
    job_id: Optional[str] = None
    status: str                     # "scheduled", "duplicate", "failed"
    message: str


class BatchScheduleOutput(BaseModel):
    scheduled: list[BatchScheduleResult]
    total: int
    succeeded: int
    failed: int

