# helpers shared by all tests; each test uses its own temp folder and database
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from src import schemas
from src.config import load_config
from src.run_pipeline import run

DEFAULT_CUSTOMERS = [
    ("C1", "2025-06-01", "IN", "consumer"),
    ("C2", "2025-06-01", "US", "smb"),
    ("C3", "2026-02-15", "", "consumer"),   # signs up mid-period, country is null
]


def txn(tid, cust, ts, amount="10.00", txn_type="purchase", cat="grocery", ingested=None):
    # ingested_at defaults to 1 hour after txn_ts
    if ingested is None:
        ingested = (datetime.fromisoformat(ts.replace("Z", "")) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    return {"transaction_id": tid, "customer_id": cust, "txn_ts": ts, "ingested_at": ingested,
            "amount": amount, "txn_type": txn_type, "merchant_category": cat}


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow(r if isinstance(r, (list, tuple)) else [r.get(c, "") for c in columns])


class Env(SimpleNamespace):
    def customers(self, rows=DEFAULT_CUSTOMERS):
        write_csv(self.input / "customers.csv", schemas.CUSTOMER_COLUMNS, rows)

    def txns(self, name, rows, columns=schemas.TRANSACTION_COLUMNS):
        write_csv(self.input / name, columns, rows)

    def run(self, **kw):
        return run(self.cfg, **kw)

    def query(self, sql):
        con = duckdb.connect(str(self.cfg["paths"]["db_path"]), read_only=True)
        try:
            return con.execute(sql).df()
        finally:
            con.close()

    def scalar(self, sql):
        return self.query(sql).iloc[0, 0]

    def rejects(self):
        return self.query("SELECT source_file, source_line_number, reject_reason, reject_detail "
                          "FROM rejected_records ORDER BY source_file, source_line_number")


def make_env(tmp_path, as_of_dates=("2026-03-01",)) -> Env:
    inp = tmp_path / "input"
    inp.mkdir(parents=True, exist_ok=True)
    cfg = load_config(overrides={
        "paths": {"input_dir": str(inp), "db_path": str(tmp_path / "store.duckdb"),
                  "output_dir": str(tmp_path / "out"),
                  "expected_rejects": str(tmp_path / "no_answer_key.csv")},
        "features": {"as_of_dates": list(as_of_dates)},
    })
    return Env(input=inp, cfg=cfg, tmp=tmp_path)


@pytest.fixture
def env(tmp_path):
    e = make_env(tmp_path)
    e.customers()
    return e
