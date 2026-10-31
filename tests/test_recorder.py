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
