| rule | gate | routed (winner) | proceed | strict among proceeds | Stage 3 shipped / not_a_rep / no_orig / prospective | validated proceeds: n, not a replication when strict passes vs fails | deferred |
|---|---|--:|--:|--:|---|---|--:|
| `curated-observatory` | strict | 1,760 | 1,646 | 89% | 69% / 0% / 1% / 0% | 32: 10% (n=29) vs 0% (n=3) | 167 |
| `osf-registration-completed` | strict | 542 | 502 | 70% | 21% / 4% / 5% / 31% | 11: 22% (n=9) vs 50% (n=2) | 115 |
| `replication-claim-cited-abstract` | strict | 2,219 | 2,087 | 93% | 57% / 0% / 1% / 14% | 44: 12% (n=43) vs 0% (n=1) | 150 |
| `replication-claim-cited-title` | lenient | 1,258 | 1,219 | 86% | 58% / 3% / 0% / 9% | 107: 1% (n=94) vs 8% (n=13) | 0 |
| `replication-claim-extend-prior` | lenient | 2,784 | 2,518 | 89% | 45% / 0% / 2% / 5% | 17: 12% (n=17) vs – (n=0) | 0 |
| `replication-claim-failure-prior` | lenient | 319 | 299 | 93% | 53% / 0% / 0% / 2% | 3: 0% (n=3) vs – (n=0) | 0 |
| `replication-claim-general` | strict | 17,866 | 11,190 | 80% | 42% / 0% / 3% / 9% | 161: 10% (n=151) vs 60% (n=10) | 2,196 |
| `replication-claim-title-broad` | strict | 3,227 | 2,594 | 72% | 31% / 4% / 9% / 1% | 84: 6% (n=71) vs 38% (n=13) | 711 |
| `replication-claim-title-strong` | lenient | 2,288 | 2,115 | 79% | 42% / 2% / 4% / 2% | 98: 7% (n=87) vs 0% (n=11) | 0 |

```json
{
 "release_id": "61f07fd07f1997fe5454cfdea372d3cadbab18c64b70c26ff0336406d0747865",
 "screen_pile_works": 32263,
 "proceeds": 24170,
 "deferred": 3339,
 "exempted_human_judged": 29,
 "deferred_if_winner_gate": 3384,
 "deferred_already_ended": {
  "target_pending": 1524,
  "extracted": 857,
  "no_original_found": 377,
  "prospective_registration": 154,
  "search_link_unconfirmed": 139,
  "no_evidence": 110,
  "unidentified_original": 70,
  "not_a_replication": 42,
  "keyed_link_disputed": 2,
  "api_error": 1
 },
 "deferred_never_extracted": 63,
 "validated_deferred_n": 0,
 "validated_deferred_not_a_rep": 0,
 "validated_proceed_strict_pass": {
  "n": 504,
  "not_a_replication": 38
 },
 "validated_proceed_strict_fail": {
  "n": 53,
  "not_a_replication": 13
 },
 "validated_admitted_n": 557,
 "validated_admitted_not_a_rep": 51
}
```
