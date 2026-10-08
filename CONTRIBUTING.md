# Contributing to FLoRA Extractor

**Issues with evidence and suggestions are the best way to contribute.** FLoRA Extractor is a research pipeline: changes to discovery, screening, linking, prompts, and outcome coding affect the scientific dataset and ongoing evaluations. Unsolicited pull requests are unlikely to be merged. Please discuss a proposal with maintainers before implementing it, and follow the [FORRT Code of Conduct](https://forrt.org/coc/).

## Open a useful issue

Check [existing issues](https://github.com/forrtproject/flora-extractor/issues) and pull requests first. We welcome:

- Examples of missed replication/reproduction studies or false positives, with public DOIs, URLs, and the evidence supporting your interpretation.
- Incorrect links to original studies or outcome labels, with the relevant quotation, page/section, and source.
- Reproducible software problems, documentation corrections, and suggestions for evaluating or improving the research pipeline.

State the pipeline stage, expected and actual result, and why the example matters. For a software bug, include the revision and relevant command or a small reproducible example. Remove API keys, database URLs, private records, and restricted full-text content from reports. A reported problem does not require you to rerun the pipeline or buy model calls.

Discovery, screening, and extraction belong here. Human validation, entry-sheet ingestion, and prepared-data publication belong in [flora-validation](https://github.com/forrtproject/flora-validation). See the [README](README.md) and [documentation index](docs/README.md) for the current boundaries.

## Work on an agreed change

Once maintainers have agreed the scope and evaluation, fork the repository, create a focused branch from the current `main`, and open the PR against `main`. The historical `dev` branch is stale. Follow [docs/setup.md](docs/setup.md) for environment setup, and read [CLAUDE.md](CLAUDE.md) for architecture, scientific contracts, and development instructions. [AGENTS.md](AGENTS.md) points all coding agents to the same instructions.

Use your project Python environment. Ordinary tests mock external APIs; live tests belong in `tests/live/`. The current guards enable live requests for any non-empty `TEST_LIVE_API` value, including `0`; leave it unset for ordinary tests. Use `TEST_LIVE_API=1` only for an explicitly agreed live evaluation. Run affected tests while developing, then the ordinary full suite before pushing code:

```bash
env -u TEST_LIVE_API python -m pytest -q
```

Documentation-only changes can be checked for accurate commands, local links, and consistency with the implementation without running a research campaign. Do not run screening/extraction tiers, change stored verdicts or shared datasets, or make paid/live API calls as a routine PR check. Agree any research evaluation, data access, and spending with maintainers first.

Keep evidence and provenance together, preserve stage boundaries and the single extraction-export writer, and explain any effect on dataset membership, identity, interpretation, or repeatability. Do not edit generated research outputs to make a test or example pass.

## Submit for review

Link the agreed issue, describe the resulting behaviour, and provide the tests or evaluation supporting it. State what was not tested, including any live-service or research-data limitations. Separate code changes from proposed scientific-method changes so reviewers can assess both. Maintainers decide whether the change fits the research design before merging; an agreed issue is an invitation to work on a proposal, not a guarantee of acceptance.
