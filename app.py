"""
IdentityForge web service.

Two modes, chosen by the IF_LIVE env var:

  demo (default)  fixtures only, zero outbound requests. Safe for a public URL,
                  survives Render cold starts, and lets you show the workflow
                  without exposing keys or hammering Wikidata.
  live (IF_LIVE=1) real Wikidata / TMDB / MusicBrainz / link-in-bio fetches
                  through CachedFetcher, with rate limiting and a domain
                  allowlist.

Deliberately read-only. Nothing here writes to a client system, posts anywhere,
or takes a credential in a query string.
"""

from __future__ import annotations

import json
import os
from flask import Flask, jsonify, render_template, request

from identityforge import authorities
from identityforge.authorities import OCCUPATION_BUCKETS
from identityforge.fetcher import CachedFetcher, FixtureFetcher, build_fetcher
from identityforge.resolver import EntityStore, Intake, resolve

app = Flask(__name__)

LIVE = os.environ.get("IF_LIVE", "0") == "1"
TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
DB_PATH = os.environ.get("IF_DB_PATH", "identityforge.db")

_store = EntityStore(DB_PATH)
_live_fetcher: CachedFetcher | None = None


# ---------------------------------------------------------------------------
# demo fixtures - the Michael Jackson collision, which is a real one
# ---------------------------------------------------------------------------

_LINKTREE = """<html><script id="__NEXT_DATA__" type="application/json">
%s
</script></html>""" % json.dumps({"props": {"pageProps": {"links": [
    {"url": "https://instagram.com/michaeljackson?igshid=x"},
    {"url": "https://twitter.com/michaeljackson"},
    {"url": "https://www.youtube.com/@michaeljackson"},
    {"url": "https://www.facebook.com/michaeljackson"},
    {"url": "https://www.tiktok.com/@michaeljackson"},
    {"url": "https://ffm.to/thriller"},
]}}})

DEMO_FIXTURES: dict[str, object] = {
    "sparql": {"results": {"bindings": [
        {"p": {"value": "http://www.wikidata.org/entity/Q2831"},
         "pLabel": {"value": "Michael Jackson"},
         "pDesc": {"value": "American singer, songwriter and dancer (1958-2009)"},
         "occ": {"value": "http://www.wikidata.org/entity/Q177220"},
         "dob": {"value": "1958-08-29T00:00:00Z"},
         "dod": {"value": "2009-06-25T00:00:00Z"}},
        {"p": {"value": "http://www.wikidata.org/entity/Q6831397"},
         "pLabel": {"value": "Michael Jackson"},
         "pDesc": {"value": "English footballer"},
         "occ": {"value": "http://www.wikidata.org/entity/Q937857"},
         "dob": {"value": "1973-01-01T00:00:00Z"}},
        {"p": {"value": "http://www.wikidata.org/entity/Q1928447"},
         "pLabel": {"value": "Michael Jackson"},
         "pDesc": {"value": "British writer on beer and whisky (1942-2007)"},
         "occ": {"value": "http://www.wikidata.org/entity/Q36180"},
         "dob": {"value": "1942-03-27T00:00:00Z"},
         "dod": {"value": "2007-08-30T00:00:00Z"}},
    ]}},
    "wbgetentities": {"entities": {"Q2831": {
        "claims": {
            "P2003": [{"mainsnak": {"datavalue": {"value": "michaeljackson"}}}],
            "P2002": [{"mainsnak": {"datavalue": {"value": "michaeljackson"}}}],
            "P345":  [{"mainsnak": {"datavalue": {"value": "nm0001391"}}}],
            "P1902": [{"mainsnak": {"datavalue": {"value": "3fMbdgg4jU18AjLCKBhRSm"}}}],
            "P434":  [{"mainsnak": {"datavalue": {"value": "f27ec8db-af05-4f36-916e-3d57f91ecf5e"}}}],
            "P4985": [{"mainsnak": {"datavalue": {"value": "22226"}}}],
            "P2397": [{"mainsnak": {"datavalue": {"value": "UCoUM-UJ7rirJYP8CQ0EIaHA"}}}],
        },
        "sitelinks": {"enwiki": {"title": "Michael Jackson"},
                      "hiwiki": {"title": "\u092e\u093e\u0907\u0915\u0932 \u091c\u0948\u0915\u094d\u0938\u0928"},
                      "jawiki": {"title": "\u30de\u30a4\u30b1\u30eb\u30fb\u30b8\u30e3\u30af\u30bd\u30f3"}},
    }}},
    "musicbrainz.org/ws": {"relations": [
        {"type": "social network",
         "url": {"resource": "https://www.tiktok.com/@michaeljackson"}},
        {"type": "social network",
         "url": {"resource": "https://www.facebook.com/michaeljackson"}},
    ]},
    "instagram.com": '<html><a href="https://linktr.ee/michaeljackson">links</a></html>',
    "linktr.ee": _LINKTREE,
}


def _fetcher():
    global _live_fetcher
    if not LIVE:
        return FixtureFetcher(DEMO_FIXTURES)
    if _live_fetcher is None:
        _live_fetcher = build_fetcher()
    return _live_fetcher


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", live=LIVE,
                           roles=sorted(OCCUPATION_BUCKETS.keys()))


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "mode": "live" if LIVE else "demo",
                    "tmdb_key_present": bool(TMDB_KEY),
                    "version": "0.2.0"})


@app.get("/api/audit")
def audit():
    """P-number self-audit. Unverified properties fail SILENTLY in production,
    so this endpoint exists to make that failure loud."""
    unver = [{"property": a.wikidata_prop, "key": a.key, "notes": a.notes}
             for a in authorities.unverified_properties()]
    payload = {"unverified_count": len(unver), "unverified": unver,
               "validated": None}
    if LIVE:
        f = _fetcher()
        payload["validated"] = authorities.validate_property_map(
            lambda u: f(u, kind="json"))
    return jsonify(payload)


@app.post("/api/resolve")
def api_resolve():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a name to resolve."}), 400
    if len(name) > 120:
        return jsonify({"error": "Name is too long."}), 400

    role = (body.get("role") or "").strip() or None
    if role and role not in OCCUPATION_BUCKETS:
        return jsonify({"error": f"Unknown role: {role}"}), 400

    year = body.get("active_year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None

    intake = Intake(name=name, expected_role=role, active_year=year,
                    context=(body.get("context") or "").strip(),
                    client=(body.get("client") or "").strip())

    fetch = _fetcher()
    try:
        result = resolve(intake, fetch, _store, tmdb_key=TMDB_KEY,
                         do_bidirectional=bool(body.get("bidirectional")))
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({"error": "Resolution failed.",
                        "detail": str(exc)[:300]}), 502

    result["mode"] = "live" if LIVE else "demo"
    if isinstance(fetch, CachedFetcher):
        result["fetch_stats"] = fetch.stats.as_dict()
        fetch.reset_stats()
    else:
        result["fetch_stats"] = {"fixture_calls": len(fetch.calls)}
    return jsonify(result)


@app.post("/api/classify")
def api_classify():
    """Paste any URL, get (platform, handle). Useful on its own for cleaning
    handle columns in a spreadsheet."""
    from identityforge import classify_url
    urls = (request.get_json(silent=True) or {}).get("urls") or []
    out = []
    for u in urls[:200]:
        ref = classify_url(u)
        out.append({"input": u,
                    "platform": ref.platform.value if ref else None,
                    "handle": ref.handle if ref else None,
                    "lang": ref.lang if ref else None,
                    "id_kind": ref.id_kind if ref else None,
                    "canonical_url": ref.canonical_url if ref else None})
    return jsonify({"results": out})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8020)), debug=False)
