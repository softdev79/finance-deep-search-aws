# Eval scorecard: CONDITIONAL

6 cases.

| metric | value | threshold | result |
|---|---|---|---|
| run_ok | 1.000 | >= 1.0 | pass |
| completeness | 1.000 | >= 0.95 | pass |
| factual_accuracy | 1.000 | >= 1.0 | pass |
| no_invented_values | 1.000 | >= 1.0 | pass |
| citations_valid | 1.000 | >= 1.0 | pass |
| tenk_cited | 1.000 | >= 0.9 | pass |
| number_grounding | 0.942 | >= 0.85 | pass |
| avg_cost_usd | 0.010 | <= 0.05 | pass |
| p95_seconds | 129.900 | <= 300 | pass |
| judge_avg | 3.000 | >= 3.5 | FAIL (CONDITIONAL) |

## Per case

| case | run_ok | completeness | factual_accuracy | no_invented_values | number_grounding | citations_valid | tenk_cited | cost | seconds | judge |
|---|---|---|---|---|---|---|---|---|---|---|
| ai-chips | 1.000 | 1.000 | 1.000 | 1.000 | 0.913 | 1.000 | 1.000 | 0.009 | 69.700 | 2.500 |
| big-tech | 1.000 | 1.000 | 1.000 | 1.000 | 0.960 | 1.000 | 1.000 | 0.008 | 52.000 | 3.500 |
| beverages | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.004 | 34.300 | 2.000 |
| banks | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.011 | 90.400 | 2.500 |
| autos | 1.000 | 1.000 | 1.000 | 1.000 | 0.778 | 1.000 | 1.000 | 0.007 | 129.900 | 3.500 |
| retail | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.019 | 61.800 | 4.000 |

