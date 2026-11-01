# Design notes & trade-offs

## 1. DuckDB + pandas instead of Spark / Postgres
**Choice:** an embedded DuckDB file plus pandas for validation.
**Why:** it runs anywhere with `pip install`, needs no server or credentials, and has real SQL
with window filters. It easily handles 100K+ rows. Point-in-time logic is clearer in SQL than in loops.
**Trade-off:** one writer at a time, single machine. The SQL is close to standard, so it would
port to Postgres, Snowflake or Spark SQL with small changes.

## 2. Two timestamps: `txn_ts` (event) and `ingested_at` (knowledge)
Using only `txn_ts < D` is not enough. A Jan 31 purchase delivered on Feb 3 would appear in a
recomputed Feb 1 snapshot, but at real inference time on Feb 1 we didn't have it. That is a
subtle form of leakage (training data "knows more" than production). Filtering on `ingested_at < D`
as well makes historical snapshots match what production would have seen, and they stay
**reproducible** even after late data arrives.

## 3. One feature definition
Features exist only in `FEATURE_SQL` ([src/features.py](../src/features.py)). Training builds many
snapshots and inference builds one, but both call `build_features()`. This avoids training/serving
skew, which is a common real-world bug where two code paths drift apart.

## 4. Labels separate from features
Labels are a different table built by a different query that looks *forward*. The feature query
cannot reference it. Immature labels (future window not fully observed) are skipped and
logged, not silently computed as "churned".

## 5. Idempotency by "replace what this file produced"
Options considered:
- *Full rebuild every run*: simplest, but slow for big history and has no restart semantics.
- *Insert-only with a dedup at the end*: easy to get wrong on partial failures.
- **Chosen: per-file atomic replace + file hash registry.** In one DB transaction we delete
  everything that came from a file (staging rows, rejects, file status) and insert the new
  result. Reruns skip unchanged files. A crash rolls back only the current file, and a corrected file
  (new hash) replaces its old version.

## 6. Duplicates: "first seen wins"
Transactions are immutable events, so a second copy is a re-send, not an update. Keeping the
first copy (by file order, then line) is deterministic and matches append-only event semantics.
Duplicate checks only compare otherwise-valid rows, so a broken first copy doesn't block a good later
copy. If the producer sent *updates*, we would instead keep the latest version and store the
history (SCD-2). That's documented as a next step.

## 7. Reject, don't crash: and quarantine whole files on schema change
Bad rows go to `rejected_records` with reason, line and raw values, and the rest of the file
loads. A **missing** column makes every row in the file untrustworthy, so the whole file is
quarantined, and the run is `PARTIAL_SUCCESS` until a fixed file arrives. **Extra** or reordered
columns are tolerated (logged), because they don't change the meaning of the known columns.
A broken `customers.csv` fails the run, because foreign keys can't be checked without it.

## 8. Keys enforced by logic, verified by checks (not DB constraints)
DuckDB constraints combined with delete-then-reinsert of the same key inside one transaction
have historically been fragile. So uniqueness is guaranteed by the pipeline (dedup +
replace) and **verified on every run** by reconciliation, and a test proves reconciliation fails
when a duplicate is injected. Trade-off: a bug would be detected after the write, not
prevented at write time.

## 9. Independent reconciliation
Reconciliation re-reads the raw CSVs with the plain `csv` module (not the pipeline's ingest code)
and recomputes the feature totals with a separate SQL query. If the pipeline had a bug, the
two sides would disagree. The generator's answer key adds a third, external check.

## 10. Row-level validation in pandas (vectorized)
Each rule is a boolean mask over the whole file, and the first failing rule wins. That makes
it fast and deterministic, and each rule is easy to read in isolation.

## Limitations to be aware of
See README §14: customers snapshot (no history), deleted source files are not removed, label
maturity based on `max(txn_ts)`, single writer.
