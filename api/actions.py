"""
actions.py — Action layer for the BASt Traffic Demo.

Provides:
  - Evidence store: server-issued references binding agent proposals to
    the exact get_object result and provenance the server produced
  - Action proposals with payload-aware idempotency
  - Append-only status history with atomic, conditional transitions
  - Approval authority kept outside the agent: resolved_by is caller-supplied
    attribution from an authenticated human request, not an agent parameter

Database: SQLite (ephemeral on ECS; file-based locally).
Tables are created on first init_db() call.

Design invariants:
  - evidence rows are INSERT-only after creation
  - action proposal content (everything except status) is INSERT-only
  - action_status_log is INSERT-only; the application never UPDATE or DELETE it
  - actions.status is a read-cache updated only by resolve_action()
  - Concurrent resolutions: the conditional UPDATE (WHERE status='pending')
    produces exactly one winner; the loser gets a 409

Limitations acknowledged:
  - resolved_by is attribution, not verified identity (no auth layer in demo)
  - evidence has no TTL; stale evidence_ids remain valid across the container
    lifetime (containers reset on redeploy, so window is bounded)
  - SQLite WAL mode enabled to reduce write contention under concurrent reads
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


VALID_TYPES    = {"MAINTENANCE_RECOMMENDATION", "ANOMALY_FLAG", "REPORT_DRAFT"}
VALID_STATUSES = {"pending", "approved", "rejected"}
TERMINAL_STATUSES = {"approved", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_hash(action_type: str, title: str, description: str, evidence_id: str) -> str:
    """SHA-256 of the immutable proposal payload fields — used for conflict detection."""
    blob = json.dumps(
        {"type": action_type, "title": title, "description": description, "evidence_id": evidence_id},
        sort_keys=True,
    ).encode()
    return hashlib.sha256(blob).hexdigest()


# ── DB setup ───────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection.

    Enables WAL mode and enforces foreign keys. Safe to call multiple times
    on the same path — CREATE TABLE IF NOT EXISTS is idempotent.
    """
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")

    con.executescript("""
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id    TEXT PRIMARY KEY,
            object_type    TEXT NOT NULL,
            object_id      TEXT NOT NULL,
            snapshot_id    TEXT,
            metric_version TEXT,
            result_json    TEXT NOT NULL,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS actions (
            id              TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            type            TEXT NOT NULL
                                CHECK(type IN (
                                    'MAINTENANCE_RECOMMENDATION',
                                    'ANOMALY_FLAG',
                                    'REPORT_DRAFT'
                                )),
            status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'approved', 'rejected')),
            title           TEXT NOT NULL,
            description     TEXT NOT NULL,
            object_type     TEXT NOT NULL,
            object_id       TEXT NOT NULL,
            evidence_id     TEXT NOT NULL REFERENCES evidence(evidence_id),
            snapshot_id     TEXT,
            metric_version  TEXT,
            evidence_json   TEXT NOT NULL,
            payload_hash    TEXT NOT NULL,
            proposed_at     TEXT NOT NULL,
            proposed_by     TEXT NOT NULL DEFAULT 'agent'
        );

        CREATE TABLE IF NOT EXISTS action_status_log (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id  TEXT NOT NULL REFERENCES actions(id),
            status     TEXT NOT NULL
                           CHECK(status IN ('pending', 'approved', 'rejected')),
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_actions_status      ON actions(status);
        CREATE INDEX IF NOT EXISTS idx_log_action_id       ON action_status_log(action_id);
    """)
    con.commit()
    return con


# ── Evidence ───────────────────────────────────────────────────────────────────

def store_evidence(con: sqlite3.Connection, object_type: str, object_id: str, result: dict) -> str:
    """Persist a get_object result and return a server-issued evidence_id."""
    evidence_id    = str(uuid.uuid4())
    provenance     = result.get("provenance", {})
    snapshot_id    = provenance.get("snapshot_id")
    metric_version = provenance.get("metric_version")

    con.execute(
        """
        INSERT INTO evidence
            (evidence_id, object_type, object_id, snapshot_id, metric_version, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [evidence_id, object_type, object_id,
         snapshot_id, metric_version, json.dumps(result), _now()],
    )
    con.commit()
    return evidence_id


def get_evidence(con: sqlite3.Connection, evidence_id: str) -> dict | None:
    """Return the stored evidence row as a dict, or None if not found."""
    row = con.execute(
        "SELECT evidence_id, object_type, object_id, snapshot_id, metric_version, result_json, created_at "
        "FROM evidence WHERE evidence_id = ?",
        [evidence_id],
    ).fetchone()
    if row is None:
        return None
    cols = ["evidence_id", "object_type", "object_id", "snapshot_id", "metric_version", "result_json", "created_at"]
    return dict(zip(cols, row))


# ── Actions ────────────────────────────────────────────────────────────────────

def _row_to_action(row: tuple, cols: list[str]) -> dict:
    d = dict(zip(cols, row))
    if d.get("evidence_json"):
        try:
            d["evidence_json"] = json.loads(d["evidence_json"])
        except Exception:
            pass
    return d


def create_action(
    con: sqlite3.Connection,
    idempotency_key: str,
    action_type: str,
    title: str,
    description: str,
    evidence_id: str,
    proposed_by: str = "agent",
) -> tuple[dict, str]:
    """Create an action proposal, or return an existing one if the key matches.

    Returns (action_dict, outcome) where outcome is one of:
      "created"   — new action inserted
      "duplicate" — same key, same payload → existing action returned
      "conflict"  — same key, different payload → caller must raise 409

    Raises ValueError if action_type is invalid.
    Raises LookupError if evidence_id does not exist.

    Proposal content and the initial 'pending' log entry are written in one
    transaction; if either fails, neither is committed.
    """
    if action_type not in VALID_TYPES:
        raise ValueError(f"Invalid action type '{action_type}'. Valid: {sorted(VALID_TYPES)}")

    # Resolve evidence server-side — agent cannot supply or alter these values
    ev = get_evidence(con, evidence_id)
    if ev is None:
        raise LookupError(f"evidence_id '{evidence_id}' not found. Call get_object first.")

    phash = _payload_hash(action_type, title, description, evidence_id)

    # Check idempotency key
    existing = con.execute(
        "SELECT id, payload_hash FROM actions WHERE idempotency_key = ?",
        [idempotency_key],
    ).fetchone()

    if existing is not None:
        existing_id, existing_hash = existing
        if existing_hash == phash:
            return _get_action(con, existing_id), "duplicate"
        else:
            return _get_action(con, existing_id), "conflict"

    # Create new action + initial log entry atomically
    action_id = str(uuid.uuid4())
    now       = _now()

    with con:
        con.execute(
            """
            INSERT INTO actions
                (id, idempotency_key, type, status, title, description,
                 object_type, object_id, evidence_id, snapshot_id, metric_version,
                 evidence_json, payload_hash, proposed_at, proposed_by)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                action_id, idempotency_key, action_type, title, description,
                ev["object_type"], ev["object_id"], evidence_id,
                ev["snapshot_id"], ev["metric_version"],
                ev["result_json"],  # evidence_json = stored result, not agent-supplied
                phash, now, proposed_by,
            ],
        )
        con.execute(
            "INSERT INTO action_status_log (action_id, status, changed_at, changed_by) VALUES (?, 'pending', ?, ?)",
            [action_id, now, proposed_by],
        )

    return _get_action(con, action_id), "created"


def _get_action(con: sqlite3.Connection, action_id: str) -> dict | None:
    cols = [
        "id", "idempotency_key", "type", "status", "title", "description",
        "object_type", "object_id", "evidence_id", "snapshot_id", "metric_version",
        "evidence_json", "proposed_at", "proposed_by",
    ]
    row = con.execute(
        f"SELECT {', '.join(cols)} FROM actions WHERE id = ?", [action_id]
    ).fetchone()
    if row is None:
        return None
    return _row_to_action(row, cols)


def get_action(con: sqlite3.Connection, action_id: str) -> dict | None:
    """Return a full action dict with its status history, or None."""
    action = _get_action(con, action_id)
    if action is None:
        return None
    action["history"] = get_action_history(con, action_id)
    return action


def list_actions(
    con: sqlite3.Connection,
    status: str | None = None,
    action_type: str | None = None,
) -> list[dict]:
    """Return all actions matching optional filters, newest first."""
    cols = [
        "id", "type", "status", "title", "description",
        "object_type", "object_id", "evidence_id",
        "snapshot_id", "metric_version", "proposed_at", "proposed_by",
    ]
    where_clauses = []
    params: list = []
    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if action_type:
        where_clauses.append("type = ?")
        params.append(action_type)
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM actions {where} ORDER BY proposed_at DESC",
        params,
    ).fetchall()
    return [_row_to_action(r, cols) for r in rows]


def resolve_action(
    con: sqlite3.Connection,
    action_id: str,
    new_status: str,
    resolved_by: str,
) -> tuple[dict | None, bool]:
    """Approve or reject a pending action.

    Returns (action_dict, success).
    success=False when the action is not found or is already resolved.
    The UPDATE is conditional on status='pending' to make concurrent
    approve/reject requests produce exactly one winner.
    Both the status update and the log entry are written in one transaction.
    """
    if new_status not in TERMINAL_STATUSES:
        raise ValueError(f"new_status must be one of {sorted(TERMINAL_STATUSES)}, got '{new_status}'.")

    now = _now()
    with con:
        cur = con.execute(
            "UPDATE actions SET status = ? WHERE id = ? AND status = 'pending'",
            [new_status, action_id],
        )
        if cur.rowcount == 0:
            return get_action(con, action_id), False
        con.execute(
            "INSERT INTO action_status_log (action_id, status, changed_at, changed_by) VALUES (?, ?, ?, ?)",
            [action_id, new_status, now, resolved_by],
        )

    return get_action(con, action_id), True


def get_action_history(con: sqlite3.Connection, action_id: str) -> list[dict]:
    """Return the append-only status log for an action, ordered by seq."""
    rows = con.execute(
        "SELECT seq, status, changed_at, changed_by FROM action_status_log "
        "WHERE action_id = ? ORDER BY seq",
        [action_id],
    ).fetchall()
    return [
        {"seq": r[0], "status": r[1], "changed_at": r[2], "changed_by": r[3]}
        for r in rows
    ]
