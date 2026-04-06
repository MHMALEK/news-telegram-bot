"""SQLite persistence: users, feeds, keywords, seen IDs, and digest queue."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from news_telegram_bot.keywords import (
    MAX_USER_KEYWORDS,
    keyword_display_label,
    normalize_keyword_phrase,
    validate_keyword_raw,
)

logger = logging.getLogger(__name__)


class Database:
    """Thread-safe SQLite access for the bot."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_chat_id INTEGER NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS user_feeds (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                feed_url TEXT NOT NULL,
                PRIMARY KEY (user_id, feed_url)
            );

            CREATE TABLE IF NOT EXISTS seen_entries (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                entry_id TEXT NOT NULL,
                PRIMARY KEY (user_id, entry_id)
            );

            CREATE INDEX IF NOT EXISTS idx_user_feeds_url ON user_feeds(feed_url);
            CREATE INDEX IF NOT EXISTS idx_seen_user ON seen_entries(user_id);

            CREATE TABLE IF NOT EXISTS digest_queue (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                entry_id TEXT NOT NULL,
                feed_title TEXT,
                entry_json TEXT NOT NULL,
                keyword_matches TEXT,
                PRIMARY KEY (user_id, entry_id)
            );

            CREATE TABLE IF NOT EXISTS user_keywords (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                keyword_normalized TEXT NOT NULL,
                keyword_label TEXT NOT NULL,
                PRIMARY KEY (user_id, keyword_normalized)
            );
            CREATE INDEX IF NOT EXISTS idx_user_keywords_user ON user_keywords(user_id);
            """
        )
        self._ensure_users_digest_column()
        self._ensure_digest_keyword_column()

    def _ensure_users_digest_column(self) -> None:
        with self._lock:
            rows = self._conn.execute("PRAGMA table_info(users)").fetchall()
        colnames = {str(r[1]) for r in rows}
        if "digest_enabled" in colnames:
            return
        with self._lock:
            self._conn.execute(
                "ALTER TABLE users ADD COLUMN digest_enabled INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_digest_keyword_column(self) -> None:
        with self._lock:
            rows = self._conn.execute("PRAGMA table_info(digest_queue)").fetchall()
        colnames = {str(r[1]) for r in rows}
        if "keyword_matches" in colnames:
            return
        with self._lock:
            self._conn.execute(
                "ALTER TABLE digest_queue ADD COLUMN keyword_matches TEXT"
            )

    def get_or_create_user(self, telegram_chat_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM users WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
            if row:
                return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO users (telegram_chat_id) VALUES (?)",
                (telegram_chat_id,),
            )
            return int(cur.lastrowid)

    def list_users(self) -> list[tuple[int, int]]:
        """Return (internal user id, telegram_chat_id) rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, telegram_chat_id FROM users ORDER BY id"
            ).fetchall()
        return [(int(r["id"]), int(r["telegram_chat_id"])) for r in rows]

    def get_digest_enabled(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT digest_enabled FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return False
        return bool(row["digest_enabled"])

    def set_digest_enabled(self, user_id: int, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET digest_enabled = ? WHERE id = ?",
                (1 if enabled else 0, user_id),
            )

    def get_feed_urls(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT feed_url FROM user_feeds WHERE user_id = ? ORDER BY feed_url",
                (user_id,),
            ).fetchall()
        return [str(r["feed_url"]) for r in rows]

    def seed_feeds_if_empty(self, user_id: int, default_urls: list[str]) -> None:
        if not default_urls:
            return
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS c FROM user_feeds WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if n and int(n["c"]) > 0:
                return
            self._conn.executemany(
                "INSERT OR IGNORE INTO user_feeds (user_id, feed_url) VALUES (?, ?)",
                [(user_id, u) for u in default_urls],
            )

    def replace_feeds(self, user_id: int, feed_urls: list[str]) -> None:
        """Replace subscriptions with exactly ``feed_urls`` (for env sync)."""
        cleaned = [u.strip() for u in feed_urls if u.strip()]
        with self._lock:
            self._conn.execute("DELETE FROM user_feeds WHERE user_id = ?", (user_id,))
            if not cleaned:
                return
            self._conn.executemany(
                "INSERT INTO user_feeds (user_id, feed_url) VALUES (?, ?)",
                [(user_id, u) for u in cleaned],
            )

    def add_feed(self, user_id: int, feed_url: str) -> bool:
        feed_url = feed_url.strip()
        if not feed_url:
            return False
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO user_feeds (user_id, feed_url) VALUES (?, ?)",
                    (user_id, feed_url),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def remove_feed(self, user_id: int, feed_url: str) -> bool:
        feed_url = feed_url.strip()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM user_feeds WHERE user_id = ? AND feed_url = ?",
                (user_id, feed_url),
            )
        return cur.rowcount > 0

    def list_keyword_labels(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT keyword_label FROM user_keywords
                WHERE user_id = ?
                ORDER BY keyword_label COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()
        return [str(r["keyword_label"]) for r in rows]

    def list_keywords_for_match(self, user_id: int) -> list[tuple[str, str]]:
        """(normalized, display label) for substring matching."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT keyword_normalized, keyword_label FROM user_keywords
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
        return [(str(r["keyword_normalized"]), str(r["keyword_label"])) for r in rows]

    def add_keyword(self, user_id: int, raw: str) -> tuple[bool, str | None]:
        """Return (success, error_message)."""
        err = validate_keyword_raw(raw)
        if err:
            return False, err
        norm = normalize_keyword_phrase(raw)
        if not norm:
            return False, "Keyword is empty."
        label = keyword_display_label(raw)
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS c FROM user_keywords WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if n and int(n["c"]) >= MAX_USER_KEYWORDS:
                return False, f"Maximum {MAX_USER_KEYWORDS} keywords per user."
            try:
                self._conn.execute(
                    """
                    INSERT INTO user_keywords (user_id, keyword_normalized, keyword_label)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, norm, label),
                )
            except sqlite3.IntegrityError:
                return False, "You already have that keyword."
        return True, None

    def remove_keyword(self, user_id: int, raw: str) -> bool:
        norm = normalize_keyword_phrase(raw)
        if not norm:
            return False
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM user_keywords
                WHERE user_id = ? AND keyword_normalized = ?
                """,
                (user_id, norm),
            )
        return cur.rowcount > 0

    def clear_keywords(self, user_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM user_keywords WHERE user_id = ?", (user_id,)
            )
        return cur.rowcount

    def subscribers_by_feed_url(self) -> dict[str, list[tuple[int, int]]]:
        """Map feed_url -> [(internal_user_id, telegram_chat_id), ...]."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.feed_url AS feed_url, u.id AS uid, u.telegram_chat_id AS chat_id
                FROM user_feeds f
                JOIN users u ON u.id = f.user_id
                """
            ).fetchall()
        out: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for r in rows:
            out[str(r["feed_url"])].append((int(r["uid"]), int(r["chat_id"])))
        return dict(out)

    def seen_count(self, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM seen_entries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def is_new(self, user_id: int, entry_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_entries WHERE user_id = ? AND entry_id = ? LIMIT 1",
                (user_id, entry_id),
            ).fetchone()
        return row is None

    def mark(self, user_id: int, entry_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_entries (user_id, entry_id) VALUES (?, ?)",
                (user_id, entry_id),
            )

    def mark_many(self, user_id: int, entry_ids: Iterable[str]) -> None:
        ids = list(entry_ids)
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_entries (user_id, entry_id) VALUES (?, ?)",
                [(user_id, eid) for eid in ids],
            )

    def enqueue_digest_item(
        self,
        user_id: int,
        entry_id: str,
        feed_title: str | None,
        entry: dict[str, Any],
        keyword_matches: list[str] | None = None,
    ) -> None:
        payload = json.dumps(entry, default=str)
        kw_json = json.dumps(keyword_matches) if keyword_matches else None
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO digest_queue
                    (user_id, entry_id, feed_title, entry_json, keyword_matches)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, entry_id, feed_title, payload, kw_json),
            )

    def list_digest_queue(
        self, user_id: int
    ) -> list[tuple[str | None, dict[str, Any], str, list[str] | None]]:
        """Return queued items without removing (feed_title, entry, stable_id, keyword_labels)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT entry_id, feed_title, entry_json, keyword_matches
                FROM digest_queue
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
        out: list[tuple[str | None, dict[str, Any], str, list[str] | None]] = []
        for r in rows:
            eid = str(r["entry_id"])
            ft = r["feed_title"]
            raw = json.loads(r["entry_json"])
            if not isinstance(raw, dict):
                continue
            kw: list[str] | None = None
            raw_kw = r["keyword_matches"]
            if raw_kw:
                try:
                    parsed = json.loads(str(raw_kw))
                    if isinstance(parsed, list):
                        kw = [str(x) for x in parsed]
                except json.JSONDecodeError:
                    kw = None
            out.append((str(ft) if ft else None, raw, eid, kw))
        return out

    def clear_digest_queue(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM digest_queue WHERE user_id = ?", (user_id,)
            )

    def migrate_json_state(
        self,
        json_path: Path,
        telegram_chat_id: int,
    ) -> int:
        """Import seen IDs from legacy state.json for one chat. Returns count imported."""
        if not json_path.exists():
            return 0
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            ids = data.get("ids")
            if not isinstance(ids, list):
                return 0
            raw = [str(x) for x in ids]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read legacy state %s: %s", json_path, e)
            return 0
        user_id = self.get_or_create_user(telegram_chat_id)
        with self._lock:
            n_before = self._conn.execute(
                "SELECT COUNT(*) AS c FROM seen_entries WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if n_before and int(n_before["c"]) > 0:
                return 0
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_entries (user_id, entry_id) VALUES (?, ?)",
                [(user_id, eid) for eid in raw],
            )
        logger.info(
            "Migrated %d seen id(s) from %s into SQLite for chat %s",
            len(raw),
            json_path,
            telegram_chat_id,
        )
        return len(raw)
