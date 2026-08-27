# Talent Social Media Finder (IdentityForge)

Identity-anchor resolution for talent / influencer / celebrity social handles.
Solves the same-name collision problem structurally rather than by fuzzy matching.

## The one idea

Stop resolving **names**. Resolve **identity anchors**.

A name is a non-unique string, so no amount of scraping fixes it. An anchor is a
node the person themselves attached their accounts to. Two consequences:

- **Wikidata is the hub, every platform ID is a spoke.** Spotify artist ID, IMDb
  `nm`, TMDB person, MusicBrainz MBID, Transfermarkt — all live as properties on
  one Q-item, next to occupation and DOB. Resolve to a Q-item once, harvest every
  spoke in one call. You never integrate 20 APIs pairwise.
- **A link-in-bio page is a self-declared multi-platform cluster in one fetch.**
  Nobody else's Linktree contains your Instagram, so one hit settles the collision.

## Modules

| file | role |
|---|---|
| `platforms.py` | URL → `(Platform, handle)` for the 8 targets. Strips tracking params, mobile subdomains, `twitter.com`→`x.com`; rejects `/p/`, `/watch`, `/tag/`, `/feed/` and other product routes that are not identities. |
| `authorities.py` | The ID hub. 11 target authorities + 20 spoke authorities, each mapped to its Wikidata P-number. Occupation buckets and per-role anchor priority. |
| `aggregators.py` | 22 link-in-bio domains, `__NEXT_DATA__` / `__NUXT__` / JSON-LD / anchor extractors, forward handle probing, reverse cascade, generic aggregator heuristic. |
| `evidence.py` | 5-tier evidence model, diminishing-returns confidence, role-match scoring, `resolved` / `disambiguate` / `not_found` routing. |
| `resolver.py` | Pipeline orchestration + SQLite entity store, collision registry and resolution log. |
| `wikidata.py` | Candidate lookup: `wbsearchentities` for recall across name variants and languages, then batched `wbgetentities` to keep humans only and pull occupation/dates. |
| `labels.py` | Name normalisation, variant generation, script detection, fuzzy label scoring. |
| `providers.py` | TMDB person search that refuses to guess on a collision; OMDb title context. |
| `discovery.py` | SerpAPI handle discovery. Proposals are Tier 5 — search can never auto-accept. |
| `fetcher.py` | Cached, rate-limited, allowlisted HTTP with query-string secret redaction. |
| `bulk.py` | CSV/XLSX ingest with tolerant header mapping, batching under a wall-clock budget, and CSV/XLSX writers. |
| `template.py` | Generates the fill-in upload template (Instructions / Input / Reference sheets). |

## Evidence tiers

| tier | example | weight |
|---|---|---|
| 1 structured ID | Wikidata claim, TMDB `external_ids`, MusicBrainz url-rel | 0.55 |
| 2 self-declared | Linktree, bio link, schema.org `sameAs` | 0.45 |
| 3 bidirectional | A links to B **and** B links to A | 0.78 |
| 4 corroborating | avatar hash, verified badge, shared domain | 0.15 |
| 5 name match | string equality | **0.00** |

Auto-accept ≥ 0.75, review ≥ 0.35. **A name match can never promote a claim** —
that hard rule is the entire point of the tool.

`Evidence.authoritative` marks the narrow set of sources where one hit is enough:
a Wikidata sitelink (1:1 with the Q-item by construction) and a curated IMDb
`nm`. Volunteer-added social handles are *not* authoritative — they go stale when
someone rebrands, so they still want corroboration.

## Design decisions worth knowing

**`disambiguate` is a feature, not a failure.** When the top two candidates are
within 0.25, the resolver refuses to guess and returns cards (photo, occupation,
DOB, notable work, ID count). A human clicks in 3 seconds. That beats 20 minutes
of searching *and* beats a silently wrong handle in a client deliverable.

**All network I/O is injected** as one `fetch(url, kind)` callable. Rate limiting,
caching, retries and user-agent policy live in one place, and the whole package is
unit-testable offline.

**Nine P-numbers are flagged unverified.** Run `authorities.unverified_properties()`
at startup and log it; a wrong P-number returns nothing *silently*. `validate_property_map(fetch_json)`
confirms each against the Wikidata API in batches of 40.

## Run it

```bash
pip install -r requirements.txt
python app.py            # http://localhost:8020  (demo mode, zero network)
IF_LIVE=1 python app.py  # real Wikidata / TMDB / MusicBrainz / link-in-bio
```

| route | does |
|---|---|
| `GET /` | UI: name + role + year in, tier ledger out |
| `POST /api/resolve` | `{name, role, active_year, context}` → handles + provenance |
| `POST /api/classify` | `{urls:[...]}` → `(platform, handle)`. Handy on its own for cleaning handle columns |
| `GET /api/audit` | P-number self-audit; validates against Wikidata in live mode |
| `GET /api/health` | mode + version, used as Render's health check |

## Bulk upload

Download the template from the running app: `/api/template.xlsx` or
`/api/template.csv`.

**Only `name` is required.** Headers are matched loosely, so a sheet that came
from a client or a Zendesk export usually works untouched — `Talent Name`,
`Title`, `Full Name`, `Artist Name` all map to `name`; `Title Category` and
`Occupation` map to `role`; `Sr. No.` and `TICKET-ID` map to `row_id`.

| column | required | what it does |
|---|---|---|
| `name` | **yes** | Punctuation, diacritic and initials variants are handled. Non-Latin scripts are searched in their own language. |
| `role` | strongly recommended | The single most valuable field — without it a shared name returns `disambiguate` instead of an answer. |
| `active_year` | no | Rules out candidates dead or unborn at the time. |
| `country` | no | Country name or a Wikidata Q-id. |
| `context` | no | Show, brand or campaign. Recorded for audit. |
| `client` | no | Account or brand set. Recorded for audit. |
| `row_id` | no | Echoed back so you can join results to your source sheet. |
| `notes` | no | Passed through. |

Role synonyms are mapped (`singer`→musician, `footballer`→athlete,
`actress`→actor, `influencer`→creator). An **unrecognised** role is dropped with
a warning rather than guessed — a wrong role hint is worse than none, because it
actively pushes the resolver at the wrong person.

### Endpoints

| route | does |
|---|---|
| `POST /api/bulk/validate` | Parse and report **without resolving**. Shows how each header was read and which rows will be skipped, before spending upstream quota. |
| `POST /api/bulk` | Resolve. `max_rows` and `budget_seconds` form fields. |
| `POST /api/bulk/export` | JSON rows back out as `.xlsx` or `.csv`. |

### Why batches are small

Live resolution costs roughly 5–10 seconds per name, because the rate limiter
deliberately paces MusicBrainz at 1.1s and Wikidata at 0.4s. Against a 120s
gunicorn timeout that is ~15–25 names per request, so `run_batch` works to a
wall-clock budget and returns the rows it did not attempt instead of dying at
row 14. Resubmit those; the HTTP cache makes the second pass much cheaper.

For hundreds of names, run it locally where there is no HTTP timeout.

### Output

One row per input row, with the eight platform columns plus `decision`,
`entity_id`, `coverage`, `needs_review`, `spoke_ids`, `alternates` and `notes`.
Four decisions are visibly distinct: `resolved`, `disambiguate` (candidates in
`alternates`), `unverified_only` (search proposals, confirm before use) and
`not_found`. Platform columns stay **empty** unless a handle was actually
accepted — a guess is never written into a handle column.

### Deploy to Render

`render.yaml` is a blueprint — point Render at the repo and it reads it. Free plan,
`gunicorn`, health check on `/api/health`, **demo mode by default** so a public URL
never fires outbound requests until you deliberately set `IF_LIVE=1`.

Set `TMDB_API_KEY` in the Render dashboard, never in the repo.

**Render's free tier has an ephemeral filesystem.** The SQLite entity store and
HTTP cache live in `/tmp` and are wiped on every deploy and cold start. That is
fine for evaluation, but the compounding entity store — the thing that stops you
researching the same ambiguous name twice — only compounds with a persistent
disk or Postgres. Budget for that before relying on it.

## Usage

```python
from identityforge import Intake, EntityStore, resolve

store = EntityStore("identityforge.db")
result = resolve(
    Intake(name="Michael Jackson", expected_role="musician",
           active_year=2005, context="Sony Music catalogue"),
    fetch=my_cached_fetcher, store=store, tmdb_key=TMDB_KEY)

if result["decision"] == "disambiguate":
    render_cards(result["cards"])       # human picks
else:
    result["handles"]        # accepted, per platform, with provenance
    result["needs_review"]   # 0.35-0.75, one click to confirm
    result["external_ids"]   # spotify/musicbrainz/tmdb/... for free
```

## Name matching

The first version matched labels exactly:

```
?p rdfs:label|skos:altLabel "A. R. Rahman"@en
```

Case-, punctuation- and diacritic-sensitive, so `A.R. Rahman`, `AR Rahman` and
every non-Latin spelling returned nothing while the person sat in the graph.
Silent false negatives are worse than collisions: the operator reads
`not_found` as "no Wikidata item" and goes back to manual work.

Now: `wbsearchentities` across generated name variants in the name's own
script plus English (recall), then batched `wbgetentities` to drop non-humans
and score the label match (precision). Every candidate carries
`label_match: {score, how, matched}` where `how` is one of exact, normalized,
initials, token, fuzzy, translit — a `translit` match deserves more scrutiny
than an `exact` one, so the UI can say which it was.

**Transliteration is deliberately not the matching mechanism.** Unidecode
renders `शाहरुख़ ख़ान` as `shaahrukh' kh'aan` and Tamil `ஏ. ஆர். ரகுமான்` as
`ee. aar. rkumaannn` — fine for telling names apart, useless for matching them.
Cross-script matching is delegated to Wikidata, which already stores each
person's name in every script they're known in. Cross-script scores are capped
at 0.75 so they can never alone carry a decision.

## Verification status

161 offline tests pass across three suites (`test_all`, `test_providers`,
`test_labels`), including the full Michael Jackson collision (singer /
footballer / beer writer) end to end, the guarantee that search-only evidence
can never auto-accept, and a check that no API key reaches the cache table.

**Not yet verified against live endpoints** — this container can only reach
PyPI/GitHub, so no request has actually hit Wikidata, Linktree, TMDB or
MusicBrainz. Before trusting it: run the P-number audit, confirm the SPARQL query
returns on a real endpoint (`rdfs:label|skos:altLabel` with a bare `"name"@en`
match is exact and case-sensitive — you will likely need to add a normalised
label fallback), and confirm the Linktree `__NEXT_DATA__` shape still holds.

## Phase 2 candidates

1. **Live validation + `fetch` adapter** with caching, backoff, robots/ToS policy.
2. **Avatar perceptual hashing** — Tier 4, and unusually decisive in practice.
3. **Transliteration for label matching** — you already hit this on the ~5k talent
   reconciliation; Devanagari/Latin variants of the same name need to collapse to
   one candidate set before role scoring runs.
4. **Handle-permutation probing** for people with no Wikidata item at all — the
   long tail of micro-influencers, where the forward probe path carries the load.
5. **TitleForge integration**: `title_category` / `title_sub_category` already
   imply a role bucket, so intake hints come free from data you hold.
