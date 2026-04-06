"""User keyword normalization and matching against RSS entries."""

from __future__ import annotations

from typing import Any

from news_telegram_bot.formatter import entry_match_text

_MAX_KEYWORD_LEN = 200
MAX_USER_KEYWORDS = 50


def validate_keyword_raw(raw: str) -> str | None:
    """Return error message if invalid, else None."""
    s = raw.strip()
    if not s:
        return "Keyword is empty."
    if len(s) > _MAX_KEYWORD_LEN:
        return f"Keyword is too long (max {_MAX_KEYWORD_LEN} characters)."
    return None


def normalize_keyword_phrase(raw: str) -> str:
    """Lowercase phrase for storage and matching; strips a single leading #."""
    s = raw.strip()
    if s.startswith("#"):
        s = s[1:].strip()
    return s.lower()


def keyword_display_label(raw: str) -> str:
    """Trimmed text shown when listing keywords (keeps # if user added it)."""
    return raw.strip()


def match_keyword_labels(
    entry: dict[str, Any],
    keywords: list[tuple[str, str]],
) -> list[str]:
    """
    Return display labels for keywords that match the entry (substring, case-insensitive).

    ``keywords`` is (normalized_phrase, display_label) per row from the database.
    """
    blob = entry_match_text(entry)
    if not blob:
        return []
    matched: list[str] = []
    seen: set[str] = set()
    for norm, label in keywords:
        if not norm or norm in seen:
            continue
        if norm in blob:
            matched.append(label)
            seen.add(norm)
    return matched
