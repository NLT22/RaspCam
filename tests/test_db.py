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
