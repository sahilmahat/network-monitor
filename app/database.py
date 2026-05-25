"""
database.py — SQLite storage for ping results and incidents.

Public functions:
  init_db()                          -> create tables if not exist
  insert_ping_result(result)         -> store one ping (detects state transitions)
  get_latest_status()                -> latest result per host
  get_history(host, hours)           -> history for one host
  get_uptime_percentage(host, hours) -> uptime % over window
  get_recent_incidents(hours)        -> recent state transitions
  cleanup_old_records(retention_days)-> delete old rows, reclaim disk space
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("data/monitor.db")


@contextmanager
def get_conn():
    """
    Context manager for SQLite connections.
    Ensures connection is closed even if something blows up mid-query.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["host"]
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables and indexes if they don't already exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ping_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                host        TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                type        TEXT    NOT NULL,
                is_up       INTEGER NOT NULL,
                latency_ms  REAL,
                error       TEXT,
                checked_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_ping_host_time
                ON ping_results(host, checked_at DESC);

            CREATE TABLE IF NOT EXISTS incidents (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                host             TEXT    NOT NULL,
                name             TEXT    NOT NULL,
                event_type       TEXT    NOT NULL,  -- 'down' or 'recovered'
                occurred_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                duration_seconds INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_time
                ON incidents(occurred_at DESC);
        """)


def insert_ping_result(result: dict) -> None:
    """
    Store a single ping result.
    Also detects state changes and logs them to the incidents table.
    """
    with get_conn() as conn:
        # Look up the previous state to detect transitions
        prev = conn.execute("""
            SELECT is_up FROM ping_results
            WHERE host = ?
            ORDER BY checked_at DESC
            LIMIT 1
        """, (result["host"],)).fetchone()

        # Insert the new ping
        conn.execute("""
            INSERT INTO ping_results (host, name, type, is_up, latency_ms, error, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            result["host"],
            result["name"],
            result["type"],
            1 if result["is_up"] else 0,
            result["latency_ms"],
            result["error"],
            result["checked_at"],
        ))

        # Detect state change → log incident
        if prev is not None:
            was_up = bool(prev["is_up"])
            now_up = result["is_up"]

            if was_up and not now_up:
                conn.execute("""
                    INSERT INTO incidents (host, name, event_type)
                    VALUES (?, ?, 'down')
                """, (result["host"], result["name"]))

            elif not was_up and now_up:
                # Calculate downtime duration since the 'down' event
                last_down = conn.execute("""
                    SELECT occurred_at FROM incidents
                    WHERE host = ? AND event_type = 'down'
                    ORDER BY occurred_at DESC
                    LIMIT 1
                """, (result["host"],)).fetchone()

                duration = None
                if last_down:
                    down_time = datetime.fromisoformat(last_down["occurred_at"])
                    duration = int((datetime.now() - down_time).total_seconds())

                conn.execute("""
                    INSERT INTO incidents (host, name, event_type, duration_seconds)
                    VALUES (?, ?, 'recovered', ?)
                """, (result["host"], result["name"], duration))


def get_latest_status() -> list[dict]:
    """Return the most recent ping for every host."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT p.host, p.name, p.type, p.is_up, p.latency_ms, p.error, p.checked_at
            FROM ping_results p
            INNER JOIN (
                SELECT host, MAX(checked_at) AS max_time
                FROM ping_results
                GROUP BY host
            ) latest
              ON p.host = latest.host AND p.checked_at = latest.max_time
            ORDER BY p.type, p.name
        """).fetchall()
        return [dict(r) for r in rows]


def get_history(host: str, hours: int = 24) -> list[dict]:
    """Return ping history for one host over the last N hours."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT host, is_up, latency_ms, checked_at
            FROM ping_results
            WHERE host = ? AND checked_at >= ?
            ORDER BY checked_at ASC
        """, (host, cutoff)).fetchall()
        return [dict(r) for r in rows]


def get_uptime_percentage(host: str, hours: int = 24) -> dict:
    """Return uptime % over the last N hours."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                       AS total_pings,
                SUM(CASE WHEN is_up = 1 THEN 1 ELSE 0 END) AS successful_pings
            FROM ping_results
            WHERE host = ? AND checked_at >= ?
        """, (host, cutoff)).fetchone()

        total = row["total_pings"] or 0
        successful = row["successful_pings"] or 0
        percentage = round((successful / total) * 100, 2) if total > 0 else 0.0

        return {
            "host": host,
            "hours": hours,
            "uptime_percentage": percentage,
            "total_pings": total,
            "successful_pings": successful,
        }


def get_recent_incidents(hours: int = 24) -> list[dict]:
    """Return recent state-transition events."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT host, name, event_type, occurred_at, duration_seconds
            FROM incidents
            WHERE occurred_at >= ?
            ORDER BY occurred_at DESC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


def cleanup_old_records(retention_days: int) -> dict:
    """
    Delete ping_results older than retention_days.
    Also deletes 'recovered' incidents older than 1 year.
    VACUUMs to reclaim disk space.
    Returns count of deleted rows. Run this nightly.
    """
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    with get_conn() as conn:
        # Count first so we can log how much we deleted
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM ping_results WHERE checked_at < ?",
            (cutoff,)
        ).fetchone()
        deleted_pings = count_row["c"]

        conn.execute("DELETE FROM ping_results WHERE checked_at < ?", (cutoff,))

        # Also clean up old recovered incidents (keep 1 year)
        incident_cutoff = (datetime.now() - timedelta(days=365)).isoformat()
        conn.execute(
            "DELETE FROM incidents WHERE occurred_at < ? AND event_type = 'recovered'",
            (incident_cutoff,)
        )

    # VACUUM must run OUTSIDE a transaction → separate connection, no context manager
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.isolation_level = None  # autocommit mode so VACUUM works
        conn.execute("VACUUM")
    finally:
        conn.close()

    return {"deleted_pings": deleted_pings, "cutoff": cutoff}


# Standalone test runner — `python -m app.database`
if __name__ == "__main__":
    print("Initialising database...")
    init_db()
    print(f"✅ Database ready at: {DB_PATH.resolve()}")

    print("\nInserting a test ping result...")
    insert_ping_result({
        "host": "8.8.8.8",
        "name": "Google DNS",
        "type": "vm",
        "is_up": True,
        "latency_ms": 12.3,
        "error": None,
        "checked_at": datetime.now().isoformat(),
    })

    print("\nLatest status:")
    for r in get_latest_status():
        print(f"  {r}")
