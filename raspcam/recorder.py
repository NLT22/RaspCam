"""Run ffmpeg, restart it when it dies, and feed live frames to LatestFrame."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque

from .frames import LatestFrame
from .mjpeg import MjpegSplitter

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, argv: list[str], latest: LatestFrame, *,
                 backoff_initial: float = 1.0, backoff_max: float = 30.0,
                 stable_after: float = 60.0):
        self._argv = argv
        self._latest = latest
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._stable_after = stable_after  # a run this long resets the backoff
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self.restarts = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            started = self._started_at
        return {
            "ffmpeg_running": running,
            "ffmpeg_uptime": time.monotonic() - started if running and started else None,
            "restarts": self.restarts,
            "last_error": self._stderr_tail[-1] if self._stderr_tail else None,
        }

    def _run(self) -> None:
        delay = self._backoff_initial
        while not self._stop.is_set():
            started = time.monotonic()
            self._run_once()
            if self._stop.is_set():
                break
            if time.monotonic() - started >= self._stable_after:
                delay = self._backoff_initial
            self.restarts += 1
            log.warning("ffmpeg exited; restarting in %.1fs", delay)
            self._stop.wait(delay)
            delay = min(delay * 2, self._backoff_max)

    def _run_once(self) -> None:
        try:
            proc = subprocess.Popen(self._argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self._stderr_tail.append(str(e))
            log.error("cannot start ffmpeg: %s", e)
            return
        with self._lock:
            self._proc = proc
            self._started_at = time.monotonic()
        if self._stop.is_set():  # stop() ran before _proc was visible to it
            proc.terminate()
        err = threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True)
        err.start()
        splitter = MjpegSplitter()
        try:
            while chunk := proc.stdout.read1(65536):
                for frame in splitter.feed(chunk):
                    self._latest.set(frame)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            err.join(timeout=1)
            with self._lock:
                self._proc = None

    def _drain_stderr(self, stream) -> None:
        for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                log.warning("ffmpeg: %s", line)
