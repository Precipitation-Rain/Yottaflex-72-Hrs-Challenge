"""
Usage:
    python -m src.generate_data --rows 5000 --seed 42

Creates customers.csv and weekly transactions_*.csv files with some bad rows mixed in.
The list of bad rows is written separately to data/labels/expected_rejects.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from src import schemas as s
from src.config import PROJECT_ROOT

START = datetime(2026, 1, 1)
END = datetime(2026, 5, 1)          # txn_ts is in [START, END)
CATEGORIES = ["grocery", "electronics", "fashion", "travel", "dining", "utilities"]
COUNTRIES = ["IN", "US", "GB", "DE", "SG"]
SEGMENTS = ["consumer", "smb", "enterprise"]

# Bad records per 5,000 rows (scaled with --rows).
INJECT_PER_5K = {
    "duplicate": 60,
    "missing_field": 25,
    "invalid_timestamp": 15,
    "invalid_amount": 10,
    "amount_out_of_range": 15,
    "invalid_txn_type": 10,
    "invalid_category": 10,
    "ingested_before_event": 10,
    "unknown_customer": 20,
    "before_signup": 10,
    "malformed": 10,
    "schema_drift_rows": 20,
}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def week_index(dt: datetime) -> int:
    return max(0, (dt - START).days // 7)


def week_file(i: int) -> str:
    return f"transactions_{(START + timedelta(days=7 * i)).date().isoformat()}.csv"


class Generator:
    def __init__(self, rows: int, seed: int):
        self.rng = random.Random(seed)
        self.rows = rows
        self.seed = seed
        scale = rows / 5000
        self.counts = {k: max(1, round(v * scale)) for k, v in INJECT_PER_5K.items()}
        self.next_id = 1
        self.expected = []   # answer key rows

    def make_customers(self) -> list[dict]:
        rng = self.rng
        n = max(50, self.rows // 16)
        self.customers = []
        for i in range(1, n + 1):
            if rng.random() < 0.7:
                signup = datetime(2025, 1, 1) + timedelta(days=rng.randrange(365))
            else:
                signup = datetime(2026, 1, 1) + timedelta(days=rng.randrange(105))   # up to Apr 15
            lo = max(signup, START)
            churner = rng.random() < 0.35
            active_until = lo + (END - lo) * rng.uniform(0.2, 0.9) if churner else END
            self.customers.append({
                "customer_id": f"C{i:05d}",
                "signup_date": signup.date().isoformat(),
                "country": "" if rng.random() < 0.03 else rng.choice(COUNTRIES),   # nullable
                "segment": rng.choice(SEGMENTS),
                "_signup": signup, "_lo": lo, "_hi": active_until,
                "_weight": rng.lognormvariate(0, 0.8),
            })
        return self.customers

    def _new_id(self) -> str:
        tid = f"T{self.next_id:07d}"
        self.next_id += 1
        return tid

    def _row(self, cust: dict, ts: datetime, late: bool | None = None) -> dict:
        rng = self.rng
        if late is None:
            late = rng.random() < 0.05
        delay = timedelta(days=rng.uniform(2, 10)) if late else timedelta(hours=rng.uniform(0, 6))
        return {
            "transaction_id": self._new_id(),
            "customer_id": cust["customer_id"],
            "txn_ts": iso(ts),
            "ingested_at": iso(ts + delay),
            "amount": f"{min(max(rng.lognormvariate(3.5, 1.0), 0.01), 99999.0):.2f}",
            "txn_type": "refund" if rng.random() < 0.08 else "purchase",
            "merchant_category": rng.choice(CATEGORIES),
        }

    def _random_valid(self) -> dict:
        c = self.rng.choices(self.active, weights=[x["_weight"] for x in self.active])[0]
        ts = c["_lo"] + (c["_hi"] - c["_lo"]) * self.rng.random()
        return self._row(c, ts.replace(microsecond=0))

    def make_transactions(self):
        rng = self.rng
        self.active = [c for c in self.customers if c["_hi"] > c["_lo"]]
        late_signup = [c for c in self.active if c["_signup"] >= datetime(2026, 1, 15)]
        n_bad = sum(v for k, v in self.counts.items() if k not in ("duplicate", "schema_drift_rows"))
        n_valid = self.rows - n_bad - self.counts["duplicate"] - self.counts["schema_drift_rows"]

        # boundary values, these should be loaded (not rejected)
        c0 = next(c for c in self.active if c["_signup"] < START and c["_hi"] == END)
        cs = late_signup[0]
        boundary = [
            {**self._row(c0, datetime(2026, 2, 10, 12), late=False), "amount": "0.01"},
            {**self._row(c0, datetime(2026, 2, 11, 12), late=False), "amount": "100000.00"},
            # exactly at 2026-03-01, so not part of the 03-01 snapshot
            {**self._row(c0, datetime(2026, 3, 1), late=False), "ingested_at": iso(datetime(2026, 3, 1, 1))},
            # happened before 03-01 but arrived exactly at 03-01, also not in that snapshot
            {**self._row(c0, datetime(2026, 2, 28, 23, 59, 59), late=False),
             "ingested_at": iso(datetime(2026, 3, 1))},
            # exactly at signup time
            {**self._row(cs, cs["_signup"], late=False), "txn_type": "purchase"},
        ]
        valid = boundary + [self._random_valid() for _ in range(n_valid - len(boundary))]

        bad = []   # (row_as_list_or_dict, expected_reason, issue)
        def add(row, reason, issue):
            bad.append((row, reason, issue))

        blank_cols = ["transaction_id", "customer_id", "txn_ts", "amount", "txn_type"]
        for i in range(self.counts["missing_field"]):
            add({**self._random_valid(), blank_cols[i % len(blank_cols)]: ""},
                s.MISSING_REQUIRED_FIELD, f"blank {blank_cols[i % len(blank_cols)]}")
        for v in self._cycle(["2026-13-45T10:00:00Z", "yesterday", "31/01/2026 10:00",
                              "2026-02-30T25:61:00Z"], "invalid_timestamp"):
            add({**self._random_valid(), "txn_ts": v}, s.INVALID_TIMESTAMP, f"txn_ts={v}")
        for v in self._cycle(["abc", "12,50", "N/A", "$40"], "invalid_amount"):
            add({**self._random_valid(), "amount": v}, s.INVALID_AMOUNT, f"amount={v}")
        for v in self._cycle(["0", "-15.50", "100000.01", "2500000"], "amount_out_of_range"):
            add({**self._random_valid(), "amount": v}, s.AMOUNT_OUT_OF_RANGE, f"amount={v}")
        for v in self._cycle(["PURCHASE", "chargeback", "transfer"], "invalid_txn_type"):
            add({**self._random_valid(), "txn_type": v}, s.INVALID_TXN_TYPE, f"txn_type={v}")
        for v in self._cycle(["gambling", "crypto", "Grocery"], "invalid_category"):
            add({**self._random_valid(), "merchant_category": v}, s.INVALID_MERCHANT_CATEGORY,
                f"merchant_category={v}")
        for _ in range(self.counts["ingested_before_event"]):
            r = self._random_valid()
            ts = datetime.strptime(r["txn_ts"], "%Y-%m-%dT%H:%M:%SZ")
            add({**r, "ingested_at": iso(ts - timedelta(hours=rng.uniform(1, 48)))},
                s.INGESTED_BEFORE_EVENT, "ingested_at earlier than txn_ts")
        for _ in range(self.counts["unknown_customer"]):
            add({**self._random_valid(), "customer_id": f"X{rng.randint(10000, 99999)}"},
                s.UNKNOWN_CUSTOMER_ID, "customer_id not in customers.csv")
        for i in range(self.counts["before_signup"]):
            c = late_signup[i % len(late_signup)]
            ts = c["_signup"] - timedelta(hours=rng.uniform(1, 240))
            add(self._row(c, ts.replace(microsecond=0), late=False), s.TXN_BEFORE_SIGNUP,
                "txn_ts before signup_date")
        for i in range(self.counts["malformed"]):
            vals = list(self._random_valid().values())
            row = vals + ["EXTRA"] if i % 2 == 0 else vals[:-1]
            add(row, s.MALFORMED_ROW, "extra field" if i % 2 == 0 else "missing field")

        return valid, bad

    def _cycle(self, values, key):
        return [values[i % len(values)] for i in range(self.counts[key])]

    def write(self, out: Path, labels_dir: Path) -> dict:
        out.mkdir(parents=True, exist_ok=True)
        labels_dir.mkdir(parents=True, exist_ok=True)
        for old in out.glob("transactions_*.csv"):   # only files this generator owns
            old.unlink()

        customers = self.make_customers()
        valid, bad = self.make_transactions()
        rng = self.rng

        # customers.csv (+ 3 bad customer rows, which have no transactions)
        cust_rows = [{k: c[k] for k in s.CUSTOMER_COLUMNS} for c in customers]
        bad_customers = [
            ({**cust_rows[5]}, s.DUPLICATE_CUSTOMER_ID, "exact duplicate customer row"),
            ({"customer_id": "C99990", "signup_date": "2026/02/30", "country": "IN",
              "segment": "consumer"}, s.INVALID_DATE, "signup_date not ISO"),
            ({"customer_id": "", "signup_date": "2025-06-01", "country": "US",
              "segment": "smb"}, s.MISSING_REQUIRED_FIELD, "blank customer_id"),
        ]
        all_cust = cust_rows + [r for r, _, _ in bad_customers]
        self._write_csv(out / "customers.csv", s.CUSTOMER_COLUMNS, all_cust)
        for i, (_, reason, issue) in enumerate(bad_customers):
            self.expected.append(("customers.csv", len(cust_rows) + i + 2, reason, issue))

        # put rows into weekly files and shuffle them (out-of-order data)
        files: dict[int, list] = {}
        for r in valid:
            files.setdefault(self._file_of(r), []).append((r, None, None))
        for r, reason, issue in bad:
            files.setdefault(self._file_of(r), []).append((r, reason, issue))
        for rows in files.values():
            rng.shuffle(rows)

        # duplicates: copy added at the end of the same file or a later file
        last = max(files)
        for r in rng.sample(valid[5:], self.counts["duplicate"]):
            i = self._file_of(r)
            target = i if rng.random() < 0.5 or i == last else rng.randint(i + 1, last)
            files.setdefault(target, []).append((dict(r), s.DUPLICATE_TRANSACTION_ID,
                                                 "same-file re-send" if target == i else "cross-file re-send"))

        for i in sorted(files):
            name = week_file(i)
            self._write_csv(out / name, s.TRANSACTION_COLUMNS, [r for r, _, _ in files[i]])
            for line, (_, reason, issue) in enumerate(files[i], start=2):
                if reason:
                    self.expected.append((name, line, reason, issue))

        # empty batch: a delivery with a header and no rows
        empty_name = week_file(last + 1)
        self._write_csv(out / empty_name, s.TRANSACTION_COLUMNS, [])

        # schema change: amount renamed to amount_usd
        drift_name = week_file(9).replace(".csv", "_v2.csv")
        drift_cols = [("amount_usd" if c == "amount" else c) for c in s.TRANSACTION_COLUMNS]
        drift_rows = []
        for _ in range(self.counts["schema_drift_rows"]):
            r = self._random_valid()
            r["amount_usd"] = r.pop("amount")
            drift_rows.append(r)
        self._write_csv(out / drift_name, drift_cols, drift_rows)
        self.expected.append((drift_name, 1, s.SCHEMA_MISMATCH, "column amount renamed to amount_usd"))

        self.expected.sort()
        with open(labels_dir / "expected_rejects.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["source_file", "source_line_number", "expected_reason", "injected_issue"])
            w.writerows(self.expected)

        total_rows = sum(len(v) for v in files.values()) + len(drift_rows)
        manifest = {
            "seed": self.seed,
            "requested_rows": self.rows,
            "transaction_rows_written": total_rows,
            "transaction_files": len(files) + 2,
            "customers_written": len(all_cust),
            "valid_transactions": len(valid),
            "injected": self.counts,
            "expected_rejects": len(self.expected),
            "txn_ts_range": [iso(START), iso(END)],
        }
        (labels_dir / "generation_manifest.json").write_text(json.dumps(manifest, indent=2))
        return manifest

    def _file_of(self, r) -> int:
        # file = week of ingested_at (falls back to txn_ts, then week 0)
        if isinstance(r, list):
            r = dict(zip(s.TRANSACTION_COLUMNS, r))
        for col in ("ingested_at", "txn_ts"):
            try:
                return week_index(datetime.strptime(r.get(col, ""), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
        return 0

    @staticmethod
    def _write_csv(path: Path, columns: list[str], rows: list):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(columns)
            for r in rows:
                w.writerow(r if isinstance(r, list) else [r.get(c, "") for c in columns])


def generate(rows: int, seed: int, out: Path, labels_dir: Path) -> dict:
    return Generator(rows, seed).write(Path(out), Path(labels_dir))


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate synthetic transactions")
    p.add_argument("--rows", type=int, default=5000, help="total transaction rows to write")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default=str(PROJECT_ROOT / "data" / "sample"))
    p.add_argument("--labels-dir", default=str(PROJECT_ROOT / "data" / "labels"))
    a = p.parse_args(argv)
    manifest = generate(a.rows, a.seed, Path(a.out), Path(a.labels_dir))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
