"""Routing: every row of a pool batch into exactly one pile.

The pile resolves once, by the highest-precedence matching rule; multi-match is
normal and the full set of matches is kept in `matched_rules` because overlap
between rules is what diagnostics measure. Two things are engine policy rather
than spec: an unclaimed row is `pending/no_filter_matched` (a rule that does not
fire has said nothing, not "keep"), and a row routed to a screening tier whose
abstract is empty is downgraded to `pending/no_text` — absence of evidence must
not convert into a proceed. Discards are unaffected: structural rules do not
read the abstract.

Shadow specs are evaluated (`eval_all()`) but never win a pile; store.py records
their evaluations so an unmeasured rule can be measured before it is trusted.
"""

from typing import Optional

import numpy as np
import pyarrow as pa

from filter.engine.backends import BatchContext, eval_spec_batch, match_evidence
from filter.engine.spec import FilterSpec
from filter.engine.workids import resolve, work_id

# The piles the no-text downgrade applies to: only an LLM tier needs text.
_TEXT_PILES = frozenset({"screen_expensive", "screen_cheap"})

ROUTING_SCHEMA = pa.schema([
    ("work_id", pa.int64()),
    ("pile", pa.string()),
    ("pending_reason", pa.string()),
    ("rule_id", pa.string()),
    ("precedence", pa.int32()),
    ("matched_rules", pa.list_(pa.string())),
    ("evidence", pa.string()),
])


def eval_all(specs: list[FilterSpec], batch: pa.RecordBatch,
             ctx: Optional[BatchContext] = None) -> dict[str, pa.Array]:
    """Every spec's mask over *batch*, shadow specs included, one scan of the batch."""
    ctx = ctx or BatchContext(batch)
    return {spec.id: eval_spec_batch(spec, batch, ctx) for spec in specs}


def route_batch(specs: list[FilterSpec], batch: pa.RecordBatch,
                aliases: Optional[dict[int, int]] = None,
                evals: Optional[dict[str, pa.Array]] = None) -> pa.Table:
    """Route every row of *batch*, keyed by alias-resolved work id."""
    ctx = BatchContext(batch)
    if evals is None:
        evals = eval_all(specs, batch, ctx)
    aliases = aliases or {}

    n = batch.num_rows
    # load_specs() sorts by (-precedence, id), so the first spec to claim a row
    # is its winner and appending in order gives matched_rules precedence-desc.
    routable = [s for s in specs if not s.shadow]

    winner: list[Optional[FilterSpec]] = [None] * n
    matched: list[list[str]] = [[] for _ in range(n)]
    for spec in routable:
        mask = np.asarray(evals[spec.id].to_numpy(zero_copy_only=False), dtype=bool)
        for index in np.nonzero(mask)[0]:
            matched[index].append(spec.id)
            if winner[index] is None:
                winner[index] = spec

    no_text = np.asarray(ctx.abstract_empty.to_numpy(zero_copy_only=False), dtype=bool)
    piles: list[str] = []
    reasons: list[str] = []
    rule_ids: list[str] = []
    precedences: list[int] = []
    for index in range(n):
        spec = winner[index]
        if spec is None:
            piles.append("pending")
            reasons.append("no_filter_matched")
            rule_ids.append("")
            precedences.append(0)
            continue
        downgraded = spec.pile in _TEXT_PILES and bool(no_text[index])
        piles.append("pending" if downgraded else spec.pile)
        reasons.append("no_text" if downgraded else "")
        rule_ids.append(spec.id)
        precedences.append(spec.precedence)

    return pa.Table.from_pydict({
        "work_id": _work_ids(batch, aliases),
        "pile": piles,
        "pending_reason": reasons,
        "rule_id": rule_ids,
        "precedence": precedences,
        "matched_rules": matched,
        "evidence": _evidence(batch, winner),
    }, schema=ROUTING_SCHEMA)


def _work_ids(batch: pa.RecordBatch, aliases: dict[int, int]) -> list[int]:
    return [resolve(work_id(value), aliases) for value in batch.column("id").to_pylist()]


def _evidence(batch: pa.RecordBatch, winner: list[Optional[FilterSpec]]) -> list[str]:
    """What the winning rule matched, recovered for the winners only.

    The backend answers whether a row matched, not where; recovering the matched
    substring costs a regex span per row, so it is paid once, for one rule, over
    the sub-batch of the rows that rule actually claimed.
    """
    evidence = [""] * len(winner)
    by_spec: dict[str, tuple[FilterSpec, list[int]]] = {}
    for index, spec in enumerate(winner):
        if spec is not None:
            by_spec.setdefault(spec.id, (spec, []))[1].append(index)
    for spec, indices in by_spec.values():
        claimed = batch.take(pa.array(indices, type=pa.int64()))
        for index, found in zip(indices, match_evidence(spec, claimed)):
            evidence[index] = found
    return evidence
