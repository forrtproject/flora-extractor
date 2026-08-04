#!/usr/bin/env bash
#
# Unattended OpenAlex snapshot scan on a fresh Ubuntu box (EC2 user-data or by hand).
#
#   sudo -E bash scripts/aws_snapshot_scan.sh          # bootstrap + start (detached)
#   sudo bash /opt/flora/aws_snapshot_scan.sh status   # how far along is it?
#   sudo bash /opt/flora/aws_snapshot_scan.sh --run    # the long phase, in the foreground
#
# What it does, in order: install system deps, clone the repo at a pinned ref, build a
# venv, write .env from the environment, PROVE Hugging Face write access, then run the
# scan detached under tmux and publish the pool (and, by default, a prebuilt candidates
# artifact) when it finishes.
#
# The Hugging Face pre-flight is deliberately before the scan. A scan is hours long and
# its only product is the artifact at the end, so a token that cannot publish must fail
# in the first minute, not the fifth hour.
#
# Idempotent: every step is safe to repeat, and re-invoking after an interruption
# resumes — the scan is checkpointed per manifest file in cache/snapshot/ledger.json and
# the pool is one parquet per partition, so nothing is re-read and nothing is duplicated.
# A second invocation while a scan is running does not start a second scan (flock).
#
# Secrets: HF_TOKEN and RESEARCHER_EMAIL arrive as environment variables and are written
# only to $WORK_DIR/.env (mode 600). They are never echoed and never logged.

set -Eeuo pipefail
umask 077

# ── Inputs (environment) ──────────────────────────────────────────────────────
# Required
: "${RESEARCHER_EMAIL:=}"        # OpenAlex/CrossRef politeness header
: "${HF_TOKEN:=}"                # Hugging Face token WITH WRITE ACCESS to the pool repo
: "${FLORA_POOL_REPO:=}"         # e.g. forrtproject/flora-survivor-pool (private dataset)
# Optional
: "${REPO_URL:=https://github.com/forrtproject/flora-extractor.git}"
: "${REPO_REF:=main}"            # branch, tag or commit SHA to scan with
: "${WORK_DIR:=/opt/flora}"
: "${LOG_DIR:=/var/log/flora}"
: "${PUSH_POOL:=1}"              # 1 = push the survivor pool to Hugging Face when done
: "${SHUTDOWN_WHEN_DONE:=0}"     # 1 = `shutdown -h now` after a successful publish
: "${TMUX_SESSION:=flora}"

CHECKOUT="$WORK_DIR/flora-extractor"
VENV="$WORK_DIR/venv"
PY="$VENV/bin/python"
: "${FLORA_POOL_DIR:=$WORK_DIR/pool}"   # survivor pool (~2-3 GB), on the root volume
SELF_COPY="$WORK_DIR/aws_snapshot_scan.sh"
LOG="$LOG_DIR/scan.log"
LOCK="$WORK_DIR/scan.lock"
DONE_MARKER="$WORK_DIR/DONE"

log()  { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# One loud block for both failure paths, because the thing a maintainer must not
# miss in a screenful of scan output is that the box is still billing.
fail_loud() {
  echo "" >&2
  echo "########## FLORA SNAPSHOT SCAN FAILED ##########" >&2
  echo "## $*" >&2
  echo "## THE INSTANCE IS STILL RUNNING and still billing — nothing here" >&2
  echo "## terminates it. Read $LOG, then terminate it yourself:" >&2
  echo "##   aws ec2 terminate-instances --region <region> --instance-ids <id>" >&2
  echo "###############################################" >&2
}

die()  { fail_loud "$*"; echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FATAL: $*" >&2; exit 1; }

# set -E already propagates ERR into functions and subshells, but nothing was
# trapping it, so an unguarded failure died silently mid-log. `exit` does not fire
# ERR, so this covers exactly the failures `die` does not.
trap 'fail_loud "unhandled failure at line $LINENO (exit $?): $BASH_COMMAND"' ERR

# ── Phases ────────────────────────────────────────────────────────────────────

load_env_fallback() {
  # Resume path. After the first bootstrap the token lives in $CHECKOUT/.env (mode
  # 600) and nowhere else — not in the self-copy of this script, not in the shell
  # that re-runs it. require_inputs happens before any python, so shared/config.py's
  # own .env loading is too late to satisfy it; read the file here instead.
  #
  # Fills BLANKS ONLY: a re-run that passes a corrected token must win over the
  # stale value on disk.
  local f="$CHECKOUT/.env" key val
  [ -f "$f" ] || return 0
  for key in RESEARCHER_EMAIL HF_TOKEN FLORA_POOL_REPO FLORA_POOL_DIR; do
    if [ -z "${!key}" ]; then
      val="$(sed -n "s/^${key}=//p" "$f" | head -1)"
      if [ -n "$val" ]; then export "$key=$val"; fi
    fi
  done
  return 0
}

require_inputs() {
  [ -n "$RESEARCHER_EMAIL" ] || die "RESEARCHER_EMAIL is not set. OpenAlex asks for a contact address; set it and re-run."
  [ -n "$HF_TOKEN" ] || die "HF_TOKEN is not set. The pool is published to a PRIVATE Hugging Face dataset repo, so a write-scoped token is required."
  [ -n "$FLORA_POOL_REPO" ] || die "FLORA_POOL_REPO is not set (e.g. my-org/flora-survivor-pool)."
  case "$FLORA_POOL_REPO" in
    */*) : ;;
    *) die "FLORA_POOL_REPO must be <owner>/<name>, got a value without a slash." ;;
  esac
}

install_system_deps() {
  # Only touches apt when something is actually missing, so a re-run costs nothing.
  local missing=0
  for cmd in git tmux python3 flock; do command -v "$cmd" >/dev/null || missing=1; done
  # "import venv" succeeds on Ubuntu's bare python3; it is ensurepip that lives in
  # the separate python3-venv package and that "python3 -m venv" actually needs.
  python3 -c "import venv, ensurepip" >/dev/null 2>&1 || missing=1
  if [ "$missing" -eq 0 ]; then log "System dependencies already present"; return; fi

  log "Installing system dependencies (apt)"
  export DEBIAN_FRONTEND=noninteractive
  # A fresh instance races cloud-init's own apt run; DPkg::Lock::Timeout waits for it
  # instead of failing the bootstrap on a lock we know will be released.
  local apt_opts=(-o DPkg::Lock::Timeout=600 -y)
  apt-get update "${apt_opts[@]}"
  apt-get install "${apt_opts[@]}" --no-install-recommends \
    git tmux ca-certificates curl util-linux python3 python3-venv python3-pip
}

sync_checkout() {
  mkdir -p "$WORK_DIR" "$LOG_DIR"
  chmod 755 "$WORK_DIR" "$LOG_DIR"
  if [ ! -d "$CHECKOUT/.git" ]; then
    log "Cloning $REPO_URL into $CHECKOUT"
    git clone --no-checkout "$REPO_URL" "$CHECKOUT"
  fi
  git -C "$CHECKOUT" fetch --tags --force origin
  # Works for a branch, a tag or a bare SHA; detached HEAD is what we want on a
  # throwaway box — the run is pinned to exactly one ref.
  git -C "$CHECKOUT" checkout --force --detach "origin/$REPO_REF" 2>/dev/null \
    || git -C "$CHECKOUT" checkout --force --detach "$REPO_REF"
  log "Checkout at $(git -C "$CHECKOUT" rev-parse --short HEAD) ($REPO_REF)"
}

build_venv() {
  if [ ! -x "$PY" ]; then
    log "Creating virtualenv at $VENV"
    python3 -m venv "$VENV"
  fi
  log "Installing Python dependencies (pip)"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$CHECKOUT/requirements.txt"
}

write_env() {
  # The ONLY place the secrets land on disk, and only readable by root.
  local env_file="$CHECKOUT/.env"
  umask 077
  cat > "$env_file" <<EOF
# Written by scripts/aws_snapshot_scan.sh — do not commit, do not copy off the box.
RESEARCHER_EMAIL=$RESEARCHER_EMAIL
HF_TOKEN=$HF_TOKEN
FLORA_POOL_REPO=$FLORA_POOL_REPO
FLORA_POOL_DIR=$FLORA_POOL_DIR
EOF
  chmod 600 "$env_file"
  mkdir -p "$FLORA_POOL_DIR"
  log "Wrote $env_file (mode 600) and pool directory $FLORA_POOL_DIR"
}

preflight() {
  # Everything that can fail cheaply, before anything that fails expensively.
  log "Pre-flight: Hugging Face write access to $FLORA_POOL_REPO"
  (cd "$CHECKOUT" && "$PY" -m search.pool_sync --check-access) \
    || die "Hugging Face pre-flight FAILED — fix the token or the repo before scanning. Nothing has been scanned."

  log "Pre-flight: OpenAlex snapshot manifest"
  (cd "$CHECKOUT" && "$PY" - <<'PYEOF'
from search.snapshot_scan import fetch_manifest, _manifest_files
files = _manifest_files(fetch_manifest())
size = sum(int((m.get("content_length") or 0)) for _, m in files)
records = sum(int((m.get("record_count") or 0)) for _, m in files)
print(f"manifest OK: {len(files):,} files, {size / 1e9:,.0f} GB, {records:,} records")
PYEOF
  ) || die "Could not read the OpenAlex parquet manifest — no point starting the scan."

  local free_gb
  free_gb=$(df -BG --output=avail "$WORK_DIR" | tail -1 | tr -dc '0-9')
  log "Free disk at $WORK_DIR: ${free_gb} GB"
  [ "${free_gb:-0}" -ge 25 ] || die "Only ${free_gb} GB free at $WORK_DIR. The snapshot is streamed, but the pool (~3 GB) and its parquet writes need room — 25 GB free is the floor, and VOLUME_GB=100 is the recommended size."
}

run_pipeline() {
  # The long phase. Guarded by flock so a second invocation observes rather than
  # competes: two scanners would fight over the same ledger and survivor pool.
  [ -x "$PY" ] || die "No virtualenv at $VENV — run the bootstrap first (sudo -E bash $0)."
  [ -f "$CHECKOUT/.env" ] || die "No $CHECKOUT/.env — run the bootstrap first, it writes the credentials."
  exec 9>"$LOCK"
  flock -n 9 || die "Another scan is already running (lock $LOCK). Watch it with: $SELF_COPY status"

  cd "$CHECKOUT"
  local started
  started=$(date -u +%s)

  log "=== Snapshot scan starting (resumes automatically if a ledger exists) ==="
  "$PY" -m search.run_search --scan --survivor-pool "$FLORA_POOL_DIR"
  log "=== Snapshot scan finished ==="
  "$PY" -m search.snapshot_scan --status --pool-dir "$FLORA_POOL_DIR" || true

  if [ "$PUSH_POOL" = "1" ]; then
    log "Pushing the survivor pool to $FLORA_POOL_REPO"
    retry 3 "$PY" -m search.pool_sync --push
  fi


  {
    echo "finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "elapsed_seconds=$(( $(date -u +%s) - started ))"
    echo "commit=$(git -C "$CHECKOUT" rev-parse HEAD)"
    echo "repo=$FLORA_POOL_REPO"
  } > "$DONE_MARKER"
  log "=== DONE — summary at $DONE_MARKER, status below ==="
  "$PY" -m search.snapshot_scan --status --pool-dir "$FLORA_POOL_DIR" || true

  if [ "$SHUTDOWN_WHEN_DONE" = "1" ]; then
    log "SHUTDOWN_WHEN_DONE=1 — powering off in 60s"
    shutdown -h +1 || true
  fi
}

retry() {
  local attempts="$1"; shift
  # Capture the command BEFORE the loop: inside it, "$1" is the interpreter path,
  # which named the wrong thing in every failure message.
  local what="$*"
  local n=1
  until "$@"; do
    if [ "$n" -ge "$attempts" ]; then
      die "Command failed after $attempts attempts: $what (the scan itself is safe on disk; re-run this script to retry publishing)"
    fi
    log "Attempt $n/$attempts failed — retrying in $(( n * 30 ))s"
    sleep $(( n * 30 ))
    n=$(( n + 1 ))
  done
}

show_status() {
  [ -x "$PY" ] || die "No virtualenv at $VENV yet — the bootstrap has not got that far. See $LOG"
  (cd "$CHECKOUT" && "$PY" -m search.snapshot_scan --status --pool-dir "$FLORA_POOL_DIR")
  if [ -f "$DONE_MARKER" ]; then echo "--- $DONE_MARKER ---"; cat "$DONE_MARKER"; fi
  echo "--- last 20 log lines ($LOG) ---"
  tail -n 20 "$LOG" 2>/dev/null || echo "(no log yet)"
}

start_detached() {
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    log "A '$TMUX_SESSION' tmux session already exists — leaving it alone."
    log "Attach with: tmux attach -t $TMUX_SESSION    Status: $SELF_COPY status"
    return
  fi
  log "Starting the scan detached in tmux session '$TMUX_SESSION' (log: $LOG)"
  # tmux, not nohup: an SSH drop cannot kill it and the maintainer can attach to a
  # live terminal. Output is teed to the log so `status` works without attaching.
  tmux new-session -d -s "$TMUX_SESSION" \
    "bash '$SELF_COPY' --run >> '$LOG' 2>&1"
  sleep 2
  tmux has-session -t "$TMUX_SESSION" 2>/dev/null \
    || die "tmux session did not start — see $LOG"
}

# ── Entry point ───────────────────────────────────────────────────────────────

case "${1:-}" in
  status)
    show_status
    exit 0
    ;;
  --run)
    # Re-entry from tmux (or a manual foreground run): env comes from .env, which the
    # bootstrap already wrote, so only the long phase runs here.
    run_pipeline
    exit 0
    ;;
  "" | --bootstrap)
    ;;
  *)
    die "Unknown argument '${1}'. Usage: $0 [--bootstrap|--run|status]"
    ;;
esac

# Inputs first: a missing token is the cheapest possible failure and must not wait
# behind a root check, an apt run or a clone. On a resume the environment is empty
# and everything comes back from $CHECKOUT/.env.
load_env_fallback
require_inputs
[ "$(id -u)" -eq 0 ] || die "Run as root (sudo -E bash $0) — it installs packages and writes to $WORK_DIR."

mkdir -p "$WORK_DIR" "$LOG_DIR"
chmod 755 "$WORK_DIR" "$LOG_DIR"
touch "$LOG"; chmod 640 "$LOG"
# Everything the bootstrap says goes to the log as well as to the console (which, under
# EC2 user-data, is /var/log/cloud-init-output.log). No secret is ever echoed.
exec > >(tee -a "$LOG") 2>&1

install_system_deps
sync_checkout
build_venv
write_env

# Keep a copy of THIS script next to the checkout, so `--run` and `status` have a
# stable path whether the original came from user-data or a git checkout.
#
# The `export HF_TOKEN=…` line aws_launch.sh injects at the top of the user-data
# script is stripped out: the token belongs in .env (mode 600) and nowhere else, and
# a second copy of it on disk is a second thing to worry about. A resume reads it
# back through load_env_fallback.
#
# Skipped when we ARE the self-copy (the documented resume command runs this file):
# `cp -f x x` fails, and redirecting into the script bash is still reading corrupts
# the run.
if [ "$(readlink -f "$0")" != "$(readlink -f "$SELF_COPY")" ]; then
  grep -v '^export HF_TOKEN=' "$0" > "$SELF_COPY.tmp"
  mv -f "$SELF_COPY.tmp" "$SELF_COPY"
  chmod 700 "$SELF_COPY"
fi

preflight
start_detached

cat <<EOF

Snapshot scan started.
  log        : $LOG        (tail -f $LOG)
  attach     : tmux attach -t $TMUX_SESSION
  status     : sudo bash $SELF_COPY status
  pool       : $FLORA_POOL_DIR
  publishes  : $FLORA_POOL_REPO (survivor pool)
  done marker: $DONE_MARKER
EOF
