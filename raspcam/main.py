"""Wire config, recorder, and web app together."""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import load_config
from .ffmpeg_cmd import build_command
from .frames import LatestFrame
from .recorder import Recorder
from .web import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="raspcam")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    latest = LatestFrame()
    recorder = Recorder(build_command(cfg), latest)
    recorder.start()
    try:
        uvicorn.run(create_app(cfg, latest, recorder.status),
                    host=cfg.web.host, port=cfg.web.port, log_level="info")
    finally:
        recorder.stop()
