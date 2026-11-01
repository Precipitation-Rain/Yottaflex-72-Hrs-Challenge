# 🧮 Point-in-Time ML Feature Pipeline

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![DuckDB](https://img.shields.io/badge/DuckDB-1.1-FFF000?logo=duckdb&logoColor=black)
![pandas](https://img.shields.io/badge/pandas-2.2-150458?logo=pandas&logoColor=white)
![SQL](https://img.shields.io/badge/SQL-feature%20logic-336791?logo=postgresql&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-40%20passing-0A9EDC?logo=pytest&logoColor=white)
![YAML](https://img.shields.io/badge/YAML-config-CB171E?logo=yaml&logoColor=white)

A pipeline that turns raw **transaction data** into **ML features** for a churn model, with
**no target leakage**, **safe reruns**, and **the same feature logic for training and inference**.
It runs fully locally: no server, no cloud, no secrets.

---

## 🛠️ Tech stack

| Tool | Used for |
|---|---|
| 🐍 **Python** | Runs every step of the pipeline |
| 📄 **csv module** | Reads raw files line by line, keeping line numbers and catching broken rows |
| 🐼 **pandas** | Checks all rows of a file against each validation rule |
| 🦆 **DuckDB** | Local database file that stores all tables and runs the SQL |
| 🧾 **SQL** | Feature, label and reconciliation logic |
| ⚙️ **PyYAML** | Reads settings from `config.yaml` |
| ✅ **pytest** | 40 automated tests |
| 📝 **logging** | Console output plus a log file for each run |

---

## 🚀 How to run

```bash
python -m venv .venv
.venv\Scripts\activate              # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

python -m src.generate_data --rows 5000 --seed 42      # 1. create sample data
python -m src.run_pipeline                             # 2. run pipeline (training)
python -m src.run_pipeline                             # 3. run again -> no duplicates
python -m src.run_pipeline --mode inference --as-of 2026-05-01   # 4. inference
python -m src.show_results                             # 5. sample queries
pytest -v                                              # 6. tests
```

| Flag | Meaning |
|---|---|
| `--force` | Reprocess every file, even if already loaded |
| `--input-dir`, `--db-path`, `--output-dir` | Override paths from `config.yaml` |

Exit codes: `0` = OK, `1` = run failed, `2` = reconciliation failed.

---

## 🔄 How it works

```mermaid
flowchart LR
    A[📁 CSV files] --> B[📥 Ingest]
    B --> C[🔍 Validate]
    C -->|good rows| D[(🗄️ stg_transactions)]
    C -->|bad rows| E[(🚫 rejected_records)]
    D --> F[🧮 Features]
    D --> G[🏷️ Labels]
    F --> H[(customer_features)]
    G --> I[(churn_labels)]
    H & I --> J[📊 training_set]
    D & H & E --> K[✅ Reconciliation report]
```

1. **📥 Ingest**: read `customers.csv` and each weekly `transactions_*.csv` as text, with line numbers.
2. **🔍 Validate**: check every row. Good rows are saved, and bad rows go to the reject table with a reason.
3. **🧮 Transform**: compute features for each customer at each snapshot date (`as_of_date`).
4. **🏷️ Labels**: did the customer stop buying in the next 30 days? (stored separately)
5. **📤 Publish and verify**: save the tables, run the reconciliation checks, write the reports and the run log.

---

## 🔑 Key rules

### ⏱️ Point-in-time (no target leakage)
For a snapshot date **D**, a transaction is used only if:
- ✅ it **happened** before D (`txn_ts < D`), **and**
- ✅ it **arrived** before D (`ingested_at < D`). This blocks late data we didn't have yet.

Also: customers count only if `signup_date < D`. Labels look **forward** `[D, D+30 days)` and live in
a separate table, and a label is built only once the full 30 days of data exist.

### 🔁 Training = inference
All features are defined in **one SQL query** ([src/features.py](src/features.py)).
Training and inference call the same function, and a test checks they return identical values.

### ♻️ Safe reruns (idempotent)
- Each file's **hash** is saved. If the file is unchanged on a rerun, it's skipped.
- A file is loaded as **all-or-nothing** (one DB transaction): its old rows are deleted, then the new rows are inserted.
- 💥 **Crash mid-run**: the failed file is rolled back, so you just rerun. The final result equals a clean run.
- ✏️ **Corrected file** (same name, new content): it replaces its old rows.
- 📅 **Backfill**: add dates to `as_of_dates` in `config.yaml` and rerun. Only those dates are rebuilt.

### 🚫 Reject and quarantine
- A bad row is saved to `rejected_records` with its **file, line number, reason and raw values**.
- A file with a **missing column** is quarantined as a whole (status `PARTIAL_SUCCESS`).
- Extra or reordered columns are OK, they are just logged.
- Duplicates: the **first one seen wins**, and later copies are rejected.

Checks: missing fields, bad dates and amounts, bad categories, unknown customer, transaction before signup,
arrival before the event happened, duplicates, malformed rows, schema change.
👉 Full list of reason codes: [data_dictionary.md](data_dictionary.md#reject-reason-codes)

---

## 🗄️ Output tables

Stored in `output/feature_store.duckdb` and also exported as CSV to `output/`.

| Table | One row = | Purpose |
|---|---|---|
| `customers` | a customer | valid customers |
| `stg_transactions` | a transaction | clean, deduplicated transactions |
| `rejected_records` | a bad row or file | 🚫 quarantine with the reason |
| `processed_files` | a file | hash and status, used for skipping on rerun |
| `customer_features` | customer + date | ⭐ **ML features** |
| `churn_labels` | customer + date | 🏷️ target (kept separate) |
| `training_set` (view) | customer + date | features + label |
| `pipeline_runs` | a run | 📈 run log |

**Features:** `tenure_days`, `purchase_count_7d/30d/90d`, `purchase_amount_30d`,
`avg_purchase_amount_30d`, `max_purchase_amount_90d`, `refund_count_90d`,
`distinct_categories_30d`, `days_since_last_purchase`.
👉 Column details: [data_dictionary.md](data_dictionary.md)

---

## 📈 Logging and monitoring

Every run adds a row to `pipeline_runs` with:
`run_id` · status (`SUCCESS` / `PARTIAL_SUCCESS` / `FAILED`) · start/end time · input / loaded / rejected
rows · files skipped / quarantined · time per stage · error message.

There is also a log file per run in `output/logs/`.

---

## 🧪 Sample data

`generate_data.py` creates **5,000 transactions** (fixed seed, so the same files every time) in **20 weekly files**:

| ✅ Normal | ⚠️ Edge cases | ❌ Deliberately bad |
|---|---|---|
| 4,785 valid rows | boundary amounts (`0.01`, `100000`) | 60 duplicates |
| out-of-order rows | transaction exactly at the snapshot time | blank fields, bad dates and amounts |
| ~5% late arrivals | transaction exactly at signup | unknown customers |
| | empty file | transactions before signup |
| | | malformed rows |
| | | 1 file with a changed schema |

The list of bad rows (the **answer key**) is kept separately in `data/labels/expected_rejects.csv`,
never inside the input files.

---

## ✅ Reconciliation (proof it's correct)

Each run writes `output/reconciliation_report.md`. The raw files are re-read independently and the results compared:

- 🔢 source rows = loaded + rejected
- 💰 source amount total = loaded total + rejected total
- 🔑 no duplicate keys in any table
- 🔗 every transaction has a valid customer
- 👥 one feature row per eligible customer per date
- ⏱️ feature totals match a separate point-in-time query
- 🎯 rejects match the answer key exactly

**Result on the sample: all 12 checks PASS** ✅ (199 expected rejects = 199 actual).

---

## 🧪 Tests

`pytest -v` runs **40 tests, all passing** ✅ in about 15 s. Evidence: [output/test_results.txt](output/test_results.txt).

They cover the happy path, empty and 0-byte files, malformed rows, nulls, 12 kinds of invalid values,
duplicates, unknown customers, schema change, reruns, crash and retry, a corrected file, late data,
**no leakage**, the snapshot boundary, signup rules, labels, and train = inference.
👉 Mapping of each test to its requirement: [docs/design_notes.md](docs/design_notes.md#test-coverage)

---

## ⚡ Performance

| Run | Rows | Time |
|---|---|---|
| First run | 5,000 | ~1.3 s |
| Rerun (all skipped) | 0 | ~0.2 s |
| Large run | 100,000 | ⏳ to be measured |

🐢 **Bottleneck:** loading transactions (~55 ms per file). For each file, every already-loaded ID
is pulled into Python for the duplicate check.
💡 **Optimization:** do that duplicate check inside DuckDB (anti-join) instead.

---

## 🏷️ Naming conventions

- `snake_case` for all tables and columns. Staging tables start with `stg_`.
- Timestamps are ISO-8601 UTC (`2026-02-25T10:00:00Z`). Dates are `YYYY-MM-DD`.
- `transaction_id` and `customer_id` come from the source. `reject_id` = hash of file + line (the same on every rerun).
- `run_id` = `run_<UTC time>_<random>`. Every row stores `run_id`, `source_file` and `source_line_number`.

---

## 📂 Project structure

```
├── config.yaml            ⚙️ settings (paths, dates, rules)
├── src/
│   ├── generate_data.py   🎲 sample data + answer key
│   ├── config.py          ⚙️ reads config.yaml
│   ├── schemas.py         📐 columns, reject codes, tables
│   ├── ingest.py          📥 reads CSV files
│   ├── validate.py        🔍 row checks
│   ├── features.py        🧮 features + labels (SQL)
│   ├── publish.py         📤 database writes
│   ├── reconcile.py       ✅ correctness checks
│   ├── run_pipeline.py    ▶️ main entry point
│   └── show_results.py    📊 sample queries
├── sql/sample_queries.sql
├── tests/                 🧪 40 tests
├── data/sample/           📁 input data
├── data/labels/           🎯 answer key
├── output/                📦 results, reports
├── data_dictionary.md     📖 tables + columns
└── docs/design_notes.md   🧠 design choices + trade-offs
```

---

## 📌 Assumptions

- Transactions are **events that never change**. A second copy is a duplicate, and corrections come as refunds.
- Amounts are positive and in a single currency.
- Files arrive in name order (weekly).
- `customers.csv` is a full snapshot. If it's missing or broken, the run stops.
- Snapshots are taken at midnight UTC.

## ⚠️ Limitations

- Customers have no change history (they are a snapshot).
- Rows from a source file that is deleted later are not removed.
- A very late arrival after a label is built could change that label.
- DuckDB allows only one writer, so don't run two pipelines at once.

## 🔭 Next steps

- Run and profile 100K rows, then apply the optimization above.
- Keep customer history (SCD-2).
- Export features as Parquet.
- Schedule runs (cron / Airflow) and alert on failures.
