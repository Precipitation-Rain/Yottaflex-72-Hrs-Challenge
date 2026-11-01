-- Sample queries against output/feature_store.duckdb.
-- Run them all with:  python -m src.show_results
-- Each query starts with a "-- name:" line.

-- name: Latest pipeline runs
SELECT run_id, mode, status, reconciliation_status, input_rows, loaded_rows,
       rejected_rows, feature_rows, label_rows, duration_seconds
FROM pipeline_runs ORDER BY started_at DESC LIMIT 5;

-- name: Rejects by reason
SELECT entity, reject_reason, COUNT(*) AS n
FROM rejected_records GROUP BY ALL ORDER BY n DESC;

-- name: Trace three rejects back to their source line
SELECT source_file, source_line_number, reject_reason, reject_detail, raw_record
FROM rejected_records ORDER BY source_file, source_line_number LIMIT 3;

-- name: File status (loaded / quarantined / empty)
SELECT source_file, status, source_rows, loaded_rows, rejected_rows
FROM processed_files ORDER BY source_file;

-- name: Late-arriving data (event before snapshot, delivered after it)
SELECT DATE '2026-03-01' AS as_of_date, COUNT(*) AS late_txns_hidden_from_snapshot
FROM stg_transactions
WHERE txn_ts < TIMESTAMP '2026-03-01' AND ingested_at >= TIMESTAMP '2026-03-01';

-- name: Feature summary per snapshot
SELECT as_of_date, COUNT(*) AS customers,
       ROUND(AVG(purchase_count_30d), 2)      AS avg_purchases_30d,
       ROUND(AVG(purchase_amount_30d), 2)     AS avg_spend_30d,
       SUM(CASE WHEN days_since_last_purchase IS NULL THEN 1 ELSE 0 END) AS never_purchased
FROM customer_features GROUP BY as_of_date ORDER BY as_of_date;

-- name: Label balance in the training set
SELECT as_of_date, COUNT(*) AS rows, SUM(churn_label) AS churned,
       ROUND(AVG(churn_label), 3) AS churn_rate
FROM training_set GROUP BY as_of_date ORDER BY as_of_date;

-- name: Leakage check - every feature row equals a strictly-before-snapshot recount (must be 0)
SELECT COUNT(*) AS mismatched_rows
FROM customer_features f
WHERE f.purchase_count_90d <> (
    SELECT COUNT(*) FROM stg_transactions t
    WHERE t.customer_id = f.customer_id AND t.txn_type = 'purchase'
      AND t.txn_ts      <  CAST(f.as_of_date AS TIMESTAMP)
      AND t.ingested_at <  CAST(f.as_of_date AS TIMESTAMP)
      AND t.txn_ts      >= CAST(f.as_of_date AS TIMESTAMP) - INTERVAL 90 DAY);

-- name: One customer's features over time
SELECT customer_id, as_of_date, tenure_days, purchase_count_30d, purchase_amount_30d,
       days_since_last_purchase
FROM customer_features WHERE customer_id = 'C00010' ORDER BY as_of_date;
