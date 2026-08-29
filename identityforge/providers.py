"""
TMDB and OMDb providers.

TMDB is the strongest structured source for actors and directors, and
`/person/{id}/external_ids` returns imdb + instagram + twitter + tiktok +
youtube + facebook in a single request. Two ways in:

  1. Wikidata already had P4985 -> use it directly (free, no search needed)
  2. Wikidata had no TMDB id  -> search TMDB by name, then DISAMBIGUATE the
     search results the same way we disambiguate Wikidata candidates

Point 2 matters. TMDB's /search/person is a name search, so it has the identical
collision problem: query "Michael Jackson" and you get several people. Taking
result[0] would reintroduce exactly the bug this tool exists to fix, so the
search path scores candidates on known_for_department and popularity and refuses
a close call.

OMDb is honestly a poor fit for person resolution - it is a TITLE database and
exposes no person-search endpoint. It is wired here for one narrow job:
confirming a notable work on a disambiguation card, which helps a human choose.
It contributes no handles and no identity evidence.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from .evidence import Candidate, Evidence, Tier
from .platforms import Platform, PlatformRef, build_url

TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "https://www.omdbapi.com/"

# TMDB department -> our role buckets, for scoring search hits
_DEPT_TO_ROLE = {
    "Acting": {"actor", "comedian", "model"},
    "Directing": {"director"},
    "Writing": {"writer", "director"},
    "Production": {"director", "executive"},
    "Sound": {"musician"},
    "Crew": set(),
}


def _tmdb_url(path: str, api_key: str, **params) -> str:
    q = {k: v for k, v in params.items() if v not in (None, "")}
    if api_key:
        q["api_key"] = api_key
    return f"{TMDB_BASE}{path}?{urlencode(q)}"


def search_person(name: str, fetch, api_key: str,
                  expected_role: Optional[str] = None,
                  margin: float = 1.6) -> dict:
    """
    Find a TMDB person id for `name` without guessing on a collision.

    Returns {"decision": "resolved"|"ambiguous"|"not_found",
             "tmdb_id": str|None, "candidates": [...]}
    """
    data = fetch(_tmdb_url("/search/person", api_key, query=name,
                           include_adult="false"), kind="json") or {}
    results = data.get("results") or []
    if not results:
        return {"decision": "not_found", "tmdb_id": None, "candidates": []}

    scored = []
    for r in results:
        dept = r.get("known_for_department") or ""
        pop = float(r.get("popularity") or 0)
        score = min(pop, 50.0) / 50.0          # 0..1, popularity as weak prior
        if expected_role:
            roles = _DEPT_TO_ROLE.get(dept, set())
            score += 1.0 if expected_role in roles else -0.5
        # exact label match matters, but only as a tiebreak
        if (r.get("name") or "").strip().lower() == name.strip().lower():
            score += 0.3
        scored.append({
            "tmdb_id": str(r.get("id")),
            "name": r.get("name") or "",
            "department": dept,
            "popularity": pop,
            "known_for": [w.get("title") or w.get("name") or ""
                          for w in (r.get("known_for") or [])][:3],
            "score": round(score, 3),
        })

    scored.sort(key=lambda c: c["score"], reverse=True)
    if len(scored) == 1:
        return {"decision": "resolved", "tmdb_id": scored[0]["tmdb_id"],
                "candidates": scored}
    gap = scored[0]["score"] - scored[1]["score"]
    if scored[0]["score"] > 0 and gap >= (margin - 1.0):
        return {"decision": "resolved", "tmdb_id": scored[0]["tmdb_id"],
                "candidates": scored[:6], "margin": round(gap, 3)}
    return {"decision": "ambiguous", "tmdb_id": None,
            "candidates": scored[:6], "margin": round(gap, 3)}


_EXTERNAL_FIELDS = {
    "imdb_id": Platform.IMDB,
    "instagram_id": Platform.INSTAGRAM,
    "twitter_id": Platform.TWITTER,
    "tiktok_id": Platform.TIKTOK,
    "youtube_id": Platform.YOUTUBE,
    "facebook_id": Platform.FACEBOOK,
}


def enrich_from_tmdb(cand: Candidate, fetch, api_key: str,
                     expected_role: Optional[str] = None) -> dict:
    """
    Fill platform gaps from TMDB. Uses the Wikidata-supplied id when present,
    otherwise searches - and records how the id was obtained, because a
    searched id is weaker provenance than a Wikidata-asserted one.
    """
    info = {"tmdb_id": cand.external_ids.get("tmdb_person"),
            "source": "wikidata", "added": []}

    if not info["tmdb_id"]:
        found = search_person(cand.label or "", fetch, api_key, expected_role)
        info["search"] = found
        if found["decision"] != "resolved":
            info["source"] = "none"
            return info
        info["tmdb_id"] = found["tmdb_id"]
        info["source"] = "tmdb_search"
        cand.external_ids["tmdb_person"] = found["tmdb_id"]

    data = fetch(_tmdb_url(f"/person/{info['tmdb_id']}/external_ids",
                           api_key), kind="json") or {}
    for field_name, plat in _EXTERNAL_FIELDS.items():
        val = data.get(field_name)
        if not val or not isinstance(val, str):
            continue
        val = val.strip().lstrip("@")
        if not val:
            continue
        ref = PlatformRef(plat, val, "handle", canonical_url=build_url(plat, val))
        # A TMDB id that came from a name SEARCH is not a structured identity
        # link - demote it so it needs corroboration like any other guess.
        tier = (Tier.STRUCTURED_ID if info["source"] == "wikidata"
                else Tier.CORROBORATING)
        cand.add_claim(ref, Evidence(
            tier, f"tmdb:{field_name}",
            f"https://www.themoviedb.org/person/{info['tmdb_id']}",
            detail=f"id via {info['source']}",
            authoritative=(plat is Platform.IMDB
                           and info["source"] == "wikidata")))
        info["added"].append(plat.value)
    return info


def omdb_title(imdb_or_title: str, fetch, api_key: str) -> dict:
    """
    Look up ONE title for disambiguation-card context.

    OMDb has no person endpoint, so this cannot resolve handles. It exists only
    to put a recognisable credit in front of the human choosing between
    same-named candidates.
    """
    if not api_key or not imdb_or_title:
        return {}
    key = "i" if imdb_or_title.lower().startswith("tt") else "t"
    url = OMDB_BASE + "?" + urlencode({key: imdb_or_title, "apikey": api_key})
    data = fetch(url, kind="json") or {}
    if data.get("Response") != "True":
        return {}
    return {"title": data.get("Title"), "year": data.get("Year"),
            "type": data.get("Type"), "imdb_id": data.get("imdbID"),
            "director": data.get("Director"), "actors": data.get("Actors")}
