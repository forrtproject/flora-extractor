"""Which labels a batch's stored payloads no longer support.

Labels were carried between iterations on the VERDICT alone, and that is not enough:
a work can keep `provisional` and change the paper it points at, because a route
changed underneath it. Work 3185325517 did exactly that — the title search's book
chapter became an author-and-year pick of a different record — and the stale label
sat under the headline wrong-settle count for two iterations.

    python -m analysis.stage3_eval.check_labels eval-dev-3 labels-dev-3.json

Prints one line per settled work whose recorded verdict or `doi_o` differs from the
label's, which is the list to re-adjudicate. Silence means the labels describe what is
on disk.
"""

import argparse
import json
from pathlib import Path

import shared.config  # noqa: F401  — ClaimsClient raises ClaimsNotConfigured without it
from analysis.stage3_eval.read_batch import batch_results

HERE = Path(__file__).parent
SETTLING = {"resolved", "provisional", "no_original_found", "not_a_replication"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("labels_file")
    ap.add_argument("--mode", default="validation")
    args = ap.parse_args()

    labels = json.loads((HERE / args.labels_file).read_text())["labels"]
    rows = {str(r["work_id"]): r for r in batch_results(args.label, args.mode)}

    drifted = 0
    for work, entry in sorted(labels.items()):
        row = rows.get(work)
        if row is None:
            print(f"{work}: no result row in {args.label}")
            drifted += 1
            continue
        dois = sorted({str(t.get("doi_o") or "").strip().lower()
                       for t in (row["payload"].get("targets") or [])} - {""})
        if row["verdict"] != entry["verdict"] or dois != entry["doi_o"]:
            print(f"{work}: label {entry['verdict']}/{entry['doi_o']} — "
                  f"payload {row['verdict']}/{dois}")
            drifted += 1
    print(f"{drifted} of {len(labels)} labels no longer describe the payload")


if __name__ == "__main__":
    main()
