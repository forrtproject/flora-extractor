"""
Replication-discovery engine — Python port of SciMeto's TS engine.

The engine reads the YAML spec at ``search/spec/`` and runs OR-bundled phrase
searches against OpenAlex, Crossref, and Semantic Scholar. Output is a stream
of normalized candidates ready to be flattened into Stage 1's candidates.csv.

The TS source-of-truth lives at:
    apps/worker/src/services/replication/discovery/

The hand-off contract (per ``search/spec/README.md``) is that the YAML files
travel verbatim and the engine modules are re-implementations, not interpreters
of the TS code. Algorithm equivalence is checked by the SciMeto benchmark
harness; if you change behaviour here, run that benchmark before merging.
"""
