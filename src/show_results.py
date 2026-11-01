# Runs sql/sample_queries.sql and saves results to output/sample_query_results.md
# Usage: python -m src.show_results
from __future__ import annotations

import argparse
import re
from pathlib import Path

import duckdb

from src.config import PROJECT_ROOT, load_config

QUERIES = PROJECT_ROOT / "sql" / "sample_queries.sql"


def parse_queries(text: str) -> list[tuple[str, str]]:
    out = []
    for block in re.split(r"^-- name:", text, flags=re.MULTILINE)[1:]:
        name, _, sql = block.partition("\n")
        out.append((name.strip(), sql.strip().rstrip(";")))
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--db-path")
    a = p.parse_args(argv)
    cfg = load_config()
    db = Path(a.db_path) if a.db_path else cfg["paths"]["db_path"]
    con = duckdb.connect(str(db), read_only=True)

    md = ["# Sample query results", ""]
    for name, sql in parse_queries(QUERIES.read_text(encoding="utf-8")):
        df = con.execute(sql).df()
        print(f"\n== {name}\n{df.to_string(index=False)}")
        md += [f"## {name}", "", "```sql", sql, "```", "", "```", df.to_string(index=False), "```", ""]
    con.close()
    out = cfg["paths"]["output_dir"] / "sample_query_results.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
