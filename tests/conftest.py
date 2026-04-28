"""
tests/conftest.py — Shared fixtures for the full test suite.

All external HTTP calls are intercepted by respx so no real API
credits are spent during testing.
"""
from __future__ import annotations

import json
import struct
import zlib

import pytest
import respx
from httpx import Response


# ── Tiny valid 1×1 transparent PNG ────────────────────────────────────────────

def _make_1x1_png() -> bytes:
    """Build a minimal valid PNG in-memory (8-byte header + 3 chunks)."""
    def chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)
    raw_row = b"\x00\xff\xff\xff"
    compressed = zlib.compress(raw_row)
    idat = chunk(b"IDAT", compressed)
    iend = chunk(b"IEND", b"")
    return header + ihdr + idat + iend


_PNG_BYTES: bytes = _make_1x1_png()

# ── Canonical fixture data ────────────────────────────────────────────────────

FAKE_ARTICLE_JSON: dict = {
    "title": "How Artificial Intelligence Is Reshaping Modern Healthcare Systems",
    "subtitle": "From diagnosis to drug discovery, AI is rewriting the rules of medicine in ways few anticipated",
    "body": (
        "Three years ago, a Stanford algorithm outperformed dermatologists at detecting "
        "skin cancer from photographs. That single result changed everything.\n\n"
        "## The Diagnostic Revolution\n\n"
        "### Computer Vision in Radiology\n\n"
        "Radiologists spend years learning to spot subtle anomalies on scans. "
        "[Recent research from MIT](https://mit.edu/ai-radiology) shows AI can flag "
        "pneumonia on chest X-rays with 94% sensitivity.\n\n"
        "## Drug Discovery at Machine Speed\n\n"
        "### Protein Folding Breakthroughs\n\n"
        "AlphaFold predicted the structure of virtually every known protein. "
        "[DeepMind's landmark paper](https://deepmind.com/alphafold) demonstrated "
        "this in under two years.\n\n"
        "## Personalised Treatment Paths\n\n"
        "### Genomic Medicine\n\n"
        "Each patient's genome is unique. AI models can now cross-reference genetic "
        "markers with drug efficacy databases in real time.\n\n"
        "## The Ethical Minefield\n\n"
        "### Bias in Training Data\n\n"
        "If the training data skews toward one demographic, outcomes suffer for others. "
        "[Harvard's ethics board report](https://harvard.edu/ai-ethics) details the stakes.\n\n"
        "## What this means for you\n\n"
        "- Ask your doctor whether AI-assisted diagnostics are available at your clinic\n"
        "- Check if your hospital participates in federated learning trials\n"
        "- Review your genetic privacy settings on health apps this week\n"
        "- Advocate for diverse training datasets in any AI health product you use\n\n"
        "The single most important insight here is that AI in healthcare is not a future "
        "promise — it is a present-tense reality that affects treatment decisions today. "
        "What worries you most about AI making medical decisions?"
    ),
    "image_prompt": (
        "A modern hospital corridor bathed in cool blue light, a physician reviewing "
        "holographic data overlays, calm and professional atmosphere, shallow depth of field."
    ),
    "tags": ["AI", "Healthcare", "Technology", "Medicine", "Innovation"],
}

FAKE_ARTICLE_JSON_STR: str = json.dumps(FAKE_ARTICLE_JSON)

FAKE_TAVILY_RESULT: dict = {
    "results": [
        {
            "title": "AI in Healthcare 2024",
            "url": "https://example-research.org/ai-health-2024",
            "content": "Artificial intelligence is transforming diagnostics and treatment planning.",
            "published_date": "2024-03-15",
        },
        {
            "title": "Machine Learning Drug Discovery",
            "url": "https://pharma-news.io/ml-drugs",
            "content": "ML models accelerate drug candidate screening from years to weeks.",
            "published_date": "2024-02-20",
        },
    ]
}


# ── respx fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_tavily(respx_mock: respx.MockRouter) -> respx.MockRouter:
    """Intercept Tavily search API calls."""
    respx_mock.post("https://api.tavily.com/search").mock(
        return_value=Response(200, json=FAKE_TAVILY_RESULT)
    )
    return respx_mock


@pytest.fixture
def mock_gemini(respx_mock: respx.MockRouter) -> respx.MockRouter:
    """Intercept Google Gemini generateContent endpoint."""
    gemini_response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": FAKE_ARTICLE_JSON_STR}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "modelVersion": "gemini-2.5-flash",
    }
    respx_mock.post(
        url__regex=r"https://generativelanguage\.googleapis\.com/.*/models/.*:generateContent.*"
    ).mock(return_value=Response(200, json=gemini_response))
    return respx_mock


@pytest.fixture
def mock_pollinations(respx_mock: respx.MockRouter) -> respx.MockRouter:
    """Intercept Pollinations.ai image generation endpoint."""
    respx_mock.get(url__regex=r"https://gen\.pollinations\.ai/image/.*").mock(
        return_value=Response(200, content=_PNG_BYTES)
    )
    return respx_mock


# ── Environment mock ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject dummy secrets so Settings validation passes without a real .env."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("SCHEDULER_DB_PATH", ":memory:")
    monkeypatch.setenv("JOBS_DB_PATH", ":memory:")
    # Clear Settings cache between tests
    import config
    config._settings_cache = None
