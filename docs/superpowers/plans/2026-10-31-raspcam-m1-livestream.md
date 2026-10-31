# RaspCam Milestone 1 (Livestream) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Password-protected live view of the USB webcam in a browser on the LAN, running as a systemd service on the Pi.

**Architecture:** One Python process. A `Recorder` thread supervises a single ffmpeg child that reads the camera and writes 720p@5fps MJPEG to stdout; frames are split out and kept in a `LatestFrame` buffer. A FastAPI app serves login, a `multipart/x-mixed-replace` live stream from that buffer, and a status endpoint. Recording, motion, and retention come in later milestones and extend `ffmpeg_cmd` with more outputs.

**Tech Stack:** Python ≥3.11, ffmpeg, FastAPI + uvicorn, Jinja2, PyYAML, Starlette SessionMiddleware (itsdangerous), pytest + httpx.

**Spec:** `docs/superpowers/specs/2026-10-31-security-camera-design.md`

## Global Constraints

- Python `>=3.11` (Pi has 3.13.5, dev PC has 3.12.3).
- Runtime deps only: `fastapi`, `uvicorn`, `jinja2`, `pyyaml`, `itsdangerous`, `python-multipart`. No OpenCV.
- Pi: `nlt22@192.168.1.90`, code at `/home/nlt22/raspcam`, venv at `/home/nlt22/raspcam/.venv`.
- Camera: `/dev/v4l/by-id/usb-Wed_Camera_Wed_Camera_20240420112206-video-index0`, MJPEG 1920x1080@30.
- Live output: 1280x720 @ 5 fps MJPEG.
- `camera.device: testsrc` means ffmpeg `lavfi testsrc2` (dev/tests without a camera).
- Passwords stored only as scrypt hashes; `config.yaml` is git-ignored.
- UI text in Vietnamese.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, pytest config |
| `config.example.yaml` | Documented example config |
| `raspcam/__init__.py` | Package marker |
| `raspcam/config.py` | `Config` dataclasses, `parse_config`, `load_config`, `ConfigError` |
| `raspcam/ffmpeg_cmd.py` | `build_command(cfg) -> list[str]` |
| `raspcam/mjpeg.py` | `MjpegSplitter` — split a JPEG byte stream into frames |
| `raspcam/frames.py` | `LatestFrame` — thread-safe latest-frame holder |
| `raspcam/recorder.py` | `Recorder` — run/restart ffmpeg, feed `LatestFrame` |
| `raspcam/auth.py` | `hash_password`, `verify_password`, CLI to print a hash |
| `raspcam/web.py` | `create_app`, `mjpeg_stream` |
| `raspcam/templates/{base,login,live}.html` | UI |
| `raspcam/main.py`, `raspcam/__main__.py` | Wiring + `python -m raspcam` |
| `deploy/raspcam.service` | systemd unit |
| `scripts/deploy.sh` | rsync to Pi, install, restart service |
| `tests/test_*.py` | Tests per module |

---

### Task 1: Project scaffold and config

**Files:**
- Create: `pyproject.toml`, `config.example.yaml`, `raspcam/__init__.py`, `raspcam/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `CameraConfig(device, input_format="mjpeg", width=1920, height=1080, input_fps=30)`, `LiveConfig(width=1280, height=720, fps=5, quality=5)`, `WebConfig(password_hash, host="0.0.0.0", port=8080, username="admin", secret_key="")`, `Config(camera, live, web)`; `parse_config(data: dict) -> Config`; `load_config(path) -> Config`; `ConfigError(ValueError)`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "raspcam"
version = "0.1.0"
description = "Security camera for Raspberry Pi 5"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn>=0.29",
  "jinja2>=3.1",
  "pyyaml>=6.0",
  "itsdangerous>=2.1",
  "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]

[tool.setuptools.packages.find]
include = ["raspcam*"]

[tool.setuptools.package-data]
raspcam = ["templates/*.html"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`raspcam/__init__.py`:

```python
"""RaspCam: security camera for Raspberry Pi 5."""
```

- [ ] **Step 2: Create venv and install**

Run: `python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"`
Expected: installs without errors.

- [ ] **Step 3: Write the failing tests** — `tests/test_config.py`

```python
import pytest

from raspcam.config import ConfigError, load_config, parse_config

MINIMAL = {"camera": {"device": "testsrc"}, "web": {"password_hash": "scrypt$x"}}


def test_minimal_config_uses_defaults():
    cfg = parse_config(MINIMAL)
    assert cfg.camera.device == "testsrc"
    assert (cfg.camera.width, cfg.camera.height, cfg.camera.input_fps) == (1920, 1080, 30)
    assert (cfg.live.width, cfg.live.height, cfg.live.fps, cfg.live.quality) == (1280, 720, 5, 5)
    assert (cfg.web.host, cfg.web.port, cfg.web.username) == ("0.0.0.0", 8080, "admin")


def test_missing_camera_device_is_an_error():
    with pytest.raises(ConfigError, match="camera"):
        parse_config({"web": {"password_hash": "scrypt$x"}})


def test_unknown_key_in_section_is_an_error():
    data = {**MINIMAL, "live": {"fsp": 5}}
    with pytest.raises(ConfigError, match="fsp"):
        parse_config(data)


def test_empty_password_hash_is_an_error():
    data = {**MINIMAL, "web": {"password_hash": ""}}
    with pytest.raises(ConfigError, match="password_hash"):
        parse_config(data)


@pytest.mark.parametrize("section,key,value", [
    ("live", "fps", 0),
    ("live", "width", -1),
    ("camera", "input_fps", "30"),
    ("live", "quality", 40),
])
def test_invalid_numbers_are_errors(section, key, value):
    data = {**MINIMAL, section: {**MINIMAL.get(section, {}), key: value}}
    with pytest.raises(ConfigError, match=key):
        parse_config(data)


def test_unknown_top_level_sections_are_ignored():
    cfg = parse_config({**MINIMAL, "recording": {"mode": "both"}})
    assert cfg.camera.device == "testsrc"


def test_load_config_reads_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("camera:\n  device: testsrc\nweb:\n  password_hash: 'scrypt$x'\n  port: 9000\n")
    assert load_config(path).web.port == 9000


def test_example_config_parses():
    from pathlib import Path
    example = Path(__file__).parent.parent / "config.example.yaml"
    text = example.read_text().replace('password_hash: ""', 'password_hash: "scrypt$x"')
    import yaml
    cfg = parse_config(yaml.safe_load(text))
    assert cfg.camera.device.startswith("/dev/v4l/by-id/")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'raspcam.config'`

- [ ] **Step 5: Implement `raspcam/config.py`**

```python
"""Load and validate config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CameraConfig:
    device: str
    input_format: str = "mjpeg"
    width: int = 1920
    height: int = 1080
    input_fps: int = 30


@dataclass(frozen=True)
class LiveConfig:
    width: int = 1280
    height: int = 720
    fps: int = 5
    quality: int = 5  # ffmpeg -q:v, 2 (best) .. 31 (worst)


@dataclass(frozen=True)
class WebConfig:
    password_hash: str
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    secret_key: str = ""  # empty: random per start (sessions end on restart)


@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    live: LiveConfig
    web: WebConfig


def _section(cls, data, name):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"[{name}] must be a mapping")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"[{name}] unknown keys: {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"[{name}] {e}") from None


def _positive_int(name: str, value) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {value!r}")


def parse_config(data: dict) -> Config:
    """Build a Config from parsed YAML. Unknown top-level sections are ignored
    so configs written for later milestones still load."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    cfg = Config(
        camera=_section(CameraConfig, data.get("camera"), "camera"),
        live=_section(LiveConfig, data.get("live"), "live"),
        web=_section(WebConfig, data.get("web"), "web"),
    )
    for name in ("width", "height", "input_fps"):
        _positive_int(f"camera.{name}", getattr(cfg.camera, name))
    for name in ("width", "height", "fps"):
        _positive_int(f"live.{name}", getattr(cfg.live, name))
    _positive_int("web.port", cfg.web.port)
    if not isinstance(cfg.live.quality, int) or not 2 <= cfg.live.quality <= 31:
        raise ConfigError(f"live.quality must be 2..31, got {cfg.live.quality!r}")
    if not cfg.web.password_hash:
        raise ConfigError("web.password_hash is empty; generate one with: python -m raspcam.auth")
    return cfg


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))
```

- [ ] **Step 6: Create `config.example.yaml`**

```yaml
# Copy to config.yaml, then fill web.password_hash and web.secret_key.
# Generate both with:  python -m raspcam.auth

camera:
  device: /dev/v4l/by-id/usb-Wed_Camera_Wed_Camera_20240420112206-video-index0
  # device: testsrc       # ffmpeg test pattern, for development without a camera
  input_format: mjpeg
  width: 1920
  height: 1080
  input_fps: 30

live:
  width: 1280
  height: 720
  fps: 5
  quality: 5              # 2 (best) .. 31 (worst)

web:
  host: 0.0.0.0
  port: 8080
  username: admin
  password_hash: ""
  secret_key: ""
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml config.example.yaml raspcam/ tests/test_config.py
git commit -m "feat: project scaffold and config loading"
```

---

### Task 2: ffmpeg command builder

**Files:**
- Create: `raspcam/ffmpeg_cmd.py`
- Test: `tests/test_ffmpeg_cmd.py`

**Interfaces:**
- Consumes: `Config` from Task 1.
- Produces: `TEST_SOURCE = "testsrc"`; `build_command(cfg: Config) -> list[str]` — argv whose stdout is a 720p MJPEG stream.

- [ ] **Step 1: Write the failing tests** — `tests/test_ffmpeg_cmd.py`

```python
from raspcam.config import parse_config
from raspcam.ffmpeg_cmd import build_command


def make_cfg(device):
    return parse_config({"camera": {"device": device}, "web": {"password_hash": "scrypt$x"}})


def test_v4l2_camera_command():
    argv = build_command(make_cfg("/dev/video0"))
    assert argv[0] == "ffmpeg"
    i = argv.index("-i")
    assert argv[i + 1] == "/dev/video0"
    assert argv[i - 8:i] == ["-f", "v4l2", "-input_format", "mjpeg",
                             "-video_size", "1920x1080", "-framerate", "30"]
    assert argv[argv.index("-vf") + 1] == "fps=5,scale=1280:720"
    assert argv[argv.index("-c:v"):] == ["-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "pipe:1"]
    assert "-nostdin" in argv


def test_test_source_uses_lavfi_in_real_time():
    argv = build_command(make_cfg("testsrc"))
    i = argv.index("-i")
    assert argv[i + 1] == "testsrc2=size=1920x1080:rate=30"
    assert argv[i - 2:i] == ["-f", "lavfi"]
    assert "-re" in argv[:i]
    assert "v4l2" not in argv
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ffmpeg_cmd.py -v`
Expected: FAIL — `No module named 'raspcam.ffmpeg_cmd'`

- [ ] **Step 3: Implement `raspcam/ffmpeg_cmd.py`**

```python
"""Build the ffmpeg command line from config."""
from __future__ import annotations

from .config import Config

TEST_SOURCE = "testsrc"


def _input_args(cfg: Config) -> list[str]:
    cam = cfg.camera
    if cam.device == TEST_SOURCE:
        # -re: lavfi would otherwise generate frames as fast as the CPU allows
        return ["-re", "-f", "lavfi", "-i",
                f"testsrc2=size={cam.width}x{cam.height}:rate={cam.input_fps}"]
    return ["-f", "v4l2", "-input_format", cam.input_format,
            "-video_size", f"{cam.width}x{cam.height}",
            "-framerate", str(cam.input_fps), "-i", cam.device]


def build_command(cfg: Config) -> list[str]:
    """ffmpeg argv that writes the live MJPEG stream to stdout."""
    live = cfg.live
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        *_input_args(cfg),
        "-vf", f"fps={live.fps},scale={live.width}:{live.height}",
        "-c:v", "mjpeg", "-q:v", str(live.quality),
        "-f", "mjpeg", "pipe:1",
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ffmpeg_cmd.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add raspcam/ffmpeg_cmd.py tests/test_ffmpeg_cmd.py
git commit -m "feat: ffmpeg command builder for live MJPEG"
```

---

### Task 3: MJPEG splitter and latest-frame buffer

**Files:**
- Create: `raspcam/mjpeg.py`, `raspcam/frames.py`
- Test: `tests/test_mjpeg.py`, `tests/test_frames.py`

**Interfaces:**
- Produces: `MjpegSplitter(max_buffer=8*1024*1024).feed(data: bytes) -> list[bytes]`; `LatestFrame(clock=time.monotonic)` with `set(jpeg: bytes) -> None`, `get() -> tuple[int, bytes | None]` (sequence number starts at 0 with `None`), `age() -> float | None` (seconds since last `set`).

- [ ] **Step 1: Write the failing tests** — `tests/test_mjpeg.py`

```python
from raspcam.mjpeg import MjpegSplitter

A = b"\xff\xd8AAAA\xff\xd9"
B = b"\xff\xd8BB\xff\x00B\xff\xd9"


def test_two_frames_in_one_chunk():
    assert MjpegSplitter().feed(A + B) == [A, B]


def test_frames_split_at_every_byte_boundary():
    s = MjpegSplitter()
    out = []
    for byte in A + B:
        out += s.feed(bytes([byte]))
    assert out == [A, B]


def test_garbage_before_frame_is_dropped():
    assert MjpegSplitter().feed(b"junk" + A) == [A]


def test_soi_split_across_chunks():
    s = MjpegSplitter()
    assert s.feed(b"junk\xff") == []
    assert s.feed(A[1:]) == [A]


def test_oversized_partial_frame_is_discarded():
    s = MjpegSplitter(max_buffer=16)
    assert s.feed(b"\xff\xd8" + b"x" * 32) == []
    assert s.feed(A) == [A]
```

`tests/test_frames.py`:

```python
from raspcam.frames import LatestFrame


def test_empty_buffer():
    f = LatestFrame()
    assert f.get() == (0, None)
    assert f.age() is None


def test_set_increments_sequence_and_tracks_age():
    now = [100.0]
    f = LatestFrame(clock=lambda: now[0])
    f.set(b"one")
    f.set(b"two")
    assert f.get() == (2, b"two")
    now[0] = 103.5
    assert f.age() == 3.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mjpeg.py tests/test_frames.py -v`
Expected: FAIL — modules not found

- [ ] **Step 3: Implement `raspcam/mjpeg.py`**

```python
"""Split a concatenated JPEG byte stream (ffmpeg -f mjpeg) into frames."""
from __future__ import annotations

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


class MjpegSplitter:
    def __init__(self, max_buffer: int = 8 * 1024 * 1024):
        self._buf = bytearray()
        self._max = max_buffer

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes; return the complete frames they finish."""
        self._buf += data
        frames = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                # keep a trailing 0xFF: it may be the first half of the next SOI
                keep = 1 if self._buf.endswith(b"\xff") else 0
                del self._buf[:len(self._buf) - keep]
                break
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                del self._buf[:start]
                break
            frames.append(bytes(self._buf[start:end + 2]))
            del self._buf[:end + 2]
        if len(self._buf) > self._max:
            self._buf.clear()
        return frames
```

- [ ] **Step 4: Implement `raspcam/frames.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mjpeg.py tests/test_frames.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add raspcam/mjpeg.py raspcam/frames.py tests/test_mjpeg.py tests/test_frames.py
git commit -m "feat: MJPEG frame splitter and latest-frame buffer"
```

---

### Task 4: Recorder (ffmpeg supervisor)

**Files:**
- Create: `raspcam/recorder.py`
- Test: `tests/test_recorder.py`

**Interfaces:**
- Consumes: `MjpegSplitter`, `LatestFrame` (Task 3); `build_command`, `parse_config` (Tasks 1–2) in the integration test.
- Produces: `Recorder(argv: list[str], latest: LatestFrame, backoff_initial=1.0, backoff_max=30.0, stable_after=60.0)` with `start()`, `stop(timeout=5.0)`, `running: bool` property, `restarts: int`, `status() -> dict` with keys `ffmpeg_running: bool`, `ffmpeg_uptime: float | None`, `restarts: int`, `last_error: str | None`.

- [ ] **Step 1: Write the failing tests** — `tests/test_recorder.py`

```python
import shutil
import sys
import time

import pytest

from raspcam.config import parse_config
from raspcam.ffmpeg_cmd import build_command
from raspcam.frames import LatestFrame
from raspcam.recorder import Recorder


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def py(code):
    return [sys.executable, "-c", code]


@pytest.fixture
def latest():
    return LatestFrame()


def test_frames_from_stdout_reach_latest_frame(latest):
    rec = Recorder(py(
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\xff\\xd8A\\xff\\xd9\\xff\\xd8B\\xff\\xd9')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"), latest)
    rec.start()
    try:
        assert wait_until(lambda: latest.get()[0] >= 2)
        assert latest.get()[1] == b"\xff\xd8B\xff\xd9"
        assert rec.status()["ffmpeg_running"] is True
    finally:
        rec.stop()
    assert not rec.running


def test_restarts_with_backoff_and_reports_last_error(latest):
    rec = Recorder(py("import sys; sys.stderr.write('boom\\n'); sys.exit(1)"), latest,
                   backoff_initial=0.01, backoff_max=0.02)
    rec.start()
    try:
        assert wait_until(lambda: rec.restarts >= 3)
        assert rec.status()["last_error"] == "boom"
    finally:
        rec.stop()


def test_missing_binary_counts_as_failure(latest):
    rec = Recorder(["/nonexistent/ffmpeg"], latest, backoff_initial=0.01, backoff_max=0.02)
    rec.start()
    try:
        assert wait_until(lambda: rec.restarts >= 1)
        assert "No such file" in rec.status()["last_error"]
    finally:
        rec.stop()


def test_stop_terminates_child_quickly(latest):
    rec = Recorder(py("import time; time.sleep(30)"), latest)
    rec.start()
    assert wait_until(lambda: rec.running)
    t0 = time.monotonic()
    rec.stop()
    assert time.monotonic() - t0 < 3
    assert not rec.running


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_with_test_source(latest):
    cfg = parse_config({
        "camera": {"device": "testsrc", "width": 640, "height": 360, "input_fps": 10},
        "live": {"width": 320, "height": 180, "fps": 5},
        "web": {"password_hash": "scrypt$x"},
    })
    rec = Recorder(build_command(cfg), latest)
    rec.start()
    try:
        assert wait_until(lambda: latest.get()[0] >= 3, timeout=10)
        jpeg = latest.get()[1]
        assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    finally:
        rec.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recorder.py -v`
Expected: FAIL — `No module named 'raspcam.recorder'`

- [ ] **Step 3: Implement `raspcam/recorder.py`**

```python
"""Run ffmpeg, restart it when it dies, and feed live frames to LatestFrame."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque

from .frames import LatestFrame
from .mjpeg import MjpegSplitter

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, argv: list[str], latest: LatestFrame, *,
                 backoff_initial: float = 1.0, backoff_max: float = 30.0,
                 stable_after: float = 60.0):
        self._argv = argv
        self._latest = latest
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._stable_after = stable_after  # a run this long resets the backoff
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self.restarts = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            started = self._started_at
        return {
            "ffmpeg_running": running,
            "ffmpeg_uptime": time.monotonic() - started if running and started else None,
            "restarts": self.restarts,
            "last_error": self._stderr_tail[-1] if self._stderr_tail else None,
        }

    def _run(self) -> None:
        delay = self._backoff_initial
        while not self._stop.is_set():
            started = time.monotonic()
            self._run_once()
            if self._stop.is_set():
                break
            if time.monotonic() - started >= self._stable_after:
                delay = self._backoff_initial
            self.restarts += 1
            log.warning("ffmpeg exited; restarting in %.1fs", delay)
            self._stop.wait(delay)
            delay = min(delay * 2, self._backoff_max)

    def _run_once(self) -> None:
        try:
            proc = subprocess.Popen(self._argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self._stderr_tail.append(str(e))
            log.error("cannot start ffmpeg: %s", e)
            return
        with self._lock:
            self._proc = proc
            self._started_at = time.monotonic()
        if self._stop.is_set():  # stop() ran before _proc was visible to it
            proc.terminate()
        err = threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True)
        err.start()
        splitter = MjpegSplitter()
        try:
            while chunk := proc.stdout.read1(65536):
                for frame in splitter.feed(chunk):
                    self._latest.set(frame)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            err.join(timeout=1)
            with self._lock:
                self._proc = None

    def _drain_stderr(self, stream) -> None:
        for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                log.warning("ffmpeg: %s", line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_recorder.py -v`
Expected: PASS (including the real-ffmpeg test, since ffmpeg is on the dev PC)

- [ ] **Step 5: Commit**

```bash
git add raspcam/recorder.py tests/test_recorder.py
git commit -m "feat: ffmpeg supervisor with restart backoff"
```

---

### Task 5: Password hashing

**Files:**
- Create: `raspcam/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `hash_password(password: str, *, salt: bytes | None = None) -> str` (format `scrypt$N$r$p$salt_b64$hash_b64`); `verify_password(password: str, stored: str) -> bool` (never raises); `main() -> int` CLI printing `password_hash:` and `secret_key:` lines.

- [ ] **Step 1: Write the failing tests** — `tests/test_auth.py`

```python
from raspcam.auth import hash_password, verify_password


def test_roundtrip():
    stored = hash_password("letmein")
    assert stored.startswith("scrypt$")
    assert verify_password("letmein", stored)
    assert not verify_password("wrong", stored)


def test_salt_makes_hashes_differ():
    assert hash_password("same") != hash_password("same")


def test_malformed_hash_is_rejected_without_raising():
    for bad in ["", "scrypt$x", "bcrypt$1$2$3$c2FsdA==$aGFzaA==", "scrypt$a$b$c$!!$!!"]:
        assert verify_password("pw", bad) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: FAIL — `No module named 'raspcam.auth'`

- [ ] **Step 3: Implement `raspcam/auth.py`**

```python
"""Password hashing (scrypt) and a CLI that prints values for config.yaml."""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys

N, R, P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest, validate=True)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt, validate=True),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def main() -> int:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match", file=sys.stderr)
        return 1
    print(f'  password_hash: "{hash_password(password)}"')
    print(f'  secret_key: "{secrets.token_hex(32)}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add raspcam/auth.py tests/test_auth.py
git commit -m "feat: scrypt password hashing and hash CLI"
```

---

### Task 6: Web app (login, live stream, status)

**Files:**
- Create: `raspcam/web.py`, `raspcam/templates/base.html`, `raspcam/templates/login.html`, `raspcam/templates/live.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `LatestFrame` (Task 3), `verify_password` (Task 5), a `recorder_status: Callable[[], dict]` shaped like `Recorder.status()` (Task 4).
- Produces: `create_app(cfg, latest, recorder_status) -> FastAPI`; `async mjpeg_stream(latest, fps, is_disconnected) -> AsyncIterator[bytes]`; routes `GET/POST /login`, `POST /logout`, `GET /`, `GET /live.mjpg`, `GET /api/status` (JSON: `camera_online`, `last_frame_age`, plus `recorder_status()` keys).

- [ ] **Step 1: Write the failing tests** — `tests/test_web.py`

```python
import asyncio

import pytest
from fastapi.testclient import TestClient

from raspcam.auth import hash_password
from raspcam.config import parse_config
from raspcam.frames import LatestFrame
from raspcam.web import create_app, mjpeg_stream

STATUS = {"ffmpeg_running": True, "ffmpeg_uptime": 1.0, "restarts": 2, "last_error": None}


@pytest.fixture
def latest():
    return LatestFrame()


@pytest.fixture
def client(latest):
    cfg = parse_config({
        "camera": {"device": "testsrc"},
        "web": {"password_hash": hash_password("secret"), "secret_key": "k" * 32},
    })
    return TestClient(create_app(cfg, latest, lambda: STATUS))


def login(client, password="secret"):
    return client.post("/login", data={"username": "admin", "password": password},
                       follow_redirects=False)


def test_index_redirects_to_login_when_logged_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_protected_endpoints_return_401_when_logged_out(client):
    assert client.get("/live.mjpg").status_code == 401
    assert client.get("/api/status").status_code == 401


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Đăng nhập" in r.text


def test_wrong_password_is_rejected(client):
    r = login(client, "nope")
    assert r.status_code == 401
    assert "Sai tên đăng nhập hoặc mật khẩu" in r.text
    assert client.get("/api/status").status_code == 401


def test_login_gives_access_to_index_and_status(client, latest):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "/live.mjpg" in client.get("/").text
    s = client.get("/api/status").json()
    assert s["camera_online"] is False and s["last_frame_age"] is None
    assert s["restarts"] == 2
    latest.set(b"\xff\xd8x\xff\xd9")
    assert client.get("/api/status").json()["camera_online"] is True


def test_logout_ends_session(client):
    login(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/status").status_code == 401


def test_mjpeg_stream_yields_each_new_frame_once(latest):
    latest.set(b"\xff\xd8x\xff\xd9")
    answers = iter([False, False, True])

    async def is_disconnected():
        return next(answers)

    async def collect():
        return [c async for c in mjpeg_stream(latest, fps=50, is_disconnected=is_disconnected)]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1
    assert chunks[0].startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n")
    assert chunks[0].endswith(b"\xff\xd8x\xff\xd9\r\n")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: FAIL — `No module named 'raspcam.web'`

- [ ] **Step 3: Implement `raspcam/web.py`**

```python
"""FastAPI app: login, live MJPEG stream, status."""
from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import verify_password
from .config import Config
from .frames import LatestFrame

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
OFFLINE_AFTER = 5.0  # seconds without a frame before the camera counts as offline
BOUNDARY = "frame"


async def mjpeg_stream(latest: LatestFrame, fps: int,
                       is_disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[bytes]:
    """Yield each new frame once as a multipart part until the client leaves."""
    last_seq = 0
    poll = 1 / (fps * 2)
    while not await is_disconnected():
        seq, jpeg = latest.get()
        if jpeg is None or seq == last_seq:
            await asyncio.sleep(poll)
            continue
        last_seq = seq
        yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
               f"Content-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"


def create_app(cfg: Config, latest: LatestFrame,
               recorder_status: Callable[[], dict]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware,
                       secret_key=cfg.web.secret_key or secrets.token_hex(32),
                       same_site="lax", max_age=30 * 24 * 3600)

    def logged_in(request: Request) -> bool:
        return request.session.get("user") == cfg.web.username

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if username == cfg.web.username and verify_password(password, cfg.web.password_hash):
            request.session["user"] = username
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": "Sai tên đăng nhập hoặc mật khẩu"}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        return TEMPLATES.TemplateResponse(request, "live.html", {})

    @app.get("/live.mjpg")
    def live(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        return StreamingResponse(
            mjpeg_stream(latest, cfg.live.fps, request.is_disconnected),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status(request: Request):
        if not logged_in(request):
            return Response(status_code=401)
        age = latest.age()
        return {"camera_online": age is not None and age < OFFLINE_AFTER,
                "last_frame_age": age, **recorder_status()}

    return app
```

- [ ] **Step 4: Create templates**

`raspcam/templates/base.html`:

```html
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RaspCam</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f4f5; --fg:#18181b; --card:#ffffff; --muted:#71717a;
          --ok:#16a34a; --bad:#dc2626; --accent:#2563eb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#09090b; --fg:#f4f4f5; --card:#18181b; --muted:#a1a1aa; } }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; background:var(--card); }
  main { max-width:1280px; margin:0 auto; padding:16px; }
  .card { background:var(--card); border-radius:12px; padding:16px; }
  label { display:block; margin-bottom:12px; }
  input { font:inherit; width:100%; margin-top:4px; padding:8px 10px; border-radius:8px;
          border:1px solid var(--muted); background:transparent; color:inherit; }
  button { font:inherit; padding:8px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  .muted { color:var(--muted); }
</style>
</head>
<body>
{% block body %}{% endblock %}
</body>
</html>
```

`raspcam/templates/login.html`:

```html
{% extends "base.html" %}
{% block body %}
<main style="max-width:360px; margin-top:10vh">
  <form class="card" method="post" action="/login">
    <h1 style="margin-top:0">RaspCam</h1>
    {% if error %}<p style="color:var(--bad)">{{ error }}</p>{% endif %}
    <label>Tên đăng nhập<input name="username" autocomplete="username" required></label>
    <label>Mật khẩu<input name="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">Đăng nhập</button>
  </form>
</main>
{% endblock %}
```

`raspcam/templates/live.html`:

```html
{% extends "base.html" %}
{% block body %}
<header>
  <strong>RaspCam</strong>
  <form method="post" action="/logout" style="margin:0"><button type="submit">Đăng xuất</button></form>
</header>
<main>
  <div class="card">
    <img id="live" src="/live.mjpg" alt="Camera trực tiếp"
         style="width:100%; height:auto; display:block; border-radius:8px; background:#000; aspect-ratio:16/9">
    <p id="status" class="muted">Đang kết nối…</p>
  </div>
</main>
<script>
  const statusEl = document.getElementById("status");
  const img = document.getElementById("live");
  let wasDown = false;
  async function refresh() {
    try {
      const r = await fetch("/api/status");
      if (r.status === 401) { location.href = "/login"; return; }
      const s = await r.json();
      let text = s.camera_online ? "● Camera online" : "● Camera offline";
      text += ` · ffmpeg khởi động lại: ${s.restarts}`;
      if (!s.camera_online && s.last_error) text += ` · lỗi: ${s.last_error}`;
      statusEl.textContent = text;
      statusEl.style.color = s.camera_online ? "var(--ok)" : "var(--bad)";
      if (s.camera_online && wasDown) img.src = "/live.mjpg?t=" + Date.now();
      wasDown = !s.camera_online;
    } catch (e) {
      statusEl.textContent = "● Mất kết nối tới Pi";
      statusEl.style.color = "var(--bad)";
      wasDown = true;
    }
  }
  refresh();
  setInterval(refresh, 2000);
</script>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add raspcam/web.py raspcam/templates tests/test_web.py
git commit -m "feat: web app with login, live MJPEG stream, status"
```

---

### Task 7: Entry point, deployment, and on-Pi smoke test

**Files:**
- Create: `raspcam/main.py`, `raspcam/__main__.py`, `deploy/raspcam.service`, `scripts/deploy.sh`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `python -m raspcam --config PATH`; `scripts/deploy.sh` (env `PI`, `DEST` override defaults).

- [ ] **Step 1: Implement `raspcam/main.py` and `raspcam/__main__.py`**

`raspcam/main.py`:

```python
"""Wire config, recorder, and web app together."""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import load_config
from .ffmpeg_cmd import build_command
from .frames import LatestFrame
from .recorder import Recorder
from .web import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="raspcam")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    latest = LatestFrame()
    recorder = Recorder(build_command(cfg), latest)
    recorder.start()
    try:
        uvicorn.run(create_app(cfg, latest, recorder.status),
                    host=cfg.web.host, port=cfg.web.port, log_level="info")
    finally:
        recorder.stop()
```

`raspcam/__main__.py`:

```python
from .main import main

main()
```

- [ ] **Step 2: Local smoke test with the test source**

```bash
cp config.example.yaml /tmp/raspcam-dev.yaml   # then set device: testsrc, port 8090, and a hash
.venv/bin/python -m raspcam --config /tmp/raspcam-dev.yaml
```

Expected: log shows uvicorn on :8090; logging in at `http://localhost:8090` shows the moving test pattern.

- [ ] **Step 3: Create `deploy/raspcam.service`**

```ini
[Unit]
Description=RaspCam security camera
After=network-online.target
Wants=network-online.target

[Service]
User=nlt22
Group=nlt22
SupplementaryGroups=video
WorkingDirectory=/home/nlt22/raspcam
ExecStart=/home/nlt22/raspcam/.venv/bin/python -m raspcam --config /home/nlt22/raspcam/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Create `scripts/deploy.sh`** (and `chmod +x`)

```bash
#!/usr/bin/env bash
# Sync the code to the Pi, install it into a venv, and restart the service.
set -euo pipefail
PI="${PI:-nlt22@192.168.1.90}"
DEST="${DEST:-/home/nlt22/raspcam}"
cd "$(dirname "$0")/.."

rsync -az --delete \
  --exclude .git --exclude .venv --exclude config.yaml --exclude __pycache__ \
  --exclude .pytest_cache --exclude '*.egg-info' \
  ./ "$PI:$DEST/"

ssh "$PI" bash -s -- "$DEST" <<'EOF'
set -euo pipefail
cd "$1"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e .
if [ ! -f config.yaml ]; then
  echo "config.yaml missing in $1: copy config.example.yaml and run .venv/bin/python -m raspcam.auth" >&2
  exit 1
fi
sudo install -m 644 deploy/raspcam.service /etc/systemd/system/raspcam.service
sudo systemctl daemon-reload
sudo systemctl enable --quiet raspcam
sudo systemctl restart raspcam
sleep 3
systemctl is-active raspcam
EOF
```

- [ ] **Step 5: Create `README.md`** — short: what it is, dev setup (`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, `.venv/bin/pytest`), config (`python -m raspcam.auth`), deploy (`scripts/deploy.sh`), URL `http://192.168.1.90:8080`.

- [ ] **Step 6: First deploy to the Pi**

1. Run `scripts/deploy.sh` once — it installs the venv and then stops at "config.yaml missing".
2. On the Pi: `cp config.example.yaml config.yaml` and put the output of `.venv/bin/python -m raspcam.auth` (password chosen by the user) into the `web` section.
3. Run `scripts/deploy.sh` again. Expected final line: `active`.

- [ ] **Step 7: Smoke test on the Pi**

- `curl -s -o /dev/null -w '%{http_code}' http://192.168.1.90:8080/` → `303`
- Log in with curl (cookie jar), read `/api/status` → `camera_online: true`, `restarts: 0`.
- Read ~3 s of `/live.mjpg` → ≥10 frames.
- `ssh pi 'top -bn1 | head -15; vcgencmd measure_temp'` → ffmpeg CPU noted (expected well under 100% of one core).
- Unplug/replug camera (or `systemctl restart raspcam`) → stream recovers.

- [ ] **Step 8: Run full test suite and commit**

Run: `.venv/bin/pytest -v` → all PASS

```bash
git add raspcam/main.py raspcam/__main__.py deploy scripts README.md
git commit -m "feat: entry point, systemd unit, and deploy script"
git push
```

---

## Later milestones (separate plans)

- M2: H.264 segment recording + SQLite + reconcile + playback UI.
- M3: Motion detection + events UI + motion/both modes.
- M4: Retention (quota, min free, motion-mode pruning) + `require_mount` + status page details.
