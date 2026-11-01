from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pandas as pd


def _check(name, passed, expected, actual, detail=""):
    return {"check": name, "status": "PASS" if passed else "FAIL",
            "expected": expected, "actual": actual, "detail": detail}


def _to_float(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _scan_source(path: Path) -> tuple[int, float]:
    # Count rows and sum amounts straight from the raw file, without using
    # the pipeline's own code, so the comparison is independent.
    rows, total = 0, 0.0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            if header is None:
                header = [c.strip() for c in row]
                continue
            rows += 1
            if len(row) == len(header) and "amount" in header:
                amt = _to_float(row[header.index("amount")].strip())
                total += amt or 0.0
    return rows, total


def run_reconciliation(con, cfg: dict) -> dict:
    input_dir = Path(cfg["paths"]["input_dir"])
    checks = []
    files = con.execute(
        "SELECT source_file, status, source_rows FROM processed_files ORDER BY source_file"
    ).df()
    loaded_files = files[files["status"].isin(["LOADED", "EMPTY"])]

    # 1. source rows = loaded + rejected (per file)
    per_file = con.execute("""
        SELECT f.source_file,
               (SELECT COUNT(*) FROM stg_transactions s WHERE s.source_file = f.source_file) AS loaded,
               (SELECT COUNT(*) FROM rejected_records r WHERE r.source_file = f.source_file
                                                          AND r.entity = 'transaction') AS rejected
        FROM processed_files f WHERE f.status IN ('LOADED', 'EMPTY')
    """).df().set_index("source_file")
    mismatched, src_total, src_amount = [], 0, 0.0
    for name in loaded_files["source_file"]:
        path = input_dir / name
        if not path.exists():
            mismatched.append(f"{name}: source file missing")
            continue
        n, amt = _scan_source(path)
        src_total += n
        src_amount += amt
        got = int(per_file.loc[name, "loaded"] + per_file.loc[name, "rejected"])
        if n != got:
            mismatched.append(f"{name}: source={n} loaded+rejected={got}")
    out_total = int(per_file["loaded"].sum() + per_file["rejected"].sum()) if len(per_file) else 0
    quarantined = files[files["status"] == "QUARANTINED"]
    note = (f"excludes {int(quarantined['source_rows'].sum())} row(s) in "
            f"{len(quarantined)} quarantined file(s)") if len(quarantined) else ""
    checks.append(_check("row_count_source_equals_loaded_plus_rejected",
                         not mismatched, src_total, out_total,
                         "; ".join(mismatched[:5] + ([note] if note else []))))

    cust_path = input_dir / cfg["paths"]["customers_file"]
    if cust_path.exists():
        n_src, _ = _scan_source(cust_path)
        n_out = con.execute("""
            SELECT (SELECT COUNT(*) FROM customers)
                 + (SELECT COUNT(*) FROM rejected_records WHERE entity = 'customer')
        """).fetchone()[0]
        checks.append(_check("customer_rows_source_equals_loaded_plus_rejected",
                             n_src == n_out, n_src, n_out))

    # 2. source amount = loaded amount + rejected amount
    stg_amount = float(con.execute("SELECT COALESCE(SUM(amount), 0) FROM stg_transactions").fetchone()[0])
    rej_amount = 0.0
    loaded_names = set(loaded_files["source_file"])
    for f, raw in con.execute(
        "SELECT source_file, raw_record FROM rejected_records WHERE entity = 'transaction'"
    ).fetchall():
        rec = json.loads(raw) if raw else None
        # malformed rows are stored as lists and are skipped on both sides
        if f in loaded_names and isinstance(rec, dict):
            rej_amount += _to_float(rec.get("amount")) or 0.0
    diff = round(src_amount - (stg_amount + rej_amount), 2)
    checks.append(_check("amount_source_equals_loaded_plus_rejected", abs(diff) < 0.01,
                         round(src_amount, 2), round(stg_amount + rej_amount, 2),
                         f"loaded={stg_amount:.2f} rejected={rej_amount:.2f} diff={diff}"))

    # 3. primary keys are unique
    for table, key in [("stg_transactions", "transaction_id"),
                       ("customers", "customer_id"),
                       ("customer_features", "customer_id, as_of_date"),
                       ("churn_labels", "customer_id, as_of_date")]:
        total, distinct = con.execute(
            f"SELECT COUNT(*), (SELECT COUNT(*) FROM (SELECT DISTINCT {key} FROM {table})) FROM {table}"
        ).fetchone()
        checks.append(_check(f"primary_key_unique_{table}", total == distinct, total, distinct,
                             f"key=({key})"))

    # 4. every transaction has a customer
    orphans = con.execute("""
        SELECT COUNT(*) FROM stg_transactions t
        LEFT JOIN customers c USING (customer_id) WHERE c.customer_id IS NULL
    """).fetchone()[0]
    checks.append(_check("referential_integrity_transactions_to_customers", orphans == 0, 0, orphans))

    # 5. one feature row per eligible customer per as_of_date
    cover = con.execute("""
        SELECT d.as_of_date,
               (SELECT COUNT(*) FROM customers c WHERE c.signup_date < d.as_of_date) AS expected,
               (SELECT COUNT(*) FROM customer_features f WHERE f.as_of_date = d.as_of_date) AS actual
        FROM (SELECT DISTINCT as_of_date FROM customer_features) d ORDER BY 1
    """).df()
    bad = cover[cover["expected"] != cover["actual"]]
    checks.append(_check("features_cover_all_eligible_customers", bad.empty,
                         int(cover["expected"].sum()), int(cover["actual"].sum()),
                         bad.to_string(index=False) if not bad.empty else ""))

    # 6. feature totals match a separate point-in-time query
    pit = con.execute("""
        WITH d AS (SELECT DISTINCT as_of_date, CAST(as_of_date AS TIMESTAMP) AS ts FROM customer_features),
        src AS (
            SELECT d.as_of_date, COUNT(*) AS n, SUM(t.amount) FILTER (WHERE t.txn_ts >= d.ts - INTERVAL 30 DAY) AS amt30
            FROM d JOIN stg_transactions t
              ON t.txn_type = 'purchase' AND t.txn_ts < d.ts AND t.ingested_at < d.ts
             AND t.txn_ts >= d.ts - INTERVAL 90 DAY
            JOIN customers c ON c.customer_id = t.customer_id AND c.signup_date < d.as_of_date
            GROUP BY 1
        ),
        feat AS (
            SELECT as_of_date, SUM(purchase_count_90d) AS n, SUM(purchase_amount_30d) AS amt30
            FROM customer_features GROUP BY 1
        )
        SELECT feat.as_of_date, feat.n AS feat_n, COALESCE(src.n, 0) AS src_n,
               feat.amt30 AS feat_amt, COALESCE(src.amt30, 0) AS src_amt
        FROM feat LEFT JOIN src USING (as_of_date) ORDER BY 1
    """).df()
    bad = pit[(pit["feat_n"] != pit["src_n"]) | ((pit["feat_amt"] - pit["src_amt"]).abs() > 0.01)]
    checks.append(_check("point_in_time_feature_totals_match_recompute", bad.empty,
                         f"count90d={int(pit['src_n'].sum())}", f"count90d={int(pit['feat_n'].sum())}",
                         bad.to_string(index=False) if not bad.empty else ""))

    # 7. every reject points to a file, line and reason
    untraceable = con.execute("""
        SELECT COUNT(*) FROM rejected_records
        WHERE source_file IS NULL OR source_line_number IS NULL OR reject_reason IS NULL
    """).fetchone()[0]
    checks.append(_check("rejects_traceable_to_source", untraceable == 0, 0, untraceable))

    # 8. rejects match the generator's answer key (only if the file exists)
    key_path = cfg["paths"].get("expected_rejects")
    if key_path and Path(key_path).exists():
        expected = pd.read_csv(key_path, dtype={"source_file": str})
        exp = set(zip(expected["source_file"], expected["source_line_number"].astype(int),
                      expected["expected_reason"]))
        act = set(con.execute(
            "SELECT source_file, source_line_number, reject_reason FROM rejected_records"
        ).fetchall())
        missing, unexpected = sorted(exp - act), sorted(act - exp)
        checks.append(_check("rejects_match_answer_key", not missing and not unexpected,
                             len(exp), len(act),
                             f"missing={missing[:5]} unexpected={unexpected[:5]}"))
    else:
        checks.append({"check": "rejects_match_answer_key", "status": "SKIPPED",
                       "expected": None, "actual": None, "detail": "no answer key file"})

    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS"
    return {"status": status, "checks": checks}


def write_report(result: dict, summary: dict, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "reconciliation_report" + ("" if summary.get("mode") == "train" else f"_{summary.get('mode')}")
    with open(out_dir / f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump({"run": summary, **result}, f, indent=2, default=str)

    lines = [
        "# Reconciliation report",
        "",
        f"- run_id: `{summary.get('run_id')}`",
        f"- run status: **{summary.get('status')}**",
        f"- reconciliation: **{result['status']}**",
        f"- input rows: {summary.get('input_rows')} | loaded: {summary.get('loaded_rows')} "
        f"| rejected: {summary.get('rejected_rows')}",
        f"- feature rows: {summary.get('feature_rows')} | label rows: {summary.get('label_rows')}",
        "",
        "| check | status | expected | actual | detail |",
        "|---|---|---|---|---|",
    ]
    for c in result["checks"]:
        detail = str(c["detail"] or "").replace("\n", " ").replace("|", "/")
        lines.append(f"| {c['check']} | {c['status']} | {c['expected']} | {c['actual']} | {detail} |")
    (out_dir / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
