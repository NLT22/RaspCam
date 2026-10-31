"""SQLite index of recorded segments."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    name     TEXT PRIMARY KEY,
    day      TEXT NOT NULL,     -- local date of start_ts, YYYY-MM-DD
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL,
    size     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_day ON segments(day, start_ts);
"""
COLUMNS = "name, start_ts, end_ts, size"


@dataclass(frozen=True)
class Segment:
    name: str
    start_ts: float
    end_ts: float
    size: int

    @property
    def duration(self) -> float:
        return self.end_ts - self.start_ts


class SegmentStore:
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _query(self, sql: str, args=()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _write(self, sql: str, args=()) -> None:
        with self._lock, self._db:
            self._db.execute(sql, args)

    def add(self, seg: Segment) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(seg.start_ts))
        self._write("INSERT OR REPLACE INTO segments (name, day, start_ts, end_ts, size) "
                    "VALUES (?, ?, ?, ?, ?)", (seg.name, day, seg.start_ts, seg.end_ts, seg.size))

    def remove(self, name: str) -> None:
        self._write("DELETE FROM segments WHERE name = ?", (name,))

    def get(self, name: str) -> Segment | None:
        rows = self._query(f"SELECT {COLUMNS} FROM segments WHERE name = ?", (name,))
        return Segment(*rows[0]) if rows else None

    def names(self) -> set[str]:
        return {r[0] for r in self._query("SELECT name FROM segments")}

    def oldest_first(self) -> list[Segment]:
        return [Segment(*r) for r in self._query(f"SELECT {COLUMNS} FROM segments ORDER BY start_ts")]

    def days(self) -> list[str]:
        return [r[0] for r in self._query("SELECT DISTINCT day FROM segments ORDER BY day DESC")]

    def by_day(self, day: str) -> list[Segment]:
        return [Segment(*r) for r in self._query(
            f"SELECT {COLUMNS} FROM segments WHERE day = ? ORDER BY start_ts", (day,))]

    def total_size(self) -> int:
        return self._query("SELECT COALESCE(SUM(size), 0) FROM segments")[0][0]
