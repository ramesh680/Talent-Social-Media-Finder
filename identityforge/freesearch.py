"""
Free discovery. No API key, no billing, no quota to run out of.

WHY THIS EXISTS
---------------
SerpAPI works, but at 15,000 talents it is roughly 56,000 searches and about
$840. Most of what we were paying Google to find is available directly from
APIs that are free and, in two cases, purpose-built for exactly this job.

The paid search becomes a last resort instead of the default:

    1. MusicBrainz artist search   FREE   musicians - and it returns the
                                          social URLs directly as url-rels
    2. MediaWiki search            FREE   fuzzy name -> article -> Q-id ->
                                          every structured ID
    3. Aggregator handle probe     FREE   guess the slug, fetch the page
    4. SerpAPI                     PAID   only if the above found nothing

Step 2 is the same Wikipedia bridge Ramesh asked for, just sourced from
MediaWiki's own search endpoint instead of buying the result from Google.
Google fuzzy-matches names better, but MediaWiki's search is close enough on
names that have an article at all, and it costs nothing.

Step 1 matters most for this workload: a list of 25 emerging musicians is
precisely what MusicBrainz covers well, and its artist records carry
Instagram, X, Facebook, YouTube, SoundCloud and Bandcamp as typed relations -
self-declared by the artist's own catalogue entry, not guessed.

HONEST LIMITS
-------------
- MusicBrainz asks for <=1 request/second. Free, but 15,000 names is ~4 hours
  of wall clock. That is a background-job workload, not a web request.
- MediaWiki search finds people who HAVE an article. Genuinely undocumented
  micro-influencers still will not appear, and no free source fixes that.
- Handle probing produces candidates, never confirmations. A page existing at
  linktr.ee/name does not prove it is your person; the cascade still has to
  corroborate it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote, urlencode

from .labels import search_languages
from .platforms import Platform, PlatformRef, classify_url

MEDIAWIKI_API = "https://{lang}.wikipedia.org/w/api.php"
MB_ARTIST = "https://musicbrainz.org/ws/2/artist"


@dataclass
class FreeResult:
    wikipedia_urls: list[str] = field(default_factory=list)
    aggregator_urls: list[str] = field(default_factory=list)
    social_refs: list[PlatformRef] = field(default_factory=list)
    musicbrainz_ids: list[str] = field(default_factory=list)
    calls: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def found_anything(self) -> bool:
        return bool(self.wikipedia_urls or self.social_refs
                    or self.aggregator_urls or self.musicbrainz_ids)


# ---------------------------------------------------------------------------
# 1. MediaWiki search - the free version of the Wikipedia step
# ---------------------------------------------------------------------------

# Country Q-id -> the Wikipedia editions most likely to carry that person.
# A Turkish presenter or a Thai actor usually has an article in their own
# language long before an English one exists, so searching only en.wikipedia
# silently misses most non-Anglophone talent.
COUNTRY_LANGS: dict[str, tuple[str, ...]] = {
    "Q668": ("hi", "ta", "te", "bn", "mr"),     # India
    "Q30": ("en",), "Q145": ("en",), "Q16": ("en", "fr"),
    "Q408": ("en",), "Q17": ("ja",), "Q884": ("ko",),
    "Q142": ("fr",), "Q183": ("de",), "Q29": ("es",), "Q38": ("it",),
    "Q155": ("pt",), "Q96": ("es",), "Q1033": ("en", "yo"),
    "Q252": ("id",), "Q928": ("tl", "en"), "Q869": ("th",),
    "Q148": ("zh",), "Q159": ("ru",), "Q55": ("nl",), "Q34": ("sv",),
    "Q878": ("ar", "en"), "Q851": ("ar",), "Q334": ("en", "zh"),
    "Q43": ("tr",), "Q794": ("fa",), "Q801": ("he",), "Q212": ("uk",),
    "Q219": ("bg",), "Q403": ("sr",), "Q36": ("pl",), "Q213": ("cs",),
    "Q20": ("no",), "Q35": ("da",), "Q33": ("fi",), "Q45": ("pt",),
    "Q414": ("es",), "Q298": ("es",), "Q739": ("es",), "Q77": ("es",),
    "Q265": ("uz",), "Q232": ("kk",), "Q843": ("ur",), "Q902": ("bn",),
    "Q117": ("en",), "Q1005": ("en",), "Q258": ("en", "af"),
    "Q79": ("ar",), "Q1028": ("ar", "fr"), "Q262": ("ar", "fr"),
    "Q189": ("is",), "Q40": ("de",), "Q39": ("de", "fr", "it"),
    "Q31": ("nl", "fr"), "Q27": ("en", "ga"), "Q191": ("et",),
    "Q211": ("lv",), "Q37": ("lt",), "Q214": ("sk",), "Q215": ("sl",),
    "Q224": ("hr",), "Q41": ("el",), "Q28": ("hu",), "Q218": ("ro",),
}


def languages_for(name: str, country: str = "",
                  max_langs: int = 3) -> list[str]:
    """
    Which Wikipedia editions to search for this person.

    Combines the name's own script (a Devanagari name -> hi) with the country
    hint (an Indian actor with a Latin-spelled name -> hi, ta), and always
    keeps English because most notable people have an English article too.
    """
    langs: list[str] = []
    for lg in search_languages(name):
        if lg not in langs:
            langs.append(lg)
    for lg in COUNTRY_LANGS.get(country or "", ()):
        if lg not in langs:
            langs.append(lg)
    if "en" not in langs:
        langs.append("en")
    return langs[:max_langs]


def wikipedia_search(name: str, fetch: Callable, lang: str = "en",
                     limit: int = 5) -> list[str]:
    """One Wikipedia edition. Free, no key."""
    if not (name or "").strip():
        return []
    url = MEDIAWIKI_API.format(lang=lang) + "?" + urlencode({
        "action": "query", "list": "search", "srsearch": name,
        "srlimit": limit, "srnamespace": 0, "format": "json",
    })
    data = fetch(url, kind="json") or {}
    hits = ((data.get("query") or {}).get("search") or [])
    out = []
    for h in hits:
        title = h.get("title")
        if title:
            out.append(f"https://{lang}.wikipedia.org/wiki/"
                       f"{quote(title.replace(' ', '_'))}")
    return out


def wikipedia_search_multi(name: str, fetch: Callable, country: str = "",
                           max_langs: int = 3) -> list[str]:
    """
    Search several Wikipedia editions, stopping at the first that answers.

    This is the universal step: it works for every profession and every
    country, needs no key, and each hit bridges to a Wikidata item that carries
    the platform IDs.
    """
    urls: list[str] = []
    for lg in languages_for(name, country, max_langs):
        found = wikipedia_search(name, fetch, lang=lg)
        for u in found:
            if u not in urls:
                urls.append(u)
        if urls:
            break
    return urls


# ---------------------------------------------------------------------------
# 2. MusicBrainz - free, and the best fit for a musician list
# ---------------------------------------------------------------------------

def _mb_search_url(name: str, limit: int = 5) -> str:
    return MB_ARTIST + "?" + urlencode({
        "query": f'artist:"{name}"', "limit": limit, "fmt": "json"})


def _mb_lookup_url(mbid: str) -> str:
    return f"{MB_ARTIST}/{mbid}?inc=url-rels&fmt=json"


def musicbrainz_search(name: str, fetch: Callable,
                       min_score: int = 85) -> tuple[list[str], list[PlatformRef]]:
    """
    Find an artist by name and read their social URLs off the record.

    MusicBrainz returns a match score; anything below min_score is a different
    artist with a similar name, which is the exact error this tool exists to
    avoid, so it is dropped rather than kept as a maybe.
    """
    if not (name or "").strip():
        return [], []
    data = fetch(_mb_search_url(name), kind="json") or {}
    artists = data.get("artists") or []
    ids: list[str] = []
    for a in artists:
        try:
            score = int(a.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if score >= min_score and a.get("id"):
            ids.append(a["id"])

    refs: list[PlatformRef] = []
    for mbid in ids[:2]:
        rel = fetch(_mb_lookup_url(mbid), kind="json") or {}
        for r in rel.get("relations", []):
            target = (r.get("url") or {}).get("resource")
            ref = classify_url(target or "")
            if ref and not any(x.key() == ref.key() for x in refs):
                refs.append(ref)
    return ids, refs


# ---------------------------------------------------------------------------
# 2b. Other verticals - all free, all keyless. MusicBrainz is ONE of these,
#     not the front door.
# ---------------------------------------------------------------------------

SPORTSDB = "https://www.thesportsdb.com/api/v1/json/3/searchplayers.php"
OPENLIBRARY = "https://openlibrary.org/search/authors.json"
VIAF_SUGGEST = "https://viaf.org/viaf/AutoSuggest"


def sportsdb_search(name: str, fetch: Callable) -> list[PlatformRef]:
    """
    Athletes. Free public test key, and the player record carries social
    handles directly - the same shape of win MusicBrainz gives for musicians.
    """
    if not (name or "").strip():
        return []
    data = fetch(SPORTSDB + "?" + urlencode({"p": name}), kind="json") or {}
    players = data.get("player") or []
    refs: list[PlatformRef] = []
    for pl in players[:2]:
        for key in ("strInstagram", "strTwitter", "strFacebook", "strYoutube",
                    "strWebsite"):
            val = pl.get(key)
            if not val or not isinstance(val, str):
                continue
            url = val if val.startswith("http") else "https://" + val
            ref = classify_url(url)
            if ref and not any(x.key() == ref.key() for x in refs):
                refs.append(ref)
    return refs


def openlibrary_search(name: str, fetch: Callable) -> list[str]:
    """Writers. Free, no key. Returns Wikidata-linkable author keys."""
    if not (name or "").strip():
        return []
    data = fetch(OPENLIBRARY + "?" + urlencode({"q": name, "limit": 3}),
                 kind="json") or {}
    docs = data.get("docs") or []
    return [d["key"] for d in docs if d.get("key")][:3]


def viaf_search(name: str, fetch: Callable) -> list[str]:
    """
    Authors, academics, artists, institutions. Free, no key.

    VIAF clusters library authority records worldwide, so it is unusually good
    for non-Anglophone names that Wikipedia has not covered yet.
    """
    if not (name or "").strip():
        return []
    data = fetch(VIAF_SUGGEST + "?" + urlencode({"query": name}),
                 kind="json") or {}
    return [r.get("viafid") for r in (data.get("result") or [])[:3]
            if r.get("viafid")]


# role -> the free, keyless source that covers that vertical best.
# Absent from this table simply means "Wikipedia and Wikidata only", which is
# still universal coverage - not a failure.
VERTICAL_SOURCES: dict[str, str] = {
    "musician": "musicbrainz",
    "athlete": "sportsdb",
    "writer": "openlibrary",
    "journalist": "viaf",
}


# ---------------------------------------------------------------------------
# 3. Aggregator handle probing - free, just HTTP
# ---------------------------------------------------------------------------

_PROBE_HOSTS = ("linktr.ee", "beacons.ai", "bio.link", "allmylinks.com",
                "solo.to", "komi.io", "hoo.be", "stan.store")


def handle_candidates(name: str, limit: int = 4) -> list[str]:
    """Plausible slugs for a name: 'Cash Cobain' -> cashcobain, cash_cobain..."""
    base = re.sub(r"[^a-z0-9 ]+", "", (name or "").lower()).strip()
    toks = [t for t in base.split() if t]
    if not toks:
        return []
    joined = "".join(toks)
    out = [joined]
    if len(toks) > 1:
        out.append("_".join(toks))
        out.append("".join(toks) + "music")
        out.append(toks[0] + toks[-1])
    seen, uniq = set(), []
    for h in out:
        if 2 < len(h) <= 40 and h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq[:limit]


def probe_aggregators(name: str, fetch: Callable, max_probes: int = 8,
                      hosts: Optional[tuple] = None) -> list[str]:
    """
    Guess link-in-bio slugs and see which pages exist.

    Free, but noisy: a page existing proves nothing about identity, so hits go
    into the normal cascade as candidates and still need corroboration.
    """
    hosts = hosts or _PROBE_HOSTS
    found: list[str] = []
    probes = 0
    for h in handle_candidates(name):
        for host in hosts:
            if probes >= max_probes:
                return found
            url = f"https://{host}/{h}"
            probes += 1
            body = fetch(url, kind="text")
            if isinstance(body, str) and body.strip():
                low = body[:4000].lower()
                if "page not found" in low or "404" in low[:200]:
                    continue
                found.append(url)
    return found


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def free_discover(name: str, fetch: Callable, role: str = "",
                  country: str = "",
                  try_musicbrainz: Optional[bool] = None,
                  try_wikipedia: bool = True,
                  try_probe: bool = False,
                  max_langs: int = 3) -> FreeResult:
    """
    Zero-cost discovery for ANY profession, in ANY language.

    Order:
      1. Wikipedia, searched in the languages that match the name's script and
         the country - universal, one call, and every hit bridges to a Wikidata
         item carrying the platform IDs.
      2. The vertical source for this role, if there is a free one:
         musician -> MusicBrainz, athlete -> TheSportsDB, writer -> OpenLibrary,
         journalist -> VIAF. A role with no entry is not a failure; step 1
         already covers it.
      3. Aggregator slug probing, off by default (many requests, noisy).

    Wikipedia leads because it is the only source that is genuinely universal.
    The vertical sources are enrichment for the cases where a person is too
    niche for an encyclopaedia but well catalogued in their own industry.
    """
    res = FreeResult()
    if not (name or "").strip():
        return res

    # 1. universal
    if try_wikipedia:
        res.wikipedia_urls = wikipedia_search_multi(
            name, fetch, country=country, max_langs=max_langs)
        res.calls += 1
        if res.wikipedia_urls:
            res.notes.append(
                f"wikipedia: {len(res.wikipedia_urls)} candidate articles")

    # 2. vertical, only when the universal step came up empty
    source = VERTICAL_SOURCES.get(role or "")
    if try_musicbrainz is False:
        source = None if source == "musicbrainz" else source
    if try_musicbrainz and source is None:
        source = "musicbrainz"

    if source and not res.wikipedia_urls:
        if source == "musicbrainz":
            ids, refs = musicbrainz_search(name, fetch)
            res.calls += 1 + min(len(ids), 2)
            res.musicbrainz_ids = ids
            res.social_refs.extend(refs)
        elif source == "sportsdb":
            refs = sportsdb_search(name, fetch)
            res.calls += 1
            res.social_refs.extend(refs)
        elif source == "openlibrary":
            res.calls += 1
            if openlibrary_search(name, fetch):
                res.notes.append("openlibrary: author record exists")
        elif source == "viaf":
            res.calls += 1
            if viaf_search(name, fetch):
                res.notes.append("viaf: authority record exists")
        if res.social_refs:
            res.notes.append(f"{source}: {len(res.social_refs)} social urls")

    # 3. optional probing
    if try_probe and not res.found_anything:
        res.aggregator_urls = probe_aggregators(name, fetch)
        res.calls += 8
        if res.aggregator_urls:
            res.notes.append(
                f"probe: {len(res.aggregator_urls)} link-in-bio pages exist")

    return res
