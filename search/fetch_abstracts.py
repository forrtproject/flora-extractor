"""
fetch_abstracts.py — Fetch missing abstracts for no-abstract rows in candidates.csv.

Strategy (waterfall by identifier type):

  1. OpenAlex batch   — rows with openalex_id_r (305K rows → ~6,100 batch calls)
  2. CrossRef by DOI  — rows still missing after step 1 but with doi_r
  3. Semantic Scholar — fallback for CrossRef misses (requires S2_API_KEY in .env)
  4. Scopus by DOI    — Elsevier Abstract Retrieval API fallback (requires
                        ELSEVIER_API_KEY; ~10k requests/week quota, so a run is
                        capped by --scopus-limit)

Results are cached per identifier in cache/abstracts/. candidates.csv is updated
in-place and flushed every 500 abstract-fills to survive interruption.

Checkpoint (cache/fetch_abstracts_done.txt): one identifier per line (oa:<id as
stored in CSV>, doi:10.x/y, s2:10.x/y, scopus:10.x/y). On restart, already-tried
identifiers are skipped — even those that returned no abstract, so we don't re-hit
the API for known misses.

OpenAlex miss recovery
----------------------
An earlier bug in the OpenAlex batch join keyed the result dict on the full URL
form of openalex_id_r ('https://openalex.org/W123') while the response was matched
on the bare id ('W123'), so no batch row ever matched. Every OpenAlex row was
therefore recorded as a miss and checkpointed as done, permanently suppressing
recovery. The join is now normalised to the bare id on both sides. To re-attempt
rows poisoned by the old bug, run with --retry-openalex-misses: it drops every
'oa:' checkpoint entry whose cached abstract is absent or '__none__' and clears
those poisoned cache files, so the fixed batch phase re-fetches them.

Usage
-----
    python -m search.fetch_abstracts                       # full run
    python -m search.fetch_abstracts --limit 1000          # first 1000 missing rows
    python -m search.fetch_abstracts --reset               # clear checkpoint, start fresh
    python -m search.fetch_abstracts --dry-run             # count missing rows, no API calls
    python -m search.fetch_abstracts --retry-openalex-misses  # re-attempt bug-poisoned OA rows
    python -m search.fetch_abstracts --scopus-limit 9000   # cap Scopus calls (weekly quota)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from shared.config import (
    CACHE_DIR, DATA_DIR, ELSEVIER_API_KEY, OPENALEX_API_KEY, RESEARCHER_EMAIL, log,
)
from shared.utils import clean_doi, cache_key
from shared.dashboard_cache import _parquet_path, refresh as _dc_refresh

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

CANDIDATES_PATH    = DATA_DIR / "candidates.csv"
ABSTRACT_CACHE_DIR = CACHE_DIR / "abstracts"
CHECKPOINT_PATH    = CACHE_DIR / "fetch_abstracts_done.txt"
ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

OA_BATCH_SIZE      = 50    # OpenAlex filter= supports up to 50 pipe-separated IDs
OA_RATE_SEC        = 0.1
CROSSREF_RATE_SEC  = 0.15
S2_RATE_SEC        = 0.5
SCOPUS_RATE_SEC    = 1.0   # Elsevier Scopus: ~1 req/sec
SCOPUS_DEFAULT_LIMIT = 9000  # keep a run under the ~10k/week Scopus quota
FLUSH_EVERY        = 500   # flush candidates.csv every N abstracts found

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": f"FLoRA-Extractor/1.0 (mailto:{RESEARCHER_EMAIL})"})

# OpenAlex auth header — sent only on OA requests, never on CrossRef/S2 calls.
_OA_HEADERS = {"Authorization": f"Bearer {OPENALEX_API_KEY}"} if OPENALEX_API_KEY else {}


# ---------------------------------------------------------------------------
# Abstract cache helpers
# ---------------------------------------------------------------------------

def _cache_path(ident: str) -> Path:
    return ABSTRACT_CACHE_DIR / f"{cache_key(ident)}.json"


def _read_abstract_cache(ident: str) -> Optional[str]:
    p = _cache_path(ident)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("abstract")
        except Exception:
            return None
    return None


def _write_abstract_cache(ident: str, abstract: Optional[str]) -> None:
    _cache_path(ident).write_text(
        json.dumps({"ident": ident, "abstract": abstract}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> set[str]:
    if not CHECKPOINT_PATH.exists():
        return set()
    return {l.strip() for l in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}


def _append_checkpoint(ident: str) -> None:
    with open(CHECKPOINT_PATH, "a", encoding="utf-8") as f:
        f.write(ident + "\n")


# ---------------------------------------------------------------------------
# OpenAlex inverted-index decoder
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[k] for k in sorted(positions)) if positions else None


# ---------------------------------------------------------------------------
# Source 1: OpenAlex batch
# ---------------------------------------------------------------------------

def _fetch_openalex_batch(oa_ids: list[str]) -> dict[str, Optional[str]]:
    """Fetch abstracts for up to OA_BATCH_SIZE OpenAlex IDs in one call.

    *oa_ids* may be full URLs ('https://openalex.org/W123') or bare ids ('W123');
    the openalex_id_r column stores the URL form. The returned dict is keyed by the
    exact strings passed in, so the caller can join results back to its CSV values
    and cache keys. Both the query filter and the response are matched on the bare
    'W…' id — mismatching the two forms is what previously made every row a miss.
    """
    bare_to_input: dict[str, str] = {}
    for oid in oa_ids:
        bare = oid.replace("https://openalex.org/", "").strip()
        bare_to_input[bare] = oid
    pipe_ids = "|".join(bare_to_input.keys())
    url = (
        "https://api.openalex.org/works"
        f"?filter=ids.openalex:{pipe_ids}"
        "&select=id,abstract_inverted_index"
        f"&per-page={OA_BATCH_SIZE}"
    )
    result: dict[str, Optional[str]] = {oid: None for oid in oa_ids}
    try:
        resp = _SESSION.get(url, timeout=30, headers=_OA_HEADERS)
        resp.raise_for_status()
        for work in resp.json().get("results", []):
            wid = work.get("id", "").replace("https://openalex.org/", "").strip()
            abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
            input_key = bare_to_input.get(wid)
            if input_key is not None:
                result[input_key] = abstract
    except Exception as exc:
        log.warning("OpenAlex batch error: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Source 2: CrossRef by DOI
# ---------------------------------------------------------------------------

_JATS_RE = re.compile(r"<[^>]+>")


def _fetch_crossref_abstract(doi: str) -> Optional[str]:
    url = f"https://api.crossref.org/works/{doi}"
    try:
        resp = _SESSION.get(url, timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("abstract", "")
        if raw:
            return _JATS_RE.sub("", raw).strip() or None
    except Exception as exc:
        log.warning("CrossRef error for %s: %s", doi, exc)
    return None


# ---------------------------------------------------------------------------
# Source 3: Semantic Scholar by DOI
# ---------------------------------------------------------------------------

def _fetch_s2_abstract(doi: str, s2_key: str) -> Optional[str]:
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    headers = {"x-api-key": s2_key} if s2_key else {}
    try:
        resp = _SESSION.get(url, timeout=20, headers=headers)
        if resp.status_code in (404, 429):
            return None
        resp.raise_for_status()
        return resp.json().get("abstract") or None
    except Exception as exc:
        log.warning("S2 error for %s: %s", doi, exc)
    return None


# ---------------------------------------------------------------------------
# Source 4: Elsevier Scopus by DOI
# ---------------------------------------------------------------------------

def _parse_scopus_abstract(payload: dict) -> Optional[str]:
    """Pull the abstract out of a Scopus Abstract Retrieval JSON response.

    Abstract text lives at abstracts-retrieval-response → coredata → dc:description.
    """
    coredata = (
        (payload or {})
        .get("abstracts-retrieval-response", {})
        .get("coredata", {})
    )
    desc = coredata.get("dc:description")
    if not desc:
        return None
    return _JATS_RE.sub("", str(desc)).strip() or None


def _fetch_scopus_abstract(doi: str, api_key: str) -> tuple[Optional[str], bool]:
    """Fetch an abstract from Elsevier Scopus by DOI.

    Returns (abstract_or_none, quota_exhausted). On a 429 whose
    X-RateLimit-Remaining header is "0" — or after 3 backed-off retries still
    hitting 429 — the ~10k/week quota is treated as spent and quota_exhausted is
    True so the caller stops the phase gracefully. Transient errors retry 3× with
    1s/2s/4s backoff per repo convention.
    """
    url = f"https://api.elsevier.com/content/abstract/doi/{doi}"
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
    for attempt in range(3):
        try:
            resp = _SESSION.get(url, timeout=20, headers=headers)
        except Exception as exc:
            log.warning("Scopus error for %s: %s", doi, exc)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            if resp.headers.get("X-RateLimit-Remaining", "").strip() == "0":
                return None, True
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (400, 404):
            return None, False
        if resp.status_code >= 400:
            time.sleep(2 ** attempt)
            continue
        return _parse_scopus_abstract(resp.json()), False
    # Exhausted retries still hitting errors/429 — assume the quota is gone.
    return None, True


# ---------------------------------------------------------------------------
# OpenAlex miss recovery (undo the pre-bugfix poisoned checkpoint/cache)
# ---------------------------------------------------------------------------

def _drop_openalex_misses() -> int:
    """Drop OpenAlex-phase miss entries from the checkpoint so they re-run.

    A miss is an 'oa:' checkpoint line whose cached abstract is absent or the
    '__none__' sentinel. Its poisoned cache file is deleted too, so the fixed
    batch phase actually re-fetches it instead of reading the stale miss. Rows
    that genuinely recovered an abstract are kept. Returns the number dropped.
    """
    if not CHECKPOINT_PATH.exists():
        return 0
    lines = [l.strip() for l in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if line.startswith("oa:"):
            val = _read_abstract_cache(line)   # checkpoint line == abstract-cache ident
            if val is None or val == "__none__":
                p = _cache_path(line)
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
                dropped += 1
                continue
        kept.append(line)
    CHECKPOINT_PATH.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return dropped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def enrich_abstracts(df: "pd.DataFrame") -> "pd.DataFrame":
    """Fill missing abstracts in *df* in-place using CrossRef then S2.

    Called by run_search._merge_into_candidates_csv before writing new rows
    so every candidate arrives with the best available abstract.

    Modifies df in-place and returns it. Uses the same cache as the standalone
    fetch_abstracts run command, so results are shared across both code paths.
    """
    import os
    import pandas as pd

    s2_key = os.getenv("S2_API_KEY", "")
    missing_mask = df["abstract_r"].fillna("").str.strip() == ""
    if not missing_mask.any():
        return df

    n_missing = missing_mask.sum()
    log.debug("enrich_abstracts: %d rows have no abstract — trying CrossRef + S2", n_missing)

    n_found = 0
    for idx, row in df[missing_mask].iterrows():
        doi = clean_doi(str(row.get("doi_r", "") or ""))
        if not doi:
            continue

        # CrossRef
        cached = _read_abstract_cache(f"doi:{doi}")
        if cached is None:
            time.sleep(CROSSREF_RATE_SEC)
            cached = _fetch_crossref_abstract(doi)
            _write_abstract_cache(f"doi:{doi}", cached if cached else "__none__")
        abstract = cached if cached and cached != "__none__" else None

        # S2 fallback
        if not abstract and s2_key:
            s2_cached = _read_abstract_cache(f"s2:{doi}")
            if s2_cached is None:
                time.sleep(S2_RATE_SEC)
                s2_cached = _fetch_s2_abstract(doi, s2_key)
                _write_abstract_cache(f"s2:{doi}", s2_cached if s2_cached else "__none__")
            abstract = s2_cached if s2_cached and s2_cached != "__none__" else None

        if abstract:
            df.at[idx, "abstract_r"] = abstract
            n_found += 1

    log.info("enrich_abstracts: recovered %d / %d missing abstracts", n_found, n_missing)
    return df


def _load_scopus_priority(path: Path) -> dict[str, int]:
    """Map cleaned DOI → rank (file line order) from a priority-DOI file.

    Lines are one DOI each; earlier lines get the Scopus quota first. Blank
    lines and '#' comments are skipped.
    """
    ranks: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        doi = clean_doi(line)
        if doi and doi not in ranks:
            ranks[doi] = len(ranks)
    return ranks


def run(dry_run: bool = False, limit: Optional[int] = None,
        scopus_limit: int = SCOPUS_DEFAULT_LIMIT,
        scopus_priority: Optional[Path] = None) -> None:
    import os
    import pandas as pd

    if not CANDIDATES_PATH.exists():
        sys.exit(f"ERROR: {CANDIDATES_PATH} not found.")

    s2_key = os.getenv("S2_API_KEY", "")
    elsevier_key = os.getenv("ELSEVIER_API_KEY", "") or ELSEVIER_API_KEY

    # ------------------------------------------------------------------
    # Load candidates — Parquet if available (faster + less RAM), else CSV
    # ------------------------------------------------------------------
    pq_path = _parquet_path("candidates")
    if pq_path.exists():
        try:
            import pyarrow.parquet as pq
            log.info("Loading from Parquet: %s", pq_path)
            df = pq.read_table(pq_path).to_pandas().fillna("")
        except Exception as exc:
            log.warning("Parquet read failed (%s) — falling back to CSV", exc)
            df = pd.read_csv(CANDIDATES_PATH, dtype=str, encoding="utf-8-sig", low_memory=False).fillna("")
    else:
        log.info("Loading candidates.csv (no Parquet found)...")
        df = pd.read_csv(CANDIDATES_PATH, dtype=str, encoding="utf-8-sig", low_memory=False).fillna("")
    log.info("Loaded %d total rows.", len(df))

    missing_mask = df["abstract_r"].str.strip() == ""
    missing_df   = df[missing_mask].copy()
    log.info("Rows missing abstract: %d", len(missing_df))

    if dry_run:
        has_oa  = (missing_df["openalex_id_r"].str.strip() != "").sum()
        has_doi = (missing_df["doi_r"].str.strip() != "").sum()
        log.info("  with openalex_id_r: %d  (OpenAlex batch)", has_oa)
        log.info("  with doi_r:         %d  (CrossRef / S2 fallback)", has_doi)
        log.info("  neither:            %d  (skipped)", len(missing_df) - has_oa)
        log.info("DRY RUN — no API calls. Re-run without --dry-run to fetch.")
        return

    # ------------------------------------------------------------------
    # Checkpoint — skip already-tried identifiers
    # ------------------------------------------------------------------
    done = _load_checkpoint()
    if done:
        log.info("Checkpoint: %d identifiers already tried — skipping.", len(done))

    # ------------------------------------------------------------------
    # Apply limit after checkpoint exclusion
    # ------------------------------------------------------------------
    if limit:
        missing_df = missing_df.head(limit)
        log.info("--limit %d: processing first %d missing rows.", limit, len(missing_df))

    # ------------------------------------------------------------------
    # Phase 1: OpenAlex batch (rows with openalex_id_r)
    # ------------------------------------------------------------------
    oa_rows = missing_df[missing_df["openalex_id_r"].str.strip() != ""].copy()
    # .astype(bool) throughout: .apply() on an EMPTY frame yields an object-dtype
    # Series, and indexing with an empty non-bool Series is treated by pandas as
    # column selection → KeyError when a phase's queue is empty.
    oa_rows = oa_rows[~oa_rows["openalex_id_r"].apply(lambda x: f"oa:{x}" in done).astype(bool)]
    log.info("Phase 1 — OpenAlex batch: %d rows to try.", len(oa_rows))

    n_found = 0
    n_flushed = 0

    oa_ids   = oa_rows["openalex_id_r"].str.strip().tolist()
    oa_idx   = oa_rows.index.tolist()

    for batch_start in range(0, len(oa_ids), OA_BATCH_SIZE):
        batch_ids  = oa_ids[batch_start : batch_start + OA_BATCH_SIZE]
        batch_idxs = oa_idx[batch_start : batch_start + OA_BATCH_SIZE]

        # Check cache first — skip call if all cached
        results: dict[str, Optional[str]] = {}
        uncached_ids: list[str] = []
        for oid in batch_ids:
            cached = _read_abstract_cache(f"oa:{oid}")
            if cached is not None:
                results[oid] = cached if cached != "__none__" else None
            else:
                uncached_ids.append(oid)

        if uncached_ids:
            time.sleep(OA_RATE_SEC)
            fetched = _fetch_openalex_batch(uncached_ids)
            for oid, abstract in fetched.items():
                _write_abstract_cache(f"oa:{oid}", abstract if abstract else "__none__")
                results[oid] = abstract

        # Write abstracts back to df
        for oid, idx in zip(batch_ids, batch_idxs):
            abstract = results.get(oid)
            _append_checkpoint(f"oa:{oid}")
            if abstract:
                df.at[idx, "abstract_r"] = abstract
                n_found += 1
                if n_found - n_flushed >= FLUSH_EVERY:
                    df.to_csv(CANDIDATES_PATH, index=False, encoding="utf-8-sig")
                    log.info("  Flushed candidates.csv (Phase 1, %d abstracts found so far)", n_found)
                    n_flushed = n_found

        done_so_far = batch_start + len(batch_ids)
        if done_so_far % 5000 == 0:
            log.info("  OpenAlex progress: %d / %d  (found: %d)", done_so_far, len(oa_ids), n_found)

    log.info("Phase 1 complete. Abstracts found: %d", n_found)

    # ------------------------------------------------------------------
    # Phase 2: CrossRef by DOI (rows still missing after Phase 1)
    # ------------------------------------------------------------------
    # Refresh missing mask after Phase 1 updates
    still_missing = df["abstract_r"].str.strip() == ""
    crossref_rows = df[still_missing & (df["doi_r"].str.strip() != "")].copy()
    crossref_rows = crossref_rows[~crossref_rows["doi_r"].apply(lambda x: f"doi:{clean_doi(x)}" in done).astype(bool)]
    log.info("Phase 2 — CrossRef: %d rows to try.", len(crossref_rows))

    phase2_found = 0
    for i, (idx, row) in enumerate(crossref_rows.iterrows(), 1):
        doi = clean_doi(str(row.get("doi_r", "") or ""))
        if not doi:
            continue

        cached = _read_abstract_cache(f"doi:{doi}")
        if cached is not None:
            abstract = cached if cached != "__none__" else None
        else:
            time.sleep(CROSSREF_RATE_SEC)
            abstract = _fetch_crossref_abstract(doi)
            _write_abstract_cache(f"doi:{doi}", abstract if abstract else "__none__")

        _append_checkpoint(f"doi:{doi}")

        if abstract:
            df.at[idx, "abstract_r"] = abstract
            phase2_found += 1
            n_found += 1
            if n_found - n_flushed >= FLUSH_EVERY:
                df.to_csv(CANDIDATES_PATH, index=False, encoding="utf-8-sig")
                log.info("  Flushed candidates.csv (Phase 2, %d total found)", n_found)
                n_flushed = n_found

        if i % 2000 == 0:
            log.info("  CrossRef progress: %d / %d  (found: %d)", i, len(crossref_rows), phase2_found)

    log.info("Phase 2 complete. Abstracts found: %d", phase2_found)

    # ------------------------------------------------------------------
    # Phase 3: Semantic Scholar (fallback for remaining CrossRef misses)
    # ------------------------------------------------------------------
    if not s2_key:
        log.info("Phase 3 — S2: skipped (S2_API_KEY not set in .env).")
    else:
        still_missing2 = df["abstract_r"].str.strip() == ""
        s2_rows = df[still_missing2 & (df["doi_r"].str.strip() != "")].copy()
        s2_rows = s2_rows[~s2_rows["doi_r"].apply(lambda x: f"s2:{clean_doi(x)}" in done).astype(bool)]
        log.info("Phase 3 — Semantic Scholar: %d rows to try.", len(s2_rows))

        phase3_found = 0
        for i, (idx, row) in enumerate(s2_rows.iterrows(), 1):
            doi = clean_doi(str(row.get("doi_r", "") or ""))
            if not doi:
                continue

            cached = _read_abstract_cache(f"s2:{doi}")
            if cached is not None:
                abstract = cached if cached != "__none__" else None
            else:
                time.sleep(S2_RATE_SEC)
                abstract = _fetch_s2_abstract(doi, s2_key)
                _write_abstract_cache(f"s2:{doi}", abstract if abstract else "__none__")

            _append_checkpoint(f"s2:{doi}")

            if abstract:
                df.at[idx, "abstract_r"] = abstract
                phase3_found += 1
                n_found += 1
                if n_found - n_flushed >= FLUSH_EVERY:
                    df.to_csv(CANDIDATES_PATH, index=False, encoding="utf-8-sig")
                    log.info("  Flushed candidates.csv (Phase 3, %d total found)", n_found)
                    n_flushed = n_found

            if i % 2000 == 0:
                log.info("  S2 progress: %d / %d  (found: %d)", i, len(s2_rows), phase3_found)

        log.info("Phase 3 complete. Abstracts found: %d", phase3_found)

    # ------------------------------------------------------------------
    # Phase 4: Elsevier Scopus (fallback for rows still missing a DOI abstract)
    # ------------------------------------------------------------------
    if not elsevier_key:
        log.info("Phase 4 — Scopus: skipped (ELSEVIER_API_KEY not set in .env).")
    else:
        still_missing3 = df["abstract_r"].str.strip() == ""
        scopus_rows = df[still_missing3 & (df["doi_r"].str.strip() != "")].copy()
        scopus_rows = scopus_rows[~scopus_rows["doi_r"].apply(lambda x: f"scopus:{clean_doi(x)}" in done).astype(bool)]
        if scopus_priority is not None:
            # The weekly quota (~10k) is far smaller than the missing-abstract pool,
            # so which rows get it matters. DOIs in the priority file (in file order)
            # are tried first; everything else keeps CSV order after them.
            ranks = _load_scopus_priority(scopus_priority)
            order = scopus_rows["doi_r"].map(lambda x: ranks.get(clean_doi(str(x)), len(ranks)))
            scopus_rows = scopus_rows.iloc[order.argsort(kind="stable")]
            log.info("Phase 4 — Scopus priority: %d DOIs in %s, %d matched in queue.",
                     len(ranks), scopus_priority, int((order < len(ranks)).sum()))
        if scopus_limit and scopus_limit > 0:
            scopus_rows = scopus_rows.head(scopus_limit)
        log.info("Phase 4 — Scopus: %d rows to try (weekly-quota cap: %s).",
                 len(scopus_rows), scopus_limit)

        phase4_found = 0
        for i, (idx, row) in enumerate(scopus_rows.iterrows(), 1):
            doi = clean_doi(str(row.get("doi_r", "") or ""))
            if not doi:
                continue

            cached = _read_abstract_cache(f"scopus:{doi}")
            if cached is not None:
                abstract = cached if cached != "__none__" else None
            else:
                time.sleep(SCOPUS_RATE_SEC)
                abstract, quota_exhausted = _fetch_scopus_abstract(doi, elsevier_key)
                if quota_exhausted:
                    log.warning("Phase 4 — Scopus quota exhausted; stopping phase "
                                "(%d rows done, %d found).", i - 1, phase4_found)
                    break
                _write_abstract_cache(f"scopus:{doi}", abstract if abstract else "__none__")

            _append_checkpoint(f"scopus:{doi}")

            if abstract:
                df.at[idx, "abstract_r"] = abstract
                phase4_found += 1
                n_found += 1
                if n_found - n_flushed >= FLUSH_EVERY:
                    df.to_csv(CANDIDATES_PATH, index=False, encoding="utf-8-sig")
                    log.info("  Flushed candidates.csv (Phase 4, %d total found)", n_found)
                    n_flushed = n_found

            if i % 500 == 0:
                log.info("  Scopus progress: %d / %d  (found: %d)", i, len(scopus_rows), phase4_found)

        log.info("Phase 4 complete. Abstracts found: %d", phase4_found)

    # Final flush to CSV, then rebuild Parquet mirror
    df.to_csv(CANDIDATES_PATH, index=False, encoding="utf-8-sig")
    _dc_refresh("candidates")
    still_missing_final = (df["abstract_r"].str.strip() == "").sum()

    log.info("=" * 60)
    log.info("FETCH ABSTRACTS COMPLETE")
    log.info("=" * 60)
    log.info("Abstracts recovered:  %d", n_found)
    log.info("Still missing:        %d", still_missing_final)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch missing abstracts for candidates.csv. Resumable."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Count missing rows by identifier type — no API calls.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only the first N missing rows (testing).")
    parser.add_argument("--reset", action="store_true",
                        help="Clear the checkpoint and start fresh.")
    parser.add_argument("--retry-openalex-misses", action="store_true",
                        help="Drop OpenAlex-phase miss entries (and their poisoned "
                             "cache files) from the checkpoint so the fixed batch "
                             "phase re-attempts them, then continue the run.")
    parser.add_argument("--scopus-limit", type=int, default=SCOPUS_DEFAULT_LIMIT, metavar="N",
                        help="Max Scopus calls this run (weekly quota ~10k; "
                             f"default {SCOPUS_DEFAULT_LIMIT}). 0 disables the cap.")
    parser.add_argument("--scopus-priority", type=Path, default=None, metavar="FILE",
                        help="File of DOIs (one per line, # comments allowed) that get "
                             "the Scopus quota first, in file order. Rows not listed "
                             "follow in CSV order.")
    args = parser.parse_args()

    if args.reset:
        if CHECKPOINT_PATH.exists():
            CHECKPOINT_PATH.unlink()
            print(f"Checkpoint cleared: {CHECKPOINT_PATH}")
        sys.exit(0)

    if args.retry_openalex_misses:
        n = _drop_openalex_misses()
        print(f"Dropped {n} OpenAlex miss entries from checkpoint — they will be retried.")

    run(dry_run=args.dry_run, limit=args.limit, scopus_limit=args.scopus_limit,
        scopus_priority=args.scopus_priority)
