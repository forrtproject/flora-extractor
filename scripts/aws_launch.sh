#!/usr/bin/env bash
#
# Launch ONE EC2 instance that runs the full OpenAlex snapshot scan unattended and
# publishes the result to the private Hugging Face dataset repo. Run this LOCALLY.
#
#   export KEY_NAME=my-keypair FLORA_POOL_REPO=my-org/flora-survivor-pool
#   export RESEARCHER_EMAIL=you@example.com
#   read -rs HF_TOKEN && export HF_TOKEN     # typed, never in your shell history
#   bash scripts/aws_launch.sh
#
# It creates: one instance, and (only if you do not name one) one security group
# allowing SSH from your current public IP. Nothing else. The bootstrap script
# scripts/aws_snapshot_scan.sh is inlined into user-data, so the instance needs no
# access to anything but the public OpenAlex bucket, PyPI, GitHub and Hugging Face.
#
# Region: us-east-1 by default and on purpose — the OpenAlex parquet bucket lives
# there, and S3 → EC2 traffic inside one region is free. Running this anywhere else
# turns ~450 GB of reads into a data-transfer bill.

set -Eeuo pipefail

# ── REQUIRED — fill these in (or pass them as environment variables) ──────────
KEY_NAME="${KEY_NAME:-}"                 # EC2 key pair name for SSH (must already exist in REGION)
RESEARCHER_EMAIL="${RESEARCHER_EMAIL:-}" # OpenAlex politeness contact
HF_TOKEN="${HF_TOKEN:-}"                 # Hugging Face token WITH WRITE access to the repo below
FLORA_POOL_REPO="${FLORA_POOL_REPO:-}"   # private HF dataset repo, e.g. my-org/flora-survivor-pool

# ── Optional — sensible defaults ──────────────────────────────────────────────
REGION="${REGION:-us-east-1}"
# 4 vCPU is plenty: the scan is one sequential process, network- and decode-bound, so
# more cores buy nothing. c7i is the current Intel generation with 12.5 Gbps burst.
INSTANCE_TYPE="${INSTANCE_TYPE:-c7i.xlarge}"
# Root volume. The snapshot itself is STREAMED and never lands on disk; the space is
# for the OS and venv (~8 GB), the survivor pool (~2-3 GB), data/candidates.csv plus
# its sidecar index (a few GB), and the prebuilt candidates artifact (~1 GB). 100 GB
# is generous slack at ~$0.01 for the life of the run — undersizing it is the one
# failure that wastes the whole scan, oversizing it costs cents.
VOLUME_GB="${VOLUME_GB:-100}"
REPO_REF="${REPO_REF:-main}"                   # branch/tag/SHA the instance scans with
REPO_URL="${REPO_URL:-https://github.com/forrtproject/flora-extractor.git}"
BUILD_CANDIDATES="${BUILD_CANDIDATES:-1}"      # also build + push the prebuilt corpus
SHUTDOWN_WHEN_DONE="${SHUTDOWN_WHEN_DONE:-0}"  # 1 = power off after publishing (see runbook)
SPOT="${SPOT:-0}"                              # 1 = spot instance (~70% cheaper, LOSES the scan if reclaimed)
NAME_TAG="${NAME_TAG:-flora-snapshot-scan}"
# Leave empty to use the default VPC's default subnet and a security group this script
# creates. Set them if your account has no default VPC or you want an existing SG.
SUBNET_ID="${SUBNET_ID:-}"
SECURITY_GROUP_ID="${SECURITY_GROUP_ID:-}"
SSH_CIDR="${SSH_CIDR:-}"                       # default: your current public IP /32
FORCE_NEW="${FORCE_NEW:-0}"                    # 1 = launch even if one is already running

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP="$HERE/aws_snapshot_scan.sh"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v aws >/dev/null || die "The AWS CLI is not installed (https://aws.amazon.com/cli/)."
aws sts get-caller-identity --region "$REGION" >/dev/null \
  || die "AWS CLI is not authenticated for $REGION — run 'aws configure'."
[ -f "$BOOTSTRAP" ] || die "Missing $BOOTSTRAP"
[ -n "$KEY_NAME" ] || die "KEY_NAME is required (an EC2 key pair in $REGION — 'aws ec2 describe-key-pairs' lists yours)."
[ -n "$RESEARCHER_EMAIL" ] || die "RESEARCHER_EMAIL is required."
[ -n "$HF_TOKEN" ] || die "HF_TOKEN is required (write scope on $FLORA_POOL_REPO)."
[ -n "$FLORA_POOL_REPO" ] || die "FLORA_POOL_REPO is required, e.g. my-org/flora-survivor-pool."

aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" >/dev/null \
  || die "Key pair '$KEY_NAME' does not exist in $REGION."

# ── One scan at a time ────────────────────────────────────────────────────────
# A second launch is almost always a re-run of this command after losing the first
# instance's output, not a deliberate second scan. Two boxes doing the same 2–5 hour
# job double the bill and race each other's pushes to the same HF repo.
if [ "$FORCE_NEW" != "1" ]; then
  EXISTING="$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME_TAG" \
              "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null || echo "")"
  EXISTING="$(echo "$EXISTING" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  [ -z "$EXISTING" ] || die "An instance tagged Name=$NAME_TAG is already pending/running in $REGION: $EXISTING
  Check it first (ssh in, 'sudo bash /opt/flora/aws_snapshot_scan.sh status'), then either
  terminate it (aws ec2 terminate-instances --region $REGION --instance-ids $EXISTING)
  or set FORCE_NEW=1 if you really want a second scan."
fi

# ── The ref must exist on the REMOTE ─────────────────────────────────────────
# The instance clones from GitHub, so uncommitted or unpushed local work is invisible
# to it — including the pre-flight and status commands this runbook depends on. Catch
# that here rather than in a bootstrap failure five minutes after launch.
REMOTE_SHA="$(git ls-remote "$REPO_URL" "refs/heads/$REPO_REF" "refs/tags/$REPO_REF" 2>/dev/null | head -1 | cut -f1)"
[ -n "$REMOTE_SHA" ] || die "'$REPO_REF' is not a branch or tag on $REPO_URL — push it first (git push -u origin $REPO_REF)."
LOCAL_SHA="$(git -C "$HERE/.." rev-parse HEAD 2>/dev/null || echo "")"
if [ -n "$LOCAL_SHA" ] && [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
  echo "NOTE: your local HEAD (${LOCAL_SHA:0:8}) differs from origin/$REPO_REF (${REMOTE_SHA:0:8})."
  echo "      The instance scans the REMOTE ref. Push, or set REPO_REF, if that is not what you want."
fi

# ── AMI: asked for by name, never hardcoded ───────────────────────────────────
# Canonical publishes the current Ubuntu 24.04 LTS AMI id as a public SSM parameter,
# so this always launches a patched image instead of an id that rots in a script.
AMI_ID="$(aws ssm get-parameters --region "$REGION" \
  --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameters[0].Value' --output text)"
[ -n "$AMI_ID" ] && [ "$AMI_ID" != "None" ] || die "Could not resolve an Ubuntu 24.04 AMI in $REGION."
ROOT_DEVICE="$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI_ID" \
  --query 'Images[0].RootDeviceName' --output text)"

# ── Security group: reuse yours, or make a minimal SSH-only one ───────────────
if [ -z "$SECURITY_GROUP_ID" ]; then
  VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)"
  [ "$VPC_ID" != "None" ] || die "No default VPC in $REGION — set SUBNET_ID and SECURITY_GROUP_ID explicitly."
  SG_NAME="flora-snapshot-ssh"
  SECURITY_GROUP_ID="$(aws ec2 describe-security-groups --region "$REGION" \
    --filters Name=group-name,Values="$SG_NAME" Name=vpc-id,Values="$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)"
  if [ "$SECURITY_GROUP_ID" = "None" ] || [ -z "$SECURITY_GROUP_ID" ]; then
    echo "Creating security group $SG_NAME in $VPC_ID"
    SECURITY_GROUP_ID="$(aws ec2 create-security-group --region "$REGION" \
      --group-name "$SG_NAME" --vpc-id "$VPC_ID" \
      --description "SSH to the FLoRA snapshot scan instance" \
      --query 'GroupId' --output text)"
  fi
  if [ -z "$SSH_CIDR" ]; then
    MY_IP="$(curl -fsS https://checkip.amazonaws.com || true)"
    [ -n "$MY_IP" ] || die "Could not detect your public IP — set SSH_CIDR yourself (e.g. 203.0.113.4/32)."
    SSH_CIDR="${MY_IP//[$'\n\r']/}/32"
  fi
  echo "Allowing SSH from $SSH_CIDR"
  # Already-authorized is the normal second-run case, not an error.
  aws ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SECURITY_GROUP_ID" --protocol tcp --port 22 --cidr "$SSH_CIDR" \
    >/dev/null 2>&1 || true
fi

# ── User data: exported settings + the bootstrap script, verbatim ─────────────
# Written to a 600 temp file and deleted on exit: it carries HF_TOKEN, which must not
# sit in /tmp or in your shell history.
USER_DATA="$(mktemp)"
chmod 600 "$USER_DATA"
trap 'rm -f "$USER_DATA"' EXIT

{
  echo "#!/usr/bin/env bash"
  echo "export RESEARCHER_EMAIL='$RESEARCHER_EMAIL'"
  echo "export HF_TOKEN='$HF_TOKEN'"
  echo "export FLORA_POOL_REPO='$FLORA_POOL_REPO'"
  echo "export REPO_URL='$REPO_URL'"
  echo "export REPO_REF='$REPO_REF'"
  echo "export BUILD_CANDIDATES='$BUILD_CANDIDATES'"
  echo "export SHUTDOWN_WHEN_DONE='$SHUTDOWN_WHEN_DONE'"
  tail -n +2 "$BOOTSTRAP"    # everything but its shebang
} > "$USER_DATA"

# EC2 caps user-data at 16 KB of raw bytes. The bootstrap is ~12 KB, so there is room,
# but a future edit could cross the line silently and the instance would boot with a
# truncated script — catch it here instead.
UD_BYTES="$(wc -c < "$USER_DATA")"
[ "$UD_BYTES" -lt 16384 ] || die "User data is $UD_BYTES bytes, over EC2's 16384-byte limit. Trim scripts/aws_snapshot_scan.sh, or host it and fetch it from a short bootstrap."

MARKET_OPTS=()
if [ "$SPOT" = "1" ]; then
  # One-time spot request. A reclaim TERMINATES the instance, and the root volume is
  # DeleteOnTermination — so the ledger, the survivor pool and candidates.csv go with
  # it and the next launch starts from zero. Spot is for a run somebody is watching
  # and willing to restart from scratch; for a one-shot unattended scan use the
  # default on-demand, where ~$1 of extra cost buys the whole scan back.
  echo "WARNING: SPOT=1 — a spot reclaim terminates the instance and DELETES the root"
  echo "         volume, losing everything scanned so far. On-demand is the default"
  echo "         for a one-shot run."
  MARKET_OPTS=(--instance-market-options 'MarketType=spot')
fi
SUBNET_OPTS=()
[ -n "$SUBNET_ID" ] && SUBNET_OPTS=(--subnet-id "$SUBNET_ID")

echo "Launching $INSTANCE_TYPE in $REGION from $AMI_ID (${VOLUME_GB} GB root)…"
INSTANCE_ID="$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SECURITY_GROUP_ID" \
  "${SUBNET_OPTS[@]}" \
  "${MARKET_OPTS[@]}" \
  --block-device-mappings "[{\"DeviceName\":\"$ROOT_DEVICE\",\"Ebs\":{\"VolumeSize\":$VOLUME_GB,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true,\"Encrypted\":true}}]" \
  --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME_TAG}]" \
  --user-data "file://$USER_DATA" \
  --query 'Instances[0].InstanceId' --output text)"

echo "Instance $INSTANCE_ID — waiting for it to run…"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
PUBLIC_IP="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"

cat <<EOF

Launched.
  instance      : $INSTANCE_ID  ($INSTANCE_TYPE, $REGION$([ "$SPOT" = "1" ] && echo ", SPOT"))
  public IP     : $PUBLIC_IP
  ssh           : ssh -i ~/.ssh/$KEY_NAME.pem ubuntu@$PUBLIC_IP
  bootstrap log : sudo tail -f /var/log/cloud-init-output.log
  scan log      : sudo tail -f /var/log/flora/scan.log
  progress      : sudo bash /opt/flora/aws_snapshot_scan.sh status
  finished when : /opt/flora/DONE exists
  terminate     : aws ec2 terminate-instances --region $REGION --instance-ids $INSTANCE_ID

The Hugging Face pre-flight runs in the first minute — if the token is wrong you will
see it at the end of cloud-init-output.log long before any scan time is spent.
EOF
