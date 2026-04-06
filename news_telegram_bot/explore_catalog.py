"""Load /explore categories and feeds from a JSON file (not stored in the SQLite user DB)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class CatalogFeed:
    title: str
    url: str


@dataclass(frozen=True)
class ExploreCatalog:
    """In-memory catalog from ``explore_catalog.json``."""

    _order: tuple[str, ...]
    _categories: dict[str, tuple[str, tuple[CatalogFeed, ...]]]

    @classmethod
    def empty(cls) -> ExploreCatalog:
        return cls((), {})

    @classmethod
    def from_path(cls, path: Path) -> ExploreCatalog:
        if not path.is_file():
            logger.warning("Explore catalog file not found: %s", path)
            return cls.empty()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Could not read explore catalog %s: %s", path, e)
            return cls.empty()
        return cls.from_dict(data, source=str(path))

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "<dict>") -> ExploreCatalog:
        if not isinstance(data, dict):
            logger.error("Explore catalog %s: root must be an object", source)
            return cls.empty()

        order_raw = data.get("category_order")
        if not isinstance(order_raw, list):
            logger.error("Explore catalog %s: missing category_order array", source)
            return cls.empty()

        cats_raw = data.get("categories")
        if not isinstance(cats_raw, dict):
            logger.error("Explore catalog %s: missing categories object", source)
            return cls.empty()

        order: list[str] = []
        for s in order_raw:
            if not isinstance(s, str) or not _SLUG_RE.match(s):
                logger.error(
                    "Explore catalog %s: invalid slug %r in category_order",
                    source,
                    s,
                )
                return cls.empty()
            order.append(s)

        categories: dict[str, tuple[str, tuple[CatalogFeed, ...]]] = {}
        for slug in order:
            block = cats_raw.get(slug)
            if not isinstance(block, dict):
                logger.error(
                    "Explore catalog %s: missing category %r",
                    source,
                    slug,
                )
                return cls.empty()
            title = block.get("title")
            feeds_raw = block.get("feeds")
            if not isinstance(title, str) or not title.strip():
                logger.error(
                    "Explore catalog %s: invalid title for %r",
                    source,
                    slug,
                )
                return cls.empty()
            if not isinstance(feeds_raw, list) or not feeds_raw:
                logger.error(
                    "Explore catalog %s: feeds for %r must be a non-empty array",
                    source,
                    slug,
                )
                return cls.empty()
            feeds: list[CatalogFeed] = []
            for i, fr in enumerate(feeds_raw):
                if not isinstance(fr, dict):
                    return cls.empty()
                t = fr.get("title")
                u = fr.get("url")
                if (
                    not isinstance(t, str)
                    or not t.strip()
                    or not isinstance(u, str)
                    or not u.strip()
                ):
                    logger.error(
                        "Explore catalog %s: invalid feed #%s in %r",
                        source,
                        i,
                        slug,
                    )
                    return cls.empty()
                feeds.append(CatalogFeed(title=t.strip(), url=u.strip()))
            categories[slug] = (title.strip(), tuple(feeds))

        extra = set(cats_raw.keys()) - set(order)
        if extra:
            logger.error(
                "Explore catalog %s: categories has keys not in category_order: %s",
                source,
                extra,
            )
            return cls.empty()

        missing = set(order) - set(categories.keys())
        if missing:
            logger.error(
                "Explore catalog %s: category_order references unknown %s",
                source,
                missing,
            )
            return cls.empty()

        return cls(tuple(order), categories)

    def category_order(self) -> list[str]:
        return list(self._order)

    def get_category_title(self, slug: str) -> str | None:
        entry = self._categories.get(slug)
        return entry[0] if entry else None

    def get_feeds(self, slug: str) -> list[CatalogFeed]:
        entry = self._categories.get(slug)
        return list(entry[1]) if entry else []

    def get_feed(self, slug: str, index: int) -> CatalogFeed | None:
        feeds = self.get_feeds(slug)
        if 0 <= index < len(feeds):
            return feeds[index]
        return None

    def __bool__(self) -> bool:
        return bool(self._order)
