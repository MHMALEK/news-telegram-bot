"""SQLite persistence: users, feed subscriptions, and per-user seen entry IDs."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import Iterable

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
            """
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
