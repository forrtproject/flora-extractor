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
SNAPSHOT_CACHE_DIR   = CACHE_DIR / "snapshot"        # OpenAlex bulk-parquet manifest + scan ledger
ENGINE_CACHE_DIR     = CACHE_DIR / "engine"          # filter-engine routing releases + DuckDB store

for _d in [DATA_DIR, PDF_CACHE_DIR, GROBID_CACHE_DIR, LLM_CACHE_DIR,
           OA_CACHE_DIR, OA_XML_CACHE_DIR, PARSE_CACHE_DIR, MARKITDOWN_CACHE_DIR,
           DOI_VERIFY_CACHE_DIR, SNAPSHOT_CACHE_DIR, ENGINE_CACHE_DIR]:
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

# ── Stage 3 cheap pre-screen (issue #130) ─────────────────────────────────────
# An optional tier in front of the validated screen: two very small models that can
# only DISCARD, and only when both agree the row is clearly out of scope. Everything
# else — one keep, an unreadable reply, an API failure — falls through to the screen
# unchanged, so the tier can lose papers but can never add them.
#
# Default OFF. A pre-screen discard is terminal and never reaches a human, so the
# validated screen's measured zero-settled-miss property does not extend to it.
# Deliberately separate from SCREEN_VOTER2_MODEL: changing a pre-screen model must
# never silently alter the validated screen's cached verdicts.
PRESCREEN_ENABLED = os.getenv("PRESCREEN_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
# Both measured on the full eval and both reliable under concurrency: 8/8 clean replies,
# 9 output tokens, ~$0.027 per 1,000 rows each. The model matters as much as the prompt —
# on identical text, discard rates across the cheap field ran from 15% to 97%.
#
# Order is a cost choice, not a verdict choice: the gate discards only when BOTH say no,
# so voter 2 is asked only about the rows voter 1 rejects. qwen goes first because it
# rejects slightly less often, which is what voter 2 is billed for.
#
# mistral-nemo would be $1/pass cheaper for a marginally better discard rate and was NOT
# chosen: alone on this prompt it discards 39 of 567 gold positives, against 5 and 12 for
# these two. The pair's measured loss is still zero, but a voter that says no to one
# genuine replication in fourteen makes the AND gate a single-voter gate with a noisy
# co-signer, and the tier's whole safety argument is that two independent voters agree.
#
# Not inclusionai/ling-2.6-flash, 3x cheaper again: it has a single OpenRouter endpoint
# (Novita) that returned 429 for every call on 2026-08-02 under every routing mode, with
# credit on the account. If that clears it is worth re-measuring — one env var.
PRESCREEN_VOTER1_MODEL = os.getenv("PRESCREEN_VOTER1_MODEL", "qwen/qwen3-30b-a3b-instruct-2507")
PRESCREEN_VOTER2_MODEL = os.getenv("PRESCREEN_VOTER2_MODEL", "mistralai/mistral-small-24b-instruct-2501")
# Below this many characters of abstract there is not enough text for a 3B-class model
# to be trusted with a terminal verdict, so the row bypasses the pre-screen.
PRESCREEN_MIN_ABSTRACT_CHARS = int(os.getenv("PRESCREEN_MIN_ABSTRACT_CHARS", "200"))

# ── Stage 1 engine source ─────────────────────────────────────────────────────
# FLORA_USE_ENGINE=1 (or true/yes/on) opts into the YAML-spec engine source.
FLORA_USE_ENGINE = os.getenv("FLORA_USE_ENGINE", "").strip().lower() in {"1", "true", "yes", "on"}

# ── Stage 2 curated sources ───────────────────────────────────────────────────
# Rows from these sources were put on a curated replication/reproduction list by a
# human, so Stage 2's keyword discovery can only lose them: an I4R reproduction
# titled "A comment on Smith et al. (2023)" carries no replication vocabulary at
# all. They bypass phrase matching and go straight to needs_review for Stage 3's
# screen — the validated decider — to settle.
CURATED_SOURCES = frozenset(
    s.strip().lower()
    for s in os.getenv("CURATED_SOURCES", "i4r,bob_reed,backfill_old_pipeline").split(",")
    if s.strip()
)

# ── OpenAlex snapshot (bulk parquet) ──────────────────────────────────────────
# The public S3 bucket holding the whole corpus as column-projectable parquet.
# Overridable so a mirror or a local copy can be pointed at without code changes.
SNAPSHOT_BASE_URL = os.getenv("FLORA_SNAPSHOT_BASE_URL",
                              "https://openalex.s3.amazonaws.com/data/parquet")
# Attempts per partition file before it is skipped and reported at the end of the run.
SNAPSHOT_HTTP_RETRIES = int(os.getenv("SNAPSHOT_HTTP_RETRIES", "3"))
# Rows per pyarrow batch. Survivors are merged per batch (never per file), so this
# also bounds how much of a large partition is ever held in memory.
SNAPSHOT_BATCH_ROWS = int(os.getenv("SNAPSHOT_BATCH_ROWS", "50000"))
SNAPSHOT_HTTP_TIMEOUT = int(os.getenv("SNAPSHOT_HTTP_TIMEOUT", "60"))
# Compression for the Stage A survivor pool. The pool is the artifact that makes a
# Stage B vocabulary change a local re-run instead of a 725 GB rescan, so it is kept
# small and portable; zstd is the best size/speed trade pyarrow ships by default.
SNAPSHOT_POOL_COMPRESSION = os.getenv("SNAPSHOT_POOL_COMPRESSION", "zstd")
# Where that pool lives. Deliberately NOT in the mkdir loop above: the pool is a
# few GB and is often pointed at an external or shared disk, so importing config
# must not create it (or fail on an unmounted path) for the runs that never touch
# it — the scanner and search/pool_sync.py create it when they write.
SNAPSHOT_POOL_DIR = Path(os.getenv("FLORA_POOL_DIR") or (CACHE_DIR / "snapshot_pool"))
# Private Hugging Face dataset repo the pool is shared through (search/pool_sync.py).
# Empty by default — pool_sync says which variable to set rather than guessing a repo.
FLORA_POOL_REPO = os.getenv("FLORA_POOL_REPO", "")
# Files per Hugging Face commit when pushing. One commit per file would put a single
# pool push (~2,446 files) past the "few thousand commits" at which HF says repo UX
# degrades, so uploads are batched into multi-file commits.
FLORA_HF_COMMIT_BATCH = int(os.getenv("FLORA_HF_COMMIT_BATCH", "100"))
# Concurrent file downloads in a pool pull. Each file costs ~0.7s of auth + CDN
# redirect before its first byte and only ~0.8s of transfer, so a serial pull
# spends about half its wall clock idle and never approaches the link's rate;
# measured on a home connection, 8 streams moved 4.4 MB/s against 1.0 MB/s
# serial. 8 is what huggingface_hub's own snapshot_download defaults to. Lower
# it on a metered or rate-limited connection.
FLORA_HF_PULL_WORKERS = max(1, int(os.getenv("FLORA_HF_PULL_WORKERS", "8")))
# Where a prebuilt candidates artifact (chunked parquet + manifest.json) is written
# and pulled into. Like the pool, not created at import time.
SNAPSHOT_BUILD_DIR = Path(os.getenv("FLORA_BUILD_DIR") or (CACHE_DIR / "snapshot_build"))
# Rows per parquet chunk in that artifact: dozens of files, not thousands, and each
# one small enough to read into memory whole while merging.
SNAPSHOT_BUILD_CHUNK_ROWS = int(os.getenv("SNAPSHOT_BUILD_CHUNK_ROWS", "100000"))

# ── Filter-engine LLM tiers: the dry-run cost estimate (issue #146 §6) ────────
# Rough list prices per 1,000 tokens, SUMMED OVER A TIER'S TWO VOTERS, so a tier's
# estimate is one multiplication per row. They exist to answer "is this run $3 or
# $3,000?" before it starts and are deliberately not a billing record — what a run
# actually cost is read from cache/token_usage.json afterwards. Update them when a
# voter model changes; they are env-overridable so a price cut needs no release.
ENGINE_TIER_PRICE_PER_1K_IN = {
    "screen_cheap":     float(os.getenv("ENGINE_PRICE_CHEAP_IN", "0.00014")),
    "screen_expensive": float(os.getenv("ENGINE_PRICE_EXPENSIVE_IN", "0.00055")),
}
ENGINE_TIER_PRICE_PER_1K_OUT = {
    "screen_cheap":     float(os.getenv("ENGINE_PRICE_CHEAP_OUT", "0.00045")),
    "screen_expensive": float(os.getenv("ENGINE_PRICE_EXPENSIVE_OUT", "0.00450")),
}
# Output tokens one row costs a tier. The cheap tier answers with one field; the
# expensive tier returns the five-field v3.3 schema with a quote and a reasoning.
ENGINE_TIER_OUTPUT_TOKENS = {
    "screen_cheap":     int(os.getenv("ENGINE_OUTPUT_TOKENS_CHEAP", "20")),
    "screen_expensive": int(os.getenv("ENGINE_OUTPUT_TOKENS_EXPENSIVE", "300")),
}
# Characters per token for the estimate. Nothing is tokenized to produce a number
# nobody will be billed on; 4.0 is the usual English-prose approximation.
ENGINE_CHARS_PER_TOKEN = float(os.getenv("ENGINE_CHARS_PER_TOKEN", "4.0"))

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
