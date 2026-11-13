"""Recent gold headlines from Google News RSS + keyword-based theme counts.

This is *context only*. Nothing here feeds the statistical forecast. Themes are simple keyword
matches on headlines - they say what is being talked about, not which way prices will move.
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx
from defusedxml import ElementTree as ET

from . import config

QUERIES = [
    "gold price",
    "gold Federal Reserve interest rates dollar",
    "gold central bank buying",
    "gold inflation geopolitical safe haven",
]
MAX_AGE_DAYS = 21
MAX_ITEMS = 30

GOLD_RE = re.compile(r"\b(gold|bullion|xau)\b", re.I)
# Off-topic: sports/places, single-stock/miner news, and local-currency retail price quotes.
EXCLUDE_RE = re.compile(
    r"gold coast|gold medal|gold cup|gold rush|golden|gold card"
    r"|\b(?:NYSE|TSX|ASX|NASDAQ|stock|shares|Q[1-4]\b.*results|financial results)\b|\([A-Z]+:[A-Z]+\)"
    r"|\b(?:bhari|tola|lakh|crore|SJC|per gram|Tk|rupees?|taka)\b",
    re.I,
)

THEMES = {
    "rates": ("Interest rates", re.compile(r"\b(fed|federal reserve|fomc|powell|interest rates?|rate (?:cuts?|hikes?|decision)|rates?|treasury yields?|yields?)\b", re.I)),
    "dollar": ("US dollar", re.compile(r"\b(dollar|usd|greenback|dxy)\b", re.I)),
    "inflation": ("Inflation", re.compile(r"\b(inflation|cpi|pce|ppi|stagflation|consumer prices?)\b", re.I)),
    "geopolitics": ("Geopolitical risk", re.compile(r"\b(war|conflicts?|tensions?|geopolitic\w*|ukraine|russia|israel|gaza|iran|middle east|tariffs?|sanctions?|safe[- ]haven|trade war)\b", re.I)),
    "central_banks": ("Central-bank demand", re.compile(r"\b(central[- ]banks?|pboc|reserve bank|gold reserves|official[- ]sector|world gold council|wgc|net purchases|gold purchases)\b", re.I)),
}


# General background, written by the project authors. NOT extracted from the articles and NOT a prediction.
BACKGROUND = {
    "rates": "Gold pays no interest, so higher interest rates and Treasury yields tend to make it less attractive, while expected rate cuts tend to support it.",
    "dollar": "Gold is priced in US dollars. A stronger dollar usually makes it costlier for buyers using other currencies and tends to weigh on the price; a weaker dollar tends to help.",
    "inflation": "Gold is often held as an inflation hedge, but high inflation can also lead central banks to raise rates, so the net effect varies.",
    "geopolitics": "Conflict, tariffs and political uncertainty can raise demand for gold as a safe-haven asset.",
    "central_banks": "Central-bank purchases add to physical demand and have supported prices in recent years; slower buying or sales would remove that support.",
}


def _parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        elif " - " in title and not source:
            title, _, source = title.rpartition(" - ")
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if not title or not link.startswith("http"):
            continue
        items.append({"title": title, "source": source or "Unknown", "link": link, "published": published})
    return items


def classify(title: str) -> list[str]:
    return [key for key, (_, rx) in THEMES.items() if rx.search(title)]


def build_digest(items: list[dict], now: datetime | None = None) -> dict:
    """Filter, dedupe, sort and theme-tag raw feed items."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    seen, kept = set(), []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        if it["published"] < cutoff or it["published"] > now + timedelta(hours=6):
            continue
        if not GOLD_RE.search(it["title"]) or EXCLUDE_RE.search(it["title"]):
            continue
        key = re.sub(r"\W+", " ", it["title"].lower()).strip()[:90]
        if key in seen:
            continue
        seen.add(key)
        kept.append(it)
    kept = kept[:MAX_ITEMS]

    out_items = []
    theme_hits: dict[str, list[dict]] = {k: [] for k in THEMES}
    for it in kept:
        tags = classify(it["title"])
        row = {
            "title": it["title"],
            "source": it["source"],
            "link": it["link"],
            "published": it["published"].isoformat(timespec="minutes"),
            "themes": tags,
        }
        out_items.append(row)
        for t in tags:
            theme_hits[t].append(row)
    themes = [
        {
            "id": k,
            "label": THEMES[k][0],
            "background": BACKGROUND[k],
            "count": len(v),
            "share_pct": round(100 * len(v) / len(out_items), 0) if out_items else 0,
            "examples": [{"title": r["title"], "link": r["link"], "source": r["source"], "published": r["published"]} for r in v[:2]],
        }
        for k, v in theme_hits.items()
    ]
    return {"items": out_items, "themes": themes, "n_items": len(out_items)}


class NewsService:
    def __init__(self, fetcher=None):
        self._fetcher = fetcher or self._fetch_all
        self._cache: dict | None = None
        self._cache_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _fetch_query(client: httpx.Client, q: str) -> list[dict]:
        url = f"https://news.google.com/rss/search?q={quote_plus(q + ' when:14d')}&hl=en-US&gl=US&ceid=US:en"
        resp = client.get(url, headers={"User-Agent": config.USER_AGENT})
        resp.raise_for_status()
        return _parse_feed(resp.content)

    def _fetch_all(self) -> tuple[list[dict], list[str]]:
        errors: list[str] = []
        items: list[dict] = []
        with httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
            with ThreadPoolExecutor(max_workers=len(QUERIES)) as pool:
                futures = {q: pool.submit(self._fetch_query, client, q) for q in QUERIES}
                for q, fut in futures.items():
                    try:
                        items.extend(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"'{q}': {exc.__class__.__name__}")
        return items, errors

    def get(self, force: bool = False) -> dict:
        with self._lock:
            now = time.time()
            if self._cache and not force and now - self._cache_at < config.NEWS_TTL_SECONDS:
                return self._cache
            try:
                items, errors = self._fetcher()
            except Exception as exc:  # noqa: BLE001
                items, errors = [], [exc.__class__.__name__]
            digest = build_digest(items)
            if not digest["items"]:
                reason = "News feed could not be retrieved" if errors else "No recent gold headlines were found"
                if self._cache:
                    stale = dict(self._cache)
                    stale.update(status="stale", message=f"{reason}; showing headlines fetched earlier.", errors=errors)
                    return stale
                return {"status": "error", "message": f"{reason}. {'; '.join(errors)}".strip(), "items": [], "themes": [], "n_items": 0, "errors": errors,
                        "fetched_at": None, "source": "Google News RSS"}
            digest.update(
                status="partial" if errors else "ok",
                message=(f"Some news queries failed ({len(errors)} of {len(QUERIES)}); results may be incomplete." if errors else None),
                errors=errors,
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source="Google News RSS",
                method="Headlines are filtered for the words gold/bullion/XAU, de-duplicated and tagged by keyword. This is context only and is not used by the prediction model.",
            )
            self._cache, self._cache_at = digest, now
            return digest
