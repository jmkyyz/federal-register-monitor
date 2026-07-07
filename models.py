"""Database setup for the Federal Register Monitor.

Raw sqlite3 — single file, no server, same pattern as the Gazette monitor.
init_db() is idempotent.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_number     TEXT PRIMARY KEY,  -- FR's own ID, used for dedup
    title               TEXT,
    document_type       TEXT,              -- RULE / PRORULE / NOTICE / PRESDOCU
    agencies            TEXT,              -- JSON array of agency names
    abstract            TEXT,
    publication_date    TEXT,              -- official pub date (null pre-publication)
    scheduled_pub_date  TEXT,              -- from Public Inspection listing
    significant         BOOLEAN,           -- EO 12866 significance flag, if present
    filing_type         TEXT,              -- PI only: 'regular' or 'special' (emergency)
    html_url            TEXT,
    pdf_url             TEXT,
    source_stage        TEXT,              -- 'public_inspection' or 'published'
    matched_terms       TEXT,              -- JSON array of watchlist terms/agencies that hit
    is_watchlist_hit    BOOLEAN DEFAULT 0,
    is_urgent           BOOLEAN DEFAULT 0,
    first_seen_at       TEXT,
    notified_at         TEXT               -- null until Cowork has sent a notification
);

CREATE INDEX IF NOT EXISTS idx_documents_pending
    ON documents (is_watchlist_hit, is_urgent, notified_at);
CREATE INDEX IF NOT EXISTS idx_documents_pub_date
    ON documents (publication_date);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
