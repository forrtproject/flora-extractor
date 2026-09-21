"""repair_cache_manifest.py — republish a cache manifest that lost parts to issue #209.

`cache_manifest()` built its `parts` map by iterating the parts being PUSHED rather
than the merged map its caller assembled, so a `--parts <one>` push rewrote the
manifest to name that part alone. The shards of every other part stayed in the repo
and became unreachable: `pull_cache()` iterates the manifest and nothing else, so a
pull reported one shard, unpacked it, and left 32 shards of already-paid-for answers
sitting in the repo. That is the state `lukaswallrich/flora-survivor-pool` has been in
since the 2026-09-08 push (git_commit b30c5bf) — `cache/llm` alone holds 16 shards and
~180k LLM answers, including the screen's `classifyvote_*` entries.

The code is fixed, so no future push can do this again. A fixed pusher does not repair
the damage, though: it merges against the manifest it READS, and the manifest it reads
is the broken one, so the parts already dropped stay dropped. This restores them.

**It republishes a description, never data.** It reads the repo's own file list, and
for each `cache/<part>/<shard>.tar.gz` present, downloads that shard and recomputes its
digest exactly as `push_cache` does (`cache_key(blob.hex())` over the uploaded bytes),
so the manifest it writes says what the repo actually holds. No shard is uploaded,
deleted or altered. Every other manifest field — `abstract_sources`, `abstract_rows`,
`capabilities`, `prompt_versions` — is carried over from the remote manifest unchanged,
because they describe pushes this is not making.

A digest computed from the downloaded bytes is what a puller's own pull-state would
record, so a machine that has already pulled a shard is not made to fetch it twice.

    .venv/bin/python -m tools.repair_cache_manifest              # report, the default
    .venv/bin/python -m tools.repair_cache_manifest --apply      # publish it

`--apply` needs an HF_TOKEN with WRITE access to the repo. Read access is enough for
the report, which is the whole point of the report.
"""

import argparse
import datetime
import json
import sys
from typing import Optional

from shared.cache_sync import _MANIFEST, PARTS, _remote_shard
from shared.config import FLORA_HF_COMMIT_BATCH, FLORA_POOL_REPO
from shared.hf import read_remote_json, require_token, resolve_repo, upload_batched
from shared.utils import cache_key


def remote_shards(api, repo_id: str) -> dict[str, list[str]]:
    """`{part name: [remote path, …]}` for every shard the repo actually holds.

    Keyed off the repo's file list rather than the manifest, because the manifest is
    the thing that is wrong. Only paths matching a part this checkout knows about are
    reported: a directory under `cache/` that is not a `Part` is not something a pull
    could unpack anyway.
    """
    files = set(api.list_repo_files(repo_id, repo_type="dataset"))
    found: dict[str, list[str]] = {}
    for name, part in PARTS.items():
        if part.sqlite:
            continue                       # pushed whole, not as shards
        shards = sorted(f for f in files
                        if f.startswith(f"cache/{name}/") and f.endswith(".tar.gz"))
        if shards:
            found[name] = shards
    return found


def shard_digests(hf, repo_id: str, token: str, shards: dict[str, list[str]],
                  ) -> dict[str, dict[str, str]]:
    """`{part: {shard: digest}}`, each digest recomputed from the uploaded bytes.

    The same function `push_cache` hashes with, over the same bytes, so a puller
    comparing this against its pull state sees the shard it already has as already
    had — and a digest that somehow disagreed would cost a re-download, never a
    skipped shard, because the state only ever records a digest it has unpacked.

    This DOWNLOADS every shard it hashes (~8 GB for the full repo), into the
    huggingface_hub cache. Having run `cache_sync --pull` first does not help: the
    pull fetches into a temporary directory, so nothing of it survives for this to
    reuse. Repeat runs of this tool are free.
    """
    out: dict[str, dict[str, str]] = {}
    for name, paths in sorted(shards.items()):
        part = PARTS[name]
        for remote in paths:
            shard = remote.rsplit("/", 1)[-1][: -len(".tar.gz")]
            if _remote_shard(part, shard) != remote:
                # A nested or oddly named path this code could not rebuild, so a
                # pull could not ask for it either. Checked BEFORE the download:
                # a shard we are going to skip is not worth several hundred MB.
                print(f"  ! {remote} does not match {part.name}'s shard naming — skipped")
                continue
            local = hf.hf_hub_download(repo_id, remote, repo_type="dataset",
                                       token=token)
            with open(local, "rb") as handle:
                digest = cache_key(handle.read().hex())
            out.setdefault(name, {})[shard] = digest
    return out


def repaired(previous: dict, parts: dict[str, dict[str, str]]) -> dict:
    """*previous* with its `parts` replaced by what the repo holds.

    A union rather than a replacement: a part the manifest names and the repo does
    not is left alone and reported. Removing it would be this script deciding that a
    listing it just took is more authoritative than a push's own record, and the
    failure it is repairing is exactly a manifest that dropped what it could not see.
    """
    out = dict(previous)
    out["parts"] = {**(previous.get("parts") or {}), **parts}
    out["repaired_at"] = datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")
    out["repaired_note"] = (
        "parts re-derived from the repo's own file list by "
        "tools/repair_cache_manifest.py; no shard was uploaded or changed")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true",
                        help="publish the repaired manifest (needs write access)")
    parser.add_argument("--repo", default=None, help="dataset repo (default: FLORA_POOL_REPO)")
    args = parser.parse_args(argv)

    import huggingface_hub as hf

    repo_id = resolve_repo(args.repo, FLORA_POOL_REPO, "the API caches")
    token = require_token(hf)
    api = hf.HfApi(token=token)

    previous = read_remote_json(hf, repo_id, _MANIFEST, token)
    if previous is None:
        raise SystemExit(
            f"No {_MANIFEST} in {repo_id}. This script repairs a manifest that lost "
            "parts; with none at all, push from the machine that holds the caches.")

    shards = remote_shards(api, repo_id)
    print(f"{repo_id}\n  manifest pushed {previous.get('pushed_at') or '?'} "
          f"(commit {str(previous.get('git_commit') or '?')[:12]})")
    named = previous.get("parts") or {}
    print(f"  manifest names : {', '.join(sorted(named)) or '(none)'}")
    print(f"  repo holds     : {', '.join(sorted(shards)) or '(none)'}")

    missing = {n: s for n, s in shards.items() if not named.get(n)}
    if not missing:
        print("\nNothing to repair — every part the repo holds is already named.")
        return 0
    print("\nUNREACHABLE — in the repo, absent from the manifest, so no pull can "
          "fetch them:")
    for name, paths in sorted(missing.items()):
        print(f"  {name:14s} {len(paths):3d} shard(s)")

    print("\nHashing the shards (downloads are cached on disk)…")
    digests = shard_digests(hf, repo_id, token, missing)
    payload = repaired(previous, digests)
    print(f"  repaired manifest names: {', '.join(sorted(payload['parts']))}")

    if not args.apply:
        print("\nReport only. Re-run with --apply to publish it.")
        return 0

    # Hashing takes minutes, and this writes the manifest WHOLE. A push that
    # committed while it ran would have its own new digests overwritten by a
    # manifest built before they existed — its shards would go unreachable, which
    # is the exact failure this repairs. Re-read and refuse: the repair is free to
    # run again, and a repair that quietly undid a push would not be a repair.
    current = read_remote_json(hf, repo_id, _MANIFEST, token)
    if current != previous:
        raise SystemExit(
            "The manifest changed while this was hashing — something pushed. "
            "Nothing was written. Re-run: the downloads are cached now, so the "
            "second pass is quick.")

    upload_batched(api, hf, repo_id,
                   [(_MANIFEST, json.dumps(payload, indent=1).encode("utf-8"))],
                   "Repair the cache manifest (issue #209)", FLORA_HF_COMMIT_BATCH)
    print("\nPublished. `python -m shared.cache_sync --pull` now reaches every part.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
