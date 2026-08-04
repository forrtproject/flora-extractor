"""
Stage 1 source: the OpenAlex bulk-parquet snapshot, scanned end to end.

A phrase search can only find works whose title or abstract matches a phrase we
thought to write down. The snapshot is the whole corpus, so this scanner reads
every work once and decides locally what to keep.
Stage 1 IS this scan: every API-harvest source has been removed, and the
survivor pool this writes is Stage 2's input.

One gate, recall-oriented — **the search gate** (vectorized, pyarrow): a broad
token regex over title and the RAW abstract inverted-index JSON, OR membership of
a replication concept. A work that trips either arm is a survivor; nothing else
about it is judged here.

Stage 1 only searches. It applies NO exclusion patterns and no phrase guards, and
it does not import ``keyword_verdict``: every exclusion decision belongs to
Stage 2's filter engine, which reads the survivor pool. That is the whole point —
one rule set, in one place, over a pool that keeps everything the search found.

Progress is checkpointed per manifest file in ``cache/snapshot/ledger.json`` so
an interrupted 400+ GB scan resumes where it stopped.

The survivor pool
-----------------
``--survivor-pool PATH`` persists EVERY survivor — before the year filter — as a
parquet dataset, one file per manifest partition. The gate keeps well under 1% of
the corpus, so the pool is a few GB against 725 GB of snapshot: with it on disk,
every downstream decision is a local re-run over the pool rather than a 13-21 hour
rescan. Only a change to the gate itself (``_TOKEN_GATE`` or ``CONCEPT_IDS``) is
expensive after this — see ``search_gate_fingerprint``.

Pool columns (``_POOL_SCHEMA``): the identity/metadata needed to rebuild a
candidate row without the snapshot — ``id``, ``doi``, ``title``,
``display_name``, ``publication_year``, ``type``, plus ``authorships``,
``primary_location``, ``open_access`` and ``concepts`` as JSON strings; the
already-reconstructed ``abstract_text`` (reading-order plain text — smaller than
the inverted index and it saves redoing the reconstruction); and the three
booleans recording WHY the gate kept the row: ``hit_token_title``,
``hit_token_abstract``, ``hit_concept``.

Usage (via Stage 1's entry point):
    python -m search.run_search --scan --survivor-pool data/pool
    python -m search.run_search --snapshot-pilot data/snapshot_pilot.csv --snapshot-max-files 3

Progress of a running scan (read-only, safe to run concurrently):
    python -m search.snapshot_scan --status
"""

import argparse
import datetime
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests

from shared.config import (
    RESEARCHER_EMAIL,
    SNAPSHOT_BASE_URL,
    SNAPSHOT_BATCH_ROWS,
    SNAPSHOT_CACHE_DIR,
    SNAPSHOT_HTTP_RETRIES,
    SNAPSHOT_HTTP_TIMEOUT,
    SNAPSHOT_POOL_DIR,
    log,
)

# The survivor pool's on-disk compression. This is the pool's FORMAT, not a
# preference: the pool is written here and read by every collaborator, so a
# per-machine value would produce shards the rest of the team cannot open. zstd is
# the best size/speed trade pyarrow ships by default.
SNAPSHOT_POOL_COMPRESSION = "zstd"
from shared.row_key import row_keys
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi, reconstruct_abstract
from filter.phrase_detection import CONCEPT_IDS, REPLICATION_STEM_PATTERN

SOURCE_TAG_SNAPSHOT = "openalex_snapshot"

# The search gate's vocabulary. Deliberately loose stems, not phrases — see
# _gate_mask for why this runs against the raw inverted-index JSON and therefore
# cannot use phrases. It is a SEARCH term list, the one thing Stage 1 and Stage 2
# are allowed to share; no exclusion pattern is read here.
_TOKEN_GATE = REPLICATION_STEM_PATTERN

_SCAN_COLUMNS = ["id", "doi", "title", "display_name", "publication_year", "type",
                 "authorships", "primary_location", "open_access", "concepts",
                 "abstract_inverted_index"]

_POOL_NESTED_COLUMNS = ["authorships", "primary_location", "open_access", "concepts"]

_POOL_SCHEMA = pa.schema([
    ("id", pa.string()),
    ("doi", pa.string()),
    ("title", pa.string()),
    ("display_name", pa.string()),
    ("publication_year", pa.int32()),
    ("type", pa.string()),
    ("authorships", pa.string()),
    ("primary_location", pa.string()),
    ("open_access", pa.string()),
    ("concepts", pa.string()),
    ("abstract_text", pa.string()),
    ("hit_token_title", pa.bool_()),
    ("hit_token_abstract", pa.bool_()),
    ("hit_concept", pa.bool_()),
])

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


def _fingerprint(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def search_gate_fingerprint() -> str:
    """Hash of the vectorized search gate — the only part a rescan can change.

    A file marked done under one gate has not been read under another, and the rows
    it rejected were never stored anywhere: recovering them means reading the
    partition again. This is the fingerprint whose mismatch is loud.

    Its INPUTS are unchanged from when this function was called
    ``stage_a_fingerprint``, so the value is unchanged too and an in-flight scan
    resumes across the rename. See ``ledger_gate_fingerprint``.
    """
    return _fingerprint([_TOKEN_GATE, "|".join(sorted(_CONCEPT_IDS_BARE))])


# The ledger key the gate fingerprint was persisted under before the rename. Read,
# never written: a 510M-row scan checkpointed under this name must keep resuming.
_LEGACY_GATE_KEY = "stage_a_fingerprint"
_GATE_KEY = "search_gate_fingerprint"


def ledger_gate_fingerprint(ledger: dict) -> Optional[str]:
    """The gate fingerprint *ledger* records, under either the new or the old key."""
    return ledger.get(_GATE_KEY) or ledger.get(_LEGACY_GATE_KEY)


def ledger_hash(ledger: dict) -> str:
    """Stable hash of what the ledger says was consumed.

    ``{url -> content_length}``, sorted by url so the hash does not move with the
    order files happened to be scanned in.

    Each entry's ``kept`` count is deliberately left out: it is a per-run
    observation, not part of what the ledger consumed. The ledger keeps it for
    reporting (``--status``); no identity is built on it.
    """
    files = ledger.get("files", {}) or {}
    parts = [f"{url}\t{(files.get(url) or {}).get('content_length')}" for url in sorted(files)]
    return _fingerprint(parts)


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
    return {"snapshot_date": "", _GATE_KEY: search_gate_fingerprint(), "files": {}}


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
# The search gate — vectorized
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


def _gate_masks(batch: pa.RecordBatch) -> tuple[pa.Array, pa.Array]:
    """The two search-gate token masks, kept apart: ``(title_hit, abstract_hit)``.

    The abstract test runs on the RAW ``abstract_inverted_index`` JSON string, on
    purpose. An inverted index is a {word: [positions]} dictionary whose key order
    is arbitrary, so adjacent words are NOT adjacent in the JSON: a phrase regex
    over this text would match or miss by accident. Only single-token tests are
    sound here. Phrases are Stage 2's job, over the reconstructed abstract_text the
    pool stores. Do not "optimise" this into a phrase match.
    """
    title = pc.coalesce(_as_string(_column(batch, "display_name")),
                        _as_string(_column(batch, "title")))
    abstract_json = _as_string(_column(batch, "abstract_inverted_index"))

    return (pc.fill_null(pc.match_substring_regex(title, _TOKEN_GATE), False),
            pc.fill_null(pc.match_substring_regex(abstract_json, _TOKEN_GATE), False))


def _gate_mask(batch: pa.RecordBatch) -> pa.Array:
    """Search-gate token mask: a replication stem in the title or anywhere in the abstract."""
    title_hit, abstract_hit = _gate_masks(batch)
    return pc.or_(title_hit, abstract_hit)


def _concept_mask(batch: pa.RecordBatch) -> pa.Array:
    """Search-gate concept mask: the work carries one of ``CONCEPT_IDS``.

    Snapshot concept ids are URL-form (``https://openalex.org/C12590798``) while
    ``CONCEPT_IDS`` holds bare ids, so ids are stripped before comparison.
    """
    if "concepts" not in batch.schema.names:
        return pa.array(np.zeros(batch.num_rows, dtype=bool))

    col = batch.column("concepts")
    if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
        # Some snapshot builds ship concepts as a JSON string rather than a list
        # of structs. The id must be anchored on the closing quote of the JSON
        # string value it ends: unanchored, C9893847 also matches C98938470 —
        # a different concept — and a concept hit admits the work on its own.
        # (Anchoring the left side would need a lookbehind, which RE2 — pyarrow's
        # engine — does not have; ids are prefix-free in practice because they all
        # start at a "C" that no id contains elsewhere.)
        pattern = "|".join(re.escape(c) + '"' for c in sorted(_CONCEPT_IDS_BARE))
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


def _abstract_text(value: "object") -> Optional[str]:
    """Reading-order abstract text for a snapshot ``abstract_inverted_index`` value.

    The snapshot ships the index as a JSON string, and a few records hold something
    that is valid JSON but not a ``{word: [positions]}`` object — a list, a bare
    string, a number. ``reconstruct_abstract`` raises AttributeError on those, which
    used to look like a failed partition READ and cost the other ~200k records in it.
    A record with no usable index simply has no abstract.
    """
    parsed = _maybe_json(value)
    if not isinstance(parsed, dict):
        return None
    return reconstruct_abstract(parsed)


def _build_ref(authors_r: "str | None", year_r: "int | None", journal_r: "str | None") -> str:
    """Build a FLoRA-style reference string: 'Surname · Year · Journal'.

    Uses only the last-name component of the first author. Returns a partial
    string (e.g. 'Smith · 2020') when journal is unavailable.
    """
    if not authors_r:
        surname = ""
    else:
        first_author = str(authors_r).split(";")[0].strip()
        parts = first_author.split()
        surname = parts[-1] if parts else ""
    segments = [s for s in [surname, str(year_r) if year_r else "", journal_r or ""] if s]
    return " · ".join(segments)


def _row_from_snapshot(rec: dict, abstract: Optional[str] = None) -> dict:
    """Convert one snapshot record into the shared candidate-row schema.

    Field-for-field the same mapping as ``_extract_row()`` on the API JSON, except
    that nested fields may arrive as JSON strings and ``source`` names the snapshot.

    *abstract* supplies an already-reconstructed abstract: pool rows store the
    reading-order text instead of the inverted index, so re-admission from the pool
    reuses this mapping without re-inverting anything.
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
        "abstract_r":    abstract if abstract is not None
                         else _abstract_text(rec.get("abstract_inverted_index")),
        "year_r":        year,
        "authors_r":     authors,
        "journal_r":     journal,
        "url_r":         open_access.get("oa_url") or location.get("landing_page_url"),
        "openalex_id_r": rec.get("id"),
        "source":        SOURCE_TAG_SNAPSHOT,
        "ref_r":         _build_ref(authors, year, journal),
    }


def _admitted_row(rec: dict, counters: dict, abstract: Optional[str] = None) -> dict:
    """The candidate row for *rec* — a search-gate survivor is admitted, full stop.

    THE single admission site: the scanner and the pool row builder both go through
    here, so a pool re-admission cannot drift from what the scan would have kept.
    Nothing is rejected here because Stage 1 applies no exclusions; a row this
    function produces is a row the search found, and Stage 2 decides its fate.
    """
    row = _row_from_snapshot(rec, abstract=abstract)
    counters["admitted"] += 1
    counters["no_abstract"] += int(not (row["abstract_r"] or ""))
    return row


# ---------------------------------------------------------------------------
# The survivor pool
# ---------------------------------------------------------------------------


def _json_str(value: "object") -> Optional[str]:
    """A nested snapshot field as a JSON string, whichever form the partition ships."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


def _pool_record(rec: dict, abstract: str, title_hit: bool,
                 abstract_hit: bool, concept_hit: bool) -> dict:
    """One search-gate survivor in ``_POOL_SCHEMA`` form."""
    year = rec.get("publication_year")
    return {
        "id":                 rec.get("id"),
        "doi":                rec.get("doi"),
        "title":              rec.get("title"),
        "display_name":       rec.get("display_name"),
        "publication_year":   int(year) if year is not None else None,
        "type":               rec.get("type"),
        **{c: _json_str(rec.get(c)) for c in _POOL_NESTED_COLUMNS},
        "abstract_text":      abstract,
        "hit_token_title":    title_hit,
        "hit_token_abstract": abstract_hit,
        "hit_concept":        concept_hit,
    }


def _pool_file_name(url: str) -> str:
    """The pool file for partition *url*, e.g. ``part-2016-01-24-part_0000.parquet``.

    Derived from the partition, never from a counter: a rescan of the same partition
    must overwrite its pool file rather than deposit a second copy of those rows.
    """
    parts = url.rstrip("/").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    folder = parts[-2] if len(parts) > 1 else ""
    date = folder.split("=", 1)[1] if folder.startswith("updated_date=") else folder
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{date}-{stem}").strip("-")
    return f"part-{slug}.parquet"


class _PoolWriter:
    """Streams one partition's search-gate survivors into ``<name>.tmp``, then commits.

    The pool file must be complete before the partition's ledger entry flips to
    ``done``, or a crash mid-write would leave a truncated pool that a resumed scan
    considers finished. Writing to a temp name and replacing at the end is what makes
    the two states — scanned, pooled — flip together.
    """

    def __init__(self, pool_dir: Path, url: str) -> None:
        pool_dir.mkdir(parents=True, exist_ok=True)
        self.final = pool_dir / _pool_file_name(url)
        self.tmp = self.final.with_suffix(".tmp")
        self._writer: Optional[pq.ParquetWriter] = None
        self.rows = 0

    def reset(self) -> None:
        """Start the file over — a retried partition re-reads from row zero."""
        self._close()
        self.tmp.unlink(missing_ok=True)
        self.rows = 0

    def write(self, records: list[dict]) -> None:
        if not records:
            return
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.tmp, _POOL_SCHEMA,
                                            compression=SNAPSHOT_POOL_COMPRESSION)
        self._writer.write_table(pa.Table.from_pylist(records, schema=_POOL_SCHEMA))
        self.rows += len(records)

    def commit(self) -> int:
        """Close the temp file and put it in place. Returns the rows written.

        A partition with no survivor at all leaves no file — and drops any
        file an earlier scan of that same partition left, which would otherwise
        outlive the rows it was written from.
        """
        self._close()
        if self.tmp.exists():
            self.tmp.replace(self.final)
        else:
            self.final.unlink(missing_ok=True)
        return self.rows

    def abandon(self) -> None:
        """Drop a partial file: the partition was not consumed, so neither was its pool."""
        self.reset()

    def _close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def _pool_size_bytes(pool_dir: Path) -> int:
    return sum(f.stat().st_size for f in pool_dir.glob("*.parquet")) if pool_dir.exists() else 0


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


# Per-record parse defects are logged individually up to this many, then only counted:
# a partition can hold a systematic defect, and one line per record would bury the run.
_MAX_ROW_ERROR_LOGS = 20


class _MergeFailed(Exception):
    """The pilot CSV write failed — never a retryable read error.

    Retrying it is unsafe: the rows it was appending are already recorded as seen
    in memory, so a second attempt would write a partial duplicate. This wrapper
    carries such failures past the read-retry handler so they propagate out of
    ``scan_snapshot`` with the ledger left at "merging".
    """


def _batch_rows(batch: pa.RecordBatch, from_year: Optional[int], to_year: Optional[int],
                counters: dict, pool: Optional["_PoolWriter"] = None) -> list[dict]:
    """Gate one batch and return the admitted candidate rows, updating *counters*.

    Every survivor goes to *pool* when one is given — before the year filter, because
    the pool exists precisely so that that decision can be revisited without the
    snapshot.
    """
    counters["scanned"] += batch.num_rows

    title_token_mask, abstract_token_mask = _gate_masks(batch)
    token = pc.or_(title_token_mask, abstract_token_mask)
    concept = _concept_mask(batch)
    survivors = pc.or_(token, concept)
    counters["gate_token"] += int(pc.sum(token).as_py() or 0)
    counters["gate_concept"] += int(pc.sum(concept).as_py() or 0)

    n_survivors = int(pc.sum(survivors).as_py() or 0)
    if not n_survivors:
        return []
    counters["gate_survivors"] += n_survivors

    kept = batch.filter(survivors)
    concept_flags = pc.filter(concept, survivors).to_pylist()
    title_flags = pc.filter(title_token_mask, survivors).to_pylist()
    abstract_flags = pc.filter(abstract_token_mask, survivors).to_pylist()

    rows: list[dict] = []
    pool_records: list[dict] = []
    for rec, concept_hit, title_hit, abstract_hit in zip(
            kept.to_pylist(), concept_flags, title_flags, abstract_flags):
        # A malformed RECORD costs that record and nothing else. The retry/skip
        # handler around the partition read exists for transport failures, where
        # reading again can succeed; a record the snapshot ships with a field we
        # cannot parse fails identically on every retry, and letting it out of here
        # would discard the ~200k sound records around it under the report of a
        # successful scan.
        try:
            abstract = _abstract_text(rec.get("abstract_inverted_index")) or ""
            if pool is not None:
                pool_records.append(_pool_record(rec, abstract, bool(title_hit),
                                                 bool(abstract_hit), bool(concept_hit)))
            year = rec.get("publication_year")
            if year is not None:
                if from_year is not None and year < from_year:
                    continue
                if to_year is not None and year > to_year:
                    continue
            rows.append(_admitted_row(rec, counters, abstract=abstract))
        except Exception as exc:  # noqa: BLE001 — one unparseable record, not a read failure
            counters["row_errors"] += 1
            if counters["row_errors"] <= _MAX_ROW_ERROR_LOGS:
                log.warning("Snapshot record skipped (%s): %s", rec.get("id"), exc)
            elif counters["row_errors"] == _MAX_ROW_ERROR_LOGS + 1:
                log.warning("Further malformed snapshot records will only be counted.")

    if pool is not None:
        pool.write(pool_records)
    return rows


def scan_snapshot(max_files: Optional[int] = None,
                  from_year: Optional[int] = None,
                  to_year: Optional[int] = None,
                  pilot_csv: Optional[Path] = None,
                  files: Optional[list[str]] = None,
                  survivor_pool: Optional[Path] = None) -> int:
    """Scan OpenAlex snapshot partitions and write every survivor to the pool.

    Production mode (``pilot_csv=None``) is ledger-backed and full-corpus: it scans
    every manifest file not already marked done and writes the survivors to
    *survivor_pool*, one parquet file per partition. The pool is the output —
    Stage 2 reads it directly.

    Pilot mode (``pilot_csv`` set) also writes the admitted rows to that CSV, keeps
    NO ledger, dedupes in memory against it, and prints a gate report. It is the
    only mode where *from_year*/*to_year* apply — a production file marked done
    under a narrow year filter would be an unsound checkpoint, so year bounds are
    ignored (with a warning) outside pilot mode.

    *files* pins an explicit list of partition URLs (used by pilot runs and the
    live test) and skips the manifest fetch entirely; otherwise the manifest order
    is followed, capped by *max_files*.

    A partition that cannot be READ is retried and then skipped, but a failure of
    the merge or of the pilot write propagates out of this function immediately —
    it leaves local state half-written, which is not something a retry can repair.
    A single malformed RECORD is neither: it is logged, skipped, and the rest of its
    partition is consumed (see ``_batch_rows``).

    *survivor_pool* persists every search-gate survivor under that directory, one
    parquet file per partition, before the year filter — so a later gate-independent
    question is answered locally instead of by a 13-21 hour rescan.

    Returns the number of rows admitted (in pilot mode, the number appended to the
    pilot CSV).
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

    ledger: dict = {}

    if pilot_csv is not None:
        targets = source_files
        if max_files is not None:
            targets = targets[:max_files]
        seen_keys = _pilot_keys(pilot_csv)
    else:
        ledger = load_ledger()
        if ledger_gate_fingerprint(ledger) not in (None, search_gate_fingerprint()):
            log.warning("Snapshot ledger was written under a DIFFERENT gate — files already "
                        "marked done were not scanned with the current tokens/concepts, and "
                        "the rows that gate rejected were never stored. "
                        "Delete %s to force a full rescan.", _LEDGER_PATH)
        if files is None:
            ledger["snapshot_date"] = (manifest.get("meta") or {}).get("updated_date", "") \
                or ledger.get("snapshot_date", "")
        targets = [(u, m) for u, m in source_files if _needs_scan(u, m, ledger)]
        if max_files is not None:
            targets = targets[:max_files]
        seen_keys = set()

    log.info("Snapshot scan: %d of %d manifest files to read%s",
             len(targets), n_available, " (pilot)" if pilot_csv is not None else "")

    counters = {"scanned": 0, "gate_survivors": 0, "gate_token": 0, "gate_concept": 0,
                "admitted": 0, "no_abstract": 0, "pooled": 0, "row_errors": 0}
    total_merged = 0
    skipped: list[str] = []

    for i, (url, meta) in enumerate(targets, 1):
        # What the ledger said about this partition before this attempt. A partition is
        # re-targeted when the manifest rewrote it, and a failed rescan must leave the
        # earlier completed record standing rather than erase what WAS consumed.
        previous_entry = ledger.get("files", {}).get(url) if pilot_csv is None else None
        if pilot_csv is None:
            ledger["files"][url] = {**meta, "status": "merging"}
            # Only ever recorded on a ledger that names no gate at all: overwriting a
            # mismatching fingerprint would silence the warning above from the first file
            # scanned onwards, while most files on record were still scanned under the old
            # gate. A ledger holding only the legacy key keeps it — the value is the same
            # under either name, so rewriting it would gain nothing and could look like a
            # gate change to an older checkout.
            if ledger_gate_fingerprint(ledger) is None:
                ledger[_GATE_KEY] = search_gate_fingerprint()
            save_ledger(ledger)

        pool = _PoolWriter(survivor_pool, url) if survivor_pool is not None else None
        file_merged = 0
        for attempt in range(SNAPSHOT_HTTP_RETRIES):
            try:
                if pool is not None:
                    pool.reset()  # a retry re-reads the partition from row zero
                pf = _open_parquet(url)
                columns = [c for c in _SCAN_COLUMNS if c in pf.schema_arrow.names]
                for batch in pf.iter_batches(batch_size=SNAPSHOT_BATCH_ROWS, columns=columns):
                    rows = _batch_rows(batch, from_year, to_year, counters, pool=pool)
                    if not rows:
                        continue
                    file_merged += len(rows)
                    if pilot_csv is not None:
                        fresh = []
                        for row in rows:
                            keys = [k for k in row_keys(row) if k]
                            if keys and any(k in seen_keys for k in keys):
                                continue
                            seen_keys.update(keys)
                            fresh.append(row)
                        file_merged -= len(rows) - len(fresh)
                        if fresh:
                            try:
                                _write_pilot(pd.DataFrame(fresh, columns=CANDIDATES_COLS),
                                             pilot_csv)
                            except Exception as exc:
                                raise _MergeFailed(url) from exc
                break
            except _MergeFailed as failure:
                # Out of the retry loop untouched, and with the ledger entry left at
                # "merging" so the next run rebuilds the index before rescanning.
                if pool is not None:
                    pool.abandon()
                raise failure.__cause__ from None
            except Exception as exc:  # noqa: BLE001 — any read failure is retried, then skipped
                if attempt == SNAPSHOT_HTTP_RETRIES - 1:
                    log.error("Snapshot file failed after %d attempts — skipping %s (%s)",
                              SNAPSHOT_HTTP_RETRIES, url, exc)
                    skipped.append(url)
                    if pool is not None:
                        pool.abandon()
                    if pilot_csv is None:
                        # Never leave a skipped file at "merging": it was not consumed,
                        # and "merging" would trigger an index rebuild on every later
                        # run. What it said BEFORE this attempt still holds, though —
                        # a rewritten partition whose rescan failed is still on record
                        # as consumed at its old size, and will be re-targeted again.
                        if previous_entry is not None:
                            ledger["files"][url] = previous_entry
                        else:
                            ledger["files"].pop(url, None)
                        save_ledger(ledger)
                    break
                wait = 2 ** attempt
                log.warning("Snapshot read error on %s (%s) — retry %d/%d in %ds",
                            url, exc, attempt + 1, SNAPSHOT_HTTP_RETRIES, wait)
                time.sleep(wait)

        total_merged += file_merged
        # The pool file is complete and in place BEFORE the ledger says "done", so a
        # crash can leave a partition unscanned but never scanned-without-its-pool.
        if pool is not None and url not in skipped:
            counters["pooled"] += pool.commit()
        if pilot_csv is None and url not in skipped:
            ledger["files"][url] = {**meta, "status": "done", "kept": file_merged,
                                    "scanned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
            save_ledger(ledger)
        log.info("Snapshot %d/%d  %s  merged=%d  (running total %d)",
                 i, len(targets), "/".join(url.split("/")[-2:]), file_merged, total_merged)

    if skipped:
        log.warning("Snapshot scan finished with %d unreadable file(s), left unscanned:\n%s",
                    len(skipped), "\n".join(skipped))
    if counters["row_errors"]:
        log.warning("Snapshot scan skipped %d malformed record(s); their partitions were "
                    "otherwise consumed in full.", counters["row_errors"])

    if survivor_pool is not None:
        log.info("Snapshot survivor pool: %d rows, %.1f MB at %s (gate=%s)",
                 counters["pooled"], _pool_size_bytes(survivor_pool) / 1e6, survivor_pool,
                 search_gate_fingerprint()[:12])

    if pilot_csv is not None:
        _print_pilot_report(counters, total_merged, len(targets), pilot_csv, survivor_pool)

    return total_merged


def _print_pilot_report(counters: dict, written: int, n_files: int, pilot_csv: Path,
                        survivor_pool: Optional[Path] = None) -> None:
    """Print the Phase 0 numbers the admission rule is meant to be judged on."""
    print(f"\n=== Snapshot pilot report ({n_files} file(s) -> {pilot_csv}) ===")
    print(f"  rows scanned                          {counters['scanned']:,}")
    print(f"  search-gate survivors                 {counters['gate_survivors']:,}"
          f"  (token {counters['gate_token']:,}, concept {counters['gate_concept']:,})")
    print(f"  admitted                              {counters['admitted']:,}")
    print(f"  admitted with no abstract             {counters['no_abstract']:,}")
    print(f"  rows written to pilot CSV             {written:,}")
    if survivor_pool is not None:
        print(f"  rows written to survivor pool         {counters['pooled']:,}")
        print(f"  survivor pool on disk                 "
              f"{_pool_size_bytes(survivor_pool) / 1e6:,.1f} MB  ({survivor_pool})")
    print(f"  search-gate fingerprint               {search_gate_fingerprint()[:12]}\n")


# ---------------------------------------------------------------------------
# Status — how far along is a running scan?
# ---------------------------------------------------------------------------

# Files whose timestamps the throughput estimate is measured over. A scan that was
# stopped and resumed has an idle gap between two files somewhere in its ledger, and
# averaging over the whole ledger would charge that gap to the current run; a recent
# window is almost always inside one run.
_RATE_WINDOW_FILES = 50


def _parse_ts(value: "object") -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _humanize(seconds: float) -> str:
    """A duration as ``4h 12m`` / ``12m 30s`` — the granularity a watcher reads at."""
    seconds = int(max(seconds, 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def scan_status(pool_dir: Optional[Path] = None) -> dict:
    """What the ledger says about progress, without touching the network or the scan.

    Read-only by construction — it opens the cached manifest, the ledger and the pool
    directory and nothing else — so it is safe to run against a scan in flight. The
    manifest is read from cache only: fetching one would be a network call from a
    command whose whole point is to observe, and before the first scan there is
    nothing to observe anyway.

    Throughput and the ETA are measured over the last ``_RATE_WINDOW_FILES`` files by
    ``scanned_at``, in manifest bytes rather than files, because partitions differ in
    size and the job is network-bound.
    """
    pool_dir = SNAPSHOT_POOL_DIR if pool_dir is None else pool_dir

    manifest: dict = {}
    if _MANIFEST_PATH.exists():
        try:
            with open(_MANIFEST_PATH, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:  # noqa: BLE001 — a corrupt cache is "totals unknown", not a crash
            log.warning("Corrupt cached manifest at %s — totals unavailable", _MANIFEST_PATH)

    all_files = _manifest_files(manifest) if manifest else []
    total_bytes = sum(int((m.get("content_length") or 0)) for _, m in all_files)
    total_records = sum(int((m.get("record_count") or 0)) for _, m in all_files)

    ledger = load_ledger()
    entries = ledger.get("files", {}) or {}
    done = {u: e for u, e in entries.items() if (e or {}).get("status") == "done"}
    in_flight = [u for u, e in entries.items() if (e or {}).get("status") != "done"]

    done_bytes = sum(int((e.get("content_length") or 0)) for e in done.values())
    done_records = sum(int((e.get("record_count") or 0)) for e in done.values())
    kept = sum(int((e.get("kept") or 0)) for e in done.values())

    stamped = sorted(((_parse_ts(e.get("scanned_at")), e) for e in done.values()
                      if _parse_ts(e.get("scanned_at"))), key=lambda p: p[0])
    window = stamped[-_RATE_WINDOW_FILES:]
    bytes_per_sec = 0.0
    if len(window) >= 2:
        span = (window[-1][0] - window[0][0]).total_seconds()
        # The first file of the window finished before the window started, so its bytes
        # were not transferred during `span`.
        moved = sum(int((e.get("content_length") or 0)) for _, e in window[1:])
        if span > 0:
            bytes_per_sec = moved / span

    remaining_bytes = max(total_bytes - done_bytes, 0)
    eta_seconds = remaining_bytes / bytes_per_sec if bytes_per_sec > 0 and total_bytes else None

    pool_files = sorted(pool_dir.glob("*.parquet")) if pool_dir.exists() else []

    return {
        "files_done": len(done),
        "files_total": len(all_files),
        "files_in_flight": in_flight,
        "bytes_done": done_bytes,
        "bytes_total": total_bytes,
        "records_done": done_records,
        "records_total": total_records,
        "rows_kept": kept,
        "pool_dir": str(pool_dir),
        "pool_files": len(pool_files),
        "pool_bytes": sum(f.stat().st_size for f in pool_files),
        "first_scanned_at": stamped[0][0].isoformat(timespec="seconds") if stamped else "",
        "last_scanned_at": stamped[-1][0].isoformat(timespec="seconds") if stamped else "",
        "bytes_per_sec": bytes_per_sec,
        "eta_seconds": eta_seconds,
        "snapshot_date": ledger.get("snapshot_date", "") or "",
        "search_gate_fingerprint": ledger_gate_fingerprint(ledger) or "",
        "ledger_path": str(_LEDGER_PATH),
    }


def _print_status(status: dict) -> None:
    pct = (100.0 * status["files_done"] / status["files_total"]) if status["files_total"] else 0.0
    last = status["last_scanned_at"]
    idle = ""
    stamp = _parse_ts(last)
    if stamp is not None:
        now = datetime.datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.datetime.now()
        idle = f"  ({_humanize((now - stamp).total_seconds())} ago)"

    print(f"\n=== Snapshot scan status ({status['ledger_path']}) ===")
    if not status["files_total"]:
        print("  no cached manifest — totals unknown until a scan has fetched one")
    print(f"  files consumed                        {status['files_done']:,}"
          f" / {status['files_total']:,}  ({pct:.1f}%)")
    print(f"  bytes consumed                        {status['bytes_done'] / 1e9:,.2f}"
          f" / {status['bytes_total'] / 1e9:,.2f} GB")
    print(f"  records scanned                       {status['records_done']:,}"
          f" / {status['records_total']:,}")
    print(f"  rows admitted                         {status['rows_kept']:,}")
    print(f"  survivor pool                         {status['pool_files']:,} file(s), "
          f"{status['pool_bytes'] / 1e9:,.2f} GB  ({status['pool_dir']})")
    if status["files_in_flight"]:
        print(f"  file(s) mid-scan                      {len(status['files_in_flight'])} "
              f"({'/'.join(status['files_in_flight'][0].split('/')[-2:])})")
    print(f"  first / last file finished            {status['first_scanned_at'] or '—'} / "
          f"{last or '—'}{idle}")
    if status["bytes_per_sec"]:
        print(f"  recent throughput                     "
              f"{status['bytes_per_sec'] / 1e6:,.1f} MB/s "
              f"(last {_RATE_WINDOW_FILES} files)")
    if status["eta_seconds"] is not None:
        print(f"  estimated time remaining              {_humanize(status['eta_seconds'])}")
    print(f"  snapshot date                         {status['snapshot_date'] or '—'}")
    print(f"  search-gate fingerprint               "
          f"{status['search_gate_fingerprint'][:12] or '—'}")
    print(f"  this checkout                         {search_gate_fingerprint()[:12]}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only progress report for the OpenAlex snapshot scan. Safe to "
                    "run at any time, including against a scan in flight — it reads the "
                    "ledger, the cached manifest and the pool directory, and writes "
                    "nothing. The scan itself is started from "
                    "`python -m search.run_search --scan`.")
    parser.add_argument("--status", action="store_true",
                        help="Print progress (the only action; implied when no flag is given).")
    parser.add_argument("--json", action="store_true", help="Emit the status as JSON.")
    parser.add_argument("--pool-dir", metavar="PATH", default=None,
                        help=f"Survivor pool directory (default: {SNAPSHOT_POOL_DIR}).")
    args = parser.parse_args()

    status = scan_status(Path(args.pool_dir) if args.pool_dir else None)
    if args.json:
        print(json.dumps(status, indent=1))
    else:
        _print_status(status)


if __name__ == "__main__":
    main()
