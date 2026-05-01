"""
config.py — Centralised configuration for the disambiguation pipeline.
"""
import os
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ── Directory layout ──────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent.parent
DATA_DIR         = BASE_DIR / "data"
CACHE_DIR        = BASE_DIR / "cache"
PDF_CACHE_DIR    = CACHE_DIR / "pdfs"
GROBID_CACHE_DIR = CACHE_DIR / "grobid"
LLM_CACHE_DIR    = CACHE_DIR / "llm"
OA_CACHE_DIR     = CACHE_DIR / "openalex"

for _d in [DATA_DIR, PDF_CACHE_DIR, GROBID_CACHE_DIR, LLM_CACHE_DIR, OA_CACHE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Input / output files ──────────────────────────────────────────────────────
FLORA_SHEET_PATH    = DATA_DIR / "FLoRA entry sheet - replication list.csv"
OPENALEX_CANDS_PATH = DATA_DIR / "openalex_candidates.csv"
ALL_REPLICATIONS_PATH = DATA_DIR / "all_replications.csv"

# Multiple Matches pipeline
FILTERED_CSV_PATH   = DATA_DIR / "multiple_match_candidates.csv"
FINAL_OUTPUT_PATH   = DATA_DIR / "multiple_match_resolved.csv"
REVIEW_CSV_PATH     = DATA_DIR / "multiple_match_resolved_review.csv"

# Multiple Originals pipeline
MULTI_ORIG_CANDS_PATH    = DATA_DIR / "multi_original_candidates.csv"
MULTI_ORIG_RESOLVED_PATH = DATA_DIR / "multi_original_resolved.csv"

# ── API keys ──────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")

# SerpAPI keys in rotation order — add SERPAPI_KEY_2 to .env for failover
SERPAPI_KEYS: list[str] = [
    k for k in [
        os.getenv("SERPAPI_KEY",  ""),
        os.getenv("SERPAPI_KEY_2", ""),
    ] if k
]
SERPAPI_KEY = SERPAPI_KEYS[0] if SERPAPI_KEYS else ""  # backward-compat

# All Gemini keys in rotation order
GEMINI_API_KEYS: list[str] = [
    k for k in [
        os.getenv("GEMINI_API_KEY",   ""),
        os.getenv("GEMINI_API_KEY_2", ""),
        os.getenv("GEMINI_API_KEY_3", ""),
        os.getenv("GEMINI_API_KEY_4", ""),
    ] if k
]
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""  # backward-compat

RESEARCHER_EMAIL = os.getenv("RESEARCHER_EMAIL", "research@example.com")

# ── Model identifiers ─────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

# ── External servers ──────────────────────────────────────────────────────────
GROBID_SERVER = "https://kermitt2-grobid.hf.space"

# ── Rate limits (seconds between calls) ──────────────────────────────────────
OPENALEX_RATE_SEC  = 0.1
UNPAYWALL_RATE_SEC = 0.5
GROBID_RATE_SEC    = 3.0
LLM_RATE_SEC       = 1.0

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("flora.disambiguation")
