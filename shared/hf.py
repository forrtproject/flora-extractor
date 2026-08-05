"""
hf.py — The Hugging Face plumbing shared by everything that syncs through the repo.

Three modules move bytes through the same private dataset repo: `search/pool_sync.py`
(the survivor pool), `shared/cache_sync.py` (the API caches) and `filter/engine/tiers.py`
(raw response blobs). What they need from Hugging Face is identical and none of it is
obvious — which exceptions establish that a file is ABSENT as opposed to unreadable,
which failures a different token would fix, and why uploads must be batched — so it
lives here once rather than being reimplemented per caller with subtly different
answers.

`huggingface_hub` is imported by the CALLER and passed in, never imported at module
scope: it is a pipeline-only dependency and read-only/web deployments must not need
it. Passing the module also keeps exception-class identity exact — a class looked up
on the module that raised always matches.
"""

import json
import tempfile
from pathlib import Path
from typing import Optional, Union

from shared.config import FLORA_HF_COMMIT_BATCH, log

REPO_TYPE = "dataset"


class RemoteReadError(RuntimeError):
    """A remote file could not be read AND its absence was not established.

    The distinction matters wherever "there is no such file" and "I could not find
    out" lead to opposite actions — the search-gate check in `push_pool`, the
    capability check in `pull_cache`.
    """


def status_of(exc: Exception) -> Optional[int]:
    """The HTTP status behind *exc*, when it carries a response."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return int(code) if isinstance(code, int) else None


def error_class(hf, name: str) -> tuple:
    """``(cls,)`` for the huggingface_hub error *name*, or ``()`` if this version
    does not expose it. Looked up on the passed module so the class identity always
    matches the one that raised."""
    for holder in (getattr(hf, "errors", None), getattr(hf, "utils", None), hf):
        cls = getattr(holder, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return (cls,)
    return ()


def is_absent(hf, exc: Exception) -> bool:
    """True only when the Hub actually said the entry is not there.

    ``LocalEntryNotFoundError`` subclasses ``EntryNotFoundError`` but means the Hub
    could not be reached at all, and ``GatedRepoError`` subclasses
    ``RepositoryNotFoundError`` but means this token may not look — neither
    establishes anything about what the repo holds, so both are excluded first.
    """
    if isinstance(exc, error_class(hf, "LocalEntryNotFoundError")
                  + error_class(hf, "GatedRepoError")):
        return False
    absent = (error_class(hf, "EntryNotFoundError")
              + error_class(hf, "RepositoryNotFoundError")
              + error_class(hf, "RevisionNotFoundError"))
    return isinstance(exc, absent) or status_of(exc) == 404


def is_auth_error(hf, exc: Exception) -> bool:
    """Whether *exc* is the kind of failure a different HF_TOKEN would fix.

    ``RepositoryNotFoundError`` counts: a private repo this token cannot see is
    reported as missing, so "no such repo" and "not yours" are the same answer.
    """
    if status_of(exc) in (401, 403):
        return True
    return isinstance(exc, error_class(hf, "GatedRepoError")
                      + error_class(hf, "RepositoryNotFoundError")
                      + error_class(hf, "DisabledRepoError")
                      + error_class(hf, "LocalTokenNotFoundError"))


def auth_hint(hf, repo: str, exc: Exception) -> str:
    """The message to raise for *exc* — with the token advice only when it applies.

    A 503 and a 403 need different things from the operator, and sending someone
    after their token while the Hub is down costs them the one thing they had:
    knowing what actually failed.
    """
    if not is_auth_error(hf, exc):
        return (f"Hugging Face request to {repo!r} failed "
                f"({type(exc).__name__}: {exc}).")
    return (f"Hugging Face refused access to {repo!r} ({exc}). Check that HF_TOKEN belongs "
            f"to an account with access to this private repo, and that it has write "
            f"permission if you are pushing.")


def require_token(hf) -> str:
    """The HF token, or a message naming the env var that is missing.

    ``huggingface_hub`` resolves ``HF_TOKEN`` (and a cached ``hf auth login``) by
    itself; this only turns "no token" into an actionable error before a private
    repo answers with a bare 401.
    """
    token = hf.get_token()
    if not token:
        raise ValueError(
            "No Hugging Face token. This is a PRIVATE dataset repo, so a token is "
            "required: set HF_TOKEN in your .env (create one at "
            "https://huggingface.co/settings/tokens with write access to push, "
            "read access to pull) or run `hf auth login`.")
    return token


def resolve_repo(repo: Optional[str], configured: str, what: str) -> str:
    """The dataset repo id to sync with, or a message saying how to supply one."""
    resolved = (repo or configured or "").strip()
    if not resolved:
        raise ValueError(
            f"No Hugging Face dataset repo for {what}. Set FLORA_POOL_REPO in your "
            ".env (e.g. FLORA_POOL_REPO=my-org/flora-survivor-pool) or pass "
            "--repo <owner>/<name>.")
    return resolved


def read_remote_json(hf, repo_id: str, remote_path: str,
                     token: Optional[str]) -> Optional[dict]:
    """Fetch a small JSON file from the repo, or None when it is genuinely not there.

    A missing file is the normal first-push case, not an error. Any OTHER failure —
    a 503, a rate limit, a dropped connection, malformed JSON — says nothing about
    what the repo holds, and reading it as "not there" is what would let a transient
    blip walk a push straight past a safety check. Those raise.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = hf.hf_hub_download(repo_id=repo_id, filename=remote_path,
                                      repo_type=REPO_TYPE, token=token, local_dir=tmp)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:  # noqa: BLE001 — boundary: absence vs. any other failure
        if is_absent(hf, exc):
            log.debug("No %s in %s (%s)", remote_path, repo_id, exc)
            return None
        raise RemoteReadError(
            f"Could not read {remote_path} from {repo_id} ({type(exc).__name__}: {exc}). "
            "That is not evidence it is absent, so nothing was assumed about it — "
            "retry once Hugging Face answers.") from exc


def upload_batched(api, hf, repo_id: str, uploads: list[tuple[str, Union[Path, bytes]]],
                   message: str, batch_size: Optional[int] = None) -> None:
    """Commit *uploads* (``[(remote path, local path or bytes), …]``) in batches.

    One ``upload_file`` per file is one COMMIT per file: a single pool push (~2,446
    files) would put the repo straight at the few-thousand-commit mark where Hugging
    Face says repo UX degrades, and every re-push would add as many again. Batching
    into ``FLORA_HF_COMMIT_BATCH``-file commits turns that into a couple of dozen.
    """
    size = max(1, batch_size or FLORA_HF_COMMIT_BATCH)
    for start in range(0, len(uploads), size):
        batch = uploads[start:start + size]
        operations = [hf.CommitOperationAdd(path_in_repo=target, path_or_fileobj=payload
                                            if isinstance(payload, bytes) else str(payload))
                      for target, payload in batch]
        try:
            api.create_commit(repo_id=repo_id, repo_type=REPO_TYPE, operations=operations,
                              commit_message=f"{message} ({start + 1}-{start + len(batch)} "
                                             f"of {len(uploads)})")
        except Exception as exc:  # noqa: BLE001 — boundary: turn 401/403 into instructions
            raise RuntimeError(auth_hint(hf, repo_id, exc)) from exc
        log.info("%s: %d/%d file(s) committed", message, start + len(batch), len(uploads))


def remote_sizes(api, repo_id: str, suffix: str) -> dict[str, int]:
    """``{remote path: size in bytes}`` for every *suffix* file already in the repo.

    One tree listing instead of a metadata call per file — with thousands of files
    the per-file form would cost more round trips than the transfer it is meant to
    skip. An empty repo (or one that does not exist yet) is an empty map, not an
    error: the first push creates it.
    """
    try:
        entries = api.list_repo_tree(repo_id, repo_type=REPO_TYPE, recursive=True)
    except Exception as exc:  # noqa: BLE001 — a missing repo is the first-push case
        log.warning("Could not list %s (%s) — treating the remote as empty, so nothing "
                    "will be skipped and every file transfers again", repo_id, exc)
        return {}
    sizes: dict[str, int] = {}
    for entry in entries:
        path = getattr(entry, "path", None)
        size = getattr(entry, "size", None)
        if path and str(path).endswith(suffix) and size is not None:
            sizes[str(path)] = int(size)
    return sizes
