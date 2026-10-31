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
