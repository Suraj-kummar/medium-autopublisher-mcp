"""
tests/test_calendar.py

Tests for the content calendar generator and batch scheduling.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from pipeline import calendar as calendar_gen
from models.schemas import (
    CalendarEntry,
    GenerateCalendarOutput,
    BatchScheduleResult,
    BatchScheduleOutput,
)


# ── Fake calendar data ────────────────────────────────────────────────────────

FAKE_CALENDAR_JSON = [
    {
        "topic": "Why Retrieval-Augmented Generation Is Replacing Fine-Tuning",
        "tags": ["AI", "RAG", "LLM", "Machine Learning", "Engineering"],
        "style_hint": "technical",
        "rationale": "RAG vs fine-tuning is the hottest debate in AI engineering right now.",
    },
    {
        "topic": "The Hidden Cost of AI Hallucinations in Production Systems",
        "tags": ["AI", "Production", "Reliability", "Engineering", "MLOps"],
        "style_hint": "case-study",
        "rationale": "Every company deploying LLMs is grappling with hallucination costs.",
    },
    {
        "topic": "How Vector Databases Are Quietly Becoming Infrastructure",
        "tags": ["Databases", "AI", "Infrastructure", "Pinecone", "Tech"],
        "style_hint": "technical",
        "rationale": "Vector DBs moved from niche to essential in under 2 years.",
    },
    {
        "topic": "AI Agents Will Replace Your SaaS Stack by 2027",
        "tags": ["AI", "SaaS", "Automation", "Future", "Startups"],
        "style_hint": "opinion",
        "rationale": "Bold prediction backed by current agent capabilities.",
    },
]


# ── Tests for calendar generation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_calendar_returns_correct_count(mock_gemini_calendar):
    """generate_calendar() should return exactly num_articles entries."""
    result = await calendar_gen.generate_calendar(
        niche="AI in production",
        num_articles=4,
        cadence="weekly",
    )

    assert len(result) == 4
    assert all("topic" in entry for entry in result)
    assert all("tags" in entry for entry in result)
    assert all("suggested_date" in entry for entry in result)
    assert all("rationale" in entry for entry in result)


@pytest.mark.asyncio
async def test_generate_calendar_weekly_dates_are_7_days_apart(mock_gemini_calendar):
    """Weekly cadence should produce dates 7 days apart."""
    result = await calendar_gen.generate_calendar(
        niche="AI in production",
        num_articles=4,
        cadence="weekly",
    )

    dates = [datetime.fromisoformat(e["suggested_date"]) for e in result]
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        assert delta == 7, f"Expected 7 day gap, got {delta}"


@pytest.mark.asyncio
async def test_generate_calendar_daily_cadence(mock_gemini_calendar):
    """Daily cadence should produce dates 1 day apart."""
    result = await calendar_gen.generate_calendar(
        niche="AI in production",
        num_articles=4,
        cadence="daily",
    )

    dates = [datetime.fromisoformat(e["suggested_date"]) for e in result]
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        assert delta == 1, f"Expected 1 day gap, got {delta}"


@pytest.mark.asyncio
async def test_generate_calendar_biweekly_cadence(mock_gemini_calendar):
    """Biweekly cadence should produce dates 14 days apart."""
    result = await calendar_gen.generate_calendar(
        niche="AI in production",
        num_articles=3,
        cadence="biweekly",
    )

    dates = [datetime.fromisoformat(e["suggested_date"]) for e in result]
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        assert delta == 14, f"Expected 14 day gap, got {delta}"


@pytest.mark.asyncio
async def test_generate_calendar_caps_at_num_articles(mock_gemini_calendar):
    """Even if Gemini returns more entries, we cap at num_articles."""
    result = await calendar_gen.generate_calendar(
        niche="AI in production",
        num_articles=2,  # Only want 2, Gemini returns 4
        cadence="weekly",
    )

    assert len(result) == 2


@pytest.mark.asyncio
async def test_generate_calendar_tags_capped_at_5(mock_gemini_calendar):
    """Each entry's tags should be capped at 5."""
    result = await calendar_gen.generate_calendar(
        niche="AI in production",
        num_articles=4,
        cadence="weekly",
    )

    for entry in result:
        assert len(entry["tags"]) <= 5


# ── Tests for schema models ──────────────────────────────────────────────────

def test_calendar_entry_model():
    """CalendarEntry should validate correctly."""
    entry = CalendarEntry(
        topic="Test Topic",
        tags=["AI", "Tech"],
        suggested_date="2026-05-01T09:00:00+00:00",
        style_hint="technical",
        rationale="Testing the model",
    )
    assert entry.topic == "Test Topic"
    assert len(entry.tags) == 2


def test_batch_schedule_output_model():
    """BatchScheduleOutput should track success/failure counts."""
    output = BatchScheduleOutput(
        scheduled=[
            BatchScheduleResult(
                topic="Topic 1", job_id="abc", status="scheduled", message="OK"
            ),
            BatchScheduleResult(
                topic="Topic 2", job_id=None, status="failed", message="Error"
            ),
        ],
        total=2,
        succeeded=1,
        failed=1,
    )
    assert output.total == 2
    assert output.succeeded == 1
    assert output.failed == 1


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_gemini_calendar(respx_mock: respx.MockRouter) -> respx.MockRouter:
    """Intercept Google Gemini API for calendar generation."""
    gemini_response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(FAKE_CALENDAR_JSON)}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "modelVersion": "gemini-2.5-flash",
    }
    respx_mock.post(
        url__regex=r"https://generativelanguage\.googleapis\.com/.*/models/.*:generateContent.*"
    ).mock(return_value=Response(200, json=gemini_response))
    return respx_mock
