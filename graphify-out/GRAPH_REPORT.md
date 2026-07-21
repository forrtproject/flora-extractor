# Graph Report - .  (2026-07-13)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1671 nodes · 3393 edges · 88 communities (78 shown, 10 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 47 edges (avg confidence: 0.58)
- Token cost: 7,116 input · 984 output

## Graph Freshness
- Built from commit: `1f21b557`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- DOI Metadata Verification
- PDF Section Extraction
- Study Disambiguation Logic
- Outcome Extraction Tests
- Replication Phrase Detection
- Flask Web Application
- Sensitivity Analysis
- Crossref API Adapter
- Candidate Ranking Engine
- Abstract Enrichment Pipeline
- Extraction Orchestration
- Multi-Original Study Pipeline
- Candidate Normalization
- Extraction Audit Tools
- APA Reference Resolver
- OpenAlex Metadata Client
- LLM Filtering and Cache
- Search Orchestrator
- PDF Parsing and Scoring
- Metadata Backfill Utilities
- Multi-Original Pipeline Tests
- Impact Reporting
- OpenAlex Search Client
- Single-DOI Orchestration
- Semantic Scholar Client
- Match Type Classification
- Candidate Deduplication
- Monitoring App Tests
- Data Loading Utilities
- LLM Outcome Extraction
- Monitoring Dashboard Routes
- Filter Stage Orchestrator
- Keyword Context Extraction
- Pipeline Gap Analysis
- Pipeline Comparison Tools
- Gap Row Backfilling
- Candidate Discovery Tests
- Exclusion Regex Filtering
- Discovery Source Manager
- Multi-Original Routes
- Database Import Utilities
- Supabase API Wrapper
- Extraction Pipeline Tests
- Citation Context Extraction
- Citation Pattern Parsing
- DOI Verification Tests
- Frontend Rendering Scripts
- Validation Sampling Schema
- External List Scrapers
- Author Matching Logic
- URL Matching Logic
- Search API Tests
- I4R Scraper Tests
- OpenAlex Abstract Reconstruction
- Supabase Client Tests
- Pipeline Statistics API
- Report Generation Utilities
- Phrase Coverage Analysis
- OpenAlex Date Filtering
- Data Listing Templates
- Input Generation Templates
- Semantic Scholar Search
- Disambiguation State Management
- DOI Matching Logic
- Fuzzy Title Matching
- OpenAlex API Examples
- Source Data Cleanup
- Fixture Generation
- Gemini API Example
- Filter API Templates
- Validation Templates
- Discovery Shell Script
- Analysis Module
- Pipeline API Templates
- Pipeline Shell Script
- Discovery Engine Core
- Search Adapters
- LLM Token Tracking
- User Session Templates

## God Nodes (most connected - your core abstractions)
1. `clean_doi()` - 121 edges
2. `cache_key()` - 78 edges
3. `resolve_doi_by_metadata()` - 31 edges
4. `run_for_doi()` - 28 edges
5. `run_extract()` - 28 edges
6. `run_discovery()` - 27 edges
7. `fetch_doi_metadata()` - 24 edges
8. `extract_author_year_patterns()` - 24 edges
9. `find_all_candidates()` - 24 edges
10. `extract_outcome()` - 23 edges

## Surprising Connections (you probably didn't know these)
- `load_candidates()` --indirect_call--> `clean_doi()`  [INFERRED]
  analysis/data_loader.py → shared/utils.py
- `load_filtered()` --indirect_call--> `clean_doi()`  [INFERRED]
  analysis/data_loader.py → shared/utils.py
- `load_all_replications()` --indirect_call--> `clean_doi()`  [INFERRED]
  analysis/data_loader.py → shared/utils.py
- `_dedup_by_doi()` --indirect_call--> `clean_doi()`  [INFERRED]
  search/deduplicate.py → shared/utils.py
- `_load_openalex_rows()` --indirect_call--> `clean_doi()`  [INFERRED]
  tools/add_old_openalex_candidates.py → shared/utils.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **FLoRA 4-Stage Pipeline** — search_run_search, filter_run_filter, extract_run_extract, validate_app [EXTRACTED 1.00]
- **Shared Pipeline Utilities** — shared_schema, shared_utils, shared_cache, shared_pdf_parsing, shared_doi_verify [EXTRACTED 1.00]
- **Post-Extraction Analysis Tools** — analysis_run_overlap_analysis, analysis_rule_analysis, analysis_apa_resolver [EXTRACTED 1.00]
- **FLoRA Extraction & Validation Pipeline** — validate_templates_search_html, validate_templates_filter_html, validate_templates_extract_html, validate_templates_target_pending_html, validate_templates_validate_templates_validate_templates_validate_html, validate_templates_flora_html [INFERRED 0.90]

## Communities (88 total, 10 thin omitted)

### Community 0 - "DOI Metadata Verification"
Cohesion: 0.05
Nodes (41): audit_file(), main(), Path, Audit every row of *csv_path*. Returns per-status counts., Verify/correct doi_o in a finished result row before it is written.      Keeps, _verify_row(), _crossref_year(), fetch_doi_metadata() (+33 more)

### Community 1 - "PDF Section Extraction"
Cohesion: 0.05
Nodes (57): _extract_pdf_text(), _extract_refs_via_grobid(), _extract_refs_via_pdf_direct(), _extract_refs_via_pdf_images(), parse_pdf_sections(), _parse_references_block(), parse_tei_sections(), process_pdf_with_grobid() (+49 more)

### Community 2 - "Study Disambiguation Logic"
Cohesion: 0.06
Nodes (29): _extract_title_target(), Try to resolve the original study by matching the replication paper's title, Extract the original study target from a replication paper's title.     Returns, _resolve_by_title_pattern(), jaccard_similarity(), disambiguation.py — Same-author / same-year original study disambiguation.  Pu, Match GROBID reference list against pre-identified candidate originals.      F, Lowercase word tokens of ≥ 3 characters. (+21 more)

### Community 3 - "Outcome Extraction Tests"
Cohesion: 0.05
Nodes (36): extract_outcome(), Extract replication outcome from available text.      Returns a dict with keys, best_parse_result(), Return the highest-scoring parse result from a parse_all() output dict.     Ret, _make_extracted_csv(), _make_filtered_csv(), Path, Tests for --no-llm, --match-type-only, --outcome-only CLI flags. (+28 more)

### Community 4 - "Replication Phrase Detection"
Cohesion: 0.07
Nodes (49): find_replication_phrase(), find_replication_phrase_span(), has_replication_phrase(), is_non_scholarly_context(), is_reproduction_only(), _load_exclusion_regexes(), Pattern, Replication-phrase detection — port of SciMeto's ``apps/worker/src/services/rep (+41 more)

### Community 5 - "Flask Web Application"
Cohesion: 0.05
Nodes (40): data/validated.csv, Flask, app(), create_app(), app.py — Flask entry point for the FLoRA monitoring web app.  Read-only monitori, api_check_download(), api_check_search(), _apply_filters() (+32 more)

### Community 6 - "Sensitivity Analysis"
Cohesion: 0.06
Nodes (46): _clean_doi_col(), _load(), _phrase_hits(), DataFrame, Path, Series, sensitivity_check.py — Compare all_replications.csv against candidates.csv to me, Return which SEARCH_PHRASES appear in text (case-insensitive). (+38 more)

### Community 7 - "Crossref API Adapter"
Cohesion: 0.09
Nodes (23): ABC, _clean_abstract(), CrossrefSourceAdapter, datetime, Session, CrossrefSourceAdapter — OR-bundled phrase search against Crossref /works.  Str, OpenAlexSourceAdapter — OR-bundled phrase search against /works.  Strategy (pe, datetime (+15 more)

### Community 8 - "Candidate Ranking Engine"
Cohesion: 0.10
Nodes (42): CandidateCallback, compute_search_score(), load_ranking_weights(), Path, SourceId, RankingContribution, RankingWeights, Candidate ranker — computes the deterministic search_score for a NormalizedCand (+34 more)

### Community 9 - "Abstract Enrichment Pipeline"
Cohesion: 0.09
Nodes (44): _append_change_log(), _append_checkpoint(), _load_checkpoint(), refilter_fp.py — Re-classify false_positive and needs_review rows in filtered.cs, run_refilter(), _append_checkpoint(), _cache_path(), enrich_abstracts() (+36 more)

### Community 10 - "Extraction Orchestration"
Cohesion: 0.09
Nodes (43): predict_outcome_keyword(), Fast keyword-only outcome prediction for pre-filtering before extraction., _append_row(), _best_fulltext_from_cache(), _build_cands_df(), _build_rep_df(), _empty_row(), _extract_row_key() (+35 more)

### Community 11 - "Multi-Original Study Pipeline"
Cohesion: 0.08
Nodes (35): DataFrame, multi_original.py — Pipeline for identifying multiple original studies in multi, Return all_replications.csv fields for doi_r., Run the multi-original pipeline for doi_r.      Pipeline stages:       1. Loa, _rep_row(), run_multi_original_for_doi(), acquire_pdf(), extract_html_text_as_fulltext() (+27 more)

### Community 12 - "Candidate Normalization"
Cohesion: 0.10
Nodes (34): _clean(), merge_candidates(), normalize_candidate(), normalize_doi(), Candidate normalizer — converts RawCandidate → NormalizedCandidate, normalizes, Lowercase, no leading https://doi.org/ or doi:, no trailing slash., Convert a RawCandidate; ``search_score`` stays 0 until the ranker runs., Merge two candidates with the same DOI.      Keeps the first non-null metadata (+26 more)

### Community 13 - "Extraction Audit Tools"
Cohesion: 0.10
Nodes (32): analyze_confidence_distribution(), analyze_link_method_distribution(), audit_extracted_csv(), compare_task2_gaps_with_extracted(), count_by_link_method_and_confidence(), find_missing_doi_rows(), generate_extraction_audit_report(), generate_improvement_opportunities() (+24 more)

### Community 14 - "APA Reference Resolver"
Cohesion: 0.10
Nodes (30): format_apa_reference(), fuzzy_match_csv(), load_fallback_csv(), load_missing_dois(), Any, DataFrame, Path, Series (+22 more)

### Community 15 - "OpenAlex Metadata Client"
Cohesion: 0.11
Nodes (30): _all_authors_apa(), _crossref_author_apa(), _fetch_crossref_full_meta(), _fetch_doi_org_full_meta(), fetch_openalex_by_doi(), fetch_openalex_full_metadata(), fetch_referenced_works_metadata(), _first_author_surnames() (+22 more)

### Community 16 - "LLM Filtering and Cache"
Cohesion: 0.10
Nodes (28): _build_prompt(), classify_with_llm(), llm_filter.py — Stage 2 LLM uplift for rows the rule filter couldn't decide.  On, Return a dict with filter_status, filter_confidence, filter_evidence, or None on, clear_cache(), Path, cache.py — Cache read/write/clear helpers.  All cache files live under CACHE_DIR, Return cached dict for *key*, or None if not cached. (+20 more)

### Community 17 - "Search Orchestrator"
Cohesion: 0.09
Nodes (29): Namespace, _advance_state(), build_candidates_index(), dedup_candidates_csv(), _load_candidates_index(), _load_or_build_candidates_index(), _load_search_state(), _parse_args() (+21 more)

### Community 18 - "PDF Parsing and Scoring"
Cohesion: 0.11
Nodes (28): Blueprint, promote_rows(), Merge rows from extracted-test.csv into extracted.csv.      Returns {"promoted":, Run all PDF parsers for doi_r and cache results to PARSE_CACHE_DIR., _save_parse_cache(), build_identification_prompt(), Build the LLM identification prompt.      pdf_url   — passed when PDF download, best_parse_method_name() (+20 more)

### Community 19 - "Metadata Backfill Utilities"
Cohesion: 0.11
Nodes (22): audit_dois.py — Retroactive DOI verification for extracted.csv.  Checks every ro, backfill(), backfill_authors.py — Retroactively update authors_o and ref_o in extracted.csv., promote_test.py — Merge rows from extracted-test.csv into extracted.csv.  Usage:, build_bibtex(), _build_bibtex_r(), _build_ref_o(), Build a BibTeX entry for the replication paper from its row metadata.      Use (+14 more)

### Community 20 - "Multi-Original Pipeline Tests"
Cohesion: 0.15
Nodes (13): _llm_result(), Tests for extract/multi_original.py — multi-original pipeline.  All external A, When LLM finds only 1 original (false positive), flag must be True., Even a false-positive result should have the partial originals in json, LLM returning zero originals — n_originals=0, originals_json='[]'., Prefixed DOI must be stripped in the returned dict., Helper: run multi_original_for_doi with all external calls mocked., _rep_df() (+5 more)

### Community 21 - "Impact Reporting"
Cohesion: 0.11
Nodes (21): audit_filter_gate_impact(), audit_stage3_relink_impact(), _cached_only_journal(), DataFrame, rescan_impact_report.py — Read-only impact report for the 2026-07-08 classificat, Re-run _resolve_rule_based() (narrative-citation-aware extraction already applie, Cache-only stand-in for link_original._fetch_journal_cached — never makes a, Re-run classify_row() (proximity gate + stopword filter already applied) against (+13 more)

### Community 22 - "OpenAlex Search Client"
Cohesion: 0.12
Nodes (26): _build_ref(), _cursor_path(), _extract_row(), fetch_concept(), fetch_phrase(), _get_page(), _job_key(), list_oa_concepts() (+18 more)

### Community 23 - "Single-DOI Orchestration"
Cohesion: 0.12
Nodes (24): _best_parse_result(), _build_output(), _cands_row(), clear_pipeline_caches(), _fetch_journal_cached(), _flora_row(), _journal_token_sim(), DataFrame (+16 more)

### Community 24 - "Semantic Scholar Client"
Cohesion: 0.12
Nodes (24): Exception, Response, _backoff_sleep(), fetch_phrase(), _get_page(), _job_key(), _load_offset_state(), _offset_exhausted() (+16 more)

### Community 25 - "Match Type Classification"
Cohesion: 0.12
Nodes (13): classify_match_type(), Return a classification dict if title or abstract contains unambiguous signals, Classify original_match_type for a filtered.csv row.      Steps:       0. Rul, _rule_classify_multi_original(), TestNoLlmClassifyMatchType, Issue 8 — unit tests for classify_match_type.      All external calls (OpenAle, Helper: run classify_match_type with mocked OpenAlex + LLM., OpenAlex exception should return single_original without crashing. (+5 more)

### Community 26 - "Candidate Deduplication"
Cohesion: 0.16
Nodes (21): _best_row(), _dedup_by_doi(), _dedup_by_title(), _dedup_versioned_preprints(), deduplicate_candidates(), _exclude_by_doi_pattern(), _load_flora_dois(), DataFrame (+13 more)

### Community 27 - "Monitoring App Tests"
Cohesion: 0.13
Nodes (17): _pipeline_df(), Tests for validate/ monitoring app routes.  Stale tests for the old SQLite votin, New /check page must exist., POST /set-name stores reviewer_id in session and redirects., Dashboard is reachable without setting a reviewer name., Minimal flora_all.csv-shaped DataFrame for pipeline route tests., test_check_route_accessible(), test_dashboard_accessible_without_name() (+9 more)

### Community 28 - "Data Loading Utilities"
Cohesion: 0.14
Nodes (19): load_all_replications(), load_candidates(), load_filtered(), DataFrame, data_loader.py — Load and normalize input CSVs for analysis.  Handles:   - Loadi, Load candidates.csv and normalize DOI/URL., Load filtered.csv and normalize DOI/URL., Load all_replications.csv and normalize DOI/URL.      Note: all_replications.csv (+11 more)

### Community 29 - "LLM Outcome Extraction"
Cohesion: 0.11
Nodes (17): _llm_outcome(), code_outcome.py — Keyword + LLM outcome extraction for Stage 3.  Pass 1: keywo, LLM-based outcome extraction. Result cached per doi_r., _llm_classify_match_type(), LLM call to classify original_match_type. Returns a dict with both fields., call_llm(), llm.py — LLM-based original study identification.  Primary model  : OpenRouter, Route a prompt through the configured provider chain and return the first     s (+9 more)

### Community 30 - "Monitoring Dashboard Routes"
Cohesion: 0.11
Nodes (18): load_cached(), api_analysis_gaps(), api_analysis_stats(), api_dashboard_download(), api_old_pipeline_analysis(), api_supabase_corrections(), api_supabase_drilldown(), api_supabase_outcomes() (+10 more)

### Community 31 - "Filter Stage Orchestrator"
Cohesion: 0.16
Nodes (18): data/candidates.csv, _append_key_to_filtered_index(), _append_row(), _build_filtered_index(), dedup_filtered_csv(), _load_filtered_index(), DataFrame, run_filter.py — Stage 2 orchestrator.  Reads data/candidates.csv, applies the ru (+10 more)

### Community 32 - "Keyword Context Extraction"
Cohesion: 0.17
Nodes (6): _expand_to_sentences(), _keyword_scan(), Return a result dict if a keyword pattern matches, else None.      Check order, Return the sentence containing the match plus n_context sentences on each side., TestExpandToSentences, TestKeywordScan

### Community 33 - "Pipeline Gap Analysis"
Cohesion: 0.20
Nodes (16): analyze_filter_gap(), analyze_filter_rules(), analyze_older_pipeline(), analyze_recall_gap(), analyze_source_contribution(), _build_candidate_index_sets(), DataFrame, analyses.py — Five analysis functions for overlap comparison (1a-1e).  Each anal (+8 more)

### Community 34 - "Pipeline Comparison Tools"
Cohesion: 0.18
Nodes (17): _build_candidate_index(), _check_rows_against_index(), _load_new_keywords(), _load_new_pipeline(), _load_old_pipeline(), _overlap_analysis(), Any, DataFrame (+9 more)

### Community 35 - "Gap Row Backfilling"
Cohesion: 0.14
Nodes (16): load_gap_rows(), main(), DataFrame, Series, backfill_gap_rows.py — Add old-pipeline TP gap rows directly to candidates.csv., Map an all_replications row to candidates.csv schema., Load confirmed TP rows from all_replications.csv that are NOT already in     can, _to_candidates_row() (+8 more)

### Community 36 - "Candidate Discovery Tests"
Cohesion: 0.16
Nodes (9): find_all_candidates(), Re-fetch all referenced works for *openalex_id_r* and return EVERY work     tha, Pattern citing year 2009 should match a reference with year 2010 (±1 window)., Pattern citing year 2011 should match a reference with year 2010 (±1 window)., When the abstract has no author-year patterns, no candidates are returned., The replication paper's own DOI must not appear in the candidate list., Second call with the same doi_r must return from cache without calling the API., Every candidate dict must have the required fields. (+1 more)

### Community 37 - "Exclusion Regex Filtering"
Cohesion: 0.21
Nodes (16): apply_compiled_exclusions(), apply_exclusions(), _compile(), compile_exclusions(), ExclusionResult, load_exclusion_patterns(), Path, Pattern (+8 more)

### Community 38 - "Discovery Source Manager"
Cohesion: 0.16
Nodes (16): is_engine_enabled(), Env-var gate: ``FLORA_USE_ENGINE=1`` (or true/yes/on) to opt in., fetch_openalex_candidates(), fetch_openalex_concept_candidates(), DataFrame, Fetch OpenAlex candidates across all ``SEARCH_PHRASES``.      Each phrase is a, Fetch OpenAlex candidates across all ``CONCEPT_IDS``.      Each concept is an, _harvest_oa_cache() (+8 more)

### Community 39 - "Multi-Original Routes"
Cohesion: 0.16
Nodes (12): api_candidates(), api_export(), api_result(), api_run(), _build_candidate_row(), _pdf_serve_url(), Series, routes/multi_originals.py — Multi-original study identification pipeline.  Route (+4 more)

### Community 40 - "Database Import Utilities"
Cohesion: 0.23
Nodes (14): Client, _build_metadata_row(), _build_queue_rows(), _build_unvalidated_row(), _derive_url_o(), _int_or_none(), _load_existing_pair_ids(), Path (+6 more)

### Community 41 - "Supabase API Wrapper"
Cohesion: 0.18
Nodes (14): _cached(), _get(), get_correction_frequency(), get_drilldown_page(), get_validated_outcomes(), get_validation_stats(), _headers(), Any (+6 more)

### Community 42 - "Extraction Pipeline Tests"
Cohesion: 0.18
Nodes (6): Helper: write a temp CSV, run extract with mocked APIs, return result DataFrame., False positives must appear in output without calling classify_match_type., Routing test: false_positive must bypass classify_match_type entirely., When extraction throws an exception, link_method and outcome must be         'a, _get_outcome must pass resolved_title_o/author_o/year_o to extract_outcome., TestRunExtract

### Community 43 - "Citation Context Extraction"
Cohesion: 0.22
Nodes (6): _extract_cit_contexts(), Return list of {surnames, year, journal, raw} from all author-year citations., Tests for citation-context extraction in extract/link_original.py., Reconstructs the real aepp.13320 case: the true target is cited         narrativ, The old local regex only matched fully-parenthetical citations like         '(An, TestExtractCitContexts

### Community 44 - "Citation Pattern Parsing"
Cohesion: 0.25
Nodes (3): extract_author_year_patterns(), Parse author-year citation patterns from *text*.      Returns a list of dicts:, TestExtractAuthorYearPatterns

### Community 46 - "Frontend Rendering Scripts"
Cohesion: 0.33
Nodes (11): collap(), esc(), field(), grid(), pill(), pillConf(), pillMethod(), pillOutcome() (+3 more)

### Community 47 - "Validation Sampling Schema"
Cohesion: 0.15
Nodes (7): mix_for_validation(), DataFrame, mix_for_validation.py — Sample extracted.csv into a validation-ready mix.  Build, Sample extracted.csv into a validation-ready mix.      Parameters     ----------, schema.py — CSV column definitions for all pipeline stages.  This is the contr, Check that a DataFrame has all required columns for a given stage.     Returns, validate_csv_columns()

### Community 48 - "External List Scrapers"
Cohesion: 0.17
Nodes (12): _fetch_i4r_paper_detail(), fetch_replication_network(), _lookup_i4r_openalex(), _parse_repec_page(), DataFrame, external_lists.py — Scrapers for I4R list and Replication Network.  Public API:, Extract paper rows from one RepEC listing page., Fetch abstract and EconStor PDF URL from an individual IDEAS paper page.     Cac (+4 more)

### Community 49 - "Author Matching Logic"
Cohesion: 0.26
Nodes (4): author_matches(), Return True if *cited_surname* plausibly matches any name in *ref_authors*., Tests for shared/openalex_client.py — candidate-matching logic.  All OpenAlex, TestAuthorMatches

### Community 50 - "URL Matching Logic"
Cohesion: 0.20
Nodes (11): find_best_match(), match_by_url(), normalize_url(), matching.py — Matching logic for linking candidates to all_replications.  Strate, Find best match using priority order: DOI → URL → fuzzy title.      Returns:, Normalize URL for comparison., Match by exact URL.      Args:         row_candidate: dict with 'url_r' key, URL normalization should strip trailing slashes. (+3 more)

### Community 51 - "Search API Tests"
Cohesion: 0.18
Nodes (6): /api/search/list, DummyResponse, make_payload(), Tests for search functions, test_extract_row_maps_expected_fields(), Search Template

### Community 52 - "I4R Scraper Tests"
Cohesion: 0.27
Nodes (4): fetch_i4r(), Scrape I4R discussion papers from the IDEAS/RepEC series (all pages).      With, RepEC page currently lists 98 I4R papers for 2024., TestI4RDateRange

### Community 53 - "OpenAlex Abstract Reconstruction"
Cohesion: 0.25
Nodes (5): _abstract_index_to_text(), OpenAlexSourceAdapter, datetime, Session, Reconstruct an abstract from OpenAlex's inverted-index format.

### Community 54 - "Supabase Client Tests"
Cohesion: 0.20
Nodes (9): Tests for shared/supabase_client.py — all Supabase calls are mocked., When SUPABASE_URL is empty, all functions return error dict., get_validation_stats returns expected keys with mocked HTTP., Second call within TTL does not make HTTP request., Counts use validation_queue schema: record_id + type_check/original_check/outcom, test_cache_is_used(), test_correction_frequency_shape(), test_not_configured_returns_error() (+1 more)

### Community 55 - "Pipeline Statistics API"
Cohesion: 0.20
Nodes (10): _add_extracted_stats(), api_csv_stats(), _model_family(), Read only the listed columns from Parquet if it exists, else from CSV., Return pipeline stats. Fast path: stats.json; mid path: Parquet; slow: CSV., Bucket a model identifier into gemini / gpt / qwen / none / other., Populate stats dict with extracted-CSV metrics under the given prefix., Translate stats.json to the flat dict the dashboard JS expects.      Returns Non (+2 more)

### Community 56 - "Report Generation Utilities"
Cohesion: 0.32
Nodes (7): DataFrame, Path, output_writer.py — CSV and markdown output generators.  Handles:   - Writing gap, Write gap analysis DataFrame to CSV with Excel-friendly encoding.      Args:, Write comprehensive markdown report with all findings.      Args:         output, write_gap_csv(), write_report_markdown()

### Community 57 - "Phrase Coverage Analysis"
Cohesion: 0.32
Nodes (7): _first_added_hit(), Series, phrase_coverage_analysis.py — Compare phrase-detection coverage between the curr, Case-insensitive vectorized str.contains — NaN-safe, returns bool Series., Return the matched text of the first ADDED phrase that fires (for sample display, run_analysis(), _vec_contains()

### Community 59 - "Data Listing Templates"
Cohesion: 0.29
Nodes (7): /api/extract/list, /api/flora/detail, /api/flora/list, /api/target-pending/list, Extract Template, FLoRA Template, Target Pending Template

### Community 60 - "Input Generation Templates"
Cohesion: 0.29
Nodes (7): /api/input/counts, /api/input/generate-matches, /api/input/generate-originals, /api/multi-originals/candidates, /api/multi-originals/run, Input Template, Multi-Originals Template

### Community 61 - "Semantic Scholar Search"
Cohesion: 0.38
Nodes (4): fetch_semantic_scholar_candidates(), DataFrame, Search Semantic Scholar for papers matching replication phrases.      Each phr, TestSemanticScholarDateRange

### Community 62 - "Disambiguation State Management"
Cohesion: 0.33
Nodes (4): api_lookup(), _pdf_serve_url(), routes/disambiguation.py — Single-DOI disambiguation page and API.  Blueprint pr, state.py — Shared mutable application state, populated by app.py at startup.  Bl

### Community 63 - "DOI Matching Logic"
Cohesion: 0.33
Nodes (6): match_by_doi(), Match by exact DOI.      Args:         row_candidate: dict with 'doi_r' key, Two rows with matching DOI should match., Rows with missing DOI should not match by DOI., test_match_by_doi_exact(), test_match_by_doi_missing()

### Community 64 - "Fuzzy Title Matching"
Cohesion: 0.33
Nodes (6): match_by_fuzzy_title(), Match by fuzzy title similarity + exact year + first author.      Args:, Different years should not fuzzy-match., Similar titles should fuzzy-match above threshold., test_match_by_fuzzy_title_similar(), test_match_by_fuzzy_title_year_mismatch()

### Community 65 - "OpenAlex API Examples"
Cohesion: 0.33
Nodes (5): fetch_referenced_works(), openalex_api_example.py — Standalone example of calling the OpenAlex API.  Run d, Search OpenAlex for papers containing a replication phrase., Fetch metadata for all works referenced by a given OpenAlex ID., search_replications()

### Community 66 - "Source Data Cleanup"
Cohesion: 0.40
Nodes (5): clean_csv(), main(), Path, Remove rows where source is 'bob_reed' or 'i4r' (case-insensitive).     Returns, Clean both candidates.csv and filtered.csv.

### Community 67 - "Fixture Generation"
Cohesion: 0.50
Nodes (3): main(), Path, Generate a 5-row synthetic candidates.csv fixture for the offline demo.  The r

### Community 68 - "Gemini API Example"
Cohesion: 0.50
Nodes (3): call_gemini_json(), gemini_api_example.py — Standalone example of calling the Gemini API.  Run direc, Call Gemini and get a JSON response.     responseMimeType=application/json force

### Community 70 - "Filter API Templates"
Cohesion: 0.67
Nodes (3): /api/filter/list, /api/filter/run-stage3, Filter Template

### Community 71 - "Validation Templates"
Cohesion: 0.67
Nodes (3): /api/review/next, /vote, Validate Template

## Knowledge Gaps
- **19 isolated node(s):** `pipeline_example.sh script`, `data/validated.csv`, `Pipeline Template`, `Search Template`, `Set Name Template` (+14 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **10 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `clean_doi()` connect `OpenAlex Metadata Client` to `DOI Metadata Verification`, `Outcome Extraction Tests`, `Flask Web Application`, `Sensitivity Analysis`, `Candidate Ranking Engine`, `Abstract Enrichment Pipeline`, `Extraction Orchestration`, `Multi-Original Study Pipeline`, `Search Orchestrator`, `PDF Parsing and Scoring`, `Metadata Backfill Utilities`, `Impact Reporting`, `OpenAlex Search Client`, `Single-DOI Orchestration`, `Semantic Scholar Client`, `Match Type Classification`, `Candidate Deduplication`, `Data Loading Utilities`, `Filter Stage Orchestrator`, `Gap Row Backfilling`, `Candidate Discovery Tests`, `Discovery Source Manager`, `Multi-Original Routes`, `External List Scrapers`, `URL Matching Logic`, `Disambiguation State Management`, `DOI Matching Logic`?**
  _High betweenness centrality (0.220) - this node is a cross-community bridge._
- **Why does `cache_key()` connect `Impact Reporting` to `DOI Metadata Verification`, `PDF Section Extraction`, `Outcome Extraction Tests`, `Sensitivity Analysis`, `Abstract Enrichment Pipeline`, `Extraction Orchestration`, `Multi-Original Study Pipeline`, `OpenAlex Metadata Client`, `LLM Filtering and Cache`, `PDF Parsing and Scoring`, `Metadata Backfill Utilities`, `OpenAlex Search Client`, `Single-DOI Orchestration`, `Semantic Scholar Client`, `Match Type Classification`, `LLM Outcome Extraction`, `Candidate Discovery Tests`, `Multi-Original Routes`, `External List Scrapers`, `I4R Scraper Tests`, `Disambiguation State Management`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Why does `find_all_candidates()` connect `Candidate Discovery Tests` to `Extraction Orchestration`, `Multi-Original Study Pipeline`, `Citation Pattern Parsing`, `OpenAlex Metadata Client`, `Author Matching Logic`, `PDF Parsing and Scoring`, `Impact Reporting`, `Single-DOI Orchestration`, `Match Type Classification`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `clean_doi()` (e.g. with `load_all_replications()` and `load_candidates()`) actually correct?**
  _`clean_doi()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `pipeline_example.sh script`, `data/validated.csv`, `Pipeline Template` to the rest of the system?**
  _19 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `DOI Metadata Verification` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._
- **Should `PDF Section Extraction` be split into smaller, more focused modules?**
  _Cohesion score 0.05126452494873548 - nodes in this community are weakly interconnected._