from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pandas as pd

FEATURE_VERSION = "v1"

FEATURE_COLUMNS = [
    "tenure_days",
    "purchase_count_7d",
    "purchase_count_30d",
    "purchase_count_90d",
    "purchase_amount_30d",
    "avg_purchase_amount_30d",
    "max_purchase_amount_90d",
    "refund_count_90d",
    "distinct_categories_30d",
    "days_since_last_purchase",
]

# customers who had signed up before each as_of_date
_ELIGIBLE = """
    snapshots AS (
        SELECT DISTINCT CAST(as_of_date AS DATE) AS as_of_date FROM _as_of_dates
    ),
    eligible AS (
        SELECT c.customer_id, c.signup_date, s.as_of_date,
               CAST(s.as_of_date AS TIMESTAMP) AS as_of_ts
        FROM customers c
        CROSS JOIN snapshots s
        WHERE c.signup_date < s.as_of_date
    )
"""

# Point-in-time rule: a transaction is used only if it happened before as_of
# AND we had received it before as_of (ingested_at). This blocks late data
# that wasn't available yet at that time.
FEATURE_SQL = f"""
WITH {_ELIGIBLE},
visible AS (
    SELECT e.customer_id, e.as_of_date, t.txn_ts, t.amount, t.txn_type, t.merchant_category
    FROM eligible e
    JOIN stg_transactions t
      ON  t.customer_id = e.customer_id
      AND t.txn_ts      < e.as_of_ts
      AND t.ingested_at < e.as_of_ts
)
SELECT
    e.customer_id,
    e.as_of_date,
    CAST(date_diff('day', e.signup_date, e.as_of_date) AS INTEGER)                       AS tenure_days,
    COUNT(v.txn_ts) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 7 DAY)  AS purchase_count_7d,
    COUNT(v.txn_ts) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 30 DAY) AS purchase_count_30d,
    COUNT(v.txn_ts) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 90 DAY) AS purchase_count_90d,
    COALESCE(SUM(v.amount) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 30 DAY), 0)
                                                                                         AS purchase_amount_30d,
    ROUND(AVG(v.amount) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 30 DAY), 2)
                                                                                         AS avg_purchase_amount_30d,
    MAX(v.amount) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 90 DAY)   AS max_purchase_amount_90d,
    COUNT(v.txn_ts) FILTER (WHERE v.txn_type = 'refund' AND v.txn_ts >= e.as_of_ts - INTERVAL 90 DAY)   AS refund_count_90d,
    COUNT(DISTINCT v.merchant_category) FILTER (WHERE v.txn_type = 'purchase' AND v.txn_ts >= e.as_of_ts - INTERVAL 30 DAY)
                                                                                         AS distinct_categories_30d,
    CAST(date_diff('day', CAST(MAX(v.txn_ts) FILTER (WHERE v.txn_type = 'purchase') AS DATE), e.as_of_date) AS INTEGER)
                                                                                         AS days_since_last_purchase
FROM eligible e
LEFT JOIN visible v
  ON v.customer_id = e.customer_id AND v.as_of_date = e.as_of_date
GROUP BY e.customer_id, e.as_of_date, e.signup_date, e.as_of_ts
ORDER BY e.as_of_date, e.customer_id
"""

# label = 1 if no purchase in [as_of, as_of + window)
LABEL_SQL = f"""
WITH {_ELIGIBLE}
SELECT
    e.customer_id,
    e.as_of_date,
    $window                                                    AS label_window_days,
    CASE WHEN COUNT(t.transaction_id) = 0 THEN 1 ELSE 0 END    AS churn_label
FROM eligible e
LEFT JOIN stg_transactions t
  ON  t.customer_id = e.customer_id
  AND t.txn_type = 'purchase'
  AND t.txn_ts >= e.as_of_ts
  AND t.txn_ts <  e.as_of_ts + to_days(CAST($window AS INTEGER))
GROUP BY e.customer_id, e.as_of_date
ORDER BY e.as_of_date, e.customer_id
"""


def _with_dates(con: duckdb.DuckDBPyConnection, as_of_dates: list[str]):
    dates = pd.DataFrame({"as_of_date": pd.to_datetime(list(as_of_dates)).date})
    con.register("_as_of_dates", dates)


def build_features(con: duckdb.DuckDBPyConnection, as_of_dates: list[str]) -> pd.DataFrame:
    # used by both train and inference mode
    if not as_of_dates:
        return pd.DataFrame(columns=["customer_id", "as_of_date", *FEATURE_COLUMNS])
    _with_dates(con, as_of_dates)
    try:
        return con.execute(FEATURE_SQL).df()
    finally:
        con.unregister("_as_of_dates")


def mature_label_dates(con: duckdb.DuckDBPyConnection, as_of_dates: list[str],
                       window_days: int) -> tuple[list[str], list[str]]:
    # a label can only be built if we have data for the whole future window
    max_ts = con.execute("SELECT MAX(txn_ts) FROM stg_transactions").fetchone()[0]
    mature, skipped = [], []
    for d in as_of_dates:
        end = datetime.fromisoformat(str(d)) + timedelta(days=int(window_days))
        (mature if max_ts is not None and max_ts >= end else skipped).append(d)
    return mature, skipped


def build_labels(con: duckdb.DuckDBPyConnection, as_of_dates: list[str],
                 window_days: int) -> pd.DataFrame:
    if not as_of_dates:
        return pd.DataFrame(columns=["customer_id", "as_of_date", "label_window_days", "churn_label"])
    _with_dates(con, as_of_dates)
    try:
        return con.execute(LABEL_SQL, {"window": window_days}).df()
    finally:
        con.unregister("_as_of_dates")
