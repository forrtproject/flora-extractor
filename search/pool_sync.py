"""
Share the Stage A survivor pool through a private Hugging Face dataset repo.

The pool (``search/snapshot_scan.py``) is what turns a Stage B vocabulary change
from a 13-21 hour, 725 GB rescan into a local ``--admit-from-pool`` run. It is
~2-3 GB across ~2,446 parquet files — small enough to move, far too big to keep
in git, and the thing nobody should have to reproduce. This module pushes it to
a private HF dataset repo once and lets collaborators pull it back:

    python -m search.pool_sync --push
    python -m search.pool_sync --pull                 # then --admit-from-pool

Most collaborators want the CORPUS, not the freedom to re-admit it, and paying 15
minutes of Stage B plus a 2-3 GB pool download for a result that is identical for
everyone is waste. So the Stage B pass is done once and its output shared as a
prebuilt candidates artifact — chunked parquet under ``builds/<build_hash>/`` with
a manifest naming everything the rows depend on:

    python -m search.pool_sync --build-candidates     # pool  -> build/
    python -m search.pool_sync --push-build           # build -> builds/<hash>/
    python -m search.pool_sync --pull-build           # -> merged into candidates.csv

``build_hash`` names the snapshot date, both gate fingerprints, what the ledger
consumed and the row-builder version, so a build is addressed by exactly what
produced it. On pull, a build whose Stage B fingerprint or row-builder version
differs from the local checkout is merged with a loud warning: the rows are then
someone else's admission decisions, which is precisely what passing a CSV around
would never have told you.

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

``huggingface_hub`` is imported inside the functions, not at module scope: it is
a pipeline-only dependency and read-only/web deployments must not need it.
"""

import argparse
import datetime
import json
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, Union

from shared.config import (
    DATA_DIR,
    FLORA_HF_COMMIT_BATCH,
    FLORA_HF_PULL_WORKERS,
    FLORA_POOL_REPO,
    SNAPSHOT_BUILD_DIR,
    SNAPSHOT_POOL_DIR,
    log,
)
from search.snapshot_scan import (
    ROW_BUILDER_VERSION,
    build_candidates,
    ledger_hash,
    load_ledger,
    stage_a_fingerprint,
    stage_b_fingerprint,
)

_REPO_TYPE = "dataset"

_POOL_MANIFEST = "pool_manifest.json"
_BUILDS_PREFIX = "builds"
_LATEST_BUILD = f"{_BUILDS_PREFIX}/latest.json"
_BUILD_MANIFEST = "manifest.json"

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
    resolved = (repo or FLORA_POOL_REPO or "").strip()
    if not resolved:
        raise ValueError(
            "No Hugging Face dataset repo for the survivor pool. Set FLORA_POOL_REPO "
            "in your .env (e.g. FLORA_POOL_REPO=my-org/flora-survivor-pool) or pass "
            "--repo <owner>/<name>.")
    return resolved


def _require_token(hf) -> str:
    """The HF token, or a message naming the env var that is missing.

    ``huggingface_hub`` resolves ``HF_TOKEN`` (and a cached ``hf auth login``) by
    itself; this only turns "no token" into an actionable error before a private
    repo answers with a bare 401.
    """
    token = hf.get_token()
    if not token:
        raise ValueError(
            "No Hugging Face token. The survivor pool lives in a PRIVATE dataset repo, "
            "so a token is required: set HF_TOKEN in your .env (create one at "
            "https://huggingface.co/settings/tokens with write access for --push, "
            "read access for --pull) or run `hf auth login`.")
    return token


class RemoteReadError(RuntimeError):
    """A remote file could not be read AND its absence was not established.

    The distinction matters at exactly one place — the Stage A gate check in
    ``push_pool`` — where "there is no manifest" and "I could not read the manifest"
    lead to opposite actions.
    """


def _status_of(exc: Exception) -> Optional[int]:
    """The HTTP status behind *exc*, when it carries a response."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return int(code) if isinstance(code, int) else None


def _error_class(hf, name: str) -> tuple:
    """``(cls,)`` for the huggingface_hub error *name*, or ``()`` if this version
    does not expose it. Looked up on the passed module so the class identity always
    matches the one that raised."""
    for holder in (getattr(hf, "errors", None), getattr(hf, "utils", None), hf):
        cls = getattr(holder, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return (cls,)
    return ()


def _is_absent(hf, exc: Exception) -> bool:
    """True only when the Hub actually said the entry is not there.

    ``LocalEntryNotFoundError`` subclasses ``EntryNotFoundError`` but means the Hub
    could not be reached at all, and ``GatedRepoError`` subclasses
    ``RepositoryNotFoundError`` but means this token may not look — neither
    establishes anything about what the repo holds, so both are excluded first.
    """
    if isinstance(exc, _error_class(hf, "LocalEntryNotFoundError")
                  + _error_class(hf, "GatedRepoError")):
        return False
    absent = (_error_class(hf, "EntryNotFoundError")
              + _error_class(hf, "RepositoryNotFoundError")
              + _error_class(hf, "RevisionNotFoundError"))
    return isinstance(exc, absent) or _status_of(exc) == 404


def _is_auth_error(hf, exc: Exception) -> bool:
    """Whether *exc* is the kind of failure a different HF_TOKEN would fix.

    ``RepositoryNotFoundError`` counts: a private repo this token cannot see is
    reported as missing, so "no such repo" and "not yours" are the same answer.
    """
    if _status_of(exc) in (401, 403):
        return True
    return isinstance(exc, _error_class(hf, "GatedRepoError")
                      + _error_class(hf, "RepositoryNotFoundError")
                      + _error_class(hf, "DisabledRepoError")
                      + _error_class(hf, "LocalTokenNotFoundError"))


def _auth_hint(hf, repo: str, exc: Exception) -> str:
    """The message to raise for *exc* — with the token advice only when it applies.

    A 503 and a 403 need different things from the operator, and sending someone
    after their token while the Hub is down costs them the one thing they had:
    knowing what actually failed.
    """
    if not _is_auth_error(hf, exc):
        return (f"Hugging Face request to {repo!r} failed "
                f"({type(exc).__name__}: {exc}).")
    return (f"Hugging Face refused access to {repo!r} ({exc}). Check that HF_TOKEN belongs "
            f"to an account with access to this private repo, and that it has write "
            f"permission if you are pushing.")


def _remote_sizes(api, repo: str) -> dict[str, int]:
    """``{remote path: size in bytes}`` for every parquet already in the repo.

    One tree listing instead of a metadata call per file — with ~2,446 files the
    per-file form would cost more round trips than the transfer it is meant to skip.
    An empty repo (or one that does not exist yet) is an empty map, not an error:
    the first push creates it.
    """
    try:
        entries = api.list_repo_tree(repo, repo_type=_REPO_TYPE, recursive=True)
    except Exception as exc:  # noqa: BLE001 — a missing repo is the first-push case
        log.warning("Could not list %s (%s) — treating the remote as empty, so nothing "
                    "will be skipped and every file transfers again", repo, exc)
        return {}
    sizes: dict[str, int] = {}
    for entry in entries:
        path = getattr(entry, "path", None)
        size = getattr(entry, "size", None)
        if path and str(path).endswith(".parquet") and size is not None:
            sizes[str(path)] = int(size)
    return sizes


def _upload_batched(api, hf, repo_id: str, uploads: list[tuple[str, Union[Path, bytes]]],
                    message: str) -> None:
    """Commit *uploads* (``[(remote path, local path or bytes), …]``) in batches.

    One ``upload_file`` per file is one COMMIT per file: a single pool push (~2,446
    files) would put the repo straight at the few-thousand-commit mark where Hugging
    Face says repo UX degrades, and every re-push would add as many again. Batching
    into ``FLORA_HF_COMMIT_BATCH``-file commits turns that into a couple of dozen.
    """
    for start in range(0, len(uploads), FLORA_HF_COMMIT_BATCH):
        batch = uploads[start:start + FLORA_HF_COMMIT_BATCH]
        operations = [hf.CommitOperationAdd(path_in_repo=target, path_or_fileobj=payload
                                            if isinstance(payload, bytes) else str(payload))
                      for target, payload in batch]
        try:
            api.create_commit(repo_id=repo_id, repo_type=_REPO_TYPE, operations=operations,
                              commit_message=f"{message} ({start + 1}-{start + len(batch)} "
                                             f"of {len(uploads)})")
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc
        log.info("%s: %d/%d file(s) committed", message, start + len(batch), len(uploads))


def _read_remote_json(hf, repo_id: str, remote_path: str, token: Optional[str]) -> Optional[dict]:
    """Fetch a small JSON file from the repo, or None when it is genuinely not there.

    A missing file is the normal first-push/first-build case, not an error. Any OTHER
    failure — a 503, a rate limit, a dropped connection, malformed JSON — says nothing
    about what the repo holds, and reading it as "not there" is what would let a
    transient blip walk a push straight past the Stage A gate check. Those raise.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = hf.hf_hub_download(repo_id=repo_id, filename=remote_path,
                                      repo_type=_REPO_TYPE, token=token, local_dir=tmp)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:  # noqa: BLE001 — boundary: absence vs. any other failure
        if _is_absent(hf, exc):
            log.debug("No %s in %s (%s)", remote_path, repo_id, exc)
            return None
        raise RemoteReadError(
            f"Could not read {remote_path} from {repo_id} ({type(exc).__name__}: {exc}). "
            "That is not evidence it is absent, so nothing was assumed about it — "
            "retry once Hugging Face answers.") from exc


def pool_manifest(ledger: Optional[dict] = None) -> dict:
    """What a pushed pool was built from — the sidecar that makes a mixed pool visible.

    Two people scanning under different Stage A gates produce pool files that look
    alike and are not: the rows one gate rejected were never written by either. The
    manifest is how the second push finds out before overwriting the first.
    """
    ledger = load_ledger() if ledger is None else ledger
    files = ledger.get("files", {}) or {}
    return {
        "stage_a_fingerprint": stage_a_fingerprint(),
        "stage_b_fingerprint": stage_b_fingerprint(),
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
    ``preflight.json`` naming who checked, when, and under which Stage A gate.

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
        "stage_a_fingerprint": stage_a_fingerprint(),
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

    A remote manifest naming a DIFFERENT Stage A fingerprint stops the push unless
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
            f"{repo_id} already holds a pool scanned under a different Stage A gate, so "
            "it refuses rather than risk overwriting one. Re-run the same command; it "
            "resumes.") from exc
    if remote_manifest:
        theirs = remote_manifest.get("stage_a_fingerprint")
        if theirs and theirs != manifest["stage_a_fingerprint"]:
            if not force:
                raise RuntimeError(
                    f"{repo_id} holds a pool scanned under a DIFFERENT Stage A gate "
                    f"(remote {str(theirs)[:12]}, local {manifest['stage_a_fingerprint'][:12]}). "
                    "Pushing would mix two gates' survivors into one pool that is complete "
                    "under neither. Align the gate (_TOKEN_GATE / CONCEPT_IDS), push to a "
                    "different repo, or pass --force if you mean to replace the remote pool.")
            log.warning("--force: pushing over a pool scanned under a different Stage A gate "
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
        # no fingerprint at all, and the next push from a different Stage A gate finds
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
    """Say loudly when the remote pool was scanned under another Stage A gate.

    A warning, not a refusal: pulling someone else's pool is a legitimate thing to
    want (it is the whole point of sharing one). What must never happen is doing it
    without knowing.
    """
    if not remote_manifest:
        log.info("No %s in %s — the remote pool does not record which gate it was "
                 "scanned under.", _POOL_MANIFEST, repo_id)
        return
    theirs = remote_manifest.get("stage_a_fingerprint")
    if theirs and theirs != stage_a_fingerprint():
        log.warning(
            "The pool in %s was scanned under a DIFFERENT Stage A gate (remote %s, your "
            "checkout %s). Its files hold the survivors of THEIR _TOKEN_GATE/CONCEPT_IDS, "
            "and the rows their gate rejected are in no pool at all — re-admitting it "
            "locally cannot recover them. Pulling anyway.",
            repo_id, str(theirs)[:12], stage_a_fingerprint()[:12])


def pull_pool(pool_dir: Path, repo: Optional[str] = None,
              years: Optional[list[int]] = None, dry_run: bool = False) -> int:
    """Download the pool (or only *years*) into the flat *pool_dir*.

    Per-file downloads rather than a whole-repo snapshot: that is what makes
    ``--years`` partial, and each file is independently resumable and locally
    cached. Files already present at the remote's size are skipped, and the rest
    are fetched several at a time (see ``_download_pool_files``). Returns the
    number of files downloaded (or, under *dry_run*, that would be).
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    api = hf.HfApi(token=token)

    try:
        # The repo holds the pool AND the prebuilt candidates artifact, which is
        # also parquet. Only the year shards are the pool: a build file landing
        # in the flat pool directory is a different schema in the directory every
        # pool consumer globs, and `route` dies on the first one it reads.
        # `--pull-build` is how a build is fetched, into its own directory.
        remote_files = [f for f in hf.list_repo_files(repo_id, repo_type=_REPO_TYPE,
                                                      token=token)
                        if f.endswith(".parquet")
                        and not f.startswith(f"{_BUILDS_PREFIX}/")]
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
        log.warning("%s Pulling without knowing which Stage A gate this pool was "
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
        # because admit_from_pool globs one directory.
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
            for pending in futures:
                pending.cancel()
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    # Once, after every worker is done: racing threads must not each try to
    # remove the same year folder.
    for shard in {pool_dir / _year_of(f) for f in wanted}:
        if shard.is_dir() and not any(shard.iterdir()):
            shard.rmdir()


# ---------------------------------------------------------------------------
# Prebuilt candidates artifact
# ---------------------------------------------------------------------------


def _read_build_manifest(build_dir: Path) -> dict:
    path = build_dir / _BUILD_MANIFEST
    if not path.exists():
        raise ValueError(f"No {_BUILD_MANIFEST} under {build_dir} — build the artifact first "
                         "with `python -m search.pool_sync --build-candidates`.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def push_build(build_dir: Path, repo: Optional[str] = None, dry_run: bool = False) -> int:
    """Upload a built candidates artifact to ``builds/<build_hash>/`` and point latest at it.

    Builds are addressed by content hash, so a push never overwrites a build that
    describes different rows; ``builds/latest.json`` is the only mutable name, and it
    is what ``pull_build`` follows when no hash is given. Returns files uploaded.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    manifest = _read_build_manifest(build_dir)
    build = manifest["build_hash"]
    chunks = sorted(build_dir.glob("candidates-*.parquet"))
    if not chunks:
        raise ValueError(f"No candidates-*.parquet under {build_dir}")

    api = hf.HfApi(token=token)
    if not dry_run:
        try:
            api.create_repo(repo_id, repo_type=_REPO_TYPE, private=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc

    remote = _remote_sizes(api, repo_id)
    prefix = f"{_BUILDS_PREFIX}/{build}"
    uploads: list[tuple[str, Union[Path, bytes]]] = [
        (f"{prefix}/{path.name}", path) for path in chunks
        if remote.get(f"{prefix}/{path.name}") != path.stat().st_size]
    uploaded = len(uploads)

    if not dry_run:
        uploads.append((f"{prefix}/{_BUILD_MANIFEST}",
                        json.dumps(manifest, indent=1).encode("utf-8")))
        latest = {"build_hash": build, "path": prefix,
                  "created_at": manifest.get("created_at", ""),
                  "rows": manifest.get("rows", 0),
                  "snapshot_date": manifest.get("snapshot_date", "")}
        uploads.append((_LATEST_BUILD, json.dumps(latest, indent=1).encode("utf-8")))
        _upload_batched(api, hf, repo_id, uploads, "Build push")

    log.info("Build push%s: %d chunk(s) uploaded, %d already present -> %s/%s",
             " (dry run)" if dry_run else "", uploaded, len(chunks) - uploaded,
             repo_id, prefix)
    return uploaded


def _warn_on_admission_mismatch(manifest: dict) -> None:
    """Say loudly when a build was admitted under rules this checkout does not have.

    This check is what makes a shared build safer than passing a CSV around: a CSV
    says nothing about the vocabulary that produced it, whereas a build carries the
    Stage B fingerprint and row-builder version it was made with, and merging one
    into your candidates.csv is merging someone else's admission decisions.
    """
    if manifest.get("stage_b_fingerprint") not in (None, stage_b_fingerprint()):
        log.warning(
            "This corpus was ADMITTED UNDER DIFFERENT RULES than your checkout: build "
            "Stage B %s, yours %s. The rows you are about to merge are what THEIR "
            "REPLICATION_PHRASES/admission rule kept. Re-run `--admit-from-pool` over the "
            "pool if you need your own vocabulary applied.",
            str(manifest.get("stage_b_fingerprint"))[:12], stage_b_fingerprint()[:12])
    if manifest.get("row_builder_version") not in (None, ROW_BUILDER_VERSION):
        log.warning(
            "This corpus was BUILT BY A DIFFERENT ROW BUILDER than your checkout: build "
            "%s, yours %s. Column shapes or values may differ from what your code would "
            "produce for the same works.",
            manifest.get("row_builder_version"), ROW_BUILDER_VERSION)


def pull_build(build_dir: Path, repo: Optional[str] = None, build_hash: Optional[str] = None,
               dry_run: bool = False, merge_fn: Optional[Callable] = None) -> int:
    """Download a prebuilt candidates artifact and merge it into candidates.csv.

    The routine path for a collaborator who wants the corpus rather than the freedom
    to re-admit it: minutes of download instead of a pool pull plus a local Stage B
    pass. Rows go into ``data/candidates.csv`` through
    ``_merge_into_candidates_csv(enrich=False)``, so dedup and the candidates index
    stay exactly as correct as any other Stage 1 source. Returns rows merged.
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)

    if not build_hash:
        latest = _read_remote_json(hf, repo_id, _LATEST_BUILD, token)
        if not latest or not latest.get("build_hash"):
            raise ValueError(
                f"Could not read {_LATEST_BUILD} from {repo_id} — either nobody has pushed "
                "a prebuilt corpus yet, or HF_TOKEN cannot read this private repo. Pull the "
                "survivor pool instead (--pull, then "
                "`python -m search.run_search --admit-from-pool`).")
        build_hash = latest["build_hash"]

    prefix = f"{_BUILDS_PREFIX}/{build_hash}"
    manifest = _read_remote_json(hf, repo_id, f"{prefix}/{_BUILD_MANIFEST}", token)
    if not manifest:
        raise ValueError(f"No build {build_hash} in {repo_id} (expected {prefix}/"
                         f"{_BUILD_MANIFEST}).")
    _warn_on_admission_mismatch(manifest)

    chunks = [c["name"] for c in manifest.get("chunks", [])]
    if not chunks:
        raise ValueError(f"Build {build_hash} in {repo_id} lists no chunks.")

    log.info("Build %s: %d chunk(s), %s row(s), built %s from snapshot %s",
             build_hash[:12], len(chunks), f"{manifest.get('rows', 0):,}",
             manifest.get("created_at", "?"), manifest.get("snapshot_date", "?"))
    if dry_run:
        log.info("Build pull (dry run): nothing downloaded, nothing merged")
        return 0

    if merge_fn is None:
        from search.run_search import _merge_into_candidates_csv as merge_fn
    import pandas as pd  # noqa: PLC0415 — deferred with the rest of the merge path

    build_dir = build_dir / build_hash
    build_dir.mkdir(parents=True, exist_ok=True)
    with open(build_dir / _BUILD_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)

    candidates_path = DATA_DIR / "candidates.csv"
    merged = 0
    for i, name in enumerate(chunks, 1):
        try:
            got = hf.hf_hub_download(repo_id=repo_id, filename=f"{prefix}/{name}",
                                     repo_type=_REPO_TYPE, token=token,
                                     local_dir=str(build_dir))
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc
        # local_dir reproduces the remote's builds/<hash>/ folder; keep the build flat.
        local = build_dir / name
        if Path(got).resolve() != local.resolve():
            shutil.move(str(got), str(local))
        df = pd.read_parquet(local)
        merged += int(merge_fn(df, candidates_path, enrich=False) or 0)
        log.info("Build pull %d/%d  %s  %d row(s), merged so far %d",
                 i, len(chunks), name, len(df), merged)

    print(f"\n=== Prebuilt candidates ({repo_id} {build_hash[:12]}) ===")
    print(f"  chunks downloaded                     {len(chunks)}")
    print(f"  rows in the build                     {manifest.get('rows', 0):,}")
    print(f"  merged into candidates.csv            {merged:,}")
    print(f"  build Stage B / row builder           {str(manifest.get('stage_b_fingerprint'))[:12]}"
          f" / {manifest.get('row_builder_version')}")
    print(f"  yours                                 {stage_b_fingerprint()[:12]}"
          f" / {ROW_BUILDER_VERSION}\n")
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Share the Stage 1 snapshot artifacts through a private Hugging Face "
            "dataset repo. --push/--pull move the Stage A survivor pool (year-sharded "
            "remotely, flat locally) so a collaborator can run --admit-from-pool "
            "without repeating the 13-21 hour scan; --build-candidates/--push-build/"
            "--pull-build share the ADMITTED corpus itself, so the routine collaborator "
            "pays neither the scan nor the local Stage B pass."),
        epilog=(
            "Repo id: --repo, else FLORA_POOL_REPO from .env. Token: HF_TOKEN from .env "
            f"(the repo is private). Default pool dir: {SNAPSHOT_POOL_DIR} "
            "(override with --pool-dir or FLORA_POOL_DIR)."),
    )
    direction = parser.add_mutually_exclusive_group(required=True)
    direction.add_argument("--push", action="store_true",
                           help="Upload the local pool to the dataset repo.")
    direction.add_argument("--pull", action="store_true",
                           help="Download the pool from the dataset repo.")
    direction.add_argument("--build-candidates", action="store_true",
                           help="Run the current Stage B over the local pool and write a "
                                "chunked, hashed candidates artifact to --build-dir.")
    direction.add_argument("--push-build", action="store_true",
                           help="Upload the artifact in --build-dir to builds/<hash>/ "
                                "and point builds/latest.json at it.")
    direction.add_argument("--check-access", action="store_true",
                           help="Prove this machine can write to the dataset repo (commits a "
                                "small preflight.json). Run BEFORE a long scan.")
    direction.add_argument("--pull-build", action="store_true",
                           help="Download a prebuilt corpus (latest, or --build-hash) and "
                                "merge it into data/candidates.csv.")
    parser.add_argument("--pool-dir", metavar="PATH", default=None,
                        help=f"Local pool directory (default: {SNAPSHOT_POOL_DIR}).")
    parser.add_argument("--build-dir", metavar="PATH", default=None,
                        help=f"Local build directory (default: {SNAPSHOT_BUILD_DIR}).")
    parser.add_argument("--build-hash", metavar="HASH", default=None,
                        help="--pull-build only: a specific build instead of the latest.")
    parser.add_argument("--force", action="store_true",
                        help="--push only: push even though the remote pool was scanned "
                             "under a different Stage A gate.")
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
    build_dir = Path(args.build_dir) if args.build_dir else SNAPSHOT_BUILD_DIR
    years = parse_years(args.years) if args.years else None
    if years is not None and not args.pull:
        parser.error("--years applies to --pull only")
    if args.build_hash and not args.pull_build:
        parser.error("--build-hash applies to --pull-build only")
    if args.force and not args.push:
        parser.error("--force applies to --push only")

    if args.check_access:
        info = check_access(repo=args.repo)
        print(f"Hugging Face OK: {info['by']} can write to {info['repo']}")
    elif args.push:
        n = push_pool(pool_dir, repo=args.repo, dry_run=args.dry_run, force=args.force)
        print(f"{'Would upload' if args.dry_run else 'Uploaded'} {n} pool file(s)")
    elif args.pull:
        n = pull_pool(pool_dir, repo=args.repo, years=years, dry_run=args.dry_run)
        print(f"{'Would download' if args.dry_run else 'Downloaded'} {n} pool file(s)")
    elif args.build_candidates:
        manifest = build_candidates(pool_dir, build_dir)
        print(f"Built {manifest['rows']:,} row(s) in {len(manifest['chunks'])} chunk(s) "
              f"at {build_dir} (build {manifest['build_hash'][:12]})")
    elif args.push_build:
        n = push_build(build_dir, repo=args.repo, dry_run=args.dry_run)
        print(f"{'Would upload' if args.dry_run else 'Uploaded'} {n} build chunk(s)")
    else:
        n = pull_build(build_dir, repo=args.repo, build_hash=args.build_hash,
                       dry_run=args.dry_run)
        print(f"Merged {n:,} row(s) into candidates.csv")


if __name__ == "__main__":
    main()
