"""
server.py — MCP entry point for the Medium Auto-Publisher.

Registers 11 tools:
  1. schedule_article    — schedule a future publish
  2. publish_now         — fire pipeline immediately (background)
  3. list_scheduled      — list pending/running jobs
  4. cancel_job          — cancel a pending job
  5. get_status          — poll a job's current state
  6. preview_article     — research + write only (no API spend)
  7. list_drafts         — browse saved draft articles
  8. read_draft          — read a specific draft's content
  9. generate_calendar   — AI-powered content calendar generation
  10. batch_schedule     — schedule multiple articles at once
  11. publish_draft      — auto-post a saved draft to Medium via Playwright bot

Supports two transports:
  - stdio  (default) — for Claude Desktop
  - sse    (--transport sse) — for cloud deployment via HTTP

Logging is configured once here via structlog and propagated throughout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from config import MIN_PUBLISH_DELAY_SECONDS, get_settings
from models.errors import (
    IdempotencyError,
    JobCancellationError,
    JobNotFoundError,
    ValidationError,
)
from models.schemas import (
    BatchScheduleOutput,
    BatchScheduleResult,
    CalendarEntry,
    CancelJobOutput,
    DraftInfo,
    GenerateCalendarOutput,
    GetStatusOutput,
    ListDraftsOutput,
    ListScheduledOutput,
    PreviewArticleOutput,
    PublishNowOutput,
    ReadDraftOutput,
    ScheduleArticleOutput,
    ScheduledJobInfo,
)
from pipeline import calendar as calendar_gen, orchestrator, researcher, writer, validator
from scheduler.fingerprint import make_fingerprint
from scheduler.job_manager import get_job_manager

# ── Logging setup ─────────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ── MCP server ────────────────────────────────────────────────────────────────
app = Server("medium-autopublisher")


# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="schedule_article",
            description=(
                "Schedule an article to be researched, written, illustrated, and "
                "published to Medium at a specific future time. Idempotent — "
                "duplicate calls with the same topic+time return the existing job."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 200,
                        "description": "The article topic.",
                    },
                    "publish_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime with timezone (must be ≥2 min in future).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 25},
                        "minItems": 1,
                        "maxItems": 5,
                        "description": "1–5 Medium tags.",
                    },
                    "style_hint": {
                        "type": "string",
                        "default": "",
                        "description": "Writing style: 'technical', 'opinion', or 'beginner'.",
                    },
                },
                "required": ["topic", "publish_at", "tags"],
            },
        ),
        Tool(
            name="publish_now",
            description=(
                "Start the full pipeline immediately for a given topic. "
                "Returns right away — poll get_status(job_id) to track progress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "minLength": 3, "maxLength": 200},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 25},
                        "minItems": 1,
                        "maxItems": 5,
                    },
                    "style_hint": {"type": "string", "default": ""},
                },
                "required": ["topic", "tags"],
            },
        ),
        Tool(
            name="list_scheduled",
            description="List all pending and running pipeline jobs.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cancel_job",
            description="Cancel a pending (not yet running) scheduled job.",
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        Tool(
            name="get_status",
            description=(
                "Get the current status, stage, Medium URL, error, and duration "
                "for a pipeline job."
            ),
            inputSchema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        ),
        Tool(
            name="preview_article",
            description=(
                "Run research + write stages only. Returns draft article without "
                "touching Medium API or spending DALL-E credits."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "minLength": 3, "maxLength": 200},
                    "style_hint": {"type": "string", "default": ""},
                },
                "required": ["topic"],
            },
        ),
        Tool(
            name="list_drafts",
            description=(
                "List all locally saved draft articles in the drafts/ directory. "
                "Shows slug, title, tags, image availability, and creation date."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="read_draft",
            description=(
                "Read the full markdown content of a specific draft by its slug. "
                "Use list_drafts first to get available slugs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "The draft folder slug (from list_drafts).",
                    },
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="generate_calendar",
            description=(
                "Generate an AI-powered content calendar for a given niche. "
                "Returns topic suggestions with tags, dates, style hints, and rationale. "
                "Feed the output into batch_schedule to queue all articles at once."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "niche": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 200,
                        "description": "Content niche or theme (e.g., 'AI in healthcare', 'startup growth').",
                    },
                    "num_articles": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 12,
                        "default": 4,
                        "description": "Number of article topics to generate (1-12).",
                    },
                    "cadence": {
                        "type": "string",
                        "enum": ["daily", "twice_weekly", "weekly", "biweekly"],
                        "default": "weekly",
                        "description": "Publishing cadence.",
                    },
                    "style_hint": {
                        "type": "string",
                        "default": "",
                        "description": "Overall style preference.",
                    },
                },
                "required": ["niche"],
            },
        ),
        Tool(
            name="batch_schedule",
            description=(
                "Schedule multiple articles at once. Accepts an array of article entries "
                "(from generate_calendar or manual input). Each entry needs topic, publish_at, "
                "and tags. Returns a summary of what was scheduled, duplicates, and failures."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "articles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "topic": {"type": "string"},
                                "publish_at": {"type": "string", "description": "ISO 8601 datetime with timezone."},
                                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
                                "style_hint": {"type": "string", "default": ""},
                            },
                            "required": ["topic", "publish_at", "tags"],
                        },
                        "minItems": 1,
                        "maxItems": 12,
                        "description": "Array of articles to schedule.",
                    },
                },
                "required": ["articles"],
            },
        ),
        Tool(
            name="publish_draft",
            description=(
                "Automatically publish a saved local draft to Medium using the Playwright bot. "
                "Logs into your Medium account, creates a new story, pastes the article content, "
                "uploads the header image, adds tags, and clicks Publish. "
                "Use list_drafts first to get available slugs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "The draft folder slug (from list_drafts).",
                    },
                    "headless": {
                        "type": "boolean",
                        "default": True,
                        "description": "Run browser in headless mode (invisible). Set false to watch the bot work.",
                    },
                },
                "required": ["slug"],
            },
        ),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Tool handler
# ─────────────────────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "schedule_article":
            result = await _schedule_article(arguments)
        elif name == "publish_now":
            result = await _publish_now(arguments)
        elif name == "list_scheduled":
            result = await _list_scheduled()
        elif name == "cancel_job":
            result = await _cancel_job(arguments)
        elif name == "get_status":
            result = await _get_status(arguments)
        elif name == "preview_article":
            result = await _preview_article(arguments)
        elif name == "list_drafts":
            result = await _list_drafts()
        elif name == "read_draft":
            result = await _read_draft(arguments)
        elif name == "generate_calendar":
            result = await _generate_calendar(arguments)
        elif name == "batch_schedule":
            result = await _batch_schedule(arguments)
        elif name == "publish_draft":
            result = await _publish_draft(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        log.error("tool_error", tool=name, error=str(exc))
        result = {"error": str(exc)}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ─────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────────────────────

async def _schedule_article(args: dict[str, Any]) -> dict[str, Any]:
    topic: str = args["topic"]
    publish_at_raw: str = args["publish_at"]
    tags: list[str] = args["tags"]
    style_hint: str = args.get("style_hint", "")

    # Parse and validate datetime
    try:
        publish_at = datetime.fromisoformat(publish_at_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid publish_at datetime: {publish_at_raw}") from exc

    if publish_at.tzinfo is None:
        raise ValueError("publish_at must include timezone info (e.g. 2025-06-01T09:00:00+05:30)")

    now = datetime.now(timezone.utc)
    delay = (publish_at - now).total_seconds()
    if delay < MIN_PUBLISH_DELAY_SECONDS:
        raise ValueError(
            f"publish_at must be at least {MIN_PUBLISH_DELAY_SECONDS // 60} minutes in the future "
            f"(currently {delay:.0f}s away)."
        )

    fingerprint = make_fingerprint(topic, publish_at.isoformat())
    jm = get_job_manager()

    # Idempotency layer 1: check existing fingerprint
    existing = jm.find_by_fingerprint(fingerprint)
    if existing:
        log.info("duplicate_job_detected", fingerprint=fingerprint, existing=existing)
        return ScheduleArticleOutput(
            job_id=existing,
            fingerprint=fingerprint,
            scheduled_at=publish_at.strftime("%Y-%m-%d %H:%M %Z"),
            is_duplicate=True,
            message=f"Duplicate detected. Returning existing job {existing}.",
        ).model_dump()

    job_id = str(uuid.uuid4())
    jm.create_job(
        job_id=job_id,
        fingerprint=fingerprint,
        topic=topic,
        tags=tags,
        style_hint=style_hint,
        publish_at=publish_at.isoformat(),
    )
    jm.schedule_pipeline(
        job_id=job_id,
        topic=topic,
        tags=tags,
        style_hint=style_hint,
        publish_at=publish_at,
        orchestrator_fn=_run_pipeline_for_scheduler,
    )

    log.info("job_created", job_id=job_id, topic=topic[:60], publish_at=publish_at.isoformat())
    return ScheduleArticleOutput(
        job_id=job_id,
        fingerprint=fingerprint,
        scheduled_at=publish_at.strftime("%Y-%m-%d %H:%M %Z"),
        is_duplicate=False,
        message=f"Article scheduled for {publish_at.strftime('%Y-%m-%d %H:%M %Z')}.",
    ).model_dump()


async def _publish_now(args: dict[str, Any]) -> dict[str, Any]:
    topic: str = args["topic"]
    tags: list[str] = args["tags"]
    style_hint: str = args.get("style_hint", "")

    job_id = str(uuid.uuid4())
    fingerprint = make_fingerprint(topic, job_id)   # unique — no dedup for immediate jobs
    jm = get_job_manager()

    jm.create_job(
        job_id=job_id,
        fingerprint=fingerprint,
        topic=topic,
        tags=tags,
        style_hint=style_hint,
    )

    # Fire pipeline in background — do not await
    asyncio.create_task(
        _run_pipeline_for_scheduler(
            job_id=job_id,
            topic=topic,
            tags=tags,
            style_hint=style_hint,
        )
    )

    log.info("publish_now_started", job_id=job_id, topic=topic[:60])
    return PublishNowOutput(
        job_id=job_id,
        message=f"Pipeline started — poll get_status('{job_id}') for progress.",
    ).model_dump()


async def _list_scheduled() -> dict[str, Any]:
    jm = get_job_manager()
    rows = jm.list_active_jobs()
    jobs = [
        ScheduledJobInfo(
            job_id=r["job_id"],
            topic=r["topic"],
            scheduled_at=r.get("publish_at") or "immediate",
            status=r["status"],
            fingerprint=r["fingerprint"],
        )
        for r in rows
    ]
    return ListScheduledOutput(jobs=jobs, count=len(jobs)).model_dump()


async def _cancel_job(args: dict[str, Any]) -> dict[str, Any]:
    job_id: str = args["job_id"]
    jm = get_job_manager()
    jm.cancel_job(job_id)   # raises JobNotFoundError or JobCancellationError
    return CancelJobOutput(
        job_id=job_id,
        status="cancelled",
        message=f"Job {job_id} has been cancelled.",
    ).model_dump()


async def _get_status(args: dict[str, Any]) -> dict[str, Any]:
    job_id: str = args["job_id"]
    jm = get_job_manager()
    record = jm.get_job(job_id)
    if record is None:
        raise JobNotFoundError(job_id)

    # Compute duration
    duration: float | None = None
    if record.get("started_at") and record.get("completed_at"):
        start = datetime.fromisoformat(record["started_at"])
        end = datetime.fromisoformat(record["completed_at"])
        duration = (end - start).total_seconds()
    elif record.get("started_at"):
        start = datetime.fromisoformat(record["started_at"])
        duration = (datetime.now(timezone.utc) - start).total_seconds()

    return GetStatusOutput(
        job_id=job_id,
        status=record["status"],
        current_stage=record.get("current_stage", ""),
        medium_url=record.get("medium_url"),
        error=record.get("error"),
        duration_seconds=round(duration, 1) if duration is not None else None,
    ).model_dump()


async def _preview_article(args: dict[str, Any]) -> dict[str, Any]:
    topic: str = args["topic"]
    style_hint: str = args.get("style_hint", "")
    job_id = f"preview-{uuid.uuid4().hex[:8]}"

    # Stage 1
    research = await researcher.run(topic=topic, job_id=job_id)

    # Stage 2
    article = await writer.run(
        topic=topic,
        research=research,
        style_hint=style_hint,
        job_id=job_id,
    )

    # Quality check (does not raise for preview — returns report regardless)
    try:
        report = validator.check(article=article, job_id=job_id)
        q_score = report.score
        q_notes = report.issues
    except ValidationError as exc:
        q_score = exc.score
        q_notes = exc.issues

    word_count = len(article.body.split())

    return PreviewArticleOutput(
        title=article.title,
        subtitle=article.subtitle,
        article_markdown=article.body,
        word_count=word_count,
        suggested_tags=article.tags,
        quality_score=q_score,
        quality_notes=q_notes,
    ).model_dump()


async def _list_drafts() -> dict[str, Any]:
    """List all draft folders in the drafts/ directory."""
    from pathlib import Path
    import os

    drafts_root = Path("drafts")
    if not drafts_root.exists():
        return ListDraftsOutput(drafts=[], count=0).model_dump()

    drafts: list[DraftInfo] = []
    for child in sorted(drafts_root.iterdir()):
        if not child.is_dir():
            continue

        article_path = child / "article.md"
        image_path = child / "header.png"

        if not article_path.exists():
            continue

        # Parse title and tags from the markdown
        content = article_path.read_text(encoding="utf-8")
        title = child.name.replace("-", " ").title()
        tags = ""

        # Extract real title from markdown
        for line in content.splitlines():
            if line.startswith("# ") and not line.startswith("## "):
                title = line[2:].strip()
            match = re.search(r"<!-- Tags: (.+?) -->", line)
            if match:
                tags = match.group(1)

        # Get creation time
        stat = article_path.stat()
        created = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)

        drafts.append(DraftInfo(
            slug=child.name,
            title=title,
            tags=tags,
            has_image=image_path.exists(),
            created_at=created.strftime("%Y-%m-%d %H:%M UTC"),
        ))

    return ListDraftsOutput(drafts=drafts, count=len(drafts)).model_dump()


async def _read_draft(args: dict[str, Any]) -> dict[str, Any]:
    """Read the full content of a specific draft by slug."""
    from pathlib import Path

    slug: str = args["slug"]

    # Sanitize slug to prevent path traversal
    safe_slug = re.sub(r"[^a-z0-9\-]", "", slug.lower())
    draft_dir = Path("drafts") / safe_slug
    article_path = draft_dir / "article.md"
    image_path = draft_dir / "header.png"

    if not article_path.exists():
        raise FileNotFoundError(f"Draft not found: '{safe_slug}'. Use list_drafts to see available drafts.")

    content = article_path.read_text(encoding="utf-8")
    word_count = len(content.split())

    return ReadDraftOutput(
        slug=safe_slug,
        article_markdown=content,
        word_count=word_count,
        has_image=image_path.exists(),
        image_path=str(image_path.absolute()) if image_path.exists() else None,
    ).model_dump()


async def _generate_calendar(args: dict[str, Any]) -> dict[str, Any]:
    """Generate an AI-powered content calendar for a given niche."""
    niche: str = args["niche"]
    num_articles: int = args.get("num_articles", 4)
    cadence: str = args.get("cadence", "weekly")
    style_hint: str = args.get("style_hint", "")

    entries_raw = await calendar_gen.generate_calendar(
        niche=niche,
        num_articles=num_articles,
        cadence=cadence,
        style_hint=style_hint,
    )

    entries = [
        CalendarEntry(
            topic=e["topic"],
            tags=e["tags"],
            suggested_date=e["suggested_date"],
            style_hint=e["style_hint"],
            rationale=e["rationale"],
        )
        for e in entries_raw
    ]

    return GenerateCalendarOutput(
        niche=niche,
        cadence=cadence,
        entries=entries,
        count=len(entries),
    ).model_dump()


async def _batch_schedule(args: dict[str, Any]) -> dict[str, Any]:
    """Schedule multiple articles at once."""
    articles: list[dict] = args["articles"]
    results: list[BatchScheduleResult] = []
    succeeded = 0
    failed = 0

    for entry in articles:
        try:
            result = await _schedule_article({
                "topic": entry["topic"],
                "publish_at": entry["publish_at"],
                "tags": entry["tags"],
                "style_hint": entry.get("style_hint", ""),
            })

            is_dup = result.get("is_duplicate", False)
            results.append(BatchScheduleResult(
                topic=entry["topic"],
                job_id=result.get("job_id"),
                status="duplicate" if is_dup else "scheduled",
                message=result.get("message", "Scheduled"),
            ))
            succeeded += 1

        except Exception as exc:
            results.append(BatchScheduleResult(
                topic=entry["topic"],
                job_id=None,
                status="failed",
                message=str(exc),
            ))
            failed += 1
            log.warning("batch_schedule_entry_failed", topic=entry["topic"][:60], error=str(exc))

    return BatchScheduleOutput(
        scheduled=results,
        total=len(articles),
        succeeded=succeeded,
        failed=failed,
    ).model_dump()


async def _publish_draft(args: dict[str, Any]) -> dict[str, Any]:
    """Publish a saved draft to Medium using the Playwright bot."""
    from pathlib import Path
    from pipeline.publisher_bot import publish_draft_to_medium

    slug: str = args["slug"]
    headless: bool = args.get("headless", True)

    # Sanitize slug
    safe_slug = re.sub(r"[^a-z0-9\-]", "", slug.lower())
    draft_dir = Path("drafts") / safe_slug

    if not draft_dir.exists():
        raise FileNotFoundError(f"Draft not found: '{safe_slug}'. Use list_drafts to see available drafts.")

    settings = get_settings()
    if not settings.medium_email or not settings.medium_password:
        raise ValueError(
            "MEDIUM_EMAIL and MEDIUM_PASSWORD must be set in .env to use publish_draft."
        )

    job_id = f"bot-{slug[:20]}"
    log.info("publish_draft.start", slug=safe_slug, headless=headless)

    url = await publish_draft_to_medium(
        draft_dir=draft_dir,
        email=settings.medium_email,
        password=settings.medium_password,
        headless=headless,
        job_id=job_id,
    )

    log.info("publish_draft.done", url=url)
    return {
        "status": "published",
        "slug": safe_slug,
        "medium_url": url,
        "message": f"✅ Article published! View it at: {url}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal pipeline runner (used by both APScheduler and publish_now)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_pipeline_for_scheduler(
    job_id: str,
    topic: str,
    tags: list[str],
    style_hint: str,
) -> None:
    """
    Wrapper called by APScheduler and publish_now background tasks.
    On StageError: attempts a retry via job_manager.reschedule_retry.
    On ValidationError or after max retries: marks job permanently failed.
    """
    jm = get_job_manager()
    from models.errors import StageError, ValidationError as VE

    try:
        await orchestrator.run(
            job_id=job_id,
            topic=topic,
            tags=tags,
            style_hint=style_hint,
            job_manager=jm,
        )
    except VE:
        # Validation failures are not retried
        pass
    except StageError:
        retried = jm.reschedule_retry(
            job_id=job_id,
            topic=topic,
            tags=tags,
            style_hint=style_hint,
            orchestrator_fn=_run_pipeline_for_scheduler,
        )
        if not retried:
            record = jm.get_job(job_id)
            if record and record["status"] != "failed":
                jm.mark_failed(
                    job_id=job_id,
                    error="Max retries exhausted",
                    stage=record.get("current_stage", "unknown"),
                )


# ─────────────────────────────────────────────────────────────────────────────
# Startup / entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for transport selection."""
    parser = argparse.ArgumentParser(description="Medium Auto-Publisher MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: stdio (Claude Desktop) or sse (HTTP for cloud).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for SSE HTTP server (only used with --transport sse).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind SSE server to (default: 0.0.0.0).",
    )
    return parser.parse_args()


async def main_stdio() -> None:
    """Run the MCP server via stdio transport (for Claude Desktop)."""
    settings = get_settings()
    log.info("server_starting", transport="stdio", log_level=settings.log_level)

    jm = get_job_manager()
    await jm.start()

    log.info("server_ready", transport="stdio")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main_sse(host: str, port: int) -> None:
    """Run the MCP server via SSE transport (for cloud deployment)."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from mcp.server.sse import SseServerTransport

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(
                streams[0], streams[1], app.create_initialization_options()
            )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": "medium-autopublisher", "tools": 11})

    async def on_startup():
        settings = get_settings()
        log.info("server_starting", transport="sse", host=host, port=port)
        jm = get_job_manager()
        await jm.start()
        log.info("server_ready", transport="sse", url=f"http://{host}:{port}")

    starlette_app = Starlette(
        debug=False,
        on_startup=[on_startup],
        routes=[
            Route("/health", health),
            Route("/sse", handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
    )

    uvicorn.run(starlette_app, host=host, port=port)


if __name__ == "__main__":
    args = parse_args()
    if args.transport == "sse":
        main_sse(host=args.host, port=args.port)
    else:
        asyncio.run(main_stdio())
