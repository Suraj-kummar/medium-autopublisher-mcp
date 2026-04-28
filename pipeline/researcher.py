"""
pipeline/researcher.py — Stage 1: Research

Decomposes the topic into 3 targeted sub-questions via Gemini,
then runs all 3 Tavily searches concurrently. Deduplicates by URL
and returns a structured ResearchResult with combined context.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from google import genai
import structlog
from tavily import AsyncTavilyClient

from config import (
    GEMINI_TIMEOUT,
    DECOMPOSER_MODEL,
    RESEARCHER_DECOMPOSE_PROMPT,
    TAVILY_TIMEOUT,
    get_settings,
)
from models.errors import ResearchError
from models.schemas import ResearchResult, ResearchSource

logger = structlog.get_logger()


async def run(topic: str, job_id: str = "preview") -> ResearchResult:
    """
    Stage 1 entry point.

    Args:
        topic:  The article topic.
        job_id: Used only for structured logging.

    Returns:
        ResearchResult with deduplicated sources and combined context string.

    Raises:
        ResearchError: wraps any underlying exception so the orchestrator
                       can tag the failure with the correct stage.
    """
    bound = logger.bind(job_id=job_id, stage="research", topic=topic[:60])
    bound.info("research_start")

    try:
        settings = get_settings()
        sub_questions = await _decompose_topic(
            topic=topic,
            api_key=settings.gemini_api_key,
            log=bound,
        )
        bound.info("research_decomposed", sub_questions=sub_questions)

        raw_results = await _parallel_search(
            sub_questions=sub_questions,
            api_key=settings.tavily_api_key,
            log=bound,
        )

        sources = _deduplicate(raw_results)
        combined_context = _build_context(sub_questions, sources)

        bound.info("research_complete", source_count=len(sources))
        return ResearchResult(
            sub_questions=sub_questions,
            sources=sources,
            combined_context=combined_context,
        )

    except ResearchError:
        raise
    except Exception as exc:
        bound.error("research_failed", error=str(exc))
        raise ResearchError(stage="research", original=exc) from exc


async def _decompose_topic(
    topic: str,
    api_key: str,
    log: structlog.BoundLogger,
) -> list[str]:
    """
    Ask Gemini to produce exactly 3 targeted search queries for the topic.
    Uses gemini-2.5-flash — free tier, fast.
    """
    client = genai.Client(api_key=api_key)
    prompt = RESEARCHER_DECOMPOSE_PROMPT.format(topic=topic)

    response = await asyncio.wait_for(
        asyncio.to_thread(
            client.models.generate_content,
            model=DECOMPOSER_MODEL,
            contents=prompt,
        ),
        timeout=GEMINI_TIMEOUT,
    )

    raw = response.text.strip()

    # Strip markdown code fences if the model wraps them
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        questions: list[str] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchError(
            stage="research",
            original=ValueError(
                f"Claude returned non-JSON for decomposition: {raw[:200]}"
            ),
        ) from exc

    if not isinstance(questions, list) or len(questions) != 3:
        raise ResearchError(
            stage="research",
            original=ValueError(
                f"Expected JSON array of 3 strings, got: {questions!r}"
            ),
        )

    return [str(q) for q in questions]


async def _parallel_search(
    sub_questions: list[str],
    api_key: str,
    log: structlog.BoundLogger,
) -> list[list[dict[str, Any]]]:
    """Run all 3 Tavily searches concurrently. Returns raw result lists."""
    client = AsyncTavilyClient(api_key=api_key)

    async def _search(query: str) -> list[dict[str, Any]]:
        try:
            resp = await asyncio.wait_for(
                client.search(
                    query=query,
                    search_depth="advanced",
                    max_results=5,
                    include_answer=True,
                ),
                timeout=TAVILY_TIMEOUT,
            )
            return resp.get("results", [])
        except Exception as exc:
            log.warning("tavily_search_error", query=query[:60], error=str(exc))
            return []

    return list(await asyncio.gather(*[_search(q) for q in sub_questions]))


def _deduplicate(raw_results: list[list[dict[str, Any]]]) -> list[ResearchSource]:
    """
    Flatten all search result lists and deduplicate by URL.
    Preserves insertion order (first occurrence wins).
    """
    seen_urls: set[str] = set()
    sources: list[ResearchSource] = []

    for result_list in raw_results:
        for item in result_list:
            url: str = item.get("url", "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(
                ResearchSource(
                    title=item.get("title", "Untitled"),
                    url=url,
                    content=item.get("content", ""),
                    published_date=item.get("published_date"),
                )
            )

    return sources


def _build_context(
    sub_questions: list[str],
    sources: list[ResearchSource],
) -> str:
    """
    Produce a single text block suitable for the writer prompt.
    Groups snippets by source URL for easy scanning.
    """
    lines: list[str] = ["## Research Summary\n"]
    for i, q in enumerate(sub_questions, 1):
        lines.append(f"### Sub-question {i}: {q}\n")

    lines.append("\n## Sources\n")
    for src in sources:
        date_part = f" ({src.published_date})" if src.published_date else ""
        lines.append(f"**{src.title}**{date_part}")
        lines.append(f"URL: {src.url}")
        lines.append(src.content[:800])   # cap snippet length
        lines.append("")

    return "\n".join(lines)
