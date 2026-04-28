"""
tests/test_writer.py

Tests for pipeline/writer.py.
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from pipeline.writer import _parse_article_json, run
from models.errors import WriterError
from models.schemas import ResearchResult, ResearchSource
from tests.conftest import FAKE_ARTICLE_JSON, FAKE_ARTICLE_JSON_STR


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_research() -> ResearchResult:
    return ResearchResult(
        sub_questions=["q1", "q2", "q3"],
        sources=[
            ResearchSource(
                title="Test Source",
                url="https://test-source.example.com",
                content="Relevant content for testing.",
            )
        ],
        combined_context="## Research Summary\n\nSome research context here.",
    )


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_parses_valid_json_from_claude_response():
    """_parse_article_json must return a valid ArticleDraft from clean JSON."""
    draft = _parse_article_json(FAKE_ARTICLE_JSON_STR)
    assert draft.title == FAKE_ARTICLE_JSON["title"]
    assert draft.subtitle == FAKE_ARTICLE_JSON["subtitle"]
    assert len(draft.tags) == 5
    assert draft.image_prompt


def test_strips_json_code_fences_before_parsing():
    """Writer must handle Claude wrapping JSON in ```json ... ``` fences."""
    fenced = f"```json\n{FAKE_ARTICLE_JSON_STR}\n```"
    draft = _parse_article_json(fenced)
    assert draft.title == FAKE_ARTICLE_JSON["title"]


def test_strips_plain_code_fences_before_parsing():
    """Writer must also handle ``` ... ``` without the 'json' specifier."""
    fenced = f"```\n{FAKE_ARTICLE_JSON_STR}\n```"
    draft = _parse_article_json(fenced)
    assert draft.title == FAKE_ARTICLE_JSON["title"]


def test_raises_writer_error_on_malformed_json():
    """_parse_article_json must raise WriterError on broken JSON."""
    with pytest.raises(WriterError) as exc_info:
        _parse_article_json("this is not json at all {{{{")
    assert exc_info.value.stage == "writing"


def test_raises_writer_error_on_missing_fields():
    """_parse_article_json must raise WriterError when required fields are absent."""
    incomplete = json.dumps({"title": "Only title present"})
    with pytest.raises(WriterError) as exc_info:
        _parse_article_json(incomplete)
    assert "Missing required fields" in str(exc_info.value)


def test_raises_writer_error_on_non_object_json():
    """_parse_article_json must raise WriterError when JSON is an array, not an object."""
    with pytest.raises(WriterError):
        _parse_article_json('["just", "an", "array"]')


@pytest.mark.asyncio
async def test_parses_valid_json_from_claude_response_integration(
    mock_gemini,
):
    """Full writer.run() call returns a valid ArticleDraft using mocked Gemini."""
    research = _make_research()
    draft = await run(
        topic="AI in healthcare",
        research=research,
        style_hint="technical",
        job_id="test-writer-001",
    )
    assert draft.title
    assert draft.body
    assert len(draft.tags) > 0

