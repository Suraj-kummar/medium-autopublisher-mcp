"""
scheduler/fingerprint.py — SHA-256 job deduplication fingerprint.

A 16-char hex derived from topic + scheduled time guarantees that
the same article topic scheduled at the same moment never creates
two independent jobs — even if the caller retries.
"""
from __future__ import annotations

import hashlib


def make_fingerprint(topic: str, publish_at: str) -> str:
    """
    Return a 16-character hex fingerprint for (topic, publish_at).

    Args:
        topic:      Raw topic string (will be normalised: stripped + lowercased).
        publish_at: ISO 8601 datetime string (timezone-aware recommended).

    Returns:
        First 16 hex characters of SHA-256(normalised_topic|publish_at).
    """
    raw = f"{topic.strip().lower()}|{publish_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
