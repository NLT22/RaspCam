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


def test_child_runs_in_its_own_session(latest):
    # Ctrl+C in a terminal must not reach ffmpeg directly (see test below)
    import os
    rec = Recorder(py("import os, sys, time\n"
                      "sys.stderr.write(f'pgid={os.getpgid(0)}\\n'); sys.stderr.flush()\n"
                      "time.sleep(30)\n"), latest)
    rec.start()
    try:
        assert wait_until(lambda: (rec.status()["last_error"] or "").startswith("pgid="))
        assert rec.status()["last_error"] != f"pgid={os.getpgid(0)}"
    finally:
        rec.stop()


def test_stop_signals_child_exactly_once(latest):
    # ffmpeg 7 aborts without writing the MP4 trailer on a second SIGTERM
    rec = Recorder(py("import signal, sys, time\n"
                      "n = 0\n"
                      "def on_term(*_):\n"
                      "    global n\n"
                      "    n += 1\n"
                      "    sys.stderr.write(f'term{n}\\n'); sys.stderr.flush()\n"
                      "signal.signal(signal.SIGTERM, on_term)\n"
                      "sys.stderr.write('ready\\n'); sys.stderr.flush()\n"
                      "while n == 0: time.sleep(0.01)\n"
                      "time.sleep(0.5)\n"), latest)
    rec.start()
    assert wait_until(lambda: rec.status()["last_error"] == "ready")
    rec.stop()
    assert rec.status()["last_error"] == "term1"


def small_cfg():
    return parse_config({
        "camera": {"device": "testsrc", "width": 320, "height": 180, "input_fps": 10},
        "live": {"width": 160, "height": 90, "fps": 5},
        "recording": {"fps": 5, "segment_seconds": 1, "keyframe_seconds": 1},
        "web": {"password_hash": "scrypt$x"},
    })


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
    full = store.oldest_first()[1:-1]  # first/last may be partial
    assert all(0.5 <= s.duration <= 1.5 for s in full)
