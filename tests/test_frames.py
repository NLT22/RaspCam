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
