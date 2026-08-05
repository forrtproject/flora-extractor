r"""The one evaluator for a filter spec over the survivor pool: pyarrow compute.

`eval_spec_batch()` decides every match, for every caller. There used to be a
second implementation in Python `re` for row-at-a-time callers, held equal to
this one by an RE2-safety check and a sampling `verify` command; a spec that
matched differently in the two was a live hazard the sampling could miss, so the
duplicate is gone. `eval_spec_rows()` survives as the row-shaped ENTRY POINT to
the same backend — it builds a one-batch table and calls `eval_spec_batch()` —
which is why an analysis script and a routing run cannot disagree about what a
rule matches.

Two things the evaluator still depends on. Specs must be regexes RE2 can run
(`spec.re2_error()` makes an unrunnable one a load-time error rather than a
mid-route crash); and it reads the DECOMPOSED match, never the loader-only
`pyre_regex` key — that key is a record of the lookaround original the
decomposition replaced, evaluated by nothing. Text is folded and NFC-normalised
where it becomes matchable (`_normalize_array()`): every Unicode space separator
to a plain space, the zero-width space and BOM away, so a phrase separated by
U+00A0 still reads as separated to RE2's ASCII-only `\s`.

Python `re` appears once more, in `match_evidence()`, and decides nothing: RE2
through pyarrow answers whether a condition matched, and `re` is asked only WHERE
in the string, because it is the only engine in the process that reports a span.
When the two read a span differently — `\w`/`\b` are Unicode-aware in `re` and
ASCII in RE2 — the locator finds nothing and the evidence names the condition
instead of inventing a phrase. No row's pile depends on it.
"""

import re
import unicodedata
from typing import Any, Optional

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from filter.engine.spec import FilterSpec, MatchBlock

# The pool columns a match block can read (`spec._MATCH_KEYS` / `_FIELD_KEYS`),
# with the types the backend expects. `eval_spec_rows()` builds its batch from
# exactly these, so a row dict reaches the same code as a pool batch.
_ROW_SCHEMA = pa.schema([
    ("doi", pa.string()),
    ("title", pa.string()),
    ("display_name", pa.string()),
    ("abstract_text", pa.string()),
    ("concepts", pa.string()),
    ("type", pa.string()),
    ("publication_year", pa.int64()),
])


def _ci(pattern: str) -> str:
    """*pattern* case-insensitive for RE2. A leading `(?i)` already in the spec
    is harmless — RE2 accepts a repeated flag group."""
    return "(?i)" + pattern


def _concept_pattern(ids: tuple[Any, ...]) -> str:
    """Bare concept ids as one regex over the row's `concepts` JSON.

    The right edge has to be consumed rather than looked ahead at: RE2 has no
    lookahead, and an unanchored `C9893847` also matches `C98938470`, a different
    concept. In the JSON form every id is followed by a quote; `$` covers a bare
    id written at the very end of the value.
    """
    return "|".join(f"{re.escape(str(c))}(?:[^0-9]|$)" for c in ids)


# The pool's common encoding artifacts, folded away where text becomes
# matchable: every Unicode space separator (Zs) becomes a plain space, and the
# zero-width space / BOM vanish. RE2's `\s` is ASCII-only, so an abstract using
# U+00A0 as its word separator otherwise matches no spec's literal space.
_FOLD = {**{cp: " " for cp in (0x00A0, 0x1680, *range(0x2000, 0x200B),
                               0x202F, 0x205F, 0x3000)},
         0x200B: None, 0xFEFF: None}
_FOLD_RX = re.compile("[" + "".join(map(chr, _FOLD)) + "]")


def _normalize(text: str) -> str:
    """Encoding-artifact folding plus NFC, once, where text becomes matchable.

    NFC is the `nfd-stems` retirement: OpenAlex does not normalise, so a title
    spelled "re" + U+0301 + "plicat" matched no spec whose accented stem was
    written composed. That used to need a second stem rule with the decomposed
    spellings next to every composed one; normalising at the one seam where text
    becomes matchable gives every rule the same text instead. DOIs are not
    normalised — `_clean_doi_array()` owns their canonical form.
    """
    if _FOLD_RX.search(text):
        text = text.translate(_FOLD)
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# The backend (pyarrow compute)
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


def rows_to_batch(rows: list[dict]) -> pa.RecordBatch:
    """Row dicts as a batch of the columns a match block can read (`_ROW_SCHEMA`).

    Keys the schema does not name are dropped rather than inferred: a spec cannot
    read them, and inferring types from a handful of rows is how a column of all
    NULLs arrives as `pa.null()` and breaks a kernel that a pool batch feeds fine.
    """
    columns = {name: [row.get(name) for row in rows] for name in _ROW_SCHEMA.names}
    return pa.RecordBatch.from_pydict(columns, schema=_ROW_SCHEMA)


def eval_spec_rows(spec: FilterSpec, rows: list[dict]) -> list[bool]:
    """Whether each row in *rows* matches *spec* — the row-shaped entry point.

    Same backend, same answer: the rows become one batch and `eval_spec_batch()`
    decides. The Python `re` implementation this used to be is deleted, so an
    analysis script and a routing run cannot read a spec differently.
    """
    if not rows:
        return []
    return eval_spec_batch(spec, rows_to_batch(rows)).to_pylist()


# ---------------------------------------------------------------------------
# Evidence — WHERE a matched row matched, for `filter_evidence`
# ---------------------------------------------------------------------------


def match_evidence(spec: FilterSpec, batch: pa.RecordBatch,
                   ctx: Optional[BatchContext] = None) -> list[str]:
    """The first piece of each row of *batch* that made *spec* match ("" if none).

    Every match decision here is the backend's (`_match_batch`, `_re_match`);
    only the SPAN inside an already-matched string comes from Python `re`, which
    is the one engine in the process that reports one. A span RE2 and `re` read
    differently yields the condition's name instead of a phrase — a worse
    evidence string, never a different verdict.
    """
    return _block_evidence(spec.match, ctx or BatchContext(batch))


def _locate(pattern: str, text: str) -> Optional[str]:
    """The substring *pattern* matched in *text*, or None if `re` cannot find one."""
    found = re.search(pattern, text, re.IGNORECASE)
    return found.group(0) if found else None


def _block_evidence(block: MatchBlock, ctx: BatchContext) -> list[str]:
    out = [""] * ctx.n
    if block.doi_prefix:
        for index, doi in enumerate(ctx.doi.to_pylist()):
            registrant = (doi or "").split("/", 1)[0]
            if not out[index] and registrant in block.doi_prefix:
                out[index] = registrant
    for label, pattern, column in (("doi_regex", block.doi_regex, ctx.doi),
                                   ("title_regex", block.title_regex, ctx.title),
                                   ("abstract_regex", block.abstract_regex, ctx.abstract),
                                   ("text_regex", block.text_regex, ctx.text)):
        if pattern is None:
            continue
        _fill(out, _re_match(column, pattern), column,
              lambda text, pattern=pattern, label=label: _locate(pattern, text) or label)
    for name, values in block.fields:
        if name == "concept_ids":
            pattern = _concept_pattern(values)
            _fill(out, _re_match(ctx.concepts, pattern), ctx.concepts,
                  lambda text, pattern=pattern: _concept_evidence(pattern, text))
            continue
        column = ctx.column(name)
        if column is None:
            continue
        for index, value in enumerate(column.to_pylist()):
            if not out[index] and value in values:
                out[index] = f"{name}={value}"
    if block.abstract_missing is not None:
        for index in range(ctx.n):
            if not out[index]:
                out[index] = f"abstract_missing={block.abstract_missing}"
    for nested in (block.any_of, block.all_of):
        for child in nested:
            if all(out):
                break
            hits = _match_batch(child, ctx).to_pylist()
            child_evidence: Optional[list[str]] = None
            for index, hit in enumerate(hits):
                if hit and not out[index]:
                    if child_evidence is None:
                        child_evidence = _block_evidence(child, ctx)
                    out[index] = child_evidence[index]
    return out


def _fill(out: list[str], mask: pa.Array, column: pa.Array, evidence) -> None:
    """Write *evidence* of each matched, still-empty row; read the text lazily."""
    texts: Optional[list[str]] = None
    for index, hit in enumerate(mask.to_pylist()):
        if hit and not out[index]:
            if texts is None:
                texts = column.to_pylist()
            out[index] = evidence(texts[index] or "")


def _concept_evidence(pattern: str, text: str) -> str:
    # The pattern consumes one character past the id unless the id ended the
    # string (see _concept_pattern); report the id itself.
    raw = _locate(pattern, text)
    if raw is None:
        return "concept_ids"
    return "concept_ids=" + (raw if raw[-1].isdigit() else raw[:-1])


