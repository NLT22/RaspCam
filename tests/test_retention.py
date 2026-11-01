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


def test_housekeeper_deletes_thumbnails_with_segments(tmp_path):
    store = SegmentStore(tmp_path / "db.sqlite")
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    first, second = segs(GB, GB)
    for s in (first, second):
        store.add(s)
        (tmp_path / s.name).write_bytes(b"x")
        (thumbs / s.name.replace(".mp4", ".jpg")).write_bytes(b"j")
    hk = Housekeeper(store, FakeIndexer(), tmp_path, StorageConfig(quota_gb=1.5, min_free_gb=0),
                     writing=lambda: True, disk_free=lambda p: 100 * GB, thumbs_dir=thumbs)
    hk.run_once()
    assert not (thumbs / first.name.replace(".mp4", ".jpg")).exists()
    assert (thumbs / second.name.replace(".mp4", ".jpg")).exists()


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
