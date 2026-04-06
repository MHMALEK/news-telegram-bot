"""Fetch and normalize RSS/Atom entries."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import feedparser
import httpx

from news_telegram_bot.formatter import entry_id

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_FETCH_RETRIES = 3


def _fetch_feed_bytes(feed_url: str) -> bytes:
    last_err: BaseException | None = None
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"}
    for attempt in range(_FETCH_RETRIES):
        try:
            with httpx.Client(
                timeout=30.0,
                follow_redirects=True,
                http2=False,
            ) as client:
                response = client.get(feed_url, headers=headers)
                response.raise_for_status()
                return response.content
        except (httpx.HTTPError, OSError) as e:
            last_err = e
            if attempt + 1 < _FETCH_RETRIES:
                time.sleep(0.4 * (attempt + 1))
    assert last_err is not None
    raise last_err


def published_timestamp(entry: dict[str, Any]) -> float:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6]).timestamp()
            except (TypeError, ValueError):
                continue
    return 0.0


def validate_feed_url(feed_url: str) -> tuple[bool, str | None, str | None]:
    """
    Check that ``feed_url`` returns a parseable RSS/Atom feed with something to read.

    Returns ``(ok, error_message, channel_title)``.
    """
    try:
        body = _fetch_feed_bytes(feed_url)
    except Exception as e:
        return False, f"Could not download: {e}", None

    parsed = feedparser.parse(body)
    entries = list(parsed.entries or [])
    channel_title: str | None = None
    if parsed.feed:
        t = parsed.feed.get("title")
        if t:
            channel_title = str(t).strip() or None
        if not channel_title:
            link = parsed.feed.get("link")
            if link:
                channel_title = str(link).strip() or None

    if not entries and not channel_title:
        return (
            False,
            "Not a usable RSS or Atom feed (no channel and no entries).",
            None,
        )

    return True, None, channel_title


def sample_newest_entry(
    feed_url: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Return ``(feed_title, entry)`` for the newest item, or ``(feed_title, None)`` if empty.
    """
    feed_title, entries = fetch_entries(feed_url)
    if not entries:
        return feed_title, None
    newest = max(entries, key=lambda e: published_timestamp(e))
    return feed_title, newest


def fetch_entries(feed_url: str) -> tuple[str | None, list[dict[str, Any]]]:
    try:
        body = _fetch_feed_bytes(feed_url)
    except Exception as e:
        logger.warning("Failed to fetch feed %s after %d tries: %s", feed_url, _FETCH_RETRIES, e)
        return None, []

    parsed = feedparser.parse(body)
    if getattr(parsed, "bozo", False) and parsed.bozo_exception:
        logger.warning(
            "Feed parse warning for %s: %s",
            feed_url,
            parsed.bozo_exception,
        )
    feed_title = None
    if parsed.feed:
        ft = parsed.feed.get("title")
        if ft:
            feed_title = str(ft)
    entries = list(parsed.entries or [])
    return feed_title, entries


def collect_feed(
    feed_url: str,
) -> list[tuple[str | None, dict[str, Any], str]]:
    """Return list of (feed_title, entry, stable_id) for one feed URL."""
    out: list[tuple[str | None, dict[str, Any], str]] = []
    try:
        feed_title, entries = fetch_entries(feed_url)
    except Exception:
        logger.exception("Failed to fetch feed %s", feed_url)
        return []
    for entry in entries:
        eid = entry_id(entry)
        if not eid:
            logger.debug("Skipping entry without id from %s", feed_url)
            continue
        stable = f"{feed_url}::{eid}"
        out.append((feed_title, entry, stable))
    out.sort(key=lambda x: published_timestamp(x[1]))
    return out


def collect_all_feeds(
    feed_urls: list[str],
) -> list[tuple[str | None, dict[str, Any], str]]:
    """Return list of (feed_title, entry, stable_id) for all feeds."""
    out: list[tuple[str | None, dict[str, Any], str]] = []
    for url in feed_urls:
        out.extend(collect_feed(url))
    out.sort(key=lambda x: published_timestamp(x[1]))
    return out
