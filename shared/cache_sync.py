"""
cache_sync.py — Share the API caches through the private Hugging Face dataset repo.

`search/pool_sync.py` shares the survivor pool; this shares what the pipeline paid
for on top of it. The caches are what a collaborator CANNOT rederive:

* `cache/llm` is money. Re-running it costs the provider bill again and, because
  the models are not deterministic, returns DIFFERENT verdicts — so a collaborator
  who re-buys them does not reproduce our grading, they produce their own.
* `cache/abstracts` is six rate-limited sources over ~500k identifiers, one of
  which (Scopus) needs an Elsevier entitlement not everyone has.
* `parse` / `grobid` / `openalex_xml` are the full-text extractions, and
  `openalex` / `doi_verify` are free per call but slow in bulk.

Sharing them is SAFE because the keys are content-complete: `content_key()` folds
the prompt version and the model into the key, and model ids are constants (code
style rule 8), not per-machine env. An entry from another machine is therefore
provably the answer this checkout would have computed; a checkout that differs
MISSES rather than reading someone else's answer as its own. The failure mode is a
wasted download, never a silent disagreement.

    python -m shared.cache_sync --push
    python -m shared.cache_sync --pull
    python -m shared.cache_sync --pull --parts llm,abstracts

Remote layout: `cache/<part>/<shard>.tar.gz`, plus `cache/cache_manifest.json`.
Shards exist because of FILE COUNT, not size — the whole shareable set is ~150 MB
raw and under 60 MB packed, but `cache/abstracts` alone is 478k files of ~90 bytes,
and Hugging Face asks for fewer than 10k entries per folder. Each shard holds the
entries whose `cache_key(filename)` starts with its hex prefix, so a file's shard
never moves and a re-push transfers only the shards that actually changed.

`cache/pdfs` IS shared, on the maintainer's decision: the repo is private, the
collaborators are named, and the acquisition waterfall is slow and lossy enough
that re-running it does not reliably return the same documents. What is not shared
is `cache/engine/responses` — `filter/engine/tiers.py` already pushes those blobs
as each tier run decides a work, and a second writer would just race it.

**Misses are shared too, and that is where the one asymmetry lives.** A cached
`__none__` means "this source definitively has no abstract for this DOI", and not
re-buying a known miss is most of the value. But a miss recorded by a machine that
lacked an entitlement is not the same fact, so `pull_cache` drops a miss (and its
checkpoint line) when the PRODUCER never got a single hit in that namespace and the
PULLER is configured for it. The test is evidence, not configuration: what the
manifest records is how many abstracts that machine actually recovered per source.
"""

import argparse
import datetime
import gzip
import io
import json
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from shared import abstract_store
from shared.config import (
    ABSTRACT_DB_PATH,
    CACHE_DIR,
    DOI_VERIFY_CACHE_DIR,
    ELSEVIER_API_KEY,
    FLORA_HF_COMMIT_BATCH,
    FLORA_POOL_REPO,
    GROBID_CACHE_DIR,
    LLM_CACHE_DIR,
    OA_CACHE_DIR,
    OA_XML_CACHE_DIR,
    OSF_TOKEN,
    PARSE_CACHE_DIR,
    PDF_CACHE_DIR,
    S2_API_KEY,
    log,
)
from shared.hf import (
    REPO_TYPE,
    RemoteReadError,
    auth_hint,
    is_absent,
    read_remote_json,
    require_token,
    resolve_repo,
    upload_batched,
)
from shared.prompts import prompt_version
from shared.utils import cache_key

_MANIFEST = "cache/cache_manifest.json"

# Where a pull records the shards it has already unpacked, so a repeat pull is a
# manifest read and nothing else. Deleting it (or --force) makes the next pull
# re-extract everything, which is how a locally deleted cache is refilled.
_PULL_STATE = CACHE_DIR / ".cache_sync_pulled.json"

class Part:
    """One cache directory and how it is sharded on the remote.

    *hex_chars* is the number of leading hex characters of `cache_key(filename)`
    that name the shard — 0 for a single shard, 1 for 16, 2 for 256. It is chosen
    per part to keep a shard in the low thousands of files, and it is FIXED in code:
    changing it moves every file to a different shard and re-uploads the part.
    """

    def __init__(self, name: str, directory: Path, pattern: str, hex_chars: int,
                 sqlite: bool = False):
        self.name = name
        self.directory = directory
        self.pattern = pattern
        self.hex_chars = hex_chars
        # The abstracts are one SQLite file, not a directory of entries. It stays a
        # Part so `--parts abstracts` and the manifest name it like everything
        # else; what differs is that it is pushed whole and pulled by MERGING rows.
        self.sqlite = sqlite

    def shard_of(self, filename: str) -> str:
        return cache_key(filename)[:self.hex_chars] if self.hex_chars else "all"


PARTS: dict[str, Part] = {p.name: p for p in [
    Part("llm", LLM_CACHE_DIR, "*.json", 1),
    Part("openalex", OA_CACHE_DIR, "*.json", 1),
    Part("openalex_xml", OA_XML_CACHE_DIR, "*", 0),
    Part("parse", PARSE_CACHE_DIR, "*.json", 0),
    Part("grobid", GROBID_CACHE_DIR, "*", 0),
    Part("doi_verify", DOI_VERIFY_CACHE_DIR, "*.json", 1),
    # The acquisition waterfall's documents (`*.pdf`) and the peer-review text some
    # tiers write beside them (`*.txt`). Sharded despite being only ~110 files:
    # they are ~195 MB, they are already-compressed streams so packing them buys
    # nothing, and a single shard would re-upload all of it to share one new PDF.
    Part("pdfs", PDF_CACHE_DIR, "*", 1),
]}

# The abstracts are not a directory any more (`shared/abstract_store.py`), so they
# are not a shard-packed Part: the whole SQLite file goes up gzipped, and a pull
# MERGES its rows into the local store rather than replacing the file. Merging is
# what keeps a puller's own abstracts — and lets the unproven-miss rule drop
# individual rows, which replacing a file could not do.
ABSTRACTS_PART = "abstracts"
_ABSTRACTS_REMOTE = "cache/abstracts/abstracts.sqlite.gz"

PARTS[ABSTRACTS_PART] = Part(ABSTRACTS_PART, ABSTRACT_DB_PATH.parent, "", 0,
                             sqlite=True)

# Abstract-source namespaces (the `<ns>:<id>` prefix fetch_abstracts checkpoints
# under) that need a credential, and the setting that grants it. A namespace absent
# from this map needs nothing, so a miss recorded there means the same thing on
# every machine and always imports.
_GATED_NAMESPACES = {
    "scopus": "ELSEVIER_API_KEY",
    "s2": "S2_API_KEY",
    "osf": "OSF_TOKEN",
}

_MISS = "__none__"


def capabilities() -> dict[str, bool]:
    """Which gated abstract sources THIS machine is configured for."""
    configured = {"ELSEVIER_API_KEY": bool(ELSEVIER_API_KEY),
                  "S2_API_KEY": bool(S2_API_KEY),
                  "OSF_TOKEN": bool(OSF_TOKEN)}
    return {ns: configured[var] for ns, var in _GATED_NAMESPACES.items()}


def parse_parts(spec: Optional[str]) -> list[Part]:
    """The parts named by a ``--parts`` spec, or all of them."""
    if not spec:
        return list(PARTS.values())
    chosen = []
    for name in (n.strip() for n in spec.split(",")):
        if not name:
            continue
        if name not in PARTS:
            raise ValueError(f"Unknown cache part {name!r}. Known: "
                             f"{', '.join(sorted(PARTS))}")
        chosen.append(PARTS[name])
    if not chosen:
        raise ValueError("--parts was given but named no part")
    return chosen


def _git_commit() -> str:
    """The checkout the push was made from, for the manifest. Never fatal."""
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001 — provenance is nice to have, not required
        return ""


def _shards_of(part: Part) -> dict[str, list[Path]]:
    """``{shard: [file, …]}`` for everything currently in *part*'s directory."""
    shards: dict[str, list[Path]] = {}
    if part.sqlite or not part.directory.exists():
        return shards
    for path in part.directory.glob(part.pattern):
        if path.is_file() and not path.name.endswith(".tmp"):
            shards.setdefault(part.shard_of(path.name), []).append(path)
    for files in shards.values():
        files.sort()
    return shards


def pack_shard(files: Iterable[Path]) -> bytes:
    """A deterministic ``.tar.gz`` of *files*, by basename.

    Deterministic — sorted members, every timestamp and owner zeroed — because the
    push decides what to transfer by comparing the shard's hash against the one in
    the remote manifest. A tar that embedded mtimes would hash differently on every
    run and re-upload all 256 abstract shards to share ten new files.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path in sorted(files, key=lambda p: p.name):
            info = tarfile.TarInfo(name=path.name)
            data = path.read_bytes()
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def abstract_source_evidence() -> dict[str, dict[str, int]]:
    """``{namespace: {"hits": n, "misses": n}}`` — what each source actually recovered.

    What a pull needs to know is not whether the producing machine had an Elsevier
    key configured — entitlement is IP-bound, so a configured key can still be
    refused — but whether it ever actually got an abstract out of that source.
    Zero hits alongside real misses is the signature of a machine that could not
    read it. One grouped query now; it used to be ~478k file reads.
    """
    return abstract_store.source_evidence()


def _unreliable_namespaces(manifest: Optional[dict]) -> set[str]:
    """Namespaces whose MISSES this machine must not import from *manifest*'s push.

    A gated source the producer recorded misses under and never got a single hit
    from, that this machine is configured for: their "no abstract" is unproven
    where ours would be a real fetch. Hits always import — a recovered abstract is
    a recovered abstract regardless of who had the key, which is the whole point
    of sharing.

    The misses-must-exist half is not redundant: without it, a push that carried
    no abstracts at all (`--parts llm`) reports zero hits everywhere and every
    gated namespace would read as unreliable.
    """
    if not manifest:
        return set()
    evidence = manifest.get("abstract_sources") or {}
    mine = capabilities()
    unreliable = set()
    for namespace in _GATED_NAMESPACES:
        counts = evidence.get(namespace) or {}
        if (int(counts.get("hits") or 0) == 0 < int(counts.get("misses") or 0)
                and mine.get(namespace)):
            unreliable.add(namespace)
    return unreliable


def cache_manifest(parts: list[Part], shard_hashes: dict[str, dict[str, str]],
                   previous: Optional[dict] = None) -> dict:
    """What this push contains and what produced it.

    The per-source evidence is recomputed only when the abstracts are actually
    being pushed — it is a full pass over ~478k files, and a `--parts llm` push
    should not pay 80 seconds for a number it is not changing. What that push must
    not do either is DROP the evidence an earlier one recorded, so an untouched
    value is carried forward from the remote manifest rather than written empty:
    a missing `abstract_sources` reads as "no source was readable".
    """
    pushing_abstracts = any(p.name == "abstracts" for p in parts)
    sources = (abstract_source_evidence() if pushing_abstracts
               else (previous or {}).get("abstract_sources") or {})
    return {
        "pushed_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        # Informational: the keys already fold these in, so a changed prompt MISSES
        # rather than mis-reads. This is here so a puller can see WHY it missed.
        "prompt_versions": {name: prompt_version(name) for name in (
            "build_target_prompt", "build_classify_prompt", "build_prescreen_prompt",
            "build_outcome_prompt", "build_repro_outcome_prompt")},
        "capabilities": capabilities(),
        "abstract_sources": sources,
        "parts": {part.name: shard_hashes.get(part.name, {}) for part in parts},
    }


def _remote_shard(part: Part, shard: str) -> str:
    return f"cache/{part.name}/{shard}.tar.gz"


def push_cache(parts: list[Part], repo: Optional[str] = None,
               dry_run: bool = False) -> int:
    """Pack each part into shards and upload the ones the remote does not have.

    Returns the number of shards uploaded (or, under *dry_run*, that would be). The
    manifest goes up LAST here — unlike the pool's, it is a description of what was
    uploaded rather than a claim on the repo, and a manifest naming shards that a
    failed push never delivered would make the next pull skip them.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = resolve_repo(repo, FLORA_POOL_REPO, "the API caches")
    token = require_token(hf)
    api = hf.HfApi(token=token)

    try:
        remote_manifest = read_remote_json(hf, repo_id, _MANIFEST, token)
    except RemoteReadError as exc:
        # A push only ever ADDS shards, so an unreadable manifest costs efficiency,
        # not correctness: assume nothing is there and re-upload.
        log.warning("%s Pushing every shard rather than assuming what is already there.",
                    exc)
        remote_manifest = None
    known = (remote_manifest or {}).get("parts") or {}

    uploads: list[tuple[str, bytes]] = []
    hashes: dict[str, dict[str, str]] = {}
    for part in parts:
        if part.sqlite:
            continue
        shards = _shards_of(part)
        if not shards:
            log.info("Cache push: %s is empty locally — nothing to send", part.name)
            continue
        files = changed = 0
        for shard, paths in sorted(shards.items()):
            blob = pack_shard(paths)
            digest = cache_key(blob.hex())
            hashes.setdefault(part.name, {})[shard] = digest
            files += len(paths)
            if (known.get(part.name) or {}).get(shard) != digest:
                uploads.append((_remote_shard(part, shard), blob))
                changed += 1
        log.info("Cache push: %s — %d file(s) in %d shard(s), %d to upload",
                 part.name, files, len(shards), changed)

    abstracts_sent = False
    if any(p.name == ABSTRACTS_PART for p in parts):
        abstracts_sent = push_abstracts(api, hf, repo_id, known, dry_run)
        digest = abstracts_digest()
        if digest:
            hashes[ABSTRACTS_PART] = {"db": digest}

    if not dry_run:
        if uploads:
            upload_batched(api, hf, repo_id, list(uploads), "Cache push",
                           FLORA_HF_COMMIT_BATCH)
        merged = {**known, **hashes}
        upload_batched(api, hf, repo_id,
                       [(_MANIFEST,
                         json.dumps(cache_manifest(parts, merged, remote_manifest),
                                    indent=1).encode("utf-8"))],
                       "Cache manifest", FLORA_HF_COMMIT_BATCH)

    log.info("Cache push%s: %d shard(s)%s uploaded to %s",
             " (dry run)" if dry_run else "", len(uploads),
             " + the abstract store" if abstracts_sent else "", repo_id)
    return len(uploads) + (1 if abstracts_sent else 0)


def _load_pull_state() -> dict[str, str]:
    try:
        return json.loads(_PULL_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no state is the first-pull case
        return {}


def _save_pull_state(state: dict[str, str]) -> None:
    try:
        _PULL_STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not record which shards were pulled (%s) — the next pull "
                    "will re-extract them, which is wasteful but harmless", exc)


def pull_cache(parts: list[Part], repo: Optional[str] = None, dry_run: bool = False,
               force: bool = False) -> int:
    """Download and unpack the shards this machine does not already have.

    A local file is never overwritten. Because keys are content-complete, a name
    that exists locally holds the same answer the remote entry does, so the only
    thing an overwrite could change is the mtime. Returns shards unpacked.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = resolve_repo(repo, FLORA_POOL_REPO, "the API caches")
    token = require_token(hf)

    try:
        manifest = read_remote_json(hf, repo_id, _MANIFEST, token)
    except RemoteReadError as exc:
        raise RuntimeError(
            f"{exc} The manifest is what says which sources the pushing machine could "
            "actually read, so pulling without it could import unproven misses. "
            "Re-run; the pull resumes.") from exc
    if not manifest:
        raise ValueError(f"No {_MANIFEST} in {repo_id} — nothing has been pushed yet "
                         "(run --push from the machine that has the caches).")

    unreliable = _unreliable_namespaces(manifest)
    if unreliable:
        log.warning("The pushing machine recovered no abstracts at all from %s, and this "
                    "one is configured for it — its misses there are being SKIPPED so "
                    "your run fetches them itself.", ", ".join(sorted(unreliable)))

    state = _load_pull_state()
    unpacked = 0
    if any(p.name == ABSTRACTS_PART for p in parts):
        if pull_abstracts(hf, repo_id, token, unreliable, dry_run) or dry_run:
            unpacked += 1
    for part in parts:
        if part.sqlite:
            continue
        for shard, digest in sorted((manifest.get("parts") or {}).get(part.name, {}).items()):
            remote = _remote_shard(part, shard)
            if not force and state.get(remote) == digest:
                continue
            if dry_run:
                unpacked += 1
                continue
            blob = _download_shard(hf, repo_id, token, remote)
            if blob is None:
                continue
            _unpack(part, blob, unreliable)
            state[remote] = digest
            unpacked += 1
            if unpacked % 25 == 0:
                _save_pull_state(state)
                log.info("Cache pull: %d shard(s) unpacked", unpacked)

    if not dry_run:
        _save_pull_state(state)

    log.info("Cache pull%s: %d shard(s) from %s", " (dry run)" if dry_run else "",
             unpacked, repo_id)
    return unpacked


def _download_shard(hf, repo_id: str, token: str, remote: str) -> Optional[bytes]:
    """The shard's bytes, or None when the manifest names one the repo does not hold.

    A manifest entry with no shard behind it is a push that was interrupted between
    its data and its manifest. That is worth saying and worth continuing past — the
    other shards are fine — but it is not worth failing the whole pull over.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = hf.hf_hub_download(repo_id=repo_id, filename=remote,
                                      repo_type=REPO_TYPE, token=token, local_dir=tmp)
            return Path(path).read_bytes()
    except Exception as exc:  # noqa: BLE001 — boundary: absence vs. a real failure
        if is_absent(hf, exc):
            log.warning("%s is in the manifest but not in the repo — skipping it", remote)
            return None
        raise RuntimeError(auth_hint(hf, repo_id, exc)) from exc


def _unpack(part: Part, blob: bytes, unreliable: set[str]) -> None:
    """Extract *blob*'s members into the part's directory, skipping what is there.

    Members are written by basename through an explicit `write_bytes`, never
    `tar.extractall`: the archives are ours, but a dataset repo is still input, and
    the one thing a cache sync must not do is write outside its cache directory.
    """
    part.directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = Path(member.name).name
            if not name or name != member.name:
                log.warning("Skipping %r in a %s shard — not a plain file name",
                            member.name, part.name)
                continue
            target = part.directory / name
            if target.exists():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            target.write_bytes(handle.read())


def push_abstracts(api, hf, repo_id: str, known: dict, dry_run: bool) -> bool:
    """Upload the abstract store, gzipped. True when bytes went (or would go).

    One file, not shards: the store is a single SQLite database, so there is
    nothing to shard on. The cost of that is granularity — a push moves the whole
    ~20 MB rather than one 110 KB shard — and it is the one axis on which this
    layout is worse than the directory it replaced.
    """
    if not ABSTRACT_DB_PATH.exists():
        log.info("Cache push: no abstract store at %s — nothing to send", ABSTRACT_DB_PATH)
        return False
    # Recent rows live in the write-ahead log until this folds them back in, and
    # the upload reads the FILE — without it a push ships a store missing exactly
    # the abstracts the run just recovered.
    abstract_store.checkpoint_wal()
    blob = gzip.compress(ABSTRACT_DB_PATH.read_bytes(), 6)
    digest = cache_key(blob.hex())
    rows, hits = abstract_store.count()
    if known.get(ABSTRACTS_PART, {}).get("db") == digest:
        log.info("Cache push: abstracts unchanged (%d rows, %d hits)", rows, hits)
        return False
    log.info("Cache push: abstracts — %d row(s), %d hit(s), %.1f MB packed",
             rows, hits, len(blob) / 1e6)
    if not dry_run:
        upload_batched(api, hf, repo_id, [(_ABSTRACTS_REMOTE, blob)], "Abstract store",
                       FLORA_HF_COMMIT_BATCH)
    return True


def abstracts_digest() -> Optional[str]:
    """The hash of the local store as it would be uploaded, or None if absent."""
    if not ABSTRACT_DB_PATH.exists():
        return None
    abstract_store.checkpoint_wal()
    return cache_key(gzip.compress(ABSTRACT_DB_PATH.read_bytes(), 6).hex())


def pull_abstracts(hf, repo_id: str, token: str, unreliable: set[str],
                   dry_run: bool) -> int:
    """Merge the remote abstract store into the local one. Returns rows written.

    Rows are MERGED, never file-replaced: a puller's own abstracts survive, an
    identifier already answered locally keeps its local answer, and an unproven
    miss can be dropped row by row — none of which replacing the file allows.
    """
    blob = _download_shard(hf, repo_id, token, _ABSTRACTS_REMOTE)
    if blob is None or dry_run:
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        remote_db = Path(tmp) / "remote.sqlite"
        remote_db.write_bytes(gzip.decompress(blob))
        connection = sqlite3.connect(remote_db)
        try:
            rows = [(ident, abstract) for ident, abstract in
                    connection.execute("SELECT ident, abstract FROM abstracts")
                    if not (abstract is None
                            and ident.split(":", 1)[0] in unreliable)]
        finally:
            connection.close()
    written = abstract_store.import_rows(rows)
    log.info("Cache pull: abstracts — %d row(s) offered, %d new (the rest were "
             "already answered locally)", len(rows), written)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Share the API caches through the private Hugging Face dataset repo, so "
            "a collaborator does not re-buy the LLM verdicts or re-crawl 500k "
            "abstracts. Entries are content-keyed, so a pulled answer is the answer "
            "this checkout would have computed, and a differing checkout misses."),
        epilog=(f"Parts: {', '.join(sorted(PARTS))} — all directories of "
                "content-keyed files except `abstracts`, which is one SQLite store "
                "merged row-by-row on pull. Repo id: --repo, else FLORA_POOL_REPO "
                "from .env. Token: HF_TOKEN (the repo is private)."))
    direction = parser.add_mutually_exclusive_group(required=True)
    direction.add_argument("--push", action="store_true",
                           help="Upload this machine's caches.")
    direction.add_argument("--pull", action="store_true",
                           help="Download and unpack the shared caches.")
    parser.add_argument("--parts", metavar="LIST", default=None,
                        help="Comma-separated parts (default: all).")
    parser.add_argument("--repo", metavar="ID", default=None,
                        help="Hugging Face dataset repo (default: FLORA_POOL_REPO).")
    parser.add_argument("--force", action="store_true",
                        help="--pull only: re-unpack shards already recorded as pulled.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would transfer, transfer nothing.")
    args = parser.parse_args()

    parts = parse_parts(args.parts)
    if args.force and not args.pull:
        parser.error("--force applies to --pull only")

    if args.push:
        n = push_cache(parts, repo=args.repo, dry_run=args.dry_run)
        print(f"{'Would upload' if args.dry_run else 'Uploaded'} {n} shard(s)")
    else:
        n = pull_cache(parts, repo=args.repo, dry_run=args.dry_run, force=args.force)
        print(f"{'Would unpack' if args.dry_run else 'Unpacked'} {n} shard(s)")


if __name__ == "__main__":
    main()
