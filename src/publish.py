from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from src import schemas
from src.features import FEATURE_COLUMNS, FEATURE_VERSION


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    for ddl in schemas.DDL:
        con.execute(ddl)
    return con


class _Txn:
    # BEGIN ... COMMIT, or ROLLBACK if anything fails inside the with-block

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN TRANSACTION")

    def __exit__(self, exc_type, *_):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _insert_rejects(con, rejects: pd.DataFrame, run_id: str) -> None:
    if rejects.empty:
        return
    df = rejects.assign(run_id=run_id, rejected_at=utc_now())
    con.register("_rej", df)
    con.execute("""
        INSERT INTO rejected_records
        SELECT reject_id, run_id, entity, source_file, CAST(source_line_number AS INTEGER),
               reject_reason, reject_detail, raw_record, rejected_at
        FROM _rej
    """)
    con.unregister("_rej")


def _insert_stg(con, valid: pd.DataFrame, run_id: str) -> None:
    if valid.empty:
        return
    df = valid.assign(run_id=run_id, loaded_at=utc_now())
    con.register("_stg", df)
    con.execute("""
        INSERT INTO stg_transactions
        SELECT transaction_id, customer_id, txn_ts, ingested_at,
               CAST(amount AS DECIMAL(12, 2)), txn_type, merchant_category,
               arrival_delay_hours, is_late, source_file,
               CAST(source_line_number AS INTEGER), run_id, loaded_at
        FROM _stg
    """)
    con.unregister("_stg")


def _record_file(con, source_file, file_hash, status, source_rows, loaded, rejected, run_id):
    con.execute("DELETE FROM processed_files WHERE source_file = ?", [source_file])
    con.execute(
        "INSERT INTO processed_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [source_file, file_hash, status, source_rows, loaded, rejected, run_id, utc_now()],
    )


def replace_customers(con, valid: pd.DataFrame, rejects: pd.DataFrame,
                      source_file: str, run_id: str) -> None:
    # customers.csv is a full snapshot, so replace the whole table
    with _Txn(con):
        con.execute("DELETE FROM customers")
        con.execute("DELETE FROM rejected_records WHERE source_file = ?", [source_file])
        if not valid.empty:
            df = valid.assign(run_id=run_id, loaded_at=utc_now())
            con.register("_cust", df)
            con.execute("""
                INSERT INTO customers
                SELECT customer_id, CAST(signup_date AS DATE), country, segment,
                       source_file, CAST(source_line_number AS INTEGER), run_id, loaded_at
                FROM _cust
            """)
            con.unregister("_cust")
        _insert_rejects(con, rejects, run_id)


def load_customers_frame(con) -> pd.DataFrame:
    df = con.execute("SELECT customer_id, signup_date FROM customers").df()
    df["signup_date"] = pd.to_datetime(df["signup_date"])
    return df


def processed_files(con) -> dict[str, tuple[str, str]]:
    rows = con.execute("SELECT source_file, file_hash, status FROM processed_files").fetchall()
    return {f: (h, st) for f, h, st in rows}


def existing_transaction_ids(con, exclude_file: str) -> set[str]:
    # exclude this file's own rows because they are about to be replaced
    rows = con.execute(
        "SELECT transaction_id FROM stg_transactions WHERE source_file <> ?", [exclude_file]
    ).fetchall()
    return {r[0] for r in rows}


def load_transaction_file(con, source_file: str, file_hash: str, valid: pd.DataFrame,
                          rejects: pd.DataFrame, source_rows: int, run_id: str,
                          status: str = "LOADED") -> None:
    # Delete old rows from this file, then insert the new ones, in one transaction.
    # Reloading a file replaces it, so reruns never create duplicates.
    with _Txn(con):
        con.execute("DELETE FROM stg_transactions WHERE source_file = ?", [source_file])
        con.execute("DELETE FROM rejected_records WHERE source_file = ?", [source_file])
        _insert_stg(con, valid, run_id)
        _insert_rejects(con, rejects, run_id)
        _record_file(con, source_file, file_hash, status, source_rows,
                     len(valid), source_rows - len(valid), run_id)


def publish_features(con, features: pd.DataFrame, as_of_dates: list[str], run_id: str) -> int:
    df = features.assign(feature_version=FEATURE_VERSION, run_id=run_id, computed_at=utc_now())
    cols = ", ".join(["customer_id", "as_of_date", *FEATURE_COLUMNS,
                      "feature_version", "run_id", "computed_at"])
    with _Txn(con):
        con.execute("DELETE FROM customer_features WHERE as_of_date IN (SELECT UNNEST(?::DATE[]))",
                    [list(as_of_dates)])
        if not df.empty:
            con.register("_feat", df)
            con.execute(f"INSERT INTO customer_features SELECT {cols} FROM _feat")
            con.unregister("_feat")
    return len(df)


def publish_labels(con, labels: pd.DataFrame, as_of_dates: list[str], run_id: str) -> int:
    df = labels.assign(run_id=run_id, computed_at=utc_now())
    with _Txn(con):
        con.execute("DELETE FROM churn_labels WHERE as_of_date IN (SELECT UNNEST(?::DATE[]))",
                    [list(as_of_dates)])
        if not df.empty:
            con.register("_lab", df)
            con.execute("""
                INSERT INTO churn_labels
                SELECT customer_id, as_of_date, label_window_days, churn_label, run_id, computed_at
                FROM _lab
            """)
            con.unregister("_lab")
    return len(df)


RUN_COLUMNS = [
    "run_id", "mode", "status", "started_at", "ended_at", "duration_seconds",
    "files_seen", "files_processed", "files_skipped", "files_quarantined",
    "input_rows", "loaded_rows", "rejected_rows", "feature_rows", "label_rows",
    "reconciliation_status", "stage_timings", "error_message",
]


def start_run(con, run_id: str, mode: str, started_at: datetime) -> None:
    con.execute(
        "INSERT INTO pipeline_runs (run_id, mode, status, started_at) VALUES (?, ?, 'RUNNING', ?)",
        [run_id, mode, started_at],
    )


def finish_run(con, run_id: str, metrics: dict) -> None:
    cols = [c for c in RUN_COLUMNS if c in metrics and c != "run_id"]
    sets = ", ".join(f"{c} = ?" for c in cols)
    con.execute(f"UPDATE pipeline_runs SET {sets} WHERE run_id = ?",
                [metrics[c] for c in cols] + [run_id])


def export_csv(con, query: str, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con.execute(query).df().to_csv(path, index=False)
