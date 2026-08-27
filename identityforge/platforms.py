"""
Platform registry + URL classifier/normaliser.

Given ANY url found anywhere (a Linktree row, an IG bio, a Wikidata claim),
answer two questions deterministically:

    1. which of our 8 target platforms is this?
    2. what is the canonical handle / id?

Everything downstream keys off (Platform, handle) pairs, never raw urls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit, unquote, parse_qs


class Platform(str, Enum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TWITTER = "twitter"          # X
    YOUTUBE = "youtube"
    WIKIPEDIA = "wikipedia"
    IMDB = "imdb"
    TIKTOK = "tiktok"
    LINKEDIN = "linkedin"


TARGET_PLATFORMS: tuple[Platform, ...] = tuple(Platform)


# --------------------------------------------------------------------------
# url junk stripping
# --------------------------------------------------------------------------

_TRACKING_PREFIXES = ("utm_", "fb", "ig", "_r", "_t", "si", "gclid", "mc_")

# handles that are actually product routes, never people
_RESERVED = {
    "instagram": {"p", "reel", "reels", "explore", "stories", "tv", "accounts",
                  "direct", "about", "developer", "legal", "privacy", "help"},
    "facebook": {"pages", "groups", "events", "watch", "marketplace", "photo",
                 "photo.php", "story.php", "profile.php", "sharer", "login",
                 "help", "policies", "permalink.php", "media", "hashtag"},
    "twitter": {"i", "home", "search", "explore", "hashtag", "intent",
                "share", "settings", "messages", "notifications", "compose"},
    "tiktok": {"tag", "music", "discover", "foryou", "explore", "live",
               "legal", "about", "upload"},
    "linkedin": {"feed", "jobs", "learning", "pulse", "posts", "help",
                 "legal", "checkpoint", "authwall", "signup", "login"},
    "youtube": {"watch", "playlist", "results", "feed", "shorts", "hashtag",
                "about", "redirect", "channel", "user", "c"},
}


def _clean_query(qs: str) -> dict[str, list[str]]:
    parsed = parse_qs(qs, keep_blank_values=False)
    return {
        k: v for k, v in parsed.items()
        if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    }


def _strip_at(s: str) -> str:
    return s[1:] if s.startswith("@") else s


@dataclass(frozen=True)
class PlatformRef:
    """A resolved (platform, handle) pair plus what kind of id it is."""
    platform: Platform
    handle: str
    id_kind: str = "handle"        # handle | channel_id | numeric_id | nm_id | page_title
    lang: Optional[str] = None     # wikipedia only
    canonical_url: str = ""
    raw_url: str = ""

    def key(self) -> str:
        base = f"{self.platform.value}:{self.handle.lower()}"
        return f"{base}@{self.lang}" if self.lang else base


# --------------------------------------------------------------------------
# per-platform parsers
# --------------------------------------------------------------------------

def _parse_instagram(host, parts, q, url) -> Optional[PlatformRef]:
    if not parts:
        return None
    h = _strip_at(parts[0])
    if h.lower() in _RESERVED["instagram"] or not h:
        return None
    return PlatformRef(Platform.INSTAGRAM, h, "handle",
                       canonical_url=f"https://www.instagram.com/{h}/", raw_url=url)


def _parse_facebook(host, parts, q, url) -> Optional[PlatformRef]:
    # profile.php?id=123456
    if parts and parts[0] == "profile.php" and "id" in q:
        pid = q["id"][0]
        return PlatformRef(Platform.FACEBOOK, pid, "numeric_id",
                           canonical_url=f"https://www.facebook.com/profile.php?id={pid}",
                           raw_url=url)
    # /pages/Name/12345  -> take the numeric id, it is the stable part
    if len(parts) >= 3 and parts[0] == "pages" and parts[-1].isdigit():
        return PlatformRef(Platform.FACEBOOK, parts[-1], "numeric_id",
                           canonical_url=f"https://www.facebook.com/{parts[-1]}", raw_url=url)
    if not parts:
        return None
    h = parts[0]
    if h.lower() in _RESERVED["facebook"] or not h:
        return None
    return PlatformRef(Platform.FACEBOOK, h, "handle",
                       canonical_url=f"https://www.facebook.com/{h}", raw_url=url)


def _parse_twitter(host, parts, q, url) -> Optional[PlatformRef]:
    # /i/user/12345 numeric
    if len(parts) >= 3 and parts[0] == "i" and parts[1] == "user":
        return PlatformRef(Platform.TWITTER, parts[2], "numeric_id",
                           canonical_url=f"https://x.com/i/user/{parts[2]}", raw_url=url)
    if not parts:
        return None
    h = _strip_at(parts[0])
    if h.lower() in _RESERVED["twitter"] or not h:
        return None
    return PlatformRef(Platform.TWITTER, h, "handle",
                       canonical_url=f"https://x.com/{h}", raw_url=url)


_YT_CHANNEL_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


def _parse_youtube(host, parts, q, url) -> Optional[PlatformRef]:
    if host.endswith("youtu.be"):
        return None  # a video, not an identity
    if not parts:
        return None
    first = parts[0]
    if first == "channel" and len(parts) > 1:
        cid = parts[1]
        kind = "channel_id" if _YT_CHANNEL_RE.match(cid) else "handle"
        return PlatformRef(Platform.YOUTUBE, cid, kind,
                           canonical_url=f"https://www.youtube.com/channel/{cid}", raw_url=url)
    if first in ("c", "user") and len(parts) > 1:
        return PlatformRef(Platform.YOUTUBE, parts[1], "handle",
                           canonical_url=f"https://www.youtube.com/{first}/{parts[1]}", raw_url=url)
    if first.startswith("@"):
        h = _strip_at(first)
        return PlatformRef(Platform.YOUTUBE, h, "handle",
                           canonical_url=f"https://www.youtube.com/@{h}", raw_url=url)
    if first.lower() in _RESERVED["youtube"]:
        return None
    if _YT_CHANNEL_RE.match(first):
        return PlatformRef(Platform.YOUTUBE, first, "channel_id",
                           canonical_url=f"https://www.youtube.com/channel/{first}", raw_url=url)
    return None


def _parse_tiktok(host, parts, q, url) -> Optional[PlatformRef]:
    if not parts:
        return None
    if not parts[0].startswith("@"):
        return None                       # /tag/, /music/, short links -> not identity
    h = _strip_at(parts[0])
    if not h or h.lower() in _RESERVED["tiktok"]:
        return None
    return PlatformRef(Platform.TIKTOK, h, "handle",
                       canonical_url=f"https://www.tiktok.com/@{h}", raw_url=url)


def _parse_linkedin(host, parts, q, url) -> Optional[PlatformRef]:
    if len(parts) >= 2 and parts[0] == "in":
        h = parts[1]
        return PlatformRef(Platform.LINKEDIN, h, "handle",
                           canonical_url=f"https://www.linkedin.com/in/{h}/", raw_url=url)
    if len(parts) >= 2 and parts[0] in ("company", "school"):
        # keep it, but flagged - a company page is NOT a person
        return PlatformRef(Platform.LINKEDIN, parts[1], "company",
                           canonical_url=f"https://www.linkedin.com/company/{parts[1]}/",
                           raw_url=url)
    return None


_NM_RE = re.compile(r"\b(nm\d{5,10})\b", re.I)


def _parse_imdb(host, parts, q, url) -> Optional[PlatformRef]:
    m = _NM_RE.search("/".join(parts))
    if not m:
        return None
    nm = m.group(1).lower()
    return PlatformRef(Platform.IMDB, nm, "nm_id",
                       canonical_url=f"https://www.imdb.com/name/{nm}/", raw_url=url)


def _parse_wikipedia(host, parts, q, url) -> Optional[PlatformRef]:
    lang = host.split(".")[0]
    if lang in ("www", "wikipedia"):
        lang = "en"
    title = None
    if len(parts) >= 2 and parts[0] == "wiki":
        title = "/".join(parts[1:])
    elif parts and parts[0] == "w" and "title" in q:
        title = q["title"][0]
    if not title:
        return None
    title = unquote(title).replace("_", " ").strip()
    if not title or ":" in title.split(" ")[0] and title.split(":")[0] in (
            "File", "Category", "Template", "Help", "Special", "Talk"):
        return None
    return PlatformRef(Platform.WIKIPEDIA, title, "page_title", lang=lang,
                       canonical_url=f"https://{lang}.wikipedia.org/wiki/"
                                     f"{title.replace(' ', '_')}", raw_url=url)


_HOST_ROUTES: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"(^|\.)instagram\.com$"), _parse_instagram),
    (re.compile(r"(^|\.)instagr\.am$"), _parse_instagram),
    (re.compile(r"(^|\.)facebook\.com$"), _parse_facebook),
    (re.compile(r"(^|\.)fb\.com$"), _parse_facebook),
    (re.compile(r"(^|\.)fb\.me$"), _parse_facebook),
    (re.compile(r"(^|\.)twitter\.com$"), _parse_twitter),
    (re.compile(r"(^|\.)x\.com$"), _parse_twitter),
    (re.compile(r"(^|\.)youtube\.com$"), _parse_youtube),
    (re.compile(r"(^|\.)youtu\.be$"), _parse_youtube),
    (re.compile(r"(^|\.)tiktok\.com$"), _parse_tiktok),
    (re.compile(r"(^|\.)linkedin\.com$"), _parse_linkedin),
    (re.compile(r"(^|\.)imdb\.com$"), _parse_imdb),
    (re.compile(r"(^|\.)wikipedia\.org$"), _parse_wikipedia),
]


def classify_url(url: str) -> Optional[PlatformRef]:
    """Return a PlatformRef if `url` identifies a person on a target platform."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if "://" not in url:
        url = "https://" + url
    try:
        sp = urlsplit(url)
    except ValueError:
        return None
    host = (sp.hostname or "").lower().lstrip(".")
    if host.startswith("m.") or host.startswith("mobile."):
        host = host.split(".", 1)[1]
    parts = [p for p in sp.path.split("/") if p]
    q = _clean_query(sp.query)
    for pat, fn in _HOST_ROUTES:
        if pat.search(host):
            return fn(host, parts, q, url)
    return None


def build_url(platform: Platform, handle: str, lang: str = "en") -> str:
    """Inverse of classify_url - used for probing candidate handles."""
    h = _strip_at(handle)
    return {
        Platform.INSTAGRAM: f"https://www.instagram.com/{h}/",
        Platform.FACEBOOK: f"https://www.facebook.com/{h}",
        Platform.TWITTER: f"https://x.com/{h}",
        Platform.YOUTUBE: (f"https://www.youtube.com/channel/{h}"
                           if _YT_CHANNEL_RE.match(h) else f"https://www.youtube.com/@{h}"),
        Platform.TIKTOK: f"https://www.tiktok.com/@{h}",
        Platform.LINKEDIN: f"https://www.linkedin.com/in/{h}/",
        Platform.IMDB: f"https://www.imdb.com/name/{h.lower()}/",
        Platform.WIKIPEDIA: f"https://{lang}.wikipedia.org/wiki/{h.replace(' ', '_')}",
    }[platform]
