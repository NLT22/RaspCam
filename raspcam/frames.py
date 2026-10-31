"""Thread-safe holder for the most recent live JPEG frame."""
from __future__ import annotations

import threading
import time
from typing import Callable


class LatestFrame:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._seq = 0
        self._jpeg: bytes | None = None
        self._updated: float | None = None

    def set(self, jpeg: bytes) -> None:
        with self._lock:
            self._seq += 1
            self._jpeg = jpeg
            self._updated = self._clock()

    def get(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._seq, self._jpeg

    def age(self) -> float | None:
        """Seconds since the last frame, or None if there never was one."""
        with self._lock:
            return None if self._updated is None else self._clock() - self._updated
