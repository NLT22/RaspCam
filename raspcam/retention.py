"""Delete segments: outside motion events (motion mode), and the oldest ones
when over quota or low on disk space. Events whose footage is gone go too."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from .config import StorageConfig
from .db import Event, EventStore, Segment, SegmentStore
from .segments import Indexer, thumb_name

log = logging.getLogger(__name__)

GB = 10**9


def plan_deletions(segments: list[Segment], quota_bytes: int,
                   free_bytes: int, min_free_bytes: int) -> list[str]:
    """segments oldest first; return names to delete, oldest first."""
    total = sum(s.size for s in segments)
    doomed = []
    for s in segments:
        if total <= quota_bytes and free_bytes >= min_free_bytes:
            break
        doomed.append(s.name)
        total -= s.size
        free_bytes += s.size
    return doomed


def plan_motion_prune(segments: list[Segment], events: list[Event],
                      padding: float, now: float) -> list[str]:
    """Motion mode: segments overlapping no [event start - padding, end + padding]
    window. Segments that ended less than `padding` ago are kept because an event
    may still start; open events count as lasting until now."""
    windows = [(e.start_ts - padding, (e.end_ts if e.end_ts is not None else now) + padding)
               for e in events]
    return [s.name for s in segments
            if s.end_ts <= now - padding
            and not any(s.start_ts < hi and s.end_ts > lo for lo, hi in windows)]


class Housekeeper:
    """Periodically index new segments and enforce the storage limits."""

    def __init__(self, store: SegmentStore, indexer: Indexer, directory: Path,
                 storage: StorageConfig, writing: Callable[[], bool], *,
                 interval: float = 10.0,
                 disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
                 thumbs_dir: Path | None = None,
                 events: EventStore | None = None, mode: str = "both",
                 padding_seconds: float = 60.0, events_dir: Path | None = None,
                 clock: Callable[[], float] = time.time):
        self._store = store
        self._indexer = indexer
        self._dir = directory
        self._storage = storage
        self._writing = writing
        self._interval = interval
        self._disk_free = disk_free
        self._thumbs = thumbs_dir
        self._events = events
        self._mode = mode
        self._padding = padding_seconds
        self._events_dir = events_dir
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        self._indexer.scan(self._writing())
        segments = self._store.oldest_first()
        doomed: list[str] = []
        if self._mode == "motion" and self._events is not None:
            doomed = plan_motion_prune(segments, self._events.all(), self._padding, self._clock())
        gone = set(doomed)
        freed = sum(s.size for s in segments if s.name in gone)
        doomed += plan_deletions([s for s in segments if s.name not in gone],
                                 int(self._storage.quota_gb * GB),
                                 self._disk_free(self._dir) + freed,
                                 int(self._storage.min_free_gb * GB))
        for name in doomed:
            self._delete_segment(name)
        if doomed:
            log.info("retention deleted %d segment(s), oldest %s", len(doomed), doomed[0])
        if self._events is not None:
            self._drop_events_without_footage()

    def _delete_segment(self, name: str) -> None:
        (self._dir / name).unlink(missing_ok=True)
        if self._thumbs:
            (self._thumbs / thumb_name(name)).unlink(missing_ok=True)
        self._store.remove(name)

    def _drop_events_without_footage(self) -> None:
        remaining = self._store.oldest_first()
        cutoff = (remaining[0].start_ts if remaining else self._clock()) - self._padding
        for event_id in self._events.delete_ending_before(cutoff):
            if self._events_dir:
                (self._events_dir / f"{event_id}.jpg").unlink(missing_ok=True)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="housekeeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception:
                log.exception("housekeeping failed")
