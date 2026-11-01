"""Motion detection on small grayscale frames, and turning it into events."""
from __future__ import annotations

import numpy as np

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
