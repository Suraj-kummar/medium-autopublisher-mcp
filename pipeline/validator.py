"""
pipeline/validator.py — Quality gate between Stage 2 (write) and Stage 3 (image).

Six independent checks, each scored 0.0–1.0.
Final score = mean of all six checks.
If score < MIN_QUALITY_SCORE (0.70): raises ValidationError.
If score in [0.70, QUALITY_WARNING_THRESHOLD): logs WARNING but proceeds.
"""
from __future__ import annotations

import re

import structlog

from config import (
    CTA_KEYWORDS,
    CTA_WINDOW_CHARS,
    H2_SECTIONS_GOOD,
    H2_SECTIONS_OK,
    MIN_QUALITY_SCORE,
    PLACEHOLDER_STRINGS,
    QUALITY_WARNING_THRESHOLD,
    SOURCE_LINKS_GOOD,
    TITLE_LENGTH_MAX,
    TITLE_LENGTH_MIN,
    WORD_COUNT_MAX,
    WORD_COUNT_MIN,
)
from models.errors import ValidationError
from models.schemas import ArticleDraft, QualityReport

logger = structlog.get_logger()

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")


def check(article: ArticleDraft, job_id: str = "preview") -> QualityReport:
    """
    Run all quality checks and return a QualityReport.

    Raises ValidationError if score < MIN_QUALITY_SCORE.
    """
    bound = logger.bind(job_id=job_id, stage="validating", topic=article.title[:60])

    checks: dict[str, float] = {
        "word_count": _check_word_count(article.body),
        "has_h2_sections": _check_h2_sections(article.body),
        "has_sources": _check_sources(article.body),
        "no_placeholders": _check_no_placeholders(article.body),
        "title_quality": _check_title_quality(article.title),
        "has_closing_cta": _check_closing_cta(article.body),
    }

    score = sum(checks.values()) / len(checks)
    issues = [name for name, s in checks.items() if s < 1.0]

    report = QualityReport(score=round(score, 4), issues=issues, checks=checks)

    if score < MIN_QUALITY_SCORE:
        bound.error(
            "validation_failed",
            score=score,
            issues=issues,
            checks=checks,
        )
        raise ValidationError(score=score, issues=issues)

    if score < QUALITY_WARNING_THRESHOLD:
        bound.warning(
            "validation_near_miss",
            score=score,
            issues=issues,
        )
    else:
        bound.info("validation_passed", score=score)

    return report


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_word_count(body: str) -> float:
    count = len(body.split())
    return 1.0 if WORD_COUNT_MIN <= count <= WORD_COUNT_MAX else 0.5


def _check_h2_sections(body: str) -> float:
    # Count ## and ### headers combined (Gemini sometimes varies heading levels)
    heading_count = sum(
        1 for line in body.splitlines()
        if re.match(r"^#{2,4} ", line)
    )
    if heading_count >= H2_SECTIONS_GOOD:
        return 1.0
    if heading_count >= H2_SECTIONS_OK:
        return 0.6
    return 0.2


def _check_sources(body: str) -> float:
    link_count = len(_MARKDOWN_LINK_RE.findall(body))
    if link_count >= SOURCE_LINKS_GOOD:
        return 1.0
    if link_count == 1:
        return 0.5
    return 0.0


def _check_no_placeholders(body: str) -> float:
    for placeholder in PLACEHOLDER_STRINGS:
        if placeholder in body:
            return 0.0
    return 1.0


def _check_title_quality(title: str) -> float:
    length_ok = TITLE_LENGTH_MIN <= len(title) <= TITLE_LENGTH_MAX
    not_all_caps = title != title.upper()
    ends_with_word = bool(re.search(r"\w$", title))
    if length_ok and not_all_caps and ends_with_word:
        return 1.0
    return 0.5


def _check_closing_cta(body: str) -> float:
    tail = body[-CTA_WINDOW_CHARS:].lower()
    if "?" in tail:
        return 1.0
    if any(kw in tail for kw in CTA_KEYWORDS):
        return 1.0
    return 0.6
