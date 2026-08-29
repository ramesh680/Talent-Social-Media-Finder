"""
Evidence model.

Every handle we output carries a tier + a source url + a timestamp. Two payoffs:

  1. auto-accept / review / reject becomes a threshold, not a vibe
  2. when a handle turns out wrong, you know WHICH tier misfired and can
     retune one number instead of rewriting the pipeline

Rule that must never be relaxed: a name string match is Tier 5 and can never
by itself promote a claim to accepted. That rule is the whole point of the tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional

from .platforms import Platform, PlatformRef


class Tier(IntEnum):
    STRUCTURED_ID = 1     # Wikidata claim, TMDB external_ids, MusicBrainz relation
    SELF_DECLARED = 2     # aggregator page, bio link, schema.org sameAs
    BIDIRECTIONAL = 3     # A links to B AND B links to A  <- strongest in practice
    CORROBORATING = 4     # avatar hash, verified badge, shared domain/email
    NAME_MATCH = 5        # never sufficient alone


TIER_WEIGHT: dict[Tier, float] = {
    Tier.BIDIRECTIONAL: 0.78,   # gold standard: sufficient alone
    Tier.STRUCTURED_ID: 0.55,
    Tier.SELF_DECLARED: 0.45,   # structured + self-declared must clear 0.75
    Tier.CORROBORATING: 0.38,   # alone -> review (worth a glance), never accept
    Tier.NAME_MATCH: 0.00,
}

AUTO_ACCEPT = 0.75
REVIEW_FLOOR = 0.35


@dataclass
class Evidence:
    tier: Tier
    source: str                     # 'wikidata:P2003' | 'linktr.ee/foo' | 'avatar_phash'
    source_url: str = ""
    detail: str = ""
    authoritative: bool = False
    """
    True only for sources where the id is a STRUCTURAL identity link rather
    than an editable claim - a Wikidata sitelink (1:1 with the Q-item by
    construction) or a curated IMDb nm-id. These are self-sufficient.

    Volunteer-added social handles (P2003, P2002...) are NOT authoritative:
    they go stale when someone rebrands, so they still want corroboration.
    """
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class HandleClaim:
    """One (platform, handle) plus everything that argues for it."""
    ref: PlatformRef
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, ev: Evidence) -> "HandleClaim":
        self.evidence.append(ev)
        return self

    @property
    def tiers(self) -> set[Tier]:
        return {e.tier for e in self.evidence}

    @property
    def best_tier(self) -> Optional[Tier]:
        return min(self.tiers) if self.evidence else None

    def confidence(self) -> float:
        """
        Independent-corroboration model: each DISTINCT tier contributes once,
        with diminishing returns. Two sources of the same kind are worth far
        less than two sources of different kinds - which is exactly the real
        epistemics, since same-kind sources tend to copy each other.
        """
        if not self.evidence:
            return 0.0
        remaining = 1.0
        for tier in sorted(self.tiers):
            remaining *= (1.0 - TIER_WEIGHT[tier])
        score = 1.0 - remaining
        # a second independent source within the same tier: small top-up
        for tier in self.tiers:
            n = sum(1 for e in self.evidence if e.tier == tier)
            if n > 1 and TIER_WEIGHT[tier] > 0:
                score += min(n - 1, 2) * 0.03
        if any(e.authoritative for e in self.evidence):
            score = max(score, AUTO_ACCEPT + 0.05)
        return round(min(score, 0.99), 3)

    def verdict(self) -> str:
        c = self.confidence()
        if self.tiers == {Tier.NAME_MATCH}:
            return "reject"          # hard rule, regardless of score
        if c >= AUTO_ACCEPT:
            return "accept"
        if c >= REVIEW_FLOOR:
            return "review"
        return "reject"


@dataclass
class Candidate:
    """A possible human behind the queried name."""
    entity_id: str                       # 'Q23505' or 'nm0000138'
    label: str = ""
    description: str = ""
    occupations: list[str] = field(default_factory=list)   # Q-ids
    roles: list[str] = field(default_factory=list)         # our buckets
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    citizenship: list[str] = field(default_factory=list)
    notable_work: list[str] = field(default_factory=list)
    external_ids: dict[str, str] = field(default_factory=dict)
    claims: dict[str, HandleClaim] = field(default_factory=dict)
    role_score: float = 0.0
    thumbnail: str = ""
    label_match: dict = field(default_factory=dict)
    """How the queried name matched this entity: {score, how, matched}.
    'how' is exact | normalized | initials | token | fuzzy | translit — worth
    surfacing, because a 'translit' match deserves more scrutiny than 'exact'."""

    def add_claim(self, ref: PlatformRef, ev: Evidence) -> None:
        k = ref.key()
        if k not in self.claims:
            self.claims[k] = HandleClaim(ref)
        self.claims[k].add(ev)

    def accepted(self) -> dict[Platform, list[HandleClaim]]:
        out: dict[Platform, list[HandleClaim]] = {}
        for c in self.claims.values():
            if c.verdict() == "accept":
                out.setdefault(c.ref.platform, []).append(c)
        return out

    def coverage(self, targets) -> float:
        got = set(self.accepted().keys())
        return round(len(got & set(targets)) / max(len(targets), 1), 3)


def score_role_match(cand: Candidate, expected_role: Optional[str],
                     expected_country: Optional[str] = None,
                     expected_active_year: Optional[int] = None) -> float:
    """
    The 'director vs actor vs singer' discriminator.

    Deliberately does NOT look at the name at all - by the time we are here,
    every candidate matches the name. Only role/era/country separate them.
    """
    score = 0.0
    if expected_role:
        if expected_role in cand.roles:
            score += 0.60
        elif cand.roles:
            score -= 0.25            # positively wrong vertical
    if expected_country and cand.citizenship:
        score += 0.15 if expected_country in cand.citizenship else -0.10
    if expected_active_year and cand.birth_year:
        age = expected_active_year - cand.birth_year
        if 12 <= age <= 90:
            score += 0.15
        else:
            score -= 0.30            # dead or unborn at the relevant time
    if cand.death_year and expected_active_year and expected_active_year > cand.death_year:
        score -= 0.40
    # a person with many external ids is the notable one, weak but real signal
    score += min(len(cand.external_ids), 6) * 0.02
    cand.role_score = round(score, 3)
    return cand.role_score


def rank(candidates: list[Candidate], margin: float = 0.25) -> dict:
    """
    Returns a routing decision, not just a winner.

    'disambiguate' is a feature, not a failure: a human clicking one card in
    3 seconds beats 20 minutes of searching, and beats a silently wrong guess.
    """
    if not candidates:
        return {"decision": "not_found", "winner": None, "candidates": []}
    ranked = sorted(candidates, key=lambda c: c.role_score, reverse=True)
    top = ranked[0]
    if len(ranked) == 1:
        return {"decision": "resolved", "winner": top, "candidates": ranked}
    gap = top.role_score - ranked[1].role_score
    if top.role_score <= 0:
        return {"decision": "disambiguate", "winner": None, "candidates": ranked,
                "reason": "no candidate matches the expected role"}
    if gap >= margin:
        return {"decision": "resolved", "winner": top, "candidates": ranked,
                "margin": round(gap, 3)}
    return {"decision": "disambiguate", "winner": None, "candidates": ranked,
            "margin": round(gap, 3),
            "reason": f"top two within {margin} - collision, human picks"}
