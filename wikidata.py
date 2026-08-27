"""
Candidate lookup against Wikidata.

Replaces the exact-label SPARQL query. Two stages:

  RECALL     wbsearchentities across name variants and languages. This is
             Wikidata's own search index: alias-aware, diacritic-tolerant, and
             it already holds each person's name in every script they are known
             in, which is what makes cross-script matching work without us
             doing any transliteration.

  PRECISION  batch wbgetentities on the hits to keep only humans (P31=Q5) and
             pull occupation / dates / citizenship, then score the label match
             and drop anything that is not really the same name.

Why not SPARQL for recall: there is no cheap way to normalise labels
server-side, and `CONTAINS`/`REGEX` over all labels is a timeout. Why not
SPARQL only for precision: wbgetentities takes 50 ids per call, so one request
covers a whole candidate set.
"""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlencode

from .authorities import role_for_occupation
from .evidence import Candidate, score_role_match
from .labels import (LABEL_FLOOR, best_label_match, normalize,
                     search_languages, variants)

WD_API = "https://www.wikidata.org/w/api.php"
Q_HUMAN = "Q5"


def _search_url(term: str, lang: str, limit: int = 15) -> str:
    return WD_API + "?" + urlencode({
        "action": "wbsearchentities", "search": term, "language": lang,
        "uselang": lang, "type": "item", "limit": limit, "format": "json",
    })


def _entities_url(ids: list[str]) -> str:
    return WD_API + "?" + urlencode({
        "action": "wbgetentities", "ids": "|".join(ids),
        "props": "labels|aliases|claims|descriptions|sitelinks",
        "format": "json",
    })


def _first_id(claims: dict, prop: str) -> Optional[str]:
    for st in claims.get(prop, []):
        val = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(val, dict) and "id" in val:
            return val["id"]
    return None


def _all_ids(claims: dict, prop: str) -> list[str]:
    out = []
    for st in claims.get(prop, []):
        val = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(val, dict) and val.get("id"):
            out.append(val["id"])
    return out


def _time_year(claims: dict, prop: str) -> Optional[int]:
    for st in claims.get(prop, []):
        val = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(val, dict) and val.get("time"):
            t = val["time"]                      # e.g. '+1958-08-29T00:00:00Z'
            sign = -1 if t.startswith("-") else 1
            digits = t.lstrip("+-").split("-", 1)[0]
            if digits.isdigit():
                return sign * int(digits)
    return None


def _labels_and_aliases(ent: dict) -> list[str]:
    out: list[str] = []
    for v in (ent.get("labels") or {}).values():
        if v.get("value"):
            out.append(v["value"])
    for arr in (ent.get("aliases") or {}).values():
        for v in arr:
            if v.get("value"):
                out.append(v["value"])
    return out


def find_candidates(name: str, fetch: Callable, expected_role: Optional[str] = None,
                    country: Optional[str] = None,
                    active_year: Optional[int] = None,
                    max_queries: int = 6,
                    label_floor: float = LABEL_FLOOR) -> dict:
    """
    Return {"candidates": [...], "queries_run": n, "dropped": [...],
            "hit_ids": n} — every candidate a real human whose name plausibly
    matches, with label-match provenance attached.
    """
    if not (name or "").strip():
        return {"candidates": [], "queries_run": 0, "dropped": [], "hit_ids": 0}

    langs = search_languages(name)
    terms = variants(name)

    # Budget queries: the faithful variants in the name's own language first,
    # then English, then the looser variants. Most names resolve on query one.
    plan: list[tuple[str, str]] = []
    for lang in langs[:2]:
        for term in terms[:3]:
            plan.append((term, lang))
    for term in terms[3:]:
        plan.append((term, langs[0]))

    seen_ids: list[str] = []
    queries = 0
    for term, lang in plan:
        if queries >= max_queries:
            break
        data = fetch(_search_url(term, lang), kind="json") or {}
        queries += 1
        for row in (data.get("search") or []):
            qid = row.get("id")
            if qid and qid not in seen_ids:
                seen_ids.append(qid)
        # early exit: a strong exact hit in the first query is the common case
        if queries == 1 and len(seen_ids) == 1:
            break

    if not seen_ids:
        return {"candidates": [], "queries_run": queries, "dropped": [],
                "hit_ids": 0}

    candidates: list[Candidate] = []
    dropped: list[dict] = []
    for i in range(0, len(seen_ids[:100]), 50):
        chunk = seen_ids[i:i + 50]
        data = fetch(_entities_url(chunk), kind="json") or {}
        queries += 1
        for qid, ent in ((data.get("entities") or {}).items()):
            if not isinstance(ent, dict) or ent.get("missing") is not None:
                continue
            claims = ent.get("claims") or {}

            # humans only. A film, an album or a disambiguation page can share
            # a person's name and must never reach the candidate list.
            if Q_HUMAN not in _all_ids(claims, "P31"):
                dropped.append({"entity_id": qid, "why": "not a human (P31)"})
                continue

            labels = _labels_and_aliases(ent)
            lm = best_label_match(name, labels)
            if lm.score < label_floor:
                dropped.append({"entity_id": qid, "why": "label mismatch",
                                "score": lm.score, "closest": lm.matched_label})
                continue

            occ = _all_ids(claims, "P106")
            cand = Candidate(
                entity_id=qid,
                label=((ent.get("labels") or {}).get("en") or
                       next(iter((ent.get("labels") or {}).values()), {})
                       ).get("value", ""),
                description=((ent.get("descriptions") or {}).get("en") or {}
                             ).get("value", ""),
                occupations=occ,
                roles=sorted({r for o in occ if (r := role_for_occupation(o))}),
                birth_year=_time_year(claims, "P569"),
                death_year=_time_year(claims, "P570"),
                citizenship=_all_ids(claims, "P27"),
            )
            cand.label_match = {"score": lm.score, "how": lm.how,
                                "matched": lm.matched_label}
            score_role_match(cand, expected_role, country, active_year)
            # a weaker name match should not outrank a strong one on role alone
            cand.role_score = round(cand.role_score + (lm.score - 1.0) * 0.5, 3)
            candidates.append(cand)

    return {"candidates": candidates, "queries_run": queries,
            "dropped": dropped, "hit_ids": len(seen_ids)}
