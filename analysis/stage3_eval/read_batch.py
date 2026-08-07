"""Dump what one sandbox batch decided, one block per work, for adjudication.

Adjudication is a human (or model) reading the stored payload and assigning one of
`correct_settle` / `wrong_settle` / `correct_open` / `missed` — see
docs/stage3-quality-handover.md §1.3. This script renders exactly the evidence that
decision is made on and nothing else: the paper, the verdict, the rung that answered,
what was acquired, what was searched, and each target row's original.

    python -m analysis.stage3_eval.read_batch eval-dev-0 [--out FILE]

Reads only; the claims client is opened for its verdict rows.
"""

import argparse
import json
import sys
from pathlib import Path

import shared.config  # noqa: F401  — ClaimsClient raises ClaimsNotConfigured without it
from extract.tier import TIER_EXTRACT
from filter.engine.claims import ClaimsClient

HERE = Path(__file__).parent


def batch_results(label: str, mode: str = "validation") -> list[dict]:
    """Live result rows of the *label* batch, newest first per work.

    Matched on `meta.batch`, which is what `run_tier` writes — `meta.batch_label` is
    the correction path's key and names something else.
    """
    client = ClaimsClient()
    claims = {c["id"]: (c.get("meta") or {}) for c in client.claims(tier=TIER_EXTRACT)}
    wanted = {cid for cid, meta in claims.items()
              if meta.get("batch") == label and (meta.get("mode") or "live") == mode}
    if not wanted:
        raise SystemExit(f"no {mode}-mode claims carry meta.batch == {label!r}")
    rows = [r for r in client.verdicts(TIER_EXTRACT, with_payload=True)
            if r.get("claim_id") in wanted]
    results = [r for r in rows if (r.get("payload") or {}).get("kind") == "result"]
    latest: dict[int, dict] = {}
    for row in sorted(results, key=lambda r: str(r.get("created_at") or "")):
        latest[int(row["work_id"])] = row          # last write per work wins
    return [latest[k] for k in sorted(latest)]


def render(row: dict) -> str:
    payload = row.get("payload") or {}
    src, link = payload.get("input") or {}, payload.get("link") or {}
    out = [f"### work {row['work_id']}  →  {row.get('verdict', '')}",
           f"title_r: {src.get('title_r', '')[:160]}",
           f"doi_r:   {payload.get('doi_r', '') or '(none)'}   "
           f"url_r: {src.get('url_r', '')[:90]}",
           f"screen:  {src.get('screen_verdict', '')} / "
           f"{src.get('screen_record_type', '')} / {src.get('screen_categories', '')}",
           f"rung:    {link.get('target_stage', '')}   "
           f"method: {link.get('link_method', '')}   "
           f"resolved: {link.get('resolved')}   "
           f"targets: {link.get('n_targets')} named, "
           f"{link.get('unidentified_count')} unidentified",
           f"doc:     {link.get('pdf_source', '') or '(none)'} / "
           f"{link.get('parse_method', '') or '(none)'}"
           + (f"   ERROR: {link['error']}" if link.get("error") else ""),
           f"evidence: {link.get('link_evidence', '')}"]
    for target in payload.get("targets") or []:
        out.append(f"  - doi_o={target.get('doi_o', '') or '(none)'} "
                   f"[{target.get('link_method', '')}/{target.get('link_confidence', '')}] "
                   f"outcome={target.get('outcome', '')}\n"
                   f"    title_o: {str(target.get('title_o', ''))[:140]}\n"
                   f"    link_evidence: {str(target.get('link_evidence', ''))[:600]}")
    abstract = (src.get("abstract_r") or "")[:700]
    out.append(f"abstract_r: {abstract}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--mode", default="validation")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = batch_results(args.label, args.mode)
    text = (f"# {args.label} ({args.mode}) — {len(rows)} works\n\n"
            + "\n\n".join(render(r) for r in rows) + "\n")
    if args.out:
        Path(args.out).write_text(text)
        print(f"{len(rows)} works → {args.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
