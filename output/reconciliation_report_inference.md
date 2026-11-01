# Reconciliation report

- run_id: `run_20261101T105543Z_19d084`
- run status: **PARTIAL_SUCCESS**
- reconciliation: **PASS**
- input rows: 0 | loaded: 0 | rejected: 0
- feature rows: 312 | label rows: 0

| check | status | expected | actual | detail |
|---|---|---|---|---|
| row_count_source_equals_loaded_plus_rejected | PASS | 4980 | 4980 | excludes 20 row(s) in 1 quarantined file(s) |
| customer_rows_source_equals_loaded_plus_rejected | PASS | 315 | 315 |  |
| amount_source_equals_loaded_plus_rejected | PASS | 8277361.49 | 8277361.49 | loaded=370638.35 rejected=7906723.14 diff=0.0 |
| primary_key_unique_stg_transactions | PASS | 4785 | 4785 | key=(transaction_id) |
| primary_key_unique_customers | PASS | 312 | 312 | key=(customer_id) |
| primary_key_unique_customer_features | PASS | 1129 | 1129 | key=(customer_id, as_of_date) |
| primary_key_unique_churn_labels | PASS | 519 | 519 | key=(customer_id, as_of_date) |
| referential_integrity_transactions_to_customers | PASS | 0 | 0 |  |
| features_cover_all_eligible_customers | PASS | 1129 | 1129 |  |
| point_in_time_feature_totals_match_recompute | PASS | count90d=9815 | count90d=9815 |  |
| rejects_traceable_to_source | PASS | 0 | 0 |  |
| rejects_match_answer_key | PASS | 199 | 199 | missing=[] unexpected=[] |
