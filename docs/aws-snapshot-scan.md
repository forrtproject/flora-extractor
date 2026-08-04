# Running the OpenAlex Snapshot Scan on AWS

The Stage 1 snapshot scan reads the entire OpenAlex works corpus — 2,446 parquet
partitions, 725 GB, 510,372,821 records — and keeps what the **search gate** admits
(`search/snapshot_scan.py`). On a home connection that is a 13–21 hour job. In
**us-east-1**, where the bucket lives, it is a 2–5 hour job that costs a few dollars,
because S3 → EC2 traffic inside one region is free and column projection means only
~425–490 GB of the 725 GB is ever transferred.

This runbook launches one EC2 instance that does the scan unattended and publishes
the result to the private Hugging Face dataset repo, so that nobody ever has to run
it again.

## What gets published

| Artifact | Where | Who consumes it |
| -------- | ----- | --------------- |
| The survivor pool (~2–3 GB, one parquet per partition) | `<repo>/<year>/part-*.parquet` + `pool_manifest.json` | everyone — it is Stage 1's output and Stage 2's direct input (`python -m search.pool_sync --pull`) |

There is one artifact, and that is the point: the pool is the corpus. The prebuilt
prebuilt `candidates.csv` build belonged to the admission-gated Stage 1 that no
longer exists and has been deleted with it: `pool_sync` now offers only `--push`,
`--pull` and `--check-access`, and the `BUILD_CANDIDATES` knob is gone from both
scripts.

## Prerequisites

1. **AWS CLI configured** for an account that may launch EC2 in us-east-1
   (`aws sts get-caller-identity` must succeed).
2. **An EC2 key pair** in us-east-1 and its `.pem` locally
   (`aws ec2 describe-key-pairs --region us-east-1`).
3. **A Hugging Face token with write scope**, and the private dataset repo it may
   write to. A fine-grained token scoped to that one repo is strongly preferred —
   see [Secrets](#secrets-and-blast-radius). Create the repo private first, or let
   the first push create it.
4. **The ref you want scanned is pushed to GitHub.** The instance clones from the
   remote, so anything uncommitted or unpushed locally does not exist for it —
   including `--status` and `--check-access`. `aws_launch.sh` checks that `REPO_REF`
   resolves on the remote and warns when your local HEAD differs from it.
5. Nothing else. The snapshot path makes **zero LLM calls and zero OpenAlex API
   calls**, so no Gemini/OpenAI/OpenAlex keys are needed on the instance — only
   `RESEARCHER_EMAIL` (politeness header) and `HF_TOKEN`.

## The commands, in order

Locally, from the repo root:

```bash
export KEY_NAME=my-keypair                       # EC2 key pair in us-east-1
export RESEARCHER_EMAIL=you@example.com
export FLORA_POOL_REPO=my-org/flora-survivor-pool
read -rs HF_TOKEN && export HF_TOKEN             # typed, not stored in shell history

bash scripts/aws_launch.sh
```

That prints the instance id, the public IP, and the four commands below. It refuses to
launch when an instance tagged `Name=flora-snapshot-scan` is already pending or running
in the region, naming that instance id — set `FORCE_NEW=1` if a second concurrent scan
is really what you want.

Optional knobs, all environment variables:

| Knob | Default | |
| ---- | ------- | - |
| `INSTANCE_TYPE` | `c7i.xlarge` | 4 vCPU; more cores buy nothing (see Cost) |
| `VOLUME_GB` | `100` | root volume; the pre-flight floor is 25 GB free |
| `REGION` | `us-east-1` | where the OpenAlex bucket is — moving it costs egress |
| `REPO_REF` | `main` | branch, tag or SHA the instance scans with |
| `SHUTDOWN_WHEN_DONE` | `0` | power off (not terminate) after a successful publish |
| `SPOT` | `0` | `1` is cheaper but loses the scan on a reclaim (see Cost) |
| `FORCE_NEW` | `0` | `1` launches even if a scan instance is already running |
| `NAME_TAG` | `flora-snapshot-scan` | the `Name` tag, and the duplicate-launch key |
| `SUBNET_ID` / `SECURITY_GROUP_ID` | (default VPC) | set if you have no default VPC |
| `SSH_CIDR` | your current public IP `/32` | only used for a script-created SG |

On the instance:

```bash
ssh -i ~/.ssh/my-keypair.pem ubuntu@<public-ip>

sudo tail -f /var/log/cloud-init-output.log        # bootstrap: install, clone, pre-flight
sudo tail -f /var/log/flora/scan.log               # the scan itself
sudo bash /opt/flora/aws_snapshot_scan.sh status   # progress + ETA (read-only)
```

The scan runs in a tmux session named `flora`, so closing SSH does not stop it
(`sudo tmux attach -t flora` to watch it live, `Ctrl-b d` to detach).

### What the bootstrap does

`scripts/aws_snapshot_scan.sh` runs as user-data (and can be run by hand on any fresh
Ubuntu box with `sudo -E bash scripts/aws_snapshot_scan.sh`):

1. Refuses to continue without `RESEARCHER_EMAIL`, `HF_TOKEN`, `FLORA_POOL_REPO` —
   reading any it is missing back out of `.env`, so a re-run needs no secrets.
2. Installs `git tmux python3 python3-venv`, clones the repo at `REPO_REF`, builds a
   venv, `pip install -r requirements.txt`.
3. Writes `.env` (mode 600) from those environment variables.
4. **Pre-flight, before any scan time is spent**: `python -m search.pool_sync
   --check-access` commits a small `preflight.json` to the dataset repo — an actual
   write, because an existing repo answers a read-only token's `create_repo` happily
   — then fetches the OpenAlex manifest and checks free disk. Any failure here aborts
   with nothing scanned.
5. Starts the long phase detached in tmux: scan → push pool → write `/opt/flora/DONE`.

## What to expect

| Phase | Duration | Notes |
| ----- | -------- | ----- |
| Bootstrap + pre-flight | 2–4 min | apt, pip, HF write check |
| Snapshot scan | 2–5 h | sequential, network- and decode-bound |
| Pool push (~2–3 GB, ~2,446 files) | 10–25 min | batched commits, resumable |

Progress is visible from the first minute: partitions are consumed oldest-first, and
the early `updated_date=2016-*` partitions are small, so the file counter moves fast
at the start and the GB counter does not. Trust the **bytes** line and the ETA.

### Cost

US East (N. Virginia) on-demand list prices; check the pricing pages if these matter
to a decision.

| Item | Rate | 5 h run |
| ---- | ---- | ------- |
| c7i.xlarge (4 vCPU) | $0.1785 /h | $0.89 |
| c7i.2xlarge (8 vCPU, if you want headroom) | $0.357 /h | $1.79 |
| gp3 root volume, 100 GB | $0.08 /GB-month | ~$0.06 |
| S3 GET requests (~10⁵ range reads) | $0.0004 /1,000 | ~$0.05 |
| S3 → EC2 transfer, same region | free | $0.00 |
| Egress to Hugging Face (~4 GB) | $0.09 /GB, first 100 GB/month free | $0.00–0.36 |
| **Total** | | **~$1–3** |

More vCPUs do not shorten the scan: it is one sequential process reading one
partition at a time, so `c7i.xlarge` is the right default and a bigger instance
mostly buys network burst it does not use.

**Spot** (`SPOT=1`) is ~70% cheaper and **loses the whole scan if it is reclaimed**. The
ledger and the survivor pool both live on the instance's root volume,
which is `DeleteOnTermination`; a reclaim terminates the instance, so the volume and
every hour of scanning on it are gone and the next launch starts from an empty ledger.
The per-partition checkpointing protects against a *process* dying, not against the box
disappearing.

For a one-shot unattended run, use the default **on-demand**: the ~$1 of extra cost is
less than one restarted scan. Spot only makes sense if you are watching the run, are
willing to start over, or have set up a volume that survives the instance.

## Checking progress mid-run

```bash
sudo bash /opt/flora/aws_snapshot_scan.sh status
# or, in the checkout:
sudo /opt/flora/venv/bin/python -m search.snapshot_scan --status --json
```

It is read-only — it opens `cache/snapshot/ledger.json`, the cached manifest and the
pool directory, and writes nothing — so it is safe to run against a scan in flight, as
often as you like. Output:

```
=== Snapshot scan status (cache/snapshot/ledger.json) ===
  files consumed                        620 / 2,446  (25.3%)
  bytes consumed                        101.01 / 724.97 GB
  records scanned                       84,618,962 / 510,372,821
  rows kept                             101,576
  survivor pool                         2 file(s), 0.01 GB  (/opt/flora/pool)
  file(s) mid-scan                      1 (updated_date=2025-11-06/part_0201.parquet)
  first / last file finished            2026-08-02T23:07:32+00:00 / 2026-08-02T23:30:28+00:00  (1m 00s ago)
  recent throughput                     89.1 MB/s (last 50 files)
  estimated time remaining              1h 56m
  snapshot date                         2026-07-28
  search gate fingerprint               d536bc51b9b2
  this checkout                         d536bc51b9b2
```

Throughput and ETA are measured over the last 50 finished files in manifest bytes, not
files — partitions differ in size by two orders of magnitude, and the job is
network-bound. A resumed scan's first status call after restart can show a stale rate
until 50 new files land.

"Is it stuck?" — compare the *last file finished* timestamp with now (the status line
shows the gap). A single large partition takes a few minutes; a gap of an hour with no
new log lines means something is wrong, and `/var/log/flora/scan.log` will say what.

## Resuming after an interruption

Re-run the same command. There is nothing else to do:

```bash
sudo -E bash /opt/flora/aws_snapshot_scan.sh          # same bootstrap, resumes the scan
```

You do not have to supply the token again: the script reads `RESEARCHER_EMAIL`,
`HF_TOKEN` and `FLORA_POOL_REPO` back out of `/opt/flora/flora-extractor/.env` (mode
600) whenever they are absent from the environment. Values you *do* pass win over the
file, which is how you fix a bad token — `sudo HF_TOKEN=hf_… bash
/opt/flora/aws_snapshot_scan.sh`.

- The ledger records each manifest file as `done` only after its pool file is
  committed, so a killed process re-reads at most one partition.
- Pool pushes skip anything already on the remote at the same size, so an
  interrupted upload is resumed by re-running it.
- A second invocation while a scan is running takes no lock and does not start a
  second scanner; it tells you to use `status` instead.

After a **spot reclaim** the instance is gone. Relaunch with `bash
scripts/aws_launch.sh` — but note the new instance starts from an empty ledger, so
this only makes sense if you have not scanned much yet, or if you attach the old
volume. For a resumable-across-instances setup, run on-demand.

If only the *publish* failed (bad token fixed after the fact), the scan is on disk:

```bash
cd /opt/flora/flora-extractor
sudo /opt/flora/venv/bin/python -m search.pool_sync --push
```

## Verifying the result

On the instance, once `/opt/flora/DONE` exists:

```bash
sudo bash /opt/flora/aws_snapshot_scan.sh status
```

1. **Files consumed equals the manifest count** — `2,446 / 2,446`. Anything less means
   partitions were skipped after three read failures; they are listed in
   `scan.log` under "finished with N unreadable file(s)". Re-running the scan picks
   exactly those up (they are absent from the ledger, not marked done).
2. **Records scanned equals the manifest total** (~510 M) — the same check by row.
3. **Pool file count ≈ file count** — a partition with no gate survivor at all
   leaves no pool file, so a handful fewer is expected; hundreds fewer is not.
4. **`pool_manifest.json` in the repo** names the search gate the pool was scanned
   under: its gate fingerprint must match `this checkout` in the status output, and
   `ledger_files` / `ledger_records` must match the manifest.

Then, from any laptop with read access to the repo:

```bash
python -m search.pool_sync --pull --dry-run   # names what it would fetch, downloads nothing
python -m search.pool_sync --pull            # → the survivor pool, Stage 2's input
```

## Tearing down

```bash
aws ec2 terminate-instances --region us-east-1 --instance-ids i-0123456789abcdef0
```

The root volume is `DeleteOnTermination`, so that removes the `.env` holding the token
along with everything else. **Then revoke the HF token** — until the instance is gone
its user-data copy is readable by anyone on the box, and revoking is the only step that
does not depend on the teardown having happened. If `aws_launch.sh` created the `flora-snapshot-ssh`
security group and you do not want it kept:

```bash
aws ec2 delete-security-group --region us-east-1 --group-name flora-snapshot-ssh
```

`SHUTDOWN_WHEN_DONE=1` powers the instance off after a successful publish (it stops,
it does not terminate — the volume and its `.env` survive until you terminate).
Leave it at `0` unless you are willing to verify the result from Hugging Face alone.

## Secrets and blast radius

- `HF_TOKEN` and `RESEARCHER_EMAIL` reach the instance **in EC2 user-data**, which is
  stored unencrypted in instance metadata. `aws_launch.sh` sets `HttpTokens=required`
  (IMDSv2) and `HttpPutResponseHopLimit=1`, which stops a container or a plain
  GET-based SSRF — but **any local user on the instance can still read the token**, not
  just root: IMDSv2 needs only a `PUT` to 169.254.169.254 for a session token, which is
  an unprivileged HTTP request, and user-data is served to it in plaintext. Treat
  anyone with a shell on the box as holding the token.
- On disk the token is written only to `/opt/flora/flora-extractor/.env` (mode 600,
  root-owned) and is never echoed or logged. The self-copy at
  `/opt/flora/aws_snapshot_scan.sh` has the injected `export HF_TOKEN=…` line stripped
  out, so `.env` is the only file that holds it.
- Consequence: use a **fine-grained HF token scoped to write only this one dataset
  repo** — the worst case of a compromised instance is then someone writing to the pool
  repo — and **revoke the token when the run is done**. That is a required step, not a
  nicety: the copy in user-data outlives the scan for as long as the instance exists,
  and terminating the instance is what removes it.
- Nothing is committed to git: `.env` is gitignored and the scripts never write
  secrets into the checkout.
- The AWS side needs no instance profile at all — the OpenAlex bucket is public, not
  requester-pays — so the instance is launched without one and cannot touch your
  account.

## Doing it without AWS

The same bootstrap runs on any fresh Ubuntu box:

```bash
export RESEARCHER_EMAIL=… HF_TOKEN=… FLORA_POOL_REPO=…
sudo -E bash scripts/aws_snapshot_scan.sh
```

Outside us-east-1 expect 13–21 hours and, on a cloud provider, an egress bill for
~450 GB. The ledger makes it interruptible, so a home machine can do it over several
evenings by re-running the same command.
