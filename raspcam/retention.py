"""Delete the oldest segments when over quota or low on disk space."""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Callable

from .config import StorageConfig
from .db import Segment, SegmentStore
from .segments import Indexer

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


class Housekeeper:
    """Periodically index new segments and enforce the storage limits."""

    def __init__(self, store: SegmentStore, indexer: Indexer, directory: Path,
                 storage: StorageConfig, writing: Callable[[], bool], *,
                 interval: float = 10.0,
                 disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free):
        self._store = store
        self._indexer = indexer
        self._dir = directory
        self._storage = storage
        self._writing = writing
        self._interval = interval
        self._disk_free = disk_free
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        self._indexer.scan(self._writing())
        doomed = plan_deletions(self._store.oldest_first(),
                                int(self._storage.quota_gb * GB),
                                self._disk_free(self._dir),
                                int(self._storage.min_free_gb * GB))
        for name in doomed:
            (self._dir / name).unlink(missing_ok=True)
            self._store.remove(name)
        if doomed:
            log.info("retention deleted %d segment(s), oldest %s", len(doomed), doomed[0])

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
