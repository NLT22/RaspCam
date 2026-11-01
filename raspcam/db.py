"""SQLite index of recorded segments and motion events."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SEGMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    name     TEXT PRIMARY KEY,
    day      TEXT NOT NULL,     -- local date of start_ts, YYYY-MM-DD
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL,
    size     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_day ON segments(day, start_ts);
"""
EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    day      TEXT NOT NULL,     -- local date of start_ts
    start_ts REAL NOT NULL,
    end_ts   REAL,              -- NULL while the event is in progress
    peak     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_day ON events(day, start_ts);
"""
SEGMENT_COLUMNS = "name, start_ts, end_ts, size"
EVENT_COLUMNS = "id, start_ts, end_ts, peak"


def local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class _Store:
    def __init__(self, path: str | Path, schema: str):
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(schema)

    def _query(self, sql: str, args=()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _write(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, args)


@dataclass(frozen=True)
class Segment:
    name: str
    start_ts: float
    end_ts: float
    size: int

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts


class SegmentStore(_Store):
    def __init__(self, path: str | Path):
        super().__init__(path, SEGMENTS_SCHEMA)

    def add(self, seg: Segment) -> None:
        self._write("INSERT OR REPLACE INTO segments (name, day, start_ts, end_ts, size) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (seg.name, local_day(seg.start_ts), seg.start_ts, seg.end_ts, seg.size))

    def remove(self, name: str) -> None:
        self._write("DELETE FROM segments WHERE name = ?", (name,))

    def get(self, name: str) -> Segment | None:
        rows = self._query(f"SELECT {SEGMENT_COLUMNS} FROM segments WHERE name = ?", (name,))
        return Segment(*rows[0]) if rows else None

    def names(self) -> set[str]:
        return {r[0] for r in self._query("SELECT name FROM segments")}

    def oldest_first(self) -> list[Segment]:
        return [Segment(*r) for r in self._query(
            f"SELECT {SEGMENT_COLUMNS} FROM segments ORDER BY start_ts")]

    def days(self) -> list[str]:
        return [r[0] for r in self._query("SELECT DISTINCT day FROM segments ORDER BY day DESC")]

    def by_day(self, day: str) -> list[Segment]:
        return [Segment(*r) for r in self._query(
            f"SELECT {SEGMENT_COLUMNS} FROM segments WHERE day = ? ORDER BY start_ts", (day,))]

    def total_size(self) -> int:
        return self._query("SELECT COALESCE(SUM(size), 0) FROM segments")[0][0]


@dataclass(frozen=True)
class Event:
    id: int
    start_ts: float
    end_ts: float | None
    peak: float


class EventStore(_Store):
    def __init__(self, path: str | Path):
        super().__init__(path, EVENTS_SCHEMA)

    def open(self, start_ts: float) -> int:
        return self._write("INSERT INTO events (day, start_ts) VALUES (?, ?)",
                           (local_day(start_ts), start_ts)).lastrowid

    def set_peak(self, event_id: int, peak: float) -> None:
        self._write("UPDATE events SET peak = ? WHERE id = ?", (peak, event_id))

    def close(self, event_id: int, end_ts: float) -> None:
        self._write("UPDATE events SET end_ts = ? WHERE id = ?", (end_ts, event_id))

    def get(self, event_id: int) -> Event | None:
        rows = self._query(f"SELECT {EVENT_COLUMNS} FROM events WHERE id = ?", (event_id,))
        return Event(*rows[0]) if rows else None

    def all(self) -> list[Event]:
        return [Event(*r) for r in self._query(f"SELECT {EVENT_COLUMNS} FROM events ORDER BY start_ts")]

    def by_day(self, day: str) -> list[Event]:
        return [Event(*r) for r in self._query(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE day = ? ORDER BY start_ts", (day,))]

    def count_day(self, day: str) -> int:
        return self._query("SELECT COUNT(*) FROM events WHERE day = ?", (day,))[0][0]

    def close_dangling(self) -> int:
        """Events left open by a crash end where they started."""
        return self._write("UPDATE events SET end_ts = start_ts WHERE end_ts IS NULL").rowcount

    def delete_ending_before(self, ts: float) -> list[int]:
        ids = [r[0] for r in self._query(
            "SELECT id FROM events WHERE end_ts IS NOT NULL AND end_ts < ?", (ts,))]
        if ids:
            self._write(f"DELETE FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)
        return ids
