"""Build Telegram HTML messages from feed entries."""

from __future__ import annotations

import html
import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")


def _as_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        inner = val.get("value")
        if inner is not None:
            return str(inner).strip()
        return str(val.get("href") or "").strip()
    return str(val).strip()


def strip_html(text: str) -> str:
    if not text:
        return ""
    plain = _TAG_RE.sub(" ", text)
    plain = html.unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()


def entry_id(entry: dict[str, Any]) -> str:
    for key in ("id", "guid", "link"):
        text = _as_text(entry.get(key))
        if text:
            return text
    return _as_text(entry.get("title"))


def brief_from_entry(entry: dict[str, Any], max_len: int) -> str:
    for key in ("summary", "description", "subtitle"):
        raw = entry.get(key)
        if raw:
            text = strip_html(str(raw))
            if text:
                if len(text) > max_len:
                    return text[: max_len - 1].rstrip() + "…"
                return text
    title = entry.get("title")
    return strip_html(str(title)) if title else ""


def format_message(
    entry: dict[str, Any],
    *,
    feed_title: str | None,
    brief_max_length: int,
) -> str:
    title = strip_html(str(entry.get("title", "Untitled")))
    link = str(entry.get("link", "")).strip()
    brief = brief_from_entry(entry, brief_max_length)

    lines: list[str] = [f"<b>{html.escape(title, quote=False)}</b>"]
    if brief:
        lines.append(html.escape(brief, quote=False))
    if link:
        lines.append(f'<a href="{html.escape(link, quote=True)}">Read article</a>')

    meta_parts: list[str] = []
    if feed_title:
        meta_parts.append(html.escape(strip_html(feed_title), quote=False))
    author = entry.get("author")
    if author:
        meta_parts.append(html.escape(strip_html(str(author)), quote=False))
    if meta_parts:
        lines.append(f"<i>{' · '.join(meta_parts)}</i>")

    return "\n\n".join(lines)
