"""
SQLite database layer for battery-monitor.

The daemon writes every collection cycle; the UI reads at display time.
We use WAL mode so reads never block writes and vice-versa.

Tables
------
snapshots       – one row per collection cycle (battery-level metrics)
process_stats   – per-process rows linked to a snapshot
component_stats – per-hardware-component rows linked to a snapshot
user_preferences – persisted kill decisions (always_allow / always_deny)
"""

import sqlite3
import time
import logging
from pathlib import Path
from typing import Optional

import sys, os
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.shared.config import config, DB_FILE

log = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp            REAL    NOT NULL,
    battery_percent      REAL,
    battery_status       TEXT,
    discharge_rate_watts REAL,
    voltage_volts        REAL,
    time_remaining_min   REAL
);

CREATE TABLE IF NOT EXISTS process_stats (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id      INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    pid              INTEGER,
    name             TEXT,
    username         TEXT,
    cpu_percent      REAL,
    memory_mb        REAL,
    estimated_watts  REAL,
    kill_safety      TEXT,   -- 'safe' | 'caution' | 'unsafe'
    cmdline          TEXT
);

CREATE TABLE IF NOT EXISTS component_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    component       TEXT,
    estimated_watts REAL
);

CREATE TABLE IF NOT EXISTS user_preferences (
    process_name TEXT PRIMARY KEY,
    kill_action  TEXT NOT NULL,  -- 'always_allow' | 'always_deny' | 'ask'
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ts        ON snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_process_snap        ON process_stats(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_component_snap      ON component_stats(snapshot_id);
"""


class DatabaseManager:
    """Thread-safe (multi-process via WAL) SQLite manager."""

    def __init__(self, db_path: Path = DB_FILE) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Writes (daemon) ───────────────────────────────────────────────────────

    def save_snapshot(
        self,
        battery_percent: Optional[float],
        battery_status: Optional[str],
        discharge_rate_watts: Optional[float],
        voltage_volts: Optional[float],
        time_remaining_min: Optional[float],
    ) -> int:
        """Insert one snapshot row and return its id."""
        sql = """
            INSERT INTO snapshots
                (timestamp, battery_percent, battery_status,
                 discharge_rate_watts, voltage_volts, time_remaining_min)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._connect() as conn:
            cur = conn.execute(
                sql,
                (time.time(), battery_percent, battery_status,
                 discharge_rate_watts, voltage_volts, time_remaining_min),
            )
            return cur.lastrowid

    def save_process_stats(self, snapshot_id: int, processes: list[dict]) -> None:
        sql = """
            INSERT INTO process_stats
                (snapshot_id, pid, name, username, cpu_percent, memory_mb,
                 estimated_watts, kill_safety, cmdline)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (
                snapshot_id,
                p["pid"], p["name"], p.get("username"),
                p.get("cpu_percent", 0), p.get("memory_mb", 0),
                p.get("estimated_watts", 0), p.get("kill_safety", "caution"),
                p.get("cmdline", ""),
            )
            for p in processes
        ]
        with self._connect() as conn:
            conn.executemany(sql, rows)

    def save_component_stats(self, snapshot_id: int, components: list[dict]) -> None:
        sql = """
            INSERT INTO component_stats (snapshot_id, component, estimated_watts)
            VALUES (?, ?, ?)
        """
        rows = [(snapshot_id, c["component"], c.get("estimated_watts", 0))
                for c in components]
        with self._connect() as conn:
            conn.executemany(sql, rows)

    # ── Reads (UI) ────────────────────────────────────────────────────────────

    def get_latest_snapshot(self) -> Optional[sqlite3.Row]:
        sql = "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 1"
        with self._connect() as conn:
            return conn.execute(sql).fetchone()

    def get_latest_processes(self, limit: int = 20) -> list[sqlite3.Row]:
        """Return processes from the most recent snapshot, sorted by watts desc."""
        sql = """
            SELECT ps.*
            FROM   process_stats ps
            JOIN   snapshots s ON ps.snapshot_id = s.id
            WHERE  s.id = (SELECT id FROM snapshots ORDER BY timestamp DESC LIMIT 1)
            ORDER  BY ps.estimated_watts DESC
            LIMIT  ?
        """
        with self._connect() as conn:
            return conn.execute(sql, (limit,)).fetchall()

    def get_latest_components(self) -> list[sqlite3.Row]:
        sql = """
            SELECT cs.*
            FROM   component_stats cs
            JOIN   snapshots s ON cs.snapshot_id = s.id
            WHERE  s.id = (SELECT id FROM snapshots ORDER BY timestamp DESC LIMIT 1)
            ORDER  BY cs.estimated_watts DESC
        """
        with self._connect() as conn:
            return conn.execute(sql).fetchall()

    def get_recent_snapshots(self, minutes: int = 30) -> list[sqlite3.Row]:
        cutoff = time.time() - minutes * 60
        sql = "SELECT * FROM snapshots WHERE timestamp >= ? ORDER BY timestamp"
        with self._connect() as conn:
            return conn.execute(sql, (cutoff,)).fetchall()

    def get_process_history(self, name: str, minutes: int = 30) -> list[sqlite3.Row]:
        """Return per-process history for spike detection."""
        cutoff = time.time() - minutes * 60
        sql = """
            SELECT ps.estimated_watts, s.timestamp
            FROM   process_stats ps
            JOIN   snapshots s ON ps.snapshot_id = s.id
            WHERE  ps.name = ? AND s.timestamp >= ?
            ORDER  BY s.timestamp
        """
        with self._connect() as conn:
            return conn.execute(sql, (name, cutoff)).fetchall()

    # ── User preferences ──────────────────────────────────────────────────────

    def get_user_preference(self, process_name: str) -> Optional[str]:
        sql = "SELECT kill_action FROM user_preferences WHERE process_name = ?"
        with self._connect() as conn:
            row = conn.execute(sql, (process_name,)).fetchone()
            return row["kill_action"] if row else None

    def set_user_preference(self, process_name: str, kill_action: str) -> None:
        sql = """
            INSERT INTO user_preferences (process_name, kill_action, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(process_name) DO UPDATE SET
                kill_action = excluded.kill_action,
                updated_at  = excluded.updated_at
        """
        with self._connect() as conn:
            conn.execute(sql, (process_name, kill_action, time.time()))

    # ── Maintenance ───────────────────────────────────────────────────────────

    def purge_old_data(self, keep_minutes: int = None) -> None:
        """Delete snapshots (and their cascade children) older than keep window."""
        if keep_minutes is None:
            keep_minutes = config.history_minutes
        cutoff = time.time() - keep_minutes * 60
        with self._connect() as conn:
            conn.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))

    def export_csv(self, filepath: str) -> int:
        """Export snapshot history to CSV. Returns number of rows written."""
        import csv
        rows = self.get_recent_snapshots(minutes=60 * 24 * 7)  # last 7 days
        if not rows:
            return 0
        with open(filepath, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows([dict(r) for r in rows])
        return len(rows)
