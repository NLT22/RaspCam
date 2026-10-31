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
