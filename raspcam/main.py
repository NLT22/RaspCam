"""Wire config, recorder, housekeeping, and web app together."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .config import load_config
from .db import SegmentStore
from .ffmpeg_cmd import build_command
from .frames import LatestFrame
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
    segments_dir.mkdir(parents=True, exist_ok=True)
    store = SegmentStore(data_dir / "raspcam.db")

    latest = LatestFrame()
    recorder = Recorder(build_command(cfg, segments_dir), latest)
    housekeeper = Housekeeper(store, Indexer(store, segments_dir), segments_dir,
                              cfg.storage, writing=lambda: recorder.running)
    housekeeper.run_once()  # reconcile files left by a previous run before ffmpeg starts
    recorder.start()
    housekeeper.start()
    try:
        uvicorn.run(create_app(cfg, latest, recorder.status, store, segments_dir),
                    host=cfg.web.host, port=cfg.web.port, log_level="info")
    finally:
        housekeeper.stop()
        recorder.stop()
