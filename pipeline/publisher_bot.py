"""
pipeline/publisher_bot.py — Playwright-based Medium auto-publisher.

Automates the full posting flow:
  1. Open medium.com → Sign In (email + password)
  2. Navigate to New Story
  3. Type / paste the article title + body
  4. Upload the header image
  5. Add tags
  6. Publish

Requires:
  pip install playwright
  playwright install chromium
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import structlog
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

log = structlog.get_logger()

# ── Selectors (Medium's DOM as of 2025-04) ───────────────────────────────────
_SIGN_IN_BTN      = "text=Sign in"
_EMAIL_INPUT      = 'input[name="email"], input[type="email"]'
_PW_INPUT         = 'input[name="password"], input[type="password"]'
_SUBMIT_BTN       = 'button[type="submit"]'
_NEW_STORY_BTN    = 'a[href*="new-story"], a[data-action="new-story"]'
_TITLE_FIELD      = 'h3[data-placeholder="Title"], [data-testid="title"]'
_BODY_FIELD       = 'p[data-placeholder], div[contenteditable="true"]'
_PUBLISH_BTN      = 'button:has-text("Publish")'
_READY_TO_PUB_BTN = 'button:has-text("Publish now"), button:has-text("Publish story")'
_TAGS_INPUT       = 'input[placeholder*="tag"], input[aria-label*="tag"]'

# ── Timeouts (ms) ────────────────────────────────────────────────────────────
PAGE_TIMEOUT      = 30_000   # 30 s
SLOW_MO           = 80       # ms between each action (human-like)


async def publish_draft_to_medium(
    draft_dir: Path,
    *,
    email: str,
    password: str,
    headless: bool = False,
    job_id: str = "bot",
) -> str:
    """
    Read draft_dir/article.md + draft_dir/header.png and publish to Medium.

    Returns the URL of the published story.
    Raises RuntimeError on any failure.
    """
    article_path = draft_dir / "article.md"
    image_path   = draft_dir / "header.png"

    if not article_path.exists():
        raise FileNotFoundError(f"article.md not found in {draft_dir}")

    content  = article_path.read_text(encoding="utf-8")
    title, body, tags = _parse_article(content)

    log.info("publisher_bot.start", job_id=job_id, title=title[:60], has_image=image_path.exists())

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, slow_mo=SLOW_MO)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            url = await _full_flow(
                page=page,
                email=email,
                password=password,
                title=title,
                body=body,
                tags=tags,
                image_path=image_path if image_path.exists() else None,
                job_id=job_id,
            )
            log.info("publisher_bot.done", job_id=job_id, url=url)
            return url
        except Exception as exc:
            # Capture screenshot for debugging
            screenshot = draft_dir / "bot_error.png"
            await page.screenshot(path=str(screenshot))
            log.error("publisher_bot.error", job_id=job_id, error=str(exc), screenshot=str(screenshot))
            raise RuntimeError(f"Medium bot failed: {exc}") from exc
        finally:
            await browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _full_flow(
    page: Page,
    email: str,
    password: str,
    title: str,
    body: str,
    tags: list[str],
    image_path: Path | None,
    job_id: str,
) -> str:
    """Execute the full Medium publish flow. Returns published URL."""

    # ── Step 1: Login ─────────────────────────────────────────────────────────
    log.info("publisher_bot.step", job_id=job_id, step="login")
    await _login(page, email, password)

    # ── Step 2: Open new story ────────────────────────────────────────────────
    log.info("publisher_bot.step", job_id=job_id, step="new_story")
    await _open_new_story(page)

    # ── Step 3: Type title ────────────────────────────────────────────────────
    log.info("publisher_bot.step", job_id=job_id, step="type_title")
    await _type_title(page, title)

    # ── Step 4: Paste body content ────────────────────────────────────────────
    log.info("publisher_bot.step", job_id=job_id, step="paste_body")
    await _paste_body(page, body)

    # ── Step 5: Upload header image (optional) ────────────────────────────────
    if image_path:
        log.info("publisher_bot.step", job_id=job_id, step="upload_image")
        await _upload_image(page, image_path)

    # ── Step 6: Open publish panel + add tags ────────────────────────────────
    log.info("publisher_bot.step", job_id=job_id, step="publish_panel")
    await _open_publish_panel(page, tags)

    # ── Step 7: Confirm publish ───────────────────────────────────────────────
    log.info("publisher_bot.step", job_id=job_id, step="confirm_publish")
    url = await _confirm_publish(page)

    return url


async def _login(page: Page, email: str, password: str) -> None:
    """Navigate to Medium and sign in with email/password."""
    await page.goto("https://medium.com/m/signin", wait_until="networkidle")
    await _human_pause(1.5)

    # Click "Sign in with email" if present
    try:
        email_signin = page.locator('button:has-text("email"), a:has-text("email")')
        await email_signin.first.click(timeout=5_000)
        await _human_pause(1.0)
    except PWTimeout:
        pass  # Already on email form

    # Fill email
    email_input = page.locator(_EMAIL_INPUT).first
    await email_input.wait_for(state="visible", timeout=15_000)
    await email_input.fill(email)
    await _human_pause(0.5)

    # Submit email step (some flows have two steps)
    try:
        continue_btn = page.locator('button:has-text("Continue"), button[type="submit"]').first
        await continue_btn.click(timeout=5_000)
        await _human_pause(1.5)
    except PWTimeout:
        pass

    # Fill password if field appears
    try:
        pw_input = page.locator(_PW_INPUT).first
        await pw_input.wait_for(state="visible", timeout=8_000)
        await pw_input.fill(password)
        await _human_pause(0.5)
        submit = page.locator(_SUBMIT_BTN).first
        await submit.click()
        await _human_pause(2.0)
    except PWTimeout:
        # Medium may send a magic link — handle gracefully
        raise RuntimeError(
            "Password field not found. Medium may have sent a magic link email. "
            "Try logging in once manually first to establish session cookies."
        )

    # Wait for home page / feed
    await page.wait_for_url("**/medium.com/**", timeout=20_000)
    await _human_pause(2.0)
    log.info("publisher_bot.logged_in")


async def _open_new_story(page: Page) -> None:
    """Click 'Write' or navigate to the new story editor."""
    try:
        write_btn = page.locator('a[href*="new-story"]').first
        await write_btn.click(timeout=8_000)
    except PWTimeout:
        await page.goto("https://medium.com/new-story", wait_until="networkidle")

    await page.wait_for_url("**/medium.com/p/*/edit**", timeout=20_000)
    await _human_pause(2.0)


async def _type_title(page: Page, title: str) -> None:
    """Click title area and type the article title."""
    title_area = page.locator('h3[data-placeholder="Title"]').first
    await title_area.wait_for(state="visible", timeout=15_000)
    await title_area.click()
    await _human_pause(0.3)
    await page.keyboard.type(title, delay=30)
    await page.keyboard.press("Enter")
    await _human_pause(0.5)


async def _paste_body(page: Page, body: str) -> None:
    """Paste article body text into the editor using clipboard."""
    # Use clipboard for reliability (typing 1500 words character-by-character is slow)
    body_area = page.locator('div[contenteditable="true"]').nth(1)
    await body_area.wait_for(state="visible", timeout=10_000)
    await body_area.click()
    await _human_pause(0.5)

    # Write to clipboard then paste
    await page.evaluate(f"navigator.clipboard.writeText({repr(body)})")
    await _human_pause(0.3)

    if os.name == "nt":
        await page.keyboard.press("Control+v")
    else:
        await page.keyboard.press("Meta+v")

    await _human_pause(2.0)


async def _upload_image(page: Page, image_path: Path) -> None:
    """
    Upload header image.
    Medium's editor uses an image button in the toolbar ('+' floating menu).
    """
    try:
        # Click the '+' add content button
        add_btn = page.locator('button[aria-label*="add"], button[data-action*="image"]').first
        await add_btn.click(timeout=5_000)
        await _human_pause(0.5)

        # Click image option
        img_option = page.locator('button[aria-label*="image"], button[data-action="image"]').first
        await img_option.click(timeout=5_000)
        await _human_pause(0.5)

        # Handle file chooser
        async with page.expect_file_chooser() as fc_info:
            await page.keyboard.press("Enter")
        file_chooser = await fc_info.value
        await file_chooser.set_files(str(image_path))
        await _human_pause(3.0)  # wait for upload
        log.info("publisher_bot.image_uploaded", path=str(image_path))

    except PWTimeout:
        log.warning("publisher_bot.image_skip", reason="Could not find image button — skipping image upload")


async def _open_publish_panel(page: Page, tags: list[str]) -> None:
    """Click the Publish button to open the pre-publish panel and add tags."""
    pub_btn = page.locator('button:has-text("Publish")').first
    await pub_btn.wait_for(state="visible", timeout=15_000)
    await pub_btn.click()
    await _human_pause(1.5)

    # Add tags
    if tags:
        try:
            tag_input = page.locator('input[placeholder*="tag"], input[aria-label*="tag"]').first
            await tag_input.wait_for(state="visible", timeout=8_000)
            for tag in tags[:5]:  # Medium allows max 5 tags
                await tag_input.fill(tag)
                await page.keyboard.press("Enter")
                await _human_pause(0.4)
            log.info("publisher_bot.tags_added", tags=tags)
        except PWTimeout:
            log.warning("publisher_bot.tags_skip", reason="Tag input not found — skipping tags")


async def _confirm_publish(page: Page) -> str:
    """Click the final 'Publish now' button and return the story URL."""
    ready_btn = page.locator(
        'button:has-text("Publish now"), button:has-text("Publish story")'
    ).first
    await ready_btn.wait_for(state="visible", timeout=10_000)
    await ready_btn.click()
    await _human_pause(3.0)

    # Extract the URL of the published story
    current_url = page.url
    if "medium.com" in current_url and "/p/" in current_url:
        return current_url

    # Fallback: look for a success link
    try:
        story_link = page.locator('a[href*="medium.com"]').first
        url = await story_link.get_attribute("href", timeout=5_000)
        return url or current_url
    except PWTimeout:
        return current_url


async def _human_pause(seconds: float) -> None:
    """Simulate a human pause between actions."""
    await asyncio.sleep(seconds)


# ─────────────────────────────────────────────────────────────────────────────
# Article parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_article(content: str) -> tuple[str, str, list[str]]:
    """
    Extract title, body, and tags from the markdown content.

    Expected format:
      # Title Here
      <!-- Tags: tag1, tag2, tag3 -->
      ...body...
    """
    lines = content.splitlines()
    title  = "Untitled"
    tags: list[str] = []
    body_lines: list[str] = []

    import re
    skip_next_blank = False

    for line in lines:
        # Extract title from first H1
        if line.startswith("# ") and not line.startswith("## ") and title == "Untitled":
            title = line[2:].strip()
            continue

        # Extract tags from comment
        match = re.search(r"<!-- Tags: (.+?) -->", line)
        if match:
            tags = [t.strip() for t in match.group(1).split(",")]
            continue

        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return title, body, tags
