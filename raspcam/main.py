"""Wire config, recorder, motion detection, housekeeping, and web app together."""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import uvicorn

from .config import load_config
from .db import EventStore, SegmentStore, local_day
from .ffmpeg_cmd import build_command
from .frames import LatestFrame
from .motion import MotionMonitor
from .recorder import Recorder
from .retention import Housekeeper
from .segments import Indexer
from .web import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="raspcam")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    data_dir = Path(cfg.storage.path).expanduser()
    segments_dir = data_dir / "segments"
    thumbs_dir = data_dir / "thumbs"
    events_dir = data_dir / "events"
    segments_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(exist_ok=True)
    events_dir.mkdir(exist_ok=True)
    store = SegmentStore(data_dir / "raspcam.db")
    events = EventStore(data_dir / "raspcam.db")
    events.close_dangling()
    detect = cfg.recording.mode != "continuous"

    latest = LatestFrame()
    monitor = MotionMonitor(cfg.motion, events, latest, events_dir) if detect else None
    recorder = Recorder(lambda fd: build_command(cfg, segments_dir, fd), latest,
                        on_motion_frame=monitor.submit if monitor else None,
                        motion_frame_bytes=cfg.motion.width * cfg.motion.height)
    housekeeper = Housekeeper(
        store, Indexer(store, segments_dir, thumbs_dir=thumbs_dir), segments_dir, cfg.storage,
        writing=lambda: recorder.running, thumbs_dir=thumbs_dir,
        events=events, mode=cfg.recording.mode, events_dir=events_dir,
        padding_seconds=cfg.motion.padding_segments * cfg.recording.segment_seconds)

    def status() -> dict:
        return {**recorder.status(),
                "motion": {"enabled": detect,
                           "active": bool(monitor and monitor.active),
                           "events_today": events.count_day(local_day(time.time()))}}

    housekeeper.run_once()  # reconcile files left by a previous run before ffmpeg starts
    if monitor:
        monitor.start()
    recorder.start()
    housekeeper.start()

    def shutdown() -> None:
        housekeeper.stop()
        recorder.stop()  # one SIGTERM, then wait: ffmpeg finishes the current segment
        if monitor:
            monitor.stop()  # closes an event that is still going

    try:
        uvicorn.run(create_app(cfg, latest, status, store, segments_dir,
                               thumbs_dir=thumbs_dir, events=events, events_dir=events_dir,
                               on_shutdown=shutdown),
                    host=cfg.web.host, port=cfg.web.port, log_level="info")
    finally:
        shutdown()  # if uvicorn exited without a lifespan shutdown (e.g. startup error)
