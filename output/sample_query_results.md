# Sample query results

## Latest pipeline runs

```sql
SELECT run_id, mode, status, reconciliation_status, input_rows, loaded_rows,
       rejected_rows, feature_rows, label_rows, duration_seconds
FROM pipeline_runs ORDER BY started_at DESC LIMIT 5
```

```
                     run_id      mode          status reconciliation_status  input_rows  loaded_rows  rejected_rows  feature_rows  label_rows  duration_seconds
run_20261101T105543Z_19d084 inference PARTIAL_SUCCESS                  PASS           0            0              0           312           0             0.207
run_20261101T105527Z_6361b2     train PARTIAL_SUCCESS                  PASS           0            0              0           817         519             0.221
run_20261101T105508Z_155724     train PARTIAL_SUCCESS                  PASS           0            0              0           817         519             0.615
run_20261101T102826Z_9ce07c inference PARTIAL_SUCCESS                  PASS           0            0              0           312           0             0.189
run_20261101T102825Z_e37c3a     train PARTIAL_SUCCESS                  PASS           0            0              0           817         519             0.229
```

## Rejects by reason

```sql
SELECT entity, reject_reason, COUNT(*) AS n
FROM rejected_records GROUP BY ALL ORDER BY n DESC
```

```
     entity             reject_reason  n
transaction  duplicate_transaction_id 60
transaction    missing_required_field 25
transaction       unknown_customer_id 20
transaction       amount_out_of_range 15
transaction         invalid_timestamp 15
transaction         txn_before_signup 10
transaction            invalid_amount 10
transaction invalid_merchant_category 10
transaction          invalid_txn_type 10
transaction     ingested_before_event 10
transaction             malformed_row 10
       file           schema_mismatch  1
   customer              invalid_date  1
   customer    missing_required_field  1
   customer     duplicate_customer_id  1
```

## Trace three rejects back to their source line

```sql
SELECT source_file, source_line_number, reject_reason, reject_detail, raw_record
FROM rejected_records ORDER BY source_file, source_line_number LIMIT 3
```

```
  source_file  source_line_number          reject_reason                    reject_detail                                                                                     raw_record
customers.csv                 314  duplicate_customer_id customer_id seen earlier in file {"customer_id": "C00006", "signup_date": "2025-04-27", "country": "US", "segment": "consumer"}
customers.csv                 315           invalid_date    signup_date is not YYYY-MM-DD {"customer_id": "C99990", "signup_date": "2026/02/30", "country": "IN", "segment": "consumer"}
customers.csv                 316 missing_required_field                      customer_id            {"customer_id": "", "signup_date": "2025-06-01", "country": "US", "segment": "smb"}
```

## File status (loaded / quarantined / empty)

```sql
SELECT source_file, status, source_rows, loaded_rows, rejected_rows
FROM processed_files ORDER BY source_file
```

```
                   source_file      status  source_rows  loaded_rows  rejected_rows
   transactions_2026-01-01.csv      LOADED          242          237              5
   transactions_2026-01-08.csv      LOADED          305          295             10
   transactions_2026-01-15.csv      LOADED          258          251              7
   transactions_2026-01-22.csv      LOADED          302          291             11
   transactions_2026-01-29.csv      LOADED          269          260              9
   transactions_2026-02-05.csv      LOADED          277          269              8
   transactions_2026-02-12.csv      LOADED          294          275             19
   transactions_2026-02-19.csv      LOADED          300          292              8
   transactions_2026-02-26.csv      LOADED          310          290             20
   transactions_2026-03-05.csv      LOADED          335          320             15
transactions_2026-03-05_v2.csv QUARANTINED           20            0             20
   transactions_2026-03-12.csv      LOADED          324          311             13
   transactions_2026-03-19.csv      LOADED          273          263             10
   transactions_2026-03-26.csv      LOADED          274          262             12
   transactions_2026-04-02.csv      LOADED          284          277              7
   transactions_2026-04-09.csv      LOADED          314          305              9
   transactions_2026-04-16.csv      LOADED          291          279             12
   transactions_2026-04-23.csv      LOADED          270          256             14
   transactions_2026-04-30.csv      LOADED           58           52              6
   transactions_2026-05-07.csv       EMPTY            0            0              0
```

## Late-arriving data (event before snapshot, delivered after it)

```sql
SELECT DATE '2026-03-01' AS as_of_date, COUNT(*) AS late_txns_hidden_from_snapshot
FROM stg_transactions
WHERE txn_ts < TIMESTAMP '2026-03-01' AND ingested_at >= TIMESTAMP '2026-03-01'
```

```
as_of_date  late_txns_hidden_from_snapshot
2026-03-01                              25
```

## Feature summary per snapshot

```sql
SELECT as_of_date, COUNT(*) AS customers,
       ROUND(AVG(purchase_count_30d), 2)      AS avg_purchases_30d,
       ROUND(AVG(purchase_amount_30d), 2)     AS avg_spend_30d,
       SUM(CASE WHEN days_since_last_purchase IS NULL THEN 1 ELSE 0 END) AS never_purchased
FROM customer_features GROUP BY as_of_date ORDER BY as_of_date
```

```
as_of_date  customers  avg_purchases_30d  avg_spend_30d  never_purchased
2026-02-01        249               4.18         235.69             42.0
2026-03-01        270               4.00         597.84             12.0
2026-04-01        298               3.86         205.34             11.0
2026-05-01        312               3.47         205.36              3.0
```

## Label balance in the training set

```sql
SELECT as_of_date, COUNT(*) AS rows, SUM(churn_label) AS churned,
       ROUND(AVG(churn_label), 3) AS churn_rate
FROM training_set GROUP BY as_of_date ORDER BY as_of_date
```

```
as_of_date  rows  churned  churn_rate
2026-02-01   249     31.0       0.124
2026-03-01   270     65.0       0.241
```

## Leakage check - every feature row equals a strictly-before-snapshot recount (must be 0)

```sql
SELECT COUNT(*) AS mismatched_rows
FROM customer_features f
WHERE f.purchase_count_90d <> (
    SELECT COUNT(*) FROM stg_transactions t
    WHERE t.customer_id = f.customer_id AND t.txn_type = 'purchase'
      AND t.txn_ts      <  CAST(f.as_of_date AS TIMESTAMP)
      AND t.ingested_at <  CAST(f.as_of_date AS TIMESTAMP)
      AND t.txn_ts      >= CAST(f.as_of_date AS TIMESTAMP) - INTERVAL 90 DAY)
```

```
 mismatched_rows
               0
```

## One customer's features over time

```sql
SELECT customer_id, as_of_date, tenure_days, purchase_count_30d, purchase_amount_30d,
       days_since_last_purchase
FROM customer_features WHERE customer_id = 'C00010' ORDER BY as_of_date
```

```
customer_id as_of_date  tenure_days  purchase_count_30d  purchase_amount_30d  days_since_last_purchase
     C00010 2026-02-01          141                   1                21.98                        22
     C00010 2026-03-01          169                   0                 0.00                        50
     C00010 2026-04-01          200                   1                31.79                        18
     C00010 2026-05-01          230                   0                 0.00                        48
```
