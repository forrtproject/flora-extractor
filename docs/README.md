# FLoRA Extractor — Documentation

**This page is the documentation index.** It is the only one — `README.md` and
`CLAUDE.md` each carry a single pointer here rather than a list of their own, so a
new document is added in one place and cannot go missing from a second.

## Quick links

| Document | Description |
|----------|-------------|
| [../CLAUDE.md](../CLAUDE.md) | **Start here.** Module map, key design decisions, caching, error handling |
| [setup.md](setup.md) | Installation, environment variables, running the pipeline |
| [cli-reference.md](cli-reference.md) | All CLI commands with flags for every stage |
| [filter-engine.md](filter-engine.md) | Stage 2's declarative routing engine (issue #146) |
| [cleanup-worklist.md](cleanup-worklist.md) | Dead code, docs drift and refactors noticed 2026-08-06, for a dedicated cleanup pass |
| [aws-snapshot-scan.md](aws-snapshot-scan.md) | Runbook: the full OpenAlex snapshot scan on EC2, published to Hugging Face |
| [parquet-cache.md](parquet-cache.md) | The dashboard's parquet cache |
| [csv-schema.md](csv-schema.md) | Column definitions for the pipeline CSVs |
| [dashboard-guide.md](dashboard-guide.md) | How to use the Pipeline + Validation dashboard tabs |
| [check-page.md](check-page.md) | The dashboard's record-check page |
| [supabase-schema.md](supabase-schema.md) | Supabase table schemas used by the validation monitoring tab |
| [testing.md](testing.md) | How to run tests, write new tests, live API test guard |
| [limitations.md](limitations.md) | Known limitations and revisit obligations |

## Code-flow walkthroughs

Detailed code flows for each pipeline stage:

| Document | Description |
|----------|-------------|
| [code-flow/stage1-search.md](code-flow/stage1-search.md) | Stage 1: How papers are discovered and deduplicated |
| [code-flow/stage2-filter.md](code-flow/stage2-filter.md) | Stage 2: Rule + LLM classification |
| [code-flow/stage3-extract.md](code-flow/stage3-extract.md) | Stage 3: Original study linking + outcome extraction |
| [code-flow/stage4-validate.md](code-flow/stage4-validate.md) | Stage 4: Monitoring web app + Supabase integration |
| [code-flow/pdf-pipeline.md](code-flow/pdf-pipeline.md) | PDF acquisition waterfall + parse scoring |
| [code-flow/analysis.md](code-flow/analysis.md) | Analysis scripts: gap analysis, rule analysis, APA resolver |
