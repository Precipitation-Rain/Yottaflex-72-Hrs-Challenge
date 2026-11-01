# Data dictionary

Conventions: `snake_case` names, timestamps are UTC, `PK` = primary key (enforced by the
pipeline and checked by reconciliation), `FK` = foreign key.

---

## Source entities (input files)

### `customers.csv`: one row per customer (full snapshot)

| column | type | required | rules |
|---|---|---|---|
| `customer_id` | string | yes | **PK**, unique. Duplicates: first row wins, others are rejected. |
| `signup_date` | date `YYYY-MM-DD` | yes | strict ISO date |
| `country` | string | no | nullable |
| `segment` | string | no | nullable (`consumer`, `smb`, `enterprise` in generated data) |

### `transactions_<YYYY-MM-DD>.csv`: one row per transaction event

One file per weekly delivery. The file date is the start of the delivery week (by `ingested_at`).

| column | type | required | rules |
|---|---|---|---|
| `transaction_id` | string | yes | **PK** across all files. First seen wins. |
| `customer_id` | string | yes | **FK** to `customers.customer_id` |
| `txn_ts` | ISO-8601 timestamp | yes | when the event happened (event time). Must be `>= signup_date`. |
| `ingested_at` | ISO-8601 timestamp | yes | when the source delivered it (knowledge time). Must be `>= txn_ts`. |
| `amount` | decimal | yes | `0 < amount <= 100000` |
| `txn_type` | enum | yes | `purchase`, `refund` |
| `merchant_category` | enum | yes | `grocery, electronics, fashion, travel, dining, utilities` |

**Relationships:** `customers 1 —— * transactions` (by `customer_id`).
**Grain:** one row = one transaction event. Events are immutable, so history is never overwritten.

---

## Target tables (`output/feature_store.duckdb`)

### `customers`
Valid customers. Same columns as the source plus lineage: `source_file`, `source_line_number`,
`run_id`, `loaded_at`. `signup_date` is a DATE.

### `stg_transactions`: grain: one valid transaction, PK `transaction_id`

| column | type | null | description |
|---|---|---|---|
| `transaction_id` | VARCHAR | no | PK |
| `customer_id` | VARCHAR | no | FK to customers |
| `txn_ts` | TIMESTAMP | no | event time, UTC |
| `ingested_at` | TIMESTAMP | no | delivery time, UTC |
| `amount` | DECIMAL(12,2) | no | |
| `txn_type` | VARCHAR | no | `purchase` / `refund` |
| `merchant_category` | VARCHAR | no | |
| `arrival_delay_hours` | DOUBLE | no | **derived**: `ingested_at - txn_ts` in hours |
| `is_late` | BOOLEAN | no | **derived**: `arrival_delay_hours > late_arrival_threshold_hours` (24) |
| `source_file` | VARCHAR | no | lineage: file name |
| `source_line_number` | INTEGER | no | lineage: physical line in file (header = 1) |
| `run_id` | VARCHAR | no | run that loaded it |
| `loaded_at` | TIMESTAMP | no | |

### `rejected_records`: quarantine, PK `reject_id`

| column | type | description |
|---|---|---|
| `reject_id` | VARCHAR | deterministic `sha1(entity\|source_file\|line)[:16]` |
| `run_id` | VARCHAR | run that rejected it |
| `entity` | VARCHAR | `transaction`, `customer`, or `file` (whole file quarantined) |
| `source_file`, `source_line_number` | | exact location of the bad record. For a `file` reject, line = 1 (header). |
| `reject_reason` | VARCHAR | reason code (see [Reject reason codes](#reject-reason-codes)) |
| `reject_detail` | VARCHAR | human-readable detail, e.g. which column was blank |
| `raw_record` | VARCHAR (JSON) | original values as text: an object for normal rows, an array for malformed rows, the header for file rejects |
| `rejected_at` | TIMESTAMP | |

### `processed_files`: PK `source_file`

| column | description |
|---|---|
| `source_file` | file name |
| `file_hash` | SHA-256 of file bytes. Same hash means the file is skipped on rerun. |
| `status` | `LOADED`, `EMPTY`, `QUARANTINED` |
| `source_rows`, `loaded_rows`, `rejected_rows` | counts (`source = loaded + rejected`) |
| `run_id`, `processed_at` | |

### `customer_features`: grain: one customer at one snapshot, PK `(customer_id, as_of_date)`

All windows are **strictly before** `as_of_date 00:00 UTC`, and only include transactions with
`ingested_at < as_of_date`. A row exists for every customer with `signup_date < as_of_date`.

| column | type | null | definition |
|---|---|---|---|
| `customer_id` | VARCHAR | no | |
| `as_of_date` | DATE | no | snapshot date |
| `tenure_days` | INTEGER | no | `as_of_date - signup_date` |
| `purchase_count_7d` | INTEGER | no | purchases in `[D-7d, D)` |
| `purchase_count_30d` | INTEGER | no | purchases in `[D-30d, D)` |
| `purchase_count_90d` | INTEGER | no | purchases in `[D-90d, D)` |
| `purchase_amount_30d` | DECIMAL | no | sum of purchase amounts in `[D-30d, D)`, 0 if none |
| `avg_purchase_amount_30d` | DECIMAL | **yes** | average purchase in `[D-30d, D)`. NULL if no purchases. |
| `max_purchase_amount_90d` | DECIMAL | **yes** | largest purchase in `[D-90d, D)`. NULL if none. |
| `refund_count_90d` | INTEGER | no | refunds in `[D-90d, D)` |
| `distinct_categories_30d` | INTEGER | no | distinct merchant categories purchased in `[D-30d, D)` |
| `days_since_last_purchase` | INTEGER | **yes** | `D - date(last purchase before D)`. NULL if never purchased. |
| `feature_version` | VARCHAR | no | `v1`. Bump it when a definition changes. |
| `run_id`, `computed_at` | | no | lineage |

### `churn_labels`: the target, PK `(customer_id, as_of_date)`. Kept separate from features.

| column | type | definition |
|---|---|---|
| `customer_id`, `as_of_date` | | same key as features |
| `label_window_days` | INTEGER | 30 (configurable) |
| `churn_label` | INTEGER | `1` if **no purchase** in `[D, D + window)`, else `0` |
| `run_id`, `computed_at` | | lineage |

Labels are only written when `max(txn_ts) >= D + window` (the window is fully observed).

### `training_set` (view)
`customer_features JOIN churn_labels USING (customer_id, as_of_date)`, giving all feature columns plus
`label_window_days`, `churn_label`.

### `pipeline_runs`: PK `run_id`

| column | description |
|---|---|
| `run_id` | `run_<UTC start>_<6 hex>` |
| `mode` | `train` / `inference` |
| `status` | `RUNNING`, `SUCCESS`, `PARTIAL_SUCCESS` (file(s) quarantined), `FAILED` |
| `started_at`, `ended_at`, `duration_seconds` | |
| `files_seen`, `files_processed`, `files_skipped`, `files_quarantined` | |
| `input_rows`, `loaded_rows`, `rejected_rows` | rows read in **this** run (0 when everything was skipped) |
| `feature_rows`, `label_rows` | rows published |
| `reconciliation_status` | `PASS` / `FAIL` / `NOT_RUN` |
| `stage_timings` | JSON, seconds per stage |
| `error_message` | exception type and message when `FAILED` |

---

## Reject reason codes

Rules are checked in this order. A record gets the **first** reason that fails.

| code | rule |
|---|---|
| `malformed_row` | row has a different number of fields than the header |
| `missing_required_field` | a required column is blank (detail says which one) |
| `invalid_timestamp` / `invalid_date` | not ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`) / not `YYYY-MM-DD` |
| `invalid_amount` | not a number (e.g. `12,50`, `$40`) |
| `amount_out_of_range` | must be `> 0` and `<= 100000` (configurable) |
| `invalid_txn_type` | not `purchase` or `refund` (case-sensitive) |
| `invalid_merchant_category` | not in the allowed list |
| `ingested_before_event` | `ingested_at < txn_ts` (impossible) |
| `unknown_customer_id` | customer not in valid customers (referential integrity) |
| `txn_before_signup` | `txn_ts < signup_date` |
| `duplicate_transaction_id` / `duplicate_customer_id` | key already seen. First seen wins (earlier file, then lower line). |
| `schema_mismatch` | file is missing required columns, so the whole file is quarantined |

---

## Answer key (not input): `data/labels/`

| file | content |
|---|---|
| `expected_rejects.csv` | `source_file, source_line_number, expected_reason, injected_issue`: every line the generator deliberately broke |
| `generation_manifest.json` | seed, row counts, number of each injected issue |
