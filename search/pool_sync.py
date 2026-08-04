"""
Share the survivor pool through a private Hugging Face dataset repo.

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

``huggingface_hub`` is imported inside the functions, not at module scope: it is
a pipeline-only dependency and read-only/web deployments must not need it.
"""

import argparse
import datetime
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Union

from shared.config import (
    FLORA_HF_COMMIT_BATCH,
    FLORA_POOL_REPO,
    SNAPSHOT_POOL_DIR,
    log,
)
from search.snapshot_scan import (
    ledger_hash,
    load_ledger,
    search_gate_fingerprint,
)

_REPO_TYPE = "dataset"

_POOL_MANIFEST = "pool_manifest.json"

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

    The distinction matters at exactly one place — the search gate check in
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
    transient blip walk a push straight past the search gate check. Those raise.
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
    cached. Files already present at the remote's size are skipped. Returns the
    number of files downloaded (or, under *dry_run*, that would be).
    """
    import huggingface_hub as hf  # pipeline-only: read-only deployments never install it

    repo_id = _resolve_repo(repo)
    token = _require_token(hf)
    api = hf.HfApi(token=token)

    try:
        remote_files = [f for f in hf.list_repo_files(repo_id, repo_type=_REPO_TYPE,
                                                      token=token)
                        if f.endswith(".parquet")]
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
    downloaded = 0
    skipped = 0
    for remote_file in sorted(remote_files):
        name = remote_file.rsplit("/", 1)[-1]
        local = pool_dir / name
        if local.exists() and sizes.get(remote_file) == local.stat().st_size:
            skipped += 1
            continue
        if dry_run:
            downloaded += 1
            continue
        try:
            got = hf.hf_hub_download(repo_id=repo_id, filename=remote_file,
                                     repo_type=_REPO_TYPE, token=token,
                                     local_dir=str(pool_dir))
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(_auth_hint(hf, repo_id, exc)) from exc
        # local_dir reproduces the remote's year folder; the pool itself is flat,
        # because the pool row builder globs one directory.
        got_path = Path(got)
        if got_path.resolve() != local.resolve():
            shutil.move(str(got_path), str(local))
            shard = pool_dir / _year_of(remote_file)
            if shard.is_dir() and not any(shard.iterdir()):
                shard.rmdir()
        downloaded += 1
        if downloaded % 50 == 0:
            log.info("Pool pull: %d downloaded, %d already present", downloaded, skipped)

    log.info("Pool pull%s: %d downloaded, %d already present (%d remote files from %s -> %s)",
             " (dry run)" if dry_run else "", downloaded, skipped, len(remote_files),
             repo_id, pool_dir)
    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Share the Stage 1 snapshot artifacts through a private Hugging Face "
            "dataset repo. --push/--pull move the survivor pool (year-sharded "
            "remotely, flat locally) so a collaborator can work over the pool "
            "without repeating the 13-21 hour scan."),
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
    direction.add_argument("--check-access", action="store_true",
                           help="Prove this machine can write to the dataset repo (commits a "
                                "small preflight.json). Run BEFORE a long scan.")
    parser.add_argument("--pool-dir", metavar="PATH", default=None,
                        help=f"Local pool directory (default: {SNAPSHOT_POOL_DIR}).")
    parser.add_argument("--force", action="store_true",
                        help="--push only: push even though the remote pool was scanned "
                             "under a different search gate.")
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
    years = parse_years(args.years) if args.years else None
    if years is not None and not args.pull:
        parser.error("--years applies to --pull only")
    if args.force and not args.push:
        parser.error("--force applies to --push only")

    if args.check_access:
        info = check_access(repo=args.repo)
        print(f"Hugging Face OK: {info['by']} can write to {info['repo']}")
    elif args.push:
        n = push_pool(pool_dir, repo=args.repo, dry_run=args.dry_run, force=args.force)
        print(f"{'Would upload' if args.dry_run else 'Uploaded'} {n} pool file(s)")
    else:
        n = pull_pool(pool_dir, repo=args.repo, years=years, dry_run=args.dry_run)
        print(f"{'Would download' if args.dry_run else 'Downloaded'} {n} pool file(s)")


if __name__ == "__main__":
    main()
