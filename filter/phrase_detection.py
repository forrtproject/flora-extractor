"""
The OpenAlex search vocabulary — both arms of the gate Stage 1's snapshot scan
admits on: the loose replication stem alternation and the OpenAlex concept ids.

This is NOT a rule set. Nothing here filters, excludes or classifies anything:
every filtering decision lives in the declarative JSON specs under
``filter/spec/`` and is evaluated by the filter engine, which is the single rule
set. The search terms are the one deliberate exception to that — a scan of the
OpenAlex snapshot has to name what it is looking for before any rule can be
evaluated, so this one vocabulary is shared between Stage 1's gate and Stage 2's
specs.

``REPLICATION_STEM_PATTERN`` is consumed by ``search/snapshot_scan.py``, which
runs the source string through pyarrow over the raw inverted-index JSON. Word
order does not exist in an inverted index, so only single-token tests are sound:
stems, never phrases.

``CONCEPT_IDS`` is the gate's other arm: works OpenAlex's own classifier has
tagged as being about replication or reproducibility, which reaches works whose
text carries no matching stem at all (an absent abstract, atypical wording).
The two arms are unioned — a work admitted by either is a survivor.

Non-English stems reach works the English ones cannot: 111 of 112 Korean 재검증
works and 10 of 11 반복검증 are invisible to the English stems. They carry no
matching phrases, so such a row can only ever reach a cheap tier — which is the
intent, since the LLM screen reads these languages and the rules do not.
"""

REPLICATION_STEM_PATTERN = (
    r"(?i)replicat|replicab|reproduc|reanalys|re-analys|reanalyz|re-analyz"
    r"|replikat|réplicat|replicaci|replicaç|replicazion|reproduç|reproduzi"
    r"|追試|반복검증|재검증")

# IDs verified 2026-06-23 against the OpenAlex concepts endpoint.
CONCEPT_IDS = [
    "C12590798",   # Replication (statistics) — 263k works
    "C9893847",    # Reproducibility — 121k works
]
