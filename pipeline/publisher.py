"""
pipeline/publisher.py — Stages 4 & 5: Save locally (Option A)

Saves the article and header image to a local `drafts/` directory.
Returns the absolute path of the directory.
"""
from __future__ import annotations

import re
from pathlib import Path

import structlog

from models.errors import PublisherError
from models.schemas import ArticleDraft

logger = structlog.get_logger()


async def run(
    article: ArticleDraft,
    image_bytes: bytes,
    tags: list[str],
    job_id: str = "preview",
) -> str:
    """
    Stage 4+5 entry point.
    Saves the DALL-E image and the markdown article to `drafts/{slug}/`.

    Returns:
        The local path to the generated draft.
    """
    bound = logger.bind(job_id=job_id, stage="publish", topic=article.title[:60])
    bound.info("publisher_start_local")

    try:
        # Create safe slug from title
        slug = re.sub(r"[^a-z0-9]+", "-", article.title.lower()).strip("-")
        
        drafts_dir = Path("drafts") / slug
        drafts_dir.mkdir(parents=True, exist_ok=True)

        image_path = drafts_dir / "header.png"
        article_path = drafts_dir / "article.md"

        # Save image
        image_path.write_bytes(image_bytes)
        bound.info("publisher_image_saved", path=str(image_path))

        # Save article
        # Add tags to the top, and the image reference
        tags_str = ", ".join(tags)
        full_body = (
            f"<!-- Tags: {tags_str} -->\n"
            f"<!-- Subtitle: {article.subtitle} -->\n\n"
            f"# {article.title}\n\n"
            f"![Header Image](./header.png)\n\n"
            f"{article.body}"
        )
        
        article_path.write_text(full_body, encoding="utf-8")
        bound.info("publisher_article_saved", path=str(article_path))

        draft_url = f"file:///{article_path.absolute().as_posix()}"
        bound.info("publisher_complete", medium_url=draft_url)
        return draft_url

    except Exception as exc:
        bound.error("publisher_failed", error=str(exc))
        raise PublisherError(stage="publish", original=exc) from exc
