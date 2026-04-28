"""
pipeline/illustrator.py — Stage 3: Illustrate

Generates a 1792×1024 header image via Pollinations.ai (free, no API key).
Returns raw image bytes — no file I/O; bytes live in memory until publisher saves them.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx
import structlog

from config import (
    DALLE_PROMPT_PREFIX,
    POLLINATIONS_BASE_URL,
    POLLINATIONS_HEIGHT,
    POLLINATIONS_TIMEOUT,
    POLLINATIONS_WIDTH,
)
from models.errors import IllustratorError

logger = structlog.get_logger()


async def run(image_prompt: str, job_id: str = "preview") -> bytes:
    """
    Stage 3 entry point.

    Args:
        image_prompt: Scene description produced by the writer.
        job_id:       Used for structured logging.

    Returns:
        Raw image bytes from Pollinations.ai.

    Raises:
        IllustratorError: wraps any underlying exception.
    """
    bound = logger.bind(job_id=job_id, stage="image")
    bound.info("illustrator_start")

    try:
        image_bytes = await _generate_image(image_prompt=image_prompt)
        bound.info("illustrator_complete", size_bytes=len(image_bytes))
        return image_bytes

    except IllustratorError:
        raise
    except Exception as exc:
        bound.error("illustrator_failed", error=str(exc))
        raise IllustratorError(stage="image", original=exc) from exc


async def _generate_image(image_prompt: str) -> bytes:
    """Call Pollinations.ai and return image bytes. Free, no API key needed."""
    full_prompt = DALLE_PROMPT_PREFIX + image_prompt
    encoded_prompt = quote(full_prompt, safe="")

    url = (
        f"{POLLINATIONS_BASE_URL}{encoded_prompt}"
        f"?width={POLLINATIONS_WIDTH}"
        f"&height={POLLINATIONS_HEIGHT}"
        f"&nologo=true"
    )

    async with httpx.AsyncClient(timeout=POLLINATIONS_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()

        if len(response.content) < 1000:
            raise IllustratorError(
                stage="image",
                original=ValueError(
                    f"Pollinations returned suspiciously small response ({len(response.content)} bytes)"
                ),
            )

        return response.content
