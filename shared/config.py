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
# cache/ holds hundreds of thousands of small per-identifier files (abstracts, parse
# results, DOI verification, ...). Random reads of pre-existing small files scattered
# across one huge flat directory are a classic bad case for a spinning hard disk —
# each read costs a real seek, and NTFS lookups get slower as a directory's entry
# count grows. Measured on this repo's checkout (798k+ files in cache/abstracts/ on a
# 5400RPM HDD): 0.4ms for a nonexistent-key check (resolves from cached directory
# metadata, no seek) vs 32.7ms for an existing-file read (~80x) — enough to stall a
# 500k-row backfill for hours before it writes its first checkpoint. (Not a cloud-sync
# effect — confirmed no sync agent was running; the fixed disk was just an HDD.)
# FLORA_CACHE_DIR lets cache/ live on a faster disk (e.g. an SSD) while the repo
# itself stays wherever it needs to be; unset, behavior is unchanged (cache/ inside
# the repo, as before).
CACHE_DIR        = Path(os.getenv("FLORA_CACHE_DIR") or (BASE_DIR / "cache"))
PDF_CACHE_DIR    = CACHE_DIR / "pdfs"
GROBID_CACHE_DIR = CACHE_DIR / "grobid"
LLM_CACHE_DIR    = CACHE_DIR / "llm"
OA_CACHE_DIR     = CACHE_DIR / "openalex"
OA_XML_CACHE_DIR     = CACHE_DIR / "openalex_xml"   # GROBID XML from content.openalex.org
PARSE_CACHE_DIR      = CACHE_DIR / "parse"           # per-method parse results
MARKITDOWN_CACHE_DIR = CACHE_DIR / "markdown"        # raw .md files from MarkItDown
DOI_VERIFY_CACHE_DIR = CACHE_DIR / "doi_verify"      # CrossRef/OpenAlex DOI verification

for _d in [DATA_DIR, PDF_CACHE_DIR, GROBID_CACHE_DIR, LLM_CACHE_DIR,
           OA_CACHE_DIR, OA_XML_CACHE_DIR, PARSE_CACHE_DIR, MARKITDOWN_CACHE_DIR,
           DOI_VERIFY_CACHE_DIR]:
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
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY",    "")
OPENALEX_API_KEY  = os.getenv("OPENALEX_API_KEY",  "")  # optional: Bearer token for content.openalex.org + polite-pool upgrade

# OpenAlex keys in rotation order. OpenAlex bills per request against a per-key
# daily budget that resets at midnight UTC, so a long Stage 3 run can drain one
# key mid-run; the client rotates to the next on a budget refusal.
# Set OPENALEX_API_KEYS to a comma-separated list, or OPENALEX_API_KEY_N for N >= 2.
OPENALEX_API_KEYS: list[str] = [
    k for k in (
        [OPENALEX_API_KEY]
        + [k.strip() for k in os.getenv("OPENALEX_API_KEYS", "").split(",")]
        + [os.getenv(f"OPENALEX_API_KEY_{n}", "") for n in range(2, 10)]
    ) if k
]
# dict.fromkeys preserves order while dropping the duplicate that OPENALEX_API_KEY
# and the first entry of OPENALEX_API_KEYS usually are.
OPENALEX_API_KEYS = list(dict.fromkeys(OPENALEX_API_KEYS))

# SerpAPI keys in rotation order — add SERPAPI_KEY_2 to .env for failover
SERPAPI_KEYS: list[str] = [
    k for k in [
        os.getenv("SERPAPI_KEY",  ""),
        os.getenv("SERPAPI_KEY_2", ""),
    ] if k
]
SERPAPI_KEY = SERPAPI_KEYS[0] if SERPAPI_KEYS else ""  # backward-compat

# Dynamic Gemini key loading — add GEMINI_API_KEY_N to .env for any N ≥ 2.
# Keys must be sequential (2, 3, 4, …); loading stops at the first missing slot.
_gemini_key_list = [os.getenv("GEMINI_API_KEY", "")]
_key_idx = 2
while True:
    _k = os.getenv(f"GEMINI_API_KEY_{_key_idx}", "")
    if not _k:
        break
    _gemini_key_list.append(_k)
    _key_idx += 1
GEMINI_API_KEYS: list[str] = [k for k in _gemini_key_list if k]
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""  # backward-compat

RESEARCHER_EMAIL = os.getenv("RESEARCHER_EMAIL", "research@example.com")
# Semantic Scholar API key — accepts both S2_API_KEY and legacy SEMANTIC_SCHOLAR_KEY
S2_API_KEY = os.getenv("S2_API_KEY") or os.getenv("SEMANTIC_SCHOLAR_KEY", "")
# Elsevier Scopus API key — optional abstract-backfill tier (~10k requests/week quota)
ELSEVIER_API_KEY = os.getenv("ELSEVIER_API_KEY", "")

# ── Model identifiers ─────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
FILTER_OPENAI_MODEL = os.getenv("FILTER_OPENAI_MODEL", "gpt-5-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

# Per-task model selection — light for classify_match_type & code_outcome,
# heavy for the full identify_original_with_llm linking step.
# Default light to gemini-3.5-flash-lite (cheap, high rate limits); it is also one of
# the two independent voters in the Stage 4.5 replication classifier.
GEMINI_LIGHT_MODEL = os.getenv("GEMINI_LIGHT_MODEL", "gemini-3.5-flash-lite")
GEMINI_HEAVY_MODEL = os.getenv("GEMINI_HEAVY_MODEL", GEMINI_MODEL)

# OpenRouter (OpenAI-compatible API at openrouter.ai) — optional alternative LLMs
OPENROUTER_API_KEY    = os.getenv("OPENROUTER_API_KEY",    "")
OPENROUTER_LIGHT_MODEL = os.getenv("OPENROUTER_LIGHT_MODEL", "qwen/qwen3.5-35b-a3b")
OPENROUTER_HEAVY_MODEL = os.getenv("OPENROUTER_HEAVY_MODEL", "qwen/qwen3.5-35b-a3b")
# Second voter of the Stage 4.5 replication screen, called through OpenRouter.
# Ministral 14B beat every alternative measured on adjudicated hard cases (89.4%
# correct vs 66% for gpt-5-mini) while discarding no genuine replication, and its
# errors overlap little with the Google first voter's.
SCREEN_VOTER2_MODEL = os.getenv("SCREEN_VOTER2_MODEL", "mistralai/ministral-14b-2512")

# ── External servers ──────────────────────────────────────────────────────────
GROBID_SERVER = os.getenv("GROBID_URL", "https://kermitt2-grobid.hf.space")

# ── Gemini flex inference ─────────────────────────────────────────────────────
# Flex inference costs 50% less than standard — the same discount as Batch, but
# without any job-submission plumbing — at the price of queueing for up to 15
# minutes per call. It is only available on paid-tier (billing-enabled) keys.
GEMINI_USE_FLEX     = os.getenv("GEMINI_USE_FLEX", "").lower() in ("1", "true", "yes")
# Timeout in seconds for flex calls — must cover the 15-minute worst case.
GEMINI_FLEX_TIMEOUT = int(os.getenv("GEMINI_FLEX_TIMEOUT", "900"))
# Which keys are paid, by 1-based slot number (GEMINI_API_KEY = 1, GEMINI_API_KEY_2 = 2, …).
# Flex follows the key rather than its position in the rotation, so a disabled or
# reordered key does not silently drop every call back to standard pricing.
GEMINI_PAID_KEYS: set[int] = {
    int(n) for n in os.getenv("GEMINI_PAID_KEYS", "1").replace(",", " ").split() if n.isdigit()
}

# ── Outcome extraction ────────────────────────────────────────────────────────
# When the abstract-based outcome LLM returns cannot_be_determined (or the
# abstract is empty) and parsed fulltext is available, escalate to a second
# fulltext-based LLM call. Set to false to disable the escalation step.
OUTCOME_FULLTEXT_ESCALATION = os.getenv(
    "OUTCOME_FULLTEXT_ESCALATION", "true").strip().lower() not in {"false", "0", "no"}

# Global read policy for dual-written LLM caches (see shared/cache.py):
#   accumulate — prefer the legacy DOI-keyed entry; preserves prior results
#                across prompt/model changes (good for experimentation). Default.
#   latest     — read only the content-keyed entry; guarantees the cached result
#                matches the current prompt/model/input (good for production).
LLM_CACHE_READ = os.getenv("LLM_CACHE_READ", "accumulate").strip().lower()

# ── Rate limits (seconds between calls) ──────────────────────────────────────
OPENALEX_RATE_SEC  = float(os.getenv("OPENALEX_RATE_SEC", "0.3"))
CROSSREF_RATE_SEC  = 0.1
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
