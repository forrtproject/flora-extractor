"""Score candidate filter patterns against every label this repo already owns.

MEASUREMENT ONLY — no LLM call, no network call, no spend. The question it
answers is "should this rule or this arm go live?", and it answers it in seconds
so that a dozen pattern variants can be tried for the price of one thought.

Five measures per arm, all of them free:

1. **Pool volume** — rows of `cache/snapshot_pool` the arm matches, and how many
   it matches ALONE (exclusive). Exclusive volume is what a rule costs on its own.
2. **Admitted** — of those, how many the stored routing release put in a
   screening pile. Read off the release, never recomputed, so `admitted` means
   exactly what routing meant. Reported as unavailable when the store is absent.
3. **FLoRA capture** — `data/flora.csv` is the gold standard (`analysis/gold/`).
   Per arm: known-good replications reached, reached exclusively, and the
   **yield** (FLoRA-exclusive per 1,000 exclusive pool rows) — the ranking signal.
4. **Known negatives** — `data/not_a_replication.csv`, matched on its own
   title/abstract columns.
5. **Cached screen precision** — `cache/llm/classify_*.json` holds two-voter
   verdicts already paid for. Grouped into cohorts by (prompt template, voter
   models); only the largest cohort is scored by default so the instrument is
   held constant.

**Read the numbers as what they are.** The cached verdicts and both CSVs are
drawn from what the OLD filter admitted, so every precision here is optimistic;
and FLoRA is partly circular, because much of it was found with these very
phrases. This tool ranks candidates and gates cheap decisions. It does not
replace a human-labelled precision estimate for anything that discards.

Matching semantics come from `filter/engine/backends.py` — the same normalisation
and the same evaluator routing uses — so a number printed here is a number
routing would produce.

Usage:
    python -m analysis.arm_evidence --spec filter/spec/replication-claim.json
    python -m analysis.arm_evidence --pattern 'title:\\breplication stud(?:y|ies)\\b' \\
                                    --pattern 'text:\\breplication stud(?:y|ies)\\b'
    python -m analysis.arm_evidence --spec filter/spec/replication-claim.json \\
                                    --and '\\(\\s*\\w+' --json out.json
"""

import argparse
import csv
import glob
import hashlib
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from filter.engine.backends import (
    BatchContext,
    _ci,
    _clean_doi_array,
    eval_spec_batch,
    eval_spec_rows,
)
from filter.engine.export import ALIASES_FILENAME, SPEC_DIR
from filter.engine.spec import FilterSpec, MatchBlock
from filter.engine.store import DEFAULT_STORE_PATH
from filter.engine.workids import load_aliases, resolve, work_id
from shared.config import DATA_DIR, LLM_CACHE_DIR, SNAPSHOT_POOL_DIR
from shared.utils import clean_doi

# The piles that mean "this row will be looked at" (filter/spec/CONVENTIONS.md).
SCREENING_PILES = ("screen_expensive", "screen_cheap", "needs_human")

# Below this many labelled rows a cell is decoration, not evidence.
MIN_LABELLED = 15
DECORATION = "†"

FIELDS = ("title", "abstract", "text")

# The classify prompt's per-paper block (shared/prompts.py::_CLASSIFY_PROMPT).
# The title and abstract are recovered by cutting between these; hashing what is
# left identifies the prompt template that produced a cached verdict.
_MARK_TITLE = "\n\nTitle: "
_MARK_ABSTRACT = "\n\nAbstract: "
_MARK_END = "\n\nRespond with the JSON object only."

# The pool columns matching actually reads. `abstract_text` is 47% of file bytes
# and `concepts`/`authorships`/`primary_location` another 43% that no arm looks
# at — naming the columns is most of why a full scan takes seconds.
_MATCH_COLS = ("display_name", "title", "abstract_text")

# Every Unicode space `_normalize()` folds to a plain space, as a character-class
# body. The prefilter runs on RAW text, so its whitespace has to accept the
# unfolded spellings too or it would drop rows the exact pass would keep.
_FOLD_BODY = "\\s\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000\ufeff"


@dataclass(frozen=True)
class Arm:
    """One pattern to be scored: what is evaluated, and what to call it."""

    id: str
    label: str
    spec: FilterSpec


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def parse_pattern(value: str) -> tuple[str, str]:
    """`'title:\\bfoo'` → `("title", "\\bfoo")`; a bare pattern is `text`."""
    for field in FIELDS:
        if value.startswith(field + ":"):
            return field, value[len(field) + 1:]
    return "text", value


def _arm_spec(arm_id: str, block: dict, conjunct: Optional[dict]) -> FilterSpec:
    match = {"all_of": [block, conjunct]} if conjunct else block
    return FilterSpec(id=arm_id, description="arm", match=MatchBlock.from_dict(match),
                      pile="screen_expensive", precedence=0)


def _clause_label(clause: dict) -> str:
    """A one-line name for *clause* — its regex when it is only a regex."""
    keys = [k for k in clause if clause[k] not in (None, [], {}, ())]
    if len(keys) == 1 and keys[0].endswith("_regex"):
        field = keys[0][: -len("_regex")]
        return clause[keys[0]] if field == "text" else f"{field}:{clause[keys[0]]}"
    return json.dumps(clause, ensure_ascii=False, separators=(",", ":"))


def spec_arms(path: Path, conjunct: Optional[dict]) -> list[Arm]:
    """Each clause of *path*'s `match.any_of` as its own arm.

    A match that is not an `any_of` is one arm — a whole-rule score is the same
    question asked of a rule with a single clause.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    match = raw.get("match") or {}
    clauses = match.get("any_of") or [match]
    return [Arm(id=f"arm{i + 1:02d}", label=_clause_label(clause),
                spec=_arm_spec(f"arm{i + 1:02d}", clause, conjunct))
            for i, clause in enumerate(clauses)]


def pattern_arms(patterns: list[str], conjunct: Optional[dict]) -> list[Arm]:
    arms = []
    for i, value in enumerate(patterns):
        field, regex = parse_pattern(value)
        arm_id = f"arm{i + 1:02d}"
        arms.append(Arm(id=arm_id, label=f"{field}:{regex}",
                        spec=_arm_spec(arm_id, {f"{field}_regex": regex}, conjunct)))
    return arms


# ---------------------------------------------------------------------------
# The fused prefilter
# ---------------------------------------------------------------------------


def loosen(pattern: str) -> str:
    """*pattern* widened so that matching RAW text is a superset of matching
    normalised text.

    Two edits. `\\s` gains every Unicode space `_normalize()` would have folded
    into a plain space. `\\b` is dropped, because NFC composition absorbs a
    combining mark into the ASCII letter before it and so can move a word
    boundary — dropping the anchor can only add rows, and the prefilter's job is
    to lose none. NFC cannot bring two ASCII characters together that were apart,
    so no other atom needs widening.
    """
    out: list[str] = []
    i, n, in_class = 0, len(pattern), False
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            nxt = pattern[i + 1]
            if nxt == "b" and not in_class:
                i += 2
                continue
            if nxt == "s":
                out.append(_FOLD_BODY if in_class else f"[{_FOLD_BODY}]")
                i += 2
                continue
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if ch == "[" and not in_class:
            in_class = True
        elif ch == "]" and in_class:
            in_class = False
        out.append(ch)
        i += 1
    return "".join(out)


def _positive_regexes(block: MatchBlock) -> Optional[list[str]]:
    """Text patterns whose union is a superset of *block*, or None if there are
    none — a block reached only through DOIs or metadata fields cannot be
    prefiltered on text, and then no prefilter runs at all."""
    direct = [p for p in (block.title_regex, block.abstract_regex, block.text_regex) if p]
    if direct:
        return direct[:1]           # ANDed conditions: any one of them is a superset
    for child in block.all_of:
        found = _positive_regexes(child)
        if found:
            return found
    if block.any_of:
        union: list[str] = []
        for child in block.any_of:
            found = _positive_regexes(child)
            if found is None:
                return None
            union.extend(found)
        return union
    return None


def fused_prefilter(arms: list[Arm]) -> Optional[str]:
    """One alternation matching every row any arm could match, or None."""
    union: list[str] = []
    for arm in arms:
        found = _positive_regexes(arm.spec.match)
        if found is None:
            return None
        union.extend(found)
    return "|".join(f"(?:{loosen(p)})" for p in dict.fromkeys(union))


# ---------------------------------------------------------------------------
# Pool scan
# ---------------------------------------------------------------------------


def _patterns(arms: list[Arm], batch: pa.RecordBatch) -> np.ndarray:
    """Per-row bitmask of which arms match, evaluated by the routing backend."""
    ctx = BatchContext(batch)
    pattern = np.zeros(batch.num_rows, dtype=np.uint64)
    for i, arm in enumerate(arms):
        matched = np.asarray(
            eval_spec_batch(arm.spec, batch, ctx).to_numpy(zero_copy_only=False),
            dtype=bool)
        pattern |= matched.astype(np.uint64) << np.uint64(i)
    return pattern


def _scan_file(path: Path, arms: list[Arm], prefilter: Optional[str],
               flora: Optional[pa.Array], aliases: dict[int, int],
               ) -> tuple[int, list[tuple[int, int]], list[tuple[str, int, int]]]:
    """(rows read, [(work_id, pattern)] for matches, [(doi, work_id, pattern)] for FLoRA)."""
    columns = ["id", *_MATCH_COLS] + (["doi"] if flora is not None else [])
    table = pq.read_table(path, columns=columns)
    rows = table.num_rows
    if not rows:
        return 0, [], []

    keep: Optional[pa.Array] = None
    if prefilter is not None:
        # Raw, unnormalised text: `loosen()` is what makes this a safe superset,
        # and normalising 5M rows to prefilter them would be the whole cost.
        title = pc.fill_null(pc.coalesce(table.column("display_name"),
                                         table.column("title")), "")
        text = pc.binary_join_element_wise(
            title.combine_chunks(),
            pc.fill_null(table.column("abstract_text"), "").combine_chunks(), "\n")
        keep = pc.fill_null(pc.match_substring_regex(text, _ci(prefilter)), False)

    doi_column = None
    if flora is not None:
        doi_column = _clean_doi_array(table.column("doi").combine_chunks())
        in_flora = pc.fill_null(pc.is_in(doi_column, value_set=flora), False)
        keep = in_flora if keep is None else pc.or_(keep, in_flora)

    if keep is not None:
        if not pc.any(keep).as_py():
            return rows, [], []
        table = table.filter(keep)
        doi_column = None                          # recomputed on the small table

    batch = table.combine_chunks().to_batches()[0]
    pattern = _patterns(arms, batch)
    ids = batch.column("id").to_pylist()

    matches = [(resolve(work_id(ids[j]), aliases), int(pattern[j]))
               for j in np.nonzero(pattern)[0]]
    flora_rows: list[tuple[str, int, int]] = []
    if flora is not None:
        dois = _clean_doi_array(batch.column("doi")).to_pylist()
        wanted = set(flora.to_pylist())
        flora_rows = [(doi, resolve(work_id(ids[j]), aliases), int(pattern[j]))
                      for j, doi in enumerate(dois) if doi in wanted]
    return rows, matches, flora_rows


def scan_pool(pool_dir: Path, arms: list[Arm], flora_dois: set[str],
              aliases: dict[int, int], workers: int,
              ) -> tuple[int, int, dict[int, int], dict[str, tuple[int, int]]]:
    """Scan every pool file: (rows, files, {work_id: pattern}, {flora doi: (wid, pattern)})."""
    files = sorted(Path(pool_dir).glob("*.parquet"))
    prefilter = fused_prefilter(arms)
    flora = pa.array(sorted(flora_dois), type=pa.string()) if flora_dois else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda path: _scan_file(path, arms, prefilter, flora, aliases), files))

    rows = 0
    matched: dict[int, int] = {}
    flora_hits: dict[str, tuple[int, int]] = {}
    for count, matches, flora_rows in results:      # sorted-file order, as routing reads it
        rows += count
        for wid, pattern in matches:
            matched.setdefault(wid, pattern)         # routing keeps the first writer
        for doi, wid, pattern in flora_rows:
            previous = flora_hits.get(doi)
            if previous is None or (pattern and not previous[1]):
                flora_hits[doi] = (wid, pattern)
    return rows, len(files), matched, flora_hits


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def flora_replication_dois(path: Path) -> set[str]:
    """Every replication DOI FLoRA knows, primary and alternate."""
    dois: set[str] = set()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            for column in ("doi_r", "doi_r_alt"):
                doi = clean_doi(row.get(column) or "")
                if doi and doi.upper() != "NA":
                    dois.add(doi)
    return dois


def negative_rows(path: Path) -> list[dict]:
    """Screen-confirmed `not_a_replication` rows, as backend row dicts.

    Read from *path* AND its twin under data/legacy_pre_engine/. Those are the
    discards made on the pre-engine corpus: the papers left the input when the #146
    handoff replaced Stage 3's input, but a screen verdict is a fact about the paper,
    not about which pile it was in, and it is the only negative evidence there is at
    that volume. Scoring rules against only the negatives the current rule book still
    admits measures the book against its own selection.
    """
    paths = [Path(path), Path(path).parent / "legacy_pre_engine" / Path(path).name]
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        with open(p, encoding="utf-8-sig", newline="") as handle:
            rows += [{"title": row.get("title_r") or "",
                      "abstract_text": row.get("abstract_r") or ""}
                     for row in csv.DictReader(handle)]
    return rows


def _template(prompt: str) -> Optional[str]:
    """*prompt* with the per-paper Title/Abstract block cut out — the instrument."""
    start = prompt.rfind(_MARK_TITLE)
    end = prompt.rfind(_MARK_END)
    if start < 0 or end < start:
        return None
    return prompt[:start] + "|||" + prompt[end:]


def cached_verdicts(cache_dir: Path) -> tuple[list[dict], int]:
    """Every usable cached screen verdict, tagged with its cohort.

    A verdict is only comparable to another verdict from the same prompt and the
    same voters, so the cohort — (template hash, model string) — travels with it.
    """
    files = sorted(glob.glob(str(cache_dir / "classify_*.json")))
    records: list[dict] = []
    for path in files:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        prompt = payload.get("llm_prompt") or ""
        template = _template(prompt)
        verdict = payload.get("screen_verdict") or ""
        title_at = prompt.rfind(_MARK_TITLE)
        abstract_at = prompt.rfind(_MARK_ABSTRACT)
        end_at = prompt.rfind(_MARK_END)
        if template is None or not verdict or abstract_at < title_at or end_at < abstract_at:
            continue
        title = prompt[title_at + len(_MARK_TITLE):abstract_at].strip()
        abstract = prompt[abstract_at + len(_MARK_ABSTRACT):end_at].strip()
        title = "" if title == "(not available)" else title
        abstract = "" if abstract == "(not available)" else abstract
        if not title and not abstract:
            continue
        records.append({
            "title": title,
            "abstract_text": abstract,
            "verdict": verdict,
            "cohort": (hashlib.sha256(template.encode()).hexdigest()[:12],
                       payload.get("llm_model") or "(none)"),
        })
    return records, len(files)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def newest_release(con: duckdb.DuckDBPyConnection) -> Optional[str]:
    """The release most recently written. `routing` carries no timestamp, so
    insertion order (`rowid`) is the only ordering the store offers."""
    row = con.execute("SELECT release_id FROM routing GROUP BY 1 "
                      "ORDER BY max(rowid) DESC LIMIT 1").fetchone()
    return row[0] if row else None


def admission(store: Path, release: Optional[str], work_ids: list[int],
              ) -> tuple[Optional[str], dict[int, str]]:
    """(release scored, {work_id: pile}). An absent store is a missing column,
    not a failure — the tool has to work before anything has been routed."""
    if not Path(store).exists():
        return None, {}
    con = duckdb.connect(str(store), read_only=True)
    try:
        release = release or newest_release(con)
        if release is None:
            return None, {}
        con.execute("CREATE TEMP TABLE armhits (work_id BIGINT)")
        con.executemany("INSERT INTO armhits VALUES (?)", [(w,) for w in work_ids])
        rows = con.execute(
            "SELECT a.work_id, r.pile FROM armhits a LEFT JOIN routing r "
            "ON r.work_id = a.work_id AND r.release_id = ?", [release]).fetchall()
    finally:
        con.close()
    return release, {wid: pile for wid, pile in rows if pile}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _label_patterns(arms: list[Arm], rows: list[dict]) -> np.ndarray:
    """Bitmask per labelled row, evaluated with the backend's `re` path."""
    pattern = np.zeros(len(rows), dtype=np.uint64)
    for i, arm in enumerate(arms):
        matched = np.array(eval_spec_rows(arm.spec, rows), dtype=bool)
        pattern |= matched.astype(np.uint64) << np.uint64(i)
    return pattern


def score(arms: list[Arm], pool_patterns: np.ndarray, admitted: np.ndarray,
          flora_patterns: np.ndarray, negative_patterns: np.ndarray,
          screen_patterns: np.ndarray, screen_proceed: np.ndarray) -> list[dict]:
    """One row of measures per arm."""
    out: list[dict] = []
    for i, arm in enumerate(arms):
        bit = np.uint64(1) << np.uint64(i)
        hit = (pool_patterns & bit) != 0
        exclusive = pool_patterns == bit
        flora_hit = (flora_patterns & bit) != 0
        flora_exclusive = flora_patterns == bit
        negative_hit = (negative_patterns & bit) != 0
        screen_hit = (screen_patterns & bit) != 0
        screen_exclusive = screen_patterns == bit
        exclusive_pool = int(exclusive.sum())
        flora_exclusive_n = int(flora_exclusive.sum())
        out.append({
            "arm": arm.id,
            "pattern": arm.label,
            "pool": int(hit.sum()),
            "pool_exclusive": exclusive_pool,
            "admitted": int((hit & admitted).sum()) if admitted.size else None,
            "admitted_exclusive": (int((exclusive & admitted).sum())
                                   if admitted.size else None),
            "flora": int(flora_hit.sum()),
            "flora_exclusive": flora_exclusive_n,
            "yield_per_1k": (round(1000 * flora_exclusive_n / exclusive_pool, 2)
                             if exclusive_pool else None),
            "negatives": int(negative_hit.sum()),
            "negatives_exclusive": int((negative_patterns == bit).sum()),
            "screen_n": int(screen_hit.sum()),
            "screen_proceed": int((screen_hit & screen_proceed).sum()),
            "screen_exclusive_n": int(screen_exclusive.sum()),
            "screen_exclusive_proceed": int((screen_exclusive & screen_proceed).sum()),
        })
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "—"
    mark = DECORATION if denominator < MIN_LABELLED else ""
    return f"{100 * numerator / denominator:.0f}% (n={denominator}){mark}"


def _count(value: int) -> str:
    return f"{value:,}{DECORATION if 0 < value < MIN_LABELLED else ''}"


def render(report: dict) -> str:
    """The stdout report: denominators, baselines, the per-arm table, the caveat."""
    lines: list[str] = []
    scan = report["scan"]
    lines.append(f"arm evidence — {report['source']} · {len(report['arms'])} arms")
    if report["conjunct"]:
        lines.append(f"  AND conjunct on every arm: {report['conjunct']}")
    lines.append(
        f"pool: {scan['rows']:,} rows in {scan['files']:,} files "
        f"({scan['workers']} workers) · matched by any arm: {scan['matched']:,}")
    if report["release"]:
        piles = " · ".join(f"{k} {v:,}" for k, v in report["pile_breakdown"].items())
        lines.append(f"routing release {report['release'][:12]} · "
                     f"admitted to a screening pile: {scan['admitted']:,} [{piles}]")
    else:
        lines.append("routing release: unavailable (no store / no release) — "
                     "`admitted` columns blank")
    flora = report["flora"]
    lines.append(f"FLoRA: {flora['dois']:,} replication DOIs · {flora['in_pool']:,} in "
                 f"the pool · {flora['matched']:,} matched by the union of the arms")
    lines.append(f"known negatives: {report['negatives']['rows']:,} rows of "
                 f"{report['negatives']['path']}")
    screen = report["screen"]
    if screen["n"]:
        lines.append(
            f"cached screen: {screen['files']:,} files · {screen['cohorts']} cohorts · "
            f"scoring {screen['scope']} (n={screen['n']:,}, template "
            f"{screen['template']}, voters {screen['models']}) · "
            f"baseline proceed {100 * screen['proceed'] / screen['n']:.1f}%")
    else:
        lines.append("cached screen: no usable verdicts")
    lines.append("")

    header = ("arm", "pool", "excl", "admitted", "adm.excl", "flora", "fl.excl",
              "yield/1k", "neg", "neg.excl", "screen all", "screen excl")
    table = [header]
    for row in report["arms"]:
        table.append((
            row["arm"],
            f"{row['pool']:,}",
            f"{row['pool_exclusive']:,}",
            f"{row['admitted']:,}" if row["admitted"] is not None else "—",
            f"{row['admitted_exclusive']:,}" if row["admitted"] is not None else "—",
            _count(row["flora"]),
            _count(row["flora_exclusive"]),
            (f"{row['yield_per_1k']}{DECORATION}"
             if row["yield_per_1k"] is not None and row["flora_exclusive"] < MIN_LABELLED
             else ("—" if row["yield_per_1k"] is None else f"{row['yield_per_1k']}")),
            _count(row["negatives"]),
            _count(row["negatives_exclusive"]),
            _rate(row["screen_proceed"], row["screen_n"]),
            _rate(row["screen_exclusive_proceed"], row["screen_exclusive_n"]),
        ))
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    for i, row in enumerate(table):
        lines.append("  ".join(cell.ljust(widths[j]) if j == 0 else cell.rjust(widths[j])
                               for j, cell in enumerate(row)))
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    lines.append("")
    for row in report["arms"]:
        lines.append(f"  {row['arm']}  {row['pattern']}")
    lines.append("")
    lines.append(f"{DECORATION} fewer than {MIN_LABELLED} labelled rows behind the "
                 "cell — decoration, not evidence.")
    lines.append(
        "BIAS: the cached verdicts and both CSVs are drawn from what the OLD filter "
        "admitted, so every precision here is optimistic; FLoRA is partly circular, "
        "because much of it was found with these very phrases. Ranks candidates and "
        "gates cheap decisions — it does not replace a human-labelled precision "
        "estimate for anything that discards.")
    lines.append(f"scan: {scan['seconds']:.1f}s over {scan['files']:,} pool files.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(arms: list[Arm], *, pool_dir: Path, store: Path, release: Optional[str],
        flora_path: Path, negatives_path: Path, cache_dir: Path,
        aliases_path: Path, workers: int, all_cohorts: bool,
        conjunct: Optional[str], source: str) -> dict:
    """Every measure for *arms*, as the report dict `render()` prints."""
    if len(arms) > 64:
        raise ValueError(f"at most 64 arms per run, got {len(arms)}")

    flora_dois = flora_replication_dois(flora_path) if flora_path.exists() else set()
    aliases = load_aliases(aliases_path)

    started = time.time()
    rows, files, matched, flora_hits = scan_pool(pool_dir, arms, flora_dois,
                                                 aliases, workers)
    elapsed = time.time() - started

    work_ids = list(matched)
    release, piles = admission(store, release, work_ids)
    pool_patterns = np.array([matched[w] for w in work_ids], dtype=np.uint64)
    admitted_mask = (np.array([piles.get(w, "") in SCREENING_PILES for w in work_ids],
                              dtype=bool) if release else np.array([], dtype=bool))

    flora_patterns = np.array([p for _wid, p in flora_hits.values()], dtype=np.uint64)

    negatives = negative_rows(negatives_path) if negatives_path.exists() else []
    negative_patterns = _label_patterns(arms, negatives)

    verdicts, verdict_files = cached_verdicts(cache_dir)
    cohorts = Counter(record["cohort"] for record in verdicts)
    if all_cohorts or not cohorts:
        scope, chosen = "all cohorts", verdicts
    else:
        cohort = cohorts.most_common(1)[0][0]
        scope = "the largest cohort"
        chosen = [record for record in verdicts if record["cohort"] == cohort]
    screen_patterns = _label_patterns(arms, chosen)
    screen_proceed = np.array([record["verdict"] == "proceed" for record in chosen],
                              dtype=bool)

    arm_rows = score(arms, pool_patterns, admitted_mask, flora_patterns,
                     negative_patterns, screen_patterns, screen_proceed)
    top = cohorts.most_common(1)[0][0] if cohorts else ("", "")
    return {
        "source": source,
        "conjunct": conjunct,
        "release": release,
        "pile_breakdown": dict(Counter(
            piles[w] for w in work_ids if piles.get(w) in SCREENING_PILES).most_common()),
        "scan": {
            "rows": rows, "files": files, "workers": workers,
            "matched": len(matched), "seconds": elapsed,
            "admitted": int(admitted_mask.sum()) if admitted_mask.size else 0,
        },
        "flora": {"dois": len(flora_dois), "in_pool": len(flora_hits),
                  "matched": int((flora_patterns != 0).sum()) if flora_patterns.size else 0},
        "negatives": {"rows": len(negatives), "path": str(negatives_path)},
        "screen": {
            "files": verdict_files, "usable": len(verdicts), "cohorts": len(cohorts),
            "scope": scope, "n": len(chosen),
            "proceed": int(screen_proceed.sum()) if screen_proceed.size else 0,
            "template": top[0] if not all_cohorts else "(mixed)",
            "models": top[1] if not all_cohorts else "(mixed)",
            "cohort_sizes": {f"{h}|{m}": n for (h, m), n in cohorts.most_common()},
        },
        "arms": arm_rows,
    }


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m analysis.arm_evidence",
        description="Score candidate filter patterns against the pool, the routing "
                    "release, FLoRA, the known negatives and the cached screen verdicts.")
    parser.add_argument("--spec", type=Path,
                        help="score each clause of this spec's match.any_of as an arm")
    parser.add_argument("--pattern", action="append", default=[],
                        help="ad-hoc regex, optionally prefixed title:/abstract:/text:")
    parser.add_argument("--and", dest="conjunct",
                        help="regex ANDed onto every arm (same field prefixes)")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="also write the full table here")
    parser.add_argument("--release", help="routing release to score (default: newest)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--all-cohorts", action="store_true",
                        help="score every cached-verdict cohort, not just the largest")
    parser.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--flora", type=Path, default=DATA_DIR / "flora.csv")
    parser.add_argument("--negatives", type=Path,
                        default=DATA_DIR / "not_a_replication.csv")
    parser.add_argument("--llm-cache", type=Path, default=LLM_CACHE_DIR)
    parser.add_argument("--aliases", type=Path, default=SPEC_DIR / ALIASES_FILENAME)
    args = parser.parse_args(argv)

    if not args.spec and not args.pattern:
        parser.error("give --spec or at least one --pattern")

    conjunct_block: Optional[dict] = None
    if args.conjunct:
        field, regex = parse_pattern(args.conjunct)
        conjunct_block = {f"{field}_regex": regex}

    arms = (spec_arms(args.spec, conjunct_block) if args.spec
            else pattern_arms(args.pattern, conjunct_block))
    report = run(arms, pool_dir=args.pool, store=args.store, release=args.release,
                 flora_path=args.flora, negatives_path=args.negatives,
                 cache_dir=args.llm_cache, aliases_path=args.aliases,
                 workers=args.workers, all_cohorts=args.all_cohorts,
                 conjunct=args.conjunct,
                 source=str(args.spec) if args.spec else f"{len(arms)} ad-hoc patterns")
    print(render(report))
    if args.json_path:
        args.json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"wrote {args.json_path}")


if __name__ == "__main__":
    main()
