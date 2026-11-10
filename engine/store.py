"""SQLite persistence over a caller-supplied connection (Hermes' plugin_db() in production,
a temporary file in tests). Only sanitised data is stored: no source path, masked accounts."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    account_masked TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    raw_label TEXT NOT NULL,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    resolved INTEGER NOT NULL,
    installment_hint INTEGER NOT NULL,
    category TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tx_identity ON transactions(date, amount, raw_label, currency);
"""
TABLES = ("transactions", "imports")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def find_import(conn: sqlite3.Connection, fingerprint: str) -> dict | None:
    row = conn.execute("SELECT id, profile, imported_at, summary_json FROM imports WHERE fingerprint = ?",
                       (fingerprint,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "profile": row[1], "imported_at": row[2], "summary": json.loads(row[3])}


def add_import(conn: sqlite3.Connection, fingerprint: str, profile: str, account: str,
               summary: dict, rows: list[dict]) -> tuple[int, int]:
    """Insert an import and its rows. Overlapping exports are merged: a (date, amount, label,
    currency) tuple seen k times in the file and j times in the store adds max(0, k - j) rows,
    so genuine same-day double charges inside one file survive. Returns (added, already_known)."""
    cur = conn.cursor()
    manual = conn.isolation_level is None and not conn.in_transaction   # autocommit connection
    try:
        if manual:
            cur.execute("BEGIN")
        cur.execute("INSERT INTO imports(fingerprint, profile, imported_at, account_masked, summary_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (fingerprint, profile, datetime.now(timezone.utc).isoformat(timespec="seconds"), account,
                     json.dumps(summary, ensure_ascii=False)))
        import_id = cur.lastrowid
        wanted = Counter((r["date"], r["amount"], r["raw_label"], r["currency"]) for r in rows)
        existing = {}
        for ident in wanted:
            existing[ident] = cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE date=? AND amount=? AND raw_label=? AND currency=?",
                ident).fetchone()[0]
        seen: Counter = Counter()
        added = known = 0
        for r in rows:
            ident = (r["date"], r["amount"], r["raw_label"], r["currency"])
            seen[ident] += 1
            if seen[ident] <= existing[ident]:
                known += 1
                continue
            cur.execute("INSERT INTO transactions(import_id, date, amount, currency, raw_label, key, label, kind, "
                        "resolved, installment_hint, category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (import_id, r["date"], r["amount"], r["currency"], r["raw_label"], r["key"], r["label"],
                         r["kind"], int(r["resolved"]), int(r["installment_hint"]), r.get("category", "")))
            added += 1
        conn.commit()
        return added, known
    except Exception:
        conn.rollback()
        raise


def transactions(conn: sqlite3.Connection) -> list[dict]:
    cols = ["id", "date", "amount", "currency", "raw_label", "key", "label", "kind", "resolved", "installment_hint"]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM transactions ORDER BY date, id").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def last_import_summary(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT summary_json FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def counts(conn: sqlite3.Connection) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def purge(conn: sqlite3.Connection) -> dict:
    before = counts(conn)
    for t in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.execute("VACUUM")
    return before
