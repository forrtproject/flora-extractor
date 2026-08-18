"""
Share the survivor pool and its text overlay through a private Hugging Face
dataset repo.

The pool (``search/snapshot_scan.py``) is what turns a Stage 2 rule change
from a 13-21 hour, 725 GB rescan into a local re-run over the pool. It is
~2-3 GB across ~2,446 parquet files — small enough to move, far too big to keep
in git, and the thing nobody should have to reproduce. This module pushes it to
a private HF dataset repo once and lets collaborators pull it back:

    python -m search.pool_sync --push
    python -m search.pool_sync --pull

Remote layout: the pool is FLAT on disk but SHARDED BY YEAR on the remote, using
the partition date already in each file name —
``part-2016-06-24-part_0000.parquet`` → ``2016/part-2016-06-24-part_0000.parquet``.
Two reasons: HF asks for fewer than 10k entries per folder (one flat folder of
2,446 files is fine today but not after a few re-scans), and a year prefix is
what makes ``--years`` a genuinely partial download rather than a filter applied
after fetching everything. A name whose date cannot be read goes to ``unknown/``
rather than being skipped — an unshareable file is worse than an unsorted one.

Both directions are idempotent and resumable: a file already present on the
other side with the same size is skipped, so an interrupted transfer is restarted
by re-running the same command.

**The text overlay travels with the pool**, under the remote's ``overlay/``
prefix, and both directions move it by default (``--no-overlay`` for the pool
alone, ``--overlay-only`` to publish a backfill without touching the pool). It
belongs here rather than in ``shared/cache_sync.py`` because it is not a cache:
``overlay_hash`` is one of the six routing-release inputs, so a collaborator
holding the pool without the overlay it was routed under mints a DIFFERENT
release id from the same specs, and every overlay-only rule (the
``osf-registration-*`` pair) matches nothing on their machine without saying so.

The overlay is a frozen release, so the transfer defends that: a push refuses an
unfrozen or stale-pointer overlay, both directions refuse a chunk whose name
matches on both sides but whose content does not, and a pull verifies every
downloaded chunk against the sha256 in the manifest its pointer names. A
name-with-different-content collision is two machines having appended their own
``overlay-0002.parquet``; there is no merge here, deliberately — resolving it
means rewriting chunks so each work id appears once (``overlay.validate()``),
which is a decision, not a transfer.

``huggingface_hub`` is imported inside the functions, not at module scope: it is
a pipeline-only dependency and read-only/web deployments must not need it.
"""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional, Union

from shared.config import (
    FLORA_HF_COMMIT_BATCH,
    FLORA_HF_PULL_WORKERS,
    FLORA_POOL_REPO,
    OVERLAY_DIR,
    SNAPSHOT_POOL_DIR,
    log,
)
from shared.hf import (
    REPO_TYPE as _REPO_TYPE,
    RemoteReadError,
    auth_hint as _auth_hint,
    is_auth_error as _is_auth_error,
    read_remote_json as _read_remote_json_hf,
    remote_sizes as _remote_sizes_hf,
    require_token as _require_token,
    resolve_repo as _resolve_repo_hf,
    upload_batched as _upload_batched_hf,
)
from search.snapshot_scan import (
    ledger_hash,
    load_ledger,
    read_pool_provenance,
    search_gate_fingerprint,
    write_pool_provenance,
)

_POOL_MANIFEST = "pool_manifest.json"

# The overlay's own remote prefix. It keeps the overlay's parquet chunks out of
# every listing that means "pool file" — `pull_pool` globs the repo for
# `.parquet`, and an overlay chunk landing flat in the pool directory would be
# read as a pool file by every consumer of it.
_OVERLAY_PREFIX = "overlay/"

# part-<YYYY>-<MM>-<DD>-<stem>.parquet — the name _pool_file_name() builds.
_POOL_NAME_RE = re.compile(r"^part-(\d{4})-\d{2}-\d{2}-")

_UNKNOWN_YEAR = "unknown"


def _remote_path(name: str) -> str:
    """The remote key for local pool file *name*: ``<year>/<name>``.

    The year comes from the partition date in the name; anything else lands under
    ``unknown/`` so that a file with an unexpected name is still shared (it is
    simply never selected by ``--years``).
    """
    match = _POOL_NAME_RE.match(name)
    return f"{match.group(1) if match else _UNKNOWN_YEAR}/{name}"


def _year_of(remote_path: str) -> str:
    """The shard folder of *remote_path* — its first path segment."""
    return remote_path.split("/", 1)[0]


def parse_years(spec: str) -> list[int]:
    """Parse a ``--years`` spec: comma list, ranges, or both ("2019,2021-2023")."""
    years: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            if not lo.strip().isdigit() or not hi.strip().isdigit():
                raise ValueError(f"Bad year range {part!r} — expected e.g. 2019-2021")
            start, end = int(lo), int(hi)
            if end < start:
                raise ValueError(f"Bad year range {part!r} — end is before start")
            years.extend(range(start, end + 1))
        else:
            if not part.isdigit():
                raise ValueError(f"Bad year {part!r} — expected e.g. 2019 or 2019-2021")
            years.append(int(part))
    if not years:
        raise ValueError("--years was given but named no year")
    return sorted(dict.fromkeys(years))


def _resolve_repo(repo: Optional[str]) -> str:
    """The dataset repo id to sync with, or a message saying how to supply one."""
    return _resolve_repo_hf(repo, FLORA_POOL_REPO, "the survivor pool")


def _read_remote_json(hf, repo_id: str, remote_path: str,
                      token: Optional[str]) -> Optional[dict]:
    return _read_remote_json_hf(hf, repo_id, remote_path, token)


def _upload_batched(api, hf, repo_id: str, uploads: list[tuple[str, Union[Path, bytes]]],
                    message: str) -> None:
    """Commit *uploads* in ``FLORA_HF_COMMIT_BATCH``-file commits."""
    _upload_batched_hf(api, hf, repo_id, uploads, message, FLORA_HF_COMMIT_BATCH)


def _remote_sizes(api, repo: str) -> dict[str, int]:
    """``{remote path: size in bytes}`` for every parquet already in the repo."""
    return _remote_sizes_hf(api, repo, ".parquet")


def _is_pool_file(remote_path: str) -> bool:
    """Whether *remote_path* is a POOL parquet rather than an overlay chunk."""
    return remote_path.endswith(".parquet") and not remote_path.startswith(_OVERLAY_PREFIX)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlay_remote_sizes(api, hf, repo_id: str) -> dict[str, int]:
    """``{remote path: size}`` under the overlay prefix alone.

    Scoped, where the pool's listing is not: this repo also holds ~2,400 pool
    parquet files, the API cache shards and one screen run's ~9,300 response blobs,
    and walking all of them to compare a dozen overlay files is the whole wall clock
    of an overlay-only command. An absent prefix is an empty map — the first-push
    case, and the case of a collaborator pulling before anyone has backfilled — but
    a token the repo will not answer for is raised as itself, because "no overlay
    here" and "you may not look" must never read the same.
    """
    try:
        # list() inside the try: the API returns a LAZY generator, so the request —
        # and the 404 that says the prefix is not there yet — happens on iteration,
        # not on the call, and a try around the call alone catches nothing.
        entries = list(api.list_repo_tree(repo_id, _OVERLAY_PREFIX.rstrip("/"),
                                          repo_type=_REPO_TYPE, recursive=True))
    except Exception as exc:  # noqa: BLE001 — boundary: absent prefix vs bad token
        if _is_auth_error(hf, exc):
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc
        log.info("No overlay in %s yet (%s)", repo_id, exc)
        return {}
    return {str(e.path): int(e.size) for e in entries
            if getattr(e, "path", None) and getattr(e, "size", None) is not None}


def pool_manifest(ledger: Optional[dict] = None) -> dict:
    """What a pushed pool was built from — the sidecar that makes a mixed pool visible.

    Two people scanning under different search gates produce pool files that look
    alike and are not: the rows one gate rejected were never written by either. The
    manifest is how the second push finds out before overwriting the first.
    """
    ledger = load_ledger() if ledger is None else ledger
    files = ledger.get("files", {}) or {}
    return {
        # Key name frozen: it is the REMOTE format, and pools already pushed under it
        # must keep comparing equal. The value is search_gate_fingerprint().
        "stage_a_fingerprint": search_gate_fingerprint(),
        "snapshot_date": ledger.get("snapshot_date", "") or "",
        "ledger_files": len(files),
        "ledger_records": sum(int((e or {}).get("record_count") or 0) for e in files.values()),
        "ledger_kept": sum(int((e or {}).get("kept") or 0) for e in files.values()),
        "ledger_hash": ledger_hash(ledger),
    }


def check_access(repo: Optional[str] = None) -> dict:
    """Prove, before anything expensive runs, that this box can publish to the repo.

    A snapshot scan takes hours and its whole value is the artifact at the end, so the
    worst possible failure is discovering a bad or read-scoped token after the scan.
    Nothing short of an actual write proves write access — an existing repo answers
    ``create_repo(exist_ok=True)`` happily for a read token — so this commits a tiny
    ``preflight.json`` naming who checked, when, and under which search gate.

    Returns the identity/repo facts it established; raises with instructions otherwise.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    api = hf.HfApi(token=token)

    try:
        who = api.whoami()
    except Exception as exc:  # noqa: BLE001 — boundary: turn 401 into instructions
        raise RuntimeError(f"Hugging Face rejected HF_TOKEN ({exc}). Create a token with "
                           "write access at https://huggingface.co/settings/tokens.") from exc

    payload = {
        "checked_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "by": who.get("name", "?"),
        "stage_a_fingerprint": search_gate_fingerprint(),
    }
    try:
        api.create_repo(repo_id, repo_type=_REPO_TYPE, private=True, exist_ok=True)
        api.create_commit(
            repo_id=repo_id, repo_type=_REPO_TYPE,
            operations=[hf.CommitOperationAdd(
                path_in_repo="preflight.json",
                path_or_fileobj=json.dumps(payload, indent=1).encode("utf-8"))],
            commit_message="Pre-flight write check")
    except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
        raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    log.info("Hugging Face pre-flight OK: %s can write to %s (private dataset)",
             payload["by"], repo_id)
    return {"repo": repo_id, **payload}


def push_pool(pool_dir: Path, repo: Optional[str] = None, dry_run: bool = False,
              force: bool = False) -> int:
    """Upload every parquet in *pool_dir* to the dataset repo, year-sharded.

    Files already on the remote at the same size are skipped, so this is safe to
    re-run after an interrupted push and cheap to re-run after a partial rescan.
    Uploads go up in batched commits (``_upload_batched``), preceded by the
    ``pool_manifest.json`` recording the gate this pool was scanned under.

    A remote manifest naming a DIFFERENT search-gate fingerprint stops the push unless
    *force*: mixing two gates' survivors gives a pool that is complete under neither,
    and nothing downstream could tell. A manifest that cannot be READ stops it too —
    an unanswered Hub is not an empty repo. Returns the number of files uploaded (or,
    under *dry_run*, that would be).
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    files = sorted(pool_dir.glob("*.parquet"))
    if not files:
        raise ValueError(f"No pool parquet files under {pool_dir}")

    api = hf.HfApi(token=token)
    if not dry_run:
        try:
            api.create_repo(repo_id, repo_type=_REPO_TYPE, private=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    manifest = pool_manifest()
    try:
        remote_manifest = _read_remote_json(hf, repo_id, _POOL_MANIFEST, token)
    except RemoteReadError as exc:
        raise RuntimeError(
            f"{exc} Until {_POOL_MANIFEST} can be read, this push cannot tell whether "
            f"{repo_id} already holds a pool scanned under a different search gate, so "
            "it refuses rather than risk overwriting one. Re-run the same command; it "
            "resumes.") from exc
    if remote_manifest:
        theirs = remote_manifest.get("stage_a_fingerprint")
        if theirs and theirs != manifest["stage_a_fingerprint"]:
            if not force:
                raise RuntimeError(
                    f"{repo_id} holds a pool scanned under a DIFFERENT search gate "
                    f"(remote {str(theirs)[:12]}, local {manifest['stage_a_fingerprint'][:12]}). "
                    "Pushing would mix two gates' survivors into one pool that is complete "
                    "under neither. Align the gate (_TOKEN_GATE / CONCEPT_IDS), push to a "
                    "different repo, or pass --force if you mean to replace the remote pool.")
            log.warning("--force: pushing over a pool scanned under a different search gate "
                        "(remote %s, local %s)", str(theirs)[:12],
                        manifest["stage_a_fingerprint"][:12])

    remote = _remote_sizes(api, repo_id)
    uploads: list[tuple[str, Union[Path, bytes]]] = []
    skipped = 0
    for path in files:
        target = _remote_path(path.name)
        if remote.get(target) == path.stat().st_size:
            skipped += 1
            continue
        uploads.append((target, path))

    uploaded = len(uploads)
    if not dry_run:
        # The manifest goes up FIRST, in its own commit, because it is the CLAIM on
        # this repo. Written last, an interrupted first push leaves a large pool with
        # no fingerprint at all, and the next push from a different search gate finds
        # nothing to conflict with and mixes itself in. Manifest-first inverts the
        # failure: what an interruption leaves is a fingerprinted but incomplete pool,
        # which re-running this same command completes and a conflicting gate's push
        # is now correctly refused against.
        if remote_manifest != manifest:
            _upload_batched(api, hf, repo_id,
                            [(_POOL_MANIFEST,
                              json.dumps(manifest, indent=1).encode("utf-8"))],
                            "Pool manifest")
        if uploads:
            _upload_batched(api, hf, repo_id, uploads, "Pool push")

    log.info("Pool push%s: %d uploaded, %d already present (%d local files -> %s, stage_a=%s)",
             " (dry run)" if dry_run else "", uploaded, skipped, len(files), repo_id,
             manifest["stage_a_fingerprint"][:12])
    return uploaded


def _warn_on_gate_mismatch(remote_manifest: Optional[dict], repo_id: str) -> None:
    """Say loudly when the remote pool was scanned under another search gate.

    A warning, not a refusal: pulling someone else's pool is a legitimate thing to
    want (it is the whole point of sharing one). What must never happen is doing it
    without knowing.
    """
    if not remote_manifest:
        log.info("No %s in %s — the remote pool does not record which gate it was "
                 "scanned under.", _POOL_MANIFEST, repo_id)
        return
    theirs = remote_manifest.get("stage_a_fingerprint")
    if theirs and theirs != search_gate_fingerprint():
        log.warning(
            "The pool in %s was scanned under a DIFFERENT search gate (remote %s, your "
            "checkout %s). Its files hold the survivors of THEIR _TOKEN_GATE/CONCEPT_IDS, "
            "and the rows their gate rejected are in no pool at all — re-admitting it "
            "locally cannot recover them. Pulling anyway.",
            repo_id, str(theirs)[:12], search_gate_fingerprint()[:12])


def pull_pool(pool_dir: Path, repo: Optional[str] = None,
              years: Optional[list[int]] = None, dry_run: bool = False) -> int:
    """Download the pool (or only *years*) into the flat *pool_dir*.

    Per-file downloads rather than a whole-repo snapshot: that is what makes
    ``--years`` partial, and each file is independently resumable and locally
    cached. Files already present at the remote's size are skipped, and the rest
    are fetched several at a time (see ``_download_pool_files``). Returns the
    number of files downloaded (or, under *dry_run*, that would be).

    Writes the local provenance sidecar (``snapshot_scan.POOL_PROVENANCE``) from the
    REMOTE manifest: for a pulled pool, the remote's ``stage_a_fingerprint`` is the
    authority on which gate admitted these rows, and nothing local is. Without it
    every routing run on this machine would attribute the pool to whatever gate this
    checkout happens to compute.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    api = hf.HfApi(token=token)

    try:
        # `_is_pool_file`, not `.endswith(".parquet")`: the overlay's chunks are
        # parquet too, and they are neither pool rows nor part of what completes the
        # pool. Downloaded here they would land flat beside the pool files and be
        # globbed as pool rows, and their names would inflate the expected file count
        # written into the provenance sidecar below — after which `pool_fingerprint()`
        # would read the pool as incomplete for good.
        remote_files = [f for f in hf.list_repo_files(repo_id, repo_type=_REPO_TYPE,
                                                      token=token)
                        if _is_pool_file(f)]
    except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
        raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    if years is not None:
        wanted = {str(y) for y in years}
        remote_files = [f for f in remote_files if _year_of(f) in wanted]
    if not remote_files:
        raise ValueError(
            f"No pool files in {repo_id}"
            + (f" for year(s) {', '.join(str(y) for y in years)}" if years else ""))

    try:
        remote_manifest = _read_remote_json(hf, repo_id, _POOL_MANIFEST, token)
    except RemoteReadError as exc:
        # A pull writes nothing anyone else depends on, so an unreadable manifest
        # costs provenance, not correctness — say so and carry on.
        log.warning("%s Pulling without knowing which search gate this pool was "
                    "scanned under.", exc)
        remote_manifest = None
    _warn_on_gate_mismatch(remote_manifest, repo_id)

    sizes = _remote_sizes(api, repo_id)
    pool_dir.mkdir(parents=True, exist_ok=True)
    wanted, skipped = [], 0
    for remote_file in sorted(remote_files):
        local = pool_dir / remote_file.rsplit("/", 1)[-1]
        if local.exists() and sizes.get(remote_file) == local.stat().st_size:
            skipped += 1
        else:
            wanted.append(remote_file)

    if not dry_run:
        # Sidecar FIRST, for the same reason the push writes its manifest first: it
        # is the claim about what completes this pool, and an interrupted pull must
        # leave a pool that can be SEEN to be short rather than one that fingerprints
        # as whole. The expected count is every file that will be here when this pull
        # finishes — the ones already local plus the ones selected — so a --years pull
        # into an existing pool does not shrink the claim.
        expected = {p.name for p in pool_dir.glob("*.parquet")}
        expected |= {f.rsplit("/", 1)[-1] for f in remote_files}
        # An unreadable remote manifest is an absence of information about the gate,
        # not evidence that there is none: writing `null` over a gate a scan or an
        # earlier pull already established would erase the pool's only account of
        # what admitted its rows, and no later run could tell it had been lost.
        gate = (remote_manifest or {}).get("stage_a_fingerprint") or None
        if gate is None:
            gate = (read_pool_provenance(pool_dir) or {}).get(
                "search_gate_fingerprint") or None
            if gate is not None:
                log.warning("Keeping the search gate already recorded beside %s (%s): the "
                            "remote manifest did not supply one, and an unknown gate must "
                            "not overwrite a known one.", pool_dir, str(gate)[:12])
        write_pool_provenance(pool_dir, gate, len(expected), f"pull:{repo_id}")
    if not dry_run and wanted:
        _download_pool_files(hf, repo_id, token, pool_dir, wanted, skipped)

    downloaded = len(wanted)
    log.info("Pool pull%s: %d downloaded, %d already present (%d remote files from %s -> %s)",
             " (dry run)" if dry_run else "", downloaded, skipped, len(remote_files),
             repo_id, pool_dir)
    return downloaded


def _download_pool_files(hf, repo_id: str, token: str, pool_dir: Path,
                         wanted: list[str], skipped: int) -> None:
    """Fetch *wanted* into the flat *pool_dir*, several files at a time.

    Concurrent rather than serial because a pool pull is latency-bound, not
    bandwidth-bound: each file costs a full auth + CDN-redirect round trip
    before its first byte, and a 2,446-file pool spends about half its wall
    clock waiting for one of those with the link otherwise idle. Still one
    ``hf_hub_download`` per file, so ``--years`` stays a partial download and
    every file is independently resumable and locally cached.
    """
    done = 0
    counted = threading.Lock()

    def fetch(remote_file: str) -> None:
        got = Path(hf.hf_hub_download(repo_id=repo_id, filename=remote_file,
                                      repo_type=_REPO_TYPE, token=token,
                                      local_dir=str(pool_dir)))
        # local_dir reproduces the remote's year folder; the pool itself is flat,
        # because the pool row builder globs one directory.
        local = pool_dir / remote_file.rsplit("/", 1)[-1]
        if got.resolve() != local.resolve():
            shutil.move(str(got), str(local))
        nonlocal done
        with counted:
            done += 1
            if done % 50 == 0:
                log.info("Pool pull: %d/%d downloaded, %d already present",
                         done, len(wanted), skipped)

    workers = min(FLORA_HF_PULL_WORKERS, len(wanted))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, f) for f in wanted]
        try:
            for future in as_completed(futures):
                future.result()
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            # A worker's failure must reach the operator as the same actionable
            # error a serial pull raised; swallowed here it would read as a short
            # but successful pull.
            for pending in futures:
                pending.cancel()
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    # Once, after every worker is done: racing threads must not each try to
    # remove the same year folder.
    for shard in {pool_dir / _year_of(f) for f in wanted}:
        if shard.is_dir() and not any(shard.iterdir()):
            shard.rmdir()


# ---------------------------------------------------------------------------
# The text overlay
# ---------------------------------------------------------------------------


def _overlay_mod():
    """`filter.engine.overlay`, imported late.

    Its module scope pulls in pyarrow and the engine's row builders, and it imports
    `search.fetch_abstracts` — importing it at this module's scope would both cost
    every caller that dependency and close an import cycle.
    """
    from filter.engine import overlay
    return overlay


def _local_overlay(overlay_dir: Path) -> tuple[dict[str, str], list[Path], str]:
    """The frozen local overlay: `{chunk name: sha256}`, its manifest files, its hash.

    Raises unless the directory holds chunks AND the frozen pointer describes the
    bytes that are there now. A backfill appends chunks; the pointer only catches up
    at `freeze()`, so "a pointer exists" is not "this overlay is frozen".
    """
    ov = _overlay_mod()
    overlay_dir = Path(overlay_dir)
    chunks = ov.chunk_paths(overlay_dir)
    if not chunks:
        raise ValueError(f"No {ov.CHUNK_GLOB} files under {overlay_dir}")

    pointer = overlay_dir / ov.POINTER_NAME
    if not pointer.exists():
        raise RuntimeError(
            f"{overlay_dir} holds {len(chunks)} overlay chunk(s) but no "
            f"{ov.POINTER_NAME}. Publishing text nobody froze would give collaborators "
            "a corpus no release id names — run the freeze first:\n"
            "  .venv/bin/python -m filter.engine.backfill --worklist W --freeze")

    pointed = json.loads(pointer.read_text(encoding="utf-8"))["overlay_hash"]
    live = ov.overlay_hash(overlay_dir)
    if live != pointed:
        raise RuntimeError(
            f"{overlay_dir} has changed since it was frozen (pointer names "
            f"{pointed[:12]}, the files hash to {live[:12]}) — a backfill appended "
            "after the last freeze. Re-freeze, so what goes up is a release:\n"
            "  .venv/bin/python -m filter.engine.backfill --worklist W --freeze")

    manifests = sorted(overlay_dir.glob("overlay_manifest-*.json"))
    return {p.name: _sha256(p) for p in chunks}, manifests, live


def _remote_overlay(hf, repo_id: str, token: Optional[str]) -> tuple[Optional[dict], str]:
    """The frozen manifest the remote pointer names, and that pointer's hash.

    `(None, "")` means the repo holds no pushed overlay — the first-push case, and
    the case a collaborator pulling before anyone has backfilled is in.
    """
    ov = _overlay_mod()
    pointer = _read_remote_json(hf, repo_id, _OVERLAY_PREFIX + ov.POINTER_NAME, token)
    if not pointer:
        return None, ""
    manifest = _read_remote_json(hf, repo_id,
                                 _OVERLAY_PREFIX + pointer["manifest"], token)
    return manifest, pointer.get("overlay_hash", "")


def _overlay_conflicts(local: dict[str, str], remote_manifest: Optional[dict],
                       remote_sizes: dict[str, int],
                       overlay_dir: Path) -> list[str]:
    """Chunk names that exist on both sides holding different bytes.

    Overlay chunks are sequence-named, not content-keyed, so two machines that both
    backfilled have both written an `overlay-0002.parquet` and neither is the other.
    Prefer the manifest's sha256; with no readable manifest — an interrupted first
    push — size is all there is, so equal sizes RESUME silently and cannot prove
    equal content.
    """
    if remote_manifest:
        theirs = {f["name"]: f["sha256"] for f in remote_manifest.get("files", [])}
        return sorted(name for name, digest in local.items()
                      if name in theirs and theirs[name] != digest)
    return sorted(name for name in local
                  if _OVERLAY_PREFIX + name in remote_sizes
                  and remote_sizes[_OVERLAY_PREFIX + name]
                  != (overlay_dir / name).stat().st_size)


def push_overlay(overlay_dir: Path, repo: Optional[str] = None, dry_run: bool = False,
                 force: bool = False) -> int:
    """Upload the frozen text overlay to the dataset repo under ``overlay/``.

    Refuses an unfrozen or stale overlay (`_local_overlay`) and a chunk the remote
    holds under the same name with different bytes (`_overlay_conflicts`) unless
    *force*. Returns the number of files uploaded, or under *dry_run* that would be.

    The POINTER goes up LAST, alone — the opposite order to `push_pool`'s manifest,
    and for the opposite reason. The pool's manifest is a claim on the repo that must
    exist before the data it fingerprints. The overlay's pointer is what makes the
    chunks READABLE as a release, so an interrupted push must leave the previous
    pointer naming a complete set of chunks rather than a new one naming files that
    are not all there yet.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    overlay_dir = Path(overlay_dir)
    local, manifests, digest = _local_overlay(overlay_dir)
    ov = _overlay_mod()

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    api = hf.HfApi(token=token)
    if not dry_run:
        try:
            api.create_repo(repo_id, repo_type=_REPO_TYPE, private=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    try:
        remote_manifest, remote_hash = _remote_overlay(hf, repo_id, token)
    except RemoteReadError as exc:
        raise RuntimeError(
            f"{exc} Until the remote overlay pointer can be read, this push cannot tell "
            f"whether {repo_id} already holds a different overlay, so it refuses rather "
            "than overwrite one. Re-run the same command; it resumes.") from exc

    if remote_hash == digest:
        log.info("Overlay push: %s already holds overlay %s (%d chunk(s))",
                 repo_id, digest[:12], len(local))
        return 0

    sizes = _overlay_remote_sizes(api, hf, repo_id)
    conflicts = _overlay_conflicts(local, remote_manifest, sizes, overlay_dir)
    if conflicts:
        detail = ", ".join(conflicts[:5]) + (" …" if len(conflicts) > 5 else "")
        if not force:
            raise RuntimeError(
                f"{repo_id} holds {len(conflicts)} overlay chunk(s) under the same name(s) "
                f"with different content ({detail}). Two machines have each appended their "
                "own chunk at that sequence number, and pushing would replace text a "
                "collaborator's release id already names. There is no merge here: rewrite "
                "the chunks so every work id appears exactly once (`overlay.validate()`), "
                "re-freeze, and push that — or pass --force to replace the remote overlay.")
        log.warning("--force: replacing %d remote overlay chunk(s) of the same name (%s)",
                    len(conflicts), detail)

    uploads: list[tuple[str, Union[Path, bytes]]] = []
    for name in sorted(local):
        path = overlay_dir / name
        if sizes.get(_OVERLAY_PREFIX + name) == path.stat().st_size and name not in conflicts:
            continue
        uploads.append((_OVERLAY_PREFIX + name, path))
    # Every frozen manifest, not just the current one: each names the exact bytes of
    # the overlay a past release id was routed under, and a puller with only the
    # newest cannot check an older release's text against anything.
    for path in manifests:
        if sizes.get(_OVERLAY_PREFIX + path.name) != path.stat().st_size:
            uploads.append((_OVERLAY_PREFIX + path.name, path))

    if not dry_run and uploads:
        _upload_batched(api, hf, repo_id, uploads, "Overlay push")
    pointer = overlay_dir / ov.POINTER_NAME
    if not dry_run:
        _upload_batched(api, hf, repo_id,
                        [(_OVERLAY_PREFIX + ov.POINTER_NAME, pointer.read_bytes())],
                        "Overlay pointer")

    log.info("Overlay push%s: %d file(s) uploaded, overlay %s (%d chunk(s) -> %s)",
             " (dry run)" if dry_run else "", len(uploads) + 1, digest[:12],
             len(local), repo_id)
    return len(uploads) + 1


def pull_overlay(overlay_dir: Path, repo: Optional[str] = None, dry_run: bool = False,
                 force: bool = False) -> int:
    """Download the frozen text overlay into *overlay_dir*; return the files fetched.

    Every downloaded chunk is checked against the sha256 in the manifest the remote
    pointer names, and a mismatch is an error with the bad file removed: an overlay
    that is not the bytes its hash names would route rows under text nobody froze,
    and the release id would not show it.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    ov = _overlay_mod()
    overlay_dir = Path(overlay_dir)
    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    api = hf.HfApi(token=token)

    remote_files = sorted(_overlay_remote_sizes(api, hf, repo_id))
    if not remote_files:
        log.info("No overlay in %s — nobody has pushed one, so the pool routes on its "
                 "own text and every overlay-only rule matches nothing.", repo_id)
        return 0

    manifest, remote_hash = _remote_overlay(hf, repo_id, token)
    if not manifest:
        raise RuntimeError(
            f"{repo_id} holds overlay files but no readable frozen manifest — an "
            "interrupted push. Nothing here can say which bytes belong to the overlay, "
            "so this pull refuses. Ask whoever pushed to re-run "
            "`.venv/bin/python -m search.pool_sync --push --overlay-only`.")
    expected = {f["name"]: f["sha256"] for f in manifest.get("files", [])}

    diverged = sorted(name for name, digest in expected.items()
                      if (overlay_dir / name).exists()
                      and _sha256(overlay_dir / name) != digest)
    if diverged and not force:
        raise RuntimeError(
            f"{overlay_dir} holds {len(diverged)} chunk(s) the remote overlay names with "
            f"different content ({', '.join(diverged[:5])}). Your backfill and theirs "
            "each wrote a chunk at that sequence number. Pulling would overwrite yours. "
            "Move your overlay aside and pull into a clean directory, or pass --force to "
            "take the remote's.")
    if diverged:
        log.warning("--force: overwriting %d local overlay chunk(s) with the remote's (%s)",
                    len(diverged), ", ".join(diverged[:5]))

    extra = sorted(p.name for p in ov.chunk_paths(overlay_dir) if p.name not in expected)
    if extra:
        log.warning("%s holds %d chunk(s) the remote overlay does not (%s). After this "
                    "pull the local overlay hash is NOT %s, and routing here mints a "
                    "different release id from theirs.",
                    overlay_dir, len(extra), ", ".join(extra[:5]), remote_hash[:12])

    log.info("Overlay pull: %s — %s row(s), %d chunk(s), keyed against pool %s",
             remote_hash[:12], manifest.get("rows", "?"), len(expected),
             str(manifest.get("parent_pool_manifest_hash"))[:12])

    # A chunk the frozen manifest does not name is never taken. The push writes its
    # chunks before its pointer, so an interrupted push of a RE-frozen overlay leaves
    # newer chunks under the older pointer; taken here they would be read by
    # `load_overlay()` — which globs — while `overlay_manifest_hash()` still reported
    # the pointer's hash, and routing would stamp a release id that does not name the
    # text it read. Nothing downstream could see that.
    unnamed = sorted(f[len(_OVERLAY_PREFIX):] for f in remote_files
                     if fnmatch(f[len(_OVERLAY_PREFIX):], ov.CHUNK_GLOB)
                     and f[len(_OVERLAY_PREFIX):] not in expected)
    if unnamed:
        log.warning("%d remote chunk(s) are not named by the frozen manifest (%s) — an "
                    "interrupted newer push. Not taken; re-pull once it finishes.",
                    len(unnamed), ", ".join(unnamed[:5]))

    wanted = [f for f in sorted(remote_files)
              if f[len(_OVERLAY_PREFIX):] not in unnamed
              and (not (overlay_dir / f[len(_OVERLAY_PREFIX):]).exists()
                   or f[len(_OVERLAY_PREFIX):] in diverged
                   or f.endswith(ov.POINTER_NAME))]
    if dry_run:
        log.info("Overlay pull (dry run): %d file(s) would download from %s",
                 len(wanted), repo_id)
        return len(wanted)

    overlay_dir.mkdir(parents=True, exist_ok=True)
    # The pointer last, for the reason the push writes it last: until the chunks it
    # names are all here, an overlay directory with a pointer is a release that
    # cannot be read, and `route` would refuse rather than tell you what is missing.
    for remote_file in sorted(wanted, key=lambda f: f.endswith(ov.POINTER_NAME)):
        name = remote_file[len(_OVERLAY_PREFIX):]
        got = Path(hf.hf_hub_download(repo_id=repo_id, filename=remote_file,
                                      repo_type=_REPO_TYPE, token=token,
                                      local_dir=str(overlay_dir)))
        local = overlay_dir / name
        if got.resolve() != local.resolve():
            shutil.move(str(got), str(local))
        if name in expected and _sha256(local) != expected[name]:
            local.unlink()
            raise RuntimeError(
                f"{name} downloaded from {repo_id} does not match the sha256 its frozen "
                "manifest names. The remote overlay is not the release its hash claims; "
                "the file has been removed. Re-run the pull, and if it fails again the "
                "overlay must be re-frozen and re-pushed at the source.")

    shard = overlay_dir / _OVERLAY_PREFIX.rstrip("/")
    if shard.is_dir() and not any(shard.iterdir()):
        shard.rmdir()

    log.info("Overlay pull: %d file(s) downloaded, overlay %s (%s -> %s)",
             len(wanted), remote_hash[:12], repo_id, overlay_dir)
    return len(wanted)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Share the Stage 1 snapshot artifacts through a private Hugging Face "
            "dataset repo. --push/--pull move the survivor pool (year-sharded "
            "remotely, flat locally) so a collaborator can work over the pool "
            "without repeating the 13-21 hour scan, AND the frozen text overlay "
            "(remote overlay/), which is a routing-release input: without it the "
            "same pool and specs mint a different release id."),
        epilog=(
            "Repo id: --repo, else FLORA_POOL_REPO from .env. Token: HF_TOKEN from .env "
            f"(the repo is private). Default pool dir: {SNAPSHOT_POOL_DIR} "
            f"(override with --pool-dir or FLORA_POOL_DIR); default overlay dir: "
            f"{OVERLAY_DIR} (--overlay-dir or FLORA_OVERLAY_DIR)."),
    )
    direction = parser.add_mutually_exclusive_group(required=True)
    direction.add_argument("--push", action="store_true",
                           help="Upload the local pool and text overlay to the dataset repo.")
    direction.add_argument("--pull", action="store_true",
                           help="Download the pool and text overlay from the dataset repo.")
    direction.add_argument("--check-access", action="store_true",
                           help="Prove this machine can write to the dataset repo (commits a "
                                "small preflight.json). Run BEFORE a long scan.")
    parser.add_argument("--pool-dir", metavar="PATH", default=None,
                        help=f"Local pool directory (default: {SNAPSHOT_POOL_DIR}).")
    parser.add_argument("--overlay-dir", metavar="PATH", default=None,
                        help=f"Local text overlay directory (default: {OVERLAY_DIR}).")
    overlay = parser.add_mutually_exclusive_group()
    overlay.add_argument("--no-overlay", action="store_true",
                         help="Move the pool alone, leaving the text overlay untouched.")
    overlay.add_argument("--overlay-only", action="store_true",
                         help="Move the text overlay alone — how a backfill is published "
                              "without re-checking a 2-3 GB pool.")
    parser.add_argument("--force", action="store_true",
                        help="Push over a remote pool scanned under a different search "
                             "gate, or replace an overlay chunk the other side holds "
                             "under the same name with different content (either "
                             "direction).")
    parser.add_argument("--repo", metavar="ID", default=None,
                        help="Hugging Face dataset repo, e.g. my-org/flora-survivor-pool "
                             "(default: FLORA_POOL_REPO).")
    parser.add_argument("--years", metavar="SPEC", default=None,
                        help="--pull only: restrict to these partition years. "
                             "Comma list and/or ranges, e.g. 2019,2021-2023.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would transfer, transfer nothing.")
    args = parser.parse_args()

    pool_dir = Path(args.pool_dir) if args.pool_dir else SNAPSHOT_POOL_DIR
    overlay_dir = Path(args.overlay_dir) if args.overlay_dir else OVERLAY_DIR
    years = parse_years(args.years) if args.years else None
    if years is not None and not args.pull:
        parser.error("--years applies to --pull only")
    if args.check_access and (args.no_overlay or args.overlay_only):
        parser.error("--no-overlay/--overlay-only apply to --push and --pull only")

    if args.check_access:
        info = check_access(repo=args.repo)
        print(f"Hugging Face OK: {info['by']} can write to {info['repo']}")
        return

    verb = "upload" if args.push else "download"
    if not args.overlay_only:
        if args.push:
            n = push_pool(pool_dir, repo=args.repo, dry_run=args.dry_run, force=args.force)
        else:
            n = pull_pool(pool_dir, repo=args.repo, years=years, dry_run=args.dry_run)
        print(f"{'Would ' + verb if args.dry_run else verb.capitalize() + 'ed'} "
              f"{n} pool file(s)")
    if not args.no_overlay:
        # An absent overlay is not a failure on either side: the pool predates the
        # overlay, and a collaborator who has never backfilled has nothing to push.
        # Only a present-but-broken one stops the command.
        if args.push:
            local = Path(overlay_dir)
            if not local.exists() or not _overlay_mod().chunk_paths(local):
                print(f"No text overlay under {overlay_dir} — nothing to upload")
                return
            n = push_overlay(local, repo=args.repo, dry_run=args.dry_run,
                             force=args.force)
        else:
            n = pull_overlay(overlay_dir, repo=args.repo, dry_run=args.dry_run,
                             force=args.force)
        print(f"{'Would ' + verb if args.dry_run else verb.capitalize() + 'ed'} "
              f"{n} overlay file(s)")


if __name__ == "__main__":
    main()
