"""
config.py — All application constants and settings.
Zero magic strings anywhere else in the codebase.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─── Timeouts (seconds) ───────────────────────────────────────────────────────────
GEMINI_TIMEOUT: float = 90.0
POLLINATIONS_TIMEOUT: float = 60.0
TAVILY_TIMEOUT: float = 15.0

# ─── HTTP Retry Config ────────────────────────────────────────────────────────
HTTP_RETRIES: int = 3
HTTP_BACKOFF: float = 2.0
HTTP_RETRY_ON: list[int] = [429, 500, 502, 503]
HTTP_RESPECT_RETRY_AFTER: bool = True

# ─── Pipeline Thresholds ──────────────────────────────────────────────────────
MIN_QUALITY_SCORE: float = 0.70
QUALITY_WARNING_THRESHOLD: float = 0.80
MIN_PUBLISH_DELAY_SECONDS: int = 120        # 2 minutes

# ─── Scheduler ────────────────────────────────────────────────────────────────
SCHEDULER_MISFIRE_GRACE: int = 300          # 5 minutes
SCHEDULER_MAX_RETRIES: int = 2
SCHEDULER_RETRY_DELAY_SECONDS: int = 300    # 5 minutes

# ─── AI Models ────────────────────────────────────────────────────────────────
WRITER_MODEL: str = "gemini-2.5-flash-lite"   # free tier with quota
DECOMPOSER_MODEL: str = "gemini-2.5-flash-lite"  # same model for sub-questions
WRITER_MAX_TOKENS: int = 4096
WRITER_TEMPERATURE: float = 0.7

# ─── Image Generation (Pollinations.ai — free, no key needed) ─────────────
POLLINATIONS_BASE_URL: str = "https://image.pollinations.ai/prompt/"  # old endpoint, still free
POLLINATIONS_WIDTH: int = 1792
POLLINATIONS_HEIGHT: int = 1024


# ─── Validator Thresholds ─────────────────────────────────────────────────────
WORD_COUNT_MIN: int = 1000
WORD_COUNT_MAX: int = 2500      # raised from 1800 — Gemini writes thorough articles
H2_SECTIONS_GOOD: int = 4
H2_SECTIONS_OK: int = 2
SOURCE_LINKS_GOOD: int = 2
TITLE_LENGTH_MIN: int = 30     # lowered slightly for flexibility
TITLE_LENGTH_MAX: int = 100    # raised slightly
CTA_WINDOW_CHARS: int = 400    # expanded from 200 — Gemini CTA is sometimes in penultimate para
PLACEHOLDER_STRINGS: list[str] = ["[INSERT", "TODO", "PLACEHOLDER", "example.com"]  # removed '...' (common in prose)
CTA_KEYWORDS: list[str] = ["share", "comment", "try", "start", "join", "tell", "think", "consider", "explore", "let me know"]

# ─── Prompts (exact text — do not paraphrase) ─────────────────────────────────
RESEARCHER_DECOMPOSE_PROMPT: str = (
    "Given the blog topic '{topic}', generate exactly 3 specific web search queries "
    "that together give a writer comprehensive, up-to-date information. "
    "Output ONLY a JSON array of 3 strings. No explanation."
)

DALLE_PROMPT_PREFIX: str = (
    "Editorial blog header photograph, professional and modern, "
    "16:9 landscape, sharp focus, clean composition, no text, "
    "no watermarks, no logos, no visible human faces, "
    "suitable for a technology/business publication. "
    "Scene: "
)

WRITER_SYSTEM_PROMPT: str = """\
You are an expert writer for Medium — the platform where thoughtful people read \
long-form articles on technology, business, science, and culture.

VOICE & TONE:
- Authoritative but never condescending
- Data-driven: every major claim backed by a stat or a cited source
- Conversational: write like a smart colleague explaining over coffee
- Opinionated: take a clear stance, avoid wishy-washy both-sidesing
- Concrete: use real examples, not abstract generalities

ARTICLE STRUCTURE — follow this exactly:

1. HOOK (2–3 sentences):
   Open with a surprising stat, a counterintuitive claim, or a vivid scenario.
   NEVER start with "In today's world", "In this article", or "As we all know".

2. CONTEXT (1 short paragraph):
   Why this topic matters right now.
   One sentence max on what the reader will learn.

3. BODY (4–6 H2 sections, 150–300 words each):
   - At least one H3 sub-point per H2 section
   - Cite research sources inline as [anchor text](url)
   - Anchor text must be descriptive — never "click here"
   - Maximum one list (bullets or numbered) per section
   - Bold (**text**) for key terms on first use only

4. KEY TAKEAWAYS (H2 titled "What this means for you"):
   3–4 specific, actionable bullet points.
   Not vague advice. Concrete next steps the reader can take today, this week, or this month.

5. CLOSING (1 paragraph + CTA):
   Synthesize the single most important insight.
   End with a direct question or invitation that makes the reader want to respond in the comments.

MEDIUM MARKDOWN RULES:
- ## for H2 sections, ### for H3 sub-points
- No raw HTML anywhere in the body
- No horizontal rules (---)
- Blockquotes (>) for powerful statistics only, max 2
- Standard paragraph breaks (blank line between paras)

OUTPUT FORMAT:
Respond with ONLY a valid JSON object.
No preamble. No explanation. No markdown code fences.
No trailing commas. Just the raw JSON:

{
  "title": "40–90 character compelling title",
  "subtitle": "subtitle expanding the title, 60–120 chars",
  "body": "full Medium markdown, 1200–1800 words",
  "image_prompt": "Real-world photographic scene that represents this article's core theme. Include: setting, lighting, mood, subject. 1–2 sentences. No text, logos, UI, or screen elements.",
  "tags": ["tag1","tag2","tag3","tag4","tag5"]
}"""


class Settings(BaseSettings):
    """All secrets and runtime configuration loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = Field(..., description="Google Gemini API key")
    tavily_api_key: str = Field(..., description="Tavily API key")

    # Medium credentials for Playwright bot (optional — only needed for publish_draft)
    medium_email: str = Field("", description="Medium account email")
    medium_password: str = Field("", description="Medium account password")

    log_level: str = Field("INFO", description="Log level")
    max_concurrent_jobs: int = Field(3, description="Max concurrent pipeline jobs")
    scheduler_db_path: str = Field("./scheduler.db", description="APScheduler SQLite DB path")
    jobs_db_path: str = Field("./jobs.db", description="Application jobs SQLite DB path")


_settings_cache: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance (loaded once at startup)."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = Settings()
    return _settings_cache
