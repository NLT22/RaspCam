# RaspCam Milestone 3 (Motion Detection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect motion, store motion events with a snapshot, support `continuous` / `motion` / `both` recording modes, and show events on the timeline, in an event list, and on the live page.

**Architecture:** ffmpeg gains a third output (gray 320x180 @5fps rawvideo) on an extra pipe fd. The recorder's reader thread hands each frame to `MotionMonitor.submit()`, which only keeps the newest frame; a separate worker thread scores it with `MotionDetector` (numpy, adaptive background) and feeds `EventTracker`, which opens/closes rows in `EventStore` and saves the live JPEG at peak motion as the event snapshot. The housekeeper prunes non-event segments in `motion` mode and drops events whose footage is gone.

**Tech Stack:** As M2, plus `numpy`.

**Spec:** `docs/superpowers/specs/2026-10-31-security-camera-design.md` (§5 Motion detection, §5 Retention, §4 config)

## Global Constraints

- All M1/M2 constraints apply. New runtime dependency: `numpy` only (no OpenCV).
- Motion defaults (spec §4): `width: 320`, `height: 180`, `fps: 5`, `pixel_threshold: 25`, `area_percent: 1.0`, `min_frames: 2`, `cooldown_seconds: 10`, `padding_segments: 1`; `recording.mode: both`.
- Background: running average `bg = (1-α)·bg + α·frame`, α = 0.05, 3x3 box blur first.
- Recording must never block on motion processing: frames are dropped instead.
- In `continuous` mode no motion pipe is opened and no detection runs.
- UI text in Vietnamese. Motion markers use the amber accent; the playhead switches to the ink colour so the two never look alike.

### Additions beyond the spec (deliberate)

- Whole-scene changes (>60 % of pixels at once: lights, auto exposure) re-seed the background and score 0.
- After >2 s without frames (ffmpeg restart) the background is reset.
- Snapshot = live JPEG at the event's peak score (rate-limited to 1/s), not only at the start.
- `scripts/motion_scores.py` prints score statistics for recorded files, for tuning on real footage.

## File Structure

| File | Change |
|---|---|
| `raspcam/config.py` | + `MODES`, `RecordingConfig.mode`, `MotionConfig` |
| `raspcam/db.py` | `_Store` base; + `Event`, `EventStore` |
| `raspcam/motion.py` | new: `MotionDetector`, `EventTracker`, `MotionMonitor` |
| `raspcam/ffmpeg_cmd.py` | optional motion output on `pipe:<fd>` |
| `raspcam/recorder.py` | command factory, extra pipe, motion reader thread |
| `raspcam/retention.py` | + `plan_motion_prune`; housekeeper: mode, events cleanup |
| `raspcam/web.py` | + `/api/events`, `/events/{id}.jpg` |
| `raspcam/static/recordings.js`, `app.css`, templates | markers, event list, live badge |
| `raspcam/main.py` | wiring, status `motion` block |
| `scripts/motion_scores.py` | tuning helper |
| `pyproject.toml`, `config.example.yaml`, `README.md` | numpy, docs |

---

### Task 1: Config — recording mode and motion section

**Files:** Modify `raspcam/config.py`, `config.example.yaml`; Test `tests/test_config.py`

**Interfaces — Produces:** `MODES = ("continuous", "motion", "both")`; `RecordingConfig.mode: str = "both"`; `MotionConfig(width=320, height=180, fps=5, pixel_threshold=25, area_percent=1.0, min_frames=2, cooldown_seconds=10.0, padding_segments=1)`; `Config.motion`.

- [ ] **Step 1: Failing tests** (append to `tests/test_config.py`)

```python
def test_motion_defaults_and_mode():
    cfg = parse_config(MINIMAL)
    m = cfg.motion
    assert cfg.recording.mode == "both"
    assert (m.width, m.height, m.fps, m.pixel_threshold) == (320, 180, 5, 25)
    assert (m.area_percent, m.min_frames, m.cooldown_seconds, m.padding_segments) == (1.0, 2, 10.0, 1)


def test_unknown_mode_is_an_error():
    with pytest.raises(ConfigError, match="mode"):
        parse_config({**MINIMAL, "recording": {"mode": "sometimes"}})


@pytest.mark.parametrize("key,value", [
    ("fps", 0), ("min_frames", 0), ("pixel_threshold", 0), ("pixel_threshold", 255),
    ("area_percent", 0), ("area_percent", 101), ("cooldown_seconds", 0), ("padding_segments", -1),
])
def test_invalid_motion_values(key, value):
    with pytest.raises(ConfigError, match=key):
        parse_config({**MINIMAL, "motion": {key: value}})
```

Change `test_unknown_top_level_sections_are_ignored` to use `{"notifications": {"telegram": "x"}}`.

- [ ] **Step 2:** `.venv/bin/pytest tests/test_config.py -q` → FAIL
- [ ] **Step 3: Implement** in `raspcam/config.py`:

```python
MODES = ("continuous", "motion", "both")
```

Add `mode` as the first field of `RecordingConfig`:

```python
    mode: str = "both"       # continuous | motion | both
```

New dataclass after `StorageConfig`:

```python
@dataclass(frozen=True)
class MotionConfig:
    width: int = 320
    height: int = 180
    fps: int = 5
    pixel_threshold: int = 25       # per-pixel change (0-255) that counts as "changed"
    area_percent: float = 1.0       # % of changed pixels for a frame to count as motion
    min_frames: int = 2             # consecutive motion frames to start an event
    cooldown_seconds: float = 10.0  # this long without motion ends the event
    padding_segments: int = 1       # motion mode: segments kept before/after each event
```

`Config` gains `motion: MotionConfig`; `parse_config` builds `motion=_section(MotionConfig, data.get("motion"), "motion")` and validates:

```python
    if cfg.recording.mode not in MODES:
        raise ConfigError(f"recording.mode must be one of {', '.join(MODES)}, got {cfg.recording.mode!r}")
    for name in ("width", "height", "fps", "min_frames"):
        _positive_int(f"motion.{name}", getattr(cfg.motion, name))
    m = cfg.motion
    if not isinstance(m.pixel_threshold, int) or not 1 <= m.pixel_threshold <= 254:
        raise ConfigError(f"motion.pixel_threshold must be 1..254, got {m.pixel_threshold!r}")
    _number("motion.area_percent", m.area_percent, minimum=0, inclusive=False)
    if m.area_percent > 100:
        raise ConfigError(f"motion.area_percent must be at most 100, got {m.area_percent!r}")
    _number("motion.cooldown_seconds", m.cooldown_seconds, minimum=0, inclusive=False)
    if not isinstance(m.padding_segments, int) or isinstance(m.padding_segments, bool) or m.padding_segments < 0:
        raise ConfigError(f"motion.padding_segments must be an integer >= 0, got {m.padding_segments!r}")
```

`config.example.yaml`: add `mode: both  # continuous | motion | both` as the first `recording` key, and append:

```yaml

motion:
  pixel_threshold: 25     # how much a pixel must change (0-255) to count
  area_percent: 1.0       # % of the picture that must change; raise if you get false alarms
  min_frames: 2           # consecutive frames with motion before an event starts
  cooldown_seconds: 10    # an event ends after this long without motion
  padding_segments: 1     # motion mode: minutes kept before/after each event
```

- [ ] **Step 4:** tests PASS. **Step 5:** commit `feat: recording mode and motion config`

---

### Task 2: Event store

**Files:** Modify `raspcam/db.py`; Test `tests/test_db.py`

**Interfaces — Produces:** `Event(id: int, start_ts: float, end_ts: float | None, peak: float)`; `EventStore(path)`: `open(start_ts) -> int`, `set_peak(event_id, peak)`, `close(event_id, end_ts)`, `get(event_id) -> Event | None`, `all() -> list[Event]` (oldest first), `by_day(day) -> list[Event]` (oldest first), `count_day(day) -> int`, `close_dangling() -> int`, `delete_ending_before(ts) -> list[int]` (closed events only). `SegmentStore` unchanged externally.

- [ ] **Step 1: Failing tests** (append to `tests/test_db.py`)

```python
from raspcam.db import EventStore


def test_event_lifecycle(tmp_path):
    ev = EventStore(tmp_path / "db.sqlite")
    start = ts("2026-11-01 10:00:00")
    eid = ev.open(start)
    assert ev.get(eid).end_ts is None
    ev.set_peak(eid, 0.12)
    ev.close(eid, start + 30)
    e = ev.get(eid)
    assert (e.start_ts, e.end_ts, e.peak) == (start, start + 30, 0.12)


def test_events_by_day_and_count(tmp_path):
    ev = EventStore(tmp_path / "db.sqlite")
    a = ev.open(ts("2026-10-31 23:59:00"))
    b = ev.open(ts("2026-11-01 08:00:00"))
    c = ev.open(ts("2026-11-01 07:00:00"))
    assert [e.id for e in ev.by_day("2026-11-01")] == [c, b]
    assert ev.count_day("2026-11-01") == 2
    assert [e.id for e in ev.all()] == [a, c, b]


def test_close_dangling_and_delete_before(tmp_path):
    ev = EventStore(tmp_path / "db.sqlite")
    old = ev.open(100.0)
    ev.close(old, 110.0)
    dangling = ev.open(200.0)
    assert ev.close_dangling() == 1
    assert ev.get(dangling).end_ts == 200.0
    still_open = ev.open(300.0)
    assert ev.delete_ending_before(150.0) == [old]
    assert ev.get(old) is None and ev.get(still_open) is not None


def test_segments_and_events_share_a_database(tmp_path):
    path = tmp_path / "db.sqlite"
    store, ev = SegmentStore(path), EventStore(path)
    store.add(seg("2026-11-01 10:00:00"))
    ev.open(ts("2026-11-01 10:00:30"))
    assert store.total_size() == 100 and ev.count_day("2026-11-01") == 1
```

- [ ] **Step 2:** FAIL. **Step 3: Implement** — restructure `raspcam/db.py`:

```python
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

    # add/remove/get/names/oldest_first/days/by_day/total_size: unchanged bodies,
    # with COLUMNS renamed to SEGMENT_COLUMNS and the day computed by local_day().


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
```

(The `SegmentStore` method bodies stay exactly as in M2; only `COLUMNS` → `SEGMENT_COLUMNS`, `self._write` now returns the cursor, and `add()` uses `local_day(seg.start_ts)`.)

- [ ] **Step 4:** `.venv/bin/pytest tests/test_db.py -q` PASS. **Step 5:** commit `feat: motion event store`

---

### Task 3: Motion detector and event tracker (pure logic)

**Files:** Create `raspcam/motion.py`; Test `tests/test_motion.py`; Modify `pyproject.toml` (add `"numpy>=1.26"` to dependencies, then `.venv/bin/pip install -q -e ".[dev]"`)

**Interfaces — Produces:** `MotionDetector(width, height, pixel_threshold, alpha=0.05)` with `score(frame: bytes) -> float` (0..1) and `reset()`; `EventTracker(min_frames, cooldown)` with `update(ts, moving) -> tuple[str, float] | None` (`("start", start_ts)` / `("end", end_ts)`), attributes `active: bool`, `last_motion: float | None`; constants `LIGHTING_CHANGE = 0.6`, `GAP_RESET = 2.0`.

- [ ] **Step 1: Failing tests** — `tests/test_motion.py`

```python
import numpy as np

from raspcam.motion import EventTracker, MotionDetector

W, H = 320, 180


def frame(value=100, block=None):
    img = np.full((H, W), value, dtype=np.uint8)
    if block:
        x, y = block
        img[y:y + 40, x:x + 40] = 220
    return img.tobytes()


def test_static_scene_scores_zero():
    d = MotionDetector(W, H, pixel_threshold=25)
    assert [d.score(frame()) for _ in range(5)] == [0.0] * 5


def test_moving_block_is_motion():
    d = MotionDetector(W, H, pixel_threshold=25)
    d.score(frame())
    scores = [d.score(frame(block=(20 + 15 * i, 70))) for i in range(5)]
    assert all(s > 0.01 for s in scores)


def test_slow_lighting_change_is_not_motion():
    d = MotionDetector(W, H, pixel_threshold=25)
    assert max(d.score(frame(value=60 + i)) for i in range(120)) == 0.0


def test_sensor_noise_is_not_motion():
    rng = np.random.default_rng(1)
    d = MotionDetector(W, H, pixel_threshold=25)
    base = np.full((H, W), 100, dtype=np.int16)
    for _ in range(10):
        noisy = np.clip(base + rng.integers(-6, 7, size=(H, W)), 0, 255).astype(np.uint8)
        assert d.score(noisy.tobytes()) < 0.001


def test_sudden_whole_scene_change_reseeds_background():
    d = MotionDetector(W, H, pixel_threshold=25)
    d.score(frame(value=40))
    assert d.score(frame(value=160)) == 0.0   # lights on
    assert d.score(frame(value=160)) == 0.0


def test_reset_forgets_background():
    d = MotionDetector(W, H, pixel_threshold=25)
    d.score(frame())
    d.reset()
    assert d.score(frame(block=(50, 50))) == 0.0


def test_tracker_needs_consecutive_frames_and_cools_down():
    t = EventTracker(min_frames=2, cooldown=10)
    assert t.update(0.0, True) is None
    assert t.update(0.2, False) is None          # streak broken
    assert t.update(1.0, True) is None
    assert t.update(1.2, True) == ("start", 1.0)
    assert t.active
    assert t.update(5.0, True) is None
    assert t.update(14.9, False) is None         # 9.9 s since last motion
    assert t.update(15.0, False) == ("end", 5.0)
    assert not t.active
```

- [ ] **Step 2:** FAIL. **Step 3: Implement** — `raspcam/motion.py` (first part; Task 4 appends `MotionMonitor`):

```python
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
```

- [ ] **Step 4:** `.venv/bin/pytest tests/test_motion.py -q` PASS. **Step 5:** commit `feat: motion detector and event tracker`

---

### Task 4: MotionMonitor (worker thread, events, snapshots)

**Files:** Modify `raspcam/motion.py`; Test `tests/test_motion.py`

**Interfaces — Consumes:** `MotionConfig`, `EventStore`, `LatestFrame`. **Produces:** `MotionMonitor(cfg, events, latest, snapshots_dir)` with `submit(frame, ts)` (non-blocking, keeps newest), `process(frame, ts)` (synchronous, used by the worker and tests), `start()`, `stop()`, properties `active: bool`, attribute `score: float`. Snapshot path: `snapshots_dir / f"{event_id}.jpg"`.

- [ ] **Step 1: Failing tests** (append)

```python
import time

from raspcam.config import MotionConfig
from raspcam.db import EventStore
from raspcam.frames import LatestFrame
from raspcam.motion import MotionMonitor


def monitor(tmp_path, **kw):
    events = EventStore(tmp_path / "db.sqlite")
    latest = LatestFrame()
    latest.set(b"\xff\xd8snap\xff\xd9")
    snaps = tmp_path / "events"
    snaps.mkdir()
    cfg = MotionConfig(**{"min_frames": 2, "cooldown_seconds": 3, **kw})
    return MotionMonitor(cfg, events, latest, snaps), events, snaps


def run_scene(m, t0=1000.0):
    """1 s still, 2 s of a moving block, 5 s still (5 fps)."""
    t = t0
    for i in range(40):
        moving = 5 <= i < 15
        m.process(frame(block=(10 + 20 * (i - 5), 60)) if moving else frame(), t)
        t += 0.2
    return t


def test_monitor_records_an_event_with_snapshot(tmp_path):
    m, events, snaps = monitor(tmp_path)
    run_scene(m)
    [e] = events.all()
    assert abs(e.start_ts - 1001.0) < 1e-6 and abs(e.end_ts - 1002.8) < 1e-6
    assert e.peak > 0.01
    assert (snaps / f"{e.id}.jpg").read_bytes() == b"\xff\xd8snap\xff\xd9"
    assert not m.active


def test_stop_closes_an_open_event(tmp_path):
    m, events, _ = monitor(tmp_path, cooldown_seconds=60)
    run_scene(m)
    assert events.all()[0].end_ts is None
    m.stop()
    assert events.all()[0].end_ts is not None


def test_gap_in_frames_resets_background(tmp_path):
    m, events, _ = monitor(tmp_path)
    m.process(frame(), 0.0)
    m.process(frame(block=(100, 60)), 10.0)   # first frame after a restart: new background
    assert m.score == 0.0


def test_worker_thread_processes_submitted_frames(tmp_path):
    m, events, _ = monitor(tmp_path)
    m.start()
    try:
        m.submit(frame(), 0.0)
        deadline = time.monotonic() + 2
        while m.frames_seen < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert m.frames_seen == 1
    finally:
        m.stop()
```

- [ ] **Step 2:** FAIL. **Step 3: Implement** — append to `raspcam/motion.py` (and add imports `logging`, `threading`, `Path`, `MotionConfig`, `EventStore`, `LatestFrame`, `log = logging.getLogger(__name__)` at the top):

```python
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
```

- [ ] **Step 4:** PASS. **Step 5:** commit `feat: motion monitor with events and snapshots`

---

### Task 5: Motion output from ffmpeg into the recorder

**Files:** Modify `raspcam/ffmpeg_cmd.py`, `raspcam/recorder.py`; Test `tests/test_ffmpeg_cmd.py`, `tests/test_recorder.py`

**Interfaces — Produces:** `build_command(cfg, segments_dir, motion_fd: int | None = None)`; `Recorder(command: list[str] | Callable[[int | None], list[str]], latest, *, on_motion_frame: Callable[[bytes, float], None] | None = None, motion_frame_bytes: int = 0, backoff_initial=..., backoff_max=..., stable_after=...)`.

- [ ] **Step 1: Failing tests**

`tests/test_ffmpeg_cmd.py` (append):

```python
def test_motion_output_on_extra_fd():
    argv = build_command(make_cfg("/dev/video0"), SEG, motion_fd=7)
    assert argv[argv.index("-filter_complex") + 1] == (
        "[0:v]split=3[r][l][m];[r]fps=10[rec];[l]fps=5,scale=1280:720[live];"
        "[m]fps=5,scale=320:180,format=gray[motion]")
    assert argv[-7:] == ["-map", "[motion]", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:7"]


def test_no_motion_output_without_fd():
    argv = build_command(make_cfg("/dev/video0"), SEG)
    assert "[motion]" not in " ".join(argv) and argv[-1] == "pipe:1"
```

`tests/test_recorder.py` (append):

```python
def test_motion_frames_arrive_through_the_extra_pipe(latest):
    got = []
    script = ("import os, sys\n"
              "f = os.fdopen(int(sys.argv[1]), 'wb')\n"
              "f.write(b'a' * 6 + b'b' * 6 + b'c' * 3)\n"
              "f.flush()\n")
    rec = Recorder(lambda fd: [sys.executable, "-c", script, str(fd)], latest,
                   on_motion_frame=lambda frame, ts: got.append(frame), motion_frame_bytes=6)
    rec.start()
    try:
        assert wait_until(lambda: len(got) == 2)
        assert got == [b"a" * 6, b"b" * 6]   # trailing partial frame is dropped
    finally:
        rec.stop()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_motion_frames(latest, tmp_path):
    cfg = small_cfg()
    got = []
    size = cfg.motion.width * cfg.motion.height
    rec = Recorder(lambda fd: build_command(cfg, tmp_path, fd), latest,
                   on_motion_frame=lambda frame, ts: got.append(len(frame)), motion_frame_bytes=size)
    rec.start()
    try:
        assert wait_until(lambda: len(got) >= 3, timeout=10)
        assert set(got) == {size}
    finally:
        rec.stop()
```

and extend `small_cfg()` with `"motion": {"width": 64, "height": 36, "fps": 5}`.

- [ ] **Step 2:** FAIL. **Step 3: Implement**

`raspcam/ffmpeg_cmd.py` — replace `build_command`:

```python
def build_command(cfg: Config, segments_dir: Path, motion_fd: int | None = None) -> list[str]:
    """ffmpeg argv: H.264 segments into segments_dir, live MJPEG on stdout, and
    (if motion_fd is given) small gray frames for motion detection on that fd."""
    rec, live, mo = cfg.recording, cfg.live, cfg.motion
    branches = ["[r]", "[l]"] + (["[m]"] if motion_fd is not None else [])
    graph = [f"[0:v]split={len(branches)}{''.join(branches)}",
             f"[r]fps={rec.fps}[rec]",
             f"[l]fps={live.fps},scale={live.width}:{live.height}[live]"]
    if motion_fd is not None:
        graph.append(f"[m]fps={mo.fps},scale={mo.width}:{mo.height},format=gray[motion]")
    argv = [
        # "error": swscale prints a harmless "deprecated pixel format" warning for
        # MJPEG input that would otherwise show up as last_error in the UI
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        *_input_args(cfg),
        "-filter_complex", ";".join(graph),
        "-map", "[rec]",
        "-c:v", "libx264", "-preset", rec.preset, "-crf", str(rec.crf),
        "-pix_fmt", "yuv420p", "-g", str(rec.fps * rec.keyframe_seconds),
        "-f", "segment", "-segment_time", str(rec.segment_seconds),
        "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
        "-segment_format_options", "movflags=+faststart",
        str(segments_dir / SEGMENT_PATTERN),
        "-map", "[live]", "-c:v", "mjpeg", "-q:v", str(live.quality),
        "-f", "mjpeg", "pipe:1",
    ]
    if motion_fd is not None:
        argv += ["-map", "[motion]", "-f", "rawvideo", "-pix_fmt", "gray", f"pipe:{motion_fd}"]
    return argv
```

`raspcam/recorder.py` — add `import os`, `from typing import Callable`; constructor:

```python
    def __init__(self, command: list[str] | Callable[[int | None], list[str]],
                 latest: LatestFrame, *,
                 on_motion_frame: Callable[[bytes, float], None] | None = None,
                 motion_frame_bytes: int = 0,
                 backoff_initial: float = 1.0, backoff_max: float = 30.0,
                 stable_after: float = 60.0):
        self._command = command  # a factory gets the motion pipe's fd (or None)
        self._on_motion = on_motion_frame
        self._motion_bytes = motion_frame_bytes
        ...rest unchanged...
```

`_run_once` start:

```python
    def _run_once(self) -> None:
        motion_r = motion_w = None
        if self._on_motion:
            motion_r, motion_w = os.pipe()
        argv = self._command(motion_w) if callable(self._command) else self._command
        try:
            # (existing comment about the session)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True,
                                    pass_fds=(motion_w,) if motion_w is not None else ())
        except OSError as e:
            if motion_r is not None:
                os.close(motion_r)
            self._stderr_tail.append(str(e))
            log.error("cannot start ffmpeg: %s", e)
            return
        finally:
            if motion_w is not None:
                os.close(motion_w)  # the child has its own copy; EOF reaches us when it exits
```

Readers: replace the single stderr thread with a list:

```python
        readers = [threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True)]
        if motion_r is not None:
            readers.append(threading.Thread(target=self._read_motion, args=(motion_r,), daemon=True))
        for t in readers:
            t.start()
```

and in `finally` join each with `timeout=1`. New method:

```python
    def _read_motion(self, fd: int) -> None:
        with open(fd, "rb") as stream:
            while len(frame := stream.read(self._motion_bytes)) == self._motion_bytes:
                try:
                    self._on_motion(frame, time.time())
                except Exception:  # never stop draining, or ffmpeg would block
                    log.exception("motion frame handler failed")
```

- [ ] **Step 4:** `.venv/bin/pytest tests/test_ffmpeg_cmd.py tests/test_recorder.py -q` PASS. **Step 5:** commit `feat: motion frames from ffmpeg on a dedicated pipe`

---

### Task 6: Retention — motion mode and event cleanup

**Files:** Modify `raspcam/retention.py`; Test `tests/test_retention.py`

**Interfaces — Produces:** `plan_motion_prune(segments, events, padding, now) -> list[str]`; `Housekeeper(..., *, events: EventStore | None = None, mode: str = "both", padding_seconds: float = 60.0, events_dir: Path | None = None, clock=time.time, interval, disk_free, thumbs_dir)`.

- [ ] **Step 1: Failing tests** (append)

```python
from raspcam.db import Event, EventStore
from raspcam.retention import plan_motion_prune


def minute(i, size=10):
    return Segment(f"m{i:02d}.mp4", i * 60.0, i * 60.0 + 60, size)


def test_motion_prune_keeps_event_minutes_with_padding():
    segs = [minute(i) for i in range(10)]
    events = [Event(1, 4 * 60 + 10, 4 * 60 + 20, 0.1)]   # inside minute 4
    doomed = plan_motion_prune(segs, events, padding=60, now=10_000)
    assert doomed == [s.name for s in segs if s.name not in ("m03.mp4", "m04.mp4", "m05.mp4")]


def test_motion_prune_keeps_recent_segments_and_open_events():
    segs = [minute(i) for i in range(10)]
    events = [Event(1, 2 * 60 + 5, None, 0.1)]            # still going
    doomed = plan_motion_prune(segs, events, padding=60, now=9 * 60 + 30)
    assert doomed == ["m00.mp4"]   # m01 is padding before the event; m08, m09 are too recent


def test_housekeeper_motion_mode_and_event_cleanup(tmp_path):
    db = tmp_path / "db.sqlite"
    store, events = SegmentStore(db), EventStore(db)
    snaps = tmp_path / "events"
    snaps.mkdir()
    for i in range(6):
        store.add(minute(i))
        (tmp_path / minute(i).name).write_bytes(b"x")
    old = events.open(-500.0)
    events.close(old, -400.0)
    (snaps / f"{old}.jpg").write_bytes(b"j")
    keep = events.open(2 * 60 + 10)
    events.close(keep, 2 * 60 + 15)
    hk = Housekeeper(store, FakeIndexer(), tmp_path, StorageConfig(quota_gb=1, min_free_gb=0),
                     writing=lambda: True, disk_free=lambda p: 100 * GB,
                     events=events, mode="motion", padding_seconds=60, events_dir=snaps,
                     clock=lambda: 10_000)
    hk.run_once()
    assert sorted(store.names()) == ["m01.mp4", "m02.mp4", "m03.mp4"]
    assert not (tmp_path / "m00.mp4").exists()
    assert events.get(old) is None and not (snaps / f"{old}.jpg").exists()
    assert events.get(keep) is not None


def test_housekeeper_both_mode_keeps_all_segments(tmp_path):
    db = tmp_path / "db.sqlite"
    store, events = SegmentStore(db), EventStore(db)
    for i in range(3):
        store.add(minute(i))
    hk = Housekeeper(store, FakeIndexer(), tmp_path, StorageConfig(quota_gb=1, min_free_gb=0),
                     writing=lambda: True, disk_free=lambda p: 100 * GB,
                     events=events, mode="both", clock=lambda: 10_000)
    hk.run_once()
    assert len(store.names()) == 3
```

- [ ] **Step 2:** FAIL. **Step 3: Implement** in `raspcam/retention.py` (add `import time`, `from .db import Event, EventStore, Segment, SegmentStore`):

```python
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
```

Housekeeper constructor gains (keyword-only) `events`, `mode`, `padding_seconds`, `events_dir`, `clock`, stored as `self._events`, `self._mode`, `self._padding`, `self._events_dir`, `self._clock`. `run_once` becomes:

```python
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
```

- [ ] **Step 4:** `.venv/bin/pytest tests/test_retention.py -q` PASS. **Step 5:** commit `feat: motion-mode retention and event cleanup`

---

### Task 7: Web API for events

**Files:** Modify `raspcam/web.py`; Test `tests/test_web.py`

**Interfaces — Produces:** `create_app(..., *, thumbs_dir=None, events: EventStore | None = None, events_dir: Path | None = None, on_shutdown=None)`; `GET /api/events?day=` → `[{id, start, end, peak}]` (`end` null while open); `GET /events/{event_id}.jpg`.

- [ ] **Step 1: Failing tests** (append; add an `events_dir` fixture and pass `events=EventStore(tmp_path / "db.sqlite"), events_dir=...` in the `client` fixture, exposing the store via an `events` fixture)

```python
@pytest.fixture
def events(tmp_path):
    from raspcam.db import EventStore
    return EventStore(tmp_path / "db.sqlite")


@pytest.fixture
def events_dir(tmp_path):
    d = tmp_path / "events"
    d.mkdir()
    return d


def test_events_api(client, events, events_dir):
    start = time.mktime(time.strptime("2026-11-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
    done = events.open(start)
    events.close(done, start + 12)
    running = events.open(start + 100)
    assert client.get("/api/events?day=2026-11-01").status_code == 401
    login(client)
    got = client.get("/api/events", params={"day": "2026-11-01"}).json()
    assert [(e["id"], e["end"]) for e in got] == [(done, start + 12), (running, None)]


def test_event_snapshot(client, events, events_dir):
    eid = events.open(1.0)
    (events_dir / f"{eid}.jpg").write_bytes(b"\xff\xd8x\xff\xd9")
    assert client.get(f"/events/{eid}.jpg").status_code == 401
    login(client)
    r = client.get(f"/events/{eid}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get("/events/999.jpg").status_code == 404
```

The `client` fixture becomes:

```python
@pytest.fixture
def client(latest, store, seg_dir, thumbs_dir, events, events_dir):
    cfg = parse_config({
        "camera": {"device": "testsrc"},
        "web": {"password_hash": hash_password("secret"), "secret_key": "k" * 32},
    })
    return TestClient(create_app(cfg, latest, lambda: STATUS, store, seg_dir,
                                 thumbs_dir=thumbs_dir, events=events, events_dir=events_dir))
```

- [ ] **Step 2:** FAIL. **Step 3: Implement** — `create_app` keyword params `events: EventStore | None = None, events_dir: Path | None = None` (import `EventStore`), and routes:

```python
    @app.get("/api/events")
    def events_api(request: Request, day: str):
        if not logged_in(request):
            return Response(status_code=401)
        if events is None:
            return []
        return [{"id": e.id, "start": e.start_ts, "end": e.end_ts, "peak": e.peak}
                for e in events.by_day(day)]

    @app.get("/events/{event_id}.jpg")
    def event_snapshot(request: Request, event_id: int):
        if not logged_in(request):
            return Response(status_code=401)
        path = events_dir / f"{event_id}.jpg" if events_dir else None
        if path is None or not path.is_file():
            return Response(status_code=404)
        # short cache: the snapshot is replaced while the event is still going
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})
```

- [ ] **Step 4:** PASS. **Step 5:** commit `feat: motion events API and snapshots`

---

### Task 8: UI — timeline markers, event list, live badge

**Files:** Modify `raspcam/static/app.css`, `raspcam/static/recordings.js`, `raspcam/templates/recordings.html`, `raspcam/templates/live.html`

No unit tests (template smoke tests stay green); verified with Playwright screenshots in Task 9.

- [ ] **Step 1: CSS** — in `app.css`: change `.playhead` background to `var(--ink)`; add:

```css
.event { position: absolute; top: 0; bottom: 0; min-width: 3px; background: var(--amber); }
.track-day .event { top: -3px; bottom: -3px; border-radius: 1px; }

.events { margin-top: 26px; }
.events-nav { display: flex; gap: 6px; }
.events-nav button { min-height: 34px; padding: 5px 10px; }
.event-list { list-style: none; margin: 8px 0 0; padding: 0;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px 10px; }
.event-item { display: block; width: 100%; padding: 0; border: 0; min-height: 0; text-align: left; }
.event-item img, .event-item .blank { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover;
  border-radius: 6px; background: var(--track-empty); outline: 2px solid transparent; outline-offset: -2px; }
.event-item:hover img { outline-color: var(--muted); }
.event-item[aria-current="true"] img { outline-color: var(--amber); }
.event-item strong { display: block; margin-top: 4px; font-size: 14px; }
.event-item span { color: var(--muted); font-size: 13px; }
.events-empty { color: var(--muted); margin: 8px 0 0; }

.osd-motion { background: var(--amber); color: var(--on-amber); text-shadow: none;
  font-size: .75em; padding: 2px 8px; border-radius: 4px; }
```

- [ ] **Step 2: recordings.html** — after the `.timeline` section, add:

```html
    <section class="events" id="events" aria-label="Chuyển động" hidden>
      <div class="track-head">
        <h2 id="events-title">Chuyển động</h2>
        <div class="events-nav">
          <button id="prev-event"><svg viewBox="0 0 24 24"><path d="M15.4 7.4 14 6l-6 6 6 6 1.4-1.4L10.8 12z"/></svg>Lần trước</button>
          <button id="next-event">Lần sau<svg viewBox="0 0 24 24"><path d="M8.6 16.6 10 18l6-6-6-6-1.4 1.4 4.6 4.6z"/></svg></button>
        </div>
      </div>
      <p class="events-empty" id="events-empty" hidden>Không phát hiện chuyển động nào trong ngày này.</p>
      <ol class="event-list" id="event-list"></ol>
    </section>
```

- [ ] **Step 3: recordings.js**
  - State: `events: []`, `motionEnabled: false`.
  - `loadDay`: fetch both `const [segs, events] = await Promise.all([api("/api/segments?day=…"), api("/api/events?day=…")])`; assign; call `drawEventList()`.
  - `refreshDay`: fetch both; always replace `st.events` (an open event may have closed) and redraw markers + list when the JSON differs; keep the existing early-return only for segments.
  - Rename `drawRanges` → `drawSpans`; after drawing ranges, draw events: remove `.event`, then for each event overlapping `[from, from+span]` create `.event` with left/width from `start`..`end ?? Date.now()/1000`, inserted before the playhead.
  - `drawEventList()`:

```js
function eventDuration(e) {
  const secs = Math.max(1, Math.round((e.end ?? Date.now() / 1000) - e.start));
  if (e.end === null) return "đang diễn ra";
  return secs < 60 ? `${secs} giây` : `${Math.floor(secs / 60)} phút ${secs % 60} giây`;
}

function drawEventList() {
  const box = $("event-list");
  box.textContent = "";
  $("events").hidden = !st.motionEnabled && !st.events.length;
  $("events-empty").hidden = st.events.length > 0;
  $("events-title").textContent = st.events.length ? `Chuyển động (${st.events.length} lần)` : "Chuyển động";
  for (const e of [...st.events].reverse()) {          // newest first
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.className = "event-item";
    b.dataset.start = e.start;
    b.dataset.end = e.end ?? Infinity;
    const img = new Image();
    img.loading = "lazy";
    img.alt = "";
    img.src = `/events/${e.id}.jpg`;
    img.onerror = () => img.replaceWith(Object.assign(document.createElement("i"), {className: "blank"}));
    b.append(img,
      Object.assign(document.createElement("strong"), {textContent: hms(e.start)}),
      Object.assign(document.createElement("span"), {textContent: eventDuration(e)}));
    b.onclick = () => seekTo(e.start - 3);
    li.append(b);
    box.append(li);
  }
}

function jumpEvent(dir) {
  const t = now();
  const e = dir < 0 ? [...st.events].reverse().find(e => e.start < t - 1)
                    : st.events.find(e => e.start - 3 > t + 1);
  if (e) seekTo(e.start - 3);
  else toast(dir < 0 ? "Không có chuyển động nào trước đó trong ngày." : "Không có chuyển động nào sau đó trong ngày.");
}
```

  - Wire `$("prev-event").onclick = () => jumpEvent(-1)` and `$("next-event").onclick = () => jumpEvent(1)`; keyboard `,` → `jumpEvent(-1)`, `.` → `jumpEvent(1)`; extend the hint: `<kbd>,</kbd> <kbd>.</kbd> chuyển động trước hoặc sau`.
  - `render()`: mark `.event-item` with `aria-current` when `start - 3 <= t <= end + 3`.
  - `init()`: `st.motionEnabled = (await api("/api/status")).motion?.enabled ?? false;` before loading the day.

- [ ] **Step 4: live.html** — in `.osd` add `<span class="osd-motion" id="motion-badge" hidden>Có chuyển động</span>`; in `.facts` add `<div><dt>Chuyển động hôm nay</dt><dd id="f-motion">–</dd></div>`; in `refresh()`:

```js
      const mo = s.motion || {enabled: false};
      $("motion-badge").hidden = !(mo.enabled && mo.active);
      $("f-motion").textContent = mo.enabled ? `${mo.events_today} lần` : "Đang tắt";
```

and poll every 2 s instead of 3 s.

- [ ] **Step 5:** `.venv/bin/pytest -q` PASS. Commit `feat: motion markers, event list, and live motion badge`

---

### Task 9: Wiring, tuning helper, deploy, verify

**Files:** Modify `raspcam/main.py`, `README.md`; Create `scripts/motion_scores.py`

- [ ] **Step 1: `raspcam/main.py`** — after creating `store`:

```python
    events = EventStore(data_dir / "raspcam.db")
    events.close_dangling()
    events_dir = data_dir / "events"
    events_dir.mkdir(exist_ok=True)
    detect = cfg.recording.mode != "continuous"

    latest = LatestFrame()
    monitor = MotionMonitor(cfg.motion, events, latest, events_dir) if detect else None
    recorder = Recorder(lambda fd: build_command(cfg, segments_dir, fd), latest,
                        on_motion_frame=monitor.submit if monitor else None,
                        motion_frame_bytes=cfg.motion.width * cfg.motion.height)
    housekeeper = Housekeeper(store, Indexer(store, segments_dir, thumbs_dir=thumbs_dir),
                              segments_dir, cfg.storage, writing=lambda: recorder.running,
                              thumbs_dir=thumbs_dir, events=events, mode=cfg.recording.mode,
                              padding_seconds=cfg.motion.padding_segments * cfg.recording.segment_seconds,
                              events_dir=events_dir)

    def status() -> dict:
        return {**recorder.status(),
                "motion": {"enabled": detect,
                           "active": bool(monitor and monitor.active),
                           "events_today": events.count_day(local_day(time.time()))}}
```

`housekeeper.run_once()`, then `if monitor: monitor.start()`, `recorder.start()`, `housekeeper.start()`. `shutdown()` stops housekeeper, recorder, then `monitor.stop()` (closes an open event). `create_app(cfg, latest, status, store, segments_dir, thumbs_dir=thumbs_dir, events=events, events_dir=events_dir, on_shutdown=shutdown)`. Imports: `time`, `EventStore`, `local_day`, `MotionMonitor`.

- [ ] **Step 2: `scripts/motion_scores.py`**

```python
"""Print motion scores for recorded segments, to tune the motion settings.

Usage (on the Pi):  .venv/bin/python scripts/motion_scores.py ~/raspcam-data/segments/2026*.mp4
"""
import argparse
import subprocess

from raspcam.config import load_config
from raspcam.motion import MotionDetector


def scores(path, m):
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-vf", f"fps={m.fps},scale={m.width}:{m.height},format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], stdout=subprocess.PIPE)
    detector = MotionDetector(m.width, m.height, m.pixel_threshold)
    size = m.width * m.height
    while len(frame := proc.stdout.read(size)) == size:
        yield detector.score(frame)
    proc.wait()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    m = load_config(args.config).motion
    for f in args.files:
        s = sorted(scores(f, m))
        if not s:
            print(f"{f}: no frames")
            continue
        pct = lambda q: s[min(len(s) - 1, int(q * len(s)))] * 100
        moving = sum(v * 100 >= m.area_percent for v in s)
        print(f"{f}: median {pct(.5):.2f}%  p95 {pct(.95):.2f}%  max {s[-1] * 100:.2f}%  "
              f"frames over area_percent: {moving}/{len(s)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3:** README: modes, motion settings, `scripts/motion_scores.py`.
- [ ] **Step 4:** `.venv/bin/pytest -q` all PASS; commit `feat: wire motion detection`.
- [ ] **Step 5: Deploy + verify on the Pi**
  - `scripts/deploy.sh` → `active` (installs numpy).
  - Tune: run `scripts/motion_scores.py` over ~20 recorded segments (quiet periods and periods with someone in the room); confirm quiet minutes stay below `area_percent` and busy ones exceed it. Adjust `motion.area_percent` / `pixel_threshold` in the Pi `config.yaml` if not.
  - Live: `/api/status` shows `motion.enabled: true`; `top` shows ffmpeg + python CPU (expect python < 10 % of a core); temperature.
  - Events: after someone moves in front of the camera, `/api/events` lists an event with a snapshot; screenshots of the live badge, timeline markers, and event list (desktop + mobile + dark).
  - `systemctl restart raspcam` during an event: the event is closed, not left open.
- [ ] **Step 6:** push.
