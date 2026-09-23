"""Build cache/F_group.csv: the 417 cause-F Observatory works and their recovered text.

For each work the gate missed with no OpenAlex abstract (`not_in_pool_causes.csv`,
cause F): which store namespace holds recovered text (`screen_offline.text_for`),
whether that text fires the search-gate stem, and the offline screen's verdict. The
recall-gap set used everywhere else is `not ours AND stem fires` (165 works).

    .venv/bin/python -m analysis.recall_pregate.build_f_group
"""

import re
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.screen_offline import text_for
from filter.phrase_detection import REPLICATION_STEM_PATTERN
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
MO = HERE.parent / "mo_observatory"


def main() -> None:
    causes = pd.read_csv(MO / "not_in_pool_causes.csv")
    screened = pd.read_csv(MO / "screened.csv")[["doi_r", "screen_verdict"]]
    stem = re.compile(REPLICATION_STEM_PATTERN)
    rows = []
    for _, r in causes[causes.cause.str.startswith("F")].iterrows():
        doi = clean_doi(r.doi_clean or r.doi_r)
        text, src = text_for(doi, r.oa_id if isinstance(r.oa_id, str) else "")
        rows.append({"doi": doi, "oa_id": r.oa_id, "ours": r.ours_already, "src": src,
                     "alen": len(text), "stem": bool(stem.search(text))})
    out = pd.DataFrame(rows).merge(screened.rename(columns={"doi_r": "doi"}), how="left")
    (HERE / "cache").mkdir(exist_ok=True)
    out.to_csv(HERE / "cache" / "F_group.csv", index=False)
    print(out.groupby(["ours", "stem"]).size())


if __name__ == "__main__":
    main()
