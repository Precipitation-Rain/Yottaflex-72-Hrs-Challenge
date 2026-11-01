from __future__ import annotations

import hashlib
import json

import pandas as pd

from src import schemas as s
from src.ingest import RawFile

REJECT_COLUMNS = [
    "reject_id", "entity", "source_file", "source_line_number",
    "reject_reason", "reject_detail", "raw_record",
]


def make_reject_id(source_file: str, line: int, entity: str) -> str:
    # same bad line -> same id on every rerun
    return hashlib.sha1(f"{entity}|{source_file}|{line}".encode()).hexdigest()[:16]


class _Rules:
    # keeps the first failed rule for each row

    def __init__(self, df: pd.DataFrame):
        self.reason = pd.Series(pd.NA, index=df.index, dtype="object")
        self.detail = pd.Series(pd.NA, index=df.index, dtype="object")

    def apply(self, failed: pd.Series, code: str, detail) -> None:
        m = failed.fillna(False).astype(bool) & self.reason.isna()
        self.reason[m] = code
        self.detail[m] = detail[m] if isinstance(detail, pd.Series) else detail

    @property
    def ok(self) -> pd.Series:
        return self.reason.isna()


def _blank(col: pd.Series) -> pd.Series:
    return col.isna() | (col.astype("string").str.strip() == "")


def _rejects_frame(df, rules, raw_cols, entity, source_file, malformed=()) -> pd.DataFrame:
    bad = df[~rules.ok]
    records = [
        {
            "entity": entity,
            "source_file": source_file,
            "source_line_number": int(line),
            "reject_reason": reason,
            "reject_detail": detail,
            "raw_record": json.dumps({c: (None if pd.isna(v) else str(v)) for c, v in raw.items()}),
        }
        for line, reason, detail, raw in zip(
            bad["source_line_number"], rules.reason[~rules.ok], rules.detail[~rules.ok],
            bad[raw_cols].to_dict("records"),
        )
    ]
    for m in malformed:
        records.append({
            "entity": entity,
            "source_file": source_file,
            "source_line_number": m["line"],
            "reject_reason": s.MALFORMED_ROW,
            "reject_detail": m["detail"],
            "raw_record": json.dumps(m["raw"]),
        })
    out = pd.DataFrame(records, columns=REJECT_COLUMNS[1:])
    out.insert(0, "reject_id", [
        make_reject_id(f, int(l), e)
        for f, l, e in zip(out["source_file"], out["source_line_number"], out["entity"])
    ])
    return out.sort_values("source_line_number").reset_index(drop=True)


def validate_customers(raw: RawFile) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = raw.rows.copy()
    rules = _Rules(df)

    for col in s.CUSTOMER_REQUIRED:
        rules.apply(_blank(df[col]), s.MISSING_REQUIRED_FIELD, col)

    signup = pd.to_datetime(df["signup_date"], format="%Y-%m-%d", errors="coerce")
    rules.apply(signup.isna(), s.INVALID_DATE, "signup_date is not YYYY-MM-DD")

    # first row with a customer_id wins
    dup = df["customer_id"].where(rules.ok).duplicated(keep="first") & df["customer_id"].where(rules.ok).notna()
    rules.apply(dup, s.DUPLICATE_CUSTOMER_ID, "customer_id seen earlier in file")

    rejects = _rejects_frame(df, rules, s.CUSTOMER_COLUMNS, "customer", raw.source_file, raw.malformed)

    valid = df[rules.ok].copy()
    valid["signup_date"] = signup[rules.ok]
    valid["source_file"] = raw.source_file
    for col in ("country", "segment"):
        valid[col] = valid[col].where(~_blank(valid[col]), None)
    return valid.reset_index(drop=True), rejects


def validate_transactions(
    raw: RawFile,
    customers: pd.DataFrame,
    existing_ids: set[str],
    cfg: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    # existing_ids = transaction ids already loaded from other files
    vcfg = cfg["validation"]
    df = raw.rows.copy()
    rules = _Rules(df)

    for col in s.TRANSACTION_REQUIRED:
        rules.apply(_blank(df[col]), s.MISSING_REQUIRED_FIELD, col)

    txn_ts = pd.to_datetime(df["txn_ts"], utc=True, errors="coerce", format="ISO8601")
    ingested = pd.to_datetime(df["ingested_at"], utc=True, errors="coerce", format="ISO8601")
    rules.apply(txn_ts.isna(), s.INVALID_TIMESTAMP, "txn_ts is not ISO-8601")
    rules.apply(ingested.isna(), s.INVALID_TIMESTAMP, "ingested_at is not ISO-8601")

    amount = pd.to_numeric(df["amount"], errors="coerce")
    rules.apply(amount.isna(), s.INVALID_AMOUNT, "amount is not a number")
    rules.apply((amount <= 0) | (amount > vcfg["max_amount"]), s.AMOUNT_OUT_OF_RANGE,
                f"amount must be > 0 and <= {vcfg['max_amount']}")

    rules.apply(~df["txn_type"].isin(vcfg["allowed_txn_types"]), s.INVALID_TXN_TYPE,
                "txn_type not in allowed list")
    rules.apply(~df["merchant_category"].isin(vcfg["allowed_merchant_categories"]),
                s.INVALID_MERCHANT_CATEGORY, "merchant_category not in allowed list")

    rules.apply(ingested < txn_ts, s.INGESTED_BEFORE_EVENT, "ingested_at is earlier than txn_ts")

    known = df["customer_id"].isin(customers["customer_id"])
    rules.apply(~known, s.UNKNOWN_CUSTOMER_ID, "customer_id not found in customers")

    signup = df["customer_id"].map(customers.set_index("customer_id")["signup_date"])
    txn_naive = txn_ts.dt.tz_convert(None)
    rules.apply(known & (txn_naive < signup), s.TXN_BEFORE_SIGNUP, "txn_ts is before signup_date")

    # Duplicates: first seen wins. Only rows that passed the other rules are
    # compared, so a broken first copy doesn't block a good second copy.
    ids_ok = df["transaction_id"].where(rules.ok)
    cross = ids_ok.isin(existing_ids)
    rules.apply(cross, s.DUPLICATE_TRANSACTION_ID, "transaction_id already loaded from another file")
    ids_ok = df["transaction_id"].where(rules.ok)
    within = ids_ok.duplicated(keep="first") & ids_ok.notna()
    rules.apply(within, s.DUPLICATE_TRANSACTION_ID, "transaction_id repeated in this file")

    rejects = _rejects_frame(df, rules, s.TRANSACTION_COLUMNS, "transaction", raw.source_file,
                             raw.malformed)

    ok = rules.ok
    valid = pd.DataFrame({
        "transaction_id": df.loc[ok, "transaction_id"],
        "customer_id": df.loc[ok, "customer_id"],
        "txn_ts": txn_naive[ok],
        "ingested_at": ingested[ok].dt.tz_convert(None),
        "amount": amount[ok].round(2),
        "txn_type": df.loc[ok, "txn_type"],
        "merchant_category": df.loc[ok, "merchant_category"],
    })
    valid["arrival_delay_hours"] = (
        (valid["ingested_at"] - valid["txn_ts"]).dt.total_seconds() / 3600
    ).round(3)
    valid["is_late"] = valid["arrival_delay_hours"] > vcfg["late_arrival_threshold_hours"]
    valid["source_file"] = raw.source_file
    valid["source_line_number"] = df.loc[ok, "source_line_number"]
    return valid.reset_index(drop=True), rejects
