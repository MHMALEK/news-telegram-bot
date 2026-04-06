"""Load settings from environment."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: int
    feed_urls: list[str]
    poll_interval_seconds: int
    brief_max_length: int
    database_path: Path
    state_path: Path
    # On first run, send this many newest items, then mark the full feed as seen. 0 = silent seed.
    rss_bootstrap_count: int
    # Only notify for items with a parseable published date newer than this many hours (None = no limit).
    rss_notify_max_age_hours: float | None
    # If True, primary chat (TELEGRAM_CHAT_ID) subscriptions are replaced with RSS_FEED_URLS on startup.
    rss_sync_primary_feeds_from_env: bool


def _parse_feed_urls(raw: str) -> list[str]:
    return [u.strip() for u in raw.split(",") if u.strip()]


def load_settings() -> Settings:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    feeds_raw = os.environ.get(
        "RSS_FEED_URLS",
        "https://feeds.bbci.co.uk/news/rss.xml",
    ).strip()

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not chat_raw:
        raise ValueError("TELEGRAM_CHAT_ID is required")

    try:
        chat_id = int(chat_raw)
    except ValueError as e:
        raise ValueError("TELEGRAM_CHAT_ID must be an integer") from e

    feed_urls = _parse_feed_urls(feeds_raw)
    if not feed_urls:
        raise ValueError("RSS_FEED_URLS must list at least one URL")

    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
    brief_max = int(os.environ.get("BRIEF_MAX_LENGTH", "400"))
    database_path = Path(os.environ.get("DATABASE_PATH", "news_bot.db")).resolve()
    state_path = Path(os.environ.get("STATE_PATH", "state.json")).resolve()
    bootstrap = int(os.environ.get("RSS_BOOTSTRAP_COUNT", "1"))
    bootstrap = max(0, min(50, bootstrap))

    max_age_raw = os.environ.get("RSS_NOTIFY_MAX_AGE_HOURS", "72").strip().lower()
    if max_age_raw in ("", "0", "off", "false", "none"):
        rss_notify_max_age_hours: float | None = None
    else:
        try:
            rss_notify_max_age_hours = float(max_age_raw)
        except ValueError as e:
            raise ValueError(
                "RSS_NOTIFY_MAX_AGE_HOURS must be a number, or 0/off to disable"
            ) from e

    rss_sync_primary_feeds_from_env = os.environ.get(
        "RSS_SYNC_PRIMARY_FEEDS_FROM_ENV", ""
    ).strip().lower() in ("1", "true", "yes")

    return Settings(
        bot_token=token,
        chat_id=chat_id,
        feed_urls=feed_urls,
        poll_interval_seconds=max(10, interval),
        brief_max_length=max(80, brief_max),
        database_path=database_path,
        state_path=state_path,
        rss_bootstrap_count=bootstrap,
        rss_notify_max_age_hours=rss_notify_max_age_hours,
        rss_sync_primary_feeds_from_env=rss_sync_primary_feeds_from_env,
    )
