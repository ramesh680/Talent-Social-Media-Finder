"""IdentityForge - identity-anchor resolution for talent/influencer/celebrity data."""
from .platforms import Platform, PlatformRef, classify_url, build_url, TARGET_PLATFORMS
from .aggregators import harvest, probe_urls, is_aggregator_url, looks_like_aggregator
from .evidence import Tier, Evidence, HandleClaim, Candidate, rank, score_role_match
from .resolver import Intake, EntityStore, resolve
from . import authorities

__version__ = "0.1.0"
