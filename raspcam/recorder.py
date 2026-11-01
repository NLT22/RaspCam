"""Run ffmpeg, restart it when it dies, and feed live frames to LatestFrame."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from typing import Callable

from .frames import LatestFrame
from .mjpeg import MjpegSplitter

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, command: list[str] | Callable[[int | None], list[str]],
                 latest: LatestFrame, *,
                 on_motion_frame: Callable[[bytes, float], None] | None = None,
                 motion_frame_bytes: int = 0,
                 backoff_initial: float = 1.0, backoff_max: float = 30.0,
                 stable_after: float = 60.0):
        self._command = command  # a factory gets the motion pipe's fd (or None)
        self._on_motion = on_motion_frame
        self._motion_bytes = motion_frame_bytes
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

    def stop(self, timeout: float = 15.0) -> None:
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
        motion_r = motion_w = None
        if self._on_motion:
            motion_r, motion_w = os.pipe()
        argv = self._command(motion_w) if callable(self._command) else self._command
        try:
            # Own session: a terminal's Ctrl+C must not reach ffmpeg as well as us.
            # ffmpeg 7 aborts without writing the MP4 trailer on a second signal,
            # so only stop() may signal it (systemd side: KillMode=mixed).
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True,
                                    pass_fds=(motion_w,) if motion_w is not None else ())
        except OSError as e:
            if motion_r is not None:
                os.close(motion_r)
            self._stderr_tail.append(str(e))
            log.error("cannot start ffmpeg: %s", e)
            return
        finally:
            if motion_w is not None:
                os.close(motion_w)  # the child has its own copy; we get EOF when it exits
        with self._lock:
            self._proc = proc
            self._started_at = time.monotonic()
        if self._stop.is_set():  # stop() ran before _proc was visible to it
            proc.terminate()
        readers = [threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True)]
        if motion_r is not None:
            readers.append(threading.Thread(target=self._read_motion, args=(motion_r,), daemon=True))
        for t in readers:
            t.start()
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
            for t in readers:
                t.join(timeout=1)
            with self._lock:
                self._proc = None

    def _read_motion(self, fd: int) -> None:
        with open(fd, "rb") as stream:
            while len(frame := stream.read(self._motion_bytes)) == self._motion_bytes:
                try:
                    self._on_motion(frame, time.time())
                except Exception:  # never stop draining, or ffmpeg would block
                    log.exception("motion frame handler failed")

    def _drain_stderr(self, stream) -> None:
        for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                log.warning("ffmpeg: %s", line)
