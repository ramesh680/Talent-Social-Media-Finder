"""
The one place network I/O happens.

Everything in the resolver takes `fetch(url, kind)` as an argument, so this is
the single chokepoint for caching, rate limiting, retries, user-agent policy and
the domain allowlist. That is the whole reason the resolver is dependency-injected:
one file to audit when you get throttled or blocked.

Politeness is not optional here. Wikidata's SPARQL endpoint and MusicBrainz both
publish rate limits and will ban an IP that ignores them, and link-in-bio pages
are ordinary websites whose ToS you are subject to.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

def default_user_agent() -> str:
    """
    Wikimedia's UA policy requires a contact address and will serve 403 to
    generic agents, so WIKIMEDIA_CONTACT is not decoration - it is what keeps
    the Wikidata calls working.
    """
    contact = os.environ.get("WIKIMEDIA_CONTACT", "").strip()
    suffix = f"; contact {contact}" if contact else ""
    return f"IdentityForge/0.3 (ListenFirst Media data operations{suffix})"


DEFAULT_UA = default_user_agent()

# MusicBrainz requires <=1 req/sec. Wikidata asks for serial, low-concurrency
# use and will 429. Everything else gets a conservative default.
RATE_LIMITS: dict[str, float] = {
    "musicbrainz.org": 1.1,
    "query.wikidata.org": 1.5,
    "www.wikidata.org": 0.4,
    "api.themoviedb.org": 0.06,
    "serpapi.com": 0.5,
    "viaf.org": 0.5,
    "openlibrary.org": 0.5,
    "www.thesportsdb.com": 0.5,
    "www.omdbapi.com": 0.2,
    "_default": 1.0,
}

# Query parameters that must never be written to the cache table or a log.
# SerpAPI and TMDB v3 both authenticate via the query string, so without this
# the on-disk cache becomes a plaintext key store.
SECRET_PARAMS = ("api_key", "apikey", "key", "token", "access_token")

# Only these are ever fetched. An open-ended fetcher pointed at user input is
# an SSRF hole, so the allowlist is a security control, not just tidiness.
ALLOWED_SUFFIXES: tuple[str, ...] = (
    "wikidata.org", "wikipedia.org", "musicbrainz.org", "themoviedb.org",
    "imdb.com", "instagram.com", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "linkedin.com", "open.spotify.com",
    # link-in-bio hosts
    "linktr.ee", "beacons.ai", "bio.link", "allmylinks.com", "solo.to",
    "taplink.cc", "linkr.bio", "lnk.bio", "linkin.bio", "komi.io", "hoo.be",
    "stan.store", "direct.me", "shorby.com", "campsite.bio", "flowcode.com",
    "znap.link", "carrd.co", "about.me", "milkshake.app", "tap.bio",
    "withkoji.com",
    # API providers
    "serpapi.com", "omdbapi.com",
    # free, keyless vertical sources - no billing attached to any of these
    "thesportsdb.com", "openlibrary.org", "viaf.org", "orcid.org",
    "api.deezer.com", "itunes.apple.com",
)


def redact(url: str) -> str:
    """Replace secret query values with a placeholder, preserving structure."""
    if "?" not in url:
        return url
    base, _, qs = url.partition("?")
    parts = []
    for chunk in qs.split("&"):
        k, eq, v = chunk.partition("=")
        parts.append(f"{k}={eq and 'REDACTED'}" if k.lower() in SECRET_PARAMS
                     else chunk)
    return f"{base}?{'&'.join(parts)}"

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    url_hash TEXT PRIMARY KEY,
    url TEXT, kind TEXT, status INTEGER,
    body TEXT, fetched_at TEXT
);
"""


@dataclass
class FetchStats:
    hits: int = 0
    misses: int = 0
    blocked: int = 0
    errors: int = 0
    bytes_in: int = 0
    urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"cache_hits": self.hits, "network_calls": self.misses,
                "blocked": self.blocked, "errors": self.errors,
                "bytes_in": self.bytes_in}


class CachedFetcher:
    """
    Callable matching the resolver's `fetch(url, kind='text'|'json')` contract.

    Cache is on by default and aggressive. Identity data changes slowly; a repeat
    resolution of the same person should cost zero requests.
    """

    def __init__(self, cache_path: str = "http_cache.db",
                 ttl_seconds: int = 7 * 24 * 3600,
                 user_agent: str = DEFAULT_UA,
                 timeout: int = 20,
                 max_retries: int = 3,
                 offline: bool = False):
        self.ttl = ttl_seconds
        self.ua = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.offline = offline
        self.stats = FetchStats()
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._conn = sqlite3.connect(cache_path, check_same_thread=False)
        self._conn.executescript(CACHE_SCHEMA)
        self._conn.commit()

    # -- policy ----------------------------------------------------------
    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def _allowed(self, url: str) -> bool:
        host = self._host(url)
        return any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES)

    def _throttle(self, host: str) -> None:
        key = next((k for k in RATE_LIMITS if host.endswith(k)), "_default")
        gap = RATE_LIMITS[key]
        with self._lock:
            last = self._last_call.get(key, 0.0)
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_call[key] = time.monotonic()

    # -- cache -----------------------------------------------------------
    def _cache_get(self, url: str) -> Optional[str]:
        h = hashlib.sha256(url.encode()).hexdigest()   # key on full url
        row = self._conn.execute(
            "SELECT body, fetched_at FROM http_cache WHERE url_hash=?", (h,)
        ).fetchone()
        if not row:
            return None
        body, fetched_at = row
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(fetched_at)).total_seconds()
        except ValueError:
            return None
        return body if age < self.ttl else None

    def _cache_put(self, url: str, kind: str, status: int, body: str) -> None:
        h = hashlib.sha256(url.encode()).hexdigest()
        self._conn.execute(
            "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?)",
            (h, redact(url), kind, status, body,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self._conn.commit()

    # -- the callable ----------------------------------------------------
    def __call__(self, url: str, kind: str = "text"):
        if not self._allowed(url):
            self.stats.blocked += 1
            return {} if kind == "json" else ""

        cached = self._cache_get(url)
        if cached is not None:
            self.stats.hits += 1
            return self._decode(cached, kind)

        if self.offline:
            self.stats.blocked += 1
            return {} if kind == "json" else ""

        body = self._get(url, kind)
        if body is None:
            self.stats.errors += 1
            return {} if kind == "json" else ""

        self.stats.misses += 1
        self.stats.bytes_in += len(body)
        if len(self.stats.urls) < 200:
            self.stats.urls.append(redact(url))
        self._cache_put(url, kind, 200, body)
        return self._decode(body, kind)

    @staticmethod
    def _decode(body: str, kind: str):
        if kind != "json":
            return body
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            return {}

    def _get(self, url: str, kind: str) -> Optional[str]:
        headers = {
            "User-Agent": self.ua,
            "Accept": ("application/sparql-results+json, application/json"
                       if kind == "json" else
                       "text/html,application/xhtml+xml"),
            "Accept-Language": "en",
        }
        for attempt in range(self.max_retries):
            self._throttle(self._host(url))
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    charset = r.headers.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace")
            except urllib.error.HTTPError as e:
                # 429/503 are the throttle signals both Wikidata and
                # MusicBrainz use - back off, do not hammer.
                if e.code in (429, 500, 502, 503, 504):
                    retry_after = e.headers.get("Retry-After")
                    delay = (float(retry_after) if retry_after
                             and retry_after.isdigit() else 2 ** (attempt + 1))
                    time.sleep(min(delay, 30))
                    continue
                return None                     # 404/403 - do not retry
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(2 ** attempt)
        return None

    def reset_stats(self) -> None:
        self.stats = FetchStats()


class FixtureFetcher:
    """
    Deterministic offline fetcher for the demo mode and for tests.

    Render's free tier sleeps and cold-starts, and Wikidata is not something to
    lean on for a first-load demo, so the deployed app ships with this and only
    goes live when the operator flips the switch.
    """

    def __init__(self, fixtures: dict[str, object]):
        self.fixtures = fixtures
        self.calls: list[str] = []

    def __call__(self, url: str, kind: str = "text"):
        self.calls.append(url)
        for needle, payload in self.fixtures.items():
            if needle in url:
                return payload
        return {} if kind == "json" else ""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def build_fetcher(offline: bool = False) -> CachedFetcher:
    """
    Honours the operator-supplied knobs:
      CACHE_TTL_SECONDS       (falls back to IF_CACHE_TTL, then 7 days)
      REQUEST_TIMEOUT_SECONDS (falls back to 20)
      WIKIMEDIA_CONTACT       (folded into the User-Agent)
    """
    ttl = _env_int("CACHE_TTL_SECONDS", _env_int("IF_CACHE_TTL", 7 * 24 * 3600))
    return CachedFetcher(
        cache_path=os.environ.get("IF_CACHE_PATH", "http_cache.db"),
        ttl_seconds=ttl,
        user_agent=os.environ.get("IF_USER_AGENT", default_user_agent()),
        timeout=_env_int("REQUEST_TIMEOUT_SECONDS", 20),
        offline=offline,
    )
