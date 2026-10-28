# FLoRA Extractor — Documentation

Start with the question you need answered. The guides describe current behaviour;
dated campaign notes and evaluation reports record the configuration used at the time.

## Start here

- **Which models do we use, and how do they classify a paper?**
  [Models and screening](models-and-screening.md).
- **How do I install and run the pipeline?** [Setup](setup.md), then
  [CLI reference](cli-reference.md) for flags and dry runs.
- **What do the results mean?** [CSV columns](csv-schema.md),
  [dashboard guide](dashboard-guide.md) and [known limitations](limitations.md).
- **How does the code work?** Use the stage walkthroughs below.
  [CLAUDE.md](../CLAUDE.md) is the detailed maintainer and AI agent guide.

## Reference and operations

| Document | Use it for |
|----------|------------|
| [filter-engine.md](filter-engine.md) | Routing, claims, verdict storage and engine contracts |
| [../filter/spec/CONVENTIONS.md](../filter/spec/CONVENTIONS.md) | Rule precedence, pile meanings and evidence requirements |
| [aws-snapshot-scan.md](aws-snapshot-scan.md) | Full OpenAlex snapshot scan on EC2 and pool publication |
| [parquet-cache.md](parquet-cache.md) | Dashboard cache |
| [retiring-superseded-records.md](retiring-superseded-records.md) | Retire rows from the separate validation system |
| [check-page.md](check-page.md) | Inspect a record in the dashboard |
| [supabase-schema.md](supabase-schema.md) | Validation monitoring tables |
| [testing.md](testing.md) | Tests and live API guards |

## Code-flow walkthroughs

Detailed code flows for each pipeline stage:

| Document | Description |
|----------|-------------|
| [code-flow/stage1-search.md](code-flow/stage1-search.md) | Stage 1: How papers are discovered and deduplicated |
| [code-flow/stage2-filter.md](code-flow/stage2-filter.md) | Stage 2: Rule + LLM classification |
| [code-flow/stage3-extract.md](code-flow/stage3-extract.md) | Stage 3: Original study linking + outcome extraction |
| [code-flow/stage4-validate.md](code-flow/stage4-validate.md) | Stage 4: Monitoring web app + Supabase integration |
| [code-flow/pdf-pipeline.md](code-flow/pdf-pipeline.md) | PDF acquisition waterfall + parse scoring |
| [code-flow/analysis.md](code-flow/analysis.md) | Analysis scripts: the APA resolver's flow, plus a pointer to `analysis/README.md` (the gap/overlap analysis is retired) |

## Historical records and evaluations

- [Screening evaluations](../analysis/screening_eval/README.md) and
  [pre-screen evaluations](../analysis/prescreen_eval/README.md): check the model,
  prompt and gate used before applying a reported score to current settings.
- [Analysis index](../analysis/README.md): experiments and dated reports.
- [August cleanup record](cleanup-worklist.md), `handover-*.md`, `tasks/` and
  `superpowers/specs/`: dated work records and plans, not current setup instructions.
- `archive/`: superseded documentation.
