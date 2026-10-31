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
