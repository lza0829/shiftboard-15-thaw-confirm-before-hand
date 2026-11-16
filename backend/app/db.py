from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
FIXTURES_DIR = Path(
    os.environ.get("FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures")
)


def data_dir() -> Path:
    # 惰性解析：测试进程可在运行期切换 DATA_DIR
    return Path(os.environ.get("DATA_DIR", _DEFAULT_DATA_DIR))


def connect() -> sqlite3.Connection:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "app.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
