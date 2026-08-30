"""
Orchestration.

All network I/O is injected as a single `fetch(url, kind) -> str|dict` callable.
That is deliberate: it keeps this module unit-testable offline, and it forces
rate limiting, caching, retries and user-agent policy to live in ONE place
instead of being sprinkled across 20 integrations.

Pipeline:

    intake(name, hints)
      -> entity store lookup            (never research the same name twice)
      -> candidate discovery            Wikidata, role-filtered
      -> role scoring + rank            resolved | disambiguate | not_found
      -> harvest structured ids         one Q-item read = every spoke id
      -> gap fill                       TMDB / MusicBrainz only where missing
      -> bio-link cascade               seed -> aggregator -> all platforms
      -> bidirectional confirmation
      -> confidence -> accept/review/reject
      -> write back with provenance
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

from .authorities import (ALL_AUTHORITIES, BY_KEY, TARGET_AUTHORITIES,
                          anchors_for_role, occupations_for_role,
                          role_for_occupation)
from .aggregators import (harvest, is_aggregator_url, looks_like_aggregator,
                          probe_urls, extract_links, extract_sameas)
from .discovery import (DiscoveryResult, Proposal,
                        attach_as_unverified, discover)
from .providers import enrich_from_tmdb
from .wikidata import find_candidates
from .wikibridge import qids_for_articles
from .freesearch import free_discover
from .evidence import (Candidate, Evidence, HandleClaim, Tier, rank,
                       score_role_match)
from .platforms import (Platform, PlatformRef, TARGET_PLATFORMS, build_url,
                        classify_url)

Fetch = Callable[..., object]

WD_API = "https://www.wikidata.org/w/api.php"
WD_SPARQL = "https://query.wikidata.org/sparql"


@dataclass
class Intake:
    """What an ingest request actually gives you - use ALL of it."""
    name: str
    expected_role: Optional[str] = None      # 'actor' | 'musician' | ...
    country: Optional[str] = None            # Q-id
    active_year: Optional[int] = None
    context: str = ""                        # show/brand/campaign it came from
    known_handles: dict[str, str] = field(default_factory=dict)  # platform->handle
    client: str = ""


# ---------------------------------------------------------------------------
# candidate discovery
# ---------------------------------------------------------------------------

_SPARQL_CANDIDATES = """
SELECT ?p ?pLabel ?pDesc ?occ ?dob ?dod ?cit ?img WHERE {{
  ?p rdfs:label|skos:altLabel "{name}"@en .
  ?p wdt:P31 wd:Q5 .
  OPTIONAL {{ ?p wdt:P106 ?occ. }}
  OPTIONAL {{ ?p wdt:P569 ?dob. }}
  OPTIONAL {{ ?p wdt:P570 ?dod. }}
  OPTIONAL {{ ?p wdt:P27  ?cit. }}
  OPTIONAL {{ ?p wdt:P18  ?img. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". 
    ?p rdfs:label ?pLabel . ?p schema:description ?pDesc . }}
}} LIMIT 200
"""


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def discover_candidates(intake: Intake, fetch: Fetch) -> list[Candidate]:
    """
    Wikidata candidate enumeration.

    Delegates to wikidata.find_candidates, which searches Wikidata's own
    multilingual index across name variants rather than matching labels
    exactly. The old exact-SPARQL version is kept below as
    discover_candidates_sparql for reference; it produced silent false
    negatives on "A.R. Rahman" vs "A. R. Rahman" and on every non-Latin script.
    """
    res = find_candidates(intake.name, fetch,
                          expected_role=intake.expected_role,
                          country=intake.country,
                          active_year=intake.active_year)
    return res["candidates"]


def discover_candidates_sparql(intake: Intake, fetch: Fetch) -> list[Candidate]:
    """
    One SPARQL call returns EVERY human with this name/alias, each already a
    distinct entity. This is the step that structurally eliminates your
    same-name problem - we are no longer matching strings, we are enumerating
    people and then filtering by role.
    """
    q = _SPARQL_CANDIDATES.format(name=intake.name.replace('"', '\\"'))
    url = f"{WD_SPARQL}?query={_urlq(q)}&format=json"
    data = fetch(url, kind="json")
    rows = (data or {}).get("results", {}).get("bindings", [])

    by_id: dict[str, Candidate] = {}
    for r in rows:
        qid = _qid(r["p"]["value"])
        c = by_id.setdefault(qid, Candidate(entity_id=qid))
        c.label = c.label or r.get("pLabel", {}).get("value", "")
        c.description = c.description or r.get("pDesc", {}).get("value", "")
        if "occ" in r:
            occ = _qid(r["occ"]["value"])
            if occ not in c.occupations:
                c.occupations.append(occ)
        if "cit" in r:
            cit = _qid(r["cit"]["value"])
            if cit not in c.citizenship:
                c.citizenship.append(cit)
        if "dob" in r and not c.birth_year:
            c.birth_year = _year(r["dob"]["value"])
        if "dod" in r and not c.death_year:
            c.death_year = _year(r["dod"]["value"])
        if "img" in r and not c.thumbnail:
            c.thumbnail = r["img"]["value"]

    for c in by_id.values():
        c.roles = sorted({r for o in c.occupations
                          if (r := role_for_occupation(o))})
        score_role_match(c, intake.expected_role, intake.country,
                         intake.active_year)
    return list(by_id.values())


def _year(iso: str) -> Optional[int]:
    m = re.match(r"^(-?\d{1,4})-", iso or "")
    return int(m.group(1)) if m else None


def _urlq(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ---------------------------------------------------------------------------
# structured id harvest - the hub read
# ---------------------------------------------------------------------------

def harvest_structured_ids(cand: Candidate, fetch: Fetch) -> None:
    """
    ONE wbgetentities call gives us every spoke id + every Wikipedia sitelink.
    This is the single highest-yield request in the whole pipeline.
    """
    url = (f"{WD_API}?action=wbgetentities&ids={cand.entity_id}"
           "&props=claims|sitelinks&format=json")
    data = fetch(url, kind="json")
    ent = ((data or {}).get("entities") or {}).get(cand.entity_id) or {}
    claims = ent.get("claims") or {}

    prop_to_auth = {a.wikidata_prop: a for a in ALL_AUTHORITIES if a.wikidata_prop}

    for prop, auth in prop_to_auth.items():
        for st in claims.get(prop, []):
            val = (((st.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
            if not isinstance(val, str):
                continue
            cand.external_ids[auth.key] = val
            if auth.is_target_platform and auth.platform:
                ref = PlatformRef(
                    auth.platform, val,
                    "channel_id" if auth.key == "youtube_channel" else "handle",
                    canonical_url=(auth.url_template or "").format(id=val))
                cand.add_claim(ref, Evidence(
                    Tier.STRUCTURED_ID, f"wikidata:{prop}",
                    f"https://www.wikidata.org/wiki/{cand.entity_id}",
                    detail=auth.label,
                    authoritative=auth.self_sufficient))

    for site, link in (ent.get("sitelinks") or {}).items():
        if not site.endswith("wiki") or site in ("commonswiki", "specieswiki"):
            continue
        lang = site[:-4].replace("_", "-")
        title = link.get("title", "")
        if not title:
            continue
        ref = PlatformRef(Platform.WIKIPEDIA, title, "page_title", lang=lang,
                          canonical_url=f"https://{lang}.wikipedia.org/wiki/"
                                        f"{title.replace(' ', '_')}")
        cand.add_claim(ref, Evidence(
            Tier.STRUCTURED_ID, f"wikidata:sitelink:{site}",
            f"https://www.wikidata.org/wiki/{cand.entity_id}",
            authoritative=True))


# ---------------------------------------------------------------------------
# gap fillers - called ONLY for platforms still missing
# ---------------------------------------------------------------------------

def fill_from_tmdb(cand: Candidate, fetch: Fetch, api_key: str) -> None:
    """TMDB /external_ids: imdb + ig + x + tiktok + yt + fb in one request."""
    tid = cand.external_ids.get("tmdb_person")
    if not tid:
        return
    url = (f"https://api.themoviedb.org/3/person/{tid}/external_ids"
           f"?api_key={api_key}")
    data = fetch(url, kind="json") or {}
    mapping = {
        "imdb_id": Platform.IMDB,
        "instagram_id": Platform.INSTAGRAM,
        "twitter_id": Platform.TWITTER,
        "tiktok_id": Platform.TIKTOK,
        "youtube_id": Platform.YOUTUBE,
        "facebook_id": Platform.FACEBOOK,
    }
    for field_name, plat in mapping.items():
        val = data.get(field_name)
        if not val or not isinstance(val, str):
            continue
        ref = PlatformRef(plat, val, "handle",
                          canonical_url=build_url(plat, val))
        cand.add_claim(ref, Evidence(
            Tier.STRUCTURED_ID, f"tmdb:{field_name}",
            f"https://www.themoviedb.org/person/{tid}",
            authoritative=(plat is Platform.IMDB)))


def fill_from_musicbrainz(cand: Candidate, fetch: Fetch) -> None:
    """MusicBrainz url-rels: the best structured source for singers."""
    mbid = cand.external_ids.get("musicbrainz_artist")
    if not mbid:
        return
    url = (f"https://musicbrainz.org/ws/2/artist/{mbid}"
           "?inc=url-rels&fmt=json")
    data = fetch(url, kind="json") or {}
    for rel in data.get("relations", []):
        target = (rel.get("url") or {}).get("resource")
        ref = classify_url(target or "")
        if ref:
            cand.add_claim(ref, Evidence(
                Tier.STRUCTURED_ID, f"musicbrainz:{rel.get('type', 'url')}",
                f"https://musicbrainz.org/artist/{mbid}"))


# ---------------------------------------------------------------------------
# bio-link cascade - the reverse path, where the real leverage is
# ---------------------------------------------------------------------------

def cascade_from_seed(cand: Candidate, seed_html: str, seed_url: str,
                      fetch: Fetch, max_pages: int = 4) -> list[str]:
    """
    seed profile html -> outbound links -> aggregator pages -> harvest all.

    Returns the aggregator urls visited, for provenance.
    """
    visited: list[str] = []

    for u in extract_sameas(seed_html):
        ref = classify_url(u)
        if ref:
            cand.add_claim(ref, Evidence(
                Tier.SELF_DECLARED, "schema.org:sameAs", seed_url))

    queue = []
    for u in extract_links(seed_html):
        if is_aggregator_url(u):
            queue.append(u)

    while queue and len(visited) < max_pages:
        agg_url = queue.pop(0)
        if agg_url in visited:
            continue
        visited.append(agg_url)
        html = fetch(agg_url, kind="text") or ""
        if not isinstance(html, str):
            continue
        for ref in harvest(html):
            cand.add_claim(ref, Evidence(
                Tier.SELF_DECLARED, "aggregator", agg_url,
                detail=(is_aggregator_url(agg_url) or "").name
                if is_aggregator_url(agg_url) else "generic"))

    return visited


def confirm_bidirectional(cand: Candidate, fetch: Fetch) -> None:
    """
    A links to B and B links to A. In practice this is the single most
    reliable signal available, so it gets the heaviest weight.
    """
    keys = list(cand.claims.keys())
    for k in keys:
        claim = cand.claims[k]
        if Tier.BIDIRECTIONAL in claim.tiers:
            continue
        url = claim.ref.canonical_url or build_url(claim.ref.platform,
                                                   claim.ref.handle)
        html = fetch(url, kind="text") or ""
        if not isinstance(html, str) or not html:
            continue
        back = {r.key() for r in harvest(html)}
        for other_key, other in cand.claims.items():
            if other_key == k:
                continue
            if other_key in back:
                cand.claims[other_key].add(Evidence(
                    Tier.BIDIRECTIONAL, f"backlink_from:{claim.ref.platform.value}",
                    url, detail=f"{claim.ref.platform.value} links to "
                                f"{other.ref.platform.value}"))


# ---------------------------------------------------------------------------
# entity store - so effort compounds instead of repeating
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity (
    entity_id   TEXT PRIMARY KEY,
    label       TEXT, description TEXT,
    roles       TEXT, birth_year INTEGER,
    external_ids TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS handle (
    entity_id TEXT, platform TEXT, handle TEXT, lang TEXT,
    id_kind TEXT, confidence REAL, verdict TEXT,
    best_tier INTEGER, evidence TEXT, updated_at TEXT,
    PRIMARY KEY (entity_id, platform, handle, lang)
);
CREATE TABLE IF NOT EXISTS collision (
    name TEXT PRIMARY KEY, candidate_count INTEGER,
    roles TEXT, first_seen TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS resolution_log (
    name TEXT, role_hint TEXT, decision TEXT, winner TEXT,
    margin REAL, at TEXT
);
CREATE INDEX IF NOT EXISTS idx_handle_lookup ON handle(platform, handle);
"""


class EntityStore:
    """
    Thread-safe by explicit lock, not by luck.

    gunicorn runs threaded workers, so a connection created on the main thread
    and used from a request thread raises. check_same_thread=False permits the
    handoff; the lock is what actually makes it correct, since sqlite3 will
    happily interleave writes from two threads and corrupt a transaction.
    """

    def __init__(self, path: str = "identityforge.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()

    def known_collision(self, name: str) -> Optional[dict]:
      with self._lock:
        row = self.conn.execute(
            "SELECT name, candidate_count, roles, note FROM collision "
            "WHERE lower(name)=lower(?)", (name,)).fetchone()
        if not row:
            return None
        return {"name": row[0], "candidate_count": row[1],
                "roles": json.loads(row[2] or "[]"), "note": row[3]}

    def record_collision(self, name: str, candidates: list[Candidate]) -> None:
      with self._lock:
        roles = sorted({r for c in candidates for r in c.roles})
        self.conn.execute(
            "INSERT OR REPLACE INTO collision VALUES (?,?,?,?,?)",
            (name, len(candidates), json.dumps(roles),
             _now(), "auto-detected during resolution"))
        self.conn.commit()

    def lookup_by_handle(self, platform: Platform, handle: str) -> Optional[str]:
      with self._lock:
        row = self.conn.execute(
            "SELECT entity_id FROM handle WHERE platform=? AND lower(handle)=lower(?)",
            (platform.value, handle)).fetchone()
        return row[0] if row else None

    def save(self, cand: Candidate) -> None:
      with self._lock:
        self.conn.execute(
            "INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?,?,?)",
            (cand.entity_id, cand.label, cand.description,
             json.dumps(cand.roles), cand.birth_year,
             json.dumps(cand.external_ids), _now()))
        for claim in cand.claims.values():
            self.conn.execute(
                "INSERT OR REPLACE INTO handle VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cand.entity_id, claim.ref.platform.value, claim.ref.handle,
                 claim.ref.lang or "", claim.ref.id_kind, claim.confidence(),
                 claim.verdict(),
                 int(claim.best_tier) if claim.best_tier else 9,
                 json.dumps([asdict(e) for e in claim.evidence]), _now()))
        self.conn.commit()

    def log(self, intake: Intake, result: dict) -> None:
      with self._lock:
        w = result.get("winner")
        self.conn.execute(
            "INSERT INTO resolution_log VALUES (?,?,?,?,?,?)",
            (intake.name, intake.expected_role or "", result["decision"],
             w.entity_id if w else "", result.get("margin"), _now()))
        self.conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# top-level entry point
# ---------------------------------------------------------------------------

def resolve(intake: Intake, fetch: Fetch, store: Optional[EntityStore] = None,
            tmdb_key: str = "", do_bidirectional: bool = True,
            serpapi_key: str = "", serpapi_engine: str = "google",
            allow_discovery: bool = True, free_probe: bool = False) -> dict:
    known = store.known_collision(intake.name) if store else None

    candidates = discover_candidates(intake, fetch)

    # No Wikidata item at all - the micro-influencer long tail. Search is the
    # only way to get a seed here, and everything it returns stays unverified
    # until the cascade corroborates it.
    if not candidates and allow_discovery:
        # FREE FIRST. MusicBrainz and MediaWiki cost nothing and cover a large
        # share of real talent lists, so paid search is a fallback rather than
        # the default. At 15k names this is the difference between ~$840 and
        # a bill that only covers what the free sources genuinely missed.
        free = free_discover(intake.name, fetch,
                             role=intake.expected_role or "",
                             country=intake.country or "",
                             try_probe=free_probe)

        if free.social_refs or free.wikipedia_urls:
            disc = DiscoveryResult()
            disc.wikipedia_urls = list(free.wikipedia_urls)
            disc.stopped_early = "resolved by free sources - no paid search"
            for ref in free.social_refs:
                disc.proposals.append(Proposal(
                    ref=ref, query="musicbrainz:url-rels", rank=1,
                    title="", snippet="", name_in_title=True))
            free_used = True
        elif serpapi_key:
            disc = discover(intake.name, fetch, serpapi_key,
                            role=intake.expected_role or "",
                            engine=serpapi_engine)
            free_used = False
        else:
            return {"decision": "not_found",
                    "reason": ("No entity in the identity graph, and the free "
                               "sources (MusicBrainz, MediaWiki) found nothing. "
                               "Set SERPAPI_API_KEY to enable paid search."),
                    "free_calls": free.calls, "free_notes": free.notes,
                    "candidates": [], "cards": []}

        # A Wikipedia hit is a bridge, not a dead end: the article maps 1:1 to
        # a Wikidata item, so we can re-enter the STRUCTURED pipeline and get
        # every platform id at Tier 1 instead of shipping search guesses.
        if disc.wikipedia_urls:
            qids = qids_for_articles(disc.wikipedia_urls, fetch)
            disc.bridged_qids = qids
            for qid in qids[:2]:
                cand = Candidate(entity_id=qid, label=intake.name)
                harvest_structured_ids(cand, fetch)
                if cand.claims:
                    cand.label_match = {"score": 0.75, "how": "wikipedia_bridge",
                                        "matched": disc.wikipedia_urls[0]}
                    candidates.append(cand)
            if candidates:
                for c in candidates:
                    score_role_match(c, intake.expected_role, intake.country,
                                     intake.active_year)
                decision = rank(candidates)
                if decision["decision"] == "resolved":
                    cand = decision["winner"]
                else:
                    cand = candidates[0]
                if tmdb_key:
                    enrich_from_tmdb(cand, fetch, tmdb_key, intake.expected_role)
                fill_from_musicbrainz(cand, fetch)

                # Same cascade as the normal path: a Wikidata social handle on
                # its own is Tier 1 and only reaches "review". Reading the
                # person's own bio link adds Tier 2 self-declared evidence,
                # which is what promotes a handle to accepted.
                for plat in (Platform.INSTAGRAM, Platform.TWITTER,
                             Platform.YOUTUBE, Platform.TIKTOK):
                    seed = next((c for c in cand.claims.values()
                                 if c.ref.platform is plat), None)
                    if seed:
                        html = fetch(seed.ref.canonical_url, kind="text")
                        if isinstance(html, str) and html:
                            cascade_from_seed(cand, html,
                                              seed.ref.canonical_url, fetch)
                        break
                for agg in disc.aggregator_urls[:2]:
                    html = fetch(agg, kind="text")
                    if isinstance(html, str) and html:
                        for ref in harvest(html):
                            cand.add_claim(ref, Evidence(
                                Tier.SELF_DECLARED, "aggregator", agg))
                if store:
                    store.save(cand)
                return {
                    "decision": "resolved",
                    "via": "wikipedia_bridge",
                    "entity_id": cand.entity_id,
                    "label": cand.label,
                    "roles": cand.roles,
                    "external_ids": cand.external_ids,
                    "coverage": cand.coverage(TARGET_PLATFORMS),
                    "queries_run": disc.queries_run,
                    "handles": {
                        plat.value: [
                            {"handle": c.ref.handle, "lang": c.ref.lang,
                             "url": c.ref.canonical_url,
                             "confidence": c.confidence(),
                             "tiers": sorted(int(t) for t in c.tiers),
                             "sources": [e.source for e in c.evidence]}
                            for c in claims
                        ] for plat, claims in cand.accepted().items()
                    },
                    "needs_review": [
                        {"platform": c.ref.platform.value, "handle": c.ref.handle,
                         "confidence": c.confidence(),
                         "tiers": sorted(int(t) for t in c.tiers),
                         "sources": [e.source for e in c.evidence]}
                        for c in cand.claims.values() if c.verdict() == "review"
                    ],
                }

        if free_used and disc.proposals:
            # MusicBrainz url-rels are declared on the artist's own catalogue
            # record, so they are self-declared evidence (Tier 2), not a search
            # guess (Tier 5). That difference is what lets them be accepted.
            cand = Candidate(entity_id=f"mb:{(free.musicbrainz_ids or ['?'])[0]}",
                             label=intake.name,
                             roles=[intake.expected_role] if intake.expected_role else [])
            for p_ in disc.proposals:
                cand.add_claim(p_.ref, Evidence(
                    Tier.STRUCTURED_ID, "musicbrainz:url-rel",
                    p_.ref.canonical_url))

            # One structured source only reaches "review". Reading the artist's
            # own bio link adds Tier 2 self-declared evidence and promotes the
            # handles to accepted - and costs nothing, since it is a plain page
            # fetch rather than a billed search.
            for plat in (Platform.INSTAGRAM, Platform.TWITTER,
                         Platform.YOUTUBE, Platform.TIKTOK):
                seed = next((c for c in cand.claims.values()
                             if c.ref.platform is plat), None)
                if seed:
                    html = fetch(seed.ref.canonical_url, kind="text")
                    if isinstance(html, str) and html:
                        cascade_from_seed(cand, html, seed.ref.canonical_url,
                                          fetch)
                    break
            for agg in free.aggregator_urls[:2]:
                html = fetch(agg, kind="text")
                if isinstance(html, str) and html:
                    for ref in harvest(html):
                        cand.add_claim(ref, Evidence(
                            Tier.SELF_DECLARED, "aggregator", agg))
            if store:
                store.save(cand)
            return {
                "decision": "resolved", "via": "musicbrainz",
                "entity_id": cand.entity_id, "label": cand.label,
                "roles": cand.roles,
                "external_ids": {"musicbrainz_artist": (free.musicbrainz_ids or [""])[0]},
                "coverage": cand.coverage(TARGET_PLATFORMS),
                "free_calls": free.calls, "paid_searches": 0,
                "handles": {
                    plat.value: [
                        {"handle": c.ref.handle, "lang": c.ref.lang,
                         "url": c.ref.canonical_url, "confidence": c.confidence(),
                         "tiers": sorted(int(t) for t in c.tiers),
                         "sources": [e.source for e in c.evidence]}
                        for c in claims
                    ] for plat, claims in cand.accepted().items()
                },
                "needs_review": [
                    {"platform": c.ref.platform.value, "handle": c.ref.handle,
                     "confidence": c.confidence(),
                     "sources": [e.source for e in c.evidence]}
                    for c in cand.claims.values() if c.verdict() == "review"
                ],
            }

        prov = [p.as_dict() for p in disc.proposals]
        result = {"decision": "unverified_only",
                  "reason": ("No entity in the identity graph, so nothing can be "
                             "verified structurally. These are search proposals "
                             "only - confirm one before use."),
                  "proposals": prov,
                  "proposals_by_platform": disc.by_platform(),
                  "aggregator_urls": disc.aggregator_urls,
                  "wikipedia_urls": disc.wikipedia_urls,
                  "queries_run": disc.queries_run,
                  "stopped_early": disc.stopped_early,
                  "errors": disc.errors,
                  "candidates": [], "cards": []}
        if store:
            store.log(intake, {"decision": "unverified_only", "winner": None})
        return result

    if store and len(candidates) > 1:
        store.record_collision(intake.name, candidates)

    decision = rank(candidates)
    if store:
        store.log(intake, decision)

    if decision["decision"] != "resolved":
        # Say which of the two very different problems this is: no hint given,
        # or a hint given that nobody matches. They need opposite fixes.
        if decision["decision"] == "disambiguate" and not intake.expected_role:
            decision["reason"] = (
                f"{len(decision['candidates'])} people share this name and no "
                "role hint was supplied. Add an expected role and this usually "
                "resolves on its own.")
        return {
            **decision,
            "pre_flagged_collision": known,
            "cards": [_card(c) for c in decision["candidates"][:6]],
        }

    cand: Candidate = decision["winner"]
    harvest_structured_ids(cand, fetch)
    provider_info: dict = {}
    if tmdb_key:
        provider_info["tmdb"] = enrich_from_tmdb(
            cand, fetch, tmdb_key, intake.expected_role)
    fill_from_musicbrainz(cand, fetch)

    # seed for the cascade: prefer a platform whose bio reliably carries a link
    for plat in (Platform.INSTAGRAM, Platform.TWITTER, Platform.YOUTUBE,
                 Platform.TIKTOK):
        seed = next((c for c in cand.claims.values()
                     if c.ref.platform is plat), None)
        if seed:
            url = seed.ref.canonical_url
            html = fetch(url, kind="text")
            if isinstance(html, str) and html:
                cascade_from_seed(cand, html, url, fetch)
            break

    if do_bidirectional:
        confirm_bidirectional(cand, fetch)

    if store:
        store.save(cand)

    return {
        "decision": "resolved",
        "provider_info": provider_info,
        "entity_id": cand.entity_id,
        "label": cand.label,
        "roles": cand.roles,
        "margin": decision.get("margin"),
        "external_ids": cand.external_ids,
        "coverage": cand.coverage(TARGET_PLATFORMS),
        "handles": {
            plat.value: [
                {"handle": c.ref.handle, "lang": c.ref.lang,
                 "url": c.ref.canonical_url, "confidence": c.confidence(),
                 "tiers": sorted(int(t) for t in c.tiers),
                 "sources": [e.source for e in c.evidence]}
                for c in claims
            ] for plat, claims in cand.accepted().items()
        },
        "needs_review": [
            {"platform": c.ref.platform.value, "handle": c.ref.handle,
             "confidence": c.confidence(),
             "sources": [e.source for e in c.evidence]}
            for c in cand.claims.values() if c.verdict() == "review"
        ],
    }


def _card(c: Candidate) -> dict:
    """Payload for the human disambiguation card - 3 seconds to decide."""
    return {
        "entity_id": c.entity_id,
        "label": c.label,
        "description": c.description,
        "roles": c.roles,
        "born": c.birth_year,
        "died": c.death_year,
        "thumbnail": c.thumbnail,
        "role_score": c.role_score,
        "id_count": len(c.external_ids),
        "wikidata": f"https://www.wikidata.org/wiki/{c.entity_id}",
    }
