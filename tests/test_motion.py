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
