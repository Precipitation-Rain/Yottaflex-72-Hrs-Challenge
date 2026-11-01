# source file columns
CUSTOMER_COLUMNS = ["customer_id", "signup_date", "country", "segment"]
CUSTOMER_REQUIRED = ["customer_id", "signup_date"]

TRANSACTION_COLUMNS = [
    "transaction_id",
    "customer_id",
    "txn_ts",
    "ingested_at",
    "amount",
    "txn_type",
    "merchant_category",
]
TRANSACTION_REQUIRED = TRANSACTION_COLUMNS

# reject reasons (checked in this order, first failure wins)
MALFORMED_ROW = "malformed_row"
MISSING_REQUIRED_FIELD = "missing_required_field"
INVALID_TIMESTAMP = "invalid_timestamp"
INVALID_DATE = "invalid_date"
INVALID_AMOUNT = "invalid_amount"
AMOUNT_OUT_OF_RANGE = "amount_out_of_range"
INVALID_TXN_TYPE = "invalid_txn_type"
INVALID_MERCHANT_CATEGORY = "invalid_merchant_category"
INGESTED_BEFORE_EVENT = "ingested_before_event"
UNKNOWN_CUSTOMER_ID = "unknown_customer_id"
TXN_BEFORE_SIGNUP = "txn_before_signup"
DUPLICATE_TRANSACTION_ID = "duplicate_transaction_id"
DUPLICATE_CUSTOMER_ID = "duplicate_customer_id"
SCHEMA_MISMATCH = "schema_mismatch"

# Tables. No PRIMARY KEY constraints: uniqueness is handled by the pipeline
# and checked in reconcile.py.
DDL = [
    """
    CREATE TABLE IF NOT EXISTS customers (
        customer_id        VARCHAR,
        signup_date        DATE,
        country            VARCHAR,
        segment            VARCHAR,
        source_file        VARCHAR,
        source_line_number INTEGER,
        run_id             VARCHAR,
        loaded_at          TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stg_transactions (
        transaction_id      VARCHAR,
        customer_id         VARCHAR,
        txn_ts              TIMESTAMP,
        ingested_at         TIMESTAMP,
        amount              DECIMAL(12, 2),
        txn_type            VARCHAR,
        merchant_category   VARCHAR,
        arrival_delay_hours DOUBLE,
        is_late             BOOLEAN,
        source_file         VARCHAR,
        source_line_number  INTEGER,
        run_id              VARCHAR,
        loaded_at           TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rejected_records (
        reject_id          VARCHAR,
        run_id             VARCHAR,
        entity             VARCHAR,
        source_file        VARCHAR,
        source_line_number INTEGER,
        reject_reason      VARCHAR,
        reject_detail      VARCHAR,
        raw_record         VARCHAR,
        rejected_at        TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_files (
        source_file    VARCHAR,
        file_hash      VARCHAR,
        status         VARCHAR,
        source_rows    INTEGER,
        loaded_rows    INTEGER,
        rejected_rows  INTEGER,
        run_id         VARCHAR,
        processed_at   TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_features (
        customer_id              VARCHAR,
        as_of_date               DATE,
        tenure_days              INTEGER,
        purchase_count_7d        INTEGER,
        purchase_count_30d       INTEGER,
        purchase_count_90d       INTEGER,
        purchase_amount_30d      DECIMAL(14, 2),
        avg_purchase_amount_30d  DECIMAL(14, 2),
        max_purchase_amount_90d  DECIMAL(14, 2),
        refund_count_90d         INTEGER,
        distinct_categories_30d  INTEGER,
        days_since_last_purchase INTEGER,
        feature_version          VARCHAR,
        run_id                   VARCHAR,
        computed_at              TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS churn_labels (
        customer_id       VARCHAR,
        as_of_date        DATE,
        label_window_days INTEGER,
        churn_label       INTEGER,
        run_id            VARCHAR,
        computed_at       TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id                VARCHAR,
        mode                  VARCHAR,
        status                VARCHAR,
        started_at            TIMESTAMP,
        ended_at              TIMESTAMP,
        duration_seconds      DOUBLE,
        files_seen            INTEGER,
        files_processed       INTEGER,
        files_skipped         INTEGER,
        files_quarantined     INTEGER,
        input_rows            INTEGER,
        loaded_rows           INTEGER,
        rejected_rows         INTEGER,
        feature_rows          INTEGER,
        label_rows            INTEGER,
        reconciliation_status VARCHAR,
        stage_timings         VARCHAR,
        error_message         VARCHAR
    )
    """,
    """
    CREATE OR REPLACE VIEW training_set AS
    SELECT f.*, l.label_window_days, l.churn_label
    FROM customer_features f
    JOIN churn_labels l USING (customer_id, as_of_date)
    """,
]
