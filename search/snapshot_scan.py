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

Beside the parquet sits ``_pool_provenance.json`` (``POOL_PROVENANCE``): the search
gate the pool's rows were ADMITTED under, the file count that completes the pool,
and where both came from. It is what ``pool_fingerprint`` hashes into a Stage 2
release id, so a pool shared between machines is named by its own gate rather than
by whichever checkout reads it, and an interrupted transfer is visibly short
instead of fingerprintable. Written by this scan and by ``pool_sync --pull``; an
older pool is stamped in place with ``--stamp-pool``.

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

A sample scan is the same command against a scratch state directory:
``FLORA_CACHE_DIR=/tmp/flora-sample python -m search.run_search --scan
--snapshot-max-files 3`` puts the ledger AND the pool (``FLORA_POOL_DIR``
defaults under the cache dir) somewhere throwaway. There is no separate sample
mode: one that wrote into the production pool without ledger entries left the two
disagreeing about what had been consumed.

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


# The pool's provenance sidecar. Deliberately NOT `part-*.parquet` and not even
# `*.parquet`, because every pool reader in the project globs `*.parquet` over this
# same directory (pool_reader, store, overlay, diagnostics, dashboard_cache,
# pool_sync, the analysis scripts) — a sidecar those globs could see would be fed
# to pyarrow as pool data. The leading underscore covers the OTHER way a pool gets
# read: pyarrow/pandas dataset discovery (`pd.read_parquet(pool_dir)`, as the live
# test and any ad-hoc look do) ignores `_`- and `.`-prefixed entries, so the whole
# directory still opens as one dataset.
POOL_PROVENANCE = "_pool_provenance.json"


def read_pool_provenance(pool_dir: Optional[Path] = None) -> Optional[dict]:
    """The pool's provenance sidecar, or None when the pool is unstamped.

    A sidecar that exists but cannot be read is a hard error rather than "no
    sidecar": it is the only record of which gate the pool's rows were admitted
    under, and treating a damaged one as absence is exactly the silent
    substitution the sidecar exists to prevent.
    """
    pool_dir = SNAPSHOT_POOL_DIR if pool_dir is None else Path(pool_dir)
    path = pool_dir / POOL_PROVENANCE
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except Exception as exc:  # noqa: BLE001 — unreadable local state, reported not guessed
        raise RuntimeError(
            f"The pool provenance sidecar at {path} exists but cannot be read ({exc}). "
            "It names the search gate this pool's rows were admitted under, so this run "
            "will not guess. Restore it, or delete it and re-stamp with "
            "`python -m search.snapshot_scan --stamp-pool`.") from exc
    if not isinstance(record, dict):
        raise RuntimeError(f"The pool provenance sidecar at {path} is not a JSON object.")
    return record


def write_pool_provenance(pool_dir: Path, gate: Optional[str], expected_files: int,
                          source: str) -> Path:
    """Record what a pool is: its gate, how many parquet files complete it, whence.

    *gate* is the fingerprint the pool's rows were ADMITTED under — from the local
    ledger for a scan, from the remote manifest's ``stage_a_fingerprint`` for a
    pull. It is never ``search_gate_fingerprint()`` "because that is what this
    checkout computes"; a checkout's gate says nothing about rows it did not admit.
    """
    pool_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "search_gate_fingerprint": gate,
        "expected_files": int(expected_files),
        "source": source,
        "recorded_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    path = pool_dir / POOL_PROVENANCE
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    tmp.replace(path)
    return path


def pool_fingerprint(pool_dir: Optional[Path] = None) -> Optional[str]:
    """Stable hash of the survivor POOL — the artifact downstream stages consume.

    The ledger records one machine's scan; a pool obtained through
    ``pool_sync --pull`` has no ledger at all, so ``ledger_hash`` cannot name what
    a routing run read. This hashes the pool directory instead: the search gate the
    rows were ADMITTED under — read from the provenance sidecar, never from this
    checkout — plus every parquet as ``(filename, size_bytes, num_rows)`` sorted by
    name, so the value does not move with directory order. Row counts come from the
    parquet footer, which costs about 0.4 s over the real 2,232-file pool — cheap
    enough to recompute per route, and cheaper than a cache that can drift.

    Three states the sidecar decides:

    * **absent** — the gate enters the payload as ``null``. Not the local gate: a
      pool pulled from a differently-gated repo would then be silently attributed
      to this checkout, which is the mislabelling the gate was included to prevent.
      ``null`` collides with no real fingerprint, so stamping the pool later mints
      a new release id rather than retroactively re-describing an old one. Routing
      is still possible, because refusing would strand every pool in existence.
    * **short file count** — fewer parquet files than the sidecar says complete
      this pool: returns ``None`` (the caller routes under ``unmanifested``) rather
      than fingerprinting an interrupted transfer as though it were a pool.
    * **another gate** — legitimate (sharing a pool is the point) and reported, and
      the RECORDED gate is what is hashed, so the same pool fingerprints the same
      on every checkout.

    Known limitation, accepted deliberately: size and row count do not determine
    file CONTENTS, so a parquet rewritten to the same size with the same number of
    rows fingerprints unchanged. Hashing 7.6 GB of pool content on every route is
    not worth that case, and this is already strictly stronger than the
    ``content_length``-only ledger hash it replaced.

    Returns ``None`` when there is no pool (missing or empty directory): an empty
    file list must not hash into a real-looking digest.
    """
    pool_dir = SNAPSHOT_POOL_DIR if pool_dir is None else Path(pool_dir)
    files = sorted(pool_dir.glob("*.parquet")) if pool_dir.is_dir() else []
    if not files:
        return None

    provenance = read_pool_provenance(pool_dir)
    gate: Optional[str] = None
    if provenance is None:
        log.warning(
            "The pool at %s carries no %s, so the release id cannot say which search "
            "gate admitted its rows — it will record the gate as UNKNOWN rather than "
            "assume this checkout's (%s). Stamp it with `python -m search.snapshot_scan "
            "--stamp-pool` (it reads the local ledger) or `--stamp-pool --gate <fingerprint>`.",
            pool_dir, POOL_PROVENANCE, search_gate_fingerprint()[:12])
    else:
        expected = provenance.get("expected_files")
        if isinstance(expected, int) and len(files) < expected:
            log.error(
                "The pool at %s looks PARTIAL: %d parquet file(s) present, %d expected "
                "by %s (source %s). Routing it would checkpoint a fraction of the corpus "
                "as a definitive release, so it has no fingerprint. Complete it with "
                "`python -m search.pool_sync --pull` (it resumes) or, if this subset is "
                "deliberate, re-stamp with `python -m search.snapshot_scan --stamp-pool`.",
                pool_dir, len(files), expected, POOL_PROVENANCE,
                provenance.get("source") or "?")
            return None
        gate = provenance.get("search_gate_fingerprint") or None
        if gate is None:
            log.warning("The %s at %s records no search gate — the release id will say "
                        "UNKNOWN.", POOL_PROVENANCE, pool_dir)
        elif gate != search_gate_fingerprint():
            log.warning(
                "The pool at %s was admitted under a DIFFERENT search gate (pool %s, this "
                "checkout %s, source %s). That is legitimate — sharing a pool is the point "
                "— and the release id names the POOL's gate, not this checkout's.",
                pool_dir, str(gate)[:12], search_gate_fingerprint()[:12],
                provenance.get("source") or "?")

    payload = {
        "search_gate_fingerprint": gate,
        "files": [[p.name, p.stat().st_size, pq.ParquetFile(p).metadata.num_rows]
                  for p in files],
    }
    return _fingerprint([json.dumps(payload, sort_keys=True, separators=(",", ":"))])


def load_ledger() -> dict:
    """Load the scan ledger, or a fresh one when there is none.

    A ledger that exists but cannot be parsed is a hard error, not a fresh start:
    it records which of 2,446 partitions were consumed, and silently replacing it
    with an empty one turns a damaged file into an order to rescan 725 GB — or,
    worse, into a pool that gets a second copy of every partition it already holds.
    The operator decides: restore the file, or move it aside deliberately.
    """
    if _LEDGER_PATH.exists():
        try:
            with open(_LEDGER_PATH, encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception as exc:  # noqa: BLE001 — unreadable local state, reported not guessed
            raise RuntimeError(
                f"The snapshot ledger at {_LEDGER_PATH} exists but cannot be read ({exc}). "
                "It is the record of which partitions have been consumed, so this run "
                "will not guess. Restore it from a backup, or move it aside "
                f"(mv {_LEDGER_PATH} {_LEDGER_PATH}.broken) to start a fresh scan — "
                "which will re-read every partition and overwrite the pool file of "
                "each one it re-reads.") from exc
        if not isinstance(ledger, dict):
            raise RuntimeError(
                f"The snapshot ledger at {_LEDGER_PATH} is not a JSON object. See above: "
                "restore it or move it aside deliberately.")
        ledger.setdefault("files", {})
        return ledger
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


# Per-record parse defects are logged individually up to this many, then only counted:
# a partition can hold a systematic defect, and one line per record would bury the run.
_MAX_ROW_ERROR_LOGS = 20


def _batch_rows(batch: pa.RecordBatch, counters: dict,
                pool: Optional["_PoolWriter"] = None) -> list[dict]:
    """Gate one batch and return the admitted candidate rows, updating *counters*.

    Every survivor goes to *pool* when one is given. Stage 1 applies no filter of its
    own beyond the search gate — a year bound here would put rows in the pool that the
    ledger then records as a consumed partition, which is not a checkpoint anything
    could trust. Year bounds are Stage 2's (``filter.engine export/handoff``).
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
                  files: Optional[list[str]] = None,
                  survivor_pool: Optional[Path] = None,
                  force_gate: bool = False) -> int:
    """Scan OpenAlex snapshot partitions and write every survivor to the pool.

    One mode, ledger-backed: it scans every manifest file not already marked done
    and writes the survivors to *survivor_pool*, one parquet file per partition.
    The pool is the output — Stage 2 reads it directly. To scan a handful of
    partitions for a look, point ``FLORA_CACHE_DIR`` at a scratch directory (which
    moves the ledger and, with it, the pool) and cap ``max_files``.

    *files* pins an explicit list of partition URLs (used by the live test) and
    skips the manifest fetch entirely; otherwise the manifest order is followed,
    capped by *max_files*.

    A partition that cannot be READ is retried and then skipped; the counters and
    the pool file of a retried attempt are rolled back first, so a partition is
    counted once however many times it was read. A single malformed RECORD is not
    a read failure: it is logged, skipped, and the rest of its partition is
    consumed (see ``_batch_rows``).

    *survivor_pool* persists every search-gate survivor under that directory, one
    parquet file per partition — so a later gate-independent question is answered
    locally instead of by a 13-21 hour rescan.

    A ledger written under a different search gate stops the run unless
    *force_gate*: see the message below for why.

    Returns the number of rows admitted.
    """
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

    ledger = load_ledger()
    theirs = ledger_gate_fingerprint(ledger)
    if theirs not in (None, search_gate_fingerprint()):
        # Refusing rather than warning: continuing appends this gate's survivors to a
        # pool whose other partitions were gated differently, and the rows the OTHER
        # gate rejected are in no pool at all. The result is complete under neither
        # gate, and nothing downstream — not the pool, not ledger_hash — can tell.
        if not force_gate:
            raise RuntimeError(
                f"The snapshot ledger at {_LEDGER_PATH} was written under a DIFFERENT "
                f"search gate (ledger {str(theirs)[:12]}, this checkout "
                f"{search_gate_fingerprint()[:12]}). The partitions it marks done were "
                "not read with the current tokens/concepts, and the rows that gate "
                "rejected were never stored, so continuing would build a pool that is "
                "complete under neither gate. Either rescan from scratch into a fresh "
                "pool directory and a fresh ledger (move both aside, or set "
                "FLORA_CACHE_DIR), or pass --force-gate to add this gate's partitions "
                "to that pool knowing the mixture.")
        log.warning("--force-gate: scanning under gate %s into a pool whose ledger names "
                    "%s. The pool will be complete under neither gate.",
                    search_gate_fingerprint()[:12], str(theirs)[:12])
    if files is None:
        ledger["snapshot_date"] = (manifest.get("meta") or {}).get("updated_date", "") \
            or ledger.get("snapshot_date", "")
    targets = [(u, m) for u, m in source_files if _needs_scan(u, m, ledger)]
    if max_files is not None:
        targets = targets[:max_files]

    log.info("Snapshot scan: %d of %d manifest files to read",
             len(targets), n_available)

    counters = {"scanned": 0, "gate_survivors": 0, "gate_token": 0, "gate_concept": 0,
                "admitted": 0, "no_abstract": 0, "pooled": 0, "row_errors": 0}
    total_merged = 0
    skipped: list[str] = []

    for i, (url, meta) in enumerate(targets, 1):
        # What the ledger said about this partition before this attempt. A partition is
        # re-targeted when the manifest rewrote it, and a failed rescan must leave the
        # earlier completed record standing rather than erase what WAS consumed.
        previous_entry = ledger.get("files", {}).get(url)
        ledger["files"][url] = {**meta, "status": "merging"}
        # Only ever recorded on a ledger that names no gate at all: overwriting a
        # mismatching fingerprint is what --force-gate decides, and a ledger holding
        # only the legacy key keeps it — the value is the same under either name, so
        # rewriting it would gain nothing and could look like a gate change to an
        # older checkout.
        if ledger_gate_fingerprint(ledger) is None:
            ledger[_GATE_KEY] = search_gate_fingerprint()
        save_ledger(ledger)

        pool = _PoolWriter(survivor_pool, url) if survivor_pool is not None else None
        # A retry re-reads the partition from row zero, so everything the failed
        # attempt counted must go back too — otherwise its rows are counted twice in
        # the run's scanned/gate/admitted totals, which is what the report is read off.
        before_partition = dict(counters)
        file_merged = 0
        for attempt in range(SNAPSHOT_HTTP_RETRIES):
            counters.update(before_partition)
            file_merged = 0
            try:
                if pool is not None:
                    pool.reset()
                pf = _open_parquet(url)
                columns = [c for c in _SCAN_COLUMNS if c in pf.schema_arrow.names]
                for batch in pf.iter_batches(batch_size=SNAPSHOT_BATCH_ROWS, columns=columns):
                    rows = _batch_rows(batch, counters, pool=pool)
                    file_merged += len(rows)
                break
            except Exception as exc:  # noqa: BLE001 — any read failure is retried, then skipped
                if attempt == SNAPSHOT_HTTP_RETRIES - 1:
                    log.error("Snapshot file failed after %d attempts — skipping %s (%s)",
                              SNAPSHOT_HTTP_RETRIES, url, exc)
                    skipped.append(url)
                    # Nothing of this partition was consumed: not its pool file, not
                    # its ledger entry, and not the rows the abandoned read counted.
                    counters.update(before_partition)
                    file_merged = 0
                    if pool is not None:
                        pool.abandon()
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
        if url not in skipped:
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

    # The gate report the retired pilot mode printed, now over whatever this run
    # consumed: the same numbers, and the only place they are visible per run.
    log.info("Snapshot gate report: %d row(s) scanned, %d survivor(s) "
             "(token %d, concept %d), %d admitted, %d with no abstract",
             counters["scanned"], counters["gate_survivors"], counters["gate_token"],
             counters["gate_concept"], counters["admitted"], counters["no_abstract"])

    if survivor_pool is not None:
        # Stamped here because a scan is the one place that knows the gate its rows
        # were admitted under authoritatively — it just applied it. The count is
        # whatever this scan leaves complete; a later resumed scan raises it.
        if survivor_pool.exists():
            write_pool_provenance(
                survivor_pool, ledger_gate_fingerprint(ledger) or search_gate_fingerprint(),
                len(list(survivor_pool.glob("*.parquet"))), "scan")
        log.info("Snapshot survivor pool: %d rows, %.1f MB at %s (gate=%s)",
                 counters["pooled"], _pool_size_bytes(survivor_pool) / 1e6, survivor_pool,
                 search_gate_fingerprint()[:12])

    return total_merged


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
    try:
        provenance = read_pool_provenance(pool_dir) or {}
    except RuntimeError as exc:  # a damaged sidecar is a finding, not a crashed status
        log.warning("%s", exc)
        provenance = {}

    return {
        "pool_gate": provenance.get("search_gate_fingerprint") or "",
        "pool_expected_files": provenance.get("expected_files"),
        "pool_provenance_source": provenance.get("source") or "",
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
    expected = status["pool_expected_files"]
    print(f"  pool provenance                       "
          + (f"gate {status['pool_gate'][:12] or '—'}, {expected:,} file(s) expected "
             f"({status['pool_provenance_source'] or '?'})" if expected is not None
             else f"unstamped — run --stamp-pool to record which gate admitted these rows"))
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


def stamp_pool(pool_dir: Optional[Path] = None, gate: Optional[str] = None) -> dict:
    """Write the provenance sidecar for a pool that already exists on disk.

    Every pool created before the sidecar existed is unstamped, and re-scanning or
    re-pulling one to stamp it costs hours. The gate comes from the local ledger, or
    from *gate* given explicitly (``"local"`` means "I confirm this checkout's gate
    admitted these rows"). With neither, this REFUSES: guessing the gate is the
    failure the sidecar exists to prevent, and a wrong stamp is worse than none.
    """
    pool_dir = SNAPSHOT_POOL_DIR if pool_dir is None else Path(pool_dir)
    files = sorted(pool_dir.glob("*.parquet")) if pool_dir.is_dir() else []
    if not files:
        raise RuntimeError(f"No pool parquet files under {pool_dir} — nothing to stamp.")

    if gate == "local":
        value, source = search_gate_fingerprint(), "stamp:local"
    elif gate:
        value, source = gate, "stamp:manual"
    else:
        # `load_ledger()` MANUFACTURES a fresh ledger carrying this checkout's gate
        # when there is no file — reading that would be exactly the guess this
        # refuses. Only a ledger that exists is an account of a scan.
        value = ledger_gate_fingerprint(load_ledger()) if _LEDGER_PATH.exists() else None
        source = "stamp:ledger"
        if not value:
            raise RuntimeError(
                f"There is no scan ledger at {_LEDGER_PATH} naming a search gate, so this "
                f"machine cannot say which gate admitted the rows in {pool_dir} — and it "
                "will not guess. Pass the fingerprint explicitly: for a pulled pool it is "
                "`stage_a_fingerprint` in the repo's pool_manifest.json "
                "(--gate <fingerprint>); for a pool this checkout scanned itself, "
                "--gate local.")

    path = write_pool_provenance(pool_dir, value, len(files), source)
    log.info("Stamped %s: gate %s (%s), %d file(s) expected", path, str(value)[:12],
             source, len(files))
    return {"path": str(path), "search_gate_fingerprint": value, "source": source,
            "expected_files": len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only progress report for the OpenAlex snapshot scan. Safe to "
                    "run at any time, including against a scan in flight — it reads the "
                    "ledger, the cached manifest and the pool directory, and writes "
                    "nothing. The scan itself is started from "
                    "`python -m search.run_search --scan`. The one exception is "
                    "--stamp-pool, which writes the pool's provenance sidecar.")
    parser.add_argument("--status", action="store_true",
                        help="Print progress (the default action).")
    parser.add_argument("--json", action="store_true", help="Emit the status as JSON.")
    parser.add_argument("--pool-dir", metavar="PATH", default=None,
                        help=f"Survivor pool directory (default: {SNAPSHOT_POOL_DIR}).")
    parser.add_argument("--stamp-pool", action="store_true",
                        help=f"Write {POOL_PROVENANCE} for an existing pool (the gate its "
                             "rows were admitted under + the file count that completes "
                             "it), without re-scanning or re-pulling. Uses the local "
                             "ledger's gate; refuses when there is none unless --gate says.")
    parser.add_argument("--gate", metavar="FINGERPRINT", default=None,
                        help="--stamp-pool only: the search-gate fingerprint the pool's "
                             "rows were admitted under (for a pulled pool: "
                             "stage_a_fingerprint from the repo's pool_manifest.json), or "
                             "the literal 'local' to claim this checkout's gate.")
    args = parser.parse_args()

    if args.gate and not args.stamp_pool:
        parser.error("--gate applies to --stamp-pool only")
    if args.stamp_pool:
        try:
            record = stamp_pool(Path(args.pool_dir) if args.pool_dir else None, args.gate)
        except RuntimeError as exc:  # an operator instruction, not a stack trace
            raise SystemExit(str(exc))
        print(f"Stamped {record['path']}: gate {str(record['search_gate_fingerprint'])[:12]} "
              f"({record['source']}), {record['expected_files']} file(s)")
        return

    status = scan_status(Path(args.pool_dir) if args.pool_dir else None)
    if args.json:
        print(json.dumps(status, indent=1))
    else:
        _print_status(status)


if __name__ == "__main__":
    main()
