"""SQLite storage for the Acme ERP sandbox."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

DEFAULT_DB = Path(__file__).resolve().parent / "acme.db"

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
);
CREATE TABLE vendors (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    gl_code TEXT NOT NULL
);
CREATE TABLE purchase_orders (
    id INTEGER PRIMARY KEY,
    po_number TEXT UNIQUE NOT NULL,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    description TEXT NOT NULL,
    total_amount REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Open', 'Closed')),
    created_date TEXT NOT NULL
);
-- Deliberately no UNIQUE(vendor_id, invoice_number): like many legacy systems,
-- the ERP happily accepts duplicate bills. Catching them is the operator's job.
CREATE TABLE bills (
    id INTEGER PRIMARY KEY,
    bill_number TEXT UNIQUE NOT NULL,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    po_number TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    amount REAL NOT NULL,
    gl_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Entered',
    created_at TEXT NOT NULL
);
CREATE TABLE exceptions (
    id INTEGER PRIMARY KEY,
    exception_number TEXT UNIQUE NOT NULL,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    invoice_number TEXT NOT NULL,
    po_number TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    employee_id TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    department TEXT NOT NULL,
    title TEXT NOT NULL,
    manager_name TEXT,
    start_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE equipment (
    id INTEGER PRIMARY KEY,
    asset_tag TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    asset_value REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Available', 'Assigned', 'In Repair')),
    assigned_to_employee_id INTEGER REFERENCES employees(id)
);
CREATE TABLE access_requests (
    id INTEGER PRIMARY KEY,
    request_number TEXT UNIQUE NOT NULL,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    system_name TEXT NOT NULL,
    access_level TEXT NOT NULL CHECK (access_level IN ('standard', 'admin')),
    status TEXT NOT NULL DEFAULT 'Submitted',
    created_at TEXT NOT NULL
);
"""


def db_path() -> Path:
    # Read on every call so tests (and evals) can point at a throwaway DB via env.
    return Path(os.environ.get("ACME_DB", DEFAULT_DB))


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def next_number(conn: sqlite3.Connection, table: str, column: str, prefix: str) -> str:
    """Next human-readable id, e.g. BILL-0004."""
    rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
    highest = max((int(r[0].rsplit("-", 1)[1]) for r in rows), default=0)
    return f"{prefix}-{highest + 1:04d}"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def parse_money(text: str) -> float | None:
    """Accepts '1203.5', '$1,203.50' or ' 1,203.50 '; returns None if not a number."""
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None
