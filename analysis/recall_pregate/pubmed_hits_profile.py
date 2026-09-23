"""Profile the PubMed stem hits the pool does not hold: what sense of 'replicat*' fired.

Re-parses the sampled baseline files (local, from pubmed_probe.py) for the hit PMIDs
only, and buckets each abstract by crude sense markers: study-replication language vs.
molecular/viral replication vs. measurement reproducibility. Crude by design — it is an
order-of-magnitude estimate of how much of the new intake Stage 2 will discard, not a rule.

    .venv/bin/python -m analysis.recall_pregate.pubmed_hits_profile
"""

import gzip
import re
from pathlib import Path

import pandas as pd
from lxml import etree

HERE = Path(__file__).resolve().parent
C = HERE / "cache"

STUDY = re.compile(r"(?i)replicat\w* (?:stud|sample|cohort|analys|attempt|effort|of (?:the |a |these |our |previous |prior |earlier )?(?:find|result|effect|stud|association|previous|prior|earlier|work))"
                   r"|(?:fail\w*|attempt\w*|sought|aim\w*|seek\w*|tried) to replicate|we replicat|not replicat|(?:successfully|independently|partially|directly|conceptually) replicat"
                   r"|replicat\w* (?:and extend|previous|prior|earlier|these|those|this finding|the finding)|replicability|reproducibility (?:crisis|project)|reanaly")
MOLECULAR = re.compile(r"(?i)(?:viral|virus|dna|rna|genome|plasmid|chromosom|telomer|mitochondrial|bacterial|cell|intracellular|hiv|hbv|hcv)\w* replicat|replicat\w* (?:fork|origin|protein|complex|cycle|stress|timing|kinetics|competen|defect)|self-replicat|replicon|reproductive|reproduction")
MEASURE = re.compile(r"(?i)reproducib|reproducibl|replicate (?:measure|sample|well|experiment|determination|analys)|in (?:duplicate|triplicate)|replicates")


def main() -> None:
    hits = pd.read_parquet(C / "pubmed_stem_hits.parquet")
    new = hits[~hits.in_pool & (hits.oa_gate != True)]  # noqa: E712 — NaN = not in OpenAlex
    want = set(new.pmid)
    text = {}
    for f in sorted((C / "pubmed").glob("*.xml.gz")):
        for _, el in etree.iterparse(gzip.open(f), tag="PubmedArticle"):
            pmid = el.findtext("MedlineCitation/PMID")
            if pmid in want:
                art = el.find("MedlineCitation/Article")
                text[pmid] = " ".join("".join(t.itertext()) for t in art.findall("Abstract/AbstractText"))
            el.clear()
    new = new.assign(abstract=new.pmid.map(text).fillna(""))
    whole = new.title + " " + new.abstract

    def bucket(t: str) -> str:
        if STUDY.search(t):
            return "study replication language"
        if MOLECULAR.search(t):
            return "molecular/biological replication or reproduction"
        if MEASURE.search(t):
            return "measurement reproducibility / technical replicates"
        return "other"

    new = new.assign(bucket=whole.map(bucket))
    print(len(new), "new-to-pool stem hits in the PubMed sample")
    print(new.bucket.value_counts().to_string())
    print(new.groupby(pd.cut(new.year, [0, 1990, 2000, 2010, 2020, 2030])).size().to_string())
    new.drop(columns=["abstract"]).to_parquet(C / "pubmed_new_hits.parquet", index=False)
    for _, r in new[new.bucket == "study replication language"].sample(12, random_state=0).iterrows():
        print("-", r.year, r.title[:120])


if __name__ == "__main__":
    main()
