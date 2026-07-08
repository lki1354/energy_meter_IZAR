"""Local SQLite store for every decoded meter reading.

Bill generation must query arbitrary past periods independent of the HA
recorder's purge settings, so the coordinator appends every reading to this
database (``/config/energy_meter_izar/readings.db``). It replaces the
``output_data_*.parquet`` intermediates of the notebook pipeline.

Pure Python and fully synchronous — no Home Assistant imports. The
coordinator runs all calls through the executor; a lock makes the shared
connection safe across executor threads.

Timestamps are stored as naive-local ISO 8601 strings (exactly as decoded
from the CP32 fields), so lexicographic comparison equals chronological
comparison and range queries can bind ISO strings directly. The primary key
``(device_number, quantity, timestamp)`` dedupes re-ingested snapshots,
which keeps retries and counter-wrap re-downloads idempotent.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from .mbus_parser import MeterReading

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_number INTEGER NOT NULL,
    quantity TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    unit TEXT NOT NULL,
    medium TEXT NOT NULL,
    location TEXT NOT NULL,
    status TEXT,
    PRIMARY KEY (device_number, quantity, timestamp)
) WITHOUT ROWID;
"""


class StoredValue(NamedTuple):
    """One numeric reading returned by :meth:`ReadingStore.series`."""

    timestamp: dt.datetime
    value: float


def _split_value(value: object) -> tuple[float | None, str | None]:
    """Route a decoded value to the numeric or the text column."""
    # bool is an int subclass but has no meaning as a meter value; and
    # datetime is a date subclass, so isoformat() covers both.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, value.isoformat() if isinstance(value, dt.date) else str(value)
    return float(value), None


class ReadingStore:
    """Append-only reading archive with exactly-once semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock, self._connection as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def add_readings(self, readings: Iterable[MeterReading]) -> int:
        """Insert readings, ignoring already-stored ones; returns rows added."""
        rows = []
        for reading in readings:
            value_num, value_text = _split_value(reading.value)
            rows.append(
                (
                    reading.device_number,
                    reading.quantity,
                    reading.timestamp.isoformat(),
                    value_num,
                    value_text,
                    reading.unit,
                    reading.medium,
                    reading.location,
                    reading.status,
                )
            )
        if not rows:
            return 0
        with self._lock, self._connection as conn:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
            return cursor.rowcount

    def series(
        self,
        device_number: int,
        quantity: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> list[StoredValue]:
        """Numeric readings of one (device, quantity), ordered by time.

        ``start`` is inclusive, ``end`` exclusive; both are naive local
        datetimes like the stored timestamps.
        """
        sql = (
            "SELECT timestamp, value_num FROM readings "
            "WHERE device_number=? AND quantity=? AND value_num IS NOT NULL"
        )
        params: list[object] = [device_number, quantity]
        if start is not None:
            sql += " AND timestamp>=?"
            params.append(start.isoformat())
        if end is not None:
            sql += " AND timestamp<?"
            params.append(end.isoformat())
        sql += " ORDER BY timestamp"
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [StoredValue(dt.datetime.fromisoformat(ts), value) for ts, value in rows]

    def reading_count(self) -> int:
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
