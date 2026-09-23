# Observatory rows without a replication DOI (handover step 11 — documented, not built)

Read off `replications_database_2026_09_04_184008.csv` on 2026-09-23. A row "has no
replication DOI" when `replication_url` is not a `doi.org/10.…` link; a "work" is one
distinct `replication_url`. Reproduce with:

```bash
.venv/bin/python -m analysis.mo_observatory.no_replication_doi
```

**262 rows / 188 works.** Only relevant if we ever import from the Observatory: our
pool, the Observatory join and the curated rule are all DOI-keyed, so none of these
reached Stage 2/3.

| Kind | Rows | Works | From FReD | In FLoRA (OSF id) | In FLoRA (OSF id or exact title) |
| --- | ---: | ---: | ---: | ---: | ---: |
| OSF (`osf.io/<guid>`) | 153 | 96 | 92 | 93 | 93 |
| web.archive.org | 66 | 64 | 1 | 0 | 4 |
| datacolada | 10 | 10 | 0 | 0 | 0 |
| thesis / institutional repository | 15 | 5 | 3 | 0 | 3 |
| other | 18 | 13 | 8 | 0 | 8 |
| **total** | **262** | **188** | **104** | **93** | **108** |

- **OSF: 93 of 96 works are already in FLoRA** (matched on the OSF guid anywhere in
  `data/flora.csv`'s `doi_r`/`url_r`/`alt_identifier_r`/`oa_url_r`). They came into both
  databases from FReD. The three that are not: `osf.io/zpwne`, `osf.io/ktnmc` (Curate
  Science scrape) and `osf.io/ht4d5` (FReD). The handover said 97 OSF works; the
  `osf.io` substring gives 96, and no other row mentions OSF.
- **web.archive.org: all 64 are archived PsychFileDrawer attempt pages**
  (`psychfiledrawer.org/replication.php?attempt=…`), 65 of 66 rows with a blank
  `source`. Unpublished replications; 4 match a FLoRA title exactly.
- **datacolada: 10** Data Colada posts (7 blank `source`, 3 from the OpenMCT
  marketing-replications export). None in FLoRA.
- **thesis / institutional repository: 5** (Helsinki HELDA, Twente essays, a Handle,
  EIU The Keep, CUNY Academic Works); 3 match a FLoRA title.
- **other: 13** — Google Drive/Docs files (4), JASNH pages (2), ERIC, clearerthinking.org,
  a De Psycholoog PDF, a PsychFileDrawer page, an Astral Codex Ten post, one blank
  `replication_url`, and one `https://doi.org/paper` (a placeholder — also listed in the
  DOI-issue sheet). 8 match a FLoRA title.

What an import would need: OSF-guid matching is already how FLoRA stores these
(`alt_identifier_r` / `url_r`), so the 3 missing OSF works are the only cheap gain; the
PsychFileDrawer and Data Colada pages have no registry record to screen from, so they
would have to be screened from the Observatory's own title/description.
