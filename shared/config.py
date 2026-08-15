"""
config.py — Centralised configuration for the disambiguation pipeline.
"""
import os
import logging
from pathlib import Path

# Two files, one mechanism. .env is gitignored and holds secrets and per-machine
# settings; .env.defaults is committed and holds the shared, non-secret project
# identifiers (see its header for what may go in it). load_dotenv never overwrites a
# variable that is already set, so loading .env first gives:
#     real environment > .env > .env.defaults > the os.getenv default below.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    load_dotenv(Path(__file__).parent.parent / ".env.defaults")
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
# The abstract cache (shared/abstract_store.py). One SQLite file rather than a
# directory: at ~478k identifiers of ~90 bytes each, file-per-key spent 1.8 GB of
# blocks on 42 MB of text and turned every whole-cache operation into half a
# million syscalls. It also absorbs the two sidecar files that layout needed.
ABSTRACT_DB_PATH     = CACHE_DIR / "abstracts.sqlite"
ENGINE_CACHE_DIR     = CACHE_DIR / "engine"          # filter-engine routing releases + DuckDB store
# The standard home of the text overlay (project-written text for pool rows the
# snapshot shipped without an abstract). The engine's commands read it here by
# default, so an overlay-dependent rule cannot silently match nothing because a
# flag was forgotten; it is NOT created up front, because "the directory holds
# overlay chunks" is the signal that there is an overlay to read at all.
OVERLAY_DIR          = Path(os.getenv("FLORA_OVERLAY_DIR")
                            or (ENGINE_CACHE_DIR / "overlay"))

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
# OSF personal access token — optional. The registration endpoint the backfill
# reads is public; a token only raises the throttle (OSF meters anonymous
# callers per hour and authenticated ones per day).
OSF_TOKEN = os.getenv("OSF_TOKEN", "").strip()

# ── Model identifiers ─────────────────────────────────────────────────────────
# Deliberately NOT env-overridable. Which model answers is a project decision, not a
# per-machine one: the models are the classifier, they are folded into every cache
# key, and the evaluations under analysis/ are evidence about these exact ids. A
# per-machine override would let two collaborators produce differently-graded rows
# into one shared corpus with nothing in the repo recording it. Changing a model is a
# commit — reviewable, and dated by git.
#
# One model answers each call. There is no provider ladder: a call that fails is
# reported as a failure, never re-asked of a different model, because a row silently
# graded by the second-choice provider is not comparable to its neighbours and the
# outage that caused it leaves no trace. Every constant below therefore names exactly
# one place in the pipeline, and every provider call names its model explicitly.
# Each is named for the QUESTION it answers, not for the provider that serves it:
# the id already says the provider, and the pipeline is described by what a model is
# asked. Every model the pipeline calls is here — nothing is set further in, so this
# block is the whole answer to "what graded this row".

# The cheap discard-only tier (shared/prescreen.py) — is this obviously out of scope?
# The tier may only DISCARD, and only on two explicit noes, so the two must not be the
# same model: two votes from one model are one vote.
#
# Both measured on the full eval and both reliable under concurrency: 8/8 clean replies,
# 9 output tokens, ~$0.027 per 1,000 rows each. The model matters as much as the prompt —
# on identical text, discard rates across the cheap field ran from 15% to 97%.
#
# Order is a cost choice, not a verdict choice: the gate discards only when BOTH say no,
# so model 2 is asked only about the rows model 1 rejects. qwen goes first because it
# rejects slightly less often, which is what model 2 is billed for.
#
# mistral-nemo would be $1/pass cheaper for a marginally better discard rate and was NOT
# chosen: alone on this prompt it discards 39 of 567 gold positives, against 5 and 12 for
# these two. The pair's measured loss is still zero, but a voter that says no to one
# genuine replication in fourteen makes the AND gate a single-voter gate with a noisy
# co-signer, and the tier's whole safety argument is that two independent voters agree.
#
# Not inclusionai/ling-2.6-flash, 3x cheaper again: it has a single OpenRouter endpoint
# (Novita) that returned 429 for every call on 2026-08-02 under every routing mode, with
# credit on the account. If that clears it is worth re-measuring.
# Evidence: analysis/prescreen_eval/REPORT.md.
PRESCREEN_MODEL_1 = "qwen/qwen3-30b-a3b-instruct-2507"
PRESCREEN_MODEL_2 = "mistralai/mistral-small-24b-instruct-2501"

# The front-door screen (classify_replication) — is this a replication at all? Two
# independent voters on the validated v3.2 schema. An id containing "/" is routed to
# OpenRouter, a leading "gemini" to Google, anything else to OpenAI direct.
#
# Voter 1 was gemini-3.5-flash-lite at "minimal" until 2026-08-13 (v3.2 gate sweep:
# 89% hard-negative discard, zero settled misses; the v3.3 re-runs of that pair span
# 0-1 misses at 84.3-88.6%). DeepSeek at reasoning effort "low" measured at parity on
# the same 390 cases against the same cached gpt-5.4-mini votes — 1 settled miss,
# 88.4% — at roughly half the price (analysis/screening_eval/cheap_voter_2026-08.md).
# The same model at effort "none" missed 7 settled positives: the thinking is what
# buys the confident-recall calibration the gate needs from this slot, so the effort
# is part of the evaluated configuration, not a tunable.
SCREENING_MODEL_1 = "deepseek/deepseek-v4-flash"
SCREENING_MODEL_2 = "gpt-5.4-mini"

# Linking (resolve_targets_and_outcomes) — WHICH original does this paper re-test? One
# model for all three rungs: the abstract, the reference list and the full text ask
# the same question of different evidence.
#
# gpt-5.6-luna is OpenAI's small flagship-generation model: stronger than gpt-5.4-mini
# on every published benchmark and, at flex tier, about a quarter of its price per
# token ($0.10/M in, $0.60/M out against $0.375/$2.25 on 2026-08-15). It is not in
# the free daily token allocation, so every call is billed — set
# OPENAI_DAILY_TOKEN_BUDGET=0 for a run, or the 9.5M free-tier cap stops it. The task
# is long-context retrieval — pick the right entries out of ~80 keyed reference lines
# without confusing them — which is why the OpenAI small models were preferred over
# Gemini 3 Flash here (MRCR v2). Rows settled under gpt-5.4-mini in the 2026-08
# campaign are kept, not re-bought: `_GENERATION_EQUIVALENCES` in extract/tier.py
# declares that generation still current, and each verdict stamps the models that
# produced it. Linking has never been scored against data/flora.csv — it is the next
# thing to measure, and this choice is a benchmark inference until then.
LINKING_MODEL = "gpt-5.6-luna"

# Outcome coding (extract/code_outcome.py) — WHAT was the outcome? Both the abstract
# pass and the full-text escalation. Same model as linking, for the same reasons; the
# per-target outcome is coded in the same call as the link, so the two are one
# reading of the evidence.
OUTCOME_MODEL = "gpt-5.6-luna"

# Reference extraction from a document (shared/grobid.py) — the only call that is sent
# a PDF or page images rather than text. Its answers are cached under filenames that
# name this id and the effort, so changing either re-parses rather than mis-reads.
# Reference extraction is transcription, not reasoning: measured over all 58 cached
# fallback PDFs (2026-08-13), flash-lite at minimal thinking matched or bettered
# gemini-3-flash-preview at its default thinking on every inspected disagreement,
# while the default-thinking condition spent ~85% of its output tokens on thinking
# and produced the one invalid-JSON answer in the sample.
PDF_PARSE_MODEL = "gemini-3.1-flash-lite"
PDF_PARSE_EFFORT = "minimal"

# How hard the linking model is asked to think. One constant, not one per provider,
# so it survives LINKING_MODEL being repointed: llm_client sends it as Gemini's
# `thinkingLevel` or as OpenAI's/OpenRouter's `reasoning_effort`, whichever the id
# routes to. Both vocabularies accept "minimal"/"low", "medium" and "high".
#
# Set explicitly rather than left to the provider default, because reasoning tokens
# are billed as OUTPUT and are the whole cost risk of a reasoning model answering a
# call whose visible answer is a small JSON. "medium" is the middle rung: linking is
# a judgement call over a long list, so the screen's "low" is too little, and the
# ladder's Gemini leg was effectively spending at the model's own default with no one
# naming it.
#
# It reaches the request because the linking call site passes it explicitly, and it
# names that call's cache entries because the same call site passes it to
# cache_model_id() (shared/llm_client.py), so two settings never share an answer.
# Nothing is inferred from the model id: two call sites may name the same model
# (LINKING_MODEL and OUTCOME_MODEL do), and dispatching on the id once sent linking's
# effort to calls that never asked for one.
LINKING_EFFORT = "medium"

# How hard each of the screen's two voters is asked to think. The screen answers a
# small fixed schema over one abstract, so it buys the cheapest rung above nothing;
# these are constants rather than literals at the call site because they reach the
# classify cache key, and a value that decides what an entry means is changed by a
# commit.
#
# One per voter, not one for the pair: these pin the configuration the screen was
# EVALUATED at, and the two voters were evaluated at different rungs. Voter 1
# (DeepSeek, whose thinking is ON by default at "high") was evaluated at "low" —
# and at "none" it discarded 7 settled positives, so the effort is load-bearing.
# Naming it explicitly rather than trusting a provider default is the point: a
# changed default could silently move the voter with no change to the cache key —
# every entry would keep claiming to be the evaluated configuration. Voter 2 was
# evaluated at "low".
SCREENING_EFFORT_1 = "low"
SCREENING_EFFORT_2 = "low"

# How hard OUTCOME_MODEL is asked to think (extract/code_outcome.py, both passes).
# Every outcome row on disk was coded at medium, and the value reaches the outcome
# cache key and the extract generation, so it is a constant here rather than a
# literal at the call site. Outcome coding may well not need medium — lowering it is
# a deliberate, evaluated change that re-codes rows, not a default.
OUTCOME_EFFORT = "medium"

# OpenRouter (OpenAI-compatible API at openrouter.ai). Nothing routes here by
# default: it is reached only by a model id that names it — the pre-screen's two
# models, and SCREENING_MODEL_2 when it carries a "/".
OPENROUTER_API_KEY    = os.getenv("OPENROUTER_API_KEY",    "")

# ── Stage 2 curated sources ───────────────────────────────────────────────────
# Rows from these sources were put on a curated replication/reproduction list by a
# human, so Stage 2's keyword discovery can only lose them: an I4R reproduction
# titled "A comment on Smith et al. (2023)" carries no replication vocabulary at
# all. They bypass phrase matching and go straight to needs_review for Stage 3's
# screen — the validated decider — to settle.
# A closed vocabulary: these strings are written by Stage 1's own source tags, so the
# set is a fact about the corpus, not a setting. Adding one is a code change.
CURATED_SOURCES = frozenset({"i4r", "bob_reed", "backfill_old_pipeline"})

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

# ── Filter-engine LLM tiers (filter/engine/tiers.py) ──────────────────────────
# How many works a tier run judges at once. The works are independent and each one
# is almost entirely provider latency, so a sequential loop spends the run waiting;
# the per-provider rate limit in shared/llm_client.py — not this number — is what
# bounds the request rate, so raising it buys overlap, never a faster provider.
ENGINE_TIER_WORKERS = int(os.getenv("ENGINE_TIER_WORKERS", "8"))
# Whether a tier run pushes its raw response blobs to the Hugging Face pool repo.
# On by default, and off for a run that wants nothing to do with HF: the blobs are
# on disk either way and their verdict rows say `response_pending_upload`, which is
# what a later reconciliation run reads. Emptying HF_TOKEN also disables the push,
# but it disables every other HF path in the process with it — this is the flag.
ENGINE_TIER_HF_UPLOAD = os.getenv("ENGINE_TIER_HF_UPLOAD", "true").lower() not in (
    "0", "false", "no")

# ── Stage 3 row loop (extract/run_extract.py) ─────────────────────────────────
# How many filtered.csv rows Stage 3 has in flight at once. A row is ~1.5 minutes of
# provider and download latency and rows are independent, so a sequential loop spends
# the run waiting. What bounds the request rate is the per-service reservation queue
# in shared/rate_limit.py — not this number — so raising it buys overlap, never a
# faster provider. 1 is a genuinely sequential run: no pool is created at all.
EXTRACT_WORKERS = int(os.getenv("EXTRACT_WORKERS", "4"))

# ── External servers ──────────────────────────────────────────────────────────
GROBID_SERVER = os.getenv("GROBID_URL", "https://kermitt2-grobid.hf.space")

# ── Gemini flex inference ─────────────────────────────────────────────────────
# Flex inference costs 50% less than standard — the same discount as Batch, but
# without any job-submission plumbing — at the price of queueing for up to 15
# minutes per call. It is only available on paid-tier (billing-enabled) keys.
GEMINI_USE_FLEX     = os.getenv("GEMINI_USE_FLEX", "").lower() in ("1", "true", "yes")
# Timeout in seconds for flex calls — must cover the 15-minute worst case.
GEMINI_FLEX_TIMEOUT = int(os.getenv("GEMINI_FLEX_TIMEOUT", "900"))
# WHICH SLOTS are paid, by 1-based number (GEMINI_API_KEY = 1, GEMINI_API_KEY_2 = 2, …)
# — slot numbers, never key values. A fact about one machine's key layout, so it is
# env-set. Flex follows the key rather than its position in the rotation, so a
# disabled or reordered key does not silently drop every call back to standard pricing.
GEMINI_PAID_KEY_SLOTS: set[int] = {
    int(n) for n in os.getenv("GEMINI_PAID_KEY_SLOTS", "1").replace(",", " ").split()
    if n.isdigit()
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

# ── Daily OpenAI token budget ─────────────────────────────────────────────────
# A hard ceiling on OpenAI tokens (prompt + completion) bought per calendar day —
# the metered spend in this pipeline. The running total is persisted (see
# shared/token_usage.py), so it survives a restart and is shared by concurrent runs;
# a call that would be made past the ceiling is refused rather than billed. Gemini
# and OpenRouter usage is recorded but never capped. Set OPENAI_DAILY_TOKEN_BUDGET=0
# to lift the cap — that is the explicit override, and nothing else disables it.
# The default is OpenAI's free daily token allocation (9.5M, resets midnight UTC —
# the day _today() in shared/token_usage.py keys on).
OPENAI_DAILY_TOKEN_BUDGET = int(os.getenv("OPENAI_DAILY_TOKEN_BUDGET", "9500000"))

# ── Rate limits (seconds between calls) ──────────────────────────────────────
OPENALEX_RATE_SEC  = float(os.getenv("OPENALEX_RATE_SEC", "0.3"))
# Europe PMC is keyless and public, so the default is deliberately polite. At 25 DOIs
# per boolean query it still clears ~60 DOIs/sec — comparable to the S2 batch tier.
EPMC_RATE_SEC      = float(os.getenv("EPMC_RATE_SEC", "0.4"))
CROSSREF_RATE_SEC  = float(os.getenv("CROSSREF_RATE_SEC",  "0.1"))
S2_RATE_SEC        = float(os.getenv("S2_RATE_SEC",        "0.5"))
SCOPUS_RATE_SEC    = float(os.getenv("SCOPUS_RATE_SEC",    "1.0"))  # Elsevier: ~1 req/sec
# OSF meters anonymous callers per hour and token-bearing ones per day, so the
# polite default suits a keyless run; set OSF_TOKEN and lower this to go faster.
OSF_RATE_SEC       = float(os.getenv("OSF_RATE_SEC",       "1.0"))

# ── Abstract backfill: batch sizes and quota caps ────────────────────────────
# OpenAlex filter= accepts up to 50 pipe-separated ids per call.
OA_BATCH_SIZE   = int(os.getenv("OA_BATCH_SIZE", "50"))
# Europe PMC's search endpoint takes a boolean query, so a batch is
# 'DOI:"a" OR DOI:"b" ...' in one request. The request is a form POST
# (`searchPOST`), so there is no URL-length ceiling: verified live 2026-08-05,
# a 500-DOI query (12.9 kB) answers HTTP 200. 100 is the working size — it keeps
# the requested page (3x the batch, for DOIs matching several records) at 300 rows,
# a third of EPMC's 1000-row page limit, above which the fetcher refuses
# the batch rather than record a truncated page's absences as misses. This is the
# bulk pathway's throughput knob: keyless and unquota'd, so the only cost of a
# bigger batch is a longer single request.
EPMC_BATCH_SIZE = int(os.getenv("EPMC_BATCH_SIZE", "100"))
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
