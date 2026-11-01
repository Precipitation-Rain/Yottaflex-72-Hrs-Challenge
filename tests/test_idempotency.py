import pytest

from src import publish
from tests.conftest import make_env, txn

FILE1 = [txn("T1", "C1", "2026-02-20T10:00:00Z"), txn("T2", "C2", "2026-02-21T10:00:00Z"),
         txn("T2", "C2", "2026-02-21T10:00:00Z"), txn("T3", "C9", "2026-02-21T10:00:00Z")]
FILE2 = [txn("T4", "C1", "2026-02-22T10:00:00Z"), txn("T1", "C1", "2026-02-20T10:00:00Z")]
FILE3 = [txn("T5", "C2", "2026-02-23T10:00:00Z", amount="12.34")]

SNAPSHOT_SQL = {
    "stg": "SELECT transaction_id, customer_id, txn_ts, amount, source_file, source_line_number "
           "FROM stg_transactions ORDER BY transaction_id",
    "rejects": "SELECT reject_id, source_file, source_line_number, reject_reason "
               "FROM rejected_records ORDER BY reject_id",
    "features": "SELECT * EXCLUDE (run_id, computed_at) FROM customer_features "
                "ORDER BY customer_id, as_of_date",
}


def snapshot(env):
    return {k: env.query(q) for k, q in SNAPSHOT_SQL.items()}


def assert_same(a, b):
    for k in SNAPSHOT_SQL:
        assert a[k].equals(b[k]), f"{k} differs"


def write_all(env):
    env.txns("transactions_01.csv", FILE1)
    env.txns("transactions_02.csv", FILE2)
    env.txns("transactions_03.csv", FILE3)


def test_rerun_same_input_creates_no_duplicates(env):
    write_all(env)
    first = env.run()
    before = snapshot(env)

    second = env.run()
    assert second["files_skipped"] == 3 and second["input_rows"] == 0
    assert_same(before, snapshot(env))

    forced = env.run(force=True)                       # reprocess everything
    assert forced["files_processed"] == 3
    assert_same(before, snapshot(env))

    assert first["loaded_rows"] == 4 and first["rejected_rows"] == 3
    assert env.scalar("SELECT COUNT(*) FROM pipeline_runs") == 3   # every run is logged


def test_corrected_file_replaces_previous_version(env):
    write_all(env)
    env.run()
    env.txns("transactions_03.csv", FILE3 + [txn("T6", "C2", "2026-02-24T10:00:00Z")])
    m = env.run()
    assert m["files_processed"] == 1 and m["files_skipped"] == 2
    ids = env.query("SELECT transaction_id FROM stg_transactions ORDER BY 1")["transaction_id"]
    assert list(ids) == ["T1", "T2", "T4", "T5", "T6"]


def test_partial_failure_then_retry_matches_clean_run(env, tmp_path, monkeypatch):
    write_all(env)
    real_insert = publish._insert_stg

    def crash_on_file2(con, valid, run_id):
        real_insert(con, valid, run_id)                # rows are written ...
        if (valid["source_file"] == "transactions_02.csv").any():
            raise IOError("simulated disk failure")    # ... then the load dies

    monkeypatch.setattr(publish, "_insert_stg", crash_on_file2)
    failed = env.run()
    assert failed["status"] == "FAILED"
    assert "simulated disk failure" in failed["error_message"]
    # file 1 committed, file 2 rolled back completely, file 3 never started
    files = env.query("SELECT source_file FROM processed_files")["source_file"].tolist()
    assert files == ["transactions_01.csv"]
    assert env.scalar("SELECT COUNT(*) FROM stg_transactions "
                      "WHERE source_file = 'transactions_02.csv'") == 0

    monkeypatch.setattr(publish, "_insert_stg", real_insert)
    retry = env.run()
    assert retry["status"] == "SUCCESS"
    assert retry["files_skipped"] == 1 and retry["files_processed"] == 2

    clean = make_env(tmp_path / "clean")
    clean.customers()
    write_all(clean)
    clean.run()
    assert_same(snapshot(clean), snapshot(env))


def test_run_log_records_metrics(env):
    write_all(env)
    env.run()
    run = env.query("SELECT * FROM pipeline_runs").iloc[0]
    assert run["status"] == "SUCCESS"
    assert run["input_rows"] == 7 and run["loaded_rows"] == 4 and run["rejected_rows"] == 3
    assert run["ended_at"] >= run["started_at"]
    assert run["reconciliation_status"] == "PASS"
    assert "transactions" in run["stage_timings"]
    assert (env.cfg["paths"]["output_dir"] / "reconciliation_report.md").exists()
    assert list((env.cfg["paths"]["output_dir"] / "logs").glob("*.log"))


@pytest.mark.parametrize("table", ["stg_transactions", "customer_features"])
def test_reconciliation_detects_duplicate_keys(env, table):
    # insert a duplicate on purpose, reconciliation should fail
    from src.reconcile import run_reconciliation
    write_all(env)
    env.run()
    con = publish.connect(env.cfg["paths"]["db_path"])
    con.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
    result = run_reconciliation(con, env.cfg)
    con.close()
    failed = {c["check"] for c in result["checks"] if c["status"] == "FAIL"}
    assert result["status"] == "FAIL"
    assert f"primary_key_unique_{table}" in failed
