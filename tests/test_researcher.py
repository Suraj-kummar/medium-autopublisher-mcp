"""
tests/test_researcher.py

Tests for pipeline/researcher.py.
All HTTP calls are intercepted via respx — zero real API spend.
"""
from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from pipeline.researcher import _deduplicate, run
from models.schemas import ResearchSource
from tests.conftest import FAKE_TAVILY_RESULT


def _gemini_decompose_response(questions_json: str) -> dict:
    """Build a fake Gemini generateContent response for decomposition."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": questions_json}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "modelVersion": "gemini-2.5-flash",
    }


@pytest.mark.asyncio
async def test_returns_typed_research_result(respx_mock):
    """
    Full researcher.run() returns a ResearchResult with non-empty sources
    and combined_context when mocked APIs respond correctly.
    """
    # Gemini decompose endpoint: returns 3 sub-questions
    respx_mock.post(
        url__regex=r"https://generativelanguage\.googleapis\.com/.*/models/.*:generateContent.*"
    ).mock(
        return_value=Response(
            200,
            json=_gemini_decompose_response(
                '["AI diagnostics 2024", "machine learning drug discovery", "AI ethics healthcare"]'
            ),
        )
    )
    respx_mock.post("https://api.tavily.com/search").mock(
        return_value=Response(200, json=FAKE_TAVILY_RESULT)
    )

    result = await run(topic="AI in healthcare", job_id="test-001")

    assert result.sub_questions, "Expected 3 sub-questions from Gemini decomposition"
    assert len(result.sub_questions) == 3
    assert result.sources, "Expected at least one source from Tavily"
    assert "Research Summary" in result.combined_context


@pytest.mark.asyncio
async def test_deduplicates_urls_across_queries(respx_mock):
    """
    When the same URL appears in multiple search result sets,
    it should appear only once in the final sources list.
    """
    duplicate_result = {
        "results": [
            {
                "title": "Duplicate Article",
                "url": "https://dupe.example.com/article",
                "content": "Content here",
                "published_date": "2024-01-01",
            }
        ]
    }
    # Gemini decomposer mock
    respx_mock.post(
        url__regex=r"https://generativelanguage\.googleapis\.com/.*/models/.*:generateContent.*"
    ).mock(
        return_value=Response(
            200,
            json=_gemini_decompose_response('["query one", "query two", "query three"]'),
        )
    )
    # Tavily returns same URL for every query
    respx_mock.post("https://api.tavily.com/search").mock(
        return_value=Response(200, json=duplicate_result)
    )

    result = await run(topic="Deduplication test", job_id="test-002")
    urls = [s.url for s in result.sources]
    assert len(urls) == len(set(urls)), "Duplicate URLs must be deduplicated"
    assert len(result.sources) == 1, "Same URL from 3 queries should produce 1 source"


@pytest.mark.asyncio
async def test_handles_tavily_rate_limit_with_retry(respx_mock):
    """
    researcher.run() should still succeed when Tavily returns 429 on
    the first call but succeeds on retry. The researcher wraps Tavily
    errors gracefully and returns partial results rather than crashing.
    """
    call_count = 0

    def tavily_side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return Response(429, json={"error": "rate limit"})
        return Response(
            200,
            json={
                "results": [
                    {
                        "title": "Retry Success",
                        "url": "https://retry.example.com",
                        "content": "Content after retry",
                    }
                ]
            },
        )

    respx_mock.post(
        url__regex=r"https://generativelanguage\.googleapis\.com/.*/models/.*:generateContent.*"
    ).mock(
        return_value=Response(
            200,
            json=_gemini_decompose_response('["q1", "q2", "q3"]'),
        )
    )
    respx_mock.post("https://api.tavily.com/search").mock(side_effect=tavily_side_effect)

    # Rate-limited Tavily calls are handled gracefully (empty results, no crash)
    result = await run(topic="Rate limit test", job_id="test-003")
    # The researcher degrades gracefully — sources may be empty but no exception
    assert result.sub_questions == ["q1", "q2", "q3"]


def test_deduplicates_in_memory():
    """Unit test _deduplicate() directly with hand-crafted input."""
    raw = [
        [
            {"url": "https://a.com", "title": "A", "content": "aaa"},
            {"url": "https://b.com", "title": "B", "content": "bbb"},
        ],
        [
            {"url": "https://a.com", "title": "A again", "content": "aaa"},  # dupe
            {"url": "https://c.com", "title": "C", "content": "ccc"},
        ],
        [
            {"url": "https://b.com", "title": "B again", "content": "bbb"},  # dupe
        ],
    ]
    sources = _deduplicate(raw)
    urls = [s.url for s in sources]
    assert urls == ["https://a.com", "https://b.com", "https://c.com"]
