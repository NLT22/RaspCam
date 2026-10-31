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
