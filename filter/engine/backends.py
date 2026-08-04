r"""Two evaluators for a filter spec over the survivor pool, equal by construction.

`eval_spec_rows()` is Python `re` over row dicts; `eval_spec_batch()` is the same
semantics in pyarrow compute. Neither is the reference: `verify_backends()` runs
both over a corpus and reports every row they disagree on, which is what keeps a
vectorized production run honest against a readable implementation.

Three things make the equality achievable rather than merely hoped for. Specs
are RE2-safe (`spec.re2_safe()`), so no pattern exists that only one engine can
run; both backends read the DECOMPOSED match, never the loader-only `pyre_regex`
key — that key is a record of the lookaround original the decomposition replaced,
evaluated by nothing, and evaluating it here would divide the backends by
construction; and both read the same characters, because `_normalize()` /
`_normalize_array()` fold the pool's encoding artifacts at the one seam per
backend — every Unicode space separator to a plain space, the zero-width space
and BOM away — so a phrase separated by U+00A0 reads the same to RE2's
ASCII-only `\s` as to Python's Unicode one.

One asymmetry survives and is not worth closing: Python's `\b`/`\w` are
Unicode-aware where RE2's are ASCII, so a pattern edge landing on a non-Latin or
mojibake character can still split the backends. `re.ASCII` would align those
escapes and break Unicode case folding, which RE2 does perform — a far larger
divergence than the handful of rows it would fix. `verify` samples the pool for
what is left.
"""

import re
import unicodedata
from typing import Any, Optional

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from filter.engine.spec import FilterSpec, MatchBlock
from shared.utils import clean_doi

# The pool's identity column (search/snapshot_scan.py::_POOL_SCHEMA).
_ID_COL = "id"

_COMPILED: dict[str, re.Pattern] = {}


def _compiled(pattern: str) -> re.Pattern:
    """*pattern* compiled for the row backend.

    Not `re.ASCII`, though RE2's `\\b`/`\\w`/`\\s` are ASCII-only and Python's are
    not: the flag also makes `IGNORECASE` ASCII-only, while RE2's `(?i)` case-folds
    Unicode, so an accented capital would stop matching its lowercase stem. Aligning
    the class escapes that way costs far more rows than the asymmetry it fixes.
    """
    rx = _COMPILED.get(pattern)
    if rx is None:
        rx = _COMPILED[pattern] = re.compile(pattern, re.IGNORECASE)
    return rx


def _ci(pattern: str) -> str:
    """*pattern* case-insensitive for RE2. A leading `(?i)` already in the spec
    is harmless — RE2 accepts a repeated flag group, Python's `re` would not,
    which is why the row backend uses the IGNORECASE flag instead."""
    return "(?i)" + pattern


def _concept_pattern(ids: tuple[Any, ...]) -> str:
    """Bare concept ids as one regex over the row's `concepts` JSON.

    The right edge has to be consumed rather than looked ahead at: RE2 has no
    lookahead, and an unanchored `C9893847` also matches `C98938470`, a different
    concept. In the JSON form every id is followed by a quote; `$` covers a bare
    id written at the very end of the value.
    """
    return "|".join(f"{re.escape(str(c))}(?:[^0-9]|$)" for c in ids)


# ---------------------------------------------------------------------------
# Row backend (Python re)
# ---------------------------------------------------------------------------


def _row_title(row: dict) -> str:
    # coalesce(display_name, title): NULL falls through, an empty string does
    # not — pyarrow's coalesce works that way and the backends must not differ.
    display_name = row.get("display_name")
    title = (display_name if display_name is not None else row.get("title")) or ""
    return _normalize(title)


def _row_abstract(row: dict) -> str:
    return _normalize(row.get("abstract_text") or "")


# The pool's common encoding artifacts, folded away where text becomes
# matchable: every Unicode space separator (Zs) becomes a plain space, and the
# zero-width space / BOM vanish. RE2's `\s` is ASCII-only while Python's is
# not, so an abstract using U+00A0 as its word separator otherwise matches one
# backend and not the other — and matches no spec's literal space in either.
_FOLD = {**{cp: " " for cp in (0x00A0, 0x1680, *range(0x2000, 0x200B),
                               0x202F, 0x205F, 0x3000)},
         0x200B: None, 0xFEFF: None}
_FOLD_RX = re.compile("[" + "".join(map(chr, _FOLD)) + "]")


def _normalize(text: str) -> str:
    """Encoding-artifact folding plus NFC, once, where text becomes matchable.

    NFC is the `nfd-stems` retirement: OpenAlex does not normalise, so a title
    spelled "re" + U+0301 + "plicat" matched no spec whose accented stem was
    written composed. That used to need a second stem rule with the decomposed
    spellings next to every composed one; normalising at the one seam per
    backend gives every rule the same text instead. DOIs are not normalised —
    `clean_doi()` owns their canonical form.
    """
    if _FOLD_RX.search(text):
        text = text.translate(_FOLD)
    return unicodedata.normalize("NFC", text)


def _row_text(row: dict) -> str:
    return _row_title(row) + "\n" + _row_abstract(row)


def _row_doi(row: dict) -> str:
    return clean_doi(row.get("doi") or "")


def _match_row(block: MatchBlock, row: dict) -> bool:
    """True if *row* satisfies every present condition of *block*."""
    if block.doi_prefix:
        doi = _row_doi(row)
        if doi.split("/", 1)[0] not in block.doi_prefix:
            return False
    if block.doi_regex is not None and not _compiled(block.doi_regex).search(_row_doi(row)):
        return False
    if block.title_regex is not None and not _compiled(block.title_regex).search(_row_title(row)):
        return False
    if block.abstract_regex is not None \
            and not _compiled(block.abstract_regex).search(_row_abstract(row)):
        return False
    if block.text_regex is not None and not _compiled(block.text_regex).search(_row_text(row)):
        return False
    for name, values in block.fields:
        if name == "concept_ids":
            if not _compiled(_concept_pattern(values)).search(row.get("concepts") or ""):
                return False
        elif row.get(name) not in values:
            return False
    if block.abstract_missing is not None:
        if (not _row_abstract(row).strip()) is not block.abstract_missing:
            return False
    if block.any_of and not any(_match_row(b, row) for b in block.any_of):
        return False
    if block.all_of and not all(_match_row(b, row) for b in block.all_of):
        return False
    if block.none_of and any(_match_row(b, row) for b in block.none_of):
        return False
    return True


def eval_spec_rows(spec: FilterSpec, rows: list[dict]) -> list[bool]:
    """Whether each row in *rows* matches *spec*, evaluated with Python `re`."""
    return [_match_row(spec.match, row) for row in rows]


# ---------------------------------------------------------------------------
# Evidence (row-wise only — the winning rule on matching rows)
# ---------------------------------------------------------------------------


def match_evidence(spec: FilterSpec, row: dict) -> str:
    """The first piece of *row* that made *spec* match, for `filter_evidence`."""
    return _block_evidence(spec.match, row) or ""


def _block_evidence(block: MatchBlock, row: dict) -> str:
    if block.doi_prefix:
        registrant = _row_doi(row).split("/", 1)[0]
        if registrant in block.doi_prefix:
            return registrant
    for pattern, text in ((block.doi_regex, _row_doi(row)),
                          (block.title_regex, _row_title(row)),
                          (block.abstract_regex, _row_abstract(row)),
                          (block.text_regex, _row_text(row))):
        if pattern is None:
            continue
        found = _compiled(pattern).search(text)
        if found:
            return found.group(0)
    for name, values in block.fields:
        if name == "concept_ids":
            found = _compiled(_concept_pattern(values)).search(row.get("concepts") or "")
            if found:
                # The pattern consumes one character past the id unless the id
                # ended the string (see _concept_pattern); report the id itself.
                raw = found.group(0)
                return "concept_ids=" + (raw if raw[-1].isdigit() else raw[:-1])
        elif row.get(name) in values:
            return f"{name}={row.get(name)}"
    if block.abstract_missing is not None:
        return f"abstract_missing={block.abstract_missing}"
    for nested in (block.any_of, block.all_of):
        for child in nested:
            if _match_row(child, row):
                evidence = _block_evidence(child, row)
                if evidence:
                    return evidence
    return ""


# ---------------------------------------------------------------------------
# Batch backend (pyarrow compute)
# ---------------------------------------------------------------------------


class BatchContext:
    """The derived columns every spec in a batch reads, computed once.

    `eval_all()` in route.py evaluates every spec in the bundle per batch;
    recomputing the coalesced title, the joined text and the cleaned DOI for each
    of them would be the dominant cost of a routing run.
    """

    def __init__(self, batch: pa.RecordBatch) -> None:
        self.batch = batch
        self.n = batch.num_rows
        names = set(batch.schema.names)

        def col(name: str) -> pa.Array:
            if name in names:
                return batch.column(name)
            return pa.nulls(self.n, pa.string())

        # Normalised at the same seam as the row backend's `_normalize()`.
        self.title = _normalize_array(
            pc.fill_null(pc.coalesce(col("display_name"), col("title")), ""))
        self.abstract = _normalize_array(pc.fill_null(col("abstract_text"), ""))
        self.text = pc.binary_join_element_wise(self.title, self.abstract, "\n")
        self.doi = _clean_doi_array(col("doi"))
        self.concepts = pc.fill_null(col("concepts"), "")
        self.abstract_empty = pc.equal(pc.utf8_trim_whitespace(self.abstract), "")

    def column(self, name: str) -> Optional[pa.Array]:
        return self.batch.column(name) if name in self.batch.schema.names else None


def _normalize_array(col: pa.Array) -> pa.Array:
    """`_normalize()` over a null-free string array, once per batch.

    Not `pc.utf8_normalize`: on pyarrow 25 it returns its input unchanged for
    NFC, so the two backends would part company on exactly the decomposed titles
    this exists for. The Python pass costs ~20 ms per 50k rows and is skipped
    entirely when the batch is already composed and artifact-free, which almost
    every batch is.
    """
    values = col.to_pylist()
    if all(unicodedata.is_normalized("NFC", value) and not _FOLD_RX.search(value)
           for value in values):
        return col
    return pa.array([_normalize(value) for value in values], type=pa.string())


def _clean_doi_array(col: pa.Array) -> pa.Array:
    """`shared.utils.clean_doi()` vectorized: strip, drop URL/`doi:` prefix, lower."""
    doi = pc.utf8_lower(pc.utf8_trim_whitespace(pc.fill_null(col, "")))
    doi = pc.replace_substring_regex(doi, "^https?://(?:dx\\.)?doi\\.org/", "")
    doi = pc.replace_substring_regex(doi, "^doi:", "")
    return pc.utf8_rtrim(pc.utf8_trim_whitespace(doi), "/")


def _re_match(arr: pa.Array, pattern: str) -> pa.Array:
    return pc.fill_null(pc.match_substring_regex(arr, _ci(pattern)), False)


def _match_batch(block: MatchBlock, ctx: BatchContext) -> pa.Array:
    mask = pa.array(np.ones(ctx.n, dtype=bool))
    if block.doi_prefix:
        prefix_hit = pa.array(np.zeros(ctx.n, dtype=bool))
        for prefix in block.doi_prefix:
            # A prefix is the DOI registrant: `10.7910` matches `10.7910/dvn/x`
            # and the bare registrant, never `10.79101/…`.
            hit = pc.or_(pc.starts_with(ctx.doi, prefix + "/"), pc.equal(ctx.doi, prefix))
            prefix_hit = pc.or_(prefix_hit, pc.fill_null(hit, False))
        mask = pc.and_(mask, prefix_hit)
    if block.doi_regex is not None:
        mask = pc.and_(mask, _re_match(ctx.doi, block.doi_regex))
    if block.title_regex is not None:
        mask = pc.and_(mask, _re_match(ctx.title, block.title_regex))
    if block.abstract_regex is not None:
        mask = pc.and_(mask, _re_match(ctx.abstract, block.abstract_regex))
    if block.text_regex is not None:
        mask = pc.and_(mask, _re_match(ctx.text, block.text_regex))
    for name, values in block.fields:
        if name == "concept_ids":
            mask = pc.and_(mask, _re_match(ctx.concepts, _concept_pattern(values)))
            continue
        column = ctx.column(name)
        if column is None:
            return pa.array(np.zeros(ctx.n, dtype=bool))
        value_set = pa.array(list(values), type=column.type)
        mask = pc.and_(mask, pc.fill_null(pc.is_in(column, value_set=value_set), False))
    if block.abstract_missing is not None:
        wanted = ctx.abstract_empty if block.abstract_missing \
            else pc.invert(ctx.abstract_empty)
        mask = pc.and_(mask, wanted)
    if block.any_of:
        any_hit = pa.array(np.zeros(ctx.n, dtype=bool))
        for child in block.any_of:
            any_hit = pc.or_(any_hit, _match_batch(child, ctx))
        mask = pc.and_(mask, any_hit)
    for child in block.all_of:
        mask = pc.and_(mask, _match_batch(child, ctx))
    for child in block.none_of:
        mask = pc.and_(mask, pc.invert(_match_batch(child, ctx)))
    return mask


def eval_spec_batch(spec: FilterSpec, batch: pa.RecordBatch,
                    ctx: Optional[BatchContext] = None) -> pa.Array:
    """Whether each row of *batch* matches *spec*, evaluated with pyarrow compute."""
    return _match_batch(spec.match, ctx or BatchContext(batch))


# ---------------------------------------------------------------------------
# Equality check
# ---------------------------------------------------------------------------


def verify_backends(specs: list[FilterSpec], table: pa.Table) -> list[str]:
    """Every (spec, row) the two backends disagree on. Empty means they agree."""
    reports: list[str] = []
    rows = table.to_pylist()
    for spec in specs:
        by_row = eval_spec_rows(spec, rows)
        by_batch: list[bool] = []
        for batch in table.to_batches():
            by_batch.extend(eval_spec_batch(spec, batch).to_pylist())
        for row, row_value, batch_value in zip(rows, by_row, by_batch):
            if bool(row_value) != bool(batch_value):
                reports.append(
                    f"{spec.id}: work {row.get(_ID_COL)!r}: "
                    f"eval_spec_rows={row_value} eval_spec_batch={batch_value}")
    return reports
