"""
Name normalisation and variant generation.

THE PROBLEM THIS FIXES
----------------------
The original candidate query matched labels exactly:

    ?p rdfs:label|skos:altLabel "A. R. Rahman"@en

That is case-sensitive, punctuation-sensitive and diacritic-sensitive, so
"A.R. Rahman", "AR Rahman", "a. r. rahman" and "Allah Rakha Rahman" all return
nothing while the person sits right there in the graph. Silent false negatives
are worse than collisions here, because the operator reads "not_found" as "this
person has no Wikidata item" and goes back to manual work.

WHAT I TRIED AND REJECTED
-------------------------
Transliterating non-Latin names to Latin and comparing strings. Unidecode turns
"शाहरुख़ ख़ान" into "shaahrukh' kh'aan" (not "Shah Rukh Khan") and the Tamil
"ஏ. ஆர். ரகுமான்" into "ee. aar. rkumaannn" (not "A. R. Rahman"). Good enough to
tell two names apart, nowhere near good enough to match them.

So transliteration is used only as a weak scoring signal, never as the matching
mechanism. The actual cross-script matching is delegated to Wikidata, which
already stores each person's name in Devanagari, Tamil, Japanese and the rest as
labels and aliases on the same Q-item. Searching its multilingual index beats
anything we can do to the string locally.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

try:                                    # optional; degrades gracefully
    from unidecode import unidecode as _unidecode
except ImportError:                     # pragma: no cover
    def _unidecode(s: str) -> str:
        return s

# Titles and suffixes that carry no identity information.
_HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "dame",
    "lord", "lady", "rev", "reverend", "hon", "capt", "sgt", "shri", "smt",
    "sri", "pandit", "ustad", "maestro",
}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq", "obe", "mbe", "cbe"}

_SCRIPT_RANGES = [
    ("devanagari", 0x0900, 0x097F),
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("arabic", 0x0600, 0x06FF),
    ("hebrew", 0x0590, 0x05FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("greek", 0x0370, 0x03FF),
    ("hangul", 0xAC00, 0xD7AF),
    ("hiragana", 0x3040, 0x309F),
    ("katakana", 0x30A0, 0x30FF),
    ("cjk", 0x4E00, 0x9FFF),
    ("thai", 0x0E00, 0x0E7F),
]

# script -> Wikidata language codes worth searching in. Searching the graph in
# the name's own script is what actually solves cross-script matching.
SCRIPT_LANGS: dict[str, tuple[str, ...]] = {
    "devanagari": ("hi", "mr", "ne", "sa"),
    "bengali": ("bn",),
    "gurmukhi": ("pa",),
    "gujarati": ("gu",),
    "tamil": ("ta",),
    "telugu": ("te",),
    "kannada": ("kn",),
    "malayalam": ("ml",),
    "arabic": ("ar", "fa", "ur"),
    "hebrew": ("he",),
    "cyrillic": ("ru", "uk", "bg", "sr"),
    "greek": ("el",),
    "hangul": ("ko",),
    "hiragana": ("ja",),
    "katakana": ("ja",),
    "cjk": ("zh", "ja"),
    "thai": ("th",),
    "latin": ("en",),
}


def script_of(text: str) -> str:
    """Dominant non-Latin script in `text`, or 'latin'."""
    counts: dict[str, int] = {}
    for ch in text or "":
        cp = ord(ch)
        if ch.isspace() or not ch.isalpha():
            continue
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
        else:
            counts["latin"] = counts.get("latin", 0) + 1
    if not counts:
        return "latin"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def search_languages(name: str) -> list[str]:
    """Which Wikidata language indexes to search for this name."""
    sc = script_of(name)
    langs = list(SCRIPT_LANGS.get(sc, ("en",)))
    if "en" not in langs:
        langs.append("en")          # most items carry an English label too
    return langs


def strip_marks(text: str) -> str:
    """Beyoncé -> Beyonce. NFKD then drop combining marks."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(name: str, drop_honorifics: bool = True) -> str:
    """
    Aggressive comparison form. Not for display.

    'Dr. A.R. Rahman Jr.' -> 'a r rahman'
    """
    s = strip_marks(name or "").lower()
    s = s.replace("&", " and ")
    # split initials glued to letters: "a.r." -> "a r "
    s = re.sub(r"([a-z])\.", r"\1 ", s)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    tokens = [t for t in s.split() if t]
    if drop_honorifics:
        while tokens and tokens[0] in _HONORIFICS:
            tokens.pop(0)
        while tokens and tokens[-1] in _SUFFIXES:
            tokens.pop()
    return " ".join(tokens)


def initials_form(name: str) -> str:
    """'Allah Rakha Rahman' -> 'a r rahman' (initialise all but the last token)."""
    toks = normalize(name).split()
    if len(toks) < 2:
        return " ".join(toks)
    return " ".join([t[0] for t in toks[:-1]] + [toks[-1]])


def variants(name: str, max_variants: int = 10) -> list[str]:
    """
    Query strings to try, most faithful first.

    Kept small on purpose: each variant is a network call, and Wikidata's own
    search already absorbs most spelling slack.
    """
    out: list[str] = []

    def add(v: str) -> None:
        v = (v or "").strip()
        if v and v.lower() not in {o.lower() for o in out}:
            out.append(v)

    add(name)
    add(strip_marks(name))                        # diacritics dropped
    n = normalize(name)
    add(n)

    toks = n.split()
    if len(toks) >= 2:
        add(initials_form(name))                  # A R Rahman
        add(" ".join(toks[:1] + toks[-1:]))       # drop middle names
        # single-letter tokens collapsed: 'a r rahman' -> 'ar rahman'
        lead = "".join(t for t in toks[:-1] if len(t) == 1)
        if lead and len(lead) > 1:
            add(f"{lead} {toks[-1]}")
        # 'Last, First' -> 'First Last'
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            if len(parts) == 2 and all(parts):
                add(f"{parts[1]} {parts[0]}")

    if script_of(name) != "latin":
        t = _unidecode(name)
        # only offer transliteration if it produced something plausible; it is
        # a weak signal and a mangled string wastes a query
        if t and t != name and re.search(r"[a-zA-Z]{3}", t):
            add(normalize(t))

    return out[:max_variants]


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

@dataclass
class LabelMatch:
    score: float
    matched_label: str
    how: str            # exact | normalized | initials | token | fuzzy | translit


def _token_overlap(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_label(query: str, label: str) -> LabelMatch:
    """How well does one candidate label match the queried name?"""
    if not query or not label:
        return LabelMatch(0.0, label, "none")
    if query.strip() == label.strip():
        return LabelMatch(1.0, label, "exact")

    nq, nl = normalize(query), normalize(label)
    if nq and nq == nl:
        return LabelMatch(0.97, label, "normalized")
    if nq and initials_form(query) == initials_form(label):
        return LabelMatch(0.90, label, "initials")

    overlap = _token_overlap(nq, nl)
    if overlap == 1.0:
        return LabelMatch(0.88, label, "token")

    ratio = SequenceMatcher(None, nq, nl).ratio()
    if script_of(query) != script_of(label):
        # cross-script: compare transliterations, but cap the score because
        # Indic transliteration is too lossy to trust for a decision
        tq, tl = normalize(_unidecode(query)), normalize(_unidecode(label))
        tratio = SequenceMatcher(None, tq, tl).ratio()
        if tratio > ratio:
            return LabelMatch(round(min(tratio, 0.75), 3), label, "translit")

    blended = 0.6 * ratio + 0.4 * overlap
    return LabelMatch(round(blended, 3), label, "fuzzy")


def best_label_match(query: str, labels: Iterable[str]) -> LabelMatch:
    best = LabelMatch(0.0, "", "none")
    for lab in labels:
        m = match_label(query, lab)
        if m.score > best.score:
            best = m
    return best


# Below this, a candidate is not the same name at all and should be dropped
# before role scoring ever sees it.
LABEL_FLOOR = 0.62
