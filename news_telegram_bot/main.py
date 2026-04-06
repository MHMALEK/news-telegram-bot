"""Telegram bot entrypoint: RSS polling and notifications."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
import time
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from news_telegram_bot.config import Settings, load_settings
from news_telegram_bot.db import Database
from news_telegram_bot.explore_catalog import ExploreCatalog
from news_telegram_bot.formatter import (
    format_digest_messages,
    format_keyword_alert,
    format_message,
)
from news_telegram_bot.keywords import match_keyword_labels
from news_telegram_bot.rss_client import (
    collect_all_feeds,
    collect_feed,
    published_timestamp,
    sample_newest_entry,
    validate_feed_url,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# Avoid logging Telegram API URLs (they embed the bot token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _match_keyword_labels(db: Database, user_id: int, entry: dict[str, Any]) -> list[str]:
    rows = db.list_keywords_for_match(user_id)
    if not rows:
        return []
    return match_keyword_labels(entry, rows)


def _register_user(db: Database, settings: Settings, telegram_chat_id: int) -> int:
    user_id = db.get_or_create_user(telegram_chat_id)
    db.seed_feeds_if_empty(user_id, settings.feed_urls)
    return user_id


def _feeds_panel_html(urls: list[str]) -> str:
    if not urls:
        return (
            "<b>Your feeds</b>\n\n"
            "No feeds yet. Use <b>/explore</b> or <code>/addfeed &lt;url&gt;</code>."
        )
    lines = "\n".join(
        f"{i + 1}. <code>{html.escape(u)}</code>" for i, u in enumerate(urls)
    )
    return (
        f"<b>Your feeds</b> ({len(urls)})\n\n"
        f"{lines}\n\n"
        "<i>Open in the browser or remove without copying the URL.</i>"
    )


def _feeds_keyboard(urls: list[str]) -> InlineKeyboardMarkup | None:
    if not urls:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for i, u in enumerate(urls):
        rows.append(
            [
                InlineKeyboardButton("🔗 Open", url=u),
                InlineKeyboardButton("🗑 Remove", callback_data=f"frm_{i}"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def _keywords_panel_html(labels: list[str]) -> str:
    if not labels:
        lines = "You have no keywords yet."
    else:
        lines = "\n".join(
            f"{i + 1}. {html.escape(l, quote=False)}" for i, l in enumerate(labels)
        )
    return (
        "<b>🔔 Keywords</b>\n\n"
        "When a new item matches a keyword, you get a <b>Keyword alert</b> "
        "instead of the usual notification.\n\n"
        f"{lines}\n\n"
        "<code>/keywords add &lt;phrase&gt;</code>\n"
        "<code>/keywords remove &lt;phrase&gt;</code>\n"
        "<code>/keywords clear</code>"
    )


def _keywords_keyboard(labels: list[str]) -> InlineKeyboardMarkup | None:
    if not labels:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for i, lab in enumerate(labels):
        btn = lab.replace("\n", " ")
        if len(btn) > 40:
            btn = btn[:37] + "…"
        rows.append([InlineKeyboardButton(f"🗑 {btn}", callback_data=f"kwm_{i}")])
    return InlineKeyboardMarkup(rows)


def _digest_panel_html(uid: int, db: Database, settings: Settings) -> str:
    on = db.get_digest_enabled(uid)
    return (
        "<b>📋 Daily digest</b>\n\n"
        f"Status: <b>{'on' if on else 'off'}</b>\n"
        "When on, new items are queued and sent once per day around "
        f"{settings.digest_time_hour:02d}:{settings.digest_time_minute:02d} "
        f"{html.escape(settings.digest_timezone)}.\n\n"
        "Use the buttons below or <code>/digest on</code> / <code>/digest off</code>."
    )


def _digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Digest on", callback_data="dig_on"),
                InlineKeyboardButton("⏹ Digest off", callback_data="dig_off"),
            ]
        ]
    )


def _help_text(settings: Settings) -> str:
    return (
        "<b>❓ Help — RSS News Bot</b>\n\n"
        "<b>Feeds</b>\n"
        "• /feeds — list with Open / Remove\n"
        "• /explore — add by category\n"
        "• /addfeed &lt;url&gt; · /removefeed &lt;url&gt;\n"
        "• /testfeed &lt;url&gt; — preview without saving\n\n"
        "<b>Alerts</b>\n"
        "• /keywords — keyword alerts when text matches\n"
        "• /digest — daily bundle (polling unchanged)\n\n"
        "<b>Misc</b>\n"
        "• /test · /testdigest — previews\n\n"
        f"<i>Digest time: {settings.digest_time_hour:02d}:{settings.digest_time_minute:02d} "
        f"{html.escape(settings.digest_timezone)}</i>"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    uid = _register_user(db, settings, chat_id)
    digest_on = db.get_digest_enabled(uid)
    n_feeds = len(db.get_feed_urls(uid))
    n_kw = len(db.list_keyword_labels(uid))

    menu_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📰 Feeds", callback_data="menu_feeds"),
                InlineKeyboardButton("🔎 Explore", callback_data="menu_explore"),
            ],
            [
                InlineKeyboardButton("🔔 Keywords", callback_data="menu_keywords"),
                InlineKeyboardButton("📋 Digest", callback_data="menu_digest"),
            ],
            [InlineKeyboardButton("❓ Help", callback_data="menu_help")],
        ]
    )
    await update.message.reply_text(
        "<b>RSS News Bot</b>\n\n"
        f"Chat <code>{chat_id}</code>\n"
        f"Feeds: <b>{n_feeds}</b> · Digest: <b>{'on' if digest_on else 'off'}</b> · "
        f"Keywords: <b>{n_kw}</b>\n\n"
        "Defaults come from <code>RSS_FEED_URLS</code> until you add your own feeds.\n"
        "Use the buttons below or type /help for every command.",
        reply_markup=menu_kb,
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    _register_user(context.application.bot_data["db"], settings, update.effective_chat.id)
    await update.message.reply_text(
        _help_text(settings),
        parse_mode=ParseMode.HTML,
    )


def _explore_categories_kb(catalog: ExploreCatalog) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    slugs = catalog.category_order()
    for i in range(0, len(slugs), 2):
        row: list[InlineKeyboardButton] = []
        for j in range(i, min(i + 2, len(slugs))):
            s = slugs[j]
            t = catalog.get_category_title(s) or s
            row.append(InlineKeyboardButton(t, callback_data=f"exp_cat_{s}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _explore_feeds_kb(catalog: ExploreCatalog, slug: str) -> InlineKeyboardMarkup | None:
    feeds = catalog.get_feeds(slug)
    if not feeds:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for i, cf in enumerate(feeds):
        label = f"+ {cf.title}"
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"exp_add_{slug}_{i}")]
        )
    rows.append([InlineKeyboardButton("« Back", callback_data="exp_back")])
    return InlineKeyboardMarkup(rows)


async def cmd_explore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    catalog: ExploreCatalog = context.application.bot_data["explore_catalog"]
    _register_user(db, settings, update.effective_chat.id)
    if not catalog:
        await update.message.reply_text(
            "Explore catalog is empty. Edit "
            "<code>explore_catalog.json</code> (or set <code>EXPLORE_CATALOG_PATH</code>) "
            "and restart the bot.",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.message.reply_text(
        "<b>Explore</b> — pick a category. Feeds are checked before they are added.",
        reply_markup=_explore_categories_kb(catalog),
        parse_mode=ParseMode.HTML,
    )


async def callback_explore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    catalog: ExploreCatalog = context.application.bot_data["explore_catalog"]
    chat_id = query.message.chat.id
    uid = _register_user(db, settings, chat_id)
    data = query.data or ""

    if not catalog:
        await query.answer(text="Explore catalog not loaded.", show_alert=True)
        return

    if data == "exp_back":
        await query.answer()
        try:
            await query.edit_message_text(
                text="<b>Explore</b> — pick a category.",
                reply_markup=_explore_categories_kb(catalog),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.warning("Explore back edit failed: %s", e)
            await context.bot.send_message(
                chat_id=chat_id,
                text="<b>Explore</b> — pick a category.",
                reply_markup=_explore_categories_kb(catalog),
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("exp_cat_"):
        slug = data[8:]
        title = catalog.get_category_title(slug)
        kb = _explore_feeds_kb(catalog, slug)
        if not title or kb is None:
            await query.answer(text="Unknown category.", show_alert=True)
            return
        await query.answer()
        body = (
            f"<b>{html.escape(title)}</b>\n\n"
            "Tap <b>+ name</b> to add. The feed is fetched and checked before it is saved."
        )
        try:
            await query.edit_message_text(
                text=body,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as e:
            logger.warning("Explore category edit failed: %s", e)
            await context.bot.send_message(
                chat_id=chat_id,
                text=body,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("exp_add_"):
        rest = data[len("exp_add_") :]
        try:
            slug, idx_str = rest.rsplit("_", 1)
            idx = int(idx_str)
        except ValueError:
            await query.answer(text="Bad button data.", show_alert=True)
            return
        cf = catalog.get_feed(slug, idx)
        if cf is None:
            await query.answer(text="Unknown feed.", show_alert=True)
            return
        ok, err, _ch = validate_feed_url(cf.url)
        if not ok:
            await query.answer(
                text=(err or "Invalid feed")[:200],
                show_alert=True,
            )
            return
        if not db.add_feed(uid, cf.url):
            await query.answer(text="Already in your list.", show_alert=True)
            return
        await query.answer(text="Added.", show_alert=False)
        ft, ent = sample_newest_entry(cf.url)
        if ent:
            preview = format_message(
                ent,
                feed_title=ft,
                brief_max_length=settings.brief_max_length,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ <b>Added from Explore</b> — {html.escape(cf.title)}\n\n"
                    f"<i>Preview (not saved as seen)</i>\n\n{preview}"
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Added <b>{html.escape(cf.title)}</b>. "
                    "Feed is valid but has no sample entry yet."
                ),
                parse_mode=ParseMode.HTML,
            )
        return

    await query.answer()


async def cmd_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    db: Database = context.application.bot_data["db"]
    settings: Settings = context.application.bot_data["settings"]
    uid = _register_user(db, settings, update.effective_chat.id)
    urls = db.get_feed_urls(uid)
    text = _feeds_panel_html(urls)
    kb = _feeds_keyboard(urls)
    await update.message.reply_text(
        text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def cmd_addfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text(
            "Usage: /addfeed https://example.com/feed.xml\n"
            "Or use /testfeed with the same URL to try without adding."
        )
        return
    db: Database = context.application.bot_data["db"]
    settings: Settings = context.application.bot_data["settings"]
    uid = _register_user(db, settings, update.effective_chat.id)
    ok, err, channel = validate_feed_url(raw)
    if not ok:
        await update.message.reply_text(
            f"Not a valid RSS/Atom feed: {err}",
        )
        return
    if not db.add_feed(uid, raw):
        await update.message.reply_text("That feed is already in your list.")
        return
    ft, ent = sample_newest_entry(raw)
    if ent:
        preview = format_message(
            ent,
            feed_title=ft,
            brief_max_length=settings.brief_max_length,
        )
        await update.message.reply_text(
            f"✅ <b>Added</b> — {html.escape(channel or 'feed')}\n\n"
            f"<i>Preview (not saved as seen)</i>\n\n{preview}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    else:
        await update.message.reply_text(
            f"✅ Added ({html.escape(channel or 'feed')}). "
            "Feed is valid but has no entries yet.",
            parse_mode=ParseMode.HTML,
        )


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
            text=(
                "<b>Instant preview (test)</b>\n"
                "<i>Same format as live notifications. Not saved as seen.</i>\n\n"
                + text
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except Exception as e:
        logger.exception("cmd_test send failed")
        await update.message.reply_text(f"Send failed: {e!s}")


async def cmd_testdigest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, update.effective_chat.id)
    urls = db.get_feed_urls(uid)
    if not urls:
        await update.message.reply_text("No feeds in your list.")
        return
    try:
        items = collect_all_feeds(urls)
    except Exception:
        logger.exception("collect_all_feeds failed in /testdigest")
        await update.message.reply_text("Could not fetch feeds (see logs).")
        return
    if not items:
        await update.message.reply_text("No entries returned from your feeds.")
        return
    newest = max(items, key=lambda x: published_timestamp(x[1]))
    feed_title, entry, eid = newest
    batch = [(feed_title, entry, eid, None)]
    texts = format_digest_messages(
        batch,
        brief_max_length=settings.brief_max_length,
    )
    if not texts:
        await update.message.reply_text("Could not build digest preview.")
        return
    body = texts[0].replace(
        "<b>Daily digest</b>",
        "<b>Digest preview (test)</b>",
        1,
    )
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                body
                + "\n\n<i>Same layout as the real daily digest. Not saved as seen.</i>"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("cmd_testdigest send failed")
        await update.message.reply_text(f"Send failed: {e!s}")


async def cmd_testfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    _register_user(db, settings, update.effective_chat.id)
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text(
            "Usage: /testfeed https://example.com/feed.xml\n"
            "Checks the URL and sends one sample without adding it."
        )
        return
    ok, err, channel = validate_feed_url(raw)
    if not ok:
        await update.message.reply_text(f"Not a valid RSS/Atom feed: {err}")
        return
    ft, ent = sample_newest_entry(raw)
    if not ent:
        await update.message.reply_text(
            f"✅ Feed looks valid ({html.escape(channel or 'feed')}) "
            "but there are no entries to preview.",
            parse_mode=ParseMode.HTML,
        )
        return
    text = format_message(
        ent,
        feed_title=ft,
        brief_max_length=settings.brief_max_length,
    )
    await update.message.reply_text(
        "<b>URL test (not added, not saved)</b>\n\n" + text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, update.effective_chat.id)
    args = context.args or []
    if not args:
        labels = db.list_keyword_labels(uid)
        await update.message.reply_text(
            _keywords_panel_html(labels),
            reply_markup=_keywords_keyboard(labels),
            parse_mode=ParseMode.HTML,
        )
        return
    sub = args[0].lower()
    if sub == "add":
        phrase = " ".join(args[1:]).strip()
        if not phrase:
            await update.message.reply_text("Usage: /keywords add <keyword or phrase>")
            return
        ok, err = db.add_keyword(uid, phrase)
        if not ok:
            await update.message.reply_text(err or "Could not add keyword.")
            return
        await update.message.reply_text(
            f"Added keyword <b>{html.escape(phrase, quote=False)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if sub == "remove":
        phrase = " ".join(args[1:]).strip()
        if not phrase:
            await update.message.reply_text("Usage: /keywords remove <keyword or phrase>")
            return
        if db.remove_keyword(uid, phrase):
            await update.message.reply_text("Keyword removed.")
        else:
            await update.message.reply_text("No matching keyword in your list.")
        return
    if sub == "clear":
        n = db.clear_keywords(uid)
        await update.message.reply_text(f"Removed {n} keyword(s).")
        return
    await update.message.reply_text(
        "Unknown subcommand. Try /keywords for help."
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, update.effective_chat.id)
    raw = (context.args[0].lower() if context.args else "").strip()

    if raw in ("on", "true", "1", "yes"):
        db.set_digest_enabled(uid, True)
        await update.message.reply_text(
            "Daily digest <b>enabled</b>. New items are queued and sent once per day around "
            f"{settings.digest_time_hour:02d}:{settings.digest_time_minute:02d} "
            f"{html.escape(settings.digest_timezone)}.",
            parse_mode=ParseMode.HTML,
        )
        return
    if raw in ("off", "false", "0", "no"):
        db.set_digest_enabled(uid, False)
        await update.message.reply_text(
            "Daily digest <b>disabled</b>. New items are sent as they appear.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        _digest_panel_html(uid, db, settings),
        reply_markup=_digest_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def callback_feed_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, query.message.chat.id)
    data = query.data or ""
    m = re.match(r"^frm_(\d+)$", data)
    if not m:
        await query.answer()
        return
    idx = int(m.group(1))
    urls = db.get_feed_urls(uid)
    if idx < 0 or idx >= len(urls):
        await query.answer(text="This list is outdated. Open /feeds again.", show_alert=True)
        return
    removed = urls[idx]
    if not db.remove_feed(uid, removed):
        await query.answer(text="Could not remove feed.", show_alert=True)
        return
    await query.answer(text="Removed.")
    urls = db.get_feed_urls(uid)
    text = _feeds_panel_html(urls)
    kb = _feeds_keyboard(urls)
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        logger.warning("Feed list edit failed: %s", e)
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


async def callback_keyword_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, query.message.chat.id)
    data = query.data or ""
    m = re.match(r"^kwm_(\d+)$", data)
    if not m:
        await query.answer()
        return
    idx = int(m.group(1))
    labels = db.list_keyword_labels(uid)
    if idx < 0 or idx >= len(labels):
        await query.answer(text="This list is outdated. Open /keywords again.", show_alert=True)
        return
    if not db.remove_keyword(uid, labels[idx]):
        await query.answer(text="Could not remove keyword.", show_alert=True)
        return
    await query.answer(text="Removed.")
    labels = db.list_keyword_labels(uid)
    text = _keywords_panel_html(labels)
    kb = _keywords_keyboard(labels)
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        logger.warning("Keyword list edit failed: %s", e)
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


async def callback_digest_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    uid = _register_user(db, settings, query.message.chat.id)
    data = query.data or ""
    if data == "dig_on":
        db.set_digest_enabled(uid, True)
        await query.answer(text="Digest on.")
    elif data == "dig_off":
        db.set_digest_enabled(uid, False)
        await query.answer(text="Digest off.")
    else:
        await query.answer()
        return
    text = _digest_panel_html(uid, db, settings)
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=_digest_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        logger.warning("Digest panel edit failed: %s", e)


async def callback_bot_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    catalog: ExploreCatalog = context.application.bot_data["explore_catalog"]
    chat_id = query.message.chat.id
    uid = _register_user(db, settings, chat_id)
    data = query.data or ""

    if data == "menu_help":
        await query.answer()
        await context.bot.send_message(
            chat_id=chat_id,
            text=_help_text(settings),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu_feeds":
        await query.answer()
        urls = db.get_feed_urls(uid)
        await context.bot.send_message(
            chat_id=chat_id,
            text=_feeds_panel_html(urls),
            reply_markup=_feeds_keyboard(urls),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu_keywords":
        await query.answer()
        labels = db.list_keyword_labels(uid)
        await context.bot.send_message(
            chat_id=chat_id,
            text=_keywords_panel_html(labels),
            reply_markup=_keywords_keyboard(labels),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu_digest":
        await query.answer()
        await context.bot.send_message(
            chat_id=chat_id,
            text=_digest_panel_html(uid, db, settings),
            reply_markup=_digest_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "menu_explore":
        if not catalog:
            await query.answer(
                text="Explore catalog not loaded on the server.",
                show_alert=True,
            )
            return
        await query.answer()
        await context.bot.send_message(
            chat_id=chat_id,
            text="<b>Explore</b> — pick a category. Feeds are checked before they are added.",
            reply_markup=_explore_categories_kb(catalog),
            parse_mode=ParseMode.HTML,
        )
        return

    await query.answer()


async def _send_entry(
    bot,
    chat_id: int,
    settings: Settings,
    feed_title: str | None,
    entry: dict[str, Any],
    entry_id: str,
    db: Database,
    user_id: int,
    keyword_labels: list[str] | None = None,
) -> None:
    if keyword_labels:
        text = format_keyword_alert(
            entry,
            feed_title=feed_title,
            brief_max_length=settings.brief_max_length,
            matched_labels=keyword_labels,
        )
    else:
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


async def _deliver_new_item(
    bot,
    chat_id: int,
    settings: Settings,
    feed_title: str | None,
    entry: dict[str, Any],
    stable_id: str,
    db: Database,
    user_id: int,
) -> None:
    """Either send immediately or queue for the daily digest, and mark seen."""
    matched = _match_keyword_labels(db, user_id, entry)
    kw = matched if matched else None
    if db.get_digest_enabled(user_id):
        db.enqueue_digest_item(user_id, stable_id, feed_title, entry, kw)
        db.mark(user_id, stable_id)
    else:
        await _send_entry(
            bot,
            chat_id,
            settings,
            feed_title,
            entry,
            stable_id,
            db,
            user_id,
            keyword_labels=kw,
        )


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]
    for user_id, telegram_chat_id in db.list_users():
        if not db.get_digest_enabled(user_id):
            continue
        batch = db.list_digest_queue(user_id)
        if not batch:
            continue
        batch.sort(key=lambda x: published_timestamp(x[1]))
        texts = format_digest_messages(
            batch,
            brief_max_length=settings.brief_max_length,
        )
        try:
            for text in texts:
                await context.bot.send_message(
                    chat_id=telegram_chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(0.35)
        except Exception:
            logger.exception(
                "Digest send failed for chat_id=%s (queue kept for retry)",
                telegram_chat_id,
            )
            continue
        db.clear_digest_queue(user_id)


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
                    matched = _match_keyword_labels(db, user_id, entry)
                    kw = matched if matched else None
                    if db.get_digest_enabled(user_id):
                        db.enqueue_digest_item(
                            user_id, eid, feed_title, entry, kw
                        )
                    else:
                        await _send_entry(
                            context.bot,
                            telegram_chat_id,
                            settings,
                            feed_title,
                            entry,
                            eid,
                            db,
                            user_id,
                            keyword_labels=kw,
                        )
                except Exception:
                    logger.exception("Bootstrap send failed for %s", eid)
        db.mark_many(user_id, ids)
        logger.info(
            "Cold start user %s: seeded %d ids (bootstrap sent up to %d, digest=%s)",
            telegram_chat_id,
            len(ids),
            bootstrap_sent,
            db.get_digest_enabled(user_id),
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
                    await _deliver_new_item(
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

    explore_catalog = ExploreCatalog.from_path(settings.explore_catalog_path)
    if explore_catalog:
        logger.info(
            "Explore catalog: %d categories from %s",
            len(explore_catalog.category_order()),
            settings.explore_catalog_path,
        )
    else:
        logger.warning(
            "Explore catalog missing or invalid: %s",
            settings.explore_catalog_path,
        )

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
    application.bot_data["explore_catalog"] = explore_catalog

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("feeds", cmd_feeds))
    application.add_handler(CommandHandler("explore", cmd_explore))
    application.add_handler(CommandHandler("addfeed", cmd_addfeed))
    application.add_handler(CommandHandler("testfeed", cmd_testfeed))
    application.add_handler(CommandHandler("removefeed", cmd_removefeed))
    application.add_handler(CommandHandler("keywords", cmd_keywords))
    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_handler(CommandHandler("test", cmd_test))
    application.add_handler(CommandHandler("testdigest", cmd_testdigest))
    application.add_handler(CallbackQueryHandler(callback_feed_actions, pattern=r"^frm_\d+$"))
    application.add_handler(CallbackQueryHandler(callback_keyword_actions, pattern=r"^kwm_\d+$"))
    application.add_handler(CallbackQueryHandler(callback_digest_menu, pattern=r"^dig_(on|off)$"))
    application.add_handler(CallbackQueryHandler(callback_bot_menu, pattern=r"^menu_"))
    application.add_handler(CallbackQueryHandler(callback_explore, pattern=r"^exp_"))

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

    digest_when = dt_time(
        hour=settings.digest_time_hour,
        minute=settings.digest_time_minute,
        tzinfo=ZoneInfo(settings.digest_timezone),
    )
    application.job_queue.run_daily(
        send_daily_digest,
        time=digest_when,
        name="daily_digest",
    )
    logger.info(
        "Daily digest job at %02d:%02d %s (per-user: /digest on)",
        settings.digest_time_hour,
        settings.digest_time_minute,
        settings.digest_timezone,
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
