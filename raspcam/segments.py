"""Segment file naming, probing, and indexing into the SegmentStore."""
from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from .db import Segment, SegmentStore

log = logging.getLogger(__name__)

SEGMENT_PATTERN = "%Y%m%d-%H%M%S.mp4"  # ffmpeg -strftime pattern, local time
_NAME_RE = re.compile(r"^(\d{8}-\d{6})\.mp4$")


def parse_start(name: str) -> float | None:
    """Start time (epoch seconds) encoded in a segment file name."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def probe_duration(path: Path) -> float | None:
    """Duration in seconds, or None if ffprobe cannot read the file."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30)
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        return None
    return duration if r.returncode == 0 and duration > 0 else None


class Indexer:
    def __init__(self, store: SegmentStore, directory: Path, *,
                 probe: Callable[[Path], float | None] = probe_duration,
                 settle_seconds: float = 5.0, clock: Callable[[], float] = time.time):
        self._store = store
        self._dir = directory
        self._probe = probe
        self._settle = settle_seconds
        self._clock = clock

    def scan(self, writing: bool) -> list[str]:
        """Make the store match the directory; return names newly indexed.

        writing: ffmpeg is running, so the newest file is still being written.
        Files modified in the last settle_seconds are also left alone, which
        covers ffmpeg (re)starting between the caller's check and this scan.
        """
        files = sorted(p for p in self._dir.iterdir() if parse_start(p.name) is not None)
        on_disk = {p.name for p in files}
        known = self._store.names()
        for name in known - on_disk:
            self._store.remove(name)
        if writing and files:
            files = files[:-1]
        now = self._clock()
        added = []
        for p in files:
            if p.name in known:
                continue
            stat = p.stat()
            if now - stat.st_mtime < self._settle:
                continue
            duration = self._probe(p)
            if duration is None:
                log.warning("deleting unreadable segment %s", p.name)
                p.unlink(missing_ok=True)
                continue
            start = parse_start(p.name)
            self._store.add(Segment(p.name, start, start + duration, stat.st_size))
            added.append(p.name)
        return added
