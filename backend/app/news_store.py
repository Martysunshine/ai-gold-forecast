"""Timestamped news archive: the raw material for a future, properly evaluated news feature.

What is stored per item: the publisher's ORIGINAL timestamp (``published_at``, never changed), the moment this app first
saw it (``first_seen_at``), source, a stable article id (hash of the link) and a normalised title used to collapse
syndicated copies of the same story.

Deliberately NOT done: no LLM extraction, no sentiment score and no backfilled history. A news feature can only be judged on
items whose first-seen time precedes the outcome they would predict, and an LLM's recollection of past headlines is not a
point-in-time record. Until months of forward items exist, news is sourced context only and is never a model input.
"""
from __future__ import annotations

import hashlib
import re
import time

from . import db
from .news import NewsService

DUPLICATE_WINDOW_S = 3 * 86400


def article_id(link: str) -> str:
    return hashlib.sha1(re.sub(r"[?#].*$", "", link.strip().lower()).encode()).hexdigest()[:20]


def title_key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))[:160]


def ingest(items: list[dict], now: float | None = None) -> dict:
    """Store new items; collapse syndicated duplicates. Returns counts."""
    now = now or time.time()
    new = dup = 0
    with db.tx() as c:
        for it in sorted(items, key=lambda i: i["published"]):
            aid, tkey = article_id(it["link"]), title_key(it["title"])
            if c.execute("SELECT 1 FROM news_items WHERE article_id=?", (aid,)).fetchone():
                continue
            twin = c.execute("SELECT article_id FROM news_items WHERE title_key=? AND duplicate_of IS NULL AND first_seen_at>=? LIMIT 1",
                             (tkey, now - DUPLICATE_WINDOW_S)).fetchone()
            c.execute("INSERT INTO news_items VALUES (?,?,?,?,?,?,?,?)",
                      (aid, tkey, it["title"], it.get("source"), it["link"], it["published"].isoformat(timespec="seconds"), now,
                       twin["article_id"] if twin else None))
            if twin:
                dup += 1
            else:
                new += 1
    return {"new": new, "duplicates": dup}


def collect() -> dict:
    items, errors = NewsService._fetch_all(NewsService())  # same queries as the live news tab
    out = ingest(items)
    out["errors"] = errors
    return out


def summary() -> dict:
    r = db.rows("SELECT COUNT(*) AS n, SUM(duplicate_of IS NULL) AS unique_n, MIN(first_seen_at) AS since, MAX(first_seen_at) AS last_seen FROM news_items")[0]
    return {"stored": r["n"] or 0, "unique_stories": r["unique_n"] or 0, "collecting_since": r["since"], "last_seen": r["last_seen"],
            "status": "collecting",
            "note": "Archived with original publication and first-seen timestamps. Not a model input: too little forward history exists to evaluate it."}
