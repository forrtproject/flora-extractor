# FLoRA Extractor — Documentation

## Quick links

| Document | Description |
|----------|-------------|
| [setup.md](setup.md) | Installation, environment variables, running the pipeline |
| [architecture.md](architecture.md) | Module map, key design decisions, caching, error handling |
| [cli-reference.md](cli-reference.md) | All CLI commands with flags for every stage |
| [csv-schema.md](csv-schema.md) | Column definitions for all four pipeline CSVs |
| [dashboard-guide.md](dashboard-guide.md) | How to use the Pipeline + Validation dashboard tabs |
| [supabase-schema.md](supabase-schema.md) | Supabase table schemas used by the validation monitoring tab |
| [testing.md](testing.md) | How to run tests, write new tests, live API test guard |

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
