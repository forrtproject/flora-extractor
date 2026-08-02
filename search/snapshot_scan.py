"""
Stage 1 source: the OpenAlex bulk-parquet snapshot, scanned end to end.

The API path (``search/openalex_search.py``) can only find works whose title or
abstract matches a phrase we thought to write down. The snapshot is the whole
corpus, so this scanner reads every work once and decides locally what to keep.
It is an ADDITIVE source — rows land in ``data/candidates.csv`` with
``source = "openalex_snapshot"`` through the same merge/dedup path as every
other source, and nothing existing is removed or re-scored.

Two admission stages, both recall-oriented:

Stage A (vectorized, pyarrow)  a broad token regex over title and the RAW
    abstract inverted-index JSON, OR membership of a replication concept.
Stage B (per row, Python)      a precise ``REPLICATION_PHRASES`` match on
    title + reconstructed abstract, OR a token hit in the title alone.
    Concept hits bypass Stage B entirely — that is the recall arm, mirroring
    what the ``openalex_concept`` API source does today.

Stage B is a POSITIVE match only, NOT Stage 2's precision semantics: no
exclusion patterns, no phrase guards. Stage 1 must never reject a paper; that
is Stage 2's and Stage 3's job.

Progress is checkpointed per manifest file in ``cache/snapshot/ledger.json`` so
an interrupted 400+ GB scan resumes where it stopped.

Usage (via Stage 1's orchestrator, explicit opt-in):
    python -m search.run_search --source openalex_snapshot
    python -m search.run_search --snapshot-pilot data/snapshot_pilot.csv --snapshot-max-files 3
"""

import datetime
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests

from shared.config import (
    DATA_DIR,
    RESEARCHER_EMAIL,
    SNAPSHOT_BASE_URL,
    SNAPSHOT_BATCH_ROWS,
    SNAPSHOT_CACHE_DIR,
    SNAPSHOT_HTTP_RETRIES,
    SNAPSHOT_HTTP_TIMEOUT,
    log,
)
from shared.row_key import row_keys
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi
from search.openalex_search import CONCEPT_IDS, _build_ref, _reconstruct_abstract
from filter.phrase_detection import REPLICATION_PHRASES

SOURCE_TAG_SNAPSHOT = "openalex_snapshot"

# Stage A. Deliberately loose stems, not phrases — see _gate_mask for why this
# runs against the raw inverted-index JSON and therefore cannot use phrases.
_TOKEN_GATE = r"(?i)replicat|replicab|reproduc|reanalys|re-analys"
_TOKEN_RE = re.compile(_TOKEN_GATE)

_SCAN_COLUMNS = ["id", "doi", "title", "display_name", "publication_year",
                 "authorships", "primary_location", "open_access", "concepts",
                 "abstract_inverted_index"]

# Bumped whenever _admit's rule changes, so the ledger's gate_fingerprint moves with it.
_ADMISSION_RULE_VERSION = "v1: precise-phrase(title+abstract) OR token(title); concepts bypass"

_CONCEPT_IDS_BARE = {c.replace("https://openalex.org/", "").strip() for c in CONCEPT_IDS}

_MANIFEST_PATH = SNAPSHOT_CACHE_DIR / "manifest.json"
_LEDGER_PATH = SNAPSHOT_CACHE_DIR / "ledger.json"

_S3_PREFIX = "s3://openalex/"
_HTTPS_PREFIX = "https://openalex.s3.amazonaws.com/"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _https_url(url: str) -> str:
    """Map a manifest ``s3://openalex/...`` URL to its public HTTPS equivalent."""
    if url.startswith(_S3_PREFIX):
        return _HTTPS_PREFIX + url[len(_S3_PREFIX):]
    return url


def fetch_manifest(refresh: bool = False) -> dict:
    """Return the works manifest, cached at ``cache/snapshot/manifest.json``.

    URLs are rewritten to their HTTPS form on the way in, so every consumer sees
    one address scheme. A manifest without a file list raises rather than
    yielding zero files: an empty scan that reports success would silently look
    like "the snapshot is fully consumed".
    """
    if _MANIFEST_PATH.exists() and not refresh:
        with open(_MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)

    url = f"{SNAPSHOT_BASE_URL.rstrip('/')}/works/manifest.json"
    for attempt in range(SNAPSHOT_HTTP_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": f"flora-extractor ({RESEARCHER_EMAIL})"},
                                timeout=SNAPSHOT_HTTP_TIMEOUT)
            resp.raise_for_status()
            manifest = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 — transport-level, retried then raised
            if attempt == SNAPSHOT_HTTP_RETRIES - 1:
                raise
            wait = 2 ** attempt
            log.warning("Snapshot manifest fetch failed (%s) — retry %d/%d in %ds",
                        exc, attempt + 1, SNAPSHOT_HTTP_RETRIES, wait)
            time.sleep(wait)

    key = "entries" if isinstance(manifest.get("entries"), list) else "files"
    if not isinstance(manifest.get(key), list):
        raise ValueError(f"Malformed OpenAlex manifest at {url}: no files/entries list")
    for entry in manifest[key]:
        if isinstance(entry, dict) and entry.get("url"):
            entry["url"] = _https_url(entry["url"])

    with open(_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return manifest


def _manifest_files(manifest: dict) -> list[tuple[str, dict]]:
    """Normalise the manifest into ``[(url, {content_length, record_count}), ...]``.

    OpenAlex nests the per-file stats under ``meta``; older/hand-written manifests
    put them flat on the entry. Both are accepted.
    """
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("Malformed OpenAlex manifest: no files/entries list")

    out: list[tuple[str, dict]] = []
    for entry in entries:
        if isinstance(entry, str):
            out.append((_https_url(entry), {}))
            continue
        url = entry.get("url")
        if not url:
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else entry
        out.append((_https_url(url), {
            "content_length": meta.get("content_length"),
            "record_count": meta.get("record_count"),
        }))
    return out


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def gate_fingerprint() -> str:
    """Hash of everything that decides admission, stored in the ledger.

    A file marked done under one gate has not been scanned under another; the
    fingerprint is what makes that visible instead of silently under-collecting.
    """
    parts = [
        _TOKEN_GATE,
        "|".join(sorted(p.pattern for p in REPLICATION_PHRASES)),
        "|".join(sorted(_CONCEPT_IDS_BARE)),
        _ADMISSION_RULE_VERSION,
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def load_ledger() -> dict:
    """Load the scan ledger, or a fresh one when absent or corrupt."""
    if _LEDGER_PATH.exists():
        try:
            with open(_LEDGER_PATH, encoding="utf-8") as f:
                ledger = json.load(f)
            ledger.setdefault("files", {})
            return ledger
        except Exception:
            log.warning("Corrupt snapshot ledger at %s — starting fresh", _LEDGER_PATH)
    return {"snapshot_date": "", "gate_fingerprint": gate_fingerprint(), "files": {}}


def save_ledger(ledger: dict) -> None:
    """Atomically persist the ledger (tmp file then replace)."""
    tmp = _LEDGER_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=1)
    tmp.replace(_LEDGER_PATH)


def _needs_scan(url: str, meta: dict, ledger: dict) -> bool:
    """True when *url* has not been fully consumed under the current manifest.

    A file is rescanned when it is unknown, when it was left mid-merge (crash),
    or when the manifest's ``content_length`` no longer matches what was scanned
    — OpenAlex rewrites partitions in place, so a changed size means new records.
    """
    entry = ledger.get("files", {}).get(url)
    if not entry:
        return True
    if entry.get("status") != "done":
        return True
    old, new = entry.get("content_length"), meta.get("content_length")
    return old is not None and new is not None and old != new


# ---------------------------------------------------------------------------
# Stage A — vectorized gate
# ---------------------------------------------------------------------------


def _column(batch: pa.RecordBatch, name: str) -> pa.Array:
    """Column *name*, or an all-null string column when the partition lacks it."""
    if name in batch.schema.names:
        return batch.column(name)
    return pa.nulls(batch.num_rows, type=pa.string())


def _as_string(arr: pa.Array) -> pa.Array:
    """Best-effort view of *arr* as strings, JSON-encoding any non-string type."""
    if pa.types.is_string(arr.type) or pa.types.is_large_string(arr.type):
        return arr
    return pa.array([None if v is None else json.dumps(v) for v in arr.to_pylist()],
                    type=pa.string())


def _gate_mask(batch: pa.RecordBatch) -> pa.Array:
    """Stage A token mask: a replication stem in the title or anywhere in the abstract.

    The abstract test runs on the RAW ``abstract_inverted_index`` JSON string, on
    purpose. An inverted index is a {word: [positions]} dictionary whose key order
    is arbitrary, so adjacent words are NOT adjacent in the JSON: a phrase regex
    over this text would match or miss by accident. Only single-token tests are
    sound here. Phrases are Stage B's job, after _reconstruct_abstract() has put
    the words back in order. Do not "optimise" this into a phrase match.
    """
    title = pc.coalesce(_as_string(_column(batch, "display_name")),
                        _as_string(_column(batch, "title")))
    abstract_json = _as_string(_column(batch, "abstract_inverted_index"))

    title_hit = pc.fill_null(pc.match_substring_regex(title, _TOKEN_GATE), False)
    abstract_hit = pc.fill_null(pc.match_substring_regex(abstract_json, _TOKEN_GATE), False)
    return pc.or_(title_hit, abstract_hit)


def _concept_mask(batch: pa.RecordBatch) -> pa.Array:
    """Stage A concept mask: the work carries one of ``CONCEPT_IDS``.

    Snapshot concept ids are URL-form (``https://openalex.org/C12590798``) while
    ``CONCEPT_IDS`` holds bare ids, so ids are stripped before comparison.
    """
    if "concepts" not in batch.schema.names:
        return pa.array(np.zeros(batch.num_rows, dtype=bool))

    col = batch.column("concepts")
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
        # Some snapshot builds ship concepts as a JSON string rather than a list
        # of structs; an id substring test is exact enough for opaque OpenAlex ids.
        pattern = "|".join(re.escape(c) for c in sorted(_CONCEPT_IDS_BARE))
        return pc.fill_null(pc.match_substring_regex(col, pattern), False)

    if not pa.types.is_list(col.type) and not pa.types.is_large_list(col.type):
        return pa.array(np.zeros(batch.num_rows, dtype=bool))

    flat = pc.list_flatten(col)
    if len(flat) == 0:
        return pa.array(np.zeros(batch.num_rows, dtype=bool))
    ids = flat if not pa.types.is_struct(flat.type) else pc.struct_field(flat, "id")
    bare = pc.replace_substring(_as_string(ids), "https://openalex.org/", "")
    hits = pc.fill_null(pc.is_in(bare, value_set=pa.array(sorted(_CONCEPT_IDS_BARE))), False)

    mask = np.zeros(batch.num_rows, dtype=bool)
    parents = pc.list_parent_indices(col)
    mask[np.asarray(pc.filter(parents, hits))] = True
    return pa.array(mask)


# ---------------------------------------------------------------------------
# Stage B — per-row admission
# ---------------------------------------------------------------------------


def _precise_hit(text: str) -> bool:
    """A ``REPLICATION_PHRASES`` match on *text*.

    Positive match only: exclusion patterns and phrase guards are deliberately not
    applied, because Stage 1 discovers and never rejects.
    """
    return any(p.search(text) for p in REPLICATION_PHRASES)


def _admit(concept_hit: bool, title: str, abstract: str) -> bool:
    """Stage B: keep the row? Concept hits are admitted unconditionally."""
    if concept_hit:
        return True
    return _precise_hit(f"{title} {abstract}") or bool(_TOKEN_RE.search(title))


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def _maybe_json(value: "object") -> "object":
    """Parse *value* when the snapshot ships a nested field as a JSON string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _row_from_snapshot(rec: dict) -> dict:
    """Convert one snapshot record into the shared candidate-row schema.

    Field-for-field the same mapping as ``_extract_row()`` on the API JSON, except
    that nested fields may arrive as JSON strings and ``source`` names the snapshot.
    """
    authorships = _maybe_json(rec.get("authorships")) or []
    names = [((a or {}).get("author") or {}).get("display_name") for a in authorships]
    authors = "; ".join(n for n in names if n) or None

    location = _maybe_json(rec.get("primary_location")) or {}
    source = location.get("source") or {}
    open_access = _maybe_json(rec.get("open_access")) or {}
    journal = source.get("display_name")
    year = rec.get("publication_year")

    return {
        "doi_r":         clean_doi(rec.get("doi") or ""),
        "title_r":       rec.get("display_name") or rec.get("title"),
        "abstract_r":    _reconstruct_abstract(_maybe_json(rec.get("abstract_inverted_index"))),
        "year_r":        year,
        "authors_r":     authors,
        "journal_r":     journal,
        "url_r":         open_access.get("oa_url") or location.get("landing_page_url"),
        "openalex_id_r": rec.get("id"),
        "source":        SOURCE_TAG_SNAPSHOT,
        "ref_r":         _build_ref(authors, year, journal),
    }


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _open_parquet(url: str) -> pq.ParquetFile:
    """Open *url* for column-projected HTTP range reads (fsspec imported lazily)."""
    import fsspec  # heavy, pipeline-only: read-only deployments never install it

    return pq.ParquetFile(fsspec.open(url, "rb").open())


def _pilot_keys(pilot_csv: Path) -> set[str]:
    """Every row key already in the pilot CSV — pilot mode's whole dedup state."""
    keys: set[str] = set()
    if not pilot_csv.exists():
        return keys
    for chunk in pd.read_csv(pilot_csv, encoding="utf-8-sig", dtype=str,
                             chunksize=50_000, low_memory=False):
        for row in chunk.fillna("").to_dict("records"):
            keys.update(k for k in row_keys(row) if k)
    return keys


def _write_pilot(df: pd.DataFrame, pilot_csv: Path) -> None:
    """Append *df* to the pilot CSV (utf-8-sig on creation, plain utf-8 after)."""
    if pilot_csv.exists():
        df.to_csv(pilot_csv, mode="a", index=False, encoding="utf-8", header=False)
    else:
        pilot_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(pilot_csv, mode="w", index=False, encoding="utf-8-sig")


def _recover_stale_index(candidates_path: Path, ledger: dict) -> None:
    """Rebuild the candidates index if a previous run died mid-merge.

    ``_merge_into_candidates_csv`` appends to the CSV and THEN to the index, so a
    crash between the two leaves an index that is non-empty (and therefore trusted
    by ``load_or_build``) yet missing the rows just written. Rescanning that file
    would duplicate them. A file left at ``status: merging`` is exactly that
    signal, so the index is rebuilt from the CSV once before anything is rescanned.
    """
    if not any(e.get("status") == "merging" for e in ledger.get("files", {}).values()):
        return
    log.warning("Snapshot ledger has a file left mid-merge — rebuilding candidates index "
                "from %s before rescanning", candidates_path.name)
    try:
        from search.run_search import CANDIDATES_INDEX

        if candidates_path.exists():
            CANDIDATES_INDEX.build(candidates_path)
    except Exception as exc:  # noqa: BLE001 — recovery is best-effort, never fatal
        log.warning("Could not rebuild candidates index (%s) — rescan may duplicate rows", exc)


class _MergeFailed(Exception):
    """A local write (merge_fn or the pilot CSV) failed — never a retryable read error.

    Retrying a merge is unsafe: ``_merge_into_candidates_csv`` appends to the CSV
    before the index, so a second attempt would run against a stale index and
    duplicate rows. This wrapper carries such failures past the read-retry handler
    so they propagate out of ``scan_snapshot`` with the ledger left at "merging".
    """


def _batch_rows(batch: pa.RecordBatch, from_year: Optional[int], to_year: Optional[int],
                counters: dict) -> list[dict]:
    """Gate one batch and return the admitted candidate rows, updating *counters*."""
    counters["scanned"] += batch.num_rows

    token = _gate_mask(batch)
    concept = _concept_mask(batch)
    survivors = pc.or_(token, concept)
    counters["stage_a_token"] += int(pc.sum(token).as_py() or 0)
    counters["stage_a_concept"] += int(pc.sum(concept).as_py() or 0)

    n_survivors = int(pc.sum(survivors).as_py() or 0)
    if not n_survivors:
        return []
    counters["stage_a"] += n_survivors

    kept = batch.filter(survivors)
    concept_flags = pc.filter(concept, survivors).to_pylist()

    rows: list[dict] = []
    for rec, concept_hit in zip(kept.to_pylist(), concept_flags):
        year = rec.get("publication_year")
        if year is not None:
            if from_year is not None and year < from_year:
                continue
            if to_year is not None and year > to_year:
                continue
        row = _row_from_snapshot(rec)
        title = row["title_r"] or ""
        abstract = row["abstract_r"] or ""
        precise = _precise_hit(f"{title} {abstract}")
        title_token = bool(_TOKEN_RE.search(title))
        counters["precise"] += int(precise)
        counters["default_rule"] += int(precise or title_token)
        if not (concept_hit or precise or title_token):
            continue
        counters["admitted"] += 1
        counters["no_abstract"] += int(not abstract)
        rows.append(row)
    return rows


def scan_snapshot(max_files: Optional[int] = None,
                  from_year: Optional[int] = None,
                  to_year: Optional[int] = None,
                  pilot_csv: Optional[Path] = None,
                  files: Optional[list[str]] = None,
                  merge_fn: Optional[Callable] = None,
                  index_loader: Optional[Callable] = None) -> int:
    """Scan OpenAlex snapshot partitions and merge admitted rows into candidates.csv.

    Production mode (``pilot_csv=None``) is ledger-backed and full-corpus: it scans
    every manifest file not already marked done, merging each batch of survivors
    straight into ``data/candidates.csv`` with enrichment bypassed (snapshot rows
    already carry OpenAlex's own abstract; the blanks are exactly the population
    OpenAlex never had, and are backfilled later by
    ``python -m search.fetch_abstracts --skip-openalex``).

    Pilot mode (``pilot_csv`` set) writes to that CSV instead, keeps NO ledger,
    dedupes in memory against the pilot CSV, and prints a gate report. It is the
    only mode where *from_year*/*to_year* apply — a production file marked done
    under a narrow year filter would be an unsound checkpoint, so year bounds are
    ignored (with a warning) outside pilot mode.

    *files* pins an explicit list of partition URLs (used by pilot runs and the
    live test) and skips the manifest fetch entirely; otherwise the manifest order
    is followed, capped by *max_files*.

    A partition that cannot be READ is retried and then skipped, but a failure of
    the merge or of the pilot write propagates out of this function immediately —
    it leaves local state half-written, which is not something a retry can repair.
    *merge_fn* / *index_loader* are injected by ``run_search`` so the two modules
    do not import each other at module level; when absent they are imported lazily.

    Returns the number of rows actually merged (0 in pilot mode's terms is the
    number of rows appended to the pilot CSV).
    """
    if pilot_csv is None and (from_year is not None or to_year is not None):
        log.warning("Snapshot production scan ignores --from-year/--to-year: the ledger "
                    "records whole files as done, so a year-filtered scan cannot be "
                    "checkpointed. Use --snapshot-pilot for year-bounded exploration.")
        from_year = to_year = None

    # An explicit file list is self-sufficient: fetching the manifest for it would be
    # a network call the caller did not ask for (and the unit tests never allow one).
    manifest: dict = {}
    all_files: list[tuple[str, dict]] = []
    if files is None:
        manifest = fetch_manifest()
        all_files = _manifest_files(manifest)
    source_files: list[tuple[str, dict]] = [(u, {}) for u in files] if files is not None \
        else all_files
    n_available = len(files) if files is not None else len(all_files)

    candidates_path = DATA_DIR / "candidates.csv"
    ledger: dict = {}

    if pilot_csv is not None:
        targets = source_files
        if max_files is not None:
            targets = targets[:max_files]
        seen_keys = _pilot_keys(pilot_csv)
        if index_loader is None:
            from search.run_search import _load_or_build_candidates_index as index_loader
        prod_index = index_loader(candidates_path)
    else:
        if merge_fn is None:
            from search.run_search import _merge_into_candidates_csv as merge_fn
        ledger = load_ledger()
        if ledger.get("gate_fingerprint") not in (None, gate_fingerprint()):
            log.warning("Snapshot ledger was written under a DIFFERENT gate — files already "
                        "marked done were not scanned with the current phrases/concepts. "
                        "Delete %s to force a full rescan.", _LEDGER_PATH)
        _recover_stale_index(candidates_path, ledger)
        if files is None:
            ledger["snapshot_date"] = (manifest.get("meta") or {}).get("updated_date", "") \
                or ledger.get("snapshot_date", "")
        targets = [(u, m) for u, m in source_files if _needs_scan(u, m, ledger)]
        if max_files is not None:
            targets = targets[:max_files]
        seen_keys = set()
        prod_index = set()

    log.info("Snapshot scan: %d of %d manifest files to read%s",
             len(targets), n_available, " (pilot)" if pilot_csv is not None else "")

    counters = {"scanned": 0, "stage_a": 0, "stage_a_token": 0, "stage_a_concept": 0,
                "precise": 0, "default_rule": 0, "admitted": 0, "no_abstract": 0,
                "already_in_candidates": 0}
    total_merged = 0
    skipped: list[str] = []

    for i, (url, meta) in enumerate(targets, 1):
        if pilot_csv is None:
            ledger["files"][url] = {**meta, "status": "merging"}
            # Only ever set on a fresh ledger: overwriting a mismatching fingerprint
            # would silence the warning above from the first file scanned onwards,
            # while most files on record were still scanned under the old gate.
            ledger.setdefault("gate_fingerprint", gate_fingerprint())
            save_ledger(ledger)

        file_merged = 0
        for attempt in range(SNAPSHOT_HTTP_RETRIES):
            try:
                pf = _open_parquet(url)
                columns = [c for c in _SCAN_COLUMNS if c in pf.schema_arrow.names]
                for batch in pf.iter_batches(batch_size=SNAPSHOT_BATCH_ROWS, columns=columns):
                    rows = _batch_rows(batch, from_year, to_year, counters)
                    if not rows:
                        continue
                    df = pd.DataFrame(rows, columns=CANDIDATES_COLS)
                    if pilot_csv is not None:
                        fresh = []
                        for row in rows:
                            keys = [k for k in row_keys(row) if k]
                            counters["already_in_candidates"] += int(
                                any(k in prod_index for k in keys))
                            if keys and any(k in seen_keys for k in keys):
                                continue
                            seen_keys.update(keys)
                            fresh.append(row)
                        if fresh:
                            try:
                                _write_pilot(pd.DataFrame(fresh, columns=CANDIDATES_COLS),
                                             pilot_csv)
                            except Exception as exc:
                                raise _MergeFailed(url) from exc
                            file_merged += len(fresh)
                    else:
                        try:
                            merged = merge_fn(df, candidates_path, enrich=False)
                        except Exception as exc:
                            raise _MergeFailed(url) from exc
                        file_merged += int(merged or 0)
                break
            except _MergeFailed as failure:
                # Out of the retry loop untouched, and with the ledger entry left at
                # "merging" so the next run rebuilds the index before rescanning.
                raise failure.__cause__ from None
            except Exception as exc:  # noqa: BLE001 — any read failure is retried, then skipped
                if attempt == SNAPSHOT_HTTP_RETRIES - 1:
                    log.error("Snapshot file failed after %d attempts — skipping %s (%s)",
                              SNAPSHOT_HTTP_RETRIES, url, exc)
                    skipped.append(url)
                    if pilot_csv is None:
                        # Never leave a skipped file in the ledger: it was not consumed,
                        # and "merging" would trigger an index rebuild on every later run.
                        ledger["files"].pop(url, None)
                        save_ledger(ledger)
                    break
                wait = 2 ** attempt
                log.warning("Snapshot read error on %s (%s) — retry %d/%d in %ds",
                            url, exc, attempt + 1, SNAPSHOT_HTTP_RETRIES, wait)
                time.sleep(wait)

        total_merged += file_merged
        if pilot_csv is None and url not in skipped:
            ledger["files"][url] = {**meta, "status": "done", "kept": file_merged,
                                    "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
            save_ledger(ledger)
        log.info("Snapshot %d/%d  %s  merged=%d  (running total %d)",
                 i, len(targets), "/".join(url.split("/")[-2:]), file_merged, total_merged)

    if skipped:
        log.warning("Snapshot scan finished with %d unreadable file(s), left unscanned:\n%s",
                    len(skipped), "\n".join(skipped))

    if pilot_csv is not None:
        _print_pilot_report(counters, total_merged, len(targets), pilot_csv)

    return total_merged


def _print_pilot_report(counters: dict, written: int, n_files: int, pilot_csv: Path) -> None:
    """Print the Phase 0 numbers the admission rule is meant to be judged on."""
    print(f"\n=== Snapshot pilot report ({n_files} file(s) -> {pilot_csv}) ===")
    print(f"  rows scanned                          {counters['scanned']:,}")
    print(f"  Stage A survivors                     {counters['stage_a']:,}"
          f"  (token {counters['stage_a_token']:,}, concept {counters['stage_a_concept']:,})")
    print(f"  (i)   Stage A alone                   {counters['stage_a']:,}")
    print(f"  (ii)  Stage A + precise phrase        {counters['precise']:,}")
    print(f"  (iii) default rule (precise|title)    {counters['default_rule']:,}")
    print(f"  admitted (iii or concept)             {counters['admitted']:,}")
    print(f"  admitted with no abstract             {counters['no_abstract']:,}")
    print(f"  admitted already in candidates.csv    {counters['already_in_candidates']:,}")
    print(f"  rows written to pilot CSV             {written:,}\n")
