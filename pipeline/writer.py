"""
pipeline/writer.py — Stage 2: Write

2-call strategy to avoid JSON truncation:
  Call 1 → small metadata JSON (title, subtitle, tags, image_prompt)
  Call 2 → full article body as plain Markdown text

Includes automatic retry with exponential backoff for transient
503/429 errors, and falls back to gemini-1.5-flash if primary
model keeps failing.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
import structlog

from config import (
    GEMINI_TIMEOUT,
    WRITER_MODEL,
    WRITER_SYSTEM_PROMPT,
    WRITER_TEMPERATURE,
    get_settings,
)
from models.errors import WriterError
from models.schemas import ArticleDraft, ResearchResult

logger = structlog.get_logger()

# Models to try in order (primary → fallback)  
# Note: use models with confirmed quota availability
_MODEL_SEQUENCE: list[str] = [WRITER_MODEL, "gemini-flash-latest"]
_META_MODEL: str = WRITER_MODEL  # same model for JSON metadata
_RETRYABLE_CODES: set[int] = {429, 500, 502, 503}
_MAX_RETRIES: int = 4          # more retries since we respect Retry-After
_BACKOFF_BASE: float = 10.0    # conservative base backoff

_META_FIELDS = {"title", "subtitle", "tags", "image_prompt"}

# Simple system prompt for the metadata call — does NOT trigger full article writing
_META_SYSTEM = (
    "You are a JSON API. You output only valid JSON objects with no additional text, "
    "no markdown formatting, and no code fences. Follow instructions exactly."
)

# ─── Prompts ──────────────────────────────────────────────────────────────────

_META_PROMPT_TEMPLATE = """\
TOPIC: {topic}

Output ONLY a valid JSON object with exactly these 4 fields:
{{
  "title": "compelling SEO title, max 80 chars",
  "subtitle": "subtitle, max 100 chars",
  "image_prompt": "photographic scene, max 150 chars",
  "tags": ["tag1","tag2","tag3","tag4","tag5"]
}}
No preamble. No markdown. Just the raw JSON.
"""

_BODY_PROMPT_TEMPLATE = """\
TOPIC: {topic}
TITLE: {title}
SUBTITLE: {subtitle}
STYLE: {style}

RESEARCH DATA:
{combined_context}

SOURCES (cite inline as [anchor text](url)):
{source_lines}

Write the complete Medium article body now. Use this structure:
1. HOOK (2-3 sentences): surprising stat or vivid scenario
2. CONTEXT (1 paragraph): why this matters now
3. BODY (4-6 H2 sections, 150-300 words each):
   - At least one H3 sub-point per H2
   - Cite sources inline
   - Max one list per section
   - Bold key terms on first use
4. KEY TAKEAWAYS: H2 "What this means for you" with 3-4 actionable bullets
5. CLOSING: 1 paragraph + direct question for comments

Rules:
- ## for H2, ### for H3
- No raw HTML
- No horizontal rules (---)
- Blockquotes (>) for powerful stats only, max 2
- Target 1200-1800 words
- Output ONLY the markdown body text. No JSON. No preamble.
"""


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def run(
    topic: str,
    research: ResearchResult,
    style_hint: str,
    job_id: str = "preview",
    tags: list[str] | None = None,
) -> ArticleDraft:
    """
    Stage 2 entry point.

    Uses 2 Gemini calls:
    - Call 1: metadata JSON (title, subtitle, tags, image_prompt) — small, safe
    - Call 2: full article body as plain Markdown — no JSON wrapping

    Returns:
        ArticleDraft with all 5 fields populated.

    Raises:
        WriterError: on API failure or malformed response.
    """
    bound = logger.bind(job_id=job_id, stage="writing", topic=topic[:60])
    bound.info("writer_start")

    try:
        settings = get_settings()
        style = style_hint.strip() or "balanced, data-driven, engaging"
        tags_str = ", ".join(tags or []) or "(generate appropriate tags)"
        source_lines = "\n".join(
            f"- [{src.title}]({src.url})" for src in research.sources
        )

        # ── Call 1: Metadata ─────────────────────────────────────────────────
        bound.info("writer_call1_meta")
        meta_prompt = _META_PROMPT_TEMPLATE.format(
            topic=topic,
        )
        meta = await _gemini_call(
            api_key=settings.gemini_api_key,
            system=_META_SYSTEM,
            user=meta_prompt,
            max_tokens=2048,  # generous for metadata
            log=bound,
            call_name="meta",
            models=[_META_MODEL],  # always use stable model for JSON
        )
        meta_data = _parse_meta_json(meta)

        # ── Call 2: Body ──────────────────────────────────────────────────────
        bound.info("writer_call2_body")
        body_prompt = _BODY_PROMPT_TEMPLATE.format(
            topic=topic,
            title=meta_data["title"],
            subtitle=meta_data["subtitle"],
            style=style,
            combined_context=research.combined_context[:8000],
            source_lines=source_lines,
        )
        body_text = await _gemini_call(
            api_key=settings.gemini_api_key,
            system=WRITER_SYSTEM_PROMPT,
            user=body_prompt,
            max_tokens=8192,
            log=bound,
            call_name="body",
            models=_MODEL_SEQUENCE,  # try primary then fallback
        )

        draft = ArticleDraft(
            title=meta_data["title"],
            subtitle=meta_data["subtitle"],
            body=body_text.strip(),
            image_prompt=meta_data["image_prompt"],
            tags=[str(t) for t in meta_data["tags"][:5]],
        )
        bound.info(
            "writer_complete",
            title=draft.title[:60],
            word_count=len(draft.body.split()),
        )
        return draft

    except WriterError:
        raise
    except Exception as exc:
        bound.error("writer_failed", error=str(exc))
        raise WriterError(stage="writing", original=exc) from exc


# ─── Gemini Call Helper ────────────────────────────────────────────────────────

async def _gemini_call(
    api_key: str,
    system: str,
    user: str,
    max_tokens: int,
    log: structlog.BoundLogger,
    call_name: str,
    models: list[str] | None = None,
) -> str:
    """
    Make a single Gemini API call with retry + fallback model logic.
    Returns the raw text response.
    """
    if models is None:
        models = _MODEL_SEQUENCE

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        temperature=WRITER_TEMPERATURE,
    )

    last_exc: Exception | None = None

    for model in models:
        log.info("writer_trying_model", model=model, call=call_name)
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=user,
                        config=config,
                    ),
                    timeout=GEMINI_TIMEOUT,
                )
                return response.text.strip()

            except (genai_errors.ServerError, genai_errors.ClientError) as exc:
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if status not in _RETRYABLE_CODES:
                    raise
                # Try to extract Retry-After from the error message
                retry_after = _parse_retry_after(str(exc))
                wait = retry_after if retry_after else _BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning(
                    "writer_retrying",
                    model=model,
                    call=call_name,
                    attempt=attempt,
                    status=status,
                    wait_seconds=wait,
                )
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(wait)

            except asyncio.TimeoutError as exc:
                log.warning("writer_timeout", model=model, call=call_name, attempt=attempt)
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BACKOFF_BASE)

        log.warning("writer_model_exhausted", model=model, call=call_name)

    raise WriterError(
        stage="writing",
        original=last_exc or RuntimeError(f"All Gemini models exhausted ({call_name})"),
    ) from last_exc


def _parse_retry_after(error_msg: str) -> float | None:
    """Extract Retry-After seconds from a Gemini 429 error message."""
    import re as _re
    match = _re.search(r"Please retry in (\d+(?:\.\d+)?)s", error_msg)
    if match:
        return float(match.group(1)) + 2.0  # add 2s buffer
    return None


# ─── Metadata Parser ──────────────────────────────────────────────────────────

def _parse_meta_json(raw: str) -> dict:
    """Parse and validate the small metadata JSON from Call 1.

    Attempts strict JSON parse first; if that fails, tries to auto-repair
    common issues (truncated JSON, trailing commas) before failing.
    """
    # Strip optional markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

    data: dict | None = None

    # Attempt 1: strict parse
    with suppress(json.JSONDecodeError):
        data = json.loads(cleaned)

    # Attempt 2: auto-close truncated JSON (append missing closing brace)
    if data is None:
        for suffix in ["}]", "]", "}"]:
            with suppress(json.JSONDecodeError):
                data = json.loads(cleaned.rstrip(",\n ") + suffix)
                if isinstance(data, dict):
                    break
                data = None

    # Attempt 3: strip trailing commas then retry
    if data is None:
        fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)
        with suppress(json.JSONDecodeError):
            data = json.loads(fixed)

    if data is None or not isinstance(data, dict):
        raise WriterError(
            stage="writing",
            original=ValueError(
                f"Gemini returned unparseable metadata JSON. Excerpt: {cleaned[:300]}"
            ),
        )

    missing = _META_FIELDS - data.keys()
    if missing:
        raise WriterError(
            stage="writing",
            original=ValueError(f"Missing metadata fields: {missing}"),
        )

    tags = data.get("tags", [])
    if not isinstance(tags, list) or len(tags) == 0:
        # Generate minimal fallback tags
        data["tags"] = ["AI", "Technology", "Software", "Engineering", "Innovation"]

    return data
