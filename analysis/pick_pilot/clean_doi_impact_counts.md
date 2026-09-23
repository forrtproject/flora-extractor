| Artifact | Column | DOI cells | would change | `//` | `%xx` |
| --- | --- | ---: | ---: | ---: | ---: |
| extracted.csv | `doi_r` | 3,550 | 8 | 8 | 0 |
| extracted.csv | `doi_o` | 4,141 | 23 | 23 | 0 |
| set-aside CSVs (9) | `doi_r` | 2,821 | 6 | 6 | 0 |
| set-aside CSVs (9) | `doi_o` | 610 | 5 | 5 | 0 |
| flora.csv | `doi_r` | 2,299 | 1 | 1 | 0 |
| flora.csv | `doi_o` | 2,487 | 7 | 5 | 2 |
| flora.csv | `alt_identifier_r` | 154 | 0 | 0 | 0 |
| FLoRA entry sheet | `doi_r` | 3,121 | 0 | 0 | 0 |
| FLoRA entry sheet | `doi_o` | 3,820 | 26 | 25 | 1 |
| FLoRA entry sheet | `alt_identifier_r` | 2 | 0 | 0 | 0 |
| validated_skip.csv | `doi` | 1,769 | 14 | 14 | 0 |
| Supabase `unvalidated` | `doi_r` | 3,040 | 9 | 9 | 0 |
| Supabase `unvalidated` | `doi_o` | 3,689 | 26 | 26 | 0 |
| Supabase `validated` | `doi_r` | 327 | 1 | 1 | 0 |
| Supabase `validated` | `doi_o` | 347 | 1 | 1 | 0 |

- extracted.csv pair_ids that are md5(doi_r|doi_o) today: 3,550; would move: 27
- set-aside pair_ids likewise: 519; would move: 6
- Supabase `unvalidated`: 3,706 rows; pair_id = md5(doi_r|doi_o) on 3,023; would move on re-export: 29
- Supabase `validated`: 349 rows; pair_id = md5(doi_r|doi_o) on 0; would move on re-export: 0
- FLoRA skip set: 2,324 DOIs → 2,324 after normalising (0 collapse into an existing spelling)
- extracted + set-aside rows that would NEWLY match the FLoRA skip list: 0; the validated skip list: 2
