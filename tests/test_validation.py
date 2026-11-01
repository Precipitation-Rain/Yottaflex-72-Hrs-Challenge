import pytest

from src import schemas as s
from src.run_pipeline import main
from tests.conftest import txn


def test_happy_path_loads_everything(env):
    env.txns("transactions_01.csv", [
        txn("T1", "C1", "2026-02-25T10:00:00Z"),
        txn("T2", "C2", "2026-02-26T10:00:00Z", txn_type="refund"),
    ])
    m = env.run()
    assert m["status"] == "SUCCESS"
    assert m["reconciliation_status"] == "PASS"
    assert (m["input_rows"], m["loaded_rows"], m["rejected_rows"]) == (2, 2, 0)
    assert env.scalar("SELECT COUNT(*) FROM stg_transactions") == 2


def test_empty_batch_header_only(env):
    env.txns("transactions_01.csv", [])
    m = env.run()
    assert m["status"] == "SUCCESS"
    assert m["input_rows"] == 0 and m["loaded_rows"] == 0
    assert env.scalar("SELECT status FROM processed_files") == "EMPTY"
    # features are still produced (all-zero activity) for eligible customers
    assert env.scalar("SELECT SUM(purchase_count_90d) FROM customer_features") == 0


def test_no_transaction_files_and_zero_byte_file(env):
    m = env.run()
    assert m["status"] == "SUCCESS" and m["files_seen"] == 0
    (env.input / "transactions_02.csv").write_bytes(b"")
    m = env.run()
    assert m["status"] == "SUCCESS"
    assert env.scalar("SELECT status FROM processed_files") == "EMPTY"


def test_malformed_rows_are_rejected_with_line_numbers(env):
    good = list(txn("T1", "C1", "2026-02-25T10:00:00Z").values())
    env.txns("transactions_01.csv", [
        good,
        good[:3],                                                      # line 3: too few fields
        list(txn("T3", "C1", "2026-02-25T11:00:00Z").values()) + ["x"],  # line 4: too many
        list(txn("T4", "C1", "2026-02-25T12:00:00Z").values()),
    ])
    m = env.run()
    rej = env.rejects()
    assert list(rej["source_line_number"]) == [3, 4]
    assert set(rej["reject_reason"]) == {s.MALFORMED_ROW}
    assert m["loaded_rows"] == 2 and m["reconciliation_status"] == "PASS"


def test_missing_required_fields_and_nullable_columns(env):
    env.txns("transactions_01.csv", [
        txn("T1", "", "2026-02-25T10:00:00Z"),
        txn("T2", "C1", "2026-02-25T10:00:00Z", amount=""),
        txn("", "C1", "2026-02-25T10:00:00Z"),
        txn("T4", "C3", "2026-02-25T10:00:00Z"),          # C3 has a NULL country: still fine
    ])
    env.run()
    rej = env.rejects()
    assert list(rej["reject_reason"]) == [s.MISSING_REQUIRED_FIELD] * 3
    assert list(rej["reject_detail"]) == ["customer_id", "amount", "transaction_id"]
    assert env.scalar("SELECT COUNT(*) FROM customers WHERE country IS NULL") == 1
    assert env.scalar("SELECT transaction_id FROM stg_transactions") == "T4"


@pytest.mark.parametrize("override, reason", [
    ({"txn_ts": "2026-13-45T10:00:00Z"}, s.INVALID_TIMESTAMP),
    ({"txn_ts": "31/01/2026 10:00"}, s.INVALID_TIMESTAMP),
    ({"ingested_at": "yesterday"}, s.INVALID_TIMESTAMP),
    ({"amount": "12,50"}, s.INVALID_AMOUNT),
    ({"amount": "abc"}, s.INVALID_AMOUNT),
    ({"amount": "0"}, s.AMOUNT_OUT_OF_RANGE),
    ({"amount": "-5"}, s.AMOUNT_OUT_OF_RANGE),
    ({"amount": "100000.01"}, s.AMOUNT_OUT_OF_RANGE),
    ({"txn_type": "PURCHASE"}, s.INVALID_TXN_TYPE),
    ({"merchant_category": "gambling"}, s.INVALID_MERCHANT_CATEGORY),
    ({"ingested_at": "2026-02-24T10:00:00Z"}, s.INGESTED_BEFORE_EVENT),
    ({"customer_id": "C999"}, s.UNKNOWN_CUSTOMER_ID),          # invalid reference key
])
def test_invalid_values_are_rejected(env, override, reason):
    env.txns("transactions_01.csv", [
        txn("T1", "C1", "2026-02-25T10:00:00Z"),
        {**txn("T2", "C1", "2026-02-25T10:00:00Z"), **override},
    ])
    m = env.run()
    rej = env.rejects()
    assert list(rej["reject_reason"]) == [reason]
    assert rej["source_line_number"].iloc[0] == 3
    assert m["loaded_rows"] == 1 and m["reconciliation_status"] == "PASS"


def test_amount_boundaries_are_accepted(env):
    env.txns("transactions_01.csv", [
        txn("T1", "C1", "2026-02-25T10:00:00Z", amount="0.01"),
        txn("T2", "C1", "2026-02-25T10:00:00Z", amount="100000.00"),
    ])
    assert env.run()["loaded_rows"] == 2


def test_duplicates_within_and_across_files(env):
    env.txns("transactions_01.csv", [
        txn("T1", "C1", "2026-02-25T10:00:00Z"),
        txn("T1", "C1", "2026-02-25T10:00:00Z"),                   # exact duplicate
        txn("T2", "C1", "2026-02-25T10:00:00Z", amount="5.00"),
        txn("T2", "C1", "2026-02-25T10:00:00Z", amount="7.00"),    # conflicting duplicate
    ])
    env.txns("transactions_02.csv", [txn("T1", "C1", "2026-02-25T10:00:00Z")])  # re-sent later
    m = env.run()
    rej = env.rejects()
    assert list(zip(rej["source_file"], rej["source_line_number"])) == [
        ("transactions_01.csv", 3), ("transactions_01.csv", 5), ("transactions_02.csv", 2)]
    assert set(rej["reject_reason"]) == {s.DUPLICATE_TRANSACTION_ID}
    # first seen wins
    assert env.scalar("SELECT amount FROM stg_transactions WHERE transaction_id = 'T2'") == 5
    assert m["loaded_rows"] == 2


def test_invalid_first_copy_does_not_block_valid_second_copy(env):
    env.txns("transactions_01.csv", [
        txn("T1", "C1", "2026-02-25T10:00:00Z", amount="oops"),
        txn("T1", "C1", "2026-02-25T10:00:00Z", amount="9.99"),
    ])
    env.run()
    assert env.scalar("SELECT amount FROM stg_transactions") == pytest.approx(9.99)
    assert list(env.rejects()["reject_reason"]) == [s.INVALID_AMOUNT]


def test_bad_customer_rows(env):
    env.customers([
        ("C1", "2025-06-01", "IN", "consumer"),
        ("C1", "2025-07-01", "IN", "consumer"),     # duplicate key
        ("C2", "2025/06/01", "US", "smb"),          # not ISO date
        ("", "2025-06-01", "US", "smb"),            # missing key
    ])
    env.txns("transactions_01.csv", [txn("T1", "C2", "2026-02-25T10:00:00Z")])
    env.run()
    rej = env.rejects()
    assert list(rej["reject_reason"]) == [s.DUPLICATE_CUSTOMER_ID, s.INVALID_DATE,
                                          s.MISSING_REQUIRED_FIELD, s.UNKNOWN_CUSTOMER_ID]
    assert env.scalar("SELECT CAST(signup_date AS VARCHAR) FROM customers") == "2025-06-01"


def test_schema_change_missing_column_quarantines_file(env):
    cols = [("amount_usd" if c == "amount" else c) for c in s.TRANSACTION_COLUMNS]
    bad = txn("T9", "C1", "2026-02-25T10:00:00Z")
    bad["amount_usd"] = bad.pop("amount")
    env.txns("transactions_01.csv", [txn("T1", "C1", "2026-02-25T10:00:00Z")])
    env.txns("transactions_02.csv", [bad, bad], columns=cols)
    m = env.run()
    assert m["status"] == "PARTIAL_SUCCESS" and m["files_quarantined"] == 1
    assert m["loaded_rows"] == 1                       # the good file is still loaded
    rej = env.rejects()
    assert rej.iloc[0]["reject_reason"] == s.SCHEMA_MISMATCH
    assert "amount" in rej.iloc[0]["reject_detail"]
    assert env.scalar("SELECT status FROM processed_files WHERE source_file = 'transactions_02.csv'") \
        == "QUARANTINED"
    # rerun keeps reporting the still-broken file
    assert env.run()["status"] == "PARTIAL_SUCCESS"


def test_schema_change_extra_and_reordered_columns_still_load(env):
    cols = list(reversed(s.TRANSACTION_COLUMNS)) + ["new_column"]
    env.txns("transactions_01.csv", [{**txn("T1", "C1", "2026-02-25T10:00:00Z"), "new_column": "x"}],
             columns=cols)
    m = env.run()
    assert m["status"] == "SUCCESS" and m["loaded_rows"] == 1


def test_missing_customers_file_fails_run_with_reason(env):
    (env.input / "customers.csv").unlink()
    m = env.run()
    assert m["status"] == "FAILED"
    assert "customers file not found" in m["error_message"]
    assert env.scalar("SELECT status FROM pipeline_runs") == "FAILED"
    exit_code = main(["--input-dir", str(env.input), "--db-path", str(env.cfg["paths"]["db_path"]),
                      "--output-dir", str(env.cfg["paths"]["output_dir"])])
    assert exit_code == 1
