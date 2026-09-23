#!/usr/bin/env bash
# One Codex judgement (CODEX_MODEL, default gpt-5.6-sol; outputs to CODEX_OUT) per packet, with web search; skips items already judged.
# Usage: analysis/mo_observatory/adjudication/run_codex.sh [parallelism]
set -u
cd "$(dirname "$0")"
judge() {
  id=$(basename "$1" .md)
  out="${CODEX_OUT:-out/codex}/$id.json"
  [ -s "$out" ] && return 0
  { cat judge_prompt.md; printf '\n\nThe item id is %s. Answer with the JSON object only.\n\n---\n\n' "$id"; cat "$1"; } |
    timeout 900 codex --search exec -m "${CODEX_MODEL:-gpt-5.6-sol}" --skip-git-repo-check --ephemeral \
      -s read-only -C /tmp --output-schema "$PWD/schema.json" -o "$out.tmp" - >"${CODEX_OUT:-out/codex}/$id.log" 2>&1 \
    && mv "$out.tmp" "$out" || echo "FAILED $id"
}
export -f judge; export CODEX_MODEL CODEX_OUT
ls ${PACKET_GLOB:-packets/*.md} | xargs -P "${1:-6}" -I{} bash -c 'judge {}'
