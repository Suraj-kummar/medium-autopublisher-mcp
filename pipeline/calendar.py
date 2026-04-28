"""
pipeline/calendar.py — Content Calendar Generator

Uses Gemini Flash (free tier) to generate a structured content
calendar for a given niche. Produces topic suggestions with tags,
dates, style hints, and rationale — ready to feed into batch_schedule.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
import structlog

from config import GEMINI_TIMEOUT, DECOMPOSER_MODEL, get_settings

logger = structlog.get_logger()

CALENDAR_SYSTEM_PROMPT = """\
You are a senior content strategist for Medium.
Given a niche/theme, generate a content calendar — a list of article topics
that would perform well on Medium.

RULES:
- Each topic must be specific and opinionated (not generic)
- Topics should build on each other, creating a cohesive content series
- Tags must be real Medium tags (5 per article)
- Style hints: "technical", "opinion", "beginner", "case-study", "listicle"
- Rationale: 1 sentence explaining why this topic will engage readers

OUTPUT FORMAT:
Respond with ONLY a valid JSON array. No preamble. No explanation.
Each element:
{
  "topic": "Specific, compelling article title (40-90 chars)",
  "tags": ["Tag1", "Tag2", "Tag3", "Tag4", "Tag5"],
  "style_hint": "technical|opinion|beginner|case-study|listicle",
  "rationale": "Why this topic will resonate with readers"
}"""


async def generate_calendar(
    niche: str,
    num_articles: int = 4,
    cadence: str = "weekly",
    style_hint: str = "",
) -> list[dict]:
    """
    Generate a content calendar for a given niche.

    Args:
        niche:        The content niche (e.g., "AI in healthcare", "startup growth").
        num_articles: Number of article ideas to generate (1-12).
        cadence:      Publishing cadence: "daily", "twice_weekly", "weekly", "biweekly".
        style_hint:   Optional overall style preference.

    Returns:
        List of calendar entry dicts with topic, tags, suggested_date, style_hint, rationale.
    """
    bound = logger.bind(stage="calendar", niche=niche[:60])
    bound.info("calendar_start", num_articles=num_articles, cadence=cadence)

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)

    style_note = f"\nPreferred overall style: {style_hint}" if style_hint else ""
    user_message = (
        f"NICHE: {niche}\n"
        f"NUMBER OF ARTICLES: {num_articles}\n"
        f"{style_note}\n\n"
        f"Generate exactly {num_articles} article topics as a JSON array."
    )

    config = types.GenerateContentConfig(
        system_instruction=CALENDAR_SYSTEM_PROMPT,
    )

    response = await asyncio.wait_for(
        asyncio.to_thread(
            client.models.generate_content,
            model=DECOMPOSER_MODEL,
            contents=user_message,
            config=config,
        ),
        timeout=GEMINI_TIMEOUT,
    )

    raw = response.text.strip()

    # Strip code fences if present
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw).strip()

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        bound.error("calendar_parse_failed", excerpt=raw[:200])
        raise ValueError(f"Gemini returned invalid JSON for calendar: {raw[:200]}") from exc

    if not isinstance(entries, list):
        raise ValueError(f"Expected JSON array, got {type(entries).__name__}")

    # Calculate publish dates based on cadence
    cadence_days = {
        "daily": 1,
        "twice_weekly": 3,   # ~2 per week: Mon, Thu
        "weekly": 7,
        "biweekly": 14,
    }
    interval = cadence_days.get(cadence, 7)
    base_date = datetime.now(timezone.utc) + timedelta(days=1)  # start tomorrow

    results = []
    for i, entry in enumerate(entries[:num_articles]):
        suggested_date = base_date + timedelta(days=i * interval)
        results.append({
            "topic": str(entry.get("topic", f"Topic {i+1}")),
            "tags": [str(t) for t in entry.get("tags", [])[:5]],
            "suggested_date": suggested_date.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "style_hint": str(entry.get("style_hint", style_hint or "balanced")),
            "rationale": str(entry.get("rationale", "")),
        })

    bound.info("calendar_complete", count=len(results))
    return results
