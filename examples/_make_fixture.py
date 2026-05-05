"""
Generate a 5-row synthetic candidates.csv fixture for the offline demo.

The rows cover every Stage-2 path so the rule filter exercises all branches:
    - 10.1/a: clear replication (phrase + author-year cite)
    - 10.1/b: reproduction-only phrase + cite
    - 10.1/c: phrase but no author-year cite -> needs_review
    - 10.1/d: DNA exclusion fires before the phrase check -> false_positive
    - 10.1/e: no replication terminology at all -> false_positive

Used by examples/pipeline_example.bat / .sh in offline mode (LIVE_SEARCH=0)
because the bundled misc/sample_candidates.csv has unquoted commas in the
authors field that break pandas.read_csv.
"""

import sys
from pathlib import Path

import pandas as pd

ROWS = [
    {
        "doi_r": "10.1/a",
        "title_r": "A direct replication of ego depletion",
        "abstract_r": "A direct replication of Smith (2010). Effect was reduced.",
        "year_r": "2018",
        "authors_r": "X; Y",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    },
    {
        "doi_r": "10.1/b",
        "title_r": "Reproducibility of Brown (2018)",
        "abstract_r": "We tested the reproducibility of Brown (2018).",
        "year_r": "2021",
        "authors_r": "Y",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    },
    {
        "doi_r": "10.1/c",
        "title_r": "We replicate prior findings",
        "abstract_r": "We replicate prior findings without naming a target study.",
        "year_r": "2019",
        "authors_r": "Z",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    },
    {
        "doi_r": "10.1/d",
        "title_r": "DNA replication forks",
        "abstract_r": "We study DNA replication forks in eukaryotes.",
        "year_r": "2017",
        "authors_r": "W",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    },
    {
        "doi_r": "10.1/e",
        "title_r": "On consumer choice",
        "abstract_r": "A field experiment with no replication terminology.",
        "year_r": "2020",
        "authors_r": "V",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    },
]


def main(out_path: str | Path) -> None:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ROWS).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"wrote {len(ROWS)} rows to {out}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data/candidates.csv"
    main(target)
