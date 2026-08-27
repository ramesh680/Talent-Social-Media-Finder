"""
The ID hub.

Your Spotify observation, generalised: nearly every platform mints its own
stable person-id, and Wikidata already stores most of them as properties on a
single Q-item. So we do NOT integrate N APIs pairwise. We:

    resolve name -> Q-item  (once, with occupation filtering)
    read every external id off that Q-item  (one call)
    call individual APIs only to fill gaps

AUTHORITY = a source that mints stable ids and is *disambiguated by design*
            (one record per human, never per name string).

`verified` marks whether the Wikidata P-number is one I am confident about.
Anything False must be confirmed by validate_property_map() before trusting -
P-numbers do get created/merged, and a wrong one silently returns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .platforms import Platform


@dataclass(frozen=True)
class Authority:
    key: str                      # our internal name
    label: str
    wikidata_prop: Optional[str]  # P-number holding this id on a person item
    url_template: Optional[str]   # {id} substitution
    domain: str                   # which vertical this authority is strong for
    is_target_platform: bool = False
    platform: Optional[Platform] = None
    verified: bool = True         # confidence in the P-number above
    self_sufficient: bool = False
    """One structured hit here is enough to auto-accept - the id is a
    structural identity link, not a mutable user-supplied handle."""
    notes: str = ""


# ---------------------------------------------------------------------------
# 1. THE 8 TARGET PLATFORMS - what we actually want to output
# ---------------------------------------------------------------------------

TARGET_AUTHORITIES: list[Authority] = [
    Authority("instagram", "Instagram username", "P2003",
              "https://www.instagram.com/{id}/", "social",
              True, Platform.INSTAGRAM),
    Authority("twitter", "X (Twitter) username", "P2002",
              "https://x.com/{id}", "social", True, Platform.TWITTER),
    Authority("twitter_numeric", "X numeric user id", "P6552",
              "https://x.com/i/user/{id}", "social", True, Platform.TWITTER,
              verified=False, notes="immune to handle changes - store when available"),
    Authority("facebook", "Facebook id", "P2013",
              "https://www.facebook.com/{id}", "social", True, Platform.FACEBOOK),
    Authority("facebook_numeric", "Facebook numeric id", "P4003",
              "https://www.facebook.com/profile.php?id={id}", "social",
              True, Platform.FACEBOOK, verified=False),
    Authority("youtube_channel", "YouTube channel id", "P2397",
              "https://www.youtube.com/channel/{id}", "social",
              True, Platform.YOUTUBE,
              notes="UC... form is permanent; the @handle is not"),
    Authority("youtube_handle", "YouTube handle", "P11245",
              "https://www.youtube.com/@{id}", "social",
              True, Platform.YOUTUBE, verified=False,
              notes="newer property, confirm before relying on it"),
    Authority("tiktok", "TikTok username", "P7085",
              "https://www.tiktok.com/@{id}", "social", True, Platform.TIKTOK),
    Authority("linkedin", "LinkedIn personal profile id", "P6634",
              "https://www.linkedin.com/in/{id}/", "professional",
              True, Platform.LINKEDIN, verified=False,
              notes="sparsely populated for entertainers; dense for execs/creators"),
    Authority("imdb", "IMDb id", "P345",
              "https://www.imdb.com/name/{id}/", "film",
              True, Platform.IMDB, self_sufficient=True,
              notes="nm-prefixed; curated, stable, one record per human"),
    # Wikipedia is not a property - it is the sitelink set on the Q-item.
    Authority("wikipedia", "Wikipedia article", None,
              "https://{lang}.wikipedia.org/wiki/{id}", "reference",
              True, Platform.WIKIPEDIA, self_sufficient=True,
              notes="read from Q-item sitelinks, not from a claim"),
]


# ---------------------------------------------------------------------------
# 2. SPOKE AUTHORITIES - not outputs, but powerful *anchors* and disambiguators
#    These are what let us pick the right person out of 6 same-named ones.
# ---------------------------------------------------------------------------

SPOKE_AUTHORITIES: list[Authority] = [
    # --- music (your Spotify case) -----------------------------------------
    Authority("spotify_artist", "Spotify artist id", "P1902",
              "https://open.spotify.com/artist/{id}", "music"),
    Authority("musicbrainz_artist", "MusicBrainz artist id", "P434",
              "https://musicbrainz.org/artist/{id}", "music",
              notes="BEST music anchor: its API returns social urls as relations"),
    Authority("apple_music_artist", "Apple Music artist id", "P2850",
              "https://music.apple.com/artist/{id}", "music"),
    Authority("deezer_artist", "Deezer artist id", "P2722",
              "https://www.deezer.com/artist/{id}", "music"),
    Authority("genius_artist", "Genius artist id", "P2373",
              "https://genius.com/artists/{id}", "music"),
    Authority("soundcloud", "SoundCloud id", "P3040",
              "https://soundcloud.com/{id}", "music"),
    Authority("discogs_artist", "Discogs artist id", "P1953",
              "https://www.discogs.com/artist/{id}", "music"),
    Authority("lastfm", "Last.fm id", "P3192",
              "https://www.last.fm/music/{id}", "music"),

    # --- film / tv ---------------------------------------------------------
    Authority("tmdb_person", "TMDB person id", "P4985",
              "https://www.themoviedb.org/person/{id}", "film",
              notes="/person/{id}/external_ids returns imdb+ig+x+tiktok+yt+fb "
                    "in ONE call - highest ROI integration you can add"),
    Authority("rotten_tomatoes", "Rotten Tomatoes id", "P1258",
              "https://www.rottentomatoes.com/{id}", "film"),
    Authority("letterboxd", "Letterboxd id", "P6127",
              "https://letterboxd.com/{id}/", "film", verified=False),

    # --- gaming / streaming ----------------------------------------------
    Authority("twitch", "Twitch channel", "P5797",
              "https://www.twitch.tv/{id}", "gaming"),

    # --- sport ------------------------------------------------------------
    Authority("transfermarkt_player", "Transfermarkt player id", "P2446",
              "https://www.transfermarkt.com/-/profil/spieler/{id}", "sport"),
    Authority("espn_player", "ESPN player id", "P8286",
              None, "sport", verified=False,
              notes="P-number uncertain - validate before use"),
    Authority("olympedia", "Olympedia athlete id", "P8286",
              None, "sport", verified=False,
              notes="placeholder - confirm; sports coverage is the weakest vertical"),

    # --- literature / business / academia ---------------------------------
    Authority("goodreads_author", "Goodreads author id", "P2963",
              "https://www.goodreads.com/author/show/{id}", "books",
              verified=False),
    Authority("crunchbase_person", "Crunchbase person id", "P2087",
              "https://www.crunchbase.com/person/{id}", "business",
              verified=False),
    Authority("orcid", "ORCID id", "P496",
              "https://orcid.org/{id}", "academia"),
]


ALL_AUTHORITIES = TARGET_AUTHORITIES + SPOKE_AUTHORITIES
BY_KEY = {a.key: a for a in ALL_AUTHORITIES}


# ---------------------------------------------------------------------------
# 3. DISAMBIGUATION PROPERTIES - the actual answer to "director vs singer"
# ---------------------------------------------------------------------------

DISAMBIGUATION_PROPS = {
    "P31":  "instance_of",          # must be Q5 (human)
    "P106": "occupation",           # THE discriminator
    "P569": "date_of_birth",
    "P570": "date_of_death",
    "P27":  "citizenship",
    "P21":  "gender",
    "P800": "notable_work",
    "P937": "work_location",
    "P641": "sport",
    "P413": "position_played",
    "P1303": "instrument",
    "P264": "record_label",
}

# occupation Q-ids we care about, grouped into the role buckets your ingest
# requests actually use. Wikidata occupations are hierarchical, so in
# production also walk P279 (subclass of) upward before giving up.
OCCUPATION_BUCKETS: dict[str, set[str]] = {
    "actor":      {"Q33999", "Q10800557", "Q10798782", "Q2405480"},   # actor, film/tv/voice actor
    "director":   {"Q2526255", "Q3455803", "Q1053574"},               # film dir, director, producer
    "musician":   {"Q639669", "Q177220", "Q36834", "Q753110", "Q183945"},  # musician, singer, composer, songwriter, record producer
    "athlete":    {"Q2066131", "Q937857", "Q3665646", "Q10871364"},   # athlete, footballer, basketball, cricketer
    "creator":    {"Q17125263", "Q2722764", "Q88802976"},             # youtuber, streamer, content creator
    "model":      {"Q4610556", "Q3286043"},
    "journalist": {"Q1930187", "Q1607826"},
    "politician": {"Q82955"},
    "writer":     {"Q36180", "Q6625963", "Q49757"},
    "executive":  {"Q484876", "Q43845"},                              # CEO, businessperson
    "comedian":   {"Q245068"},
    "chef":       {"Q3499072", "Q156839"},
}

# which authority is the strongest anchor for each role bucket -> query order
ANCHOR_PRIORITY: dict[str, list[str]] = {
    "musician":   ["musicbrainz_artist", "spotify_artist", "wikipedia", "imdb"],
    "actor":      ["tmdb_person", "imdb", "wikipedia"],
    "director":   ["tmdb_person", "imdb", "wikipedia"],
    "creator":    ["youtube_channel", "twitch", "wikipedia"],
    "athlete":    ["transfermarkt_player", "wikipedia", "imdb"],
    "executive":  ["linkedin", "crunchbase_person", "wikipedia"],
    "journalist": ["wikipedia", "linkedin", "twitter"],
    "writer":     ["goodreads_author", "wikipedia", "imdb"],
    "_default":   ["wikipedia", "imdb", "tmdb_person"],
}


def anchors_for_role(role: Optional[str]) -> list[str]:
    return ANCHOR_PRIORITY.get(role or "_default", ANCHOR_PRIORITY["_default"])


def occupations_for_role(role: str) -> set[str]:
    return OCCUPATION_BUCKETS.get(role, set())


def role_for_occupation(qid: str) -> Optional[str]:
    for role, qids in OCCUPATION_BUCKETS.items():
        if qid in qids:
            return role
    return None


def unverified_properties() -> list[Authority]:
    """Run this at startup and log the result. Do not skip it."""
    return [a for a in ALL_AUTHORITIES if a.wikidata_prop and not a.verified]


def validate_property_map(fetch_json) -> dict[str, str]:
    """
    Confirm each P-number resolves to the label we expect.

    `fetch_json(url) -> dict` is injected so this module stays offline-testable
    and so you control rate limiting / caching / user-agent centrally.
    """
    results: dict[str, str] = {}
    props = sorted({a.wikidata_prop for a in ALL_AUTHORITIES if a.wikidata_prop})
    for i in range(0, len(props), 40):
        chunk = "|".join(props[i:i + 40])
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities"
               f"&ids={chunk}&props=labels&languages=en&format=json")
        try:
            data = fetch_json(url)
        except Exception as exc:                      # noqa: BLE001
            for p in props[i:i + 40]:
                results[p] = f"ERROR: {exc}"
            continue
        for p in props[i:i + 40]:
            ent = (data.get("entities") or {}).get(p) or {}
            if ent.get("missing") is not None or "labels" not in ent:
                results[p] = "MISSING"
            else:
                results[p] = ent["labels"].get("en", {}).get("value", "?")
    return results
