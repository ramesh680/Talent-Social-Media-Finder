"""
Discovery via web search (SerpAPI).

WHY THIS IS A SEPARATE MODULE, AND SEPARATE FROM EVIDENCE
---------------------------------------------------------
A search engine returning instagram.com/foo for the query "foo singer instagram"
tells you one thing: a string matched. That is Tier 5. It is NOT the person
declaring anything, and it is exactly the failure mode this whole tool exists to
avoid - six same-named people all rank for the same query.

So search is a DISCOVERY source, never an evidence source:

    discovery proposes a handle  ->  the normal pipeline verifies it
    (Tier 5, verdict "reject")       (fetch profile -> aggregator -> Tier 2/3)

That ordering is what keeps the guarantee intact. If you promote raw search hits
to accepted, the tool becomes a fancy Google wrapper and starts shipping wrong
handles for exactly the names you most need it to get right.

Where discovery genuinely earns its cost: the long tail of micro-influencers who
have no Wikidata item at all, so candidate enumeration returns nothing. There,
search is the only way to get a seed - and once you have one seed, the reverse
bio-link cascade does the real work.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

from .evidence import Evidence, Tier
from .platforms import Platform, PlatformRef, classify_url

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# Per-platform query templates. Site-scoping is what stops the query drifting
# into news articles and fan pages.
_PLATFORM_QUERY = {
    Platform.INSTAGRAM: "site:instagram.com {name} {role}",
    Platform.TWITTER: "(site:twitter.com OR site:x.com) {name} {role}",
    Platform.FACEBOOK: "site:facebook.com {name} {role}",
    Platform.YOUTUBE: "site:youtube.com {name} {role}",
    Platform.TIKTOK: "site:tiktok.com {name} {role}",
    Platform.LINKEDIN: "site:linkedin.com/in {name} {role}",
    Platform.IMDB: "site:imdb.com/name {name} {role}",
    Platform.WIKIPEDIA: "site:wikipedia.org {name} {role}",
}

# The aggregator sweep is the highest-value single query in this module: one hit
# yields the whole cluster, self-declared, which the pipeline can then trust.
_AGGREGATOR_QUERY = (
    "({sites}) {name} {role}"
)
_AGG_SITES = " OR ".join(
    f"site:{d}" for d in
    ("linktr.ee", "beacons.ai", "bio.link", "allmylinks.com", "komi.io",
     "hoo.be", "solo.to", "stan.store", "campsite.bio", "lnk.bio")
)


@dataclass
class Proposal:
    """A handle search suggested. Unverified by construction."""
    ref: PlatformRef
    query: str
    rank: int
    title: str = ""
    snippet: str = ""
    name_in_title: bool = False

    def as_dict(self) -> dict:
        return {"platform": self.ref.platform.value, "handle": self.ref.handle,
                "url": self.ref.canonical_url, "rank": self.rank,
                "title": self.title[:120], "query": self.query,
                "name_in_title": self.name_in_title,
                "status": "unverified"}


@dataclass
class DiscoveryResult:
    proposals: list[Proposal] = field(default_factory=list)
    aggregator_urls: list[str] = field(default_factory=list)
    queries_run: int = 0
    errors: list[str] = field(default_factory=list)

    def by_platform(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for p in self.proposals:
            out.setdefault(p.ref.platform.value, []).append(p.as_dict())
        return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _serp_url(query: str, api_key: str, engine: str = "google",
              num: int = 10) -> str:
    return SERPAPI_ENDPOINT + "?" + urlencode({
        "engine": engine, "q": query, "num": num, "api_key": api_key,
    })


def _organic(data) -> list[dict]:
    if not isinstance(data, dict):
        return []
    rows = data.get("organic_results")
    return rows if isinstance(rows, list) else []


def discover(name: str, fetch, api_key: str, role: str = "",
             engine: str = "google",
             platforms: Optional[list[Platform]] = None,
             include_aggregators: bool = True) -> DiscoveryResult:
    """
    Ask a search engine what handles might belong to this name.

    Every returned Proposal is unverified. Feed the aggregator_urls into the
    normal cascade to turn guesses into Tier-2 evidence.
    """
    res = DiscoveryResult()
    if not api_key:
        res.errors.append("SERPAPI_API_KEY is not set - discovery disabled.")
        return res
    if not name.strip():
        res.errors.append("No name supplied.")
        return res

    name_key = _norm(name)

    # 1. the aggregator sweep first - one hit can end the search early
    if include_aggregators:
        q = _AGGREGATOR_QUERY.format(sites=_AGG_SITES, name=f'"{name}"',
                                     role=role).strip()
        data = fetch(_serp_url(q, api_key, engine), kind="json")
        res.queries_run += 1
        for i, row in enumerate(_organic(data), 1):
            link = row.get("link") or ""
            if link and link not in res.aggregator_urls:
                res.aggregator_urls.append(link)

    # 2. per-platform site-scoped queries
    for plat in (platforms or list(_PLATFORM_QUERY)):
        tmpl = _PLATFORM_QUERY.get(plat)
        if not tmpl:
            continue
        q = tmpl.format(name=f'"{name}"', role=role).strip()
        data = fetch(_serp_url(q, api_key, engine), kind="json")
        res.queries_run += 1
        for i, row in enumerate(_organic(data), 1):
            ref = classify_url(row.get("link") or "")
            if not ref or ref.platform is not plat:
                continue
            if any(p.ref.key() == ref.key() for p in res.proposals):
                continue
            title = row.get("title") or ""
            res.proposals.append(Proposal(
                ref=ref, query=q, rank=i, title=title,
                snippet=(row.get("snippet") or "")[:200],
                name_in_title=name_key in _norm(title)))

    return res


def attach_as_unverified(cand, result: DiscoveryResult) -> int:
    """
    Record proposals on a Candidate at Tier 5.

    Tier 5 carries weight 0.0 and verdict 'reject', so nothing here can reach
    the output on search evidence alone. They become visible only if the
    verification pass finds independent support. That is deliberate.
    """
    n = 0
    for p in result.proposals:
        cand.add_claim(p.ref, Evidence(
            Tier.NAME_MATCH, "serpapi:search", p.ref.canonical_url,
            detail=f"rank {p.rank} for {p.query[:60]}"))
        n += 1
    return n
