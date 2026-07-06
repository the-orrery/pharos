"""SQLite-backed alert store: persistent, queryable, concurrency-safe tickets.

One row per alert, keyed by check id (`key`). Status walks firing → acked →
resolved; the reconciler owns the firing/resolved transitions, `ack`/`resolve`
are the manual operator moves. We reuse telemetry's storage discipline verbatim
— XDG data path, env override, WAL + busy_timeout — so several CLI processes
(agents, cron, shells) can read/write the same db without torn writes.

The db is local state, never committed (see .gitignore `*.db*`). It is cheap to
delete to reset: tickets rebuild on the next `run`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

# Lifecycle states. firing/acked are "open" (an alert needs attention); resolved
# is terminal for that occurrence (the check recovered or was removed).
FIRING = "firing"
ACKED = "acked"
RESOLVED = "resolved"
_OPEN = (FIRING, ACKED)

_ENV_DB = "PHAROS_ALERTS_DB"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key         TEXT    PRIMARY KEY,
    source      TEXT    NOT NULL DEFAULT '',
    severity    TEXT    NOT NULL DEFAULT 'warning',
    status      TEXT    NOT NULL DEFAULT 'firing',
    title       TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '',
    first_seen  REAL    NOT NULL DEFAULT 0,
    last_seen   REAL    NOT NULL DEFAULT 0,
    resolved_at REAL,
    occurrences INTEGER NOT NULL DEFAULT 0,
    ack_note    TEXT    NOT NULL DEFAULT '',
    evidence    TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts(status);
"""

_COLUMNS = (
    "key",
    "source",
    "severity",
    "status",
    "title",
    "detail",
    "first_seen",
    "last_seen",
    "resolved_at",
    "occurrences",
    "ack_note",
    "evidence",
)


def db_path() -> Path:
    """Resolve the alert db path: $PHAROS_ALERTS_DB override,
    else ($XDG_DATA_HOME or ~/.local/share)/pharos/alerts.db."""
    override = os.environ.get(_ENV_DB)
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "pharos" / "alerts.db"


def _connect(path: Path) -> sqlite3.Connection:
    """Open the store in WAL mode with a busy timeout so concurrent CLI
    processes queue rather than fail. isolation_level=None → explicit txns.
    Returns rows as dict-like sqlite3.Row. Closes itself if setup fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version=1")  # migration anchor for future columns
    except Exception:
        conn.close()
        raise
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Materialise a row into a plain dict, decoding the JSON evidence blob."""
    d = dict(row)
    try:
        d["evidence"] = json.loads(d.get("evidence") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["evidence"] = {}
    return d


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_active(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Open tickets (firing or acked), worst-severity-ish first then oldest.

    This is the agent's "what is currently broken?" view — resolved tickets are
    excluded."""
    conn = _connect(path or db_path())
    try:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE status IN (?, ?) ORDER BY first_seen ASC",
            _OPEN,
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def list_all(*, path: Path | None = None) -> list[dict[str, Any]]:
    """Every ticket, open and resolved, most-recently-seen first."""
    conn = _connect(path or db_path())
    try:
        rows = conn.execute("SELECT * FROM alerts ORDER BY last_seen DESC").fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get(key: str, *, path: Path | None = None) -> dict[str, Any] | None:
    """One ticket by key, or None if no such alert was ever recorded."""
    conn = _connect(path or db_path())
    try:
        row = conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None


def get_open(key: str, *, path: Path | None = None) -> dict[str, Any] | None:
    """An *open* (firing/acked) ticket for key — the reconciler's lookup."""
    alert = get(key, path=path)
    if alert is not None and alert["status"] in _OPEN:
        return alert
    return None


def open_keys(*, path: Path | None = None) -> set[str]:
    """Keys of all open tickets — lets the reconciler find recoveries (open
    tickets whose key is absent from this run's failing set)."""
    conn = _connect(path or db_path())
    try:
        rows = conn.execute(
            "SELECT key FROM alerts WHERE status IN (?, ?)", _OPEN
        ).fetchall()
    finally:
        conn.close()
    return {r["key"] for r in rows}


# ---------------------------------------------------------------------------
# Reconciler primitives (upsert / touch / resolve)
# ---------------------------------------------------------------------------


def upsert_firing(
    *,
    key: str,
    source: str,
    severity: str,
    title: str,
    detail: str,
    evidence: dict[str, Any],
    now: float,
    path: Path | None = None,
) -> None:
    """Insert a brand-new firing ticket (first_seen=last_seen=now, occurrences=1).

    Used by the reconciler for a NEW transition: a failing check with no open
    ticket. Replaces any prior resolved row for the same key so a recurrence
    starts a clean lifecycle (first_seen reset)."""
    conn = _connect(path or db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO alerts "
            "(key, source, severity, status, title, detail, first_seen, last_seen, "
            " resolved_at, occurrences, ack_note, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, '', ?)",
            (
                key,
                source,
                severity,
                FIRING,
                title,
                detail,
                now,
                now,
                json.dumps(evidence, ensure_ascii=False, default=str),
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def touch_ongoing(
    *,
    key: str,
    severity: str,
    title: str,
    detail: str,
    evidence: dict[str, Any],
    now: float,
    path: Path | None = None,
) -> None:
    """Refresh an existing open ticket on a repeat failure: bump last_seen and
    occurrences, refresh severity/title/detail/evidence. Status is untouched —
    a firing ticket stays firing, an acked ticket stays acked (so an ack
    survives ongoing failures and never re-pages)."""
    conn = _connect(path or db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE alerts SET last_seen = ?, occurrences = occurrences + 1, "
            "severity = ?, title = ?, detail = ?, evidence = ? "
            "WHERE key = ? AND status IN (?, ?)",
            (
                now,
                severity,
                title,
                detail,
                json.dumps(evidence, ensure_ascii=False, default=str),
                key,
                *_OPEN,
            ),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def mark_resolved(key: str, *, now: float, path: Path | None = None) -> bool:
    """Resolve an open ticket (status=resolved, resolved_at=now). Returns True if
    a row moved. Idempotent: a non-open or absent key is a no-op → False."""
    conn = _connect(path or db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE alerts SET status = ?, resolved_at = ? "
            "WHERE key = ? AND status IN (?, ?)",
            (RESOLVED, now, key, *_OPEN),
        )
        changed = cur.rowcount
        conn.execute("COMMIT")
    finally:
        conn.close()
    return changed > 0


# ---------------------------------------------------------------------------
# Manual operator moves (CLI ack / resolve)
# ---------------------------------------------------------------------------


def ack(key: str, note: str = "", *, path: Path | None = None) -> bool:
    """Acknowledge an open ticket (firing/acked → acked), recording an optional
    note. Returns True if a row moved (the key exists and is open)."""
    conn = _connect(path or db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE alerts SET status = ?, ack_note = ? "
            "WHERE key = ? AND status IN (?, ?)",
            (ACKED, note, key, *_OPEN),
        )
        changed = cur.rowcount
        conn.execute("COMMIT")
    finally:
        conn.close()
    return changed > 0


def resolve(key: str, *, path: Path | None = None) -> bool:
    """Manually resolve an open ticket (operator decides it's handled). Same
    effect as the reconciler's auto-resolve; returns True if a row moved."""
    return mark_resolved(key, now=time.time(), path=path)


__all__ = [
    "ACKED",
    "FIRING",
    "RESOLVED",
    "ack",
    "db_path",
    "get",
    "get_open",
    "list_active",
    "list_all",
    "mark_resolved",
    "open_keys",
    "resolve",
    "touch_ongoing",
    "upsert_firing",
]
