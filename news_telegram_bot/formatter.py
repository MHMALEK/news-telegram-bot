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


def entry_match_text(entry: dict[str, Any]) -> str:
    """
    Lowercase plain text from title, summary, and category/tag terms for keyword matching.
    """
    parts: list[str] = []
    title = entry.get("title")
    if title:
        parts.append(strip_html(str(title)))
    for key in ("summary", "description", "subtitle"):
        raw = entry.get(key)
        if raw:
            parts.append(strip_html(str(raw)))
    tags = entry.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                term = t.get("term")
                if term:
                    parts.append(strip_html(str(term)))
    return " ".join(parts).lower()


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


def format_keyword_alert(
    entry: dict[str, Any],
    *,
    feed_title: str | None,
    brief_max_length: int,
    matched_labels: list[str],
) -> str:
    """Same body as ``format_message`` with a distinct keyword alert header."""
    labels = ", ".join(html.escape(l, quote=False) for l in matched_labels)
    header = f"🔔 <b>Keyword alert</b> — <b>{labels}</b>"
    body = format_message(
        entry,
        feed_title=feed_title,
        brief_max_length=brief_max_length,
    )
    return f"{header}\n\n{body}"


# Telegram hard limit; keep margin for HTML expansion.
_DIGEST_CHUNK_SAFE = 3800


def format_digest_messages(
    items: list[tuple[str | None, dict[str, Any], str, list[str] | None]],
    *,
    brief_max_length: int,
) -> list[str]:
    """
    Build one or more HTML messages for a daily digest.
    ``items`` are (feed_title, entry, stable_id, keyword_labels_or_none).
    """
    if not items:
        return []

    brief = min(brief_max_length, 280)
    blocks: list[str] = []
    for feed_title, entry, _eid, kw in items:
        if kw:
            blocks.append(
                format_keyword_alert(
                    entry,
                    feed_title=feed_title,
                    brief_max_length=brief,
                    matched_labels=kw,
                )
            )
        else:
            blocks.append(
                format_message(
                    entry,
                    feed_title=feed_title,
                    brief_max_length=brief,
                )
            )

    header = "<b>Daily digest</b>\n\n"
    sep = "\n\n──────────\n\n"
    chunks: list[str] = []
    parts: list[str] = []
    for block in blocks:
        trial = header + sep.join(parts + [block])
        if len(trial) <= _DIGEST_CHUNK_SAFE:
            parts.append(block)
            continue
        if parts:
            chunks.append(header + sep.join(parts))
            parts = [block]
            if len(header + block) > _DIGEST_CHUNK_SAFE:
                chunks.append(
                    header + block[: _DIGEST_CHUNK_SAFE - len(header) - 24] + "…"
                )
                parts = []
        else:
            chunks.append(
                header + block[: _DIGEST_CHUNK_SAFE - len(header) - 24] + "…"
            )
    if parts:
        chunks.append(header + sep.join(parts))
    return chunks
