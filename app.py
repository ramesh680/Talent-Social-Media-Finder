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

from flask import Response, send_file

from identityforge import authorities
from identityforge.authorities import OCCUPATION_BUCKETS
from identityforge import bulk as bulk_mod
from identityforge.jobs import JobRunner, JobStore
from identityforge.template import build_template_csv, build_template_xlsx
from identityforge.discovery import discover
from identityforge.fetcher import CachedFetcher, FixtureFetcher, build_fetcher
from identityforge.providers import omdb_title, search_person
from identityforge.resolver import EntityStore, Intake, resolve

app = Flask(__name__)

LIVE = os.environ.get("IF_LIVE", "0") == "1"
DB_PATH = os.environ.get("IF_DB_PATH", "identityforge.db")

# Credentials come from the environment only. Never hardcode, never log, never
# return in a response body - /api/config reports presence, not values.
TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_TOKEN = os.environ.get("TMDB_READ_ACCESS_TOKEN", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_API_KEY", "")
SERPAPI_ENGINE = os.environ.get("SERPAPI_ENGINE", "google")
WIKIMEDIA_CONTACT = os.environ.get("WIKIMEDIA_CONTACT", "")


def _present(v: str) -> bool:
    return bool(v and v.strip())

_store = EntityStore(DB_PATH)
_jobs = JobStore(os.environ.get("IF_JOBS_PATH", "/tmp/jobs.db"))
_runner = JobRunner(_jobs, max_concurrent=int(os.environ.get("IF_JOB_WORKERS", 1)))
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
    # NOTE: order matters - FixtureFetcher returns the first needle found in
    # the url, and "wbsearchentities" must be tested before "wbgetentities".
    "wbsearchentities": {"search": [{"id": "Q2831"}, {"id": "Q6831397"},
                                    {"id": "Q1928447"}]},
    "wbgetentities": {"entities": {
        "Q2831": {
            "labels": {"en": {"value": "Michael Jackson"}},
            "aliases": {"en": [{"value": "MJ"}, {"value": "King of Pop"}]},
            "descriptions": {"en": {"value": "American singer, songwriter and "
                                             "dancer (1958-2009)"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q177220"}}}}],
                "P569": [{"mainsnak": {"datavalue": {"value": {"time": "+1958-08-29T00:00:00Z"}}}}],
                "P570": [{"mainsnak": {"datavalue": {"value": {"time": "+2009-06-25T00:00:00Z"}}}}],
                "P2003": [{"mainsnak": {"datavalue": {"value": "michaeljackson"}}}],
                "P2002": [{"mainsnak": {"datavalue": {"value": "michaeljackson"}}}],
                "P345":  [{"mainsnak": {"datavalue": {"value": "nm0001391"}}}],
                "P1902": [{"mainsnak": {"datavalue": {"value": "3fMbdgg4jU18AjLCKBhRSm"}}}],
                "P434":  [{"mainsnak": {"datavalue": {"value": "f27ec8db-af05-4f36-916e-3d57f91ecf5e"}}}],
                "P4985": [{"mainsnak": {"datavalue": {"value": "22226"}}}],
                "P2397": [{"mainsnak": {"datavalue": {"value": "UCoUM-UJ7rirJYP8CQ0EIaHA"}}}],
            },
            "sitelinks": {
                "enwiki": {"title": "Michael Jackson"},
                "hiwiki": {"title": "\u092e\u093e\u0907\u0915\u0932 \u091c\u0948\u0915\u094d\u0938\u0928"},
                "jawiki": {"title": "\u30de\u30a4\u30b1\u30eb\u30fb\u30b8\u30e3\u30af\u30bd\u30f3"},
            }},
        "Q6831397": {
            "labels": {"en": {"value": "Michael Jackson"}},
            "descriptions": {"en": {"value": "English footballer"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q937857"}}}}],
                "P569": [{"mainsnak": {"datavalue": {"value": {"time": "+1973-01-01T00:00:00Z"}}}}],
            }},
        "Q1928447": {
            "labels": {"en": {"value": "Michael Jackson"}},
            "descriptions": {"en": {"value": "British writer on beer and "
                                             "whisky (1942-2007)"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q36180"}}}}],
                "P569": [{"mainsnak": {"datavalue": {"value": {"time": "+1942-03-27T00:00:00Z"}}}}],
                "P570": [{"mainsnak": {"datavalue": {"value": {"time": "+2007-08-30T00:00:00Z"}}}}],
            }},
    }},
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
                    "tmdb_key_present": _present(TMDB_KEY),
                    "version": "0.5.0"})


@app.get("/api/config")
def config():
    """
    Which providers are wired up. Reports PRESENCE ONLY - no key material, not
    even a prefix, since a public endpoint that leaks four characters of a key
    still narrows a brute force.
    """
    return jsonify({
        "mode": "live" if LIVE else "demo",
        "providers": {
            "wikidata":    {"configured": True,
                            "contact_set": _present(WIKIMEDIA_CONTACT)},
            "musicbrainz": {"configured": True},
            "tmdb":        {"configured": _present(TMDB_KEY),
                            "v4_token": _present(TMDB_TOKEN),
                            "role": "actors and directors; external_ids in one call"},
            "omdb":        {"configured": _present(OMDB_KEY),
                            "role": "TITLE lookups only - no person endpoint, "
                                    "contributes no handles"},
            "serpapi":     {"configured": _present(SERPAPI_KEY),
                            "engine": SERPAPI_ENGINE,
                            "role": "DISCOVERY only - proposals are Tier 5 and "
                                    "never auto-accepted"},
        },
        "request_timeout_seconds": os.environ.get("REQUEST_TIMEOUT_SECONDS"),
        "cache_ttl_seconds": os.environ.get("CACHE_TTL_SECONDS"),
    })


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
                         do_bidirectional=bool(body.get("bidirectional")),
                         serpapi_key=SERPAPI_KEY,
                         serpapi_engine=SERPAPI_ENGINE,
                         allow_discovery=bool(body.get("allow_discovery", True)))
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


@app.post("/api/discover")
def api_discover():
    """
    Search-based handle discovery. Everything returned is UNVERIFIED by
    construction - the response says so explicitly so a caller cannot mistake
    a proposal for a resolution.
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a name."}), 400
    if not LIVE:
        return jsonify({"error": "Discovery needs live mode.",
                        "detail": "Set IF_LIVE=1 to enable outbound search."}), 409
    if not _present(SERPAPI_KEY):
        return jsonify({"error": "SERPAPI_API_KEY is not configured."}), 503

    res = discover(name, _fetcher(), SERPAPI_KEY,
                   role=(body.get("role") or "").strip(),
                   engine=SERPAPI_ENGINE)
    return jsonify({"name": name, "verified": False,
                    "warning": "Search proposals only. Confirm before use.",
                    "proposals": [p.as_dict() for p in res.proposals],
                    "by_platform": res.by_platform(),
                    "aggregator_urls": res.aggregator_urls,
                    "queries_run": res.queries_run,
                    "errors": res.errors})


@app.post("/api/tmdb-search")
def api_tmdb_search():
    """TMDB person search that refuses to guess on a collision."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Enter a name."}), 400
    if not _present(TMDB_KEY):
        return jsonify({"error": "TMDB_API_KEY is not configured."}), 503
    return jsonify(search_person(name, _fetcher(), TMDB_KEY,
                                 (body.get("role") or "").strip() or None))


@app.post("/api/omdb-title")
def api_omdb_title():
    """Title context for disambiguation cards. Contributes no handles."""
    body = request.get_json(silent=True) or {}
    q = (body.get("title") or body.get("imdb_id") or "").strip()
    if not q:
        return jsonify({"error": "Provide a title or imdb_id."}), 400
    if not _present(OMDB_KEY):
        return jsonify({"error": "OMDB_API_KEY is not configured."}), 503
    return jsonify(omdb_title(q, _fetcher(), OMDB_KEY) or
                   {"error": "Not found in OMDb."})


# ---------------------------------------------------------------------------
# bulk upload
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


@app.get("/api/template.xlsx")
def template_xlsx():
    data = build_template_xlsx()
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 'attachment; filename="talent-finder-template.xlsx"'})


@app.get("/api/template.csv")
def template_csv():
    return Response(build_template_csv(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="talent-finder-template.csv"'})


@app.post("/api/bulk/validate")
def bulk_validate():
    """
    Parse and report WITHOUT resolving anything.

    Worth its own endpoint: it tells the operator how their headers were read
    and which rows will be skipped, before spending any upstream quota.
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded (form field 'file')."}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "The uploaded file is empty."}), 400

    parsed = bulk_mod.parse_upload(f.filename or "", data)
    if parsed.errors:
        return jsonify({"error": parsed.errors[0], "errors": parsed.errors,
                        "header_map": parsed.header_map,
                        "unmapped_headers": parsed.unmapped_headers}), 422

    return jsonify({
        "filename": f.filename,
        "header_map": parsed.header_map,
        "unmapped_headers": parsed.unmapped_headers,
        "total_rows": len(parsed.rows),
        "valid_rows": len(parsed.valid_rows),
        "skipped_rows": [{"row_number": r.row_number,
                          "why": "; ".join(r.warnings) or "empty name"}
                         for r in parsed.invalid_rows],
        "warnings": [{"row_number": r.row_number, "warnings": r.warnings}
                     for r in parsed.valid_rows if r.warnings][:50],
        "with_role": sum(1 for r in parsed.valid_rows if r.role),
        "without_role": sum(1 for r in parsed.valid_rows if not r.role),
        "preview": [{"row_id": r.row_id, "name": r.name, "role": r.role,
                     "active_year": r.active_year, "country": r.country}
                    for r in parsed.valid_rows[:10]],
        "note": ("Rows without a role will usually come back as "
                 "'disambiguate' if the name is shared."),
    })


@app.post("/api/bulk")
def bulk_resolve():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded (form field 'file')."}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "The uploaded file is empty."}), 400

    parsed = bulk_mod.parse_upload(f.filename or "", data)
    if parsed.errors:
        return jsonify({"error": parsed.errors[0],
                        "errors": parsed.errors}), 422
    if not parsed.valid_rows:
        return jsonify({"error": "No rows with a name were found."}), 422

    try:
        max_rows = min(int(request.form.get("max_rows", 500)), 1000)
    except ValueError:
        max_rows = 500
    try:
        budget = min(float(request.form.get("budget_seconds", 75)), 100.0)
    except ValueError:
        budget = 75.0

    fetch = _fetcher()

    def resolve_one(pr):
        return resolve(
            Intake(name=pr.name, expected_role=pr.role or None,
                   country=pr.country or None, active_year=pr.active_year,
                   context=pr.context, client=pr.client),
            fetch, _store, tmdb_key=TMDB_KEY, do_bidirectional=False,
            serpapi_key=SERPAPI_KEY, serpapi_engine=SERPAPI_ENGINE)

    batch = bulk_mod.run_batch(parsed, resolve_one, max_rows=max_rows,
                               time_budget_seconds=budget)
    payload = batch.as_dict()
    payload["mode"] = "live" if LIVE else "demo"
    payload["header_map"] = parsed.header_map
    counts: dict[str, int] = {}
    for r in batch.rows:
        d = str(r.get("decision", "?"))
        counts[d] = counts.get(d, 0) + 1
    payload["decision_counts"] = counts
    return jsonify(payload)


@app.post("/api/bulk/export")
def bulk_export():
    """Turn a JSON result set back into a downloadable sheet."""
    body = request.get_json(silent=True) or {}
    rows = body.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "No rows to export."}), 400
    fmt = (body.get("format") or "xlsx").lower()
    if fmt == "csv":
        return Response(bulk_mod.to_csv(rows), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 'attachment; filename="talent-finder-results.csv"'})
    return Response(
        bulk_mod.to_xlsx(rows),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 'attachment; filename="talent-finder-results.xlsx"'})


# ---------------------------------------------------------------------------
# large batches: submit -> poll -> download
# ---------------------------------------------------------------------------

MAX_JOB_ROWS = int(os.environ.get("IF_MAX_JOB_ROWS", 1000))


def _make_resolver(fetch):
    def resolve_one(pr):
        return resolve(
            Intake(name=pr.name, expected_role=pr.role or None,
                   country=pr.country or None, active_year=pr.active_year,
                   context=pr.context, client=pr.client),
            fetch, _store, tmdb_key=TMDB_KEY, do_bidirectional=False,
            serpapi_key=SERPAPI_KEY, serpapi_engine=SERPAPI_ENGINE)
    return resolve_one


@app.post("/api/bulk/submit")
def bulk_submit():
    """
    Start a background job. Returns immediately with a job_id.

    This is the route for 250-500 rows: the work outlives the request, so the
    120s gunicorn timeout stops being the ceiling on batch size.
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded (form field 'file')."}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "The uploaded file is empty."}), 400

    parsed = bulk_mod.parse_upload(f.filename or "", data)
    if parsed.errors:
        return jsonify({"error": parsed.errors[0], "errors": parsed.errors}), 422
    rows = parsed.valid_rows
    if not rows:
        return jsonify({"error": "No rows with a name were found."}), 422
    if len(rows) > MAX_JOB_ROWS:
        return jsonify({"error": f"{len(rows)} rows exceeds the {MAX_JOB_ROWS} "
                                 "row cap for one job. Split the sheet."}), 413

    job_id = _jobs.create(f.filename or "upload", len(rows))
    _runner.submit(job_id, rows, _make_resolver(_fetcher()),
                   bulk_mod.flatten_result)
    est = len(rows) * (7 if LIVE else 0.05)
    return jsonify({
        "job_id": job_id, "total": len(rows), "state": "queued",
        "header_map": parsed.header_map,
        "estimated_seconds": round(est),
        "poll": f"/api/bulk/status/{job_id}",
        "note": ("Keep this page open while it runs. On Render's free tier the "
                 "instance sleeps when idle, and a sleeping instance is not "
                 "working on your job."),
    }), 202


@app.get("/api/bulk/status/<job_id>")
def bulk_status(job_id):
    st = _jobs.status(job_id)
    if not st:
        return jsonify({"error": "Unknown job_id."}), 404
    return jsonify(st)


@app.get("/api/bulk/jobs")
def bulk_jobs():
    return jsonify({"jobs": _jobs.recent()})


@app.post("/api/bulk/cancel/<job_id>")
def bulk_cancel(job_id):
    if not _jobs.status(job_id):
        return jsonify({"error": "Unknown job_id."}), 404
    _runner.cancel(job_id)
    return jsonify(_jobs.status(job_id))


@app.get("/api/bulk/result/<job_id>")
def bulk_result(job_id):
    """
    Download whatever is finished - works mid-run, not just at the end, so a
    long job is never all-or-nothing.
    """
    st = _jobs.status(job_id)
    if not st:
        return jsonify({"error": "Unknown job_id."}), 404
    rows = _jobs.rows(job_id)
    if not rows:
        return jsonify({"error": "No rows finished yet.", "status": st}), 409
    fmt = (request.args.get("format") or "xlsx").lower()
    if fmt == "json":
        return jsonify({"status": st, "columns": bulk_mod.OUTPUT_COLUMNS,
                        "rows": rows})
    if fmt == "csv":
        return Response(bulk_mod.to_csv(rows), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="results-{job_id}.csv"'})
    return Response(
        bulk_mod.to_xlsx(rows),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="results-{job_id}.xlsx"'})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8020)), debug=False)
