# RaspCam Milestone 2 (Recording + Playback) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously record H.264 1080p@10fps in 60 s MP4 segments, index them in SQLite, enforce a storage quota, and play recordings back in the web UI.

**Architecture:** The single ffmpeg process gains a second output via `-filter_complex split`: libx264 into the segment muxer (files named by local start time). A `Housekeeper` thread periodically scans the segment directory with an `Indexer` (probe finished files with ffprobe, add to `SegmentStore`, drop rows for missing files, delete unreadable files), then applies `plan_deletions` (oldest first until under quota and above min free). The web app adds a recordings page, segment/day JSON APIs, and range-capable MP4 serving.

**Tech Stack:** As M1, plus `sqlite3` (stdlib) and `ffprobe`.

**Spec:** `docs/superpowers/specs/2026-10-31-security-camera-design.md`

## Global Constraints

- All M1 constraints still apply (Python ≥3.11, no new runtime deps, Vietnamese UI, `testsrc` device for tests).
- Recording defaults: `fps: 10`, `crf: 28`, `preset: veryfast`, `segment_seconds: 60`, keyframe every 2 s.
  Measured on the Pi (same 30 s clip): veryfast/crf28 = 129 % of one core, 1327 kbps ≈ 14.3 GB/day.
- Storage defaults: `path: ~/raspcam-data`, `quota_gb: 5` (decimal GB), `min_free_gb: 1`.
- Segment file name: `%Y%m%d-%H%M%S.mp4` in local time (Pi TZ Asia/Ho_Chi_Minh), flat directory `<storage.path>/segments/`. DB at `<storage.path>/raspcam.db`.

### Deviations from spec (deliberate)

- Flat segment directory instead of per-day folders: ffmpeg's segment muxer does not create directories, so per-day folders would break at midnight.
- Segments are discovered by scanning the directory instead of tailing `-segment_list`; the same scan does power-loss reconciliation.
- Quota / min-free retention is pulled forward from M4: at ~15 GB/day the microSD would fill in about a day. Motion-mode pruning stays in M4.

## File Structure

| File | Change |
|---|---|
| `raspcam/config.py` | + `RecordingConfig`, `StorageConfig` |
| `raspcam/segments.py` | new: name pattern, `parse_start`, `probe_duration`, `Indexer` |
| `raspcam/db.py` | new: `Segment`, `SegmentStore` (SQLite) |
| `raspcam/retention.py` | new: `plan_deletions`, `Housekeeper` |
| `raspcam/ffmpeg_cmd.py` | `build_command(cfg, segments_dir)` with two outputs |
| `raspcam/web.py` | recordings page, `/api/days`, `/api/segments`, `/media/{name}`, storage in status |
| `raspcam/templates/nav.html` | new shared header |
| `raspcam/templates/recordings.html` | new playback page |
| `raspcam/templates/live.html`, `base.html` | use nav, storage in status line, segment list styles |
| `raspcam/main.py` | wire store, indexer, housekeeper |
| `config.example.yaml`, `README.md` | document new sections |

---

### Task 1: Config sections for recording and storage

**Files:**
- Modify: `raspcam/config.py`, `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `RecordingConfig(fps=10, crf=28, preset="veryfast", segment_seconds=60, keyframe_seconds=2)`, `StorageConfig(path="~/raspcam-data", quota_gb=5.0, min_free_gb=1.0)`; `Config.recording`, `Config.storage`.

- [ ] **Step 1: Add failing tests** (append to `tests/test_config.py`)

```python
def test_recording_and_storage_defaults():
    cfg = parse_config(MINIMAL)
    r, s = cfg.recording, cfg.storage
    assert (r.fps, r.crf, r.preset, r.segment_seconds, r.keyframe_seconds) == (10, 28, "veryfast", 60, 2)
    assert (s.path, s.quota_gb, s.min_free_gb) == ("~/raspcam-data", 5.0, 1.0)


@pytest.mark.parametrize("section,key,value", [
    ("recording", "fps", 0),
    ("recording", "segment_seconds", -5),
    ("recording", "crf", 60),
    ("storage", "quota_gb", 0),
    ("storage", "min_free_gb", -1),
])
def test_invalid_recording_and_storage_values(section, key, value):
    with pytest.raises(ConfigError, match=key):
        parse_config({**MINIMAL, section: {key: value}})


def test_keyframe_interval_must_fit_in_segment():
    with pytest.raises(ConfigError, match="keyframe_seconds"):
        parse_config({**MINIMAL, "recording": {"segment_seconds": 1, "keyframe_seconds": 2}})


def test_float_quota_is_accepted():
    assert parse_config({**MINIMAL, "storage": {"quota_gb": 0.5}}).storage.quota_gb == 0.5
```

Also change `test_unknown_top_level_sections_are_ignored` to use `{"motion": {"fps": 5}}` instead of `recording`.

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_config.py -q` → FAIL (`Config` has no attribute `recording`)

- [ ] **Step 3: Implement** — add to `raspcam/config.py` after `WebConfig`:

```python
@dataclass(frozen=True)
class RecordingConfig:
    fps: int = 10
    crf: int = 28            # x264 quality: lower is better and bigger
    preset: str = "veryfast"
    segment_seconds: int = 60
    keyframe_seconds: int = 2


@dataclass(frozen=True)
class StorageConfig:
    path: str = "~/raspcam-data"
    quota_gb: float = 5.0    # decimal GB of segments to keep
    min_free_gb: float = 1.0
```

Extend `Config`:

```python
@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    live: LiveConfig
    web: WebConfig
    recording: RecordingConfig
    storage: StorageConfig
```

Add helper next to `_positive_int`:

```python
def _number(name: str, value, *, minimum: float, inclusive: bool) -> None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and (
        value >= minimum if inclusive else value > minimum)
    if not ok:
        bound = f">= {minimum}" if inclusive else f"> {minimum}"
        raise ConfigError(f"{name} must be a number {bound}, got {value!r}")
```

In `parse_config`, build the new sections and validate:

```python
    cfg = Config(
        camera=_section(CameraConfig, data.get("camera"), "camera"),
        live=_section(LiveConfig, data.get("live"), "live"),
        web=_section(WebConfig, data.get("web"), "web"),
        recording=_section(RecordingConfig, data.get("recording"), "recording"),
        storage=_section(StorageConfig, data.get("storage"), "storage"),
    )
    ...existing checks...
    for name in ("fps", "segment_seconds", "keyframe_seconds"):
        _positive_int(f"recording.{name}", getattr(cfg.recording, name))
    if not isinstance(cfg.recording.crf, int) or not 0 <= cfg.recording.crf <= 51:
        raise ConfigError(f"recording.crf must be 0..51, got {cfg.recording.crf!r}")
    if cfg.recording.keyframe_seconds > cfg.recording.segment_seconds:
        raise ConfigError("recording.keyframe_seconds must not exceed recording.segment_seconds")
    _number("storage.quota_gb", cfg.storage.quota_gb, minimum=0, inclusive=False)
    _number("storage.min_free_gb", cfg.storage.min_free_gb, minimum=0, inclusive=True)
```

Append to `config.example.yaml`:

```yaml

recording:
  fps: 10
  crf: 28                 # lower = better quality, bigger files (30 ≈ half the size)
  preset: veryfast        # ~1.3 cores on the Pi 5; ultrafast ≈ 0.5 core but much worse quality per MB
  segment_seconds: 60
  keyframe_seconds: 2

storage:
  path: ~/raspcam-data    # segments/ and raspcam.db live here
  quota_gb: 5             # oldest segments are deleted beyond this
  min_free_gb: 1          # ...or when the disk has less free space than this
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_config.py -q` → PASS
- [ ] **Step 5: Commit** `feat: recording and storage config sections`

---

### Task 2: Segment store (SQLite)

**Files:**
- Create: `raspcam/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `Segment(name: str, start_ts: float, end_ts: float, size: int)` with `.duration`; `SegmentStore(path)` with `add(seg)`, `remove(name)`, `get(name) -> Segment | None`, `names() -> set[str]`, `oldest_first() -> list[Segment]`, `days() -> list[str]` (newest first, `YYYY-MM-DD` local), `by_day(day) -> list[Segment]` (oldest first), `total_size() -> int`.

- [ ] **Step 1: Write failing tests** — `tests/test_db.py`

```python
import time

from raspcam.db import Segment, SegmentStore


def ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def seg(when, dur=60.0, size=100):
    start = ts(when)
    return Segment(name=time.strftime("%Y%m%d-%H%M%S.mp4", time.localtime(start)),
                   start_ts=start, end_ts=start + dur, size=size)


def test_add_get_remove(tmp_path):
    store = SegmentStore(tmp_path / "db.sqlite")
    s = seg("2026-11-01 10:00:00")
    store.add(s)
    assert store.get(s.name) == s
    assert s.duration == 60.0
    assert store.names() == {s.name}
    store.remove(s.name)
    assert store.get(s.name) is None


def test_days_and_by_day(tmp_path):
    store = SegmentStore(tmp_path / "db.sqlite")
    a, b, c = seg("2026-10-31 23:59:00"), seg("2026-11-01 00:01:00"), seg("2026-11-01 00:00:00")
    for s in (a, b, c):
        store.add(s)
    assert store.days() == ["2026-11-01", "2026-10-31"]
    assert [s.name for s in store.by_day("2026-11-01")] == [c.name, b.name]
    assert [s.name for s in store.oldest_first()] == [a.name, c.name, b.name]


def test_total_size_and_persistence(tmp_path):
    path = tmp_path / "db.sqlite"
    store = SegmentStore(path)
    store.add(seg("2026-11-01 10:00:00", size=10))
    store.add(seg("2026-11-01 10:01:00", size=32))
    assert SegmentStore(path).total_size() == 42


def test_empty_store(tmp_path):
    store = SegmentStore(tmp_path / "db.sqlite")
    assert store.total_size() == 0
    assert store.days() == [] and store.oldest_first() == []
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_db.py -q` → FAIL (module missing)

- [ ] **Step 3: Implement** `raspcam/db.py`

```python
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
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_db.py -q` → PASS
- [ ] **Step 5: Commit** `feat: SQLite segment store`

---

### Task 3: Segment naming, probing, and indexer

**Files:**
- Create: `raspcam/segments.py`
- Test: `tests/test_segments.py`

**Interfaces:**
- Consumes: `Segment`, `SegmentStore` (Task 2).
- Produces: `SEGMENT_PATTERN = "%Y%m%d-%H%M%S.mp4"`; `parse_start(name) -> float | None`; `probe_duration(path) -> float | None` (None = ffprobe ran and could not read it; raises `FileNotFoundError` if ffprobe is missing); `Indexer(store, directory, probe=probe_duration, settle_seconds=5.0, clock=time.time)` with `scan(writing: bool) -> list[str]` (names added).

- [ ] **Step 1: Write failing tests** — `tests/test_segments.py`

```python
import os
import time

from raspcam.db import SegmentStore
from raspcam.segments import Indexer, parse_start


def touch(directory, name, size=10, age=60.0):
    p = directory / name
    p.write_bytes(b"x" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_parse_start():
    assert parse_start("20261101-001001.mp4") == time.mktime((2026, 11, 1, 0, 10, 1, 0, 0, -1))
    assert parse_start("20261301-001001.mp4") is None
    assert parse_start("notes.txt") is None


def make(tmp_path, durations=None, **kw):
    d = tmp_path / "segments"
    d.mkdir()
    store = SegmentStore(tmp_path / "db.sqlite")
    durations = durations or {}
    idx = Indexer(store, d, probe=lambda p: durations.get(p.name, 60.0), **kw)
    return d, store, idx


def test_indexes_finished_files_and_skips_newest_while_writing(tmp_path):
    d, store, idx = make(tmp_path)
    touch(d, "20261101-100000.mp4", size=7)
    touch(d, "20261101-100100.mp4")
    assert idx.scan(writing=True) == ["20261101-100000.mp4"]
    s = store.get("20261101-100000.mp4")
    assert s.size == 7 and s.duration == 60.0
    assert idx.scan(writing=False) == ["20261101-100100.mp4"]


def test_recently_modified_files_wait_to_settle(tmp_path):
    d, store, idx = make(tmp_path)
    touch(d, "20261101-100000.mp4", age=1)
    assert idx.scan(writing=False) == []
    assert store.names() == set()


def test_unreadable_files_are_deleted(tmp_path):
    d, store, idx = make(tmp_path, durations={"20261101-100000.mp4": None})
    p = touch(d, "20261101-100000.mp4")
    assert idx.scan(writing=False) == []
    assert not p.exists()


def test_rows_for_missing_files_are_dropped(tmp_path):
    d, store, idx = make(tmp_path)
    p = touch(d, "20261101-100000.mp4")
    idx.scan(writing=False)
    p.unlink()
    idx.scan(writing=False)
    assert store.names() == set()


def test_other_files_are_ignored(tmp_path):
    d, store, idx = make(tmp_path)
    touch(d, "readme.txt")
    assert idx.scan(writing=False) == []
    assert (d / "readme.txt").exists()
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_segments.py -q` → FAIL

- [ ] **Step 3: Implement** `raspcam/segments.py`

```python
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
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_segments.py -q` → PASS
- [ ] **Step 5: Commit** `feat: segment indexer with reconciliation`

---

### Task 4: Retention planner and housekeeper

**Files:**
- Create: `raspcam/retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `Segment`, `SegmentStore`, `Indexer`, `StorageConfig`.
- Produces: `GB = 10**9`; `plan_deletions(segments, quota_bytes, free_bytes, min_free_bytes) -> list[str]`; `Housekeeper(store, indexer, directory, storage: StorageConfig, writing: Callable[[], bool], interval=10.0, disk_free=...)` with `run_once()`, `start()`, `stop()`.

- [ ] **Step 1: Write failing tests** — `tests/test_retention.py`

```python
from raspcam.config import StorageConfig
from raspcam.db import Segment, SegmentStore
from raspcam.retention import GB, Housekeeper, plan_deletions


def segs(*sizes):
    return [Segment(f"2026110{i}-000000.mp4", i, i + 60, size) for i, size in enumerate(sizes, 1)]


def test_nothing_to_delete_when_under_quota_and_enough_free():
    assert plan_deletions(segs(10, 10), quota_bytes=100, free_bytes=50, min_free_bytes=10) == []


def test_deletes_oldest_until_under_quota():
    s = segs(40, 40, 40)
    assert plan_deletions(s, quota_bytes=90, free_bytes=1000, min_free_bytes=0) == [s[0].name]


def test_deletes_until_enough_free_space():
    s = segs(10, 10, 10)
    assert plan_deletions(s, quota_bytes=1000, free_bytes=5, min_free_bytes=20) == [s[0].name, s[1].name]


def test_can_delete_everything():
    s = segs(10, 10)
    assert plan_deletions(s, quota_bytes=1, free_bytes=0, min_free_bytes=0) == [s[0].name, s[1].name]


class FakeIndexer:
    def __init__(self):
        self.calls = []

    def scan(self, writing):
        self.calls.append(writing)
        return []


def test_housekeeper_scans_then_deletes_files_and_rows(tmp_path):
    store = SegmentStore(tmp_path / "db.sqlite")
    for s in segs(GB, GB):
        store.add(s)
        (tmp_path / s.name).write_bytes(b"x")
    idx = FakeIndexer()
    hk = Housekeeper(store, idx, tmp_path, StorageConfig(quota_gb=1.5, min_free_gb=0),
                     writing=lambda: True, disk_free=lambda p: 100 * GB)
    hk.run_once()
    assert idx.calls == [True]
    first, second = segs(GB, GB)
    assert store.names() == {second.name}
    assert not (tmp_path / first.name).exists() and (tmp_path / second.name).exists()
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_retention.py -q` → FAIL

- [ ] **Step 3: Implement** `raspcam/retention.py`

```python
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
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_retention.py -q` → PASS
- [ ] **Step 5: Commit** `feat: storage quota retention and housekeeper`

---

### Task 5: ffmpeg command with recording output

**Files:**
- Modify: `raspcam/ffmpeg_cmd.py`
- Test: `tests/test_ffmpeg_cmd.py`, `tests/test_recorder.py`

**Interfaces:**
- Consumes: `SEGMENT_PATTERN` (Task 3), `Config.recording` (Task 1).
- Produces: `build_command(cfg: Config, segments_dir: Path) -> list[str]` — first output H.264 segments, second output MJPEG on stdout.

- [ ] **Step 1: Replace `tests/test_ffmpeg_cmd.py`**

```python
from pathlib import Path

from raspcam.config import parse_config
from raspcam.ffmpeg_cmd import build_command

SEG = Path("/data/segments")


def make_cfg(device, **sections):
    return parse_config({"camera": {"device": device}, "web": {"password_hash": "scrypt$x"}, **sections})


def output_args(argv):
    """Split argv after the -filter_complex graph into [recording, live] output args."""
    start = argv.index("-filter_complex") + 2
    rest = argv[start:]
    second_map = rest.index("-map", 1)
    return rest[:second_map], rest[second_map:]


def test_v4l2_input():
    argv = build_command(make_cfg("/dev/video0"), SEG)
    i = argv.index("-i")
    assert argv[i - 8:i + 2] == ["-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080",
                                 "-framerate", "30", "-i", "/dev/video0"]
    assert "-nostdin" in argv and argv[argv.index("-loglevel") + 1] == "error"


def test_test_source_uses_lavfi_in_real_time():
    argv = build_command(make_cfg("testsrc"), SEG)
    i = argv.index("-i")
    assert argv[i - 3:i + 2] == ["-re", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30"]


def test_filter_graph_splits_recording_and_live():
    argv = build_command(make_cfg("/dev/video0"), SEG)
    assert argv[argv.index("-filter_complex") + 1] == (
        "[0:v]split=2[r][l];[r]fps=10[rec];[l]fps=5,scale=1280:720[live]")


def test_recording_output_is_h264_segments():
    rec, _ = output_args(build_command(make_cfg("/dev/video0"), SEG))
    assert rec[:2] == ["-map", "[rec]"]
    opts = dict(zip(rec[2:-1:2], rec[3:-1:2]))
    assert opts["-c:v"] == "libx264" and opts["-preset"] == "veryfast" and opts["-crf"] == "28"
    assert opts["-g"] == "20" and opts["-pix_fmt"] == "yuv420p"
    assert opts["-f"] == "segment" and opts["-segment_time"] == "60"
    assert opts["-segment_atclocktime"] == "1" and opts["-strftime"] == "1"
    assert opts["-reset_timestamps"] == "1"
    assert opts["-segment_format_options"] == "movflags=+faststart"
    assert rec[-1] == "/data/segments/%Y%m%d-%H%M%S.mp4"


def test_live_output_is_mjpeg_on_stdout():
    _, live = output_args(build_command(make_cfg("/dev/video0"), SEG))
    assert live == ["-map", "[live]", "-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "pipe:1"]


def test_recording_options_follow_config():
    cfg = make_cfg("/dev/video0", recording={"fps": 5, "crf": 30, "preset": "ultrafast",
                                             "segment_seconds": 30, "keyframe_seconds": 3})
    rec, _ = output_args(build_command(cfg, SEG))
    opts = dict(zip(rec[2:-1:2], rec[3:-1:2]))
    assert (opts["-crf"], opts["-preset"], opts["-g"], opts["-segment_time"]) == ("30", "ultrafast", "15", "30")
```

In `tests/test_recorder.py`, update `test_real_ffmpeg_with_test_source` to pass a directory, and add a recording integration test:

```python
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_with_test_source(latest, tmp_path):
    rec = Recorder(build_command(small_cfg(), tmp_path), latest)
    rec.start()
    try:
        assert wait_until(lambda: latest.get()[0] >= 3, timeout=10)
        jpeg = latest.get()[1]
        assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    finally:
        rec.stop()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_writes_indexable_segments(latest, tmp_path):
    from raspcam.db import SegmentStore
    from raspcam.segments import Indexer
    seg_dir = tmp_path / "segments"
    seg_dir.mkdir()
    rec = Recorder(build_command(small_cfg(), seg_dir), latest)
    rec.start()
    try:
        assert wait_until(lambda: len(list(seg_dir.glob("*.mp4"))) >= 3, timeout=10)
    finally:
        rec.stop()
    store = SegmentStore(tmp_path / "db.sqlite")
    added = Indexer(store, seg_dir, settle_seconds=0).scan(writing=False)
    assert len(added) >= 2
    full = [s for s in store.oldest_first()[1:-1]]  # first/last may be partial
    assert all(0.5 <= s.duration <= 1.5 for s in full)
```

with helper:

```python
def small_cfg():
    return parse_config({
        "camera": {"device": "testsrc", "width": 320, "height": 180, "input_fps": 10},
        "live": {"width": 160, "height": 90, "fps": 5},
        "recording": {"fps": 5, "segment_seconds": 1, "keyframe_seconds": 1},
        "web": {"password_hash": "scrypt$x"},
    })
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_ffmpeg_cmd.py tests/test_recorder.py -q` → FAIL (signature)

- [ ] **Step 3: Implement** — replace `build_command` in `raspcam/ffmpeg_cmd.py`:

```python
from pathlib import Path

from .config import Config
from .segments import SEGMENT_PATTERN

...

def build_command(cfg: Config, segments_dir: Path) -> list[str]:
    """ffmpeg argv: H.264 segments into segments_dir, live MJPEG on stdout."""
    rec, live = cfg.recording, cfg.live
    graph = (f"[0:v]split=2[r][l];"
             f"[r]fps={rec.fps}[rec];"
             f"[l]fps={live.fps},scale={live.width}:{live.height}[live]")
    return [
        # "error": swscale prints a harmless "deprecated pixel format" warning for
        # MJPEG input that would otherwise show up as last_error in the UI
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        *_input_args(cfg),
        "-filter_complex", graph,
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
```

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_ffmpeg_cmd.py tests/test_recorder.py -v` → PASS
- [ ] **Step 5: Commit** `feat: record H.264 segments alongside the live stream`

---

### Task 6: Web — recordings page, APIs, media serving, storage status

**Files:**
- Modify: `raspcam/web.py`, `raspcam/templates/base.html`, `raspcam/templates/live.html`
- Create: `raspcam/templates/nav.html`, `raspcam/templates/recordings.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `SegmentStore`, `Segment`, `parse_start` / name regex via `parse_start`.
- Produces: `create_app(cfg, latest, recorder_status, store, segments_dir)`; `GET /recordings`; `GET /api/days -> list[str]`; `GET /api/segments?day=YYYY-MM-DD -> list[{name,start,end,duration,size}]`; `GET /media/{name}` (MP4 with Range); `/api/status` gains `storage: {used_bytes, quota_bytes, free_bytes}`.

- [ ] **Step 1: Update tests** — in `tests/test_web.py` change the `client` fixture and add tests:

```python
import time

from raspcam.db import Segment, SegmentStore


@pytest.fixture
def store(tmp_path):
    return SegmentStore(tmp_path / "db.sqlite")


@pytest.fixture
def seg_dir(tmp_path):
    d = tmp_path / "segments"
    d.mkdir()
    return d


@pytest.fixture
def client(latest, store, seg_dir):
    cfg = parse_config({
        "camera": {"device": "testsrc"},
        "web": {"password_hash": hash_password("secret"), "secret_key": "k" * 32},
    })
    return TestClient(create_app(cfg, latest, lambda: STATUS, store, seg_dir))


def add_segment(store, seg_dir, when="2026-11-01 10:00:00", data=b"0123456789"):
    start = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
    name = time.strftime("%Y%m%d-%H%M%S.mp4", time.localtime(start))
    (seg_dir / name).write_bytes(data)
    store.add(Segment(name, start, start + 60, len(data)))
    return name


def test_recordings_endpoints_require_login(client):
    assert client.get("/recordings", follow_redirects=False).status_code == 303
    for url in ["/api/days", "/api/segments?day=2026-11-01", "/media/20261101-100000.mp4"]:
        assert client.get(url).status_code == 401


def test_days_and_segments(client, store, seg_dir):
    name = add_segment(store, seg_dir)
    login(client)
    assert client.get("/api/days").json() == ["2026-11-01"]
    [s] = client.get("/api/segments", params={"day": "2026-11-01"}).json()
    assert s["name"] == name and s["duration"] == 60 and s["size"] == 10
    assert s["end"] - s["start"] == 60


def test_media_supports_range_requests(client, store, seg_dir):
    name = add_segment(store, seg_dir)
    login(client)
    full = client.get(f"/media/{name}")
    assert full.status_code == 200 and full.content == b"0123456789"
    assert full.headers["content-type"] == "video/mp4"
    part = client.get(f"/media/{name}", headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.content == b"2345"


def test_media_rejects_unknown_and_malicious_names(client, store, seg_dir):
    login(client)
    assert client.get("/media/20261101-100000.mp4").status_code == 404
    assert client.get("/media/..%2Fdb.sqlite").status_code == 404


def test_status_includes_storage(client, store, seg_dir):
    add_segment(store, seg_dir)
    login(client)
    st = client.get("/api/status").json()["storage"]
    assert st["used_bytes"] == 10 and st["quota_bytes"] == 5 * 10**9 and st["free_bytes"] > 0


def test_recordings_page_renders(client):
    login(client)
    r = client.get("/recordings")
    assert r.status_code == 200 and "Xem lại" in r.text and "<video" in r.text
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_web.py -q` → FAIL (signature)

- [ ] **Step 3: Implement** — in `raspcam/web.py`:

Imports:

```python
import shutil

from fastapi.responses import FileResponse

from .db import SegmentStore
from .retention import GB
from .segments import parse_start
```

Signature and new routes inside `create_app` (keep existing routes; add `store` and `segments_dir` params):

```python
def create_app(cfg: Config, latest: LatestFrame, recorder_status: Callable[[], dict],
               store: SegmentStore, segments_dir: Path) -> FastAPI:
    ...

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return TEMPLATES.TemplateResponse(request, "recordings.html", {})

    @app.get("/api/days")
    def days(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        return store.days()

    @app.get("/api/segments")
    def segments(request: Request, day: str):
        if not logged_in(request):
            return Response(status_code=401)
        return [{"name": s.name, "start": s.start_ts, "end": s.end_ts,
                 "duration": s.duration, "size": s.size} for s in store.by_day(day)]

    @app.get("/media/{name}")
    def media(request: Request, name: str):
        if not logged_in(request):
            return Response(status_code=401)
        # parse_start only accepts YYYYMMDD-HHMMSS.mp4, so no path tricks get through
        if parse_start(name) is None or store.get(name) is None:
            return Response(status_code=404)
        path = segments_dir / name
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="video/mp4")
```

Status route becomes:

```python
    @app.get("/api/status")
    def status(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        age = latest.age()
        return {"camera_online": age is not None and age < OFFLINE_AFTER,
                "last_frame_age": age, **recorder_status(),
                "storage": {"used_bytes": store.total_size(),
                            "quota_bytes": int(cfg.storage.quota_gb * GB),
                            "free_bytes": shutil.disk_usage(segments_dir).free}}
```

- [ ] **Step 4: Templates**

`raspcam/templates/nav.html`:

```html
<header>
  <nav style="display:flex; gap:16px; align-items:center">
    <strong>RaspCam</strong>
    <a href="/">Trực tiếp</a>
    <a href="/recordings">Xem lại</a>
  </nav>
  <form method="post" action="/logout" style="margin:0"><button type="submit">Đăng xuất</button></form>
</header>
```

`base.html` — add to the `<style>` block:

```css
  a { color: var(--accent); text-decoration: none; }
  select { font:inherit; padding:6px 8px; border-radius:8px; border:1px solid var(--muted); background:transparent; color:inherit; }
  .hour { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-top:8px; }
  .hour > span { width:48px; }
  .seg { padding:4px 8px; background:transparent; color:inherit; border:1px solid var(--muted); }
  .seg.active { background:var(--accent); border-color:var(--accent); color:#fff; }
```

`live.html` — replace the `<header>…</header>` block with `{% include "nav.html" %}`, and in `refresh()` add the storage figure after the restarts text:

```js
      const gb = b => (b / 1e9).toFixed(1);
      text += ` · lưu trữ: ${gb(s.storage.used_bytes)}/${gb(s.storage.quota_bytes)} GB (ổ trống ${gb(s.storage.free_bytes)} GB)`;
```

`raspcam/templates/recordings.html`:

```html
{% extends "base.html" %}
{% block body %}
{% include "nav.html" %}
<main>
  <div class="card">
    <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:12px">
      <h2 style="margin:0">Xem lại</h2>
      <select id="day" aria-label="Ngày"></select>
      <span id="now" class="muted"></span>
    </div>
    <video id="player" controls muted playsinline
           style="width:100%; display:block; background:#000; border-radius:8px; aspect-ratio:16/9"></video>
    <div id="list"></div>
  </div>
</main>
<script>
  const daySel = document.getElementById("day");
  const list = document.getElementById("list");
  const player = document.getElementById("player");
  const nowEl = document.getElementById("now");
  let segs = [], buttons = [], current = -1;
  const time = ts => new Date(ts * 1000).toLocaleTimeString("vi-VN", {hour12: false});
  const pad = n => String(n).padStart(2, "0");

  async function api(url) {
    const r = await fetch(url);
    if (r.status === 401) { location.href = "/login"; throw new Error("401"); }
    return r.json();
  }

  async function loadDays() {
    const days = await api("/api/days");
    daySel.innerHTML = days.map(d => `<option>${d}</option>`).join("");
    if (!days.length) { list.textContent = "Chưa có bản ghi nào."; return; }
    await loadDay(days[0]);
  }

  async function loadDay(day) {
    segs = await api("/api/segments?day=" + encodeURIComponent(day));
    list.innerHTML = "";
    buttons = [];
    const rows = {};
    segs.forEach((s, i) => {
      const d = new Date(s.start * 1000);
      const h = pad(d.getHours());
      if (!rows[h]) {
        rows[h] = document.createElement("div");
        rows[h].className = "hour";
        rows[h].innerHTML = `<span class="muted">${h}:00</span>`;
        list.appendChild(rows[h]);
      }
      const b = document.createElement("button");
      b.className = "seg";
      b.textContent = pad(d.getMinutes());
      b.title = `${time(s.start)} · ${Math.round(s.duration)} giây · ${(s.size / 1e6).toFixed(1)} MB`;
      b.onclick = () => play(i);
      rows[h].appendChild(b);
      buttons.push(b);
    });
  }

  function play(i) {
    if (i < 0 || i >= segs.length) return;
    current = i;
    player.src = "/media/" + segs[i].name;
    player.play();
    nowEl.textContent = `${time(segs[i].start)} – ${time(segs[i].end)}`;
    buttons.forEach((b, j) => b.classList.toggle("active", j === i));
  }

  player.addEventListener("ended", () => play(current + 1));
  daySel.addEventListener("change", () => loadDay(daySel.value));
  loadDays();
</script>
{% endblock %}
```

- [ ] **Step 5: Run** `.venv/bin/pytest tests/test_web.py -v` → PASS
- [ ] **Step 6: Commit** `feat: recordings page, segment APIs, and media serving`

---

### Task 7: Wire it up, deploy, and verify on the Pi

**Files:**
- Modify: `raspcam/main.py`, `README.md`

- [ ] **Step 1: Update `raspcam/main.py`**

```python
"""Wire config, recorder, housekeeping, and web app together."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .config import load_config
from .db import SegmentStore
from .ffmpeg_cmd import build_command
from .frames import LatestFrame
from .recorder import Recorder
from .retention import Housekeeper
from .segments import Indexer
from .web import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="raspcam")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    data_dir = Path(cfg.storage.path).expanduser()
    segments_dir = data_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    store = SegmentStore(data_dir / "raspcam.db")

    latest = LatestFrame()
    recorder = Recorder(build_command(cfg, segments_dir), latest)
    housekeeper = Housekeeper(store, Indexer(store, segments_dir), segments_dir,
                              cfg.storage, writing=lambda: recorder.running)
    housekeeper.run_once()  # reconcile files left by a previous run before ffmpeg starts
    recorder.start()
    housekeeper.start()
    try:
        uvicorn.run(create_app(cfg, latest, recorder.status, store, segments_dir),
                    host=cfg.web.host, port=cfg.web.port, log_level="info")
    finally:
        housekeeper.stop()
        recorder.stop()
```

- [ ] **Step 2: README** — add under Config: recordings go to `storage.path` (default `~/raspcam-data`), oldest deleted beyond `quota_gb`; "Xem lại" page at `/recordings`.

- [ ] **Step 3: Full test suite** — `.venv/bin/pytest -q` → all PASS

- [ ] **Step 4: Local run with testsrc** (`segment_seconds: 10`), log in, confirm `/api/days` and `/api/segments` list segments after ~25 s and `/media/<name>` returns `206` for a Range request.

- [ ] **Step 5: Deploy** — `scripts/deploy.sh` → `active`. (Pi `config.yaml` has no `recording`/`storage` sections, so defaults apply: `~/raspcam-data`, 5 GB quota.)

- [ ] **Step 6: Verify on the Pi (≥3 min)**
  - `ls ~/raspcam-data/segments` shows one file per minute, ~10 MB each.
  - `/api/segments` lists them with ~60 s durations; a `/media` Range request returns `206`.
  - Pull one segment, extract a frame with ffmpeg, view it.
  - `top`: ffmpeg ≈ 150 % CPU; `vcgencmd measure_temp` < 75 °C.
  - Retention: temporarily set `storage.quota_gb: 0.02` in Pi config, restart, confirm only the newest ~2 segments remain after a housekeeping cycle, then restore.
  - Kill ffmpeg (`pkill -x ffmpeg`): recording resumes; the partial segment is indexed or deleted, never left unindexed.

- [ ] **Step 7: Commit and push** `feat: wire recording, housekeeping, and playback` then `git push`.
