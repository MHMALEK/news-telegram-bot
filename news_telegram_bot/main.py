"""Telegram bot entrypoint: RSS polling and notifications."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from news_telegram_bot.config import Settings, load_settings
from news_telegram_bot.db import Database
from news_telegram_bot.formatter import format_message
from news_telegram_bot.rss_client import collect_all_feeds, collect_feed, published_timestamp

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# Avoid logging Telegram API URLs (they embed the bot token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _register_user(db: Database, settings: Settings, telegram_chat_id: int) -> int:
    user_id = db.get_or_create_user(telegram_chat_id)
    db.seed_feeds_if_empty(user_id, settings.feed_urls)
    return user_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    _register_user(db, settings, chat_id)
    await update.message.reply_text(
        "RSS bot is running. Your chat id is:\n"
        f"<code>{chat_id}</code>\n\n"
        "Feeds default from RSS_FEED_URLS until you add your own.\n"
        "Commands: /feeds, /addfeed &lt;url&gt;, /removefeed &lt;url&gt;, /test",
        parse_mode=ParseMode.HTML,
    )


async def cmd_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    db: Database = context.application.bot_data["db"]
    settings: Settings = context.application.bot_data["settings"]
    uid = _register_user(db, settings, update.effective_chat.id)
    urls = db.get_feed_urls(uid)
    if not urls:
        await update.message.reply_text("No feeds yet. Use /addfeed <url>")
        return
    lines = "\n".join(f"• {u}" for u in urls)
    await update.message.reply_text(f"Your feeds ({len(urls)}):\n{lines}")


async def cmd_addfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text("Usage: /addfeed https://example.com/feed.xml")
        return
    db: Database = context.application.bot_data["db"]
    settings: Settings = context.application.bot_data["settings"]
    uid = _register_user(db, settings, update.effective_chat.id)
    if db.add_feed(uid, raw):
        await update.message.reply_text("Feed added.")
    else:
        await update.message.reply_text("That feed is already in your list (or invalid).")


async def cmd_removefeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text("Usage: /removefeed https://example.com/feed.xml")
        return
    db: Database = context.application.bot_data["db"]
    settings: Settings = context.application.bot_data["settings"]
    uid = _register_user(db, settings, update.effective_chat.id)
    if db.remove_feed(uid, raw):
        await update.message.reply_text("Feed removed.")
    else:
        await update.message.reply_text("No matching feed URL in your list.")


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, update.effective_chat.id)
    urls = db.get_feed_urls(uid)
    if not urls:
        await update.message.reply_text("No feeds in your list. Use /addfeed or set RSS_FEED_URLS.")
        return
    try:
        items = collect_all_feeds(urls)
    except Exception:
        logger.exception("collect_all_feeds failed in /test")
        await update.message.reply_text("Could not fetch feeds (see logs).")
        return
    if not items:
        await update.message.reply_text("No entries returned from your feeds.")
        return
    newest = max(items, key=lambda x: published_timestamp(x[1]))
    feed_title, entry, _eid = newest
    text = format_message(
        entry,
        feed_title=feed_title,
        brief_max_length=settings.brief_max_length,
    )
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<b>Test (not saved as seen)</b>\n\n" + text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except Exception as e:
        logger.exception("cmd_test send failed")
        await update.message.reply_text(f"Send failed: {e!s}")


async def _send_entry(
    bot,
    chat_id: int,
    settings: Settings,
    feed_title: str | None,
    entry: dict[str, Any],
    entry_id: str,
    db: Database,
    user_id: int,
) -> None:
    text = format_message(
        entry,
        feed_title=feed_title,
        brief_max_length=settings.brief_max_length,
    )
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )
    db.mark(user_id, entry_id)
    await asyncio.sleep(0.35)


async def poll_and_notify(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]

    sub_by_url = db.subscribers_by_feed_url()
    if not sub_by_url:
        logger.warning("No feed subscriptions in the database (add users and feeds)")
        return

    cache: dict[str, list[tuple[str | None, dict[str, Any], str]]] = {}
    for url in sorted(sub_by_url.keys()):
        try:
            cache[url] = collect_feed(url)
        except Exception:
            logger.exception("collect_feed failed for %s", url)
            cache[url] = []

    # Cold start: per user with no seen rows, mirror legacy bootstrap then mark all.
    for user_id, telegram_chat_id in db.list_users():
        if db.seen_count(user_id) > 0:
            continue
        urls = db.get_feed_urls(user_id)
        if not urls:
            continue
        merged: list[tuple[str | None, dict[str, Any], str]] = []
        for u in urls:
            merged.extend(cache.get(u, []))
        merged.sort(key=lambda x: published_timestamp(x[1]))
        if not merged:
            continue

        ids = [eid for _, _, eid in merged]
        n = settings.rss_bootstrap_count
        bootstrap_sent = 0
        if n > 0:
            by_time = sorted(
                merged,
                key=lambda x: published_timestamp(x[1]),
                reverse=True,
            )[:n]
            to_send = sorted(
                by_time,
                key=lambda x: published_timestamp(x[1]),
            )
            bootstrap_sent = len(to_send)
            for feed_title, entry, eid in to_send:
                try:
                    await _send_entry(
                        context.bot,
                        telegram_chat_id,
                        settings,
                        feed_title,
                        entry,
                        eid,
                        db,
                        user_id,
                    )
                except Exception:
                    logger.exception("Bootstrap send failed for %s", eid)
        db.mark_many(user_id, ids)
        logger.info(
            "Cold start user %s: seeded %d ids (bootstrap sent up to %d)",
            telegram_chat_id,
            len(ids),
            bootstrap_sent,
        )

    now = time.time()
    max_age = settings.rss_notify_max_age_hours
    cutoff: float | None = None
    if max_age is not None and max_age > 0:
        cutoff = now - max_age * 3600.0

    for feed_url, items in cache.items():
        subs = sub_by_url.get(feed_url, [])
        for feed_title, entry, stable_id in items:
            for user_id, telegram_chat_id in subs:
                if not db.is_new(user_id, stable_id):
                    continue
                ts = published_timestamp(entry)
                if cutoff is not None and ts > 0 and ts < cutoff:
                    db.mark(user_id, stable_id)
                    continue
                try:
                    await _send_entry(
                        context.bot,
                        telegram_chat_id,
                        settings,
                        feed_title,
                        entry,
                        stable_id,
                        db,
                        user_id,
                    )
                except Exception:
                    logger.exception("Failed to send message for entry %s", stable_id)


def main() -> None:
    try:
        settings = load_settings()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    db = Database(settings.database_path)
    migrated = db.migrate_json_state(settings.state_path, settings.chat_id)
    if migrated:
        logger.info("Legacy state migration imported %d row(s)", migrated)

    primary_uid = db.get_or_create_user(settings.chat_id)
    if settings.rss_sync_primary_feeds_from_env:
        db.replace_feeds(primary_uid, settings.feed_urls)
        logger.info(
            "RSS_SYNC_PRIMARY_FEEDS_FROM_ENV: primary user feeds set to %d URL(s) from env",
            len(settings.feed_urls),
        )
    else:
        db.seed_feeds_if_empty(primary_uid, settings.feed_urls)

    application = (
        Application.builder()
        .token(settings.bot_token)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("feeds", cmd_feeds))
    application.add_handler(CommandHandler("addfeed", cmd_addfeed))
    application.add_handler(CommandHandler("removefeed", cmd_removefeed))
    application.add_handler(CommandHandler("test", cmd_test))

    if application.job_queue is None:
        logger.error(
            "Job queue unavailable. Install with: pip install 'python-telegram-bot[job-queue]'"
        )
        sys.exit(1)

    first_delay = min(10, max(2, settings.poll_interval_seconds))
    application.job_queue.run_repeating(
        poll_and_notify,
        interval=settings.poll_interval_seconds,
        first=first_delay,
        name="rss_poll",
    )

    n_feeds = len(db.get_feed_urls(primary_uid))
    logger.info(
        "Starting bot: primary chat_id=%s, db=%s, %d feed(s) for primary user, "
        "every %s s, bootstrap_count=%s, notify_max_age_hours=%s",
        settings.chat_id,
        settings.database_path,
        n_feeds,
        settings.poll_interval_seconds,
        settings.rss_bootstrap_count,
        settings.rss_notify_max_age_hours,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
