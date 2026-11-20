"""SQLite storage for uploaded boxes, ruling lines and human decisions.

Write helpers never commit on their own: the web layer groups each task
change with its search-index rebuild into one transaction and commits (or
rolls back) at the end, so the index can never drift from the task data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path, timeout=15.0):
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL + a busy timeout keep two gunicorn workers from tripping over each
    # other's short writes.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db(path):
    connection = connect(path)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def get_job(connection, job_id):
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def list_jobs(connection):
    rows = connection.execute(
        "SELECT j.*, (SELECT COUNT(*) FROM boxes b WHERE b.job_id = j.id) AS box_count,"
        " (SELECT COUNT(*) FROM lines l WHERE l.job_id = j.id) AS line_count"
        " FROM jobs j ORDER BY j.id DESC"
    ).fetchall()
    return _rows_to_dicts(rows)


def create_job(connection, filename):
    cursor = connection.execute(
        "INSERT INTO jobs (filename) VALUES (?)", (filename,)
    )
    return cursor.lastrowid


def delete_job(connection, job_id):
    # Boxes, lines, decisions and search entries cascade away together.
    connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def replace_box(connection, job_id, row):
    """Insert one OCR box; UNIQUE(job_id,row_no) makes re-upload idempotent."""
    connection.execute(
        "INSERT OR REPLACE INTO boxes"
        " (job_id, row_no, box_ref, text, x1, y1, x2, y2)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, row["row_no"], row["box_ref"], row["text"],
         row["x1"], row["y1"], row["x2"], row["y2"]),
    )


def add_boxes(connection, job_id, rows):
    for row in rows:
        replace_box(connection, job_id, row)


def get_boxes(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM boxes WHERE job_id = ? ORDER BY row_no", (job_id,)
    ).fetchall()
    return _rows_to_dicts(rows)


def set_lines(connection, job_id, horizontal, vertical):
    connection.execute("DELETE FROM lines WHERE job_id = ?", (job_id,))
    for position in horizontal:
        connection.execute(
            "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'h', ?)",
            (job_id, position),
        )
    for position in vertical:
        connection.execute(
            "INSERT INTO lines (job_id, orientation, position) VALUES (?, 'v', ?)",
            (job_id, position),
        )


def get_lines(connection, job_id):
    rows = connection.execute(
        "SELECT orientation, position FROM lines WHERE job_id = ?"
        " ORDER BY orientation, position", (job_id,)
    ).fetchall()
    horizontal = [row["position"] for row in rows if row["orientation"] == "h"]
    vertical = [row["position"] for row in rows if row["orientation"] == "v"]
    return horizontal, vertical


def get_decisions(connection, job_id):
    rows = connection.execute(
        "SELECT * FROM decisions WHERE job_id = ?", (job_id,)
    ).fetchall()
    decisions = {}
    for row in rows:
        record = dict(row)
        try:
            record["overlap_cells"] = json.loads(record["overlap_cells"] or "[]")
        except ValueError:
            record["overlap_cells"] = []
        decisions[record["box_id"]] = record
    return decisions


def set_decision(connection, job_id, box_id, kind, target_cell, overlap_cells):
    connection.execute(
        "INSERT INTO decisions"
        " (job_id, box_id, kind, target_cell, overlap_cells)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (job_id, box_id) DO UPDATE SET"
        " kind = excluded.kind, target_cell = excluded.target_cell,"
        " overlap_cells = excluded.overlap_cells,"
        " created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (job_id, box_id, kind, target_cell,
         json.dumps(sorted(overlap_cells), ensure_ascii=False)),
    )


def clear_decision(connection, job_id, box_id):
    connection.execute(
        "DELETE FROM decisions WHERE job_id = ? AND box_id = ?", (job_id, box_id)
    )


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------

def rebuild_search_index(connection, job_id, entries):
    """Replace one job's search index rows (the caller commits)."""
    connection.execute("DELETE FROM search_entries WHERE job_id = ?", (job_id,))
    for entry in entries:
        connection.execute(
            "INSERT INTO search_entries"
            " (job_id, entry_key, kind, row_idx, col_idx, column_key, status,"
            "  text, box_ids, spans)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, entry["entry_key"], entry["kind"], entry["row"],
             entry["col"], entry["column"], entry["status"], entry["text"],
             json.dumps(entry["box_ids"], ensure_ascii=False),
             json.dumps(entry["spans"], ensure_ascii=False)),
        )


def get_search_entries(connection, job_id=None):
    """Index rows (newest job last), decoded, with the job filename attached."""
    sql = ("SELECT s.*, j.filename FROM search_entries s"
           " JOIN jobs j ON j.id = s.job_id")
    params = ()
    if job_id is not None:
        sql += " WHERE s.job_id = ?"
        params = (job_id,)
    sql += " ORDER BY s.job_id, s.kind, s.row_idx, s.col_idx, s.entry_key"
    entries = []
    for row in connection.execute(sql, params).fetchall():
        record = dict(row)
        for field in ("box_ids", "spans"):
            try:
                record[field] = json.loads(record[field] or "[]")
            except ValueError:
                record[field] = []
        entries.append(record)
    return entries


def jobs_missing_index(connection):
    """Ids of jobs that have no search-index rows yet (e.g. pre-index data)."""
    rows = connection.execute(
        "SELECT id FROM jobs WHERE id NOT IN"
        " (SELECT DISTINCT job_id FROM search_entries)"
    ).fetchall()
    return [row[0] for row in rows]
