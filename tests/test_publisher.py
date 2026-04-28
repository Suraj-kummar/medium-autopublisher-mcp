"""
tests/test_publisher.py

Tests for pipeline/publisher.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipeline import publisher
from models.schemas import ArticleDraft


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."


def _make_article() -> ArticleDraft:
    return ArticleDraft(
        title="How AI Is Reshaping Modern Healthcare Systems Today",
        subtitle="A deep dive into the technology transforming medicine at every level",
        body=(
            "Three years ago, a Stanford algorithm outperformed dermatologists.\n\n"
            "## The Diagnostic Revolution\n\n### Computer Vision\n\n"
        ),
        image_prompt="Hospital corridor with blue lighting and data overlays.",
        tags=["AI", "Healthcare", "Technology", "Medicine", "Innovation"],
    )


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publisher_saves_draft_locally(tmp_path, monkeypatch):
    """
    publisher.run() must save the article.md and header.png in a drafts/slug directory.
    """
    # Override the working directory to our tmp_path so it doesn't write to the real drafts/ folder
    monkeypatch.chdir(tmp_path)

    article = _make_article()
    image_bytes = _make_png()
    
    url = await publisher.run(
        article=article,
        image_bytes=image_bytes,
        tags=["AI", "Healthcare"],
        job_id="test-pub-001",
    )

    slug = "how-ai-is-reshaping-modern-healthcare-systems-today"
    drafts_dir = tmp_path / "drafts" / slug

    assert drafts_dir.exists(), "Drafts directory was not created"
    assert drafts_dir.is_dir()

    article_path = drafts_dir / "article.md"
    image_path = drafts_dir / "header.png"

    assert article_path.exists(), "Article markdown was not saved"
    assert image_path.exists(), "Header image was not saved"

    assert image_path.read_bytes() == image_bytes

    content = article_path.read_text(encoding="utf-8")
    assert "<!-- Tags: AI, Healthcare -->" in content
    assert "<!-- Subtitle: A deep dive" in content
    assert "# How AI Is Reshaping" in content
    assert "![Header Image](./header.png)" in content
    assert "Three years ago, a Stanford algorithm" in content

    assert url.startswith("file:///"), "Must return a file URI"
    assert "drafts" in url
    assert "article.md" in url
