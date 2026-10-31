"""Split a concatenated JPEG byte stream (ffmpeg -f mjpeg) into frames."""
from __future__ import annotations

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


class MjpegSplitter:
    def __init__(self, max_buffer: int = 8 * 1024 * 1024):
        self._buf = bytearray()
        self._max = max_buffer

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes; return the complete frames they finish."""
        self._buf += data
        frames = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                # keep a trailing 0xFF: it may be the first half of the next SOI
                keep = 1 if self._buf.endswith(b"\xff") else 0
                del self._buf[:len(self._buf) - keep]
                break
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                del self._buf[:start]
                break
            frames.append(bytes(self._buf[start:end + 2]))
            del self._buf[:end + 2]
        if len(self._buf) > self._max:
            self._buf.clear()
        return frames
