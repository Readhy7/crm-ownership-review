"""The review queue: a small SQLite database of proposals and decisions.

Every proposal has a fingerprint (change type + target + new values). A fingerprint is
only ever inserted once, so re-running the pipeline never re-proposes something that was
already approved, rejected, or is still waiting for review.
"""
import datetime as dt
import hashlib
import json
import sqlite3

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT UNIQUE NOT NULL,
    run_id       INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    facility     TEXT NOT NULL,      -- grouping label shown in the app
    kind         TEXT NOT NULL,      -- rename, reparent, chow_new_account, mark_duplicate, ...
    priority     INTEGER NOT NULL,   -- 1 = ownership, 2 = data fix, 3 = contact info
    confidence   TEXT NOT NULL,      -- high / medium / low
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,      -- plain-English explanation
    target_id    TEXT,               -- account the change applies to (if any)
    action       TEXT NOT NULL,      -- JSON: exactly what will be written on approval
    before       TEXT NOT NULL,      -- JSON: CRM values when proposed (checked again before writing)
    evidence     TEXT NOT NULL,      -- JSON list of strings
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending, applied, rejected, failed, stale
    decided_at   TEXT,
    reviewer_note TEXT,
    result       TEXT                -- JSON: what the API returned / ids created
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(proposals)")}
    if "origin" not in cols:  # id of the approval that triggered a follow-up proposal
        conn.execute("ALTER TABLE proposals ADD COLUMN origin INTEGER")
        conn.commit()
    return conn


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _without_notes(value):
    """Notes carry today's date; leaving them out keeps a rejected item from looking new tomorrow."""
    if isinstance(value, dict):
        return {k: _without_notes(v) for k, v in value.items() if k != "note"}
    if isinstance(value, list):
        return [_without_notes(v) for v in value]
    return value


def fingerprint(kind, target, action):
    raw = json.dumps({"kind": kind, "target": target, "action": _without_notes(action)}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def start_run(conn):
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, summary):
    conn.execute("UPDATE runs SET summary = ? WHERE run_id = ?", (json.dumps(summary), run_id))
    conn.commit()


def add_proposals(conn, run_id, proposals, origin=None):
    """Insert proposals whose fingerprint is new. Returns (new_ids, skipped_already_known)."""
    new_ids, skipped = [], 0
    for p in proposals:
        fp = fingerprint(p["kind"], p.get("target_id") or p["facility"], p["action"])
        exists = conn.execute("SELECT 1 FROM proposals WHERE fingerprint = ?", (fp,)).fetchone()
        if exists:
            skipped += 1
            continue
        cur = conn.execute(
            """INSERT INTO proposals (fingerprint, run_id, created_at, facility, kind, priority,
               confidence, title, summary, target_id, action, before, evidence, origin)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, run_id, now(), p["facility"], p["kind"], p["priority"], p["confidence"],
             p["title"], p["summary"], p.get("target_id"), json.dumps(p["action"]),
             json.dumps(p.get("before", {})), json.dumps(p["evidence"]), origin),
        )
        new_ids.append(cur.lastrowid)
    conn.commit()
    return new_ids, skipped


def get(conn, proposal_id):
    return conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()


def list_proposals(conn, status=None):
    sql = "SELECT * FROM proposals"
    args = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY facility, priority, id"
    return conn.execute(sql, args).fetchall()


def set_status(conn, proposal_id, status, reviewer_note="", result=None):
    conn.execute(
        "UPDATE proposals SET status = ?, decided_at = ?, reviewer_note = ?, result = ? WHERE id = ?",
        (status, now(), reviewer_note, json.dumps(result) if result is not None else None, proposal_id),
    )
    conn.commit()


def save_partial_result(conn, proposal_id, result):
    """Record progress mid-apply (e.g. a created account id) so a retry never creates twice."""
    conn.execute("UPDATE proposals SET result = ? WHERE id = ?", (json.dumps(result), proposal_id))
    conn.commit()
