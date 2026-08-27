"""Offline test suite - no network. Fixtures stand in for live pages."""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from identityforge.platforms import Platform, classify_url, build_url
from identityforge.aggregators import (harvest, probe_urls, is_aggregator_url,
                                       looks_like_aggregator, extract_sameas)
from identityforge.evidence import (Candidate, Evidence, HandleClaim, Tier,
                                    rank, score_role_match)
from identityforge.platforms import PlatformRef
from identityforge.resolver import Intake, EntityStore, resolve
from identityforge import authorities

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


print("\n=== 1. URL classification / normalisation ===")
cases = [
    ("https://www.instagram.com/taylorswift/?hl=en&utm_source=x",
     (Platform.INSTAGRAM, "taylorswift")),
    ("instagram.com/@nickjonas", (Platform.INSTAGRAM, "nickjonas")),
    ("https://m.facebook.com/ARRahman", (Platform.FACEBOOK, "ARRahman")),
    ("https://www.facebook.com/profile.php?id=100044512345678&sk=about",
     (Platform.FACEBOOK, "100044512345678")),
    ("https://twitter.com/iHrithik", (Platform.TWITTER, "iHrithik")),
    ("https://x.com/i/user/1234567890", (Platform.TWITTER, "1234567890")),
    ("https://www.youtube.com/@MrBeast", (Platform.YOUTUBE, "MrBeast")),
    ("https://youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA",
     (Platform.YOUTUBE, "UCX6OQ3DkcsbYNE6H8uQQuVA")),
    ("https://www.youtube.com/c/tseries", (Platform.YOUTUBE, "tseries")),
    ("https://www.tiktok.com/@charlidamelio?lang=en",
     (Platform.TIKTOK, "charlidamelio")),
    ("https://www.linkedin.com/in/satyanadella/", (Platform.LINKEDIN, "satyanadella")),
    ("https://www.imdb.com/name/nm0000138/?ref_=nv_sr_1",
     (Platform.IMDB, "nm0000138")),
    ("https://hi.wikipedia.org/wiki/%E0%A4%B6%E0%A4%BE%E0%A4%B9%E0%A4%B0%E0%A5%81%E0%A4%96%E0%A4%BC_%E0%A4%96%E0%A4%BC%E0%A4%BE%E0%A4%A8",
     (Platform.WIKIPEDIA, "शाहरुख़ ख़ान")),
]
for url, want in cases:
    ref = classify_url(url)
    check(url[:52], (ref.platform, ref.handle) if ref else None, want)

print("\n  -- must be rejected (routes, not identities) --")
for url in ["https://www.instagram.com/p/Cabc123/",
            "https://twitter.com/i/status/1",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.tiktok.com/tag/bollywood",
            "https://www.linkedin.com/feed/",
            "https://www.facebook.com/groups/12345",
            "https://en.wikipedia.org/wiki/Category:Actors",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.imdb.com/title/tt0111161/"]:
    check(url[:52], classify_url(url), None)

print("\n  -- lang + canonical url --")
w = classify_url("https://ta.wikipedia.org/wiki/A._R._Rahman")
check("wikipedia lang", w.lang, "ta")
check("wikipedia canonical", w.canonical_url,
      "https://ta.wikipedia.org/wiki/A._R._Rahman")
check("twitter canonicalises to x.com",
      classify_url("https://twitter.com/foo").canonical_url, "https://x.com/foo")
check("build_url roundtrip",
      classify_url(build_url(Platform.TIKTOK, "@abc")).handle, "abc")
check("linkedin company flagged not person",
      classify_url("https://www.linkedin.com/company/netflix/").id_kind, "company")


print("\n=== 2. Aggregator harvesting ===")
LINKTREE = """
<html><head><title>@arrahman | Linktree</title></head><body>
<script id="__NEXT_DATA__" type="application/json">
%s
</script>
<a href="https://open.spotify.com/artist/1mYsTxnqsietFxj1OgoGbG">Spotify</a>
</body></html>
""" % json.dumps({
    "props": {"pageProps": {"account": {"username": "arrahman"}, "links": [
        {"title": "Instagram", "url": "https://instagram.com/arrahman?igshid=xx"},
        {"title": "X", "url": "https://twitter.com/arrahman"},
        {"title": "YouTube", "url": "https://www.youtube.com/@ARRahmanOfficial"},
        {"title": "Facebook", "url": "https://www.facebook.com/ARRahman"},
        {"title": "TikTok", "url": "https://www.tiktok.com/@arrahman"},
        {"title": "New single", "url": "https://ffm.to/newsingle"},
        {"title": "Store", "url": "https://shop.example.com"},
    ]}}})

refs = harvest(LINKTREE)
found = {(r.platform, r.handle) for r in refs}
check("linktree platform count", len({p for p, _ in found}), 5)
check("instagram tracking stripped", (Platform.INSTAGRAM, "arrahman") in found, True)
check("youtube handle harvested",
      (Platform.YOUTUBE, "ARRahmanOfficial") in found, True)
check("smartlink not misread as identity",
      any("ffm.to" in (r.raw_url or "") for r in refs), False)

check("linktr.ee recognised", is_aggregator_url("https://linktr.ee/x").name, "Linktree")
check("subdomain carrd recognised",
      is_aggregator_url("https://someone.carrd.co").name, "Carrd")
check("random domain not aggregator", is_aggregator_url("https://bbc.co.uk"), None)
check("probe set size", len(probe_urls("arrahman")), len(__import__("identityforge.aggregators", fromlist=["AGGREGATORS"]).AGGREGATORS))
check("probe rejects junk handle", probe_urls("a b/c"), [])
check("generic aggregator heuristic", looks_like_aggregator(LINKTREE), True)

SAMEAS = """<script type="application/ld+json">
{"@type":"Person","name":"A. R. Rahman","sameAs":[
"https://twitter.com/arrahman","https://www.imdb.com/name/nm0006249/"]}
</script>"""
check("sameAs extracted", len(extract_sameas(SAMEAS)), 2)


print("\n=== 3. Confidence model ===")
ig = PlatformRef(Platform.INSTAGRAM, "arrahman")
c1 = HandleClaim(ig).add(Evidence(Tier.NAME_MATCH, "name"))
check("name match alone -> reject", c1.verdict(), "reject")

c2 = HandleClaim(ig).add(Evidence(Tier.SELF_DECLARED, "linktr.ee/arrahman"))
check("self-declared alone -> review", c2.verdict(), "review")

c3 = (HandleClaim(ig)
      .add(Evidence(Tier.STRUCTURED_ID, "wikidata:P2003"))
      .add(Evidence(Tier.SELF_DECLARED, "linktr.ee/arrahman")))
check("structured + self-declared -> accept", c3.verdict(), "accept")

c4 = (HandleClaim(ig)
      .add(Evidence(Tier.BIDIRECTIONAL, "backlink"))
      .add(Evidence(Tier.STRUCTURED_ID, "wikidata:P2003"))
      .add(Evidence(Tier.SELF_DECLARED, "linktr.ee")))
check("three tiers -> high confidence", c4.confidence() > 0.85, True)
check("monotonic in evidence", c4.confidence() > c3.confidence() > c2.confidence(), True)


print("\n=== 4. Role disambiguation (the actual problem) ===")
# 'Michael Jackson' - singer / footballer / writer, all real collisions
singer = Candidate("Q2831", "Michael Jackson", "American singer",
                   occupations=["Q177220"], roles=["musician"], birth_year=1958,
                   death_year=2009, external_ids={"spotify_artist": "3fMbdgg4jU18AjLCKBhRSm"})
footballer = Candidate("Q6831397", "Michael Jackson", "English footballer",
                       occupations=["Q937857"], roles=["athlete"], birth_year=1973)
writer = Candidate("Q1928447", "Michael Jackson", "British beer writer",
                   occupations=["Q36180"], roles=["writer"], birth_year=1942,
                   death_year=2007)
pool = [singer, footballer, writer]

for c in pool:
    score_role_match(c, "musician", expected_active_year=2005)
r = rank(pool)
check("musician hint resolves", r["decision"], "resolved")
check("correct winner", r["winner"].entity_id, "Q2831")

for c in pool:
    score_role_match(c, "athlete", expected_active_year=2000)
r = rank(pool)
check("athlete hint resolves", r["decision"], "resolved")
check("correct winner", r["winner"].entity_id, "Q6831397")

for c in pool:
    score_role_match(c, None)
r = rank(pool)
check("no hint -> disambiguate not guess", r["decision"], "disambiguate")

for c in pool:
    score_role_match(c, "politician")
r = rank(pool)
check("wrong-vertical hint -> disambiguate", r["decision"], "disambiguate")

score_role_match(singer, "musician", expected_active_year=2020)   # after death
check("dead-at-time penalised", singer.role_score < 0.5, True)


print("\n=== 5. End-to-end with a fake network ===")
FIXTURES = {
    "sparql": {"results": {"bindings": [
        {"p": {"value": "http://www.wikidata.org/entity/Q2831"},
         "pLabel": {"value": "Michael Jackson"},
         "pDesc": {"value": "American singer (1958-2009)"},
         "occ": {"value": "http://www.wikidata.org/entity/Q177220"},
         "dob": {"value": "1958-08-29T00:00:00Z"},
         "dod": {"value": "2009-06-25T00:00:00Z"}},
        {"p": {"value": "http://www.wikidata.org/entity/Q6831397"},
         "pLabel": {"value": "Michael Jackson"},
         "pDesc": {"value": "English footballer"},
         "occ": {"value": "http://www.wikidata.org/entity/Q937857"},
         "dob": {"value": "1973-01-01T00:00:00Z"}},
    ]}},
    "entity": {"entities": {"Q2831": {
        "claims": {
            "P2003": [{"mainsnak": {"datavalue": {"value": "michaeljackson"}}}],
            "P2002": [{"mainsnak": {"datavalue": {"value": "michaeljackson"}}}],
            "P345":  [{"mainsnak": {"datavalue": {"value": "nm0001391"}}}],
            "P1902": [{"mainsnak": {"datavalue": {"value": "3fMbdgg4jU18AjLCKBhRSm"}}}],
            "P434":  [{"mainsnak": {"datavalue": {"value": "f27ec8db-af05-4f36-916e-3d57f91ecf5e"}}}],
            "P4985": [{"mainsnak": {"datavalue": {"value": "22226"}}}],
        },
        "sitelinks": {"enwiki": {"title": "Michael Jackson"},
                      "hiwiki": {"title": "माइकल जैक्सन"}},
    }}},
    "ig_profile": '<html><body><a href="https://linktr.ee/michaeljackson">bio</a></body></html>',
    "linktree": LINKTREE.replace("arrahman", "michaeljackson")
                        .replace("ARRahman", "michaeljackson")
                        .replace("ARRahmanOfficial", "michaeljackson"),
    "mb": {"relations": [
        {"type": "social network",
         "url": {"resource": "https://www.tiktok.com/@michaeljackson"}}]},
}

calls = []


def fake_fetch(url, kind="text"):
    calls.append(url)
    if "sparql" in url:
        return FIXTURES["sparql"]
    if "wbgetentities" in url:
        return FIXTURES["entity"]
    if "musicbrainz.org/ws" in url:
        return FIXTURES["mb"]
    if "instagram.com" in url:
        return FIXTURES["ig_profile"]
    if "linktr.ee" in url:
        return FIXTURES["linktree"]
    return "" if kind == "text" else {}


store = EntityStore(":memory:")
res = resolve(Intake("Michael Jackson", expected_role="musician",
                     active_year=2005, context="Sony Music catalogue"),
              fake_fetch, store, do_bidirectional=False)

check("e2e resolved", res["decision"], "resolved")
check("e2e right person", res["entity_id"], "Q2831")
check("spotify id harvested via hub",
      res["external_ids"].get("spotify_artist"), "3fMbdgg4jU18AjLCKBhRSm")
check("musicbrainz id harvested",
      "musicbrainz_artist" in res["external_ids"], True)
got = set(res["handles"].keys())
print(f"       platforms accepted: {sorted(got)}")
check("instagram accepted", "instagram" in got, True)
check("imdb accepted", "imdb" in got, True)
check("wikipedia multi-lang", len(res["handles"]["wikipedia"]), 2)
check("tiktok via musicbrainz+linktree", "tiktok" in got, True)
# Facebook + YouTube came ONLY from the Linktree -> self-declared, single
# source -> must land in review, NOT auto-accept. Coverage is 5/8 and that is
# the correct answer, not a shortfall.
check("coverage is 5/8", res["coverage"], 0.625)
review = {r["platform"] for r in res["needs_review"]}
check("aggregator-only facebook -> review", "facebook" in review, True)
check("aggregator-only youtube -> review", "youtube" in review, True)
check("linkedin genuinely absent, not invented",
      "linkedin" in res["handles"] or "linkedin" in review, False)
check("collision was recorded",
      store.known_collision("michael jackson")["candidate_count"], 2)
check("reverse lookup by handle",
      store.lookup_by_handle(Platform.INSTAGRAM, "MichaelJackson"), "Q2831")

print("\n  -- ambiguous intake must NOT auto-guess --")
res2 = resolve(Intake("Michael Jackson"), fake_fetch, EntityStore(":memory:"))
check("no hint -> disambiguate", res2["decision"], "disambiguate")
check("cards rendered for human", len(res2["cards"]), 2)
check("cards carry occupation", res2["cards"][0]["roles"] != [], True)


print("\n=== 6. Property-map self-audit ===")
unver = authorities.unverified_properties()
print(f"       {len(unver)} P-numbers flagged for runtime validation:")
for a in unver:
    print(f"         {a.wikidata_prop:<7} {a.key}")
check("audit list non-empty (honest about uncertainty)", len(unver) > 0, True)

print(f"\n{'='*54}\n  {PASS} passed, {FAIL} failed\n{'='*54}")
sys.exit(1 if FAIL else 0)
