"""Motion detection on small grayscale frames, and turning it into events."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .config import MotionConfig
from .db import EventStore
from .frames import LatestFrame

log = logging.getLogger(__name__)

LIGHTING_CHANGE = 0.6  # this much of the picture changing at once = lights/exposure, not motion
GAP_RESET = 2.0        # seconds without frames (ffmpeg restart) after which the background is stale


def _blur(img: np.ndarray) -> np.ndarray:
    """3x3 box blur with replicated edges; removes single-pixel sensor noise."""
    h, w = img.shape
    p = np.pad(img, 1, mode="edge")
    out = np.zeros_like(img)
    for dy in range(3):
        for dx in range(3):
            out += p[dy:dy + h, dx:dx + w]
    return out / 9


class MotionDetector:
    """Scores each frame by how much of it differs from a slowly adapting background."""

    def __init__(self, width: int, height: int, pixel_threshold: int, alpha: float = 0.05):
        self._shape = (height, width)
        self._threshold = pixel_threshold
        self._alpha = alpha
        self._bg: np.ndarray | None = None

    def reset(self) -> None:
        self._bg = None

    def score(self, frame: bytes) -> float:
        """Fraction (0..1) of pixels that changed. The first frame and sudden
        whole-scene changes re-seed the background and score 0."""
        img = _blur(np.frombuffer(frame, dtype=np.uint8).reshape(self._shape).astype(np.float32))
        if self._bg is None:
            self._bg = img
            return 0.0
        changed = float((np.abs(img - self._bg) > self._threshold).mean())
        if changed > LIGHTING_CHANGE:
            self._bg = img
            return 0.0
        self._bg += self._alpha * (img - self._bg)
        return changed


class EventTracker:
    """Turns per-frame motion flags into events: an event starts after
    min_frames consecutive motion frames and ends `cooldown` seconds after the
    last one."""

    def __init__(self, min_frames: int, cooldown: float):
        self._min = min_frames
        self._cooldown = cooldown
        self._streak = 0
        self._streak_start = 0.0
        self.active = False
        self.last_motion: float | None = None

    def update(self, ts: float, moving: bool) -> tuple[str, float] | None:
        if moving:
            if self._streak == 0:
                self._streak_start = ts
            self._streak += 1
            self.last_motion = ts
            if not self.active and self._streak >= self._min:
                self.active = True
                return ("start", self._streak_start)
            return None
        self._streak = 0
        if self.active and ts - self.last_motion >= self._cooldown:
            self.active = False
            return ("end", self.last_motion)
        return None


class MotionMonitor:
    """Runs detection on its own thread so a slow frame never blocks ffmpeg:
    submit() only keeps the newest frame and older unprocessed ones are dropped."""

    def __init__(self, cfg: MotionConfig, events: EventStore, latest: LatestFrame,
                 snapshots_dir: Path):
        self.detector = MotionDetector(cfg.width, cfg.height, cfg.pixel_threshold)
        self._tracker = EventTracker(cfg.min_frames, cfg.cooldown_seconds)
        self._area = cfg.area_percent / 100
        self._events = events
        self._latest = latest
        self._dir = snapshots_dir
        self._cond = threading.Condition()
        self._pending: tuple[bytes, float] | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._event_id: int | None = None
        self._peak = 0.0
        self._snapshot_at = 0.0
        self._last_ts: float | None = None
        self.score = 0.0
        self.frames_seen = 0

    @property
    def active(self) -> bool:
        return self._tracker.active

    def submit(self, frame: bytes, ts: float) -> None:
        with self._cond:
            self._pending = (frame, ts)
            self._cond.notify()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="motion", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self._thread:
            self._thread.join(timeout=5)
        if self._event_id is not None and self._tracker.last_motion is not None:
            self._events.close(self._event_id, self._tracker.last_motion)
            self._event_id = None

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                frame, ts = self._pending
                self._pending = None
            try:
                self.process(frame, ts)
            except Exception:
                log.exception("motion detection failed")

    def process(self, frame: bytes, ts: float) -> None:
        if self._last_ts is not None and ts - self._last_ts > GAP_RESET:
            self.detector.reset()
        self._last_ts = ts
        self.frames_seen += 1
        self.score = self.detector.score(frame)
        change = self._tracker.update(ts, self.score >= self._area)
        if change and change[0] == "start":
            self._event_id = self._events.open(change[1])
            self._peak = self.score
            self._events.set_peak(self._event_id, self._peak)
            self._save_snapshot(ts)
        elif self._event_id is not None and self.score > self._peak:
            self._peak = self.score
            self._events.set_peak(self._event_id, self._peak)
            if ts - self._snapshot_at >= 1.0:
                self._save_snapshot(ts)
        if change and change[0] == "end":
            self._events.close(self._event_id, change[1])
            log.info("motion event %d ended (peak %.1f%%)", self._event_id, self._peak * 100)
            self._event_id = None

    def _save_snapshot(self, ts: float) -> None:
        jpeg = self._latest.get()[1]
        if jpeg is None or self._event_id is None:
            return
        tmp = self._dir / f"{self._event_id}.tmp"
        tmp.write_bytes(jpeg)
        tmp.replace(self._dir / f"{self._event_id}.jpg")
        self._snapshot_at = ts
