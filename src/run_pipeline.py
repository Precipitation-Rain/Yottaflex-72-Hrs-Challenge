"""
Usage:
    python -m src.run_pipeline
    python -m src.run_pipeline --mode inference --as-of 2026-05-01
    python -m src.run_pipeline --force
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path

import pandas as pd

from src import ingest, publish, reconcile, schemas
from src.config import load_config
from src.features import build_features, build_labels, mature_label_dates
from src.validate import make_reject_id, validate_customers, validate_transactions

log = logging.getLogger("pipeline")


class SchemaError(Exception):
    pass


def _setup_logging(log_file: Path) -> list[logging.Handler]:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return handlers


def _file_reject(raw: ingest.RawFile, detail: str) -> pd.DataFrame:
    # one reject row for a whole quarantined file (line 1 = header)
    return pd.DataFrame([{
        "reject_id": make_reject_id(raw.source_file, 1, "file"),
        "entity": "file",
        "source_file": raw.source_file,
        "source_line_number": 1,
        "reject_reason": schemas.SCHEMA_MISMATCH,
        "reject_detail": detail,
        "raw_record": json.dumps(raw.header),
    }])


def load_customers(con, cfg, run_id) -> pd.DataFrame:
    path = Path(cfg["paths"]["input_dir"]) / cfg["paths"]["customers_file"]
    if not path.exists():
        raise FileNotFoundError(f"customers file not found: {path}")
    raw = ingest.read_csv_file(path)
    missing, extra = ingest.check_schema(raw.header, schemas.CUSTOMER_COLUMNS)
    if missing:
        # can't check transactions without customers, so stop the run
        raise SchemaError(f"{raw.source_file}: missing columns {missing}")
    if extra:
        log.warning("%s: ignoring unexpected columns %s", raw.source_file, extra)
    valid, rejects = validate_customers(raw)
    publish.replace_customers(con, valid, rejects, raw.source_file, run_id)
    log.info("customers: source=%d loaded=%d rejected=%d",
             raw.source_rows, len(valid), len(rejects))
    return publish.load_customers_frame(con)


def load_transactions(con, cfg, run_id, customers, force, m) -> None:
    files = ingest.list_transaction_files(cfg["paths"]["input_dir"], cfg["paths"]["transactions_pattern"])
    done = publish.processed_files(con)
    m["files_seen"] = len(files)
    log.info("transactions: %d file(s) found", len(files))

    for path in files:
        file_hash = ingest.file_sha256(path)
        prev = done.get(path.name)
        # same name + same hash = already loaded, skip it
        if prev and prev[0] == file_hash and not force:
            m["files_skipped"] += 1
            if prev[1] == "QUARANTINED":
                m["files_quarantined"] += 1
                log.error("  skip %s (still QUARANTINED, unchanged since last run)", path.name)
            else:
                log.info("  skip %s (already processed, status=%s, same hash)", path.name, prev[1])
            continue

        raw = ingest.read_csv_file(path)
        m["input_rows"] += raw.source_rows

        if not raw.header:  # 0-byte file
            publish.load_transaction_file(con, raw.source_file, file_hash, raw.rows.iloc[0:0],
                                          raw.rows.iloc[0:0], 0, run_id, status="EMPTY")
            m["files_processed"] += 1
            log.warning("  %s is empty (no header) - recorded as EMPTY batch", path.name)
            continue

        missing, extra = ingest.check_schema(raw.header, schemas.TRANSACTION_COLUMNS)
        if missing:
            detail = f"missing columns {missing}; got {raw.header}"
            publish.load_transaction_file(con, raw.source_file, file_hash, pd.DataFrame(),
                                          _file_reject(raw, detail), raw.source_rows, run_id,
                                          status="QUARANTINED")
            m["files_quarantined"] += 1
            m["rejected_rows"] += raw.source_rows
            log.error("  QUARANTINE %s: %s (%d rows)", path.name, detail, raw.source_rows)
            continue
        if extra:
            log.warning("  %s: ignoring unexpected columns %s", path.name, extra)
            raw.rows = raw.rows[schemas.TRANSACTION_COLUMNS + ["source_line_number"]]

        existing = publish.existing_transaction_ids(con, raw.source_file)
        valid, rejects = validate_transactions(raw, customers, existing, cfg)
        status = "EMPTY" if raw.source_rows == 0 else "LOADED"
        publish.load_transaction_file(con, raw.source_file, file_hash, valid, rejects,
                                      raw.source_rows, run_id, status=status)
        m["files_processed"] += 1
        m["loaded_rows"] += len(valid)
        m["rejected_rows"] += len(rejects)
        log.info("  %s: source=%d loaded=%d rejected=%d late=%d",
                 path.name, raw.source_rows, len(valid), len(rejects),
                 int(valid["is_late"].sum()) if len(valid) else 0)


def build_and_publish(con, cfg, run_id, mode, as_of, m) -> list[str]:
    dates = cfg["features"]["as_of_dates"] if mode == "train" else [as_of]
    feats = build_features(con, dates)
    m["feature_rows"] = publish.publish_features(con, feats, dates, run_id)
    log.info("features: %d row(s) for as_of %s", m["feature_rows"], dates)

    if mode == "train":
        window = cfg["labels"]["churn_window_days"]
        mature, skipped = mature_label_dates(con, dates, window)
        if skipped:
            log.warning("labels: skipped %s (future %d-day window not fully observed yet)", skipped, window)
        labels = build_labels(con, mature, window)
        m["label_rows"] = publish.publish_labels(con, labels, dates, run_id)
        log.info("labels: %d row(s) for as_of %s", m["label_rows"], mature)
    return dates


def export_outputs(con, out_dir: Path, mode: str, dates: list[str]) -> None:
    q = {
        "rejected_records.csv": "SELECT * FROM rejected_records ORDER BY source_file, source_line_number",
        "pipeline_runs.csv": "SELECT * FROM pipeline_runs ORDER BY started_at",
        "processed_files.csv": "SELECT * FROM processed_files ORDER BY source_file",
    }
    if mode == "train":
        q["customer_features.csv"] = "SELECT * FROM customer_features ORDER BY as_of_date, customer_id"
        q["training_set.csv"] = "SELECT * FROM training_set ORDER BY as_of_date, customer_id"
    else:
        q[f"inference_features_{dates[0]}.csv"] = (
            f"SELECT * FROM customer_features WHERE as_of_date = DATE '{dates[0]}' ORDER BY customer_id"
        )
    for name, sql in q.items():
        publish.export_csv(con, sql, out_dir / name)


def run(cfg: dict, mode: str = "train", as_of: str | None = None, force: bool = False) -> dict:
    if mode == "inference" and not as_of:
        raise ValueError("--as-of is required in inference mode")
    if as_of:
        as_of = pd.Timestamp(as_of).date().isoformat()

    started = publish.utc_now()
    run_id = f"run_{started:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
    out_dir = Path(cfg["paths"]["output_dir"])
    handlers = _setup_logging(out_dir / "logs" / f"{run_id}.log")
    t_start = time.perf_counter()

    m = {"run_id": run_id, "mode": mode, "status": "RUNNING", "files_seen": 0,
         "files_processed": 0, "files_skipped": 0, "files_quarantined": 0,
         "input_rows": 0, "loaded_rows": 0, "rejected_rows": 0, "feature_rows": 0,
         "label_rows": 0, "reconciliation_status": "NOT_RUN", "error_message": None}
    timings: dict[str, float] = {}
    dates: list[str] = []

    def timed(stage, fn, *args):
        t = time.perf_counter()
        result = fn(*args)
        timings[stage] = round(time.perf_counter() - t, 3)
        return result

    con = publish.connect(cfg["paths"]["db_path"])
    publish.start_run(con, run_id, mode, started)
    log.info("=== %s start mode=%s input=%s", run_id, mode, cfg["paths"]["input_dir"])
    try:
        customers = timed("customers", load_customers, con, cfg, run_id)
        timed("transactions", load_transactions, con, cfg, run_id, customers, force, m)
        dates = timed("features_labels", build_and_publish, con, cfg, run_id, mode, as_of, m)
        recon = timed("reconciliation", reconcile.run_reconciliation, con, cfg)
        m["reconciliation_status"] = recon["status"]
        m["status"] = "PARTIAL_SUCCESS" if m["files_quarantined"] else "SUCCESS"
    except Exception as e:
        m["status"] = "FAILED"
        m["error_message"] = f"{type(e).__name__}: {e}"
        log.exception("run failed")
        recon = None
    finally:
        m["ended_at"] = publish.utc_now()
        m["duration_seconds"] = round(time.perf_counter() - t_start, 3)
        m["stage_timings"] = json.dumps(timings)
        publish.finish_run(con, run_id, m)
        try:
            export_outputs(con, out_dir, mode, dates or [as_of or ""])
            if recon:
                reconcile.write_report(recon, m, out_dir)
        finally:
            con.close()
            log.info("=== %s end status=%s recon=%s in=%d loaded=%d rejected=%d features=%d (%.2fs)",
                     run_id, m["status"], m["reconciliation_status"], m["input_rows"],
                     m["loaded_rows"], m["rejected_rows"], m["feature_rows"], m["duration_seconds"])
            for h in handlers:
                log.removeHandler(h)
                h.close()
    return m


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Point-in-time ML feature pipeline")
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--mode", choices=["train", "inference"], default="train")
    p.add_argument("--as-of", help="snapshot date (YYYY-MM-DD) for inference mode")
    p.add_argument("--input-dir", help="override paths.input_dir")
    p.add_argument("--db-path", help="override paths.db_path")
    p.add_argument("--output-dir", help="override paths.output_dir")
    p.add_argument("--expected-rejects", help="override paths.expected_rejects (answer key)")
    p.add_argument("--force", action="store_true", help="reprocess files even if already loaded")
    a = p.parse_args(argv)

    paths = {k: v for k, v in {"input_dir": a.input_dir, "db_path": a.db_path,
                               "output_dir": a.output_dir,
                               "expected_rejects": a.expected_rejects}.items() if v}
    cfg = load_config(a.config, {"paths": paths} if paths else None)
    m = run(cfg, mode=a.mode, as_of=a.as_of, force=a.force)
    if m["status"] == "FAILED":
        return 1
    return 2 if m["reconciliation_status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
