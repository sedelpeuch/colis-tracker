from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "colis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_code TEXT UNIQUE NOT NULL,
    label TEXT,
    carrier TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    raw_status TEXT,
    sender TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    entry_date TEXT,
    locomotion_mode TEXT,
    estimated_delivery TEXT,
    url TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    last_polled_at TEXT,
    next_poll_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    raw_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Colonnes ajoutées après coup : CREATE TABLE IF NOT EXISTS ne les crée pas sur une base existante.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(packages)")}
        if "estimated_delivery" not in columns:
            conn.execute("ALTER TABLE packages ADD COLUMN estimated_delivery TEXT")
        # delivered_at est resté vide tant que deliveryDate (chaîne ISO) était lu comme un epoch :
        # on le reconstitue depuis le dernier événement « livré » de l'historique.
        conn.execute(
            """
            UPDATE packages SET delivered_at = (
                SELECT MAX(created_at) FROM events
                WHERE events.package_id = packages.id AND events.status = 'delivered'
            )
            WHERE delivered = 1 AND delivered_at IS NULL
            """
        )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
