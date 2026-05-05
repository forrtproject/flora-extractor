# `filter/spec/` — portable spec for Stage 2

`exclusion-patterns.yaml` is the same file that lives at `search/spec/` in the
`feature/search` branch. It is the SciMeto-authored list of non-scholarly
replication contexts (DNA, code/data, fork/origin/stress/timing) and travels
verbatim between the two stages.

`filter/phrase_detection.py` reads this file at import. The replication-phrase
regex set lives in code (not YAML) because it uses Python regex constructs
that don't round-trip cleanly through YAML — but the spec README in the search
branch documents the algorithmic equivalence to the SciMeto TS classifier.

If you need to update the exclusion list, update SciMeto first (the source of
truth for both stages) and copy the YAML over here. Run
`tests/test_filter.py` after changes to make sure the rule filter still
classifies the canned positives and negatives.
