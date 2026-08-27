"""Tests for label matching and the Wikidata recall/precision lookup. No network."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from identityforge.labels import (LABEL_FLOOR, best_label_match, initials_form,
                                  match_label, normalize, script_of,
                                  search_languages, strip_marks, variants)
from identityforge.wikidata import find_candidates
from identityforge.resolver import Intake, discover_candidates

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


print("\n=== 1. Normalisation ===")
check("initials spaced", normalize("A. R. Rahman"), "a r rahman")
check("initials unspaced", normalize("A.R. Rahman"), "a r rahman")
check("no dots", normalize("AR Rahman"), "ar rahman")
check("diacritics dropped", normalize("Beyoncé Knowles"), "beyonce knowles")
check("honorific dropped", normalize("Dr. A. R. Rahman"), "a r rahman")
check("suffix dropped", normalize("Robert Downey Jr."), "robert downey")
check("ampersand expanded", normalize("Simon & Garfunkel"), "simon and garfunkel")
check("whitespace collapsed", normalize("  Shah   Rukh  Khan "), "shah rukh khan")
check("strip_marks keeps case", strip_marks("Ólafur"), "Olafur")
check("initials form", initials_form("Allah Rakha Rahman"), "a r rahman")

print("\n  -- the exact-match failure this fixes --")
check("A.R. == A. R. after normalising",
      normalize("A.R. Rahman") == normalize("A. R. Rahman"), True)

print("\n=== 2. Script detection and search languages ===")
check("latin", script_of("A. R. Rahman"), "latin")
check("devanagari", script_of("शाहरुख़ ख़ान"), "devanagari")
check("tamil", script_of("ஏ. ஆர். ரகுமான்"), "tamil")
check("katakana", script_of("マイケル・ジャクソン"), "katakana")
check("cyrillic", script_of("Алла Пугачёва"), "cyrillic")
check("hindi name searches hi then en", search_languages("शाहरुख़ ख़ान")[:1], ["hi"])
check("english always included", "en" in search_languages("शाहरुख़ ख़ान"), True)
check("latin name searches en", search_languages("Rahman"), ["en"])

print("\n=== 3. Variant generation ===")
v = variants("A.R. Rahman")
check("original kept first", v[0], "A.R. Rahman")
check("normalised variant present", "a r rahman" in v, True)
check("bounded count", len(v) <= 10, True)
v2 = variants("Dr. Allah Rakha Rahman")
check("initials variant offered", "a r rahman" in v2, True)
check("middle names dropped variant", "allah rahman" in v2, True)
v3 = variants("Knowles, Beyoncé")
check("last-comma-first flipped", any("beyonc" in x.lower() and
      x.lower().startswith("beyonc") for x in v3), True)
check("no duplicates", len(v) == len(set(x.lower() for x in v)), True)

print("\n=== 4. Label match scoring ===")
check("exact -> 1.0", match_label("A. R. Rahman", "A. R. Rahman").score, 1.0)
m = match_label("A.R. Rahman", "A. R. Rahman")
check("punctuation variant -> normalized", m.how, "normalized")
check("normalized scores high", m.score > 0.9, True)
m2 = match_label("Allah Rakha Rahman", "A. R. Rahman")
check("initials match detected", m2.how, "initials")
m3 = match_label("Michael Jackson", "Michael Jackson")
check("identical", m3.score, 1.0)
m4 = match_label("Michael Jackson", "Janet Jackson")
check("different person scores low", m4.score < LABEL_FLOOR, True)
m5 = match_label("A. R. Rahman", "Christopher Nolan")
check("unrelated scores very low", m5.score < 0.4, True)

print("\n  -- cross-script is capped, because transliteration is lossy --")
m6 = match_label("शाहरुख़ ख़ान", "Shah Rukh Khan")
check("cross-script never exceeds 0.75", m6.score <= 0.75, True)
check("best_label_match picks the strongest alias",
      best_label_match("A.R. Rahman",
                       ["Christopher Nolan", "A. R. Rahman", "Rahman"]).how,
      "normalized")

print("\n=== 5. Wikidata recall + precision ===")
SEARCH = {"search": [
    {"id": "Q193338"},      # A. R. Rahman
    {"id": "Q47703"},       # a film that shares the name
    {"id": "Q999999"},      # different person, same-ish name
]}
ENTITIES = {"entities": {
    "Q193338": {
        "labels": {"en": {"value": "A. R. Rahman"},
                   "ta": {"value": "\u0b8f. \u0b86\u0bb0\u0bcd. \u0bb0\u0b95\u0bc1\u0bae\u0bbe\u0ba9\u0bcd"}},
        "aliases": {"en": [{"value": "Allah Rakha Rahman"},
                           {"value": "AR Rahman"}]},
        "descriptions": {"en": {"value": "Indian composer and singer"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q36834"}}}}],
            "P569": [{"mainsnak": {"datavalue": {"value": {"time": "+1967-01-06T00:00:00Z"}}}}],
            "P27": [{"mainsnak": {"datavalue": {"value": {"id": "Q668"}}}}],
        }},
    "Q47703": {                      # NOT a human - must be dropped
        "labels": {"en": {"value": "Rahman"}},
        "descriptions": {"en": {"value": "2016 film"}},
        "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q11424"}}}}]}},
    "Q999999": {                     # human, but a different name
        "labels": {"en": {"value": "Christopher Nolan"}},
        "descriptions": {"en": {"value": "British-American film director"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q2526255"}}}}],
        }},
}}


def wd_fetch(url, kind="text"):
    if "wbsearchentities" in url:
        return SEARCH
    if "wbgetentities" in url:
        return ENTITIES
    return {} if kind == "json" else ""


res = find_candidates("A.R. Rahman", wd_fetch, expected_role="musician")
ids = {c.entity_id for c in res["candidates"]}
check("right person found via punctuation variant", "Q193338" in ids, True)
check("non-human film dropped", "Q47703" in ids, False)
check("wrong-name human dropped", "Q999999" in ids, False)
check("exactly one survivor", len(res["candidates"]), 1)
why = {d["why"] for d in res["dropped"]}
check("drop reasons recorded", why, {"not a human (P31)", "label mismatch"})

c = res["candidates"][0]
check("label match provenance attached", c.label_match["how"], "normalized")
check("birth year parsed", c.birth_year, 1967)
check("role derived from occupation", c.roles, ["musician"])
check("citizenship captured", c.citizenship, ["Q668"])
check("queries bounded", res["queries_run"] <= 8, True)

print("\n  -- alias path: the full birth name finds the same person --")
res2 = find_candidates("Allah Rakha Rahman", wd_fetch, expected_role="musician")
check("alias resolves to same entity",
      [c.entity_id for c in res2["candidates"]], ["Q193338"])
# The alias IS one of the entity's labels, so this matches exactly - a
# stronger result than an initials match, and the right one.
check("alias matched exactly",
      res2["candidates"][0].label_match["how"], "exact")
check("alias match is full confidence",
      res2["candidates"][0].label_match["score"], 1.0)

print("\n  -- empty and no-hit paths --")
check("blank name", find_candidates("", wd_fetch)["candidates"], [])


def empty_fetch(url, kind="text"):
    return {"search": []} if "wbsearchentities" in url else {}


check("no search hits", find_candidates("Zzz Nobody", empty_fetch)["candidates"], [])
check("no hits counted", find_candidates("Zzz Nobody", empty_fetch)["hit_ids"], 0)

print("\n=== 6. Resolver uses the new path ===")
cands = discover_candidates(Intake("A.R. Rahman", expected_role="musician"), wd_fetch)
check("discover_candidates wired through",
      [c.entity_id for c in cands], ["Q193338"])
check("old sparql fn still available for reference",
      hasattr(sys.modules["identityforge.resolver"], "discover_candidates_sparql"), True)

print(f"\n{'='*54}\n  {PASS} passed, {FAIL} failed\n{'='*54}")
sys.exit(1 if FAIL else 0)
