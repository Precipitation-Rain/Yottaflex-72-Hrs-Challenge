from datetime import date

import pandas as pd
import pytest

from tests.conftest import make_env, txn

C1_HISTORY = [
    txn("T1", "C1", "2026-02-25T10:00:00Z", amount="100.00"),                     # 7d
    txn("T2", "C1", "2026-02-10T10:00:00Z", amount="50.00", cat="travel"),         # 30d
    txn("T3", "C1", "2026-01-15T10:00:00Z", amount="30.00"),                       # 90d only
    txn("T4", "C1", "2026-02-20T10:00:00Z", amount="20.00", txn_type="refund"),
    txn("T5", "C1", "2026-03-05T10:00:00Z", amount="999.00"),                      # FUTURE
    txn("T6", "C2", "2026-04-05T10:00:00Z", amount="10.00"),                       # FUTURE
]


def features(env, as_of="2026-03-01"):
    df = env.query(f"SELECT * FROM customer_features WHERE as_of_date = DATE '{as_of}' "
                   "ORDER BY customer_id")
    return df.set_index("customer_id")


def test_feature_values_are_exact(env):
    env.txns("transactions_01.csv", C1_HISTORY)
    env.run()
    c1 = features(env).loc["C1"]
    assert c1["tenure_days"] == (date(2026, 3, 1) - date(2025, 6, 1)).days
    assert (c1["purchase_count_7d"], c1["purchase_count_30d"], c1["purchase_count_90d"]) == (1, 2, 3)
    assert c1["purchase_amount_30d"] == 150
    assert c1["avg_purchase_amount_30d"] == 75
    assert c1["max_purchase_amount_90d"] == 100      # the 999 future purchase is NOT visible
    assert c1["refund_count_90d"] == 1
    assert c1["distinct_categories_30d"] == 2
    assert c1["days_since_last_purchase"] == 4

    c2 = features(env).loc["C2"]                       # customer with no history yet
    assert c2["purchase_count_90d"] == 0 and c2["purchase_amount_30d"] == 0
    assert pd.isna(c2["avg_purchase_amount_30d"]) and pd.isna(c2["days_since_last_purchase"])


def test_no_target_leakage_future_data_does_not_change_features(env):
    env.txns("transactions_01.csv", C1_HISTORY)
    env.run()
    before = features(env).drop(columns=["run_id", "computed_at"])

    # a big batch of future activity, incl. a txn exactly AT the snapshot boundary
    env.txns("transactions_02.csv", [
        txn("F1", "C1", "2026-03-01T00:00:00Z", amount="5000.00"),
        txn("F2", "C2", "2026-03-02T10:00:00Z", amount="5000.00"),
        txn("F3", "C1", "2026-04-20T10:00:00Z", amount="5000.00", cat="electronics"),
    ])
    env.run()
    after = features(env).drop(columns=["run_id", "computed_at"])
    pd.testing.assert_frame_equal(before, after)


def test_late_arriving_txn_is_invisible_until_it_was_ingested(tmp_path):
    env = make_env(tmp_path, as_of_dates=["2026-03-01", "2026-04-01"])
    env.customers()
    env.txns("transactions_01.csv", [
        # happened Feb 27, but delivered Mar 3 -> unknown on Mar 1
        txn("L1", "C1", "2026-02-27T10:00:00Z", ingested="2026-03-03T08:00:00Z"),
        # delivered exactly at the snapshot -> also unknown (strict <)
        txn("L2", "C1", "2026-02-28T23:59:59Z", ingested="2026-03-01T00:00:00Z"),
    ])
    env.run()
    assert features(env, "2026-03-01").loc["C1", "purchase_count_30d"] == 0
    assert features(env, "2026-04-01").loc["C1", "purchase_count_90d"] == 2
    assert env.scalar("SELECT COUNT(*) FROM stg_transactions WHERE is_late") == 1


def test_txn_exactly_at_as_of_is_excluded(env):
    env.txns("transactions_01.csv", [
        txn("B1", "C1", "2026-03-01T00:00:00Z"),
        txn("B2", "C1", "2026-02-28T23:59:59Z", ingested="2026-02-28T23:59:59Z"),
    ])
    env.run()
    assert features(env).loc["C1", "purchase_count_7d"] == 1    # only B2


def test_signup_rules(tmp_path):
    env = make_env(tmp_path, as_of_dates=["2026-02-01", "2026-03-01"])
    env.customers()                                             # C3 signs up 2026-02-15
    env.txns("transactions_01.csv", [
        txn("S1", "C3", "2026-02-14T23:00:00Z"),   # before signup -> rejected
        txn("S2", "C3", "2026-02-15T00:00:00Z"),   # exactly at signup -> accepted
    ])
    env.run()
    assert list(env.rejects()["reject_reason"]) == ["txn_before_signup"]
    assert "C3" not in features(env, "2026-02-01").index        # not a customer yet
    assert features(env, "2026-03-01").loc["C3", "purchase_count_30d"] == 1


def test_labels_look_forward_and_skip_immature_windows(tmp_path):
    env = make_env(tmp_path, as_of_dates=["2026-03-01", "2026-04-01"])
    env.customers()
    env.txns("transactions_01.csv", C1_HISTORY)    # latest txn_ts = 2026-04-05
    m = env.run()
    labels = env.query("SELECT * FROM churn_labels").set_index("customer_id")
    # 2026-04-01 + 30d = 2026-05-01 > 2026-04-05: not observable yet -> no labels
    assert set(labels["as_of_date"].astype(str)) == {"2026-03-01"}
    assert labels.loc["C1", "churn_label"] == 0      # bought on 03-05
    assert labels.loc["C2", "churn_label"] == 1      # next purchase 04-05 is outside the window
    assert labels.loc["C3", "churn_label"] == 1      # signed up 02-15, never bought
    assert m["label_rows"] == 3
    ts = env.query("SELECT * FROM training_set")
    assert len(ts) == 3 and "churn_label" in ts.columns


def test_training_and_inference_produce_identical_features(tmp_path):
    train = make_env(tmp_path / "train", as_of_dates=["2026-03-01"])
    infer = make_env(tmp_path / "infer")
    for e in (train, infer):
        e.customers()
        e.txns("transactions_01.csv", C1_HISTORY)
    train.run(mode="train")
    infer.run(mode="inference", as_of="2026-03-01")

    a = features(train).drop(columns=["run_id", "computed_at"])
    b = features(infer).drop(columns=["run_id", "computed_at"])
    pd.testing.assert_frame_equal(a, b)
    assert (infer.cfg["paths"]["output_dir"] / "inference_features_2026-03-01.csv").exists()
    assert infer.scalar("SELECT COUNT(*) FROM churn_labels") == 0   # no labels at inference


def test_inference_requires_as_of(env):
    with pytest.raises(ValueError):
        env.run(mode="inference")
