"""
Wikipedia -> Wikidata bridge.

WHY THIS EXISTS
---------------
Ramesh's instinct that Wikipedia should come second in the search order is
right, and for a stronger reason than "it has useful details".

A Wikipedia hit is not just another link. Every Wikipedia article is joined 1:1
to a Wikidata item by a sitelink, so a single search result can be converted
into a Q-id - and a Q-id hands back every structured platform ID the person has
(Instagram, X, YouTube, TikTok, IMDb, Spotify, MusicBrainz, TMDB) at Tier 1, for
free, with no further paid searches.

So the flow is:

    name not found by label match
      -> SerpAPI: site:wikipedia.org "name"        (1 paid search)
      -> article title -> wbgetentities&sites&titles -> Q-id   (free)
      -> harvest every external id off that Q-id              (free)

That turns one paid query into a full structured record. It also rescues the
exact failure this tool kept hitting: a person who IS in Wikidata but whose
label spelling did not match what the operator typed. Google is far better at
fuzzy name matching than an exact label lookup, so we let Google do the matching
and Wikidata do the answering.

Cost note: this is the cheapest possible way to widen coverage. Every handle it
returns is Tier 1 structured evidence, not a search guess.
"""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import unquote, urlencode, urlsplit

WD_API = "https://www.wikidata.org/w/api.php"


def parse_wikipedia_url(url: str) -> Optional[tuple[str, str]]:
    """
    'https://en.wikipedia.org/wiki/Cash_Cobain' -> ('enwiki', 'Cash Cobain')

    Returns the Wikidata *site* key and the article title, which is exactly
    what wbgetentities needs.
    """
    if not url:
        return None
    try:
        sp = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    host = (sp.hostname or "").lower()
    if not host.endswith("wikipedia.org"):
        return None
    lang = host.split(".")[0]
    if lang in ("www", "wikipedia", "m"):
        lang = "en"
    lang = lang.replace("-", "_")
    parts = [p for p in sp.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "wiki":
        return None
    title = unquote("/".join(parts[1:])).replace("_", " ").strip()
    if not title:
        return None
    # Namespaced pages are not people
    first = title.split(":")[0]
    if first in ("File", "Category", "Template", "Help", "Special", "Talk",
                 "Portal", "Wikipedia", "Draft"):
        return None
    return f"{lang}wiki", title


def _entities_by_title_url(site: str, titles: list[str]) -> str:
    return WD_API + "?" + urlencode({
        "action": "wbgetentities", "sites": site, "titles": "|".join(titles),
        "props": "info", "format": "json",
    })


def qid_for_article(url: str, fetch: Callable) -> Optional[str]:
    """
    Resolve one Wikipedia article URL to a Wikidata Q-id.

    One free API call. Returns None if the article has no Wikidata item or the
    lookup fails - callers should treat that as 'no bridge available', not as
    an error worth surfacing.
    """
    parsed = parse_wikipedia_url(url)
    if not parsed:
        return None
    site, title = parsed
    data = fetch(_entities_by_title_url(site, [title]), kind="json") or {}
    entities = data.get("entities") or {}
    for qid, ent in entities.items():
        if not qid.startswith("Q"):
            continue
        if isinstance(ent, dict) and ent.get("missing") is not None:
            continue
        return qid
    return None


def qids_for_articles(urls: list[str], fetch: Callable,
                      max_lookups: int = 3) -> list[str]:
    """
    Resolve several article URLs, grouped by wiki so each site costs one call.

    Capped: the first few search results are the plausible ones, and every
    extra lookup is latency for a rapidly worsening candidate.
    """
    by_site: dict[str, list[str]] = {}
    order: list[tuple[str, str]] = []
    for u in urls[:max_lookups]:
        parsed = parse_wikipedia_url(u)
        if not parsed:
            continue
        site, title = parsed
        by_site.setdefault(site, []).append(title)
        order.append((site, title))

    found: list[str] = []
    for site, titles in by_site.items():
        data = fetch(_entities_by_title_url(site, titles), kind="json") or {}
        for qid, ent in (data.get("entities") or {}).items():
            if not qid.startswith("Q"):
                continue
            if isinstance(ent, dict) and ent.get("missing") is not None:
                continue
            if qid not in found:
                found.append(qid)
    return found
