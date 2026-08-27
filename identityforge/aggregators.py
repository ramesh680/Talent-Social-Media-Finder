"""
Link-in-bio layer.

Why this matters: an aggregator page is a SELF-DECLARED, MULTI-PLATFORM cluster
in a single HTTP fetch. One hit gives you 5-8 platforms at once, and it is
authored by the person, so it settles same-name collisions outright - nobody
else's Linktree contains your Instagram.

Two directions of travel, both implemented here:

  FORWARD  candidate handle -> probe {domain}/{handle} across ~18 aggregators
  REVERSE  one confirmed profile -> its bio link -> aggregator -> harvest all

REVERSE is the high-value path and is where you should spend your fetch budget.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .platforms import PlatformRef, classify_url


@dataclass(frozen=True)
class Aggregator:
    domain: str
    name: str
    slug_in_path: bool = True      # linktr.ee/foo  vs  foo.carrd.co
    extractor: str = "next_data"   # next_data | nuxt | jsonld | anchors
    notes: str = ""


AGGREGATORS: list[Aggregator] = [
    Aggregator("linktr.ee", "Linktree", extractor="next_data",
               notes="__NEXT_DATA__ script tag holds every link as JSON - parse "
                     "that, never the DOM"),
    Aggregator("beacons.ai", "Beacons", extractor="next_data"),
    Aggregator("bio.link", "Bio.link", extractor="next_data"),
    Aggregator("allmylinks.com", "AllMyLinks", extractor="anchors",
               notes="plain server-rendered anchors; very high link density"),
    Aggregator("solo.to", "Solo.to", extractor="anchors"),
    Aggregator("taplink.cc", "Taplink", extractor="anchors"),
    Aggregator("linkr.bio", "Linkr", extractor="anchors"),
    Aggregator("lnk.bio", "Lnk.Bio", extractor="anchors"),
    Aggregator("linkin.bio", "Later Linkin.bio", extractor="anchors"),
    Aggregator("komi.io", "Komi", extractor="next_data",
               notes="talent-agency heavy - great for actors/musicians"),
    Aggregator("hoo.be", "hoo.be", extractor="next_data",
               notes="skews to A-list talent and athletes"),
    Aggregator("stan.store", "Stan", extractor="next_data",
               notes="creator-commerce; skews to influencers not celebrities"),
    Aggregator("direct.me", "Direct.me", extractor="anchors"),
    Aggregator("shorby.com", "Shorby", extractor="anchors"),
    Aggregator("campsite.bio", "Campsite", extractor="anchors"),
    Aggregator("flowcode.com", "Flowpage", extractor="anchors"),
    Aggregator("znap.link", "Znap", extractor="anchors"),
    Aggregator("carrd.co", "Carrd", slug_in_path=False, extractor="anchors"),
    Aggregator("about.me", "About.me", extractor="jsonld"),
    Aggregator("milkshake.app", "Milkshake", extractor="anchors"),
    Aggregator("tap.bio", "Tap.bio", extractor="anchors"),
    Aggregator("withkoji.com", "Koji", extractor="next_data",
               notes="largely wound down - keep for historic links"),
]

AGGREGATOR_DOMAINS = {a.domain: a for a in AGGREGATORS}

# music-industry smart links: not identity anchors themselves, but they very
# often sit next to one and confirm the music vertical.
SMARTLINK_DOMAINS = {
    "ffm.to", "found.ee", "smarturl.it", "lnk.to", "distrokid.com",
    "orcd.co", "backl.ink", "push.fm", "hypeddit.com", "toneden.io",
    "songwhip.com", "album.link", "song.link", "li.sten.to",
}


def is_aggregator_url(url: str) -> Optional[Aggregator]:
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    host = host.removeprefix("www.")
    if host in AGGREGATOR_DOMAINS:
        return AGGREGATOR_DOMAINS[host]
    # subdomain forms: foo.carrd.co, foo.milkshake.app
    for dom, agg in AGGREGATOR_DOMAINS.items():
        if host.endswith("." + dom):
            return agg
    return None


def probe_urls(handle: str) -> list[str]:
    """FORWARD probe: candidate urls to HEAD/GET for a given handle."""
    h = handle.lstrip("@").strip()
    if not h or not re.fullmatch(r"[A-Za-z0-9._\-]{2,40}", h):
        return []
    out = []
    for agg in AGGREGATORS:
        if agg.slug_in_path:
            out.append(f"https://{agg.domain}/{h}")
        else:
            out.append(f"https://{h}.{agg.domain}")
    return out


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.S | re.I)
_NUXT_RE = re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", re.S)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)
_HREF_RE = re.compile(r'href=["\'](https?://[^"\'<>\s]+)["\']', re.I)
_BARE_URL_RE = re.compile(r'https?://[A-Za-z0-9./_%\-@?=&#:]+')


def _walk_strings(obj) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def extract_links(html: str) -> list[str]:
    """
    Pull every outbound url from an aggregator page.

    Strategy order matters: the embedded JSON payload is authoritative and
    complete, the anchors are a fallback. We union both because some templates
    render only half the links server-side.
    """
    urls: list[str] = []

    for m in _NEXT_DATA_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        for s in _walk_strings(data):
            if s.startswith("http"):
                urls.append(s)
            else:
                urls.extend(_BARE_URL_RE.findall(s))

    for m in _NUXT_RE.finditer(html or ""):
        urls.extend(_BARE_URL_RE.findall(m.group(1)))

    for m in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        # schema.org sameAs is a first-class identity claim - highest value
        for s in _walk_strings(data):
            if s.startswith("http"):
                urls.append(s)

    urls.extend(_HREF_RE.findall(html or ""))

    seen, out = set(), []
    for u in urls:
        u = u.rstrip('\\").,;\'')
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_sameas(html: str) -> list[str]:
    """schema.org sameAs only - a person asserting 'these accounts are me'."""
    out: list[str] = []
    for m in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                same = cur.get("sameAs")
                if isinstance(same, str):
                    out.append(same)
                elif isinstance(same, list):
                    out.extend(x for x in same if isinstance(x, str))
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return out


def harvest(html: str) -> list[PlatformRef]:
    """Aggregator html -> deduped PlatformRefs on our 8 target platforms."""
    refs: dict[str, PlatformRef] = {}
    for url in extract_links(html):
        ref = classify_url(url)
        if ref and ref.key() not in refs:
            refs[ref.key()] = ref
    return list(refs.values())


# ---------------------------------------------------------------------------
# generic detector, for aggregators not yet in the registry
# ---------------------------------------------------------------------------

def looks_like_aggregator(html: str, min_platforms: int = 3,
                          max_words: int = 400) -> bool:
    """
    Heuristic: a thin page whose whole purpose is outbound links to >=3
    DISTINCT social platforms. Catches Carrd clones, custom bio pages, and
    an artist's own one-page site - all of which are equally good anchors.
    """
    refs = harvest(html)
    distinct = {r.platform for r in refs}
    text = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    words = len(text.split())
    return len(distinct) >= min_platforms and words <= max_words
