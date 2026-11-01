import hashlib

from src.generate_data import generate
from tests.conftest import make_env


def _hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.csv"))}


def test_same_seed_same_files(tmp_path):
    generate(800, 7, tmp_path / "a", tmp_path / "a_labels")
    generate(800, 7, tmp_path / "b", tmp_path / "b_labels")
    generate(800, 8, tmp_path / "c", tmp_path / "c_labels")
    assert _hashes(tmp_path / "a") == _hashes(tmp_path / "b")
    assert _hashes(tmp_path / "a_labels") == _hashes(tmp_path / "b_labels")
    assert _hashes(tmp_path / "a") != _hashes(tmp_path / "c")


def test_end_to_end_on_generated_data_matches_answer_key(tmp_path):
    env = make_env(tmp_path, as_of_dates=["2026-02-01", "2026-03-01", "2026-04-01"])
    manifest = generate(1500, 42, env.input, tmp_path / "labels")
    env.cfg["paths"]["expected_rejects"] = tmp_path / "labels" / "expected_rejects.csv"

    m = env.run()
    assert m["status"] == "PARTIAL_SUCCESS"            # the schema-drift file is quarantined
    assert m["reconciliation_status"] == "PASS"        # includes the answer-key comparison
    assert m["input_rows"] == manifest["transaction_rows_written"]
    assert m["loaded_rows"] == manifest["valid_transactions"]

    m2 = env.run()                                     # rerun: nothing new, nothing duplicated
    assert m2["input_rows"] == 0 and m2["reconciliation_status"] == "PASS"
    assert env.scalar("SELECT COUNT(*) FROM stg_transactions") == manifest["valid_transactions"]
