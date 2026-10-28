"""SQLite schema, seed data, and queries. Created and seeded automatically on first run.

Each call opens its own connection, so the CLI and the API (Phase 3, threads) can share the file.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from invoice_agent import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item       TEXT PRIMARY KEY,
    stock      INTEGER NOT NULL,
    unit_price REAL  -- catalog price Acme agreed to; NULL when there is none
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file     TEXT NOT NULL,
    source_text     TEXT,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL,
    invoice_number  TEXT,     -- normalized, e.g. INV-1012
    vendor          TEXT,     -- as extracted
    invoice_key     TEXT,     -- normalized number + vendor
    fingerprint     TEXT,
    total_cents     INTEGER,
    currency        TEXT,
    extraction_json TEXT,
    duplicate_json  TEXT,
    validation_json TEXT,
    policy_json     TEXT,
    decision_json   TEXT,
    payment_json    TEXT,
    usage_json      TEXT,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS runs_invoice_number ON runs (invoice_number);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs (id),
    ts      TEXT NOT NULL,
    stage   TEXT NOT NULL,
    type    TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id);

CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs (id),
    invoice_number  TEXT NOT NULL,
    invoice_key     TEXT NOT NULL,
    vendor          TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    approved_total_cents INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    paid_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_key ON payments (invoice_key);
"""

# Stock from the README seed; unit prices added for the price check (FakeItem has none).
INVENTORY_SEED = [
    ("WidgetA", 15, 250.0),
    ("WidgetB", 10, 500.0),
    ("GadgetX", 5, 750.0),
    ("FakeItem", 0, None),
]

RUN_COLUMNS = {
    "source_text", "status", "stage", "invoice_number", "vendor", "invoice_key", "fingerprint", "total_cents", "currency",
    "extraction_json", "duplicate_json", "validation_json", "policy_json", "decision_json", "payment_json",
    "usage_json", "error", "finished_at", "duration_ms",
}  # fmt: skip


def connect(path: Path | None = None) -> sqlite3.Connection:
    # WAL lets the CLI and the API use the same file at the same time.
    conn = sqlite3.connect(path or config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    with closing(connect(path)) as conn, conn:
        yield conn


def init_db(path: Path | None = None, reset: bool = False) -> None:
    """Create tables and seed inventory. Safe to call on every run."""
    path = path or config.DB_PATH
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    with transaction(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO inventory (item, stock, unit_price) VALUES (?, ?, ?)",
            INVENTORY_SEED,
        )


def load_inventory() -> dict[str, sqlite3.Row]:
    with transaction() as conn:
        return {row["item"]: row for row in conn.execute("SELECT item, stock, unit_price FROM inventory")}


def create_run(source_file: str, started_at: str) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO runs (source_file, status, stage, started_at) VALUES (?, 'processing', 'queued', ?)",
            (source_file, started_at),
        )
        return int(cur.lastrowid or 0)


def update_run(run_id: int, **fields: Any) -> None:
    unknown = set(fields) - RUN_COLUMNS
    if unknown:
        raise ValueError(f"unknown run columns: {unknown}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with transaction() as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))


def claim_review(run_id: int) -> bool:
    """Atomically move a needs_review run back to processing. False if someone else got there first."""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'processing' WHERE id = ? AND status = 'needs_review'", (run_id,)
        )
        return cur.rowcount == 1


def get_run(run_id: int) -> sqlite3.Row | None:
    with transaction() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_runs(status: str | None = None) -> list[sqlite3.Row]:
    """Newest first."""
    with transaction() as conn:
        if status is None:
            return conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        return conn.execute("SELECT * FROM runs WHERE status = ? ORDER BY id DESC", (status,)).fetchall()


def source_files() -> set[str]:
    with transaction() as conn:
        return {row[0] for row in conn.execute("SELECT DISTINCT source_file FROM runs")}


def finished_runs() -> list[sqlite3.Row]:
    """Oldest first, with just what the overview routes on: status, policy mode, and whether a human reviewed it."""
    with transaction() as conn:
        return conn.execute(
            "SELECT id, source_file, status, json_extract(policy_json, '$.mode') AS policy_mode, "
            "json_extract(decision_json, '$.review') IS NOT NULL AS reviewed "
            "FROM runs WHERE status != 'processing' ORDER BY id"
        ).fetchall()


def findings() -> list[sqlite3.Row]:
    """(run_id, code, severity) for every validation finding."""
    with transaction() as conn:
        return conn.execute(
            "SELECT runs.id AS run_id, json_extract(f.value, '$.code') AS code, "
            "json_extract(f.value, '$.severity') AS severity "
            "FROM runs, json_each(runs.validation_json, '$.findings') AS f ORDER BY runs.id, f.key"
        ).fetchall()


def earlier_runs(invoice_number: str, before_run_id: int) -> list[sqlite3.Row]:
    """Earlier runs with the same normalized number, newest first. Failed and duplicate runs don't count."""
    with transaction() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE invoice_number = ? AND id < ? AND status NOT IN ('failed', 'duplicate') "
            "ORDER BY id DESC",
            (invoice_number, before_run_id),
        ).fetchall()


def runs_for_number(invoice_number: str) -> list[sqlite3.Row]:
    with transaction() as conn:
        return conn.execute(
            "SELECT id, source_file, status, vendor, total_cents, currency FROM runs WHERE invoice_number = ? ORDER BY id",
            (invoice_number,),
        ).fetchall()


def insert_event(run_id: int, ts: str, stage: str, type_: str, message: str, payload: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO events (run_id, ts, stage, type, message, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, ts, stage, type_, message, payload),
        )


def list_events(run_id: int) -> list[sqlite3.Row]:
    with transaction() as conn:
        return conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


def paid_cents(invoice_key: str) -> int:
    with transaction() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE invoice_key = ?", (invoice_key,)
        ).fetchone()
        return int(row[0])


def payment_for_total(invoice_key: str, approved_total_cents: int) -> sqlite3.Row | None:
    """The payment that already brought this invoice key up to this approved total, if any."""
    with transaction() as conn:
        return conn.execute(
            "SELECT * FROM payments WHERE invoice_key = ? AND approved_total_cents = ? ORDER BY id LIMIT 1",
            (invoice_key, approved_total_cents),
        ).fetchone()


def insert_payment(
    *,
    run_id: int,
    invoice_number: str,
    invoice_key: str,
    vendor: str,
    amount_cents: int,
    currency: str,
    approved_total_cents: int,
    idempotency_key: str,
    paid_at: str,
) -> int | None:
    """None if the idempotency key was already used."""
    try:
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO payments (run_id, invoice_number, invoice_key, vendor, amount_cents, currency, "
                "approved_total_cents, idempotency_key, paid_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, invoice_number, invoice_key, vendor, amount_cents, currency, approved_total_cents,
                 idempotency_key, paid_at),
            )  # fmt: skip
            return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        return None


def get_payment_by_key(idempotency_key: str) -> sqlite3.Row | None:
    with transaction() as conn:
        return conn.execute("SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
