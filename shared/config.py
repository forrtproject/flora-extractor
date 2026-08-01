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
# An institutional token grants Scopus entitlement off the subscribing network;
# without it Elsevier entitlement is IP-bound (campus network / VPN).
ELSEVIER_INSTTOKEN = os.getenv("ELSEVIER_INSTTOKEN", "").strip()

# ── Model identifiers ─────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

# Per-task model selection — light for code_outcome and the screen's Gemini voter,
# heavy for the full identify_targets_with_llm linking step.
# Default light to gemini-3.5-flash-lite (cheap, high rate limits); it is also one of
# the two independent voters in the Stage 4.5 replication classifier.
GEMINI_LIGHT_MODEL = os.getenv("GEMINI_LIGHT_MODEL", "gemini-3.5-flash-lite")
GEMINI_HEAVY_MODEL = os.getenv("GEMINI_HEAVY_MODEL", GEMINI_MODEL)

# Thinking level for the heavy model (gemini-3-flash-preview accepts "minimal" or
# "high"). Empty — the default — sends nothing and keeps the model's own default,
# which is what every cached answer on disk was produced under. The redesign wants
# this flipped to "minimal": thinking tokens are billed as output and dominate the
# heavy model's cost, but the flip must follow a quality spot-check on linking and
# outcome coding, not precede it. It only applies to GEMINI_HEAVY_MODEL calls, and
# a non-empty value is folded into every cache key naming that model
# (cache_model_id() in shared/llm_client.py), so the two settings never share an
# answer.
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower()

# OpenRouter (OpenAI-compatible API at openrouter.ai) — optional alternative LLMs
OPENROUTER_API_KEY    = os.getenv("OPENROUTER_API_KEY",    "")
OPENROUTER_HEAVY_MODEL = os.getenv("OPENROUTER_HEAVY_MODEL", "qwen/qwen3.5-35b-a3b")
# Second voter of the front-door replication screen. On the v3.2 gate sweep this
# model paired with Gemini Flash-Lite discards 89% of adjudicated hard negatives
# with zero settled misses; Ministral via OpenRouter reached 73% on the same gate.
# An id containing "/" is routed to OpenRouter, anything else to OpenAI direct.
SCREEN_VOTER2_MODEL = os.getenv("SCREEN_VOTER2_MODEL", "gpt-5.4-mini")

# ── Stage 1 engine source ─────────────────────────────────────────────────────
# FLORA_USE_ENGINE=1 (or true/yes/on) opts into the YAML-spec engine source.
FLORA_USE_ENGINE = os.getenv("FLORA_USE_ENGINE", "").strip().lower() in {"1", "true", "yes", "on"}

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

# ── OpenAI flex tier ──────────────────────────────────────────────────────────
# The same trade as Gemini flex: 50% off standard pricing in exchange for queueing,
# on the metered provider this pipeline actually pays per token. Unlike Gemini it
# does not depend on which key is in use — flex is a property of the account — so a
# single flag decides it. A model that does not offer flex, or a queue that has no
# capacity, is answered at standard tier instead of losing the row.
OPENAI_USE_FLEX     = os.getenv("OPENAI_USE_FLEX", "").lower() in ("1", "true", "yes")
# Timeout in seconds for flex calls — must cover the queueing worst case.
OPENAI_FLEX_TIMEOUT = int(os.getenv("OPENAI_FLEX_TIMEOUT", "900"))

# ── Outcome extraction ────────────────────────────────────────────────────────
# When the abstract-based outcome LLM returns cannot_be_determined (or the
# abstract is empty) and parsed fulltext is available, escalate to a second
# fulltext-based LLM call. Set to false to disable the escalation step.
OUTCOME_FULLTEXT_ESCALATION = os.getenv(
    "OUTCOME_FULLTEXT_ESCALATION", "true").strip().lower() not in {"false", "0", "no"}

# ── Daily OpenAI token budget ─────────────────────────────────────────────────
# A hard ceiling on OpenAI tokens (prompt + completion) bought per calendar day —
# the metered spend in this pipeline. The running total is persisted (see
# shared/token_usage.py), so it survives a restart and is shared by concurrent runs;
# a call that would be made past the ceiling is refused rather than billed. Gemini
# and OpenRouter usage is recorded but never capped. Set OPENAI_DAILY_TOKEN_BUDGET=0
# to lift the cap — that is the explicit override, and nothing else disables it.
OPENAI_DAILY_TOKEN_BUDGET = int(os.getenv("OPENAI_DAILY_TOKEN_BUDGET", "8000000"))

# ── Rate limits (seconds between calls) ──────────────────────────────────────
OPENALEX_RATE_SEC  = float(os.getenv("OPENALEX_RATE_SEC", "0.3"))
# Europe PMC is keyless and public, so the default is deliberately polite. At 25 DOIs
# per boolean query it still clears ~60 DOIs/sec — comparable to the S2 batch tier.
EPMC_RATE_SEC      = float(os.getenv("EPMC_RATE_SEC", "0.4"))
CROSSREF_RATE_SEC  = float(os.getenv("CROSSREF_RATE_SEC",  "0.1"))
S2_RATE_SEC        = float(os.getenv("S2_RATE_SEC",        "0.5"))
SCOPUS_RATE_SEC    = float(os.getenv("SCOPUS_RATE_SEC",    "1.0"))  # Elsevier: ~1 req/sec
UNPAYWALL_RATE_SEC = 0.5
GROBID_RATE_SEC    = 3.0

# ── Abstract backfill: batch sizes and quota caps ────────────────────────────
# OpenAlex filter= accepts up to 50 pipe-separated ids per call.
OA_BATCH_SIZE   = int(os.getenv("OA_BATCH_SIZE", "50"))
# Europe PMC's search endpoint takes a boolean query, so a batch is
# 'DOI:"a" OR DOI:"b" ...' in one GET. 25 keeps the URL near 1.3 kB, well inside
# what the endpoint accepts.
EPMC_BATCH_SIZE = int(os.getenv("EPMC_BATCH_SIZE", "25"))
# S2's /graph/v1/paper/batch endpoint accepts up to 500 ids per call. Verified
# 2026-07-27/28 on a full production run over this corpus's entire 494,406-row S2
# target list: ~49.8 DOIs/sec sustained at a 14.5% hit rate, vs CrossRef's ~3/sec
# one-at-a-time. At 5.0s between batches, whole-batch failures (all 3 retries
# exhausted) clustered in the first ~10 minutes then dropped to near-zero (~2.4% of
# ~550 batches overall) — a real improvement over 3.0s's ~20% failure rate. Do not
# re-tune this by hammering the live API in quick isolated bursts: cumulative load
# on the key appears to matter, not just the gap between the two calls in front of you.
S2_BATCH_SIZE     = int(os.getenv("S2_BATCH_SIZE", "500"))
S2_BATCH_RATE_SEC = float(os.getenv("S2_BATCH_RATE_SEC", "5.0"))
# Keep a Scopus run under the ~10k/week quota.
SCOPUS_DEFAULT_LIMIT = int(os.getenv("SCOPUS_DEFAULT_LIMIT", "9000"))

# LLM rate limits are per provider and enforced against that provider's own
# last-call timestamp in shared/llm_client.py. A single global interval charged
# every provider for every other provider's calls — the two screen votes go to
# different providers and still waited a full second between them — which on a
# 2,000-row run is hours of pure sleeping that buys no quota headroom.
GEMINI_RATE_SEC     = float(os.getenv("GEMINI_RATE_SEC",     "1.0"))
OPENAI_RATE_SEC     = float(os.getenv("OPENAI_RATE_SEC",     "0.5"))
OPENROUTER_RATE_SEC = float(os.getenv("OPENROUTER_RATE_SEC", "0.5"))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("flora.disambiguation")
