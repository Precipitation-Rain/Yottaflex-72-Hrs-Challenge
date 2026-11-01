from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class RawFile:
    source_file: str
    file_hash: str
    header: list[str]
    rows: pd.DataFrame        # good rows, all values as strings
    malformed: list[dict] = field(default_factory=list)

    @property
    def source_rows(self) -> int:
        return len(self.rows) + len(self.malformed)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_transaction_files(input_dir: Path, pattern: str) -> list[Path]:
    # sorted, so files are always processed in the same order
    return sorted(Path(input_dir).glob(pattern))


def read_csv_file(path: Path) -> RawFile:
    # Using csv module instead of pd.read_csv to keep line numbers
    # and catch rows with the wrong number of fields.
    path = Path(path)
    header: list[str] = []
    good: list[list[str]] = []
    lines: list[int] = []
    malformed: list[dict] = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not header:
                if not row:
                    continue
                header = [c.strip() for c in row]
                continue
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) != len(header):
                malformed.append({
                    "line": reader.line_num,
                    "raw": row,
                    "detail": f"expected {len(header)} fields, got {len(row)}",
                })
                continue
            good.append([c.strip() for c in row])
            lines.append(reader.line_num)

    df = pd.DataFrame(good, columns=header, dtype="string") if header else pd.DataFrame()
    df["source_line_number"] = pd.Series(lines, dtype="int64")
    return RawFile(
        source_file=path.name,
        file_hash=file_sha256(path),
        header=header,
        rows=df,
        malformed=malformed,
    )


def check_schema(header: list[str], expected: list[str]) -> tuple[list[str], list[str]]:
    missing = [c for c in expected if c not in header]
    extra = [c for c in header if c not in expected]
    return missing, extra
