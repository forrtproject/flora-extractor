"""
Standalone CLI for the discovery engine.

Usage:
    python -m search.engine.cli \\
        --sources openalex,crossref,semantic_scholar \\
        --max-per-source 200 \\
        --year-from 2010 --year-to 2025 \\
        --out data/candidates_engine.csv

Optional ``--keywords`` lets you add ad-hoc wildcards on top of the YAML spec
(e.g. ``--keywords "registered replicat*,many labs"``).

Output goes directly into a candidates.csv-shaped file (CANDIDATES_COLS).
The file is overwritten if it exists.
"""

import argparse
import csv
import logging
import sys
from pathlib import Path

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS

from ..engine_source import _flatten as _normalized_to_row
from .runner import RunConfig, build_default_adapters, run_discovery
from .types import NormalizedCandidate, RunFilters, SourceId

DEFAULT_SOURCES: list[SourceId] = ["openalex", "crossref", "semantic_scholar"]


def _csv_writer(out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(out_path, "w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(f, fieldnames=CANDIDATES_COLS, extrasaction="ignore")
    writer.writeheader()
    return f, writer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replication-discovery engine CLI")
    parser.add_argument(
        "--spec-dir",
        type=Path,
        default=Path(__file__).parent.parent / "spec",
        help="Directory containing search-keywords.yaml etc. (default: search/spec/)",
    )
    parser.add_argument(
        "--sources",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=DEFAULT_SOURCES,
        help="Comma-separated sources (default: openalex,crossref,semantic_scholar)",
    )
    parser.add_argument(
        "--keywords",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=[],
        help="Optional comma-separated user wildcards in addition to the spec",
    )
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to", type=int, default=None)
    parser.add_argument(
        "--max-per-source",
        type=int,
        default=0,
        help="Stop a source after N kept candidates (0 = no cap)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR / "candidates_engine.csv",
        help="CSV output path (default: data/candidates_engine.csv)",
    )
    parser.add_argument(
        "--languages",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=["en"],
        help="Comma-separated ISO 639-1 codes; empty = no filter",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)

    adapters = build_default_adapters(args.spec_dir, sources=args.sources)
    if not adapters:
        log.error("No adapters could be built (check API keys / source list)")
        return 2

    log.info("Engine sources: %s", ", ".join(sorted(adapters.keys())))

    f, writer = _csv_writer(args.out)
    seen_dois: set[str] = set()
    written = 0

    def on_candidate(c: NormalizedCandidate) -> None:
        nonlocal written
        if not c.doi:
            return
        if c.doi in seen_dois:
            return
        seen_dois.add(c.doi)
        writer.writerow(_normalized_to_row(c))
        written += 1
        if written % 100 == 0:
            log.info("  written %d candidates so far", written)

    config = RunConfig(
        keywords=args.keywords,
        filters=RunFilters(
            languages=args.languages,
            sources=list(adapters.keys()),
            max_candidates_per_source=args.max_per_source,
            skip_dois_in_flora=False,    # CLI is wide; flora cross-check happens in run_search
            year_from=args.year_from,
            year_to=args.year_to,
        ),
        spec_dir=args.spec_dir,
    )

    try:
        result = run_discovery(config, adapters, on_candidate, log=log)
    finally:
        f.close()

    log.info(
        "Engine run %s: seen=%d kept=%d excluded=%d written=%d → %s",
        result.status,
        result.stats.candidates_seen,
        result.stats.candidates_kept,
        result.stats.candidates_excluded,
        written,
        args.out,
    )
    if result.error:
        log.warning("error detail: %s", result.error)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
