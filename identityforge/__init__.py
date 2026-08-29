"""IdentityForge - identity-anchor resolution for talent/influencer/celebrity data."""
from .platforms import Platform, PlatformRef, classify_url, build_url, TARGET_PLATFORMS
from .aggregators import harvest, probe_urls, is_aggregator_url, looks_like_aggregator
from .evidence import Tier, Evidence, HandleClaim, Candidate, rank, score_role_match
from .resolver import Intake, EntityStore, resolve
from .discovery import discover, DiscoveryResult, Proposal
from .providers import search_person, enrich_from_tmdb, omdb_title
from .labels import normalize, variants, match_label, script_of, search_languages
from .wikidata import find_candidates
from .bulk import parse_upload, run_batch, to_csv, to_xlsx, INPUT_COLUMNS, OUTPUT_COLUMNS
from .template import build_template_xlsx, build_template_csv
from .fetcher import CachedFetcher, FixtureFetcher, build_fetcher, redact
from . import authorities

__version__ = "0.5.0"
